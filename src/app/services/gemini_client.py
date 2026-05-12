# src/app/services/gemini_client.py
from models.gemini import MyGeminiClient
from app.config import CONFIG
from app.logger import logger
from app.utils.browser import get_cookie_from_browser
from app.services.gemini_patch import (
    apply_patches,
    inject_extra_cookies,
    install_streamgen_interceptor,
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

async def init_gemini_client() -> bool:
    """
    Initialize and set up the Gemini client based on the configuration.
    Returns True on success, False on failure.
    """
    global _gemini_client, _initialization_error
    _initialization_error = None

    if CONFIG.getboolean("EnabledAI", "gemini", fallback=True):
        try:
            gemini_cookie_1PSID = CONFIG["Cookies"].get("gemini_cookie_1PSID")
            gemini_cookie_1PSIDTS = CONFIG["Cookies"].get("gemini_cookie_1PSIDTS")
            gemini_proxy = CONFIG["Proxy"].get("http_proxy")

            if not gemini_cookie_1PSID or not gemini_cookie_1PSIDTS:
                cookies = get_cookie_from_browser("gemini")
                if cookies:
                    gemini_cookie_1PSID, gemini_cookie_1PSIDTS = cookies

            if gemini_proxy == "":
                gemini_proxy = None

            if gemini_cookie_1PSID and gemini_cookie_1PSIDTS:
                _gemini_client = MyGeminiClient(secure_1psid=gemini_cookie_1PSID, secure_1psidts=gemini_cookie_1PSIDTS, proxy=gemini_proxy)
                await _gemini_client.init()
                # After successful init, inject any extra browser cookies the
                # user provided. The upload endpoint may need SAPISID-family
                # cookies that gemini.google.com's own bootstrap doesn't set.
                extra = CONFIG["Cookies"].get("gemini_cookie_extra", "")
                if extra:
                    try:
                        n = inject_extra_cookies(_gemini_client.client, extra)
                        if n:
                            logger.info(
                                f"Injected {n} extra cookies into Gemini session."
                            )
                        else:
                            logger.warning(
                                "gemini_cookie_extra is set but no cookies could be injected."
                            )
                    except Exception as ce:
                        logger.warning(f"Failed to inject extra cookies: {ce}")
                # Install the StreamGenerate body interceptor so attached
                # files use the 9-element ref shape the server expects.
                try:
                    install_streamgen_interceptor(_gemini_client.client)
                except Exception as ie:
                    logger.warning(f"Failed to install streamgen interceptor: {ie}")
                logger.info("Gemini client initialized successfully.")
                return True
            else:
                error_msg = "Gemini cookies not found. Please provide cookies in config.conf or ensure browser is logged in."
                logger.error(error_msg)
                _initialization_error = error_msg
                return False

        except AuthError as e:
            error_msg = f"Gemini authentication failed: {e}. This usually means cookies are expired or invalid."
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
    else:
        error_msg = "Gemini client is disabled in config."
        logger.info(error_msg)
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

