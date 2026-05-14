# src/app/services/gemini_client.py
from models.gemini import MyGeminiClient
from app.config import CONFIG
from app.logger import logger
from app.utils.browser import get_cookie_from_browser
from app.services.gemini_patch import (
    apply_patches,
    inject_extra_cookies,
    install_streamgen_interceptor,
    _set_cookie_in_jar,
)
from app.services.chrome_bridge import (
    fetch_gemini_cookies as _fetch_gemini_cookies_from_chrome,
    ChromeBridgeError,
)

# Import the specific exception to handle it gracefully
from gemini_webapi.exceptions import AuthError

# Patch gemini-webapi's upload protocol once, before any client is built.
apply_patches()


class GeminiClientNotInitializedError(Exception):
    """Raised when the Gemini client is not initialized or initialization failed."""
    pass


# Global variable to store the Gemini client instance
_gemini_client = None
_initialization_error = None


async def _bootstrap_cookies_from_chrome() -> dict[str, str] | None:
    """Pull the full Google cookie set from chrome_server, or None on failure.

    Chromium is now the source of truth for the session — its DBSC private
    key is what keeps __Secure-1PSIDTS alive. We always prefer its current
    values over whatever's stale in config.conf.
    """
    try:
        cookies = await _fetch_gemini_cookies_from_chrome()
    except ChromeBridgeError as e:
        logger.warning(
            f"chrome_bridge unavailable at startup ({e}). "
            "Falling back to config.conf cookies."
        )
        return None
    if "__Secure-1PSID" not in cookies or "__Secure-1PSIDTS" not in cookies:
        logger.warning(
            "chrome_server reachable but is missing core auth cookies. "
            "Has Google been logged in via noVNC yet?"
        )
        return None
    logger.info(
        f"Bootstrapped {len(cookies)} cookies from chrome_server "
        f"(PSID={cookies['__Secure-1PSID'][:12]}..., "
        f"PSIDTS={cookies['__Secure-1PSIDTS'][:12]}...)."
    )
    return cookies


async def init_gemini_client() -> bool:
    """
    Initialize and set up the Gemini client based on the configuration.
    Returns True on success, False on failure.
    """
    global _gemini_client, _initialization_error
    _initialization_error = None

    if not CONFIG.getboolean("EnabledAI", "gemini", fallback=True):
        error_msg = "Gemini client is disabled in config."
        logger.info(error_msg)
        _initialization_error = error_msg
        return False

    try:
        # 1. Source of truth: Chromium (DBSC-rotated session).
        chrome_cookies = await _bootstrap_cookies_from_chrome()

        # 2. Pick PSID + PSIDTS, preferring Chromium over config.conf.
        if chrome_cookies:
            gemini_cookie_1PSID = chrome_cookies["__Secure-1PSID"]
            gemini_cookie_1PSIDTS = chrome_cookies["__Secure-1PSIDTS"]
        else:
            gemini_cookie_1PSID = CONFIG["Cookies"].get("gemini_cookie_1PSID")
            gemini_cookie_1PSIDTS = CONFIG["Cookies"].get("gemini_cookie_1PSIDTS")
            if not gemini_cookie_1PSID or not gemini_cookie_1PSIDTS:
                cookies = get_cookie_from_browser("gemini")
                if cookies:
                    gemini_cookie_1PSID, gemini_cookie_1PSIDTS = cookies

        gemini_proxy = CONFIG["Proxy"].get("http_proxy") or None

        if not (gemini_cookie_1PSID and gemini_cookie_1PSIDTS):
            error_msg = (
                "No Gemini cookies available. Either log into Google via the "
                "chrome_server's noVNC (http://<host>:6080/vnc.html) or set "
                "[Cookies] in config.conf."
            )
            logger.error(error_msg)
            _initialization_error = error_msg
            return False

        _gemini_client = MyGeminiClient(
            secure_1psid=gemini_cookie_1PSID,
            secure_1psidts=gemini_cookie_1PSIDTS,
            proxy=gemini_proxy,
        )

        # 3. Graft the full Chrome cookie set BEFORE init() runs. The lib's
        # init does fetch_user_status which can fail with UNAUTHENTICATED
        # if SAPISID-family cookies are missing — paste them all in first.
        if chrome_cookies:
            grafted = 0
            for name, value in chrome_cookies.items():
                if _set_cookie_in_jar(_gemini_client.client.cookies, name, value):
                    grafted += 1
            logger.info(
                f"Grafted {grafted} cookies from chrome_server onto the "
                "Gemini session before init()."
            )

        await _gemini_client.init()

        # 4. config.conf::gemini_cookie_extra still supported as a manual
        # override for users who don't run chrome_server.
        extra = CONFIG["Cookies"].get("gemini_cookie_extra", "")
        if extra:
            try:
                n = inject_extra_cookies(_gemini_client.client, extra)
                if n:
                    logger.info(
                        f"Injected {n} extra cookies from config.conf."
                    )
                else:
                    logger.warning(
                        "gemini_cookie_extra is set but no cookies were injected."
                    )
            except Exception as ce:
                logger.warning(f"Failed to inject extra cookies: {ce}")

        # 5. Install the StreamGenerate body interceptor so attached files
        # use the 9-element ref shape the server expects.
        try:
            install_streamgen_interceptor(_gemini_client.client)
        except Exception as ie:
            logger.warning(f"Failed to install streamgen interceptor: {ie}")

        logger.info("Gemini client initialized successfully.")
        return True

    except AuthError as e:
        error_msg = (
            f"Gemini authentication failed: {e}. The cookies in chrome_server "
            "or config.conf are invalid — re-login via noVNC."
        )
        logger.error(error_msg)
        _gemini_client = None
        _initialization_error = error_msg
        return False

    except Exception as e:
        error_msg = f"Unexpected error initializing Gemini client: {e}"
        logger.error(error_msg, exc_info=True)
        _gemini_client = None
        _initialization_error = error_msg
        return False


def get_gemini_client():
    """
    Returns the initialized Gemini client instance.

    Raises:
        GeminiClientNotInitializedError: If the client is not initialized.
    """
    if _gemini_client is None:
        error_detail = _initialization_error or "Gemini client was not initialized. Check logs for details."
        raise GeminiClientNotInitializedError(error_detail)
    return _gemini_client

