# Multi-session picker (user_sessions table, backend endpoints, UI header panel)

Status: **DONE** — closed out 2026-08-13. This is the feature `004` and `005` both explicitly
deferred ("multiple named sessions per user, like Claude Code"). **CLI portion implemented and
verified 2026-08-12** (see "CLI" under Design, and Verification items 6-8). **Web/backend portion
implemented and live-verified 2026-08-13** (`user_sessions` table, `/sessions` endpoints,
`sessions.py`, header panel + dropdown, `ChatApp.tsx`/`SessionHeader.tsx` on the frontend) — session
isolation confirmed via a plant-a-fact-in-A/check-not-visible-in-B/switch-back-and-recall test through
the real UI, cross-checked directly against `checkpoints`/`user_sessions` in Postgres (see
Verification).

**Known gap at close, left open by explicit choice**: three Verification items below (1, 2, 4a — the
`GET /sessions` 401-with-no-token check, cross-*user* isolation on `/sessions` specifically, and
`access_ts` touch-scoping re-verified on the *web* path) were not explicitly re-confirmed
post-implementation before closing this out — all reuse patterns already proven correct elsewhere
(`verify_token`, `WHERE ... AND user_id = %s` scoping), but none were independently exercised here.
Project owner's call to close now and check these later rather than block on them — flagged, not
forgotten.

**Real bugs found live-testing 2026-08-13, both fixed same day**:
1. `POST /sessions` failed with `create_session found no user_registry row for user_id=...` for a
   real user. Root cause: `track_user` (which upserts `user_registry`) was only wired into the chat
   route (`POST /`), not the `/sessions` routes — but a user's very first authenticated action can be
   opening the session dropdown or clicking "+" *before* ever sending a chat message, so `/sessions`
   can't assume the chat route already ran. Fixed by calling `track_user(user_pool, user_id,
   get_bearer_token(request))` at the top of both `GET /sessions` and `POST /sessions` too (same call,
   same resilience — never blocks the request), mirroring the chat route exactly.
2. **On initial page load, the dropdown visually showed the first session selected, but the chat
   panel rendered the "no session" placeholder instead of the actual chat** — a React `<select>`
   footgun. `ChatApp.tsx`'s `activeSessionId` started `null` and was never auto-set when the fetched
   session list was non-empty, but `SessionHeader`'s `<select value={activeSessionId ?? ""}>` has no
   `""` option — when a controlled `<select>`'s value doesn't match any `<option>`, the *browser*
   falls back to visually highlighting the first option regardless of what React's state actually is.
   Picking a different session (or switching back) then worked fine, because that's exactly when
   `activeSessionId` first became a real, matching value. Fixed in `refreshSessions()`: auto-select
   `data[0]?.id` (the most-recently-used session, since the backend already orders by `access_ts
   DESC`) whenever nothing is active yet, so the dropdown's initial render and React's state agree
   from the start.

**CLI verification results (2026-08-12)**: `langgraph-checkpoint-sqlite` added to `pyproject.toml`;
`carqna_cli.py` rewritten per the design below. Confirmed live: `--session test-session` created
`~/.carqna/carqna_cli.sqlite` with both `sessions` and LangGraph's own `checkpoints`/`writes` tables
on first run (no manual setup step); a real question got a real MSRP answer via the live agent; a
second invocation with the same `--session test-session` correctly continued the conversation (a
follow-up question — "What about the SE trim?" — was answered in context without restating the car,
confirming checkpoint continuity); a third invocation with `--session another-session` created a
distinct `session_id=2`/`thread_id="2"`, fully separate from session 1; running with no `--session`
flag at all correctly errors via `argparse` before touching the agent or filesystem.

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
- **`carqna_cli.py` also gets session support** — reversing `004`'s original "unaffected" assumption.
  Raised directly: Postgres is shared infrastructure, and the CLI's old hardcoded
  `thread_id = "carqna-local-session"` meant any two people running the CLI against the same shared
  Postgres would silently collide on the exact same conversation row. See "CLI" under Design below.

**Explicitly out of scope for this plan** (confirmed): session rename, session delete. The design
below should not make those hard to add later, but nothing here builds them.

## Design

### Data model: new `user_sessions` table

Following `005`'s precedent — DDL lives in `infrastructure/docker/postgres/initdb.d/`, not runtime
Python (`users_sessions.sh` — named that way, not `user_sessions.sh`, specifically because ASCII `_`
sorts before `s`, so `user_sessions.sh` would incorrectly sort *before* `users_registry.sh` and run
before the table its FK depends on exists), connecting as `carqna` so the table is carqna-owned (same
fix `005` needed after getting bitten by connecting as the `postgres` superuser instead).

```sql
CREATE TABLE IF NOT EXISTS user_sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_registry_id BIGINT NOT NULL REFERENCES user_registry(id),
    session_name VARCHAR(256) NOT NULL,
    access_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_registry_id, session_name)
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_registry_id ON user_sessions(user_registry_id);
```

`id` is a Postgres identity column (the modern equivalent of `SERIAL` — backed by a sequence
internally, which is what "could be a database sequence" in the request maps to), globally unique
across all sessions, not per-user. `user_registry_id` is a foreign key into `user_registry.id` — its
own surrogate integer key (see `005`'s 2026-08-13 revision), not the TEXT `user_id`/Auth0 `sub`.
**Decided 2026-08-13**: every Postgres table in this project gets its own auto-increment `id`, and
foreign keys reference that instead of a business-meaningful text column — applied retroactively to
`user_registry` too (was `user_id TEXT PRIMARY KEY`, now `id` is the PK and `user_id` is a plain
`UNIQUE NOT NULL` column). A session can't be created for a `user_registry_id` that doesn't exist,
same guarantee as before, just via an integer FK instead of a text one.

**`UNIQUE (user_registry_id, session_name)` — added while designing the CLI's find-or-create-by-name
flow below**, reversing an earlier draft of this plan that assumed names didn't need to be unique.
Without this constraint, "find the session named X for this user" is ambiguous if two rows could
share a name. Applies uniformly to both web- and CLI-created sessions.

**`access_ts` (renamed from `created_at`) — same pattern as the CLI's local `sessions` table,
applied here 2026-08-13** (see the CLI's `sessions` schema below for where this pattern originated,
including a real bug found and fixed there — an unscoped `UPDATE` that touched every row instead of
just one — worth being deliberately careful about when implementing the Postgres equivalent). Same
single-column semantics: "last accessed," not "first created," so `list_sessions` orders by it
descending (most-recently-used first) rather than alphabetically or by creation order. **Where the
touch happens differs from the CLI, by necessity**: the CLI touches `access_ts` once per process
invocation (`_get_or_create_session`, called once at startup), but the web path has no equivalent
single "session opened" moment — each chat message is its own independent `POST /` request, with no
persistent "this session is now active" call in between. The chosen equivalent: touch `access_ts` on
every `POST /` request for that session (see "Backend: new routes" below), scoped by both the
session's `id` *and* the caller's verified `user_id` — defense in depth beyond what the CLI needed,
matching the verified-identity-scoping principle every other query in this plan already follows, even
though `id` alone is already globally unique. This arguably produces *more* accurate "last used"
semantics than the CLI's once-per-invocation touch (reflects actual message activity, not just
whether the session was opened), not just a mechanical port of the same idea.

**Callers still pass/receive the TEXT `user_id` (Auth0 `sub`), not the surrogate `user_registry_id`**
— see "Backend: new module `src/agent/sessions.py`" below for why: `verify_token()` only ever
produces the TEXT value, so `sessions.py`'s functions resolve `user_registry_id` internally via a
join/subquery on each call, rather than requiring every caller (the `POST /` route, `list`/`create`
endpoints) to separately look it up or thread it through `track_user`'s return value.

### Checkpoint key changes shape

Currently (`copilotkit_server.py`): `input_data.thread_id = f"{user_id}:{input_data.thread_id}"`,
where the right-hand `thread_id` is a client-generated UUID CopilotKit makes up on its own.

New: the right-hand side becomes the **session's `user_sessions.id`** (stringified) instead of a
client-generated UUID. The client must tell the backend which session is active on every
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
existing session's `id` resumes that conversation's real state/context immediately, correctly,
regardless of whether the browser has seen those messages before.

**What it does NOT do**: automatically fetch/display prior message history in the chat UI when
switching. `AbstractAgent.messages` is just a plain array that fills up live as SSE events stream in
during an actual run — there's no "load history for thread X" call anywhere in the self-hosted path
(that's the exact piece the paid Intelligence-platform thread system provides, and we don't have it).
**Confirmed acceptable**: restoring the visual chat history on session switch is a nice-to-have, not a
requirement for this plan — see "Explicitly deferred" below. So this plan needs no new
history-loading endpoint; `threadId`-switching alone is sufficient.

### Backend: new module `src/agent/sessions.py`

Web path only — the CLI does not use this module at all (see "CLI" below: local SQLite instead). All
three functions take the caller's verified TEXT `user_id` (the Auth0 `sub`, exactly what
`verify_token()` returns) and resolve `user_registry_id` internally via a join, rather than requiring
callers to look it up separately:

- `list_sessions(pool, user_id) -> list[...]`:
  ```sql
  SELECT s.id, s.session_name, s.access_ts
  FROM user_sessions s
  JOIN user_registry r ON r.id = s.user_registry_id
  WHERE r.user_id = %s
  ORDER BY s.access_ts DESC
  ```
- `create_session(pool, user_id, session_name) -> ...` — validates `session_name` is non-empty and
  ≤256 chars, then resolves and inserts in one round trip:
  ```sql
  INSERT INTO user_sessions (user_registry_id, session_name)
  SELECT id, %s FROM user_registry WHERE user_id = %s
  RETURNING id, session_name, access_ts
  ```
  Fails (unique violation) if the name already exists for this user — the web UI surfaces that as an
  error. Returns no row (caller should treat as an error) if `user_id` has no `user_registry` row —
  shouldn't happen in practice since `track_user` already upserts the caller earlier in the same
  request, but not assumed silently.
- `touch_session(pool, user_id, session_id) -> None` — `session_id` here is `user_sessions.id`:
  ```sql
  UPDATE user_sessions s
  SET access_ts = now()
  FROM user_registry r
  WHERE s.id = %s AND s.user_registry_id = r.id AND r.user_id = %s
  ```
  Called from the `POST /` chat route (see below), not exposed over HTTP itself. Scoped by both the
  session's own `id` and the caller's verified `user_id` even though `id` alone is already globally
  unique — matches this plan's general "never trust/act on an id without also checking it belongs to
  the verified caller" principle, and costs nothing extra (`id` is the index anyway).

Never trust a client-supplied user id — same principle as `004`'s thread-ownership design — so a
caller can only ever see/create/touch their own sessions. The `UNIQUE (user_registry_id,
session_name)` constraint on `user_sessions` (see Data model above) was originally motivated by a
find-or-create flow the CLI no longer needs, but is kept regardless — it's a reasonable constraint for
the web path on its own (avoids a user silently ending up with duplicate-named sessions in their
dropdown).

### Backend: new routes in `copilotkit_server.py`

Reuses the existing `user_pool` (already opened in `lifespan()` for `user_registry`) and the existing
`verify_token` dependency:

- `GET /sessions` — list the caller's sessions.
- `POST /sessions` — create a session; body `{"session_name": str}`; returns the created row.
- **Both `/sessions` routes also call `track_user(user_pool, user_id, get_bearer_token(request))`
  first**, same as the chat route — a user's first authenticated action can be opening the dropdown
  or clicking "+" before ever sending a message, so `create_session`'s `user_registry` join can't
  assume the chat route already upserted that row. Found the hard way (real `POST /sessions` failure
  from a genuinely new user, fixed same day — see the note at the top of this doc).
- Existing `POST /` (the AG-UI chat route) additionally calls `sessions.touch_session(user_pool,
  user_id, session_id)` — same spot as the existing `track_user(...)` call, right after `verify_token`
  resolves `user_id` and the `session_id` is parsed out of the (now-composite) `thread_id`. Keeps
  `user_sessions.access_ts` current on every message, not just on session create/select.

Separate paths from the AG-UI `POST /` and `GET /health` routes already registered, no conflict.

### CLI: `carqna_cli.py` — local SQLite, deliberately separate from the web path's Postgres design

Raised directly during planning: `carqna_cli.py`'s old hardcoded `thread_id = "carqna-local-session"`
was a real gap once you consider Postgres is shared infrastructure — any two people running the CLI
against the same shared Postgres instance would silently collide on the exact same conversation row.
`004` explicitly assumed this was fine ("unaffected, never goes through the HTTP route"), true for the
auth work itself but leaving this pre-existing collision unaddressed.

The design went through a few iterations (multi-session floated, simplified to one-per-user, reverted
back to multi-session, all still against Postgres) before landing on the actual fix, decided last: stop
sharing Postgres for the CLI at all. The CLI moves to a **local SQLite file** — the checkpointer itself
*and* a small local sessions table — so there is no shared resource left to collide on in the first
place, which is a more fundamental fix than namespacing within Postgres would have been. Confirmed:
CLI and web are allowed to be fully separate systems here (no unified cross-surface session view).

- **Checkpointer**: swap `AsyncPostgresSaver` for LangGraph's `AsyncSqliteSaver`
  (`langgraph-checkpoint-sqlite`) for `carqna_cli.py` only — `copilotkit_server.py` stays on Postgres,
  untouched. This project used `AsyncSqliteSaver` before `002` migrated the web path to Postgres, so
  it's a known-good, previously-proven library here, not a new dependency risk. Per that same history
  (see `CLAUDE.md`), SQLite's checkpointer needs no explicit `.setup()`/migration call the way
  Postgres does — LangGraph creates its own `checkpoints`/`checkpoint_writes`/`checkpoint_blobs`/
  `checkpoint_migrations` tables automatically on first use.
- **File location**: a fixed local path, e.g. `~/.carqna/carqna_cli.sqlite`, overridable via an env var
  (mirroring `POSTGRES_URI`'s pattern) — exact default path to confirm at implementation
  time. One file holds both LangGraph's own checkpoint tables and the new `sessions` table below; no
  reason to split them across two files.
- **New local `sessions` table** — DDL created on the fly by the CLI itself at startup (`CREATE TABLE
  IF NOT EXISTS`), **not** via an infrastructure init script the way `user_registry`/`user_sessions`
  are for the shared Postgres path. That asymmetry is deliberate, not an inconsistency: Postgres DDL
  lives in `initdb.d` because it's shared infra provisioned once for many callers; this SQLite file has
  no equivalent provisioning phase — the CLI itself is the only thing that will ever create or touch
  it, so idempotent on-the-fly creation is the natural equivalent (same pattern LangGraph's own
  `AsyncSqliteSaver` already uses for its tables, just applied to our one extra table too).
  ```sql
  CREATE TABLE IF NOT EXISTS sessions (
      session_id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_name TEXT NOT NULL UNIQUE,
      access_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
  );
  ```
  No `user_id` column at all — the file itself is already scoped to one person (their own home
  directory), so there's nothing left to namespace by. This also means no `cli:{username}` identity,
  no `user_registry` row, no foreign key, and no shared `UNIQUE(user_id, session_name)` concern for the
  CLI — `UNIQUE(session_name)` alone is enough locally. All of that Postgres-shaped plumbing from the
  earlier draft of this section is gone.
  **Column renamed `created_at` → `access_ts` 2026-08-13** (user-applied directly): now means "last
  accessed," not "first created" — `_get_or_create_session` updates it on the *found* path too, not
  just on creation, and `--list`/`_list_sessions` sorts by it descending (most-recently-used first)
  instead of alphabetically. **Real bug found and fixed here**: the found-path `UPDATE` initially had
  no `WHERE` clause, so it bumped `access_ts` for every row in the table on every use, not just the
  session actually being touched — silently defeating the entire point of per-session recency
  tracking (all sessions would converge on near-identical timestamps after a couple of uses). Fixed to
  `UPDATE sessions SET access_ts = CURRENT_TIMESTAMP WHERE session_id = ?`, scoped to the resolved row.
  Verified: touching one session's `access_ts` now leaves every other session's timestamp untouched.
- **`--session <name>` stays mandatory** (`argparse`) — resolved with a plain SELECT-then-INSERT
  against the local `sessions` table (no need for Postgres's `ON CONFLICT ... RETURNING` upsert
  idiom — a single local CLI process has no concurrent-writer race to guard against, unlike the
  shared web path).
- **`--list` flag added 2026-08-12** (not in the original design, added directly against the CLI
  after `--session` shipped): lists all local sessions, most-recently-used first (`access_ts DESC`),
  showing each name and how long ago it was last accessed, and exits without initializing the agent.
  `--session` is required unless `--list` is given (argparse's own `required=True` can't express
  "required unless this other flag is set," so this is a manual post-parse check). Purely additive —
  the CLI-side equivalent of the web path's already-planned `GET /sessions`.
- **Checkpoint thread_id becomes simply `str(session_id)`** — no user-id prefix needed at all, since
  there's no shared namespace left to collide within. Simpler than the web path's
  `{user_id}:{session_id}`, and deliberately so.

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
- Migrating/associating existing pre-session-management checkpoint threads into `user_sessions`
  (web path) — orphaned, not migrated, same precedent as `004` set for pre-auth data.
- The CLI's old shared-Postgres `"carqna-local-session"` thread and its data are simply left behind in
  Postgres, unmigrated — the CLI moves to a fresh local SQLite file entirely, nothing to carry over.
- Auto-creating a first session for brand-new users — resolved: no, require an explicit "+" click.

(Last-used/most-recent-activity tracking — previously listed here as deferred — is now in scope via
`access_ts`; see Data model and "Backend: new module `src/agent/sessions.py`" above.)

## Verification

1. `GET /sessions` with no token → `401` (same pattern as the existing `POST /` route). *Not yet
   explicitly re-verified after implementation — inherited from `verify_token`'s existing behavior,
   same dependency used everywhere else.*
2. `POST /sessions` creates a row scoped to the caller's `user_id`; a different user's token can never
   see or create sessions under someone else's `user_id`. *Not yet explicitly tested with a second
   user — only one real account has been used for testing so far.*
3. **Confirmed 2026-08-13, live, through the real UI**: plant a fact in session A ("My favorite car
   is a Tesla Model 3"), confirm session B doesn't know it, switch back to session A and confirm it's
   correctly recalled — proves `threadId` switching actually reaches the backend and resumes the
   right LangGraph state, not just a fresh/empty run each time.
4. **Confirmed 2026-08-13, live**: `checkpoints.thread_id` for new messages shows
   `{user_id}:{session_id}` (integer, not a UUID) — verified directly via `psql`, cross-referenced
   against `user_sessions.id` and `user_registry.id`/`user_id` (see the worked example from this
   session: `user_registry.id=1` ↔ `user_sessions.id∈{1,2}` ↔ `checkpoints.thread_id∈{'...:1','...:2'}`).
4a. Sending a message in session A updates only session A's `user_sessions.access_ts` — session B's
    and every other user's sessions stay untouched. *Not yet explicitly re-verified against the live
    Postgres path after implementation* (the analogous CLI bug in this exact check, item 6 below, was
    real and only caught when directly asked to look for it — worth actually running this check, not
    assuming it's fine because the code looks right).
5. A brand-new user with zero sessions sees only the "+" button, no dropdown options, until they
   create their first session. **Confirmed 2026-08-13**, with a related bug found and fixed on the
   *returning*-user case (sessions already exist but none marked active on page load) — see the
   dropdown/`<select>` desync bug noted above.
6. CLI: `python -m agent.carqna_cli --session foo` twice in a row continues the same conversation
   (second run picks up prior context, confirmed via the local SQLite file's `checkpoints` table);
   `--session bar` starts a distinct one, with its own row in the local `sessions` table.
7. Two different OS users each running the CLI (each against their own `~/.carqna/carqna_cli.sqlite`)
   never interact at all, even using the identical `--session` name — confirms the original
   shared-Postgres collision this work was prompted by can no longer happen, structurally, not just by
   namespacing.
8. Confirm `copilotkit_server.py`/the web path is completely unaffected — still Postgres, still
   `AsyncPostgresSaver`, no SQLite involved there.
9. CLI: `python -m agent.carqna_cli --list` with no `--session` lists all local sessions
   (`session_name (id=session_id)`, one per line) without starting the agent; with zero sessions it
   prints a clear "No sessions yet." rather than crashing; omitting both `--list` and `--session`
   gives a clear argparse error instead of an unrelated one.
