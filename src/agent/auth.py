"""Verify incoming Okta/Auth0 access tokens (resource-server role).

This is deliberately just JWT/JWKS verification (`pyjwt` + `PyJWKClient`), not
`auth0-server-python`'s `ServerClient` -- that SDK is built for the
interactive login/session flow (a different OAuth2 role), which is the
frontend's job (`@auth0/nextjs-auth0` in carqna-copilot-ui), not this
service's. See .plans/004-2026-08-09-oauth2-okta-auth-plan-INPROG.md for why.

Env vars (see .env.example): AUTH0_DOMAIN, AUTH0_AUDIENCE.
"""

import logging
import os
from typing import Any, Optional

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from jwt import PyJWKClient

# Safe to call even if agent.graph (which also calls this) hasn't run yet --
# this module can be imported standalone, e.g. for tests.
load_dotenv()

logger = logging.getLogger(__name__)

# Module-level singleton: PyJWKClient does its own JWKS fetch/cache
# internally, so one instance should be reused across requests rather than
# rebuilt per-request. Built lazily (not at import time) since AUTH0_DOMAIN
# needs load_dotenv() to have already run.
_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        domain = os.getenv("AUTH0_DOMAIN")
        if not domain:
            raise RuntimeError("AUTH0_DOMAIN is not set")
        _jwks_client = PyJWKClient(f"https://{domain}/.well-known/jwks.json")
    return _jwks_client


def get_bearer_token(request: Request) -> str:
    """Extract and validate the `Authorization: Bearer <token>` header.

    Called by `AuthMiddleware` (see `auth_middleware.py`) before it verifies
    the token via `authenticate_request`, and by callers that just need the
    raw token to pass along elsewhere (e.g. `user_tracking.py`'s `/userinfo`
    call) without re-verifying it themselves.
    """
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or malformed Authorization header"
        )
    return auth_header.split(" ", 1)[1].strip()


def authenticate_request(token: str) -> dict[str, Any]:
    """Verify a raw bearer token and return its decoded claims.

    Called once per request by `AuthMiddleware` (see `auth_middleware.py`) --
    not a FastAPI dependency itself, since centralizing auth in middleware
    means no route needs to declare a per-route `Depends` for this anymore.
    Raises HTTPException(401) if the token fails signature, issuer, audience,
    or expiry validation, or is missing its `sub` claim.
    """
    domain = os.getenv("AUTH0_DOMAIN")
    audience = os.getenv("AUTH0_AUDIENCE")
    if not domain or not audience:
        raise RuntimeError("AUTH0_DOMAIN and AUTH0_AUDIENCE must both be set")

    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://{domain}/",
        )
    except jwt.PyJWTError as e:
        logger.warning(f"Token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid token") from e

    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Token missing 'sub' claim")

    return claims
