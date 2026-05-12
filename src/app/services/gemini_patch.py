# src/app/services/gemini_patch.py
"""Monkey-patches that fix gemini-webapi's incompatibility with Google's
current Gemini upload protocol.

The pinned gemini-webapi (2.0.0) posts files via a single multipart POST to
an endpoint Google no longer routes back into the chat model layer — uploads
appear to succeed but `generate_content` returns ``APIError 1099`` when the
model tries to use the resulting URI.

The actual browser flow, captured live, is a two-step **resumable** upload
against ``push.clients6.google.com/upload/`` with ``X-Tenant-Id:
bard-storage``; the server returns a ``/contrib_service/...`` URI which the
StreamGenerate request layer expects verbatim.

This module replaces ``gemini_webapi.utils.upload_file`` (and its rebinding
inside ``gemini_webapi.client``) with the browser-compatible flow. It also
exposes ``inject_extra_cookies`` so callers can paste a full browser Cookie
header into config and have all of ``SID``/``SAPISID``/``__Secure-*PAPISID``
loaded into the curl_cffi session — needed because the cookie-only setup
gemini-webapi bootstraps with does not always carry the SAPISID-family
cookies the upload endpoint demands.
"""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import secrets
import string
import urllib.parse
from functools import wraps
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

logger = logging.getLogger("app")

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
    logger.info(
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
    logger.info(
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


def _rewrite_file_refs(file_refs: List[Any]) -> List[Any]:
    """Convert each ``[[url], filename]`` entry into the 9-tuple shape."""
    if not isinstance(file_refs, list):
        return file_refs
    out: List[Any] = []
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
            out.append(
                [
                    [url, 2, None, mime],
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
    return out


def _rewrite_streamgen_body(body: Any) -> Any:
    """Walk the form body and reshape any file refs inside ``f.req``."""
    if body is None:
        return body
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
        return body  # dict / other shapes — leave alone

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
            try:
                outer = json.loads(v)
                if (
                    isinstance(outer, list)
                    and len(outer) >= 2
                    and isinstance(outer[1], str)
                ):
                    inner = json.loads(outer[1])
                    # inner[0] is the message_content array:
                    # [prompt, 0, None, <file_refs>, None, None, 0]
                    if (
                        isinstance(inner, list)
                        and len(inner) >= 1
                        and isinstance(inner[0], list)
                        and len(inner[0]) >= 4
                        and isinstance(inner[0][3], list)
                        and inner[0][3]  # non-empty file_refs
                    ):
                        old_refs = inner[0][3]
                        new_refs = _rewrite_file_refs(old_refs)
                        if new_refs != old_refs:
                            inner[0][3] = new_refs
                            outer[1] = json.dumps(
                                inner, ensure_ascii=False, separators=(",", ":")
                            )
                            v = json.dumps(
                                outer, ensure_ascii=False, separators=(",", ":")
                            )
                            changed = True
                            logger.info(
                                f"[patch] rewrote {len(new_refs)} file ref(s) "
                                f"in StreamGenerate body"
                            )
            except (json.JSONDecodeError, ValueError, IndexError, TypeError) as e:
                logger.warning(f"[patch] f.req rewrite skipped: {e}")
        new_pairs.append((k, v))

    if not changed:
        return body
    new_body_str = urllib.parse.urlencode(new_pairs)
    return new_body_str.encode("utf-8") if was_bytes else new_body_str


def install_streamgen_interceptor(client_wrapper: Any) -> bool:
    """Wrap the underlying session's ``post`` so StreamGenerate file refs
    get reshaped on the fly. Idempotent."""
    sess = _find_session(client_wrapper)
    if sess is None:
        logger.warning(
            "[patch] install_streamgen_interceptor: no session found"
        )
        return False
    if getattr(sess, "_streamgen_patched", False):
        return True

    orig_post = sess.post

    @wraps(orig_post)
    async def patched_post(url, *args, **kwargs):
        try:
            if url and "StreamGenerate" in str(url):
                if "data" in kwargs and kwargs["data"] is not None:
                    kwargs["data"] = _rewrite_streamgen_body(kwargs["data"])
                elif args:
                    # data positional? (curl_cffi typically uses kwarg, but
                    # be defensive)
                    pass
        except Exception as e:
            logger.warning(f"[patch] StreamGenerate intercept error: {e}")
        return await orig_post(url, *args, **kwargs)

    sess.post = patched_post
    sess._streamgen_patched = True
    logger.info("[patch] installed StreamGenerate body interceptor")
    return True


# ---------------------------------------------------------------------------
# Patch installer
# ---------------------------------------------------------------------------


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

    logger.info(
        "[patch] gemini_webapi.upload_file -> resumable browser-compatible flow"
    )
