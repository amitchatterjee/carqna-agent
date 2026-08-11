# Multi-session picker (user_sessions table, backend endpoints, UI header panel)

Status: **INPROG (planned, not yet implemented)** — this is the feature `004` and `005` both
explicitly deferred ("multiple named sessions per user, like Claude Code"). Written up per direct
instruction; implementation not started, gated on explicit go-ahead.

## Context

Today, `carqna-copilot-ui` never shows the user anything about sessions — CopilotKit generates one
client-side `thread_id` and that's the only conversation that exists per browser session. The backend
namespaces it under the verified user (`{user_id}:{client_thread_id}`, per `004`), but there's no way
for a user to have *multiple* named conversations, switch between them, or see which one they're in.

This plan adds:
- A header panel in the UI showing the logged-in user's id/email plus a dropdown of their sessions.
- A "+" button to create a new session (auto-generated internal id + a human-readable name, max 256
  chars).
- Selecting a session from the dropdown continues that conversation.
- A new `user_sessions` table (1-to-many `user_id` → session), separate from `user_registry` (which
  stays exactly as-is — identity only, per `005`).
- Backend REST endpoints (list, create — no update/delete yet) under token validation, that the UI
  calls.

**Explicitly out of scope for this plan** (confirmed): session rename, session delete. The design
below should not make those hard to add later, but nothing here builds them.

## Design

### Data model: new `user_sessions` table

Following `005`'s precedent — DDL lives in `infrastructure/docker/postgres/initdb.d/`, not runtime
Python (a new script sorting after `users_registry.sh`, e.g. `user_sessions.sh`), connecting as
`convmem` so the table is convmem-owned (same fix `005` needed after getting bitten by connecting as
the `postgres` superuser instead).

```sql
CREATE TABLE IF NOT EXISTS user_sessions (
    session_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES user_registry(user_id),
    session_name VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id);
```

`session_id` is a Postgres identity column (the modern equivalent of `SERIAL` — backed by a sequence
internally, which is what "could be a database sequence" in the request maps to), globally unique
across all users, not per-user. `user_id` is a foreign key into `user_registry.user_id` (that column
is already a primary key there, so this is a natural fit — also means a session can't be created for
a `user_id` `user_registry` has never seen, though in practice `track_user` always upserts the caller
first).

### Checkpoint key changes shape

Currently (`copilotkit_server.py`): `input_data.thread_id = f"{user_id}:{input_data.thread_id}"`,
where the right-hand `thread_id` is a client-generated UUID CopilotKit makes up on its own.

New: the right-hand side becomes the **session's `session_id`** (from `user_sessions`, stringified)
instead of a client-generated UUID. The client must tell the backend which session is active on every
request. Existing pre-session-management conversations (anything under the old
`{user_id}:{client_uuid}` scheme, including all pre-`004` `debug-*`/bare-UUID rows) become orphaned —
same precedent as `004` set for pre-auth data, not migrated.

**Resolved, confirmed against the installed `@copilotkit/react-core@1.63.1`/`@ag-ui/client@0.0.57`
source**: two separate mechanisms exist and matter here.

CopilotKit does have a full thread-management system (`useThreads`, `renameThread`, `archiveThread`,
`deleteThread`, the thread-drawer UI) — but it's built specifically to talk to CopilotKit's own
hosted **"Intelligence platform"** (their commercial cloud product) via a proprietary thread-endpoints
protocol our self-hosted `CopilotRuntime` doesn't implement (one related config comment even calls a
drawer feature "unlicensed" — this is gated behind their paid tier). **Not used by this plan.**

What *is* usable, and self-hosted-friendly: `<CopilotChatConfigurationProvider threadId={...}>` (the
same component `ChatApp.tsx` already wraps things in, just without the `threadId` prop today) plus
its `setActiveThreadId(threadId, options)` method — this directly controls which `thread_id` the next
message gets sent under. Confirmed this is sufficient for *correct conversation continuation*:
LangGraph's checkpointer keys purely off whatever `thread_id` string arrives, so pointing it at an
existing session's `session_id` resumes that conversation's real state/context immediately, correctly,
regardless of whether the browser has seen those messages before.

**What it does NOT do**: automatically fetch/display prior message history in the chat UI when
switching. `AbstractAgent.messages` is just a plain array that fills up live as SSE events stream in
during an actual run — there's no "load history for thread X" call anywhere in the self-hosted path
(that's the exact piece the paid Intelligence-platform thread system provides, and we don't have it).
**Confirmed acceptable**: restoring the visual chat history on session switch is a nice-to-have, not a
requirement for this plan — see "Explicitly deferred" below. So this plan needs no new
history-loading endpoint; `threadId`-switching alone is sufficient.

### Backend: new module `src/agent/sessions.py`

Mirrors `user_tracking.py`'s shape:
- `list_sessions(pool, user_id) -> list[...]` — `SELECT session_id, session_name, created_at FROM
  user_sessions WHERE user_id = %s ORDER BY created_at DESC`.
- `create_session(pool, user_id, session_name) -> ...` — validates `session_name` is non-empty and
  ≤256 chars, `INSERT ... RETURNING session_id, session_name, created_at`.

Both take `user_id` from the already-verified token (never trust a client-supplied user id — same
principle as `004`'s thread-ownership design), so a user can only ever see/create their own sessions.

### Backend: new routes in `copilotkit_server.py`

Reuses the existing `user_pool` (already opened in `lifespan()` for `user_registry`) and the existing
`verify_token` dependency:

- `GET /sessions` — list the caller's sessions.
- `POST /sessions` — create a session; body `{"session_name": str}`; returns the created row.

Separate paths from the AG-UI `POST /` and `GET /health` routes already registered, no conflict.

### Frontend

- **New header panel component** (e.g. `src/components/SessionHeader.tsx`): shows
  `session.user.email`/`user.sub` (already available via `auth0.getSession()` in `app/page.tsx`,
  passed down) and a dropdown populated from `GET /sessions`, plus a "+" button that calls
  `POST /sessions`.
- **New Next.js proxy route** `app/api/sessions/route.ts`, following `app/api/copilotkit/route.ts`'s
  existing pattern exactly: server-side `auth0.getAccessToken()`, attach `Authorization: Bearer
  <token>`, forward to `carqna-agent`'s `/sessions` endpoints.
- **`ChatApp.tsx`**: receives the active `session_id` as a prop and passes it as the `threadId` prop
  on `<CopilotChatConfigurationProvider>` (stringified `session_id`); switching sessions in the header
  dropdown updates this value, which drives `setActiveThreadId` internally.
- **First-login / no-sessions-yet behavior — resolved**: no auto-creation. A brand-new user with zero
  sessions sees just the "+" button (no dropdown selection possible until they create one).

## Explicitly deferred (not part of this plan)

- Session rename, session delete (explicitly confirmed out of scope for now).
- **Restoring the visual chat history when switching to an existing session** — confirmed nice-to-have,
  not required. The underlying conversation is unaffected either way (LangGraph resumes real state/
  context correctly from the first new message sent in that session); only the on-screen chat bubbles
  from before the switch won't reappear until this is built. Would need a new endpoint reading the
  LangGraph checkpoint for `{user_id}:{session_id}` and returning it in AG-UI's message shape, plus
  frontend wiring to populate `agent.messages` on session-select — real, separate scope for later.
- Migrating/associating existing pre-session-management checkpoint threads into `user_sessions`.
- Any uniqueness constraint or dedup on `session_name` (assumed not required — two sessions can share
  a name).
- Any last-used/most-recent-activity tracking on sessions beyond `created_at` (e.g. no
  `last_active_at` column) — dropdown ordering falls back to creation time only, for now.
- Auto-creating a first session for brand-new users — resolved: no, require an explicit "+" click.

## Verification (once implemented)

1. `GET /sessions` with no token → `401` (same pattern as the existing `POST /` route).
2. `POST /sessions` creates a row scoped to the caller's `user_id`; a different user's token can never
   see or create sessions under someone else's `user_id`.
3. Selecting a session in the UI dropdown and sending a message correctly continues that session's
   real conversation (the model has full prior context) — confirmed via `checkpoints` state, not via
   the chat UI re-displaying old bubbles (deferred, see above).
4. `checkpoints.thread_id` for a new message shows `{user_id}:{session_id}` (integer, not a UUID) for
   sessions created under this new scheme.
5. A brand-new user with zero sessions sees only the "+" button, no dropdown options, until they
   create their first session.
