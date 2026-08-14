# Roadmap

Future work items — not scheduled, not part of any `.plans/` doc, just recorded so they're not
forgotten between sessions.

## Postgres connection pooling/configuration

Raised 2026-08-13 while reviewing `copilotkit_server.py`'s two separate Postgres connection paths.
Nothing here is broken today; recorded for whenever connection headroom under real concurrent load
becomes a concern.

- **`user_pool`** (`psycopg_pool.AsyncConnectionPool`, backs `user_registry` and the planned
  `user_sessions`): a real, configurable pool (`min_size`, `max_size`, `timeout`, `max_lifetime`,
  `max_idle`, `num_workers`), but currently constructed with none of that set
  (`AsyncConnectionPool(conn_string, open=False)`), so it falls back to the library defaults —
  confirmed via source that `max_size` defaults to `min_size` when left `None`, so this is currently a
  **fixed pool of exactly 4 connections**, not elastic. Worth tuning explicitly once real traffic
  patterns are known.
- **The checkpointer** (`AsyncPostgresSaver.from_conn_string()`, holds all conversation state — the
  busier of the two paths, since every chat message goes through it): confirmed via source that this
  is **not pooled at all** — a single raw `psycopg.AsyncConnection`, opened once and reused for the
  entire process lifetime across every user and every request. Worth investigating whether
  `langgraph-checkpoint-postgres` supports being handed a pool instead of a single connection, or
  whether this becomes an actual bottleneck under concurrent load before making any change.

## Test coverage

From `001-2026-07-24-copilot-ui-remediation-plan-DONE.md` (F3, deferred by explicit choice, not an
oversight):
- **`carqna-agent`**: zero real unit/integration tests. The old template tests (`tests/unit_tests`,
  `tests/integration_tests`) were deleted rather than fixed — they were stale LangGraph-template
  boilerplate that never matched this project's actual `messages`-based graph input. CI's `pytest`
  step currently tolerates exit code 5 ("no tests collected") until real tests exist
  (`.github/workflows/unit-tests.yml`).
- **`carqna-copilot-ui`** (sibling repo): zero test files/config beyond the default
  `eslint-config-next`. Original F3 called out `useCarqnaChat`/`carqnaService`'s SSE buffering
  edge-case logic and `EventTraceViewer`'s event bucketing specifically, but those files are long
  since deleted (replaced by the real `CopilotChat` integration) — the underlying point (no test
  framework at all) is still true today, just needs fresh targets identified when picked up.

## Auth0 → Auth.js provider-portability reference doc

From `004-2026-08-09-oauth2-okta-auth-plan-DONE.md`'s "Provider portability" section: sketched but
never written, intentionally — a reference doc for *if/when* a provider swap is ever wanted, not
something to build now. Would live at `.plans/00N-oauth2-provider-portability-guide.md` (renumber —
the slot originally suggested, `005`, was later used for `user-tracking-plan` instead). Content
already scoped in `004`: what's Auth0-specific today (frontend package/session format) vs. already
portable (backend's plain JWT/JWKS verification, zero code changes needed for a swap); recommended
target **Auth.js (NextAuth.js)** for its pluggable-provider model; high-level swap steps; the one real
caveat (existing sessions invalidate on cutover, cookie format differs between SDKs).

## `user_registry` refinements

From `005-2026-08-10-user-tracking-plan-DONE.md`'s "Explicitly out of scope":
- **Stale `email`/`name` never refreshed** — `track_user` is insert-once for profile fields; if a
  user changes their email/display name in Auth0, `user_registry` never picks up the change. Could
  add a periodic or on-demand refresh (e.g. re-fetch `/userinfo` if `last_seen_at` is older than some
  threshold) if this ever matters in practice.
- **A true per-event user-activity log** — `user_registry` is deliberately one row per user (identity
  + first/last-seen presence), not an activity log. A separate table (many rows per user, one per
  login/request/event) was explicitly named as a possible future addition when `users` was renamed to
  `user_registry` specifically to leave room for this without a name collision — still unbuilt.

## Session management follow-ons

From `006-2026-08-11-session-management-plan-DONE.md`'s "Explicitly deferred" — `006` itself
(including the web/backend portion) is now fully implemented and closed out; these three pieces were
deliberately left out of it and remain unbuilt:
- **Session rename.**
- **Session delete.**
- **Restoring visual chat history when switching to an existing session** — today, switching sessions
  correctly resumes the real conversation (LangGraph has full context from the first new message),
  but the chat UI doesn't re-display prior messages on screen until then. `006` already sketches what
  this needs: a new endpoint reading the LangGraph checkpoint for `{user_id}:{session_id}` and
  returning it in AG-UI's message shape, plus frontend wiring to populate `agent.messages` on
  session-select.
