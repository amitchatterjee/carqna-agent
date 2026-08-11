# Track which real users have which sessions (user_registry table, backend groundwork)

Status: **DONE** — implemented and verified 2026-08-10, revised 2026-08-11 (see "Revisions" below).

## Context

`004-2026-08-09-oauth2-okta-auth-plan-DONE.md` made the LangGraph checkpoint key
`{verified_user_id}:{client_supplied_thread_id}`, where `user_id` is the JWT's opaque `sub` claim
(e.g. `auth0|6a78d5504c69cc8f16465b81`). That's correct as the storage key (stable, immutable), but
it means there's no way to look at a `checkpoints.thread_id` row and know which real person it
belongs to.

The checkpoint key itself stays exactly as-is (explicit decision: no change there). This plan adds a
separate mapping from `user_id` → human identity (email/name), as groundwork for the still-deferred
"multiple named sessions per user" feature (the Claude-Code-like session picker) — not building that
UI now, just the data backend it'll need. Explicitly scoped as pure identity/presence tracking (one
row per user), not an activity/event log (many rows per user) — a genuinely different table that may
get built separately later.

**Key finding, confirmed against the installed `@auth0/nextjs-auth0` source**:
`carqna-copilot-ui/src/lib/auth0.ts` doesn't set `authorizationParameters.scope`, so the SDK's
`DEFAULT_SCOPES` (`openid profile email offline_access`) already apply to every access token being
issued. That means the backend can fetch the user's profile from the standard OIDC `/userinfo`
endpoint using the same access token it already verifies, with **zero new Auth0 dashboard
configuration**. Chosen over adding a custom claim via an Auth0 Action because: (a) no dashboard
changes needed (the `004` dashboard steps already caused real friction), and (b) `/userinfo` is
OIDC-standard, keeping the backend provider-agnostic like the rest of `004`'s design, rather than
baking in an Auth0-specific Action.

## Design

**Table `user_registry`, in the same `convmem` Postgres database** (no new database/role needed — the
`convmem` user already has schema-create privileges, per `002`'s fix):

```sql
CREATE TABLE IF NOT EXISTS user_registry (
    user_id TEXT PRIMARY KEY,        -- JWT `sub` claim, matches the checkpoint key's prefix
    email TEXT,
    name TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

DDL lives in `infrastructure/docker/postgres/initdb.d/users_registry.sh` (see "Revisions" — moved
there from runtime code), connecting as `convmem` itself so the table is convmem-owned, same as the
checkpoint tables.

**`src/agent/user_tracking.py`**:
- `track_user(pool, user_id, access_token)` — per authenticated request: `UPDATE user_registry SET
  last_seen_at = now()`; if that matches zero rows (first time this user's ever been seen), fetch
  `email`/`name` from `GET https://{AUTH0_DOMAIN}/userinfo` (Bearer the same access token) and
  `INSERT ... ON CONFLICT DO UPDATE`. One cheap indexed UPDATE per request in the common case, one
  extra Auth0 call ever per unique user. Wrapped in try/except that logs and swallows all failures —
  this must never break an actual chat request. Trusts the table already exists (created by the
  initdb.d script) rather than creating it itself.

**`src/agent/auth.py`**: factored the existing inline Bearer-header parsing out of `verify_token` into
a reusable `get_bearer_token(request)`, so the route can get the raw token for `track_user`'s
`/userinfo` call without duplicating that logic.

**`src/agent/copilotkit_server.py`**: opens a separate `psycopg_pool.AsyncConnectionPool` (same
`conn_string` as the checkpointer, deliberately not sharing `AsyncPostgresSaver`'s internal pool) in
`lifespan()`, closed in a `finally`. The `POST /` route calls `track_user(...)` right after
`verify_token` resolves `user_id`, before the existing `thread_id` rewrite.

**`pyproject.toml`**: added `psycopg-pool` explicitly (was already transitive via
`langgraph-checkpoint-postgres`, same reasoning as why `pyjwt`/`psycopg[binary]` were made explicit in
`002`/`004`).

## Explicitly out of scope

- No frontend changes — entirely backend-side, reusing the access token already being sent.
- No session-list/picker UI — this is the data layer only.
- No periodic refresh of stale email/name if a user changes them in Auth0 — insert-once is enough for
  now.
- No activity/event log (one row per login or per request) — `user_registry` is deliberately just
  identity + first/last-seen presence, one row per user. A separate table for that may be built later
  but is a different, unstarted piece of work.

## Revisions (2026-08-11)

Two follow-up changes made after the initial 2026-08-10 verification, both applied by the project
owner directly, then double-checked here for consistency across code/docs:

1. **DDL moved out of runtime code into `infrastructure/docker/postgres/initdb.d/`** — the original
   design had `user_tracking.py` create the table itself at every startup (`ensure_users_table(pool)`,
   mirroring `checkpointer.setup()`'s idempotent-migration convention). Moved instead to
   `infrastructure/docker/postgres/initdb.d/users_registry.sh`, matching `init_user.sh`'s convention
   for the `convmem` role/database itself — infrastructure setup belongs in Postgres init scripts, not
   application code. Named to sort alphabetically after `init_user.sh` so `convmem` exists first, and
   connects as `convmem` (not `$POSTGRES_USER`) so the table ends up convmem-owned — an earlier draft
   of this script connected as the `postgres` superuser, which created the table owned by `postgres`
   and caused `permission denied for table users` for both the app itself and manual `psql` queries;
   fixed by connecting as `convmem` directly. Caveat: `docker-entrypoint-initdb.d` scripts only run
   once, on a **fresh** Postgres volume — doesn't retroactively run against an already-initialized
   container/volume.
2. **Table renamed `users` → `user_registry`** — to make room for a conceptually distinct future
   table (an actual per-event activity log, out of scope here, see above) without the two names
   colliding/confusing. Same schema, same semantics, verified working end-to-end after the rename.

## Verification

**2026-08-10 (original)**: backend restarted; a real chat message through the logged-in UI produced a
row with correct `user_id`/`email`/`name`, matching the `sub` prefix already in
`checkpoints.thread_id`.

**2026-08-11 (post-rename)**: confirmed via direct Postgres query that `user_registry` (not `users`)
is the only user-identity table present, with the same schema; a fresh chat message correctly
upserted a row (`user_id='auth0|6a78d5504c69cc8f16465b81'`, `email='amit.chatterjee@quik-j.com'`,
`name='Amit Chatterjee'` — a real display name this time, since the Auth0 profile was updated between
verifications). Spot-check command:

```bash
PGPASSWORD=convmem psql -h localhost -U convmem -d convmem -c "SELECT user_id, email, name, first_seen_at, last_seen_at FROM user_registry;"
```
