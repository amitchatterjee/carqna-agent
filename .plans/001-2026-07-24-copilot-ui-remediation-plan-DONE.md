# carqna-copilot-ui: Code Audit & Remediation Plan

Status: **DONE (2026-08-01)**. Every issue in the audit below (A1-A5, B1-B2, C1-C5, D1-D5, E1-E3)
and every step in the phased plan (Steps 1-7) is implemented and validated end-to-end by the project
owner — real backend, real frontend, real chat turns, persistent tool-call panels, multi-turn
context. Docker was simplified further than originally planned (opensearch only; see the "Dapr
sidecar routing" decision). The one deliberately open item is test coverage (F3) — deferred by
explicit choice, not an oversight. Kept in `.plans/` for future reference rather than deleted.

The rest of this document is left as-was (the original audit + plan), with status notes inserted
at the relevant points, so the history of what was found and why each decision was made stays
intact.

---

This is an analysis of `~/git/carqna-copilot-ui` as it stood on 2026-07-24 (last commits at the
time: `28d46a0 Fixed the final response - more work needed`, `e75fcc9 Fixed UI - work in progress`),
plus the plan that was then executed to fix it.

## TL;DR

The app installs and mounts `@copilotkit/react-core` / `@copilotkit/react-ui`, but **doesn't
actually use CopilotKit for anything**. Every piece of chat behavior — message list, streaming,
markdown rendering, tool-call/reasoning display — is hand-rolled in `useCarqnaChat.ts` +
`ChatInterface.tsx` + `EventTraceViewer.tsx` on top of raw `fetch`/SSE parsing against a bespoke
event schema (`GraphEvent`) that the backend (`carqna_dapr.py`) also hand-rolls. CopilotKit ships a
purpose-built solution for exactly this shape of problem — a LangGraph agent that streams tool
calls and reasoning to a chat UI — called **CoAgents** (`useCoAgent`, `useCoAgentStateRender`,
`CopilotChat`), and the backend already half-attempts to speak the CopilotKit discovery protocol
(`copilot_info`, `/agent/threads`, `"Intercepted CopilotKit discovery packet"`) without following
the real wire protocol. That combination — CopilotKit installed and partially shimmed for, but not
actually driving the UI — is the root of the "not fully utilizing CopilotKit" feeling and most of
the tech debt below.

This doc:
1. Lists concrete issues found, file-by-file, with severity.
2. Lays out the one real decision to make (go all-in on CopilotKit's LangGraph integration, or
   drop CopilotKit and keep/clean up the custom chat) with a recommendation.
3. Proposes a phased plan for the recommended path.

## Codebase inventory (for reference)

```
app/layout.tsx                 — Next.js root layout (default create-next-app template, unbranded)
app/page.tsx                   — mounts <CopilotKit> provider + <ChatInterface>
src/components/ChatInterface.tsx    — hand-built chat UI (message list, input box, session button)
src/components/EventTraceViewer.tsx — renders raw agent event trace as the "assistant response"
src/hooks/useCarqnaChat.ts     — hand-rolled chat state machine over the custom SSE service
src/services/carqnaService.ts  — fetch + manual SSE line-buffering client for carqna_dapr endpoints
src/types/carqna.ts            — GraphEvent/ChatResponse/InitResponse types mirroring carqna_dapr.py
```

No test files, no Storybook/component tests, no `src/app/api/copilotkit` route (i.e. no CopilotKit
runtime route at all).

## Issues found

### A. Architectural — CopilotKit is installed but structurally bypassed

| # | Where | Issue |
|---|-------|-------|
| A1 | `app/page.tsx` | `<CopilotKit runtimeUrl={...} publicApiKey="">` wraps the tree, but nothing under it calls a single CopilotKit hook or renders a single CopilotKit component (`grep` for `useCopilotAction`, `useCoAgent`, `CopilotChat`, `CopilotSidebar`, `CopilotPopup`, `useCopilotReadable`, `useCopilotChat` across `app/` + `src/` returns zero matches). The provider is dead weight: it opens a connection to `runtimeUrl` that nothing consumes, and ships `@copilotkit/react-ui/styles.css` for components that are never rendered. |
| A2 | `app/page.tsx` | `runtimeUrl={process.env.NEXT_PUBLIC_API_BASE}` points **directly** at the raw Dapr-fronted agent endpoint. CopilotKit's `runtimeUrl` is meant to point at a `CopilotRuntime` instance (typically your own Next.js `app/api/copilotkit/route.ts`), which speaks the CopilotKit/AG-UI wire protocol and internally proxies to a `LangGraphAgent`/`LangGraphHttpAgent`. Pointing it at a raw REST/SSE service is a protocol mismatch — it only "works" because nothing on the frontend actually issues a CopilotKit request through it. |
| A3 | `carqna_dapr.py` (backend) | The service half-implements CopilotKit's discovery/threads protocol by hand: `copilot_info()`, `copilot_threads_handler()`, and `chat_stream()` sniffing `data.get("method") == "info"` to special-case "Intercepted CopilotKit discovery packet" (see `carqna_dapr.py:452-454`, `:623-651`). This is a partial, guessed reimplementation of what the CopilotKit Python SDK (`copilotkit` package, `copilotkit.langgraph`, `CopilotKitRemoteEndpoint`/`LangGraphAgent`) or the AG-UI protocol (`TEXT_MESSAGE_*`, `TOOL_CALL_*`, `RUN_STARTED`/`RUN_FINISHED` SSE events) already provide, but it doesn't match either spec closely enough for real CopilotKit components to consume it. It's effectively dead code today since the frontend never hits these endpoints as CopilotKit — it's evidence of an earlier half-finished integration attempt. |
| A4 | Both repos | `carqna-agent` already exposes a standard **LangGraph Platform** graph (`langgraph.json` → `src/agent/graph.py:graph`, runnable via `langgraph dev`). CopilotKit's `LangGraphAgent`/`LangGraphHttpAgent` can point directly at a LangGraph Platform deployment or a FastAPI-wrapped graph. That means the custom `carqna_dapr.py` REST/SSE surface doesn't need to exist for the *CopilotKit* integration path. **Resolved**: per the Dapr decision below, the Dapr service-mesh story is being dropped, so this is no longer a fork — `carqna-agent` should be exposed directly (LangGraph Platform API or the `copilotkit` SDK mounted on `carqna_dev`), not through `carqna-dapr`. |
| A5 | `ChatInterface.tsx`, `EventTraceViewer.tsx` | Because CopilotKit's `CopilotChat`/`useCopilotChat` (message list + streaming markdown out of the box) and `useCoAgentStateRender` (render intermediate agent state / tool calls / reasoning) aren't used, this repo reimplements: message list state, a send box, a "thinking..." indicator, and a bespoke `EventTraceViewer` that manually buckets events into tool/reasoning/final-response sections. This is a lot of custom code solving a problem CopilotKit ships pre-built. |

### B. Duplicate / conflicting session (thread) initialization

| # | Where | Issue |
|---|-------|-------|
| B1 | `app/page.tsx:13-30` vs `ChatInterface.tsx:13-17` (`useCarqnaChat.startSession`) | **Two separate calls to `initConversation("user-123")`** happen: once on mount in `page.tsx` (result stored in `activeThreadId`, passed to `<CopilotKit threadId=...>`), and again when the user clicks "Start New Session" (result stored in the hook's own `threadId`, which is what `ChatInterface`/`streamChat` actually use). These produce **two different `thread_id`s**, and only the second one is ever used for chat. `activeThreadId`/`CopilotKit`'s `threadId` prop is pure dead state — it's computed, passed down, and then never read by anything that talks to the backend. |
| B2 | `app/page.tsx:32` | While `loading` is true (waiting on the *first, unused* `initConversation` call) the whole app renders `"Initializing secure workspace..."` with no styling and blocks render of `<ChatInterface>` — i.e. users wait on a network call whose result is thrown away. |

### C. Dead code

| # | Where | Issue |
|---|-------|-------|
| C1 | `useCarqnaChat.ts:25-27,49-50,58-59` | `toolsUsed`, `fullTrace`, `finalResponseText` are computed on every event and returned from the hook (`:78`), but `ChatInterface` only destructures `{ threadId, isLoading, events, error, startSession, sendMessage }` — none of these three are consumed anywhere. They duplicate what's already derivable from `events` (which `EventTraceViewer` re-derives itself with its own `.filter()` calls). |
| C2 | `carqnaService.ts:68-71` | `getHealth()` is exported and never called. |
| C3 | `types/carqna.ts:29-34` | `ChatSession` interface is defined and never used. |
| C4 | `public/*.svg` | Default create-next-app placeholder assets (`file.svg`, `globe.svg`, `next.svg`, `vercel.svg`, `window.svg`) — unused, left over from scaffolding. |
| C5 | `app/layout.tsx:15-18` | `metadata` is still the create-next-app default (`title: "Create Next App"`) — never rebranded to CarQnA. |

### D. Resilience / correctness gaps

| # | Where | Issue |
|---|-------|-------|
| D1 | `carqnaService.ts:streamChat` | `fetch` has no `AbortController`/`signal`. If a user starts a new session or sends a second message while a previous stream is still being read, the old `reader.read()` loop keeps running in the background and keeps calling the (stale-closure) `onEvent` callback — no cancellation, possible duplicate/out-of-order event application to whichever component is still mounted. |
| D2 | `useCarqnaChat.ts` / `ChatInterface.tsx` | No `useEffect` cleanup / unmount guard around the streaming loop — if `ChatInterface` unmounts mid-stream, `setEvents`/`setToolsUsed`/etc. will still fire ("set state on unmounted component"). |
| D3 | `carqnaService.ts:14,31` | Errors surface only as `res.statusText` (e.g. "Internal Server Error") — the backend's actual JSON error body (`{"error": ..., "type": ...}`, which `carqna_dapr.py` does return on failures) is discarded, so users/devs lose the real error message. |
| D4 | `app/page.tsx:36` | `runtimeUrl={process.env.NEXT_PUBLIC_API_BASE}` — if that env var is unset, this silently passes `undefined` into `CopilotKit` rather than failing fast or showing a config error. |
| D5 | `ChatInterface.tsx` | No handling for the case where the user sends a second message before the first `Thinking...` cycle completes — `isLoading` disables the input, which is fine, but there's no queueing/cancel affordance, and no scroll-to-bottom on new content in the `overflow-y-auto` container. |

### E. UX

| # | Where | Issue |
|---|-------|-------|
| E1 | `EventTraceViewer.tsx` | The assistant's actual answer is rendered as one card inside what reads as a developer diagnostics panel (`Response` / `Tools Used` / `Agent Reasoning` / `Full Trace (raw JSON)` all stacked together), rather than as a normal chat bubble. Tool-call JSON and reasoning trace are shown to *every* end user by default (`<details>` open state aside, the raw JSON dump in particular has no place in a production chat UI). |
| E2 | `ChatInterface.tsx` | No true token-level streaming of the answer — `final_response` only appears once, as a single event, after the agent finishes; the `Thinking...` bubble is the only feedback during generation, so despite using SSE the perceived UX is "spinner, then everything pops in at once," not a live-typing response. |
| E3 | `ChatInterface.tsx:56-62` | User bubbles use array index as React `key`, which is fine only because the list is append-only and never reordered/filtered — worth switching to a stable id if messages ever gain edit/delete/regenerate affordances. |

### F. Code quality

| # | Where | Issue |
|---|-------|-------|
| F1 | `useCarqnaChat.ts:25`, `:26` | `useState<any[]>([])` — loses type safety for exactly the `GraphEvent[]` type already defined in `types/carqna.ts`. |
| F2 | Multiple | Comment style throughout reads like running commentary from an assistant/vibe-coding session rather than documentation, e.g. `carqnaService.ts` — none currently, but `useCarqnaChat.ts:46` `"This line is doing ALL the heavy lifting!"`, `:57` `"FIX: Safely store..."`, `:78` `"Proactively exporting this to make UI rendering effortless!"`; `ChatInterface.tsx:41` has a duplicated/leftover comment (`{/* Main Content */}` immediately followed by `{/* Main Content Viewport */}`); `carqna_dapr.py` mirrors this same style (`"How your code looked when it outputted the raw JSON strings."`, `"Fixes the '*' wildcard crash with credentials"`, `"satisfies your TypeScript fetch"`). None of these describe non-obvious WHY — they're narration and should be removed as part of any file they're touched in. |
| F3 | Repo-wide | Zero test files/config beyond the default `eslint-config-next` — no component or hook tests for `useCarqnaChat`, `carqnaService`'s SSE buffering (which has real edge-case logic worth covering — partial-line buffering across chunk boundaries), or `EventTraceViewer`'s event bucketing. |
| F4 | `app/page.tsx`, `next.config.ts` | `next: 16.2.10` / `react: 19.2.4` — versions ahead of anything in general documentation as of early 2026. The repo's own `AGENTS.md` flags this explicitly ("This is NOT the Next.js you know... Read the relevant guide in `node_modules/next/dist/docs/`"). Worth a dedicated pass to confirm current patterns (e.g. `app/page.tsx` "use client" + top-level `useEffect` data fetching) still match this Next version's recommended approach before/while doing the rework below, rather than assuming pre-16 idioms. |

## The one real decision

Everything under **A** stems from one unresolved fork: **is this app supposed to be a CopilotKit
app, or a custom chat UI that merely uses CopilotKit's CSS/provider by accident?** Right now it's
neither — cleanly. Two ways to resolve it:

### Option 1 (recommended): Go all-in on CopilotKit's LangGraph integration ("CoAgents")

Replace the hand-rolled SSE client, event schema, and trace viewer with CopilotKit's own LangGraph
integration:

- **Backend**: with Dapr dropped (see Decisions below), expose `src/agent/graph.py:graph` to
  CopilotKit via the `copilotkit` Python package's `copilotkit.langgraph` helpers
  (`copilotkit_customize_config`, `CopilotKitRemoteEndpoint`, `LangGraphAgent`) in a new
  `src/agent/copilotkit_server.py` module on `carqna_dev` — replacing (renaming from)
  `carqna_dapr.py` and its hand-rolled aiohttp routes. See the "Backend integration path" decision
  below for why this was chosen over the LangGraph Platform route.
- **Frontend**:
  - Add `app/api/copilotkit/route.ts` running `CopilotRuntime` with a `LangGraphAgent` (or
    `LangGraphHttpAgent`) pointed at the above.
  - Point `<CopilotKit runtimeUrl="/api/copilotkit" agent="agent">` at that local route instead of
    the raw Dapr URL.
  - Replace `ChatInterface`'s hand-built message list + input with `CopilotChat` (or
    `CopilotSidebar`/`CopilotPopup` if a docked/floating layout is preferred) from
    `@copilotkit/react-ui` — this alone deletes the manual message-array state, the send form, and
    the "Thinking..." bubble, and gets real token-level streaming markdown for free.
  - Replace `EventTraceViewer`'s manual event bucketing with `useCoAgentStateRender` (or the
    built-in tool-call rendering CopilotChat already does) to show `car_price_expert`/
    `insurance_expert` subagent activity and tool calls as first-class UI, not a raw JSON dump.
  - Delete `useCarqnaChat.ts`, `carqnaService.ts`'s SSE plumbing, and `types/carqna.ts`'s
    `GraphEvent`/`ChatResponse` types once nothing depends on them.
- **Payoff**: deletes ~250 lines of hand-rolled state/streaming/parsing code, gets streaming,
  message history, error/retry, and tool-call UI that's already accessibility- and edge-case-tested
  by CopilotKit, and actually justifies the two `@copilotkit/*` dependencies already in
  `package.json`.
- **Cost**: requires backend changes in `carqna-agent` (not just the UI repo) — retiring
  `carqna_dapr.py`'s custom REST/SSE surface in favor of the LangGraph Platform API / CopilotKit
  remote endpoint, and removing the `carqna-dapr` service from `docker-compose.yml`.

### Option 2: Drop CopilotKit, keep and clean up the custom chat

If there's a concrete reason CopilotKit's LangGraph integration doesn't fit (e.g. the Dapr
service-mesh routing is a hard requirement CopilotKit's runtime can't accommodate), then:

- Remove `@copilotkit/react-core`, `@copilotkit/react-ui`, `@copilotkit/sdk-js` from
  `package.json`, and the `<CopilotKit>` provider + stylesheet import from `app/page.tsx` — stop
  paying for a dependency that isn't providing anything.
- Fix items B–F above in place: single source of truth for `thread_id`, `AbortController` +
  unmount cleanup on the stream, real error bodies surfaced, dead state/exports removed, split
  "assistant answer" out of the diagnostics panel into a normal chat bubble with the tool/reasoning
  trace as an optional expandable aside, add tests around the SSE buffering logic in
  `carqnaService.ts`.
- **Payoff**: smaller dependency footprint, no protocol confusion.
- **Cost**: forfeits CopilotKit's built-in streaming/message-list/generative-UI machinery — every
  future chat UX improvement (regenerate, stop generation, human-in-the-loop approval UI,
  multi-agent state visualization) has to be hand-built.

**Decision: Option 1, confirmed 2026-07-24.** The backend is already a multi-subagent LangGraph deep
agent with tool calls and per-subagent reasoning — exactly the shape CoAgents is designed to
visualize — and two CopilotKit packages are already a dependency. The current code is effectively a
worse, partial reimplementation of what adopting CopilotKit properly buys for free.

Per the project owner: the earlier CopilotKit integration attempt (the discovery-protocol shimming
in `carqna_dapr.py`, the unused `<CopilotKit>` provider) was abandoned because it couldn't be made
to work, not because of a deliberate choice to build custom. The intent is, and remains, to use
CopilotKit rather than hand-build this functionality. That raises the stakes on the **spike** step
below: before re-doing the UI work, diagnose *why* the first attempt failed (version mismatch
between `@copilotkit/react-core@1.63.1` and whatever `copilotkit` Python SDK version/protocol was
targeted, wrong integration pattern — REST/SSE instead of the actual AG-UI/CoAgents wire protocol —
or something else) so the second attempt doesn't repeat it.

## Proposed phased plan (assuming Option 1 is confirmed)

**Status: Steps 2-7 all done and validated 2026-08-01.** Old hand-rolled plumbing
(`carqna_dapr.py`, `ChatInterface`/`useCarqnaChat`/`carqnaService`, the CopilotKit discovery-protocol
shimming) is deleted on both repos; `app/page.tsx` runs the real `CopilotChat` + persistent tool-call
panels; the project owner confirmed live chat, tool calls, and multi-turn context all work at the
actual `/` route. Docker infra cleanup went further than this plan originally specified — see the
"Dapr sidecar routing" decision below, updated to match. Explicitly still open: no frontend test
framework exists (deferred by choice), and the stale template tests that used to live in
`tests/unit_tests`/`tests/integration_tests` were deleted rather than fixed (CI's pytest step now
tolerates zero collected tests until real ones are written).

1. **Spike**: validate the actual chosen backend integration path — the `copilotkit` Python SDK
   mounted directly on `carqna_dev` (see "Backend integration path" below), not LangGraph Platform —
   before touching any real UI code. Since a prior attempt at CopilotKit integration failed, treat
   this as a real diagnostic pass, not a formality. **Status: SPIKE VALIDATED 2026-08-01. Full
   end-to-end run completed manually by the project owner — real backend, real frontend, real chat
   turns, multi-turn context, tool-call rendering all confirmed working. Prior integration failure
   is resolved: root cause was the stale API assumption (see below), not a deeper protocol issue.
   Ready to move to the next phase of the plan.**

   **Done so far** (both repos on `feature/copilot-ui-remediation`, work done in `~/langsmith.venv`):
   - `carqna-agent`: `copilotkit`/`uvicorn` added to `pyproject.toml`, editable-installed into the
     venv. New `src/agent/copilotkit_server.py` written, wired with the same `AsyncSqliteSaver`
     checkpointer as `carqna_dapr.py` (sqlite3 now, Postgres later). Import-verified only — confirmed
     it loads cleanly and (once `opensearch` was up) that `create_graph()` really does return live
     MCP tools (`ListIndexTool`, `SearchIndexTool`). Never actually started as a running server.
   - `carqna-copilot-ui`: added `@copilotkit/runtime@1.63.1` and `@ag-ui/client@0.0.57` as explicit
     deps (both were missing). New `app/api/copilotkit/route.ts` and throwaway `app/spike/page.tsx`.
     `tsc --noEmit` and `eslint` both pass. Never actually run via `npm run dev` or opened in a
     browser.
   - `opensearch` container confirmed running and healthy (`docker ps`) as of 2026-07-25 — check
     it's still up before resuming; if not, `docker compose -f infrastructure/docker/docker-compose.yml
     up -d opensearch`.

   **Validation run — completed 2026-08-01** (all steps run manually by the project owner, backend
   and frontend in separate terminals, so behavior could be watched closely rather than automated):
   1. ✅ `opensearch` confirmed up (`docker ps`).
   2. ✅ Backend started (`~/langsmith.venv/bin/python -m agent.copilotkit_server`, port 8000) —
      startup log showed MCP tools loaded.
   3. ✅ Backend smoke-tested alone via `curl -N -X POST http://localhost:8000/ ...` before touching
      the frontend — raw AG-UI SSE stream came back correctly (`RUN_STARTED` →
      `TEXT_MESSAGE_START/CONTENT/END` → `RUN_FINISHED`).
   4. ✅ Frontend started (`npm run dev` in `carqna-copilot-ui`), `/spike` loaded.
   5. ✅ No discovery-handshake issue, as predicted — first signal was the first message send.
   6. ✅ Sent one message; reply streamed token-by-token via `text/event-stream`, not a pop-in block.
   7. ✅ Asked a car-price question; tool-call trace rendered live in `CopilotChat` while the
      `car_price_expert` subagent ran the OpenSearch lookup. **Follow-up finding**: the trace UI
      disappears once the final assistant text lands — confirmed (by reading
      `@copilotkit/react-core/dist/index.mjs`) this is *intentional* default behavior, not a bug: the
      trace is a synthetic placeholder message injected only while `agent.isRunning` is true and no
      final assistant message exists yet for the run (`shouldRenderPlaceholder && !hasAssistantForCurrentRun`);
      it's dropped the instant a real `TEXT_MESSAGE_*` assistant message exists. `RenderMessage`
      itself (`@copilotkit/react-ui`) only switches on `role: "user" | "assistant"` — there's no
      persistent lane for tool-call history in the stock UI. If a persistent trace/history view is
      wanted later, it needs a custom `useCopilotAction({ render })` that doesn't collapse on
      `status: "complete"` — a UI decision for a later phase, not a spike blocker.
   8. ✅ Sent a second message in the same thread; prior context was retained — confirms the
      `AsyncSqliteSaver` checkpointer wiring works end-to-end.
   9. Confirmed-working version set: `@copilotkit/react-core`/`react-ui`/`runtime` `1.63.1` ↔
      `@ag-ui/client` `0.0.57` ↔ `copilotkit` (Python) `0.1.94` ↔ `ag-ui-langgraph` (Python) `0.0.42`.

   The API actually shipped in `copilotkit==0.1.94` (installed 2026-07-25) differs from what this
   step originally described — `copilotkit.langgraph.LangGraphAgent` / `CopilotKitRemoteEndpoint` /
   `add_fastapi_endpoint` no longer exist. CopilotKit has moved to the **AG-UI protocol** as its
   agent-integration layer, via the separate `ag-ui-langgraph` package. The corrected shape:
   - **Backend** (`src/agent/copilotkit_server.py`, written): add `copilotkit` (which pulls in
     `ag-ui-langgraph`, `fastapi`) to `pyproject.toml`. Wrap `graph.py`'s `create_graph()` result in
     `copilotkit.LangGraphAGUIAgent` (name="carqna_agent"), and mount it with
     `ag_ui_langgraph.add_langgraph_fastapi_endpoint(app, agent, path="/")` on a tiny FastAPI app —
     this is a single `POST /` accepting `RunAgentInput` and streaming raw AG-UI SSE events, no
     GraphQL/remote-endpoint layer involved. `create_graph()` is awaited inside a `lifespan` handler
     (it's async; MCP init happens there) rather than at import time. Multi-turn state uses the same
     `AsyncSqliteSaver` (sqlite3 now, Postgres planned later) as `carqna_dapr.py`, opened via
     `async with AsyncSqliteSaver.from_conn_string(db_path)` spanning the lifespan's `yield` —
     simpler than `carqna_dapr.py`'s manual `__aenter__`/`__aexit__` + module-global juggling, which
     was only needed there because aiohttp splits startup/cleanup into separate callback functions.
     Run directly on the host (no Docker, no Dapr), `python -m agent.copilotkit_server`, port 8000.
   - **Frontend** (`app/api/copilotkit/route.ts` + `app/spike/page.tsx`, written): needs
     `@copilotkit/runtime` and `@ag-ui/client` added as explicit dependencies (neither was installed
     despite `react-core`/`react-ui` being present) — pin both to `1.63.1`/`0.0.57` to match. Build a
     `CopilotRuntime({ agents: { carqna_agent: new HttpAgent({ url: "http://localhost:8000/" }) } })`
     — `HttpAgent` from `@ag-ui/client` is the generic AG-UI HTTP client and talks directly to the
     backend's `add_langgraph_fastapi_endpoint` route. (`@copilotkit/runtime/langgraph`'s
     `LangGraphHttpAgent` is documented as the friendlier name for this exact class, but its bundled
     `.d.mts` re-export drops the inherited constructor type — TS2740/TS2554 — so import `HttpAgent`
     directly instead; functionally identical.) Serve via `copilotRuntimeNextJSAppRouterEndpoint` with
     `serviceAdapter: new EmptyAdapter()` (no direct-LLM adapter needed when agents handle everything).
     `app/spike/page.tsx` is the throwaway validation route: `<CopilotKit runtimeUrl="/api/copilotkit">`
     wrapping `<CopilotChatConfigurationProvider agentId="carqna_agent">` (imported from
     `@copilotkit/react-core/v2` — **not** re-exported from the package root) wrapping a bare
     `<CopilotChat />`. The `agentId` wiring is required: the frontend defaults to agent key
     `"default"` (`DEFAULT_AGENT_ID`) if not told otherwise, which won't match the `carqna_agent` key
     registered on the runtime.
   - **Validate** (not yet run — needs `opensearch` up, see step below): discovery handshake succeeds;
     one message streams token-by-token (not a single pop-in); one subagent/tool call (e.g. a
     car-price lookup) surfaces `TOOL_CALL_*` events via CopilotChat's built-in tool rendering; confirm
     the working `@copilotkit/react-core`/`react-ui`/`runtime` (`1.63.1`) ↔ `@ag-ui/client`
     (`0.0.57`) ↔ `copilotkit` Python SDK (`0.1.94`) ↔ `ag-ui-langgraph` Python (`0.0.42`) version
     set — a version/protocol mismatch is the leading suspect for why the earlier attempt failed, so
     record it once confirmed working end-to-end. If any check fails, that's the diagnostic signal
     this step exists to catch, before steps 4-7 sink real UI work into an approach that doesn't
     actually connect.
2. **Backend cutover**: rename `carqna_dapr.py` to `copilotkit_server.py`, replacing its hand-rolled
   endpoints and CopilotKit discovery shimming with the `copilotkit` Python SDK mounted directly on
   `carqna_dev` (decided — see "Backend integration path" below), and remove the `carqna-dapr`
   service + Dapr env vars from `docker-compose.yml`/`.env.example`. Also **delete `cors_middleware`
   and all `Access-Control-Allow-*` header handling** (`carqna_dapr.py:589-620` and the headers on
   `copilot_info`/streaming responses) rather than port it — under the new architecture the browser
   only ever talks to `app/api/copilotkit/route.ts` (same-origin as the page), which then calls
   `copilotkit_server.py` server-to-server; no browser-origin request ever reaches this service
   directly, so there's nothing for CORS to guard.
3. **Replace transport**: swap `runtimeUrl` to the new route, remove the direct-to-Dapr `fetch`
   calls in `carqnaService.ts`.
4. **Replace UI**: swap `ChatInterface`'s hand-built list/input for `CopilotChat`
   (or `CopilotSidebar`/`CopilotPopup`, pick based on desired layout), wire tool/subagent-call
   visibility.

   **Correction (2026-08-01)**: this step originally said to use `useCoAgentStateRender` for
   tool/subagent visibility — that hook is for LangGraph *state* snapshots, a different mechanism, and
   doesn't match the AG-UI tool-call events this app actually produces. The validated mechanism is
   `useRenderTool` (from `@copilotkit/react-core/v2`): it registers a renderer keyed by tool name
   (or `"*"` for all tools) into a shared registry that `CopilotChat`'s existing message-building logic
   (`useLazyToolRenderer` → `useRenderToolCall`) already reads from for every real assistant message in
   `agent.messages` — not just the in-flight run. That's the key property: it's what makes the
   rendered output persist in the transcript, unlike `CopilotChat`'s default transient "running"
   placeholder (shown only while `agent.isRunning` and no final assistant text exists yet for the
   run), which disappears the moment the real answer lands.

   **First piece delivered and validated 2026-08-01** (in `carqna-copilot-ui`, still scoped to
   `/spike` — not yet promoted into `app/page.tsx`/`ChatInterface.tsx`, which is the rest of this
   step): `src/components/ToolCallPanel.tsx` (collapsible per-tool-call panel, styled after the
   existing `EventTraceViewer.tsx` conventions — Tailwind, `<details>/<summary>`, colored left border;
   expanded while running, auto-collapses once on completion, then fully user-toggleable) +
   `src/components/ToolCallRenderers.tsx` (mounts `useRenderTool({name: "*", render: ...})` inside
   `<CopilotKit>`), wired into `app/spike/page.tsx`. Confirmed working end-to-end by the project
   owner: panel renders at the top of the assistant message, expanded while the tool runs, stays
   visible (collapsed) after the final answer, and reopens on click with args/result intact.
5. **Delete dead code**: `useCarqnaChat.ts`, SSE buffering in `carqnaService.ts`, `GraphEvent`/
   `ChatResponse` types, `EventTraceViewer.tsx` (or keep a slimmed version purely for the
   tool-call render function passed to `useCoAgentStateRender`), unused `getHealth`/`ChatSession`,
   placeholder SVGs, default layout metadata.
6. **Fix session/thread lifecycle**: single `thread_id` source (CopilotKit manages this itself once
   adopted — the manual `initConversation` dance likely goes away entirely).
7. **Polish**: rebrand `layout.tsx` metadata, add basic tests around anything still hand-written
   (the `api/copilotkit` route config, any custom tool-call renderers).

## Decisions

### Dapr sidecar routing: drop it for now

Resolved 2026-07-24. Dapr was originally added on a technology leader's recommendation, but today
it's just an HTTP hop in front of a single Python process — nothing in the code uses Dapr's SDK or
building blocks (service invocation retries/mTLS, pub/sub, actors, state store — `DAPR_STATE_STORE`
is defined in `.env.example` but never read). It's pure proxy overhead: an extra container, an extra
network hop, and an extra thing to keep healthy in `docker-compose.yml`, for zero functional payoff.

It would start earning its keep if `carqna-agent` splits into multiple independently-deployable
services that need to talk to each other (e.g. a separate pricing service, a background
notifications/workflow service, polyglot services owned by different teams) — that's the
microservice-mesh scenario Dapr's building blocks target. Near-term roadmap items (more subagents,
more tools/MCP servers, UI polish) all live inside the single LangGraph process and don't need it.

Since nothing in the code depends on Dapr's SDK today, removing it now doesn't foreclose adding it
back later if/when the system actually goes multi-service — the switching cost in either direction
is low.

**Action**: drop the `carqna-dapr` (daprd) service and the Dapr-specific env vars
(`DAPR_SERVICE_PORT`, `DAPR_RUNTIME_PORT`, `DAPR_SERVICE_NAME`, `DAPR_STATE_STORE`) from
`docker-compose.yml`/`.env.example`. This also resolves the Option 1 backend question in A4/A3
below in favor of the simpler path: expose `carqna-agent`'s LangGraph graph directly (via
`langgraph dev`/`langgraph up`'s LangGraph Platform API, or the `copilotkit` Python SDK mounted
directly on `carqna_dev`) rather than through a Dapr-fronted custom REST/SSE surface. `carqna_dev`
can be reached by the frontend directly (or via nginx, if a reverse proxy is still wanted) instead
of via `carqna-dapr` → `-app-id=carqna-service`.

**Superseded 2026-08-01 — what actually shipped went further than the plan below.** The plan as
originally written assumed `carqna-agent` would stay containerized (`carqna-dev` service kept, just
pointed at `copilotkit_server.py` instead of `carqna_dapr.py`). In practice the project owner dropped
Docker for the app layer entirely, not just the Dapr sidecar: `docker-compose.yml` now runs only
`opensearch`; the agent (`python -m agent.copilotkit_server`) and `carqna-copilot-ui` (`npm run dev`)
both run directly on the host (see `readme-developmment.md`'s "Running CarQnA" section). Rationale:
every validation this whole engagement was done host-based anyway, so containerizing those two
services added a build/rebuild loop for no actual benefit at this stage. `carqna-dev`/
`carqna-copilot-ui` Dockerfiles, `dapr-config.yaml`, and `nginx.conf` (a reverse proxy that encoded
the same discovery-protocol/Dapr-invoke-URL guesswork the spike proved wrong) are all deleted. The
original narrower plan is kept below for history but is no longer what's implemented.

- ~~**Delete** the `carqna-dapr` service block entirely~~ — done, and `carqna-dev` was deleted too,
  not kept.
- ~~**`carqna-dev` service**: keep the service, but change its `command`~~ — not kept; runs on host.
- ~~**`carqna-copilot-ui` service**: change `NEXT_PUBLIC_API_BASE`...~~ — not applicable;
  `carqnaService.ts` (which defined that env var) was deleted, and the frontend runs on host via
  `npm run dev`, talking to `/api/copilotkit` (same-origin) which calls `carqna-agent` directly via
  `CARQNA_AGENT_URL`.
- **`opensearch` service**: unaffected, as originally planned.

### Why the earlier CopilotKit attempt was abandoned

Resolved 2026-07-24: it wasn't a deliberate "install now, integrate later" step — the team tried to
make CopilotKit work and couldn't, then fell back to the hand-rolled REST/SSE approach in
`carqna_dapr.py`/`useCarqnaChat.ts`. Given that, none of the existing discovery-protocol shimming in
`carqna_dapr.py` (A3) should be treated as salvageable groundwork — it was a guess at the protocol
that didn't pan out, not a partial implementation to build on. Plan on replacing it outright with
the documented `copilotkit` SDK / LangGraph Platform integration path (Option 1 above) rather than
patching it.

### Chat layout

Resolved 2026-07-24: **`CopilotChat`, embedded as the full-page main content**, not
`CopilotSidebar` or `CopilotPopup`. CarQnA's entire product surface is the chat — there's no
separate primary app content for a sidebar to dock next to or a popup to float over, which is the
scenario those two components target (bolting a copilot onto an existing app). The current
`ChatInterface` is already a dedicated full-`h-screen` layout, so `CopilotChat` as the page's main
content is the closer fit and the smaller migration.

### Backend integration path: `copilotkit` SDK on `carqna_dev`, not LangGraph Platform

Resolved 2026-07-24. The two candidates for exposing `graph.py:graph` to CopilotKit:

- **LangGraph Platform** (`langgraph build`/`langgraph dockerfile` + `langgraph up`) — the repo is
  already wired for this (`langgraph.json` declares `graphs.agent`), so it's close to zero new code.
  But the resulting image runs on the Elastic License 2.0 `langchain/langgraph-api` base, and
  production self-hosted use requires `LANGGRAPH_CLOUD_LICENSE_KEY` — which per LangChain's own forum
  is tied to a LangSmith **Enterprise** plan, not a free/Plus account or API key. (One GitHub issue
  thread claims the license gate is actually scoped to the self-hosted *observability* feature
  specifically and that a plain API key sufficed in lighter testing — but that's an unofficial,
  reverse-engineered finding, not documented behavior worth architecting around.)
- **`copilotkit` Python SDK mounted on `carqna_dev`** — keeps the current lightweight
  `python:3.14-slim` + pip-tools Dockerfile pattern; adds `copilotkit` to `pyproject.toml`.
  `carqna_dapr.py` is renamed to `src/agent/copilotkit_server.py` and rebuilt to wrap `graph.py`'s
  `create_graph()` with `CopilotKitRemoteEndpoint`/`LangGraphAgent`, replacing the old module's
  command in `docker-compose.yml`. No license key, MIT-licensed footing throughout.

**Decision**: no LangSmith Enterprise plan is in scope for this project, so the license requirement
rules out the Platform path for production. Going with the **`copilotkit` SDK mounted on
`carqna_dev`**. This does mean writing real (if small) integration code rather than getting it for
free — the spike step (Step 1) needs to validate this wrapper actually speaks the protocol
`@copilotkit/react-core`/`react-ui` expect, since a version/protocol mismatch is the leading
suspect for why the earlier attempt failed.

All open questions are now resolved — this plan is ready to move from review into implementation
planning whenever you want to proceed.
