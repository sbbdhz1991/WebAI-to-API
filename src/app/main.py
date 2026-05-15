# src/app/main.py
# Load .env so direct invocations like `uvicorn app.main:app` also pick up vars.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

from fastapi import Depends, FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware

from app.services.gemini_client import get_gemini_client, init_gemini_client, GeminiClientNotInitializedError
from app.services.session_manager import init_session_managers
from app.services.chrome_bridge import reload_gemini_tab, ChromeBridgeError
from app.auth import verify_api_key, API_KEY_ENV_VAR
from app.logger import logger

# Import endpoint routers
from app.endpoints import gemini, chat, google_generative, debug

import asyncio
import os


# Default keepalive interval: 5 minutes. Short enough to keep Chrome's
# DBSC chain warm (PSIDRTS rotates every 10 min — refresh before it
# expires), long enough not to spam Google or interfere with manual
# noVNC use too aggressively.
_CHROME_KEEPALIVE_INTERVAL = int(os.environ.get("CHROME_KEEPALIVE_INTERVAL", "300"))


async def _chrome_keepalive_loop() -> None:
    """Background loop that periodically reloads the gemini.google.com
    tab in chrome_server, so Chrome's DBSC cookie rotation keeps firing
    and the Google session doesn't get killed for inactivity.

    On every cycle we also verify Chrome still has auth cookies; if not,
    we log loudly so operators see the session needs a re-login via
    noVNC — well before the next chat request fails with 1100.
    """
    if _CHROME_KEEPALIVE_INTERVAL <= 0:
        logger.info("Chrome keepalive disabled (CHROME_KEEPALIVE_INTERVAL=0).")
        return

    logger.info(
        f"Chrome keepalive started: tab reload every "
        f"{_CHROME_KEEPALIVE_INTERVAL}s."
    )
    while True:
        try:
            await asyncio.sleep(_CHROME_KEEPALIVE_INTERVAL)
            ok = await reload_gemini_tab()
            if ok:
                logger.debug("[keepalive] Chrome reload OK, still signed in.")
            else:
                logger.error(
                    "[keepalive] Chrome appears SIGNED OUT or unreachable. "
                    "Re-login via noVNC (http://<host>:6080/vnc.html) — "
                    "subsequent requests will fail until cookies are valid."
                )
        except asyncio.CancelledError:
            logger.info("Chrome keepalive task cancelled.")
            return
        except Exception as e:
            # Never let a keepalive failure kill the task — log and
            # continue, the next cycle might find Chrome healthy.
            logger.warning(f"[keepalive] cycle error ({type(e).__name__}): {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    Initializes services on startup.
    """
    # Try to get the existing client first
    client_initialized = False
    try:
        get_gemini_client()
        client_initialized = True
        logger.info("Gemini client found (initialized in main process).")
    except GeminiClientNotInitializedError:
        logger.info("Gemini client not initialized in worker process, attempting reinitialization...")

    # If client is not available, try to initialize it (for multiprocessing support)
    if not client_initialized:
        try:
            init_result = await init_gemini_client()
            if init_result:
                logger.info("Gemini client successfully initialized in worker process.")
            else:
                logger.error("Failed to initialize Gemini client in worker process.")
        except Exception as e:
            logger.error(f"Error initializing Gemini client in worker process: {e}")

    # Initialize session managers only if the client is available
    try:
        get_gemini_client()
        init_session_managers()
        logger.info("Session managers initialized for WebAI-to-API.")
    except GeminiClientNotInitializedError as e:
        logger.warning(f"Session managers not initialized: {e}")

    # Start the Chrome keepalive task. Independent of the Gemini client
    # initialization above: even if init failed (e.g., Chrome is logged
    # out at startup), keepalive can detect that and log a warning, and
    # the next manual re-login + automatic reconnect will get us going.
    keepalive_task = asyncio.create_task(_chrome_keepalive_loop())

    yield

    # Shutdown logic: cancel the keepalive task so uvicorn can exit
    # cleanly. HTTPX/curl_cffi clients manage their own pools.
    keepalive_task.cancel()
    try:
        await keepalive_task
    except (asyncio.CancelledError, Exception):
        pass
    logger.info("Application shutdown complete.")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API key authentication: enabled when GEMINI_API_KEY env var is set.
if os.environ.get(API_KEY_ENV_VAR, "").strip():
    logger.info(f"API key authentication enabled (env: {API_KEY_ENV_VAR}).")
else:
    logger.info(f"API key authentication disabled ({API_KEY_ENV_VAR} not set).")

_auth_dependencies = [Depends(verify_api_key)]

# Register the endpoint routers for WebAI-to-API
app.include_router(gemini.router, dependencies=_auth_dependencies)
app.include_router(chat.router, dependencies=_auth_dependencies)
app.include_router(google_generative.router, dependencies=_auth_dependencies)
app.include_router(debug.router, dependencies=_auth_dependencies)
