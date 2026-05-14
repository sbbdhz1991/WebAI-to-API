import configparser
import logging
import os
from typing import Optional, List, Union
from pathlib import Path
from gemini_webapi import GeminiClient as WebGeminiClient
from gemini_webapi.exceptions import AuthError, APIError
from gemini_webapi.utils.rotate_1psidts import (
    save_cookies as _gwa_save_cookies,
    _get_cookies_cache_path as _gwa_cache_path,
)
from app.config import CONFIG

logger = logging.getLogger("app")


# Error patterns that look like "your cookies just went stale" — these are
# what trigger the retry-with-refresh path in generate_content. We're
# conservative on purpose: retrying on every error would mask real bugs
# (model errors, content-safety rejections, rate limits, etc.).
_COOKIE_ERROR_NEEDLES = (
    "UNAUTHENTICATED",
    # APIError 1100 has been seen when PSIDCC slips past its 10-minute
    # server-side TTL between the lib's 9-minute rotation cycles.
    "1100",
)


def _is_cookie_shaped_failure(exc: BaseException) -> bool:
    """Return True if exc looks like a cookie-staleness symptom worth retrying."""
    if isinstance(exc, AuthError):
        return True
    if isinstance(exc, APIError):
        msg = str(exc)
        return any(needle in msg for needle in _COOKIE_ERROR_NEEDLES)
    return False

# Maps user-facing short names to the internal model identifiers accepted by gemini-webapi.
MODEL_ALIASES = {
    "flash":    "gemini-3-flash",
    "thinking": "gemini-3-flash-thinking",
    "pro":      "gemini-3-pro",
}

def resolve_model_name(model: str) -> str:
    """Resolve a model name alias to its internal identifier."""
    return MODEL_ALIASES.get(model, model)

class MyGeminiClient:
    """
    Wrapper for the Gemini Web API client.
    """
    def __init__(self, secure_1psid: str, secure_1psidts: str, proxy: str | None = None) -> None:
        self.client = WebGeminiClient(secure_1psid, secure_1psidts, proxy)
        self._gems_cache = None

    async def init(self) -> None:
        """Initialize the Gemini client and persist any rotated cookies."""
        await self.client.init()
        await self._persist_cookies()
        self._seed_cookie_cache()

    def _seed_cookie_cache(self) -> None:
        # auto_refresh sleeps 600s before its first write, and only writes on
        # successful rotation. Without this, the cache file stays empty for
        # the first 10 minutes and a fast restart has nothing to load from.
        # Skip if a cache already exists so a fresh worker can't overwrite a
        # newer rotation written by a sibling worker (matters under --workers>1).
        try:
            path = _gwa_cache_path(self.client._cookies)
            if path and path.exists() and path.stat().st_size > 0:
                return
            _gwa_save_cookies(self.client._cookies, self.client.verbose)
        except Exception as e:
            logger.warning(f"Failed to seed gemini cookie cache: {e}")

    async def _persist_cookies(self) -> None:
        """Persist rotated cookies back to config.conf to survive restarts."""
        config_path = "config.conf"
        if not os.path.exists(config_path):
            return
        try:
            cookies = self.client.cookies
            psid = cookies.get("__Secure-1PSID")
            psidts = cookies.get("__Secure-1PSIDTS")
            if not psid:
                return
            cfg = configparser.ConfigParser()
            cfg.read(config_path, encoding="utf-8")
            if "Cookies" not in cfg:
                cfg["Cookies"] = {}
            cfg["Cookies"]["gemini_cookie_1psid"] = psid
            if psidts:
                cfg["Cookies"]["gemini_cookie_1psidts"] = psidts
            with open(config_path, "w", encoding="utf-8") as f:
                cfg.write(f)
            logger.info("Cookies persisted to config.conf after rotation.")
        except Exception as e:
            logger.warning(f"Failed to persist cookies: {e}")

    async def generate_content(
        self,
        message: str,
        model: str,
        files: Optional[List[Union[str, Path]]] = None,
        gem: Optional[str] = None,
    ):
        """
        Generate content using the Gemini client.

        Two-layer freshness guarantee against PSIDCC's 10-minute server TTL
        (which is shorter than gemini-webapi's 9-minute auto_refresh):
          1. Pre-request: graft the latest cookies from chrome_server into
             the curl_cffi session, so a stale-on-edge PSIDCC can never be
             sent on a real request.
          2. Failure-retry: if Google still returns a cookie-shaped error
             (AuthError, or APIError 1100 / UNAUTHENTICATED), refresh
             cookies once more and retry the call exactly once.
        """
        resolved_model = resolve_model_name(model)
        resolved_gem = await self._resolve_gem(gem) if gem else None

        await self._prewarm_cookies()

        try:
            return await self.client.generate_content(
                message, model=resolved_model, files=files, gem=resolved_gem
            )
        except Exception as e:
            if not _is_cookie_shaped_failure(e):
                raise
            logger.warning(
                f"generate_content cookie-shaped failure ({type(e).__name__}: {e}); "
                "refreshing from Chrome and retrying once."
            )
            await self._prewarm_cookies(force=True)
            return await self.client.generate_content(
                message, model=resolved_model, files=files, gem=resolved_gem
            )

    async def _prewarm_cookies(self, force: bool = False) -> None:
        """Pull fresh cookies from chrome_server into the curl_cffi session.

        Best-effort: if Chrome is unreachable, the lib's own auto_refresh
        is still on the 9-minute schedule, so we keep the request flow
        moving with whatever's already in the jar.
        """
        try:
            from app.services.chrome_bridge import refresh_cookies_into_session

            n = await refresh_cookies_into_session(self.client)
            if force or n == 0:
                logger.debug(f"[prewarm] grafted {n} cookies from chrome (force={force})")
        except Exception as e:
            logger.debug(f"[prewarm] skipped: {type(e).__name__}: {e}")

    async def fetch_gems(self):
        """Fetch available gems and cache them."""
        self._gems_cache = await self.client.fetch_gems()
        return self._gems_cache

    async def _resolve_gem(self, gem_id_or_name: str):
        """Resolve a gem by ID or name."""
        if self._gems_cache is None:
            await self.fetch_gems()
        for gem in self._gems_cache:
            if gem.id == gem_id_or_name or gem.name.lower() == gem_id_or_name.lower():
                return gem
        return gem_id_or_name

    async def close(self) -> None:
        """Close the Gemini client."""
        await self.client.close()

    def start_chat(self, model: str, gem: Optional[str] = None):
        """
        Start a chat session with the given model.
        """
        resolved_model = resolve_model_name(model)
        # Note: Gem resolution might need to be async if we want to support name resolution here
        # For now, we'll assume gem is passed as ID or already resolved if possible
        # but the underlying library might expect a Gem object.
        return self.client.start_chat(model=resolved_model, gem=gem)
