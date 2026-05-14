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
    import socket as _socket

    sock = _socket.create_connection((real_host, real_port))
    sock.setblocking(False)

    # 2. URI used purely for header derivation — Host becomes "localhost".
    spoof_uri = f"ws://localhost{ws_path}"

    logger.info(
        f"[chrome_bridge] connecting CDP via {real_host}:{real_port} "
        f"(spoofed Host=localhost)"
    )
    _ws = await websockets.connect(
        spoof_uri,
        sock=sock,
        max_size=None,
        ping_interval=20,
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
        ws = await _get_ws()
        _msg_id += 1
        msg_id = _msg_id
        await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        # Bounded read loop: drop unrelated events until our reply lands.
        # 15s is plenty for a cookie dump from a healthy Chromium.
        end = asyncio.get_event_loop().time() + 15.0
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
    """Lightweight probe — True if Chromium has the core Google auth cookies."""
    try:
        cookies = await fetch_gemini_cookies()
    except ChromeBridgeError:
        return False
    return "__Secure-1PSID" in cookies and "__Secure-1PSIDTS" in cookies


async def reload_gemini_tab() -> bool:
    """Find the chrome_server's gemini.google.com tab and reload it.

    Used by the keepalive background task — making Chrome navigate
    triggers its own DBSC cookie rotation + session refresh flow, which
    is what stops Google from quietly killing the session as ``idle``.

    Returns True if the post-reload URL still looks signed-in, False
    if Chrome has been logged out (so callers can warn loudly). Any
    exception is converted to False so the keepalive loop can't crash.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{CHROME_CDP_URL}/json/list", headers={"Host": "localhost"})
            r.raise_for_status()
            targets = r.json()
    except Exception as e:
        logger.warning(f"[chrome_bridge] reload: failed to list targets: {e}")
        return False

    tab = next(
        (t for t in targets
         if t.get("type") == "page"
         and "gemini.google.com" in (t.get("url") or "")),
        None,
    )
    if not tab:
        logger.warning(
            "[chrome_bridge] reload: no gemini.google.com tab open. "
            "Has someone closed it in noVNC?"
        )
        return False

    # Connect to this specific tab's CDP and ask it to navigate.
    from urllib.parse import urlparse

    p = urlparse(tab["webSocketDebuggerUrl"])
    try:
        import socket as _socket
        sock = _socket.create_connection(
            (urlparse(CHROME_CDP_URL).hostname, urlparse(CHROME_CDP_URL).port or 80)
        )
        sock.setblocking(False)
        tab_ws = await websockets.connect(
            f"ws://localhost{p.path}", sock=sock, max_size=None
        )
    except Exception as e:
        logger.warning(f"[chrome_bridge] reload: tab WS connect failed: {e}")
        return False

    try:
        await tab_ws.send(json.dumps({
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": "https://gemini.google.com/app"},
        }))
        # Drain a couple of frames so we don't leave the buffer full.
        try:
            await asyncio.wait_for(tab_ws.recv(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            await tab_ws.close()
        except Exception:
            pass

    # Give the page a moment to start the auth/cookie dance, then
    # check whether Chrome still has the auth cookies.
    await asyncio.sleep(3.0)
    return await is_signed_in()


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
