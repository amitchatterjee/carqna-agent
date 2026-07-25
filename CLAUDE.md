# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`carqna-agent` is the backend half of **CarQnA**, an automobile-help multi-agent system built on
LangGraph + [DeepAgents](https://github.com/langchain-ai/deepagents). It's paired with the sibling
repo `carqna-copilot-ui` (Next.js + CopilotKit frontend, `~/git/carqna-copilot-ui`), which talks to
this service over Dapr/HTTP. This repo owns the agent logic itself plus the infrastructure (Docker
Compose, OpenSearch, Dapr, nginx) needed to run the whole stack locally.

Do not read or reference the `.plans/` folder — it is out of scope.

## Commands

Uses `uv`/`pip` with `pyproject.toml`. Package name is `agent`, source lives under `src/agent`.

```bash
# install
pip install -e . "langgraph-cli[inmem]"

# run all unit tests
make test                        # == python -m pytest tests/unit_tests/
make test TEST_FILE=tests/unit_tests/test_configuration.py   # single file
python -m pytest tests/unit_tests/test_configuration.py::test_name   # single test

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
  SQLite-backed checkpointer, single fixed `thread_id="carqna-local-session"` for continuity across
  turns. Useful for quickly exercising the agent without Studio or the frontend.
- **`python -m agent.carqna_dapr`** — the aiohttp HTTP service (`src/agent/carqna_dapr.py`) that
  `carqna-copilot-ui` actually talks to via a Dapr sidecar. Exposes `/health`, `/metadata`,
  `/invoke/chat`, `/invoke/chat/stream` (SSE), `/invoke/init`, plus several CopilotKit-compatible
  aliases (`/agent`, `/agent/threads`, `/invoke/info`, ...) because CopilotKit probes multiple path
  shapes. `create_graph` is imported lazily inside `initialize_agent()` so the module can be imported
  without MCP being reachable.

Required env vars (see `.env.example`): `ANTHROPIC_API_KEY`, `MCP_CONFIG_PATH`,
`INSURANCE_DOCS_ROOT`, `LLM_MODEL` (defaults to `claude-sonnet-4-5-20250929`), `PROMPTS_DIR`
(defaults to `.`), `CHECKPOINT_DB_PATH` (defaults to `./.db.sqlite3`). For the Dapr service also
`DAPR_SERVICE_PORT`/`DAPR_SERVICE_NAME`.

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

**Event formatting for the frontend**: `carqna_dapr.py`'s `_format_graph_event` / `_extract_command_final_text`
translate LangGraph's `astream_events` (`on_tool_start`/`on_tool_end`/`on_llm_start`/`on_llm_end`/`on_chain_end`)
into a flat `GraphEvent` model (`tool_call` / `tool_result` / `llm_thinking` / `final_response` / `error`)
consumed by the CopilotKit UI over SSE. Because subagents are invoked as a task/delegation tool, their
final answer can arrive as a `Command` payload on a `tool_end` event rather than a `chain_end` event —
`_extract_command_final_text` handles both shapes (and a couple of stringified-Command fallbacks). If you
touch event streaming, be careful not to break this dual-path detection.

## Infrastructure

`infrastructure/` holds everything needed to run the full stack in Docker:

- `docker/docker-compose.yml` — four services: `opensearch` (custom image with the MCP plugin, built
  from `docker/opensearch-mcp/`), `carqna-dev` (this agent, running `carqna_dapr`), `carqna-dapr` (the
  Dapr sidecar `daprd`, routes via `-app-id=carqna-service`), and `carqna-copilot-ui` (built from the
  sibling frontend repo via a relative build context `../../../carqna-copilot-ui`). Note the frontend
  talks to the agent through the Dapr sidecar URL
  (`http://localhost:3500/v1.0/invoke/carqna-service/method/agent`), not directly.
- `admin/opensearch/*.ndjson` — role/user/rolesmapping fixtures for OpenSearch security. Applied with
  the `curl`/`jq` loops documented in `readme-developmment.md`. Default fixture users: `alice`
  (read-only on `msrp-*`), `bob` (read/write on `msrp-*`); the MCP config's `Authorization` header
  uses Alice's credentials.
  ```bash
  docker compose -f ./infrastructure/docker/docker-compose.yml build carqna-dev
  docker compose -p '' -f ./infrastructure/docker/docker-compose.yml up -d
  ```
- `conf/mcp/opensearch/` — `agent.ndjson` / `mcp-tools.json` registered against OpenSearch's
  `_plugins/_ml/agents/_register` and `_plugins/_ml/mcp/tools/_register` endpoints to stand up the
  AutoGeek MCP tool.
- `conf/raven/mcp.json` — MCP config variant used elsewhere; keep credentials in sync with the
  OpenSearch user fixtures if you change them.
- `docker/dapr-config.yaml`, `docker/nginx.conf` — Dapr sidecar and reverse-proxy config.

`readme-developmment.md` has the full copy-pasteable sequence for bootstrapping OpenSearch users/roles
and registering the MCP agent/tools — consult it before re-deriving these steps.

## Conventions

- `ruff` lint config in `pyproject.toml`: Google-style docstrings (`D401` imperative first line
  required), `E501` (line length) and a few `UP0xx` rules ignored; tests are exempt from `D`/`UP`.
- `mypy --strict` on `src/` — new code should be fully typed.
