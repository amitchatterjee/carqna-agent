"""List/create/touch a user's named conversation sessions (the web half of the
multi-session picker feature -- see
.plans/006-2026-08-11-session-management-plan-DONE.md).

Deliberately separate from `carqna_cli.py`'s local SQLite `sessions` table --
that one has no `user_id` at all (the file itself is the isolation boundary);
this one is keyed by the verified Auth0 `user_id` via a join through
`user_registry`.

The `user_sessions` table's DDL lives in
infrastructure/docker/postgres/initdb.d/users_sessions.sh (same convention as
`user_registry` in users_registry.sh), not here -- these functions trust the
table already exists rather than creating it at runtime.

Every function here takes the caller's verified TEXT `user_id` (the Auth0
`sub`, exactly what `agent.auth.verify_token` returns) and resolves
`user_sessions.user_registry_id` (the actual foreign key) internally via a
join/subquery on `user_registry.user_id` -- callers never see or pass the
surrogate id directly. Never trust a client-supplied user id for any of this,
same principle as the checkpoint thread-ownership design in
.plans/004-2026-08-09-oauth2-okta-auth-plan-DONE.md.
"""

import logging
from datetime import datetime
from typing import TypedDict

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class SessionRow(TypedDict):
    """One row from `user_sessions`, as returned to callers/serialized as JSON."""

    id: int
    session_name: str
    access_ts: datetime


async def list_sessions(pool: AsyncConnectionPool, user_id: str) -> list[SessionRow]:
    """List `user_id`'s sessions, most-recently-accessed first."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.session_name, s.access_ts
                FROM user_sessions s
                JOIN user_registry r ON r.id = s.user_registry_id
                WHERE r.user_id = %s
                ORDER BY s.access_ts DESC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
    return [{"id": row[0], "session_name": row[1], "access_ts": row[2]} for row in rows]


async def create_session(
    pool: AsyncConnectionPool, user_id: str, session_name: str
) -> SessionRow | None:
    """Create a new session for `user_id`.

    Raises ValueError if `session_name` is empty or over 256 characters.
    Raises psycopg.errors.UniqueViolation if `user_id` already has a session
    with that name (callers should translate that into a user-facing error,
    not silently reuse the existing session -- that's carqna_cli.py's
    find-or-create behavior, not this one's).

    Returns None if `user_id` has no `user_registry` row -- shouldn't happen
    in practice since `user_tracking.track_user` already upserts the caller
    earlier in the same request, but not assumed silently.
    """
    if not session_name or len(session_name) > 256:
        raise ValueError("session_name must be non-empty and at most 256 characters")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_sessions (user_registry_id, session_name)
                SELECT id, %s FROM user_registry WHERE user_id = %s
                RETURNING id, session_name, access_ts
                """,
                (session_name, user_id),
            )
            row = await cur.fetchone()

    if row is None:
        return None
    return {"id": row[0], "session_name": row[1], "access_ts": row[2]}


async def touch_session(pool: AsyncConnectionPool, user_id: str, session_id: int) -> None:
    """Bump `session_id`'s `access_ts` to now, scoped to `user_id`.

    Called on every authenticated chat request (see copilotkit_server.py's
    `POST /` route), not exposed over HTTP itself. Never raises -- like
    `user_tracking.track_user`, this is a "last accessed" nicety, not a hard
    dependency of the chat path: the checkpoint itself is keyed by
    `{user_id}:{session_id}` directly and works regardless of whether this
    update (or even a `user_sessions` row for this id) exists at all.
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE user_sessions s
                    SET access_ts = now()
                    FROM user_registry r
                    WHERE s.id = %s AND s.user_registry_id = r.id AND r.user_id = %s
                    """,
                    (session_id, user_id),
                )
    except Exception:
        logger.warning(
            "Failed to touch session %s for user %s", session_id, user_id, exc_info=True
        )
