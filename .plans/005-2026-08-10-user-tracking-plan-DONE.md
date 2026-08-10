# Track which real users have which sessions (users table, backend groundwork)

Status: **DONE** — implemented and verified 2026-08-10.

## Context

`004-2026-08-09-oauth2-okta-auth-plan-DONE.md` made the LangGraph checkpoint key
`{verified_user_id}:{client_supplied_thread_id}`, where `user_id` is the JWT's opaque `sub` claim
(e.g. `auth0|6a78d5504c69cc8f16465b81`). That's correct as the storage key (stable, immutable), but
it means there's no way to look at a `checkpoints.thread_id` row and know which real person it
belongs to.

The checkpoint key itself stays exactly as-is (explicit decision: no change there). This plan adds a
separate mapping from `user_id` → human identity (email/name), as groundwork for the still-deferred
"multiple named sessions per user" feature (the Claude-Code-like session picker) — not building that
UI now, just the data backend it'll need.

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

**New table, `users`, in the same `convmem` Postgres database** (no new database/role needed — the
`convmem` user already has schema-create privileges, per `002`'s fix):

```sql
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,        -- JWT `sub` claim, matches the checkpoint key's prefix
    email TEXT,
    name TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**`src/agent/user_tracking.py`** (new module):
- `ensure_users_table(pool)` — idempotent DDL, called once at startup, same convention as
  `checkpointer.setup()`.
- `track_user(pool, user_id, access_token)` — per authenticated request: `UPDATE ... SET
  last_seen_at = now()`; if that matches zero rows (first time this user's ever been seen), fetch
  `email`/`name` from `GET https://{AUTH0_DOMAIN}/userinfo` (Bearer the same access token) and
  `INSERT ... ON CONFLICT DO UPDATE`. One cheap indexed UPDATE per request in the common case, one
  extra Auth0 call ever per unique user. Wrapped in try/except that logs and swallows all failures —
  this must never break an actual chat request.

**`src/agent/auth.py`**: factored the existing inline Bearer-header parsing out of `verify_token` into
a reusable `get_bearer_token(request)`, so the route can get the raw token for `track_user`'s
`/userinfo` call without duplicating that logic.

**`src/agent/copilotkit_server.py`**: opens a separate `psycopg_pool.AsyncConnectionPool` (same
`conn_string` as the checkpointer, deliberately not sharing `AsyncPostgresSaver`'s internal pool) in
`lifespan()`, calls `ensure_users_table` once, closes the pool in a `finally`. The `POST /` route
calls `track_user(...)` right after `verify_token` resolves `user_id`, before the existing
`thread_id` rewrite.

**`pyproject.toml`**: added `psycopg-pool` explicitly (was already transitive via
`langgraph-checkpoint-postgres`, same reasoning as why `pyjwt`/`psycopg[binary]` were made explicit in
`002`/`004`).

## Explicitly out of scope

- No frontend changes — entirely backend-side, reusing the access token already being sent.
- No session-list/picker UI — this is the data layer only.
- No periodic refresh of stale email/name if a user changes them in Auth0 — insert-once is enough for
  now.

## Verification — passed 2026-08-10

1. Backend restarted; startup logs confirmed both `checkpointer.setup()` and `ensure_users_table`
   completed without error before the (unrelated) port-8000-already-in-use situation was resolved.
2. Confirmed via direct Postgres query (`information_schema.columns`) the `users` table has the
   expected schema, initially empty.
3. Sent a real chat message through the logged-in UI. Row appeared:
   `user_id='auth0|6a78d5504c69cc8f16465b81'`, `email='amit.chatterjee@quik-j.com'`,
   `name='amit.chatterjee@quik-j.com'` (Auth0 falls back to email for `name` when no separate display
   name is set on the account — expected, not a bug). `user_id` matches the prefix already present in
   `checkpoints.thread_id`, confirming the two tables key on the same identity.
4. Verification command handed to the user directly for their own spot-checks:
   `PGPASSWORD=convmem psql -h localhost -U convmem -d convmem -c "SELECT user_id, email, name,
   first_seen_at, last_seen_at FROM users;"`
