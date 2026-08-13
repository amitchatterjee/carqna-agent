"""Track which real users have hit the agent, for the deferred multi-session
picker feature (see .plans/004-2026-08-09-oauth2-okta-auth-plan-DONE.md's
"Explicitly deferred" section).

Deliberately doesn't touch the LangGraph checkpoint key (still the opaque
`sub`-based composite key) -- this is a separate `user_registry` table mapping
that opaque `user_id` to a human-readable identity (email/name), fetched via
the standard OIDC `/userinfo` endpoint using the same access token `auth.py`
already verifies. No Auth0-specific dashboard config needed: `src/lib/auth0.ts`
(carqna-copilot-ui) doesn't override `authorizationParameters.scope`, so the
SDK's default scopes (`openid profile email offline_access`) are already on
every access token, which is what makes `/userinfo` work here.

The `user_registry` table's DDL lives in
infrastructure/docker/postgres/initdb.d/users_registry.sh (same convention as
the carqna database/role itself in init_user.sh), not here -- this module
trusts the table already exists rather than creating it at runtime.
"""

import logging
import os

import httpx
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


async def _fetch_userinfo(access_token: str) -> tuple[str | None, str | None]:
    domain = os.getenv("AUTH0_DOMAIN")
    if not domain:
        raise RuntimeError("AUTH0_DOMAIN is not set")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{domain}/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5.0,
        )
        resp.raise_for_status()
        profile = resp.json()
    return profile.get("email"), profile.get("name")


async def track_user(pool: AsyncConnectionPool, user_id: str, access_token: str) -> None:
    """Record that `user_id` made an authenticated request.

    Cheap in the common case (one indexed UPDATE); fetches the user's profile
    from Auth0's `/userinfo` endpoint only the first time a given user_id is
    ever seen. Never raises -- a `/userinfo` hiccup or DB blip must not break
    an actual chat request, this is observability groundwork, not a hard
    dependency of the chat path.
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE user_registry SET last_seen_at = now() WHERE user_id = %s",
                    (user_id,),
                )
                if cur.rowcount == 0:
                    email, name = await _fetch_userinfo(access_token)
                    await cur.execute(
                        """
                        INSERT INTO user_registry (user_id, email, name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET last_seen_at = now()
                        """,
                        (user_id, email, name),
                    )
    except Exception:
        logger.warning("Failed to track user %s", user_id, exc_info=True)
