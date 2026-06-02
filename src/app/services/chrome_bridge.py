"""Bridge to the chrome_server container's headless Chromium via CDP.

Responsibility: source-of-truth for Google session cookies.

The chrome_server sidecar holds the real, logged-in browser session — its
Chromium owns the __Secure-1PSID, __Secure-1PSIDTS, and the device-bound
session credentials (DBSC) private key that Google now requires for
PSIDTS rotation. We talk to it over Chrome DevTools Protocol on
``${CHROME_CDP_URL}`` (default ``http://chrome_server:9222``) and pull the
current cookies on demand.

This module exposes one async function used by gemini-webapi's monkey
patch: ``fetch_gemini_cookies()``. Everything else is plumbing.

Connection lifecycle: a single browser-level WebSocket is cached per
process. On error it is dropped and reconnected lazily on the next call.
The WS server inside Chromium can drop us at any moment (page reloads,
tab closes, browser restarts), so callers must tolerate transient
failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx
import websockets

# websockets 14+ moved several names. Stay loose with typing so we work on
# both the legacy (<14) and the new asyncio API.
logger = logging.getLogger("app")

CHROME_CDP_URL = os.environ.get("CHROME_CDP_URL", "http://chrome_server:9222")

# Hard timeouts so a slow/unreachable Chromium can never wedge the event
# loop. Every request's cookie pre-warm runs through _cdp_call, so an
# unbounded blocking connect/send here freezes the whole uvicorn worker.
#   _CONNECT_TIMEOUT — TCP connect + WS upgrade ceiling.
#   _CDP_CALL_TIMEOUT — single CDP send+reply ceiling.
_CONNECT_TIMEOUT = float(os.environ.get("CHROME_CDP_CONNECT_TIMEOUT", "5"))
_CDP_CALL_TIMEOUT = float(os.environ.get("CHROME_CDP_CALL_TIMEOUT", "15"))

# Single shared connection guarded by a lock so we don't open N parallel
# CDP sockets from concurrent requests.
_ws: Optional[Any] = None
_ws_lock = asyncio.Lock()
_msg_id = 0


def _ws_is_open(ws: Any) -> bool:
    """Cross-version check for whether a WebSocket connection is still alive."""
    if ws is None:
        return False
    # Old API (<14): .closed bool property.
    closed = getattr(ws, "closed", None)
    if closed is not None:
        return not closed
    # New API: .state enum where OPEN == 1.
    state = getattr(ws, "state", None)
    if state is not None:
        name = getattr(state, "name", str(state))
        return name == "OPEN"
    # Unknown — assume open until first I/O failure proves otherwise.
    return True


class ChromeBridgeError(RuntimeError):
    """Raised when the bridge cannot talk to Chromium."""


# Chrome 111+ added a DNS-rebinding mitigation that rejects any HTTP/WS
# request to the DevTools endpoint whose Host header is not "localhost"
# or an IP literal. We connect via the docker service name
# (chrome_server), so we must override the Host header on every request.
_LOCALHOST_HOST_HEADER = {"Host": "localhost"}


async def _resolve_browser_ws_url() -> str:
    """Discover Chromium's browser-level WebSocket endpoint.

    ``GET /json/version`` returns a JSON with ``webSocketDebuggerUrl``
    pointing at ``ws://<host>:<port>/devtools/browser/<uuid>``. The URL
    host as Chromium reports it is the value of ``--remote-debugging-address``
    that Chromium was started with — for our sidecar that's ``127.0.0.1``,
    which is useless to connect to from outside. We swap in the host from
    CHROME_CDP_URL.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            f"{CHROME_CDP_URL}/json/version",
            headers=_LOCALHOST_HOST_HEADER,
        )
        r.raise_for_status()
        data = r.json()
    raw_ws = data.get("webSocketDebuggerUrl")
    if not raw_ws:
        raise ChromeBridgeError(
            f"/json/version did not include webSocketDebuggerUrl: {data!r}"
        )
    # Replace whatever host Chromium reports with the host we actually
    # reach it on (the docker service name).
    from urllib.parse import urlparse, urlunparse

    cdp_host = urlparse(CHROME_CDP_URL)
    ws_parts = urlparse(raw_ws)
    fixed = ws_parts._replace(netloc=cdp_host.netloc)
    return urlunparse(fixed)


async def _get_ws() -> Any:
    """Return a live CDP WebSocket, opening a new one if needed.

    Chrome 111+ rejects WebSocket upgrades whose Host header is not
    "localhost" or an IP literal (DNS-rebinding mitigation). The
    ``websockets`` library derives the Host header from the URI's host
    and ignores any value we pass via ``additional_headers``, so:

      1. Open the TCP socket ourselves to the real destination
         (``chrome_server:9222``).
      2. Hand the socket to ``websockets.connect`` along with a URI
         whose host part is ``localhost``. The library uses our socket
         for transport but builds the Host header from the URI.
    """
    global _ws
    if _ws_is_open(_ws):
        return _ws

    ws_url = await _resolve_browser_ws_url()
    from urllib.parse import urlparse

    parsed = urlparse(ws_url)
    real_host = parsed.hostname
    real_port = parsed.port or 80
    ws_path = parsed.path or "/"

    # 1. Real TCP connection to the docker service name + port.
    #    create_connection is BLOCKING — run it off the event loop and cap
    #    it, or an unreachable Chromium freezes the entire worker.
    import socket as _socket

    sock = await asyncio.wait_for(
        asyncio.to_thread(
            _socket.create_connection, (real_host, real_port), _CONNECT_TIMEOUT
        ),
        timeout=_CONNECT_TIMEOUT + 1,
    )
    sock.setblocking(False)

    # 2. URI used purely for header derivation — Host becomes "localhost".
    spoof_uri = f"ws://localhost{ws_path}"

    logger.info(
        f"[chrome_bridge] connecting CDP via {real_host}:{real_port} "
        f"(spoofed Host=localhost)"
    )
    _ws = await asyncio.wait_for(
        websockets.connect(
            spoof_uri,
            sock=sock,
            max_size=None,
            ping_interval=20,
        ),
        timeout=_CONNECT_TIMEOUT,
    )
    return _ws


async def _drop_ws() -> None:
    global _ws
    if _ws is not None:
        try:
            await _ws.close()
        except Exception:
            pass
    _ws = None


async def _cdp_call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a single CDP message and wait for its matching reply.

    The browser sends async events on this same socket. We discard
    anything whose ``id`` doesn't match our request and read until we
    see our reply or the read times out.
    """
    global _msg_id
    async with _ws_lock:
        try:
            ws = await _get_ws()
            _msg_id += 1
            msg_id = _msg_id
            # send is unbounded by default; a half-dead TCP socket (state
            # still OPEN) can block it forever, so cap it too.
            await asyncio.wait_for(
                ws.send(
                    json.dumps({"id": msg_id, "method": method, "params": params or {}})
                ),
                timeout=_CDP_CALL_TIMEOUT,
            )
            # Bounded read loop: drop unrelated events until our reply lands.
            end = asyncio.get_event_loop().time() + _CDP_CALL_TIMEOUT
            while True:
                remaining = end - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise ChromeBridgeError(f"CDP timeout waiting for reply to {method}")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("id") != msg_id:
                    continue
                if "error" in msg:
                    raise ChromeBridgeError(
                        f"CDP error on {method}: {msg['error']!r}"
                    )
                return msg.get("result", {})
        except (asyncio.TimeoutError, OSError, websockets.exceptions.ConnectionClosed) as e:
            # Connect/send/recv stalled or the socket closed — drop the cached
            # socket so the next call reconnects cleanly instead of reusing a
            # wedged one.
            await _drop_ws()
            raise ChromeBridgeError(f"CDP {method} timed out / connection error: {e}") from e


async def fetch_gemini_cookies() -> dict[str, str]:
    """Pull all *.google.com cookies from Chromium and return name→value.

    Uses ``Storage.getCookies`` on the browser-level session, which
    returns cookies for the default browser context regardless of
    whether any specific tab is currently open. This means we don't need
    to attach to a page target.

    Raises ``ChromeBridgeError`` if the bridge cannot reach Chromium or
    Chromium returns no Google cookies (typically: not logged in yet).
    """
    try:
        result = await _cdp_call("Storage.getCookies")
    except (websockets.exceptions.ConnectionClosed, OSError) as e:
        # Reset the cached socket so the next call reconnects cleanly.
        await _drop_ws()
        raise ChromeBridgeError(f"CDP connection lost: {e}") from e

    cookies = result.get("cookies", []) or []
    out: dict[str, str] = {}
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".").lower()
        if domain == "google.com" or domain.endswith(".google.com"):
            name = c.get("name")
            value = c.get("value")
            if name and value is not None:
                out[name] = value
    if not out:
        raise ChromeBridgeError(
            "Chromium returned no google.com cookies. "
            "Has the user signed into Google via noVNC (http://<server>:6080/vnc.html)?"
        )
    if "__Secure-1PSID" not in out or "__Secure-1PSIDTS" not in out:
        # Some cookies but not the auth ones — partial / wrong account.
        logger.warning(
            f"[chrome_bridge] missing auth cookies in Chromium jar. "
            f"Have: {sorted(out)}"
        )
    return out


async def is_signed_in() -> bool:
    """True if Chromium still holds the *stable* Google auth cookies.

    Keys on the non-rotating cookies (see ``_STABLE_AUTH_COOKIES``) rather
    than ``__Secure-1PSIDTS``: the latter is rotated by Chrome roughly
    every 10 minutes and can be briefly absent mid-rotation, which would
    make a PSIDTS-based check flap between signed-in / signed-out. The
    stable cookies only disappear on a real server-side sign-out.
    """
    try:
        cookies = await fetch_gemini_cookies()
    except ChromeBridgeError:
        return False
    return all(c in cookies for c in _STABLE_AUTH_COOKIES)


# -------------------------- diagnostic --------------------------------
# Used by the /debug/cookies endpoint. When fetch_gemini_cookies looks
# like it's missing auth cookies, this runs the same query through 5
# different CDP code paths and reports which methods see which cookies,
# so you can tell "Chrome lost the session" apart from "our CDP query
# layer has a bug".

_KEY_AUTH_COOKIES = (
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-1PSIDCC",
    "SAPISID",
    "__Secure-1PAPISID",
    "HSID",
    "SSID",
    "SID",
)

# Subset of the above that does NOT rotate. Presence of these = the Google
# session is alive; absence = a real server-side sign-out. We judge session
# health on these and never on __Secure-1PSIDTS / __Secure-1PSIDCC (Chrome
# rotates those ~every 10 min, so they can be momentarily absent on a
# perfectly healthy session) nor on the page URL (a signed-out Gemini stays
# on /app as an anonymous, text-only session — it does not redirect to a
# login page, so the URL cannot distinguish dead from alive).
_STABLE_AUTH_COOKIES = ("__Secure-1PSID", "SAPISID")


def _summarize_cookies(cookies: list) -> dict:
    """Compact summary of a cookie list for the diagnostic endpoint."""
    google_names: list[str] = []
    for c in cookies or []:
        domain = (c.get("domain") or "").lstrip(".").lower()
        if domain == "google.com" or domain.endswith(".google.com"):
            name = c.get("name")
            if name:
                google_names.append(name)
    name_set = set(google_names)
    return {
        "total": len(cookies or []),
        "google_count": len(google_names),
        "google_names": sorted(name_set),
        "auth_present": {k: (k in name_set) for k in _KEY_AUTH_COOKIES},
    }


async def _diagnose_per_tab(tab: dict) -> dict:
    """Open a tab-level CDP socket and run page-scoped cookie queries."""
    from urllib.parse import urlparse
    import socket as _socket

    p = urlparse(tab["webSocketDebuggerUrl"])
    out: dict = {"url": tab.get("url")}
    try:
        sock = _socket.create_connection(
            (urlparse(CHROME_CDP_URL).hostname, urlparse(CHROME_CDP_URL).port or 80)
        )
        sock.setblocking(False)
        ws = await websockets.connect(
            f"ws://localhost{p.path}", sock=sock, max_size=None
        )
    except Exception as e:
        return {**out, "connect_error": f"{type(e).__name__}: {e}"}

    seq = {"id": 0}

    async def call(method: str, params: dict | None = None) -> dict:
        seq["id"] += 1
        mid = seq["id"]
        await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = asyncio.get_event_loop().time() + 10.0
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                raise TimeoutError(f"CDP timeout on {method}")
            raw = await asyncio.wait_for(ws.recv(), timeout=rem)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})

    try:
        try:
            r = await call("Network.getCookies")
            out["network_getCookies"] = _summarize_cookies(r.get("cookies", []))
        except Exception as e:
            out["network_getCookies"] = {"error": f"{type(e).__name__}: {e}"}

        try:
            r = await call(
                "Network.getCookies",
                {"urls": ["https://gemini.google.com/",
                          "https://accounts.google.com/"]},
            )
            out["network_getCookies_urls"] = _summarize_cookies(r.get("cookies", []))
        except Exception as e:
            out["network_getCookies_urls"] = {"error": f"{type(e).__name__}: {e}"}

        try:
            r = await call(
                "Runtime.evaluate",
                {"expression": "document.cookie", "returnByValue": True},
            )
            out["document_cookie"] = r.get("result", {}).get("value", "")
        except Exception as e:
            out["document_cookie"] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            await ws.close()
        except Exception:
            pass

    return out


async def diagnose_cookies() -> dict:
    """Side-by-side comparison of multiple CDP cookie-retrieval methods.

    Hit this when fetch_gemini_cookies looks like it's missing auth
    cookies and you want to know whether the loss is real (Chrome is
    logged out) or an artifact of one CDP API not exposing certain
    cookie kinds (HttpOnly, partitioned, cross-context, ...).
    """
    summary: dict = {}

    # 1. List all targets so the reader can see what tabs exist.
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{CHROME_CDP_URL}/json/list",
                headers=_LOCALHOST_HOST_HEADER,
            )
            r.raise_for_status()
            targets = r.json()
    except Exception as e:
        return {"error": f"could not list targets: {type(e).__name__}: {e}"}

    summary["tabs"] = [
        {
            "type": t.get("type"),
            "url": t.get("url", "")[:120],
            "id_prefix": (t.get("id") or "")[:12],
        }
        for t in targets
    ]

    # 2. Browser-level Storage.getCookies (our current production method).
    try:
        r = await _cdp_call("Storage.getCookies")
        summary["storage_getCookies"] = _summarize_cookies(r.get("cookies", []))
    except Exception as e:
        summary["storage_getCookies"] = {"error": f"{type(e).__name__}: {e}"}

    # 3. Browser-level Network.getAllCookies — different code path.
    try:
        r = await _cdp_call("Network.getAllCookies")
        summary["network_getAllCookies"] = _summarize_cookies(r.get("cookies", []))
    except Exception as e:
        summary["network_getAllCookies"] = {"error": f"{type(e).__name__}: {e}"}

    # 4. Per-tab queries on the gemini.google.com tab.
    gemini_tab = next(
        (t for t in targets
         if t.get("type") == "page"
         and "gemini.google.com" in (t.get("url") or "")),
        None,
    )
    summary["per_tab"] = (
        await _diagnose_per_tab(gemini_tab) if gemini_tab
        else {"error": "no gemini.google.com tab open"}
    )

    return summary


async def keepalive_probe() -> dict:
    """Passive session-health probe. Reads Chromium's cookie jar over CDP
    and decides whether the Google session is still alive. It does **not**
    navigate or reload any tab.

    Why passive (changed 2026-06): production logs proved that reloading
    the gemini.google.com tab does NOT drive Chrome's DBSC rotation.
    ``__Secure-1PSIDTS`` rotates on Chrome's own ~10-minute timer whether
    or not we reload, and over 1000+ cycles a reload coincided with a
    rotation only twice. So the old reload added zero keep-alive value
    while generating robotic page loads from a datacenter IP (an
    abuse-signal cost). A plain ``Storage.getCookies`` read makes no
    outbound request to Google at all, so this probe is free to run often.

    Verdict keys on the STABLE auth cookies (``_STABLE_AUTH_COOKIES``),
    never on the page URL nor on ``__Secure-1PSIDTS``:
      - a signed-out Gemini stays on /app (anonymous, text-only) — the URL
        does not redirect to a login page, so it cannot tell dead from
        alive; only the cookies can.
      - ``__Secure-1PSIDTS`` / ``__Secure-1PSIDCC`` rotate every ~10 min
        and can be momentarily absent mid-rotation, so keying on them
        flaps; the stable cookies only vanish on a real sign-out.

    Outcomes:
      stable cookies gone           -> ok=False (re-login via noVNC)
      stable present, PSIDTS absent  -> ok=True + WARN (rotation race)
      all present                    -> ok=True, healthy

    Returns: ok (bool), reason (str), stable_present (bool),
    psidts_present (bool), psidts (raw value or None), key_cookies
    (dict[name]->bool over _KEY_AUTH_COOKIES). Never raises.
    """
    report: dict = {
        "ok": False,
        "reason": "",
        "stable_present": False,
        "psidts_present": False,
        "psidts": None,
        "key_cookies": {},
    }

    try:
        cookies = await fetch_gemini_cookies()
    except ChromeBridgeError as e:
        # Empty jar / Chrome unreachable. Not-alive, but flag it as a
        # bridge problem rather than asserting a sign-out.
        report["reason"] = f"cookie fetch failed: {e}"
        return report

    report["key_cookies"] = {k: (k in cookies) for k in _KEY_AUTH_COOKIES}
    report["psidts"] = cookies.get("__Secure-1PSIDTS")
    stable_present = all(c in cookies for c in _STABLE_AUTH_COOKIES)
    psidts_present = "__Secure-1PSIDTS" in cookies
    report["stable_present"] = stable_present
    report["psidts_present"] = psidts_present

    if not stable_present:
        report["ok"] = False
        report["reason"] = (
            "session dead: stable auth cookies "
            f"({'/'.join(_STABLE_AUTH_COOKIES)}) gone from Chrome jar "
            "-- Google signed the session out"
        )
    elif not psidts_present:
        report["ok"] = True
        report["reason"] = (
            "WARN: __Secure-1PSIDTS absent at probe time (Chrome DBSC "
            "rotation race); stable cookies present so session is alive"
        )
    else:
        report["ok"] = True
        report["reason"] = "healthy"
    return report


async def refresh_cookies_into_session(curl_session: Any) -> int:
    """Pull fresh cookies from Chrome and merge into a curl_cffi session jar.

    Used by request paths that need to guarantee freshness before calling
    gemini-webapi (so a request never lands with a cookie that just
    crossed its server-side TTL). Returns the number of cookies grafted.

    Failures here are non-fatal: if Chrome is unreachable, the lib's own
    auto_refresh task is still doing rotations on its 9-minute schedule,
    so callers should not raise — log and proceed with whatever's already
    in the jar.
    """
    try:
        fresh = await fetch_gemini_cookies()
    except ChromeBridgeError as e:
        logger.debug(f"[chrome_bridge] pre-request refresh skipped: {e}")
        return 0

    # Lazy import to avoid a circular dep at module-import time.
    from app.services.gemini_patch import _set_cookie_in_jar

    jar = getattr(curl_session, "cookies", None)
    if jar is None:
        return 0
    grafted = 0
    for name, value in fresh.items():
        if _set_cookie_in_jar(jar, name, value):
            grafted += 1
    return grafted
