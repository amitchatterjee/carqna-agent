# Distributed OTel tracing across carqna-agent and carqna-copilot-ui

Status: **DONE, validated 2026-08-08.** Distributed tracing confirmed working end-to-end — a single
request produces one linked trace in Jaeger spanning both `carqna-copilot-ui` and `carqna-agent`.

**Two real bugs found and fixed beyond the original plan** (both confirmed via live Jaeger trace
inspection, not guessed):

1. **Frontend: `@vercel/otel`'s automatic fetch instrumentation never covered the outbound call at
   all.** Its "fetch" span type is `AppRender.fetch` — scoped to the App Router's rendering
   pipeline, which Route Handlers (`app/api/copilotkit/route.ts`) don't go through. Confirmed by
   pulling the actual trace JSON from Jaeger: zero fetch spans under the route handler's span.
   Fixed by passing a custom `fetch` to `HttpAgent` in `route.ts` that manually creates a span and
   injects `traceparent`/`tracestate` via `@opentelemetry/api`'s `propagation.inject()`, instead of
   relying on Next's auto-instrumentation. Also: even where that auto-instrumentation *does* apply,
   its own types (`FetchInstrumentationConfig.propagateContextUrls`) confirm it only propagates
   context to Vercel deployment URLs by default — a local backend URL needs explicit opt-in either
   way.

2. **Backend: `FastAPIInstrumentor.instrument_app(app)` silently never activated when called from
   inside `lifespan()`.** Confirmed in Starlette's own source
   (`starlette/applications.py:86-90`): `Starlette.__call__` builds and caches
   `self.middleware_stack` on its *very first* invocation, guarded by `if self.middleware_stack is
   None`, regardless of ASGI scope type. The first scope any ASGI app receives is `lifespan` itself
   (sent by uvicorn before any HTTP request) — so by the time our `lifespan()` function *body*
   executes, the stack is already built and cached. `FastAPIInstrumentor` works by monkey-patching
   `build_middleware_stack`, so patching it from inside `lifespan()` patches a method that will
   never run again. Fixed by moving `FastAPIInstrumentor.instrument_app(app)` to module level,
   immediately after `app = FastAPI(...)` — before uvicorn can send the app any scope at all.
   `langsmith.Client()` (which registers the global `TracerProvider`) stays inside `lifespan()` —
   that only needs to finish before real *requests* arrive, not before instrumentation is installed,
   since span creation itself is lazy/per-request.

Diagnosis path that found these: captured the UI-side trace JSON directly from Jaeger's API
(`grep '"type":"MESSAGES_SNAPSHOT"' ...` equivalent for traces — `curl`'d the trace by ID), which
showed the exact span hierarchy and let both bugs be pinpointed from source code rather than guessed
at from symptoms alone.

## Context

Jaeger tracing already works on the backend (`carqna-agent`, via `langsmith`'s built-in OTel exporter
— pure env-var config, no code). But traces from the UI and the agent don't link up into one
distributed trace yet. Two gaps, confirmed by reading the actual installed package source rather than
assuming:

1. **Frontend has no OTel instrumentation at all** — nothing generates spans or injects
   `traceparent`/`tracestate` headers on its outgoing request to the backend.
2. **Backend has no HTTP-level instrumentation** — `~/carqna.venv` has zero
   `opentelemetry-instrumentation-*` packages installed. `copilotkit_server.py`'s only tracing is
   LangSmith's own LangChain-run-level exporter, which starts a fresh root trace per invocation with
   no awareness of any incoming `traceparent` header — so even once the frontend sends one, nothing
   reads it.

This plan closes both gaps. `app/api/copilotkit/route.ts`'s `HttpAgent` (from `@ag-ui/client`) calls
the backend with plain global `fetch()` (confirmed in `@ag-ui/client/dist/index.mjs`:
`this.fetch = e.fetch ?? ((e, t) => fetch(e, t))`), which is exactly what Next.js auto-instruments —
so no changes needed to `route.ts` itself, only to how OTel is bootstrapped for the app.

## Backend (`carqna-agent`)

- **`pyproject.toml`**: add `opentelemetry-instrumentation-fastapi` as a dependency.

- **`src/agent/copilotkit_server.py`**: two additions —
  1. Explicitly instantiate a `langsmith.Client()` early in `lifespan()`, before `create_graph(...)`
     runs. This is the trigger that makes LangSmith register its OTel `TracerProvider` as the
     **process-global** one (confirmed in `langsmith/client.py:1364-1390`: on `Client.__init__`, if
     `tracing_mode` is `otel`/`hybrid` and no real provider is already registered globally, LangSmith
     builds one from `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_SERVICE_NAME` and calls
     `opentelemetry.trace.set_tracer_provider(...)`). Doing this explicitly — rather than relying on
     it happening implicitly, lazily, whenever LangChain first constructs a traced call — removes any
     ambiguity about whether it's registered before the app starts serving requests.
  2. Call `FastAPIInstrumentor.instrument_app(app)` (from
     `opentelemetry.instrumentation.fastapi`) on the `app` object, after the `Client()` step above.
     This auto-instruments every incoming request: extracts `traceparent`/`tracestate` from request
     headers via the standard W3C propagator, starts a span parented to that remote context, and
     (since it uses `trace.get_tracer_provider()` — now correctly pointing at LangSmith's provider
     from step 1) exports to the same Jaeger destination. LangChain/LangSmith's own spans, created
     later in the same request while this span is the ambient active span, will nest as its children
     — OTel context propagation is independent of which `TracerProvider` object different libraries
     use; it's the shared ambient `Context` that determines parent/child, so ordering (`Client()`
     before `instrument_app()`, both before the app starts serving) is what matters, not needing both
     libraries to share one provider instance explicitly.

  Net effect: one continuous trace per request, from the incoming HTTP call through every LangChain
  span underneath it.

- **Not in scope** (flagging, not building): instrumenting the backend's own *outbound* calls (to
  Anthropic, to the OpenSearch MCP server via the `httpx.AsyncClient` factory in `graph.py`) — that's
  a separate, optional enhancement (`opentelemetry-instrumentation-httpx`) beyond what's needed to
  link the UI trace through to the existing LangChain spans. `carqna_cli.py` also isn't touched — it
  has no incoming HTTP request to extract a trace context from.

## Frontend (`carqna-copilot-ui`)

- **`package.json`**: add `@vercel/otel`, `@opentelemetry/sdk-logs`, `@opentelemetry/api-logs`,
  `@opentelemetry/instrumentation` — the official Next.js-recommended set
  (`node_modules/next/dist/docs/.../open-telemetry.md`, confirmed current for this exact Next.js
  version rather than assumed from general knowledge, per this repo's own `AGENTS.md` warning about
  API drift).

- **New file `instrumentation.ts`** at the project root (sibling to `package.json`/`app/`, *not*
  inside `app/`):
  ```ts
  import { registerOTel } from '@vercel/otel'

  export function register() {
    registerOTel({ serviceName: 'carqna-copilot-ui' })
  }
  ```
  No explicit exporter wiring needed — confirmed via `@vercel/otel`'s own docs that it reads standard
  `OTEL_EXPORTER_OTLP_ENDPOINT` from the environment when no `traceExporter` is passed explicitly.

- **New/updated env var**: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` in a new
  `.env.local` (this repo currently has zero `.env*` files — confirmed). **Note the asymmetry with
  the backend**: the backend's `.env` needed the full `.../v1/traces` path because LangSmith's Python
  wrapper passes the endpoint straight to the exporter's constructor, bypassing its own
  path-auto-append logic. `@vercel/otel`'s underlying OTLP exporter does *not* have that same
  bypass — it appends `/v1/traces` itself when given a bare base URL — so the frontend's value should
  be the bare `http://localhost:4318`, *not* `http://localhost:4318/v1/traces`. Worth confirming once
  it's running rather than assuming symmetry with the backend value, same lesson as before.

- **New file `.env.example`** (doesn't exist yet in this repo either) documenting
  `OTEL_EXPORTER_OTLP_ENDPOINT` and the existing-but-previously-undocumented `CARQNA_AGENT_URL`
  (currently only has a hardcoded fallback in `app/api/copilotkit/route.ts`, confirmed no
  `.env.example` exists to document it) — small addition, matches the pattern already established
  for `carqna-agent`.

## Verification

1. Backend: restart `python -m agent.copilotkit_server`, confirm no errors from the new
   `Client()`/`FastAPIInstrumentor` setup, and that Jaeger traces still appear as before.
2. Frontend: restart `npm run dev`, confirm no build/runtime errors from `instrumentation.ts`.
3. Ask a question through the real UI (`http://localhost:3000`). In Jaeger's UI
   (`http://localhost:16686`), look for a single trace that starts with a `carqna-copilot-ui` span
   (the incoming Next.js request / outgoing fetch to the agent) and has the existing LangChain/tool
   spans nested underneath it as children — not two separate, disconnected traces from
   `carqna-copilot-ui` and `carqna-agent`/`langsmith`.
4. If they still show up disconnected: check the `traceparent` header is actually present on the
   `POST /` request from the UI to `copilotkit_server.py` (browser Network tab or a backend-side log)
   — confirms whether the gap is missing propagation (frontend side) or missing extraction (backend
   side).
