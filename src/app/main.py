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
from app.services.chrome_bridge import keepalive_probe
from app.auth import verify_api_key, API_KEY_ENV_VAR
from app.logger import logger

# Import endpoint routers
from app.endpoints import gemini, chat, google_generative, debug

import asyncio
import os


# Health-probe interval, default 5 minutes. The probe is now PASSIVE — it
# only reads Chrome's cookie jar over CDP and makes no outbound request to
# Google — so this is purely a detection-latency knob, not a keep-alive
# cadence (Chrome rotates DBSC cookies on its own ~10-min timer regardless
# of us). Set CHROME_KEEPALIVE_INTERVAL=0 to disable monitoring.
_CHROME_KEEPALIVE_INTERVAL = int(os.environ.get("CHROME_KEEPALIVE_INTERVAL", "300"))


async def _chrome_keepalive_loop() -> None:
    """Background loop that periodically checks whether Chrome still holds
    a live Google session, logging loudly the moment it doesn't so an
    operator can re-login via noVNC — well before chat requests start
    silently degrading to the anonymous, rate-limited Gemini.

    This used to *reload* the gemini tab every cycle on the theory that a
    navigation would refresh the DBSC cookie chain. Production logs
    disproved that (Chrome rotates on its own timer regardless of reloads),
    so the loop is now a passive health probe — see ``keepalive_probe``.
    """
    if _CHROME_KEEPALIVE_INTERVAL <= 0:
        logger.info("Chrome session monitor disabled (CHROME_KEEPALIVE_INTERVAL=0).")
        return

    logger.info(
        f"Chrome session monitor started: passive health probe every "
        f"{_CHROME_KEEPALIVE_INTERVAL}s."
    )
    cycle = 0
    while True:
        try:
            await asyncio.sleep(_CHROME_KEEPALIVE_INTERVAL)
            cycle += 1
            report = await keepalive_probe()

            def _fp(v):
                # Fingerprint a cookie value: first 8 chars + last 4 + length.
                # Enough to spot rotation across cycles (compare consecutive
                # log lines) without leaking the full token.
                if not isinstance(v, str) or not v:
                    return "<none>"
                if len(v) <= 12:
                    return f"{v}(len={len(v)})"
                return f"{v[:8]}..{v[-4:]}(len={len(v)})"

            present = ",".join(
                k for k, ok in report.get("key_cookies", {}).items() if ok
            ) or "<none>"
            log_line = (
                f"[monitor] cycle #{cycle}: "
                f"ok={report['ok']} reason={report['reason']!r} "
                f"stable_present={report.get('stable_present')} "
                f"psidts={_fp(report.get('psidts'))} "
                f"key_present=[{present}]"
            )
            if not report["ok"]:
                logger.error(
                    log_line
                    + "  -- session looks dead; re-login via noVNC "
                    "(http://<host>:6080/vnc.html)"
                )
            elif report["reason"].startswith("WARN"):
                logger.warning(log_line)
            else:
                logger.info(log_line)
        except asyncio.CancelledError:
            logger.info("Chrome session monitor task cancelled.")
            return
        except Exception as e:
            # Never let a probe failure kill the task — log and continue;
            # the next cycle might find Chrome healthy.
            logger.warning(
                f"[monitor] cycle #{cycle} crashed "
                f"({type(e).__name__}): {e}",
                exc_info=True,
            )

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
