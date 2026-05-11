# src/app/auth.py
import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    APIKeyHeader,
    APIKeyQuery,
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from app.logger import logger

API_KEY_ENV_VAR = "GEMINI_API_KEY"

# Registering these as Depends() makes FastAPI advertise them in the OpenAPI
# spec, so Swagger UI at /docs shows an "Authorize" button for each.
# auto_error=False so a missing credential returns None instead of 403 — we
# need to try all four sources before deciding to reject.
_bearer = HTTPBearer(
    auto_error=False,
    description="OpenAI-compatible: `Authorization: Bearer <key>`",
)
_x_goog = APIKeyHeader(
    name="x-goog-api-key",
    auto_error=False,
    description="Google Generative AI style",
)
_x_api = APIKeyHeader(
    name="x-api-key",
    auto_error=False,
    description="Generic API-key header",
)
_query_key = APIKeyQuery(
    name="key",
    auto_error=False,
    description="Google-style `?key=<key>` query parameter",
)


def _get_expected_key() -> Optional[str]:
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return key or None


async def verify_api_key(
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_goog_api_key: Optional[str] = Depends(_x_goog),
    x_api_key: Optional[str] = Depends(_x_api),
    query_key: Optional[str] = Depends(_query_key),
) -> None:
    """
    Validate the request's API key against the value in the GEMINI_API_KEY env var.

    Accepts any of: Authorization: Bearer <k>, x-goog-api-key, x-api-key, ?key=<k>.
    If GEMINI_API_KEY is unset or empty, authentication is disabled.
    """
    expected = _get_expected_key()
    if expected is None:
        return

    candidates = [
        bearer.credentials.strip() if bearer and bearer.credentials else None,
        x_goog_api_key.strip() if x_goog_api_key else None,
        x_api_key.strip() if x_api_key else None,
        query_key.strip() if query_key else None,
    ]

    for provided in candidates:
        if provided and provided == expected:
            return

    logger.warning("Rejected request: missing or invalid Gemini API key.")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "Bearer"},
    )
