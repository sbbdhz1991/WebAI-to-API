# src/app/endpoints/debug.py
"""Operator-only diagnostics. Mounted under /debug.

These endpoints exist so that when something looks wrong with the
Chrome bridge or cookie state, you can hit a URL and immediately see
side-by-side data — instead of having to guess from log lines, or wait
for the next 1100 to confirm a hypothesis.
"""
from fastapi import APIRouter

from app.logger import logger
from app.services.chrome_bridge import diagnose_cookies

router = APIRouter()


@router.get("/debug/cookies")
async def cookies_diagnostic() -> dict:
    """Compare what each CDP cookie-retrieval API returns.

    Useful when the standard ``fetch_gemini_cookies()`` looks like it's
    missing auth cookies (e.g., it reports only 10 cookies, none of
    which is __Secure-1PSID). The response shows what each of 5
    different CDP methods returns:

      - storage_getCookies            (browser-level, our default)
      - network_getAllCookies         (browser-level, alt method)
      - per_tab.network_getCookies    (page-scoped, current URL)
      - per_tab.network_getCookies_urls  (page-scoped, with URL filter)
      - per_tab.document_cookie       (page JS — non-HttpOnly only)

    Compare auth_present across them:
      - All four agree __Secure-1PSID is missing → Chrome is logged out
      - storage_getCookies missing it but per_tab sees it → CDP query bug
      - Some see it, others don't → partitioned/context confusion
    """
    try:
        return await diagnose_cookies()
    except Exception as e:
        logger.error(f"cookies_diagnostic error: {e}", exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}
