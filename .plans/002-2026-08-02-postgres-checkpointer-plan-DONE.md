# Move the checkpointer from SQLite to Postgres

Status: **DONE, validated end-to-end 2026-08-06** by the project owner. Verification steps 1-7
(Postgres up, backend starts, `checkpointer.setup()` creates tables cleanly, multi-turn conversation
through the real UI, context survives a backend restart, `carqna.py` CLI runner too) all passed.
Step 8 (`mypy --strict`) was not run — optional, not a blocker. Not committed yet.

**Renamed 2026-08-13**: the Postgres role/database was originally named `convmem`; the project owner
renamed both to `carqna` directly against the infrastructure (`init_user.sh`, `.env`,
`.env.example`, `CLAUDE.md`, `readme-developmment.md`). All references below updated to match —
`carqna` is the current, correct name throughout; the bug and fix described are unaffected by the
rename, only the name itself changed.

**One real bug found and fixed along the way**: the first version of
`infrastructure/docker/postgres/initdb.d/init_user.sh` ran `GRANT ALL ON SCHEMA public TO carqna`
in the same `psql` session as `CREATE DATABASE carqna`, connected to `$POSTGRES_DB` (defaults to
`postgres`) — so the grant landed on the wrong database's `public` schema. PG15+ no longer grants
`CREATE` on `public` to `PUBLIC` by default (a real behavior change from PG14), and that revocation
is per-database, so `checkpointer.setup()`'s first `CREATE TABLE` failed with
`psycopg.errors.InsufficientPrivilege`. Fixed by giving the schema grant its own `psql` session
connected directly to `--dbname carqna`. Required dropping and recreating the `postgres-data`
Docker volume, since `initdb.d/` scripts only run once, on a fresh volume.

## Context

Multi-turn conversation state is currently persisted via `AsyncSqliteSaver` (a local `.db.sqlite3`
file), instantiated independently in both `src/agent/copilotkit_server.py` (the production entrypoint)
and `src/agent/carqna.py` (the local CLI runner). A `postgres` service was added to
`infrastructure/docker/docker-compose.yml` with a dedicated `carqna` user/database (created
automatically via `infrastructure/docker/postgres/initdb.d/init_user.sh` on first container start),
exposed on `localhost:5432`. This plan wires the code up to use it instead.

This is a small, self-contained change: `create_graph()` in `graph.py` already accepts any
checkpointer generically (`checkpointer=checkpointer` is passed straight through to
`create_deep_agent(...)`, `graph.py:215` — LangGraph's checkpointer interface is duck-typed, so
nothing there needs to change). The work is confined to the two places that construct a checkpointer,
plus config/docs. `carqna.py` moves to Postgres too (not just `copilotkit_server.py`), for one
consistent backend everywhere.

## Changes

- **`pyproject.toml`**: add `langgraph-checkpoint-postgres` as a dependency. Confirmed via PyPI's
  `requires_dist` metadata that it declares `psycopg>=3.2.0` and `psycopg-pool>=3.2.0` as hard
  transitive deps, so a driver is pulled in automatically — but that's plain `psycopg`, not
  `psycopg[binary]`, which needs `libpq` present on the system to actually connect or it can fail at
  import/runtime. Added `psycopg[binary]>=3.2.0` explicitly to guarantee a working install regardless
  of host `libpq` availability. Remove `langgraph-checkpoint-sqlite` and `aiosqlite`, unused once both
  entrypoints move off sqlite.

- **`src/agent/graph.py`**: replace `_get_checkpointer_conn_string()` (currently returns a sqlite
  file path from `CHECKPOINT_DB_PATH`) with an equivalent that builds a Postgres connection string
  from a new env var, `POSTGRES_URI`, defaulting to
  `postgresql://carqna:carqna@localhost:5432/carqna` for local dev. Stays the single shared helper
  both entrypoints call.

- **`src/agent/copilotkit_server.py`**: in `lifespan`, swap `AsyncSqliteSaver` for
  `AsyncPostgresSaver` (`from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver`). Same
  `async with ... as checkpointer:` shape, but Postgres needs one extra call sqlite didn't require:
  `await checkpointer.setup()` right after entering the context manager, before
  `create_graph(checkpointer=checkpointer)` — creates the checkpoint tables/runs migrations,
  idempotent so safe on every startup. Update the two inline comments that say *"sqlite3 now,
  Postgres planned later"*.

- **`src/agent/carqna.py`**: same swap in `main()`, using the same `_get_checkpointer_conn_string()`
  helper it already imports from `graph.py`.

- **`.env.example`**: replace `CHECKPOINT_DB_PATH=./.db.sqlite3` with
  `POSTGRES_URI=postgresql://carqna:carqna@localhost:5432/carqna`.

- **`CLAUDE.md`**: update "Required env vars" (swap `CHECKPOINT_DB_PATH` for
  `POSTGRES_URI`), fix the spots describing the checkpointer as SQLite-based.

- **`readme-developmment.md`**: note `docker compose up` now also starts `postgres` (no manual
  bootstrap needed, unlike OpenSearch's role setup).

- **Left alone**: `langgraph dev`/LangGraph Studio (`graph.py`'s module-level `graph` object) —
  LangGraph Platform/Studio manages its own persistence independently. The old `./.db.sqlite3` file
  is left in place, not migrated — Postgres starts with empty checkpoint tables.

## Verification

1. `docker compose -p '' -f infrastructure/docker/docker-compose.yml up -d` — confirm `carqna`
   user/database exist (container logs, or `psql postgresql://carqna:carqna@localhost:5432/carqna
   -c '\dt'` after the app runs once and calls `setup()`).
2. Start the backend (`python -m agent.copilotkit_server`) — confirm no errors from
   `checkpointer.setup()`.
3. Through the real UI (`npm run dev`, visit `/`), have a multi-turn conversation, then check
   Postgres directly has rows in the `checkpoints` table.
4. Restart the backend process, continue the same conversation (same thread) — confirm context
   survives a process restart.
5. `python -m agent.carqna` — confirm the CLI runner also connects to Postgres and multi-turn
   context works there too.
6. Backend: `mypy --strict src/` (skip `ruff`/`pytest` unless wanted back in scope).
