# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`carqna-agent` is the backend half of **CarQnA**, an automobile-help multi-agent system built on
LangGraph + [DeepAgents](https://github.com/langchain-ai/deepagents). It's paired with the sibling
repo `carqna-copilot-ui` (Next.js + CopilotKit frontend, `~/git/carqna-copilot-ui`), which talks to
this service over the AG-UI protocol via a Next.js `CopilotRuntime` route, not directly and not
through Dapr. This repo owns the agent logic itself plus the infrastructure (Docker Compose,
OpenSearch, Postgres) needed to run it locally — Docker is used for OpenSearch and Postgres only;
the agent and frontend both run directly on the host.

Do not read or reference the `.plans/` folder — it is out of scope.

## Commands

Uses `uv`/`pip` with `pyproject.toml`. Package name is `agent`, source lives under `src/agent`.

```bash
# install (into an already-activated venv -- see readme-developmment.md for the
# Python-version gotcha with the langgraph-cli[inmem] -> jsonschema-rs build)
uv sync --active

# run all unit tests
make test                        # == python -m pytest tests/unit_tests/ (currently empty, stale template tests removed)

# integration tests (hit a real graph invocation)
make integration_tests

# watch mode
make test_watch

# lint / format / type-check (ruff + mypy --strict, matches CI)
make lint
make format
make spell_check                 # codespell, config in pyproject.toml

# run the LangGraph dev server + Studio UI
langgraph dev
```

CI (`.github/workflows/unit-tests.yml`) runs on Python 3.11/3.12: `ruff check .`, `mypy --strict src/`,
codespell on `README.md` and `src/`, then `pytest tests/unit_tests`. Match that locally before pushing.

## Running the agent

Three different entry points expose the same `create_graph()` agent for different contexts:

- **`langgraph dev`** — LangGraph Studio, driven by `langgraph.json` (`graphs.agent` →
  `src/agent/graph.py:graph`). Persistence handled automatically by the LangGraph API.
- **`python -m agent.carqna`** — interactive local CLI runner (`src/agent/carqna.py`) with a
  Postgres-backed checkpointer, single fixed `thread_id="carqna-local-session"` for continuity across
  turns. Useful for quickly exercising the agent without Studio or the frontend.
- **`python -m agent.copilotkit_server`** — the FastAPI service (`src/agent/copilotkit_server.py`)
  that `carqna-copilot-ui` actually talks to, via `app/api/copilotkit/route.ts` (a Next.js
  `CopilotRuntime` route), not directly. Exposes the graph over the **AG-UI protocol**: a single
  `POST /` endpoint (via `copilotkit.LangGraphAGUIAgent` +
  `ag_ui_langgraph.add_langgraph_fastapi_endpoint`, streaming raw AG-UI SSE events) plus an
  auto-added `GET /health`. `create_graph` is imported lazily inside the FastAPI `lifespan` so the
  module can be imported without MCP being reachable. This replaced an earlier hand-rolled aiohttp
  service (`carqna_dapr.py`, since deleted) that guessed at a REST/SSE + CopilotKit
  discovery-protocol shape that turned out not to be how CopilotKit actually integrates — see
  `.plans/001-2026-07-24-copilot-ui-remediation-plan-DONE.md` for that history. Multi-turn state
  uses a Postgres checkpointer (`AsyncPostgresSaver`, the `carqna` database — see
  `.plans/002-2026-08-02-postgres-checkpointer-plan-DONE.md`), shared with `carqna.py` via
  `graph.py`'s `_get_checkpointer_conn_string()` helper. Postgres requires an explicit
  `await checkpointer.setup()` call (idempotent, run on every startup) that SQLite never needed.

Required env vars (see `.env.example`): `ANTHROPIC_API_KEY`, `MCP_CONFIG_PATH`,
`INSURANCE_DOCS_ROOT`, `LLM_MODEL` (defaults to `claude-sonnet-4-5-20250929`), `PROMPTS_DIR`
(defaults to `.`), `POSTGRES_URI` (defaults to the local `carqna` Postgres database).

Optional: `LANGSMITH_TRACING_V2=true` + `LANGSMITH_TRACING_MODE=hybrid|otel` +
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces`/`OTEL_SERVICE_NAME` route
LangChain/LangGraph traces to a local Jaeger instance (the `jaeger` service in `docker-compose.yml`,
UI at `http://localhost:16686`) via `langsmith`'s built-in OTel exporter — no extra dependencies or
code changes needed, it's pure env config. `hybrid` also keeps sending to LangSmith cloud
(`LANGSMITH_API_KEY`/`LANGSMITH_PROJECT` above); use `otel` to send to Jaeger only. **Must be port
`4318` (HTTP) with the `/v1/traces` path, not `4317`/gRPC** — `langsmith`'s exporter is hardcoded to
HTTP and passes this value straight to the exporter without appending the path itself, unlike the
OTel SDK's own env-var handling (see `.env.example`'s comment for the full explanation).

## Architecture

The agent is a **DeepAgents supervisor/subagent graph**, all built in `src/agent/graph.py`:

- `main_agent` (system prompt: `main_agent.md`) — orchestrator. Routes user questions to one of two
  specialist subagents and is responsible for merging their answers with source attribution.
- `car_price_expert` subagent (system prompt: `car_price_agent.md`) — has the MCP tools (see below)
  for car pricing/spec lookups. Must call `ListIndexTool` before `SearchIndexTool`; never guesses an
  OpenSearch index name.
- `insurance_expert` subagent (system prompt: `insurance_agent.md`) — has **read-only** filesystem
  access (via `FilesystemPermission`, write/delete explicitly denied) to a directory of per-state
  insurance handbooks (`INSURANCE_DOCS_ROOT`), and answers by exploring/reading/searching that tree.
  Prompt encodes state-name normalization rules (e.g. "NC" → "North-Carolina").

Both subagents are required to attribute answers as tool-sourced vs. `[Based on LLM training data]` —
this convention is load-bearing across all three prompt files and should be preserved if prompts are
edited.

**Tools**: MCP tools come from `AutoGeek`, an OpenSearch-backed MCP server described in
`infrastructure/conf/mcp/config.json` (`MCP_CONFIG_PATH`). The MCP client and tool list are module-level
singletons (`_mcp_client`, `_mcp_tools` in `graph.py`), initialized once and kept alive — don't
re-initialize per request. TLS verification is disabled for the self-signed OpenSearch cert via a
custom `httpx_client_factory`.

**Prompts as files, not code**: `main_agent.md`, `car_price_agent.md`, `insurance_agent.md` at the repo
root are loaded at graph-build time via `_load_prompt_from_file()` (relative to `PROMPTS_DIR`). Editing
agent behavior is often just editing these markdown files rather than Python.

**Event formatting for the frontend**: handled entirely by the `ag-ui-langgraph` package now (see
`copilotkit_server.py`) — it translates LangGraph's `astream_events` into standard AG-UI SSE frames
(`TOOL_CALL_START/ARGS/END`, `TEXT_MESSAGE_START/CONTENT/END`, etc.) itself. There's no bespoke
event-formatting code left in this repo; the old hand-rolled translation (`carqna_dapr.py`'s
`_format_graph_event`/`_extract_command_final_text`, which had to special-case subagent delegation
returning a `Command` payload on a `tool_end` event) was deleted along with that file. If a subagent
delegation's final answer doesn't seem to be surfacing correctly on the frontend, that's now an
AG-UI/`ag-ui-langgraph` question, not something to patch in this repo.

## Infrastructure

Docker is used for `opensearch`, `postgres`, and `jaeger` only. The agent (`copilotkit_server.py`) and
`carqna-copilot-ui` both run directly on the host (see `readme-developmment.md`) — this replaced an
earlier setup that also containerized the agent and frontend behind a Dapr sidecar (`carqna-dapr`)
and an nginx reverse proxy; that whole layer (`carqna-dev`/`carqna-copilot-ui`/`carqna-dapr`
Dockerfiles, `dapr-config.yaml`, `nginx.conf`) has been removed, since host-based dev was always the
actual flow used to validate this app and the extra containers/proxy added nothing.

- `docker/docker-compose.yml` — three services: `opensearch` (custom image with the MCP plugin,
  built from `docker/opensearch-mcp/`), `postgres` (checkpointer storage — the `carqna`
  user/database are created automatically on first start by
  `docker/postgres/initdb.d/init_user.sh`, no manual bootstrap needed, unlike OpenSearch below), and
  `jaeger` (`jaegertracing/all-in-one`, OTLP receiver on `4317`/gRPC and `4318`/HTTP, UI on
  `16686` — see the OTel env vars above).
  ```bash
  docker compose -p '' -f ./infrastructure/docker/docker-compose.yml up -d
  ```
- `admin/opensearch/*.ndjson` — role/user/rolesmapping fixtures for OpenSearch security. Applied with
  the `curl`/`jq` loops documented in `readme-developmment.md`. Default fixture users: `alice`
  (read-only on `msrp-*`), `bob` (read/write on `msrp-*`); the MCP config's `Authorization` header
  uses Alice's credentials.
- `conf/mcp/opensearch/` — `agent.ndjson` / `mcp-tools.json` registered against OpenSearch's
  `_plugins/_ml/agents/_register` and `_plugins/_ml/mcp/tools/_register` endpoints to stand up the
  AutoGeek MCP tool.
- `conf/raven/mcp.json` — MCP config variant used elsewhere; keep credentials in sync with the
  OpenSearch user fixtures if you change them.

`readme-developmment.md` has the full copy-pasteable sequence for bootstrapping OpenSearch users/roles
and registering the MCP agent/tools — consult it before re-deriving these steps.

## Conventions

- `ruff` lint config in `pyproject.toml`: Google-style docstrings (`D401` imperative first line
  required), `E501` (line length) and a few `UP0xx` rules ignored; tests are exempt from `D`/`UP`.
- `mypy --strict` on `src/` — new code should be fully typed.
