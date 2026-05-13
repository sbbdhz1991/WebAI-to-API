# src/app/services/gemini_patch.py
"""Monkey-patches that fix gemini-webapi's incompatibility with Google's
current Gemini media upload + chat protocol (pinned at 2.0.0 in
requirements.txt).

==============================================================================
WHY THIS EXISTS
==============================================================================
gemini-webapi 2.0.0 implements the chat protocol against a snapshot of
gemini.google.com's internal API from spring 2026. Since then Google has
changed both the upload endpoint and several fields in the StreamGenerate
form payload. The library still works for text-only chats (the bits that
didn't change), but ANY request that attaches a file silently fails:

  - Upload returns a URI, but the model layer rejects it with
    ``APIError 1099`` ("Failed to generate contents").
  - With protocol fixes applied, ``API 1099`` goes away but the response
    stream stays silent until the library's 120s watchdog fires.

Patches in this module were reverse-engineered by capturing the live
browser request flow via Fiddler (.saz captures), diffing field-by-field
against what the library sends, and patching the differences.

==============================================================================
WHAT THIS MODULE PATCHES
==============================================================================

1. ``upload_file`` (replace entirely)
   ---------------------------------
   Library posts a single multipart/form-data POST to a
   ``content-push.googleusercontent.com`` endpoint. Browser does a two-step
   **resumable** upload:
     Step A: POST ``push.clients6.google.com/upload/`` with
       headers
         ``X-Goog-Upload-Protocol: resumable``
         ``X-Goog-Upload-Command: start``
         ``X-Goog-Upload-Header-Content-Length: <file_size>``
         ``X-Tenant-Id: bard-storage``
         ``Push-ID: feeds/<random13>``
       body ``File name: <filename>``
     → response ``X-Goog-Upload-URL`` to use for step B
     Step B: POST that URL with
         ``X-Goog-Upload-Command: upload, finalize``
         ``X-Goog-Upload-Offset: 0``
         + raw file bytes
     → response body is the ``/contrib_service/ttl_1d/...`` URI

   See ``upload_file_resumable``.

2. ``inject_extra_cookies`` (config-driven)
   ----------------------------------------
   Library only takes ``__Secure-1PSID`` and ``__Secure-1PSIDTS`` at init,
   then bootstraps any other cookies it needs by hitting gemini.google.com.
   But the upload endpoint expects SAPISID-family cookies (``SAPISID``,
   ``__Secure-1PAPISID``, ``__Secure-3PAPISID``, ``HSID``, ``SSID``,
   ``APISID``) which the bootstrap doesn't reliably set. We let the user
   paste a full ``Cookie:`` header from their browser into
   ``config.conf::[Cookies].gemini_cookie_extra`` and load all of them
   into the curl_cffi session jar after init.

3. StreamGenerate body rewrite (interceptor on session.request/post)
   ----------------------------------------------------------------
   The library wraps each uploaded file as a 2-element array
   ``[[url], filename]``. The browser sends a 9-element shape:
     ``[[url, kind, null, mime], filename, null, null, null, null, null, null, [0]]``

   ``kind`` is media-type-dependent:
     - 1 = image (verified)
     - 2 = video (verified)
     - 2 = others (fallback; PDF/audio not captured yet)

   The browser also pads the outer ``inner`` array (the JSON-string at
   ``outer[1]``) to length 81 with two media-type-dependent slots:
     - inner[67]: 0 for image, null for video
     - inner[80]: 1 for image, 3 for video  (terminator value)

   The library terminates at index 68 (value 2). Padding to 81 with the
   right terminator is mandatory for attachment requests.

   See ``_rewrite_freq_value`` and ``_MEDIA_PROFILES``.

4. StreamGenerate header rewrite
   -----------------------------
   On attachment requests, the library's headers cause Google to either
   stall the response or return ``INVALID_ARGUMENT 400``. Verified fixes:
     - DROP ``x-goog-ext-525001261-jspb`` entirely. Library emits a
       12-element jspb array ending with ``2``; keeping it breaks
       attachment requests.
     - Rewrite ``x-goog-ext-73010990-jspb`` from ``[0]`` to ``[0,0,0]``.
   Other ext headers are left untouched (browser/library agree on them).

==============================================================================
DIAGNOSTIC TOGGLE
==============================================================================
Set env var ``WEBAI_DEBUG_DUMP_REQUEST=1`` to enable a WARN-level dump of
the final outgoing StreamGenerate request (URL + headers + Cookie + body).
**Default off** because it logs full live cookies. Enable only for one-off
local debugging.

==============================================================================
MAINTENANCE NOTES
==============================================================================
- The constants here are version-pinned to gemini-webapi 2.0.0. If
  upgrading the library, re-run a Fiddler/HAR capture of a successful
  browser request and re-diff before trusting these patches.
- Google occasionally rotates URL endpoints, header names, and array
  layouts. Symptoms of stale patches: APIError 1099, silent stream
  timeouts, 400 INVALID_ARGUMENT.
- The library's hardcoded ``push_id='feeds/mcudyrk2a4khkz'`` (from
  someone's old capture) is accepted by Google verbatim — we don't
  override it. If that ever stops working, generate a fresh feeds/<rand>.
"""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import secrets
import string
import urllib.parse
from functools import wraps
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

logger = logging.getLogger("app")

# When set to "1"/"true", dump the entire outgoing StreamGenerate request
# (URL + headers + cookies + body) at WARN level so it can be replayed via
# curl. Sensitive — leave off unless actively debugging.
_DEBUG_DUMP_REQUEST = os.environ.get("WEBAI_DEBUG_DUMP_REQUEST", "").lower() in (
    "1", "true", "yes", "on"
)

# Override the library's stream-idle watchdog timeout. Default in
# gemini-webapi 2.0.0 is 120s. For long-running media analyses the stream
# may pause longer than 120s; bumping this gives Google more time to send
# the trailing frame. Set to 0 to keep library default.
_WATCHDOG_TIMEOUT_SECONDS = int(
    os.environ.get("WEBAI_WATCHDOG_TIMEOUT", "0") or "0"
)

_UPLOAD_INIT_URL = "https://push.clients6.google.com/upload/"
_TENANT_ID = "bard-storage"
_ORIGIN = "https://gemini.google.com"


def _random_push_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    rand = "".join(secrets.choice(alphabet) for _ in range(13))
    return f"feeds/{rand}"


async def upload_file_resumable(
    file: Union[str, Path, bytes, io.BytesIO],
    client: Any,
    push_id: str = "",
    filename: Optional[str] = None,
    verbose: bool = False,
) -> str:
    """Two-step resumable upload matching the Gemini web client.

    Signature is intentionally compatible with the original
    ``gemini_webapi.utils.upload_file`` so the call sites inside
    gemini-webapi keep working after the monkey-patch.
    """
    # Normalize input to (bytes, filename)
    logger.debug(
        f"[patch] upload_file_resumable invoked: file_type={type(file).__name__}, "
        f"push_id={push_id!r}, filename_arg={filename!r}"
    )
    if isinstance(file, (str, Path)):
        p = Path(file)
        if not p.is_file():
            raise ValueError(f"{p} is not a valid file.")
        if not filename:
            filename = p.name
        data = p.read_bytes()
    elif isinstance(file, io.BytesIO):
        data = file.getvalue()
        if not filename:
            filename = getattr(file, "name", None) or "upload.bin"
    elif isinstance(file, (bytes, bytearray)):
        data = bytes(file)
        if not filename:
            filename = "upload.bin"
    else:
        raise ValueError(f"Unsupported file type: {type(file)}")

    if not push_id:
        push_id = _random_push_id()
    elif not push_id.startswith("feeds/"):
        push_id = f"feeds/{push_id}"

    base_headers = {
        "X-Tenant-Id": _TENANT_ID,
        "Push-ID": push_id,
        "Origin": _ORIGIN,
        "Referer": _ORIGIN + "/",
    }

    # Step 1 — start the resumable upload.
    init_headers = {
        **base_headers,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    }
    r1 = await client.post(
        _UPLOAD_INIT_URL,
        headers=init_headers,
        data=f"File name: {filename}",
        allow_redirects=True,
    )
    if r1.status_code != 200:
        raise RuntimeError(
            f"resumable upload init failed: HTTP {r1.status_code}, "
            f"body={(r1.text or '')[:300]!r}"
        )
    upload_url = (
        r1.headers.get("X-Goog-Upload-URL")
        or r1.headers.get("x-goog-upload-url")
    )
    if not upload_url:
        raise RuntimeError(
            f"resumable upload init: missing X-Goog-Upload-URL header; "
            f"headers={dict(r1.headers)!r}"
        )

    # Step 2 — push bytes and finalize.
    finalize_headers = {
        **base_headers,
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    r2 = await client.post(
        upload_url,
        headers=finalize_headers,
        data=data,
        allow_redirects=True,
    )
    if r2.status_code != 200:
        raise RuntimeError(
            f"resumable upload finalize failed: HTTP {r2.status_code}, "
            f"body={(r2.text or '')[:300]!r}"
        )
    file_uri = (r2.text or "").strip()
    if not file_uri.startswith("/contrib_service/"):
        raise RuntimeError(
            f"resumable upload finalize: unexpected response body: "
            f"{file_uri[:200]!r}"
        )
    logger.debug(
        f"[patch] resumable upload OK: filename={filename!r} bytes={len(data)} "
        f"uri={file_uri}"
    )
    return file_uri


# ---------------------------------------------------------------------------
# Extra-cookie injection
# ---------------------------------------------------------------------------


def _parse_cookie_header(raw: str) -> List[Tuple[str, str]]:
    """Parse a ``Cookie:`` header value into [(name, value), ...]."""
    if not raw:
        return []
    # SimpleCookie chokes on some values (e.g. unquoted commas in PAPISID);
    # fall back to manual split.
    try:
        sc: SimpleCookie = SimpleCookie()
        sc.load(raw)
        pairs = [(k, m.value) for k, m in sc.items()]
        if pairs:
            return pairs
    except Exception:
        pass
    out: List[Tuple[str, str]] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        out.append((k.strip(), v.strip()))
    return out


def _find_session(client_wrapper: Any) -> Optional[Any]:
    """Locate the underlying curl_cffi AsyncSession through whatever attr
    name the installed gemini-webapi version exposes it as."""
    for attr in ("client", "_client", "session", "_session", "http"):
        cand = getattr(client_wrapper, attr, None)
        if cand is not None and hasattr(cand, "cookies"):
            return cand
    return None


def inject_extra_cookies(client_wrapper: Any, raw_cookie_header: str) -> int:
    """Inject cookies parsed from a Cookie-header string into the
    underlying HTTP session.

    Returns the number of cookies actually added.
    """
    pairs = _parse_cookie_header(raw_cookie_header)
    if not pairs:
        return 0

    sess = _find_session(client_wrapper)
    if sess is None:
        logger.warning(
            "inject_extra_cookies: could not locate underlying HTTP session"
        )
        return 0

    jar = sess.cookies
    n = 0
    for k, v in pairs:
        added = False
        # curl_cffi's Cookies object exposes .set(name, value, domain=...)
        for setter in (
            lambda: jar.set(k, v, domain=".google.com"),
            lambda: jar.set(k, v),
            lambda: jar.update({k: v}),
            lambda: jar.__setitem__(k, v),
        ):
            try:
                setter()
                added = True
                break
            except Exception:
                continue
        if added:
            n += 1
    return n


# ---------------------------------------------------------------------------
# StreamGenerate body interceptor — fixes file-ref shape mismatch
# ---------------------------------------------------------------------------
#
# gemini-webapi 2.0.0 wraps an uploaded file as the 2-element form:
#     [[url], filename]
# Browser DevTools shows the live server expecting the 9-element form:
#     [[url, 2, null, mime], filename, null, null, null, null, null, null, [0]]
# Sending the 2-element form makes Gemini's model layer reject the request
# with APIError 1099 even though the upload succeeded. We hook the
# AsyncSession.post call for StreamGenerate URLs and rewrite the body just
# before it goes out so the library's code stays untouched.


# Per-media-type magic numbers reverse-engineered from live browser captures.
# Each tuple is (file_ref_kind, inner_index_67, inner_terminator_80).
# file_ref_kind goes into the inner file_ref array at position 1.
# inner_index_67 / inner_terminator_80 go into the outer inner-JSON array.
_MEDIA_PROFILES = {
    "image": (1, 0, 1),
    "video": (2, None, 3),
    "audio": (2, None, 3),     # placeholder, no capture yet
    "application": (2, None, 3),  # PDF, etc. — placeholder
    "text": (2, None, 3),      # placeholder
    "default": (2, None, 3),
}


def _profile_for_mime(mime: str) -> Tuple[int, Optional[int], int]:
    top = (mime or "").split("/", 1)[0].lower()
    return _MEDIA_PROFILES.get(top, _MEDIA_PROFILES["default"])


def _rewrite_file_refs(file_refs: List[Any]) -> Tuple[List[Any], Optional[str]]:
    """Convert each ``[[url], filename]`` entry into the 9-tuple shape.

    Also returns the MIME of the first attachment so the caller can adjust
    media-type-dependent indices in the outer array.
    """
    if not isinstance(file_refs, list):
        return file_refs, None
    out: List[Any] = []
    first_mime: Optional[str] = None
    for ref in file_refs:
        if (
            isinstance(ref, list)
            and len(ref) >= 2
            and isinstance(ref[0], list)
            and len(ref[0]) >= 1
            and isinstance(ref[0][0], str)
            and ref[0][0].startswith("/contrib_service/")
        ):
            url = ref[0][0]
            filename = ref[1] if isinstance(ref[1], str) else "upload.bin"
            mime = (
                mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
            kind_code, _, _ = _profile_for_mime(mime)
            if first_mime is None:
                first_mime = mime
            out.append(
                [
                    [url, kind_code, None, mime],
                    filename,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [0],
                ]
            )
        else:
            out.append(ref)
    return out, first_mime


def _rewrite_freq_value(v: str) -> Tuple[str, bool]:
    """Given the raw ``f.req`` value (already URL-decoded), return
    ``(new_v, changed)`` with file refs reshaped to the 9-tuple form."""
    try:
        outer = json.loads(v)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug(f"[patch] f.req outer json parse failed: {e}")
        return v, False
    if not (isinstance(outer, list) and len(outer) >= 2 and isinstance(outer[1], str)):
        return v, False
    try:
        inner = json.loads(outer[1])
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug(f"[patch] f.req inner json parse failed: {e}")
        return v, False
    if not (
        isinstance(inner, list)
        and len(inner) >= 1
        and isinstance(inner[0], list)
        and len(inner[0]) >= 4
        and isinstance(inner[0][3], list)
        and inner[0][3]
    ):
        # No file refs to rewrite — text-only request.
        return v, False
    old_refs = inner[0][3]
    new_refs, first_mime = _rewrite_file_refs(old_refs)
    if new_refs == old_refs:
        return v, False
    inner[0][3] = new_refs

    # Browser-shaped inner array has length 81, with two media-type-dependent
    # slots at indices [67] and [80]. The library terminates at index 68 with
    # value 2; pad up to 81 and set the type-specific values.
    BROWSER_TAIL_LENGTH = 81
    _, idx67, idx80 = _profile_for_mime(first_mime or "")
    if len(inner) < BROWSER_TAIL_LENGTH:
        original_len = len(inner)
        inner.extend([None] * (BROWSER_TAIL_LENGTH - len(inner)))
        logger.debug(
            f"[patch] padded inner array {original_len} -> {len(inner)}"
        )
    if idx67 is not None:
        inner[67] = idx67
    inner[80] = idx80

    # Note: inner[3]/inner[4] left as null. Direct replay testing shows
    # nulls are acceptable for new conversations.

    logger.debug(
        f"[patch] media profile: mime={first_mime!r} -> "
        f"file_ref_kind={new_refs[0][0][1] if new_refs else '?'}, "
        f"inner[67]={inner[67]!r}, inner[80]={inner[80]!r}"
    )

    outer[1] = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    new_v = json.dumps(outer, ensure_ascii=False, separators=(",", ":"))
    logger.debug(
        f"[patch] rewrote {len(new_refs)} file ref(s) in StreamGenerate body"
    )
    return new_v, True


def _rewrite_streamgen_body(body: Any) -> Any:
    """Walk the form body and reshape any file refs inside ``f.req``.

    Handles dict, str, and bytes body shapes (different HTTP client
    versions pass these differently)."""
    if body is None:
        return body

    # ---------- dict (curl_cffi auto form-encodes this) ----------
    if isinstance(body, dict):
        if "f.req" in body and isinstance(body["f.req"], str):
            new_v, changed = _rewrite_freq_value(body["f.req"])
            if changed:
                # mutate a copy to avoid surprising upstream code
                new_body = dict(body)
                new_body["f.req"] = new_v
                return new_body
        return body

    # ---------- list of (k, v) pairs ----------
    if isinstance(body, list) and body and isinstance(body[0], (tuple, list)):
        changed = False
        new_pairs = []
        for k, v in body:
            if k == "f.req" and isinstance(v, str):
                new_v, c = _rewrite_freq_value(v)
                if c:
                    new_pairs.append((k, new_v))
                    changed = True
                    continue
            new_pairs.append((k, v))
        return new_pairs if changed else body

    # ---------- str / bytes (already URL-encoded form body) ----------
    if isinstance(body, bytes):
        try:
            body_str = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        was_bytes = True
    elif isinstance(body, str):
        body_str = body
        was_bytes = False
    else:
        logger.debug(
            f"[patch] streamgen body of unknown type {type(body).__name__}; "
            f"leaving untouched"
        )
        return body

    if "f.req=" not in body_str:
        return body
    try:
        pairs = urllib.parse.parse_qsl(body_str, keep_blank_values=True)
    except Exception:
        return body
    changed = False
    new_pairs: List[Tuple[str, str]] = []
    for k, v in pairs:
        if k == "f.req":
            new_v, c = _rewrite_freq_value(v)
            if c:
                v = new_v
                changed = True
        new_pairs.append((k, v))
    if not changed:
        return body
    new_body_str = urllib.parse.urlencode(new_pairs)
    return new_body_str.encode("utf-8") if was_bytes else new_body_str


_BODY_KWARGS = ("data", "content", "body", "json")


def _rewrite_streamgen_headers(headers: Any) -> Any:
    """Adjust the ``x-goog-ext-*-jspb`` headers to match what Google
    accepts when an attachment is present.

    - ``x-goog-ext-525001261-jspb`` must be **dropped entirely** for
      attachment requests; keeping the library's value (or extending it)
      makes the server stall or return errors.
    - ``x-goog-ext-73010990-jspb`` the library sends ``[0]``; the browser
      sends ``[0,0,0]``.
    """
    if not isinstance(headers, dict):
        return headers

    new_headers = dict(headers)
    dropped = False
    fixed_73010990 = False

    # Drop x-goog-ext-525001261-jspb entirely (case-insensitive).
    for k in list(new_headers.keys()):
        if str(k).lower() == "x-goog-ext-525001261-jspb":
            del new_headers[k]
            dropped = True

    # Fix x-goog-ext-73010990-jspb shape.
    for k in list(new_headers.keys()):
        if str(k).lower() == "x-goog-ext-73010990-jspb":
            v = new_headers[k]
            if isinstance(v, str):
                try:
                    arr = json.loads(v)
                    if isinstance(arr, list) and len(arr) == 1 and arr[0] == 0:
                        new_headers[k] = "[0,0,0]"
                        fixed_73010990 = True
                except Exception as e:
                    logger.debug(f"[patch] 73010990-jspb rewrite skipped: {e}")
            break

    if dropped or fixed_73010990:
        logger.debug(
            f"[patch] x-goog-ext headers adjusted "
            f"(dropped_525001261={dropped}, fixed_73010990={fixed_73010990})"
        )
        return new_headers
    return headers


def _body_carries_attachment(body: Any) -> bool:
    """Return True if the body's f.req contains a /contrib_service/ URL."""
    if isinstance(body, dict):
        v = body.get("f.req")
    elif isinstance(body, (str, bytes)):
        v = body
    else:
        return False
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(v, str):
        return False
    return "/contrib_service/" in v


def _try_rewrite_body_in_call(args: tuple, kwargs: dict) -> tuple:
    """Find the body arg (kwarg or positional after url) and rewrite it
    if it carries a StreamGenerate-shaped form body. Also extends the
    media-related x-goog-ext headers when an attachment is present."""
    body_key: Optional[str] = None
    body_val: Any = None
    for k in _BODY_KWARGS:
        if k in kwargs and kwargs[k] is not None:
            new_v = _rewrite_streamgen_body(kwargs[k])
            if new_v is not kwargs[k]:
                kwargs[k] = new_v
            body_key, body_val = k, kwargs[k]
            break
    if body_key is None and args:
        try:
            new_v = _rewrite_streamgen_body(args[0])
            if new_v is not args[0]:
                args = (new_v,) + args[1:]
            body_val = args[0] if args else None
        except Exception:
            pass

    # If the request carries an attachment, also extend the related
    # x-goog-ext-* headers in-place.
    if _body_carries_attachment(body_val) and "headers" in kwargs:
        kwargs["headers"] = _rewrite_streamgen_headers(kwargs["headers"])

    return args, kwargs


def install_streamgen_interceptor(client_wrapper: Any) -> bool:
    """Wrap both ``post`` and ``request`` on the underlying session so
    StreamGenerate file refs get reshaped on the fly regardless of which
    entry point gemini-webapi uses internally. Idempotent."""
    sess = _find_session(client_wrapper)
    if sess is None:
        logger.warning(
            "[patch] install_streamgen_interceptor: no session found"
        )
        return False
    if getattr(sess, "_streamgen_patched", False):
        return True

    def _make_post_wrapper(orig):
        @wraps(orig)
        async def patched(url, *args, **kwargs):
            url_str = str(url) if url else ""
            if "StreamGenerate" in url_str:
                logger.debug(
                    f"[patch] StreamGenerate via .post intercepted "
                    f"(positional={len(args)}, kwargs={list(kwargs.keys())})"
                )
                try:
                    args, kwargs = _try_rewrite_body_in_call(args, kwargs)
                except Exception as e:
                    logger.warning(f"[patch] post rewrite error: {e}")
            return await orig(url, *args, **kwargs)
        return patched

    def _make_request_wrapper(orig):
        @wraps(orig)
        async def patched(method, url, *args, **kwargs):
            url_str = str(url) if url else ""
            if (
                str(method).upper() == "POST"
                and "StreamGenerate" in url_str
            ):
                body_kw = next((k for k in _BODY_KWARGS if k in kwargs), None)
                body_type = (
                    type(kwargs[body_kw]).__name__ if body_kw else "n/a"
                )
                logger.debug(
                    f"[patch] StreamGenerate via .request intercepted "
                    f"(positional={len(args)}, kwargs={list(kwargs.keys())}, "
                    f"body_kw={body_kw}, body_type={body_type})"
                )
                try:
                    args, kwargs = _try_rewrite_body_in_call(args, kwargs)
                except Exception as e:
                    logger.warning(f"[patch] request rewrite error: {e}")

                # Dump the FINAL outgoing request (URL + headers + body)
                # verbatim — including Cookie — so it can be replayed.
                # WARN level for easy grep. Effective cookies from the
                # underlying session jar are appended in case the library
                # didn't put them on the call directly.
                # Gated by WEBAI_DEBUG_DUMP_REQUEST env var; default OFF
                # because the dump contains live session cookies.
                if not _DEBUG_DUMP_REQUEST:
                    return await orig(method, url, *args, **kwargs)
                try:
                    final_params = kwargs.get("params") or {}
                    if isinstance(final_params, dict) and final_params:
                        from urllib.parse import urlencode as _ue
                        sep = "&" if "?" in url_str else "?"
                        full_url = url_str + sep + _ue(final_params)
                    else:
                        full_url = url_str

                    final_headers = dict(kwargs.get("headers") or {})

                    # If Cookie wasn't on the call headers, build it from the
                    # session jar (curl_cffi uses the jar automatically; we
                    # surface it here so the log is replay-ready).
                    has_cookie_header = any(
                        str(k).lower() == "cookie" for k in final_headers
                    )
                    if not has_cookie_header:
                        try:
                            jar = sess.cookies
                            pairs: List[str] = []
                            iter_obj = (
                                jar.items() if hasattr(jar, "items") else jar
                            )
                            for item in iter_obj:
                                if isinstance(item, tuple) and len(item) == 2:
                                    pairs.append(f"{item[0]}={item[1]}")
                                else:
                                    name = getattr(item, "name", None)
                                    value = getattr(item, "value", None)
                                    if name and value is not None:
                                        pairs.append(f"{name}={value}")
                            if pairs:
                                final_headers["Cookie"] = "; ".join(pairs)
                        except Exception as ce:
                            logger.debug(f"[patch] jar->Cookie build failed: {ce}")

                    final_body = kwargs.get(body_kw) if body_kw else None
                    # Render the body the same way curl_cffi would on the
                    # wire: a form-urlencoded string. For dict input we use
                    # urlencode; for str/bytes we pass through.
                    wire_body: str
                    if isinstance(final_body, dict):
                        from urllib.parse import urlencode as _ue
                        wire_body = _ue(
                            [(k, v) for k, v in final_body.items()],
                            doseq=False,
                        )
                    elif isinstance(final_body, bytes):
                        try:
                            wire_body = final_body.decode("utf-8")
                        except UnicodeDecodeError:
                            wire_body = f"<bytes len={len(final_body)}>"
                    elif isinstance(final_body, str):
                        wire_body = final_body
                    else:
                        wire_body = repr(final_body)

                    logger.warning(
                        "[patch][REPLAY-DUMP] outgoing StreamGenerate request "
                        "(SENSITIVE — contains live cookies):\n"
                        f"  URL        : {full_url}\n"
                        f"  METHOD     : {method}\n"
                        f"  HEADERS    : {json.dumps(final_headers, ensure_ascii=False, indent=2)}\n"
                        f"  BODY(wire) : {wire_body}"
                    )
                except Exception as e:
                    logger.warning(f"[patch] replay-dump failed: {e}")
            return await orig(method, url, *args, **kwargs)
        return patched

    hooked = []
    if hasattr(sess, "post"):
        sess.post = _make_post_wrapper(sess.post)
        hooked.append("post")
    if hasattr(sess, "request"):
        sess.request = _make_request_wrapper(sess.request)
        hooked.append("request")

    sess._streamgen_patched = True
    logger.info(
        f"[patch] installed StreamGenerate body interceptor on: {hooked}"
    )
    return True


# ---------------------------------------------------------------------------
# Patch installer
# ---------------------------------------------------------------------------


def _patch_watchdog_timeout() -> None:
    """Bump gemini-webapi's stream-idle watchdog if env var is set.

    The library hardcodes 120 seconds in ``_generate``. There's no public
    setter, so we walk the module looking for a likely constant or rebind
    a class attribute. If we can't find a clean override point, fall back
    to a no-op (user has to live with 120s).
    """
    if _WATCHDOG_TIMEOUT_SECONDS <= 0:
        return
    try:
        import gemini_webapi.client as _client_mod
    except ImportError:
        return

    # The constant is typically a module- or class-level int. Scan for
    # likely names.
    candidates = [
        "WATCHDOG_TIMEOUT",
        "WATCHDOG_IDLE_TIMEOUT",
        "_WATCHDOG_TIMEOUT",
        "STREAM_IDLE_TIMEOUT",
        "IDLE_TIMEOUT",
    ]
    patched = []
    for name in candidates:
        if hasattr(_client_mod, name):
            try:
                setattr(_client_mod, name, _WATCHDOG_TIMEOUT_SECONDS)
                patched.append(f"module.{name}")
            except Exception:
                pass
    # Also poke the GeminiClient class.
    GeminiClient = getattr(_client_mod, "GeminiClient", None)
    if GeminiClient is not None:
        for name in candidates:
            if hasattr(GeminiClient, name):
                try:
                    setattr(GeminiClient, name, _WATCHDOG_TIMEOUT_SECONDS)
                    patched.append(f"GeminiClient.{name}")
                except Exception:
                    pass
    if patched:
        logger.info(
            f"[patch] watchdog timeout overridden to "
            f"{_WATCHDOG_TIMEOUT_SECONDS}s on: {patched}"
        )
    else:
        logger.warning(
            f"[patch] WEBAI_WATCHDOG_TIMEOUT={_WATCHDOG_TIMEOUT_SECONDS} "
            "but no watchdog constant found in gemini_webapi.client. "
            "Library default 120s remains in effect."
        )


def apply_patches() -> None:
    """Install our resumable upload_file in place of gemini_webapi's."""
    try:
        import gemini_webapi.utils as _utils
    except ImportError:
        logger.warning("gemini-webapi not installed; skipping patches")
        return

    # Rebind in both the submodule (if it's importable directly) and the
    # utils package, plus anywhere client.py captured a direct reference.
    try:
        import gemini_webapi.utils.upload_file as _u_mod  # type: ignore
        _u_mod.upload_file = upload_file_resumable  # type: ignore[attr-defined]
    except ImportError:
        pass

    if hasattr(_utils, "upload_file"):
        _utils.upload_file = upload_file_resumable  # type: ignore[attr-defined]

    try:
        import gemini_webapi.client as _client_mod
        if hasattr(_client_mod, "upload_file"):
            _client_mod.upload_file = upload_file_resumable  # type: ignore[attr-defined]
    except ImportError:
        pass

    _patch_watchdog_timeout()

    logger.info(
        "[patch] gemini_webapi.upload_file -> resumable browser-compatible flow"
    )
