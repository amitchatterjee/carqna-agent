# carqna-copilot-ui: Code Audit & Remediation Plan

Status: **draft for review** — no code has been changed. This is an analysis of
`~/git/carqna-copilot-ui` as it stands today (last commits: `28d46a0 Fixed the final response -
more work needed`, `e75fcc9 Fixed UI - work in progress`), plus a proposed plan to fix it.

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
  CopilotKit either (a) directly as a LangGraph Platform deployment (`langgraph dev`/`langgraph up`,
  already configured via `langgraph.json` — CopilotKit's `LangGraphAgent` can point at it as-is), or
  (b) via the `copilotkit` Python package's `copilotkit.langgraph` helpers
  (`copilotkit_customize_config`, `CopilotKitRemoteEndpoint`) mounted directly on `carqna_dev`
  (replacing the current hand-rolled aiohttp routes in `carqna_dapr.py`).
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

1. **Spike**: stand up `app/api/copilotkit/route.ts` + `CopilotRuntime` against `langgraph dev`
   running locally (bypassing Dapr entirely) to validate the LangGraph Platform route works with
   this repo's `graph.py` (subagents, MCP tools, filesystem backend) before touching any UI code.
   Since a prior attempt at this failed, treat this step as a real diagnostic pass, not a
   formality — confirm package versions are compatible (`@copilotkit/react-core`/`react-ui`
   `1.63.1` against whatever `copilotkit` Python SDK / LangGraph Platform version gets used), and
   get a minimal end-to-end message round-trip working before layering subagents/tool-call
   rendering on top.
2. **Backend cutover**: pick LangGraph Platform API directly vs. the `copilotkit` Python SDK mounted
   on `carqna_dev`, retire `carqna_dapr.py`'s hand-rolled endpoints and CopilotKit discovery
   shimming, and remove the `carqna-dapr` service + Dapr env vars from `docker-compose.yml`/
   `.env.example`.
3. **Replace transport**: swap `runtimeUrl` to the new route, remove the direct-to-Dapr `fetch`
   calls in `carqnaService.ts`.
4. **Replace UI**: swap `ChatInterface`'s hand-built list/input for `CopilotChat`
   (or `CopilotSidebar`/`CopilotPopup`, pick based on desired layout), wire `useCoAgentStateRender`
   for subagent/tool visibility.
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

**Note this does *not* remove `carqna-agent` from Docker** — only the `daprd` sidecar container
goes away. `carqna-dev` (built from `infrastructure/docker/carqna-dev/Dockerfile`, i.e. the actual
agent code) keeps running as its own container in `docker-compose.yml`. Concretely, in
`infrastructure/docker/docker-compose.yml`:

- **Delete** the `carqna-dapr` service block entirely (the `daprd` image, its
  `-app-id=carqna-service`/`-app-channel-address=carqna-dev`/`-app-port=5001` args, and the
  `dapr-config.yaml` volume mount).
- **`carqna-dev` service**: keep the service, but change its `command` from
  `python -m src.agent.carqna_dapr` (the custom aiohttp service being retired per A3) to whatever
  directly exposes the LangGraph graph — `langgraph up`'s LangGraph Platform server, or a small app
  running the `copilotkit` Python SDK mounted on this same container. Port mapping (`5001:5001`
  today) should be updated to match whatever port that process listens on.
- **`carqna-copilot-ui` service**: change `NEXT_PUBLIC_API_BASE` from
  `http://localhost:3500/v1.0/invoke/carqna-service/method/agent` (the Dapr sidecar URL) to point
  directly at `carqna-dev`'s host/port, and change `depends_on: carqna-dapr` to
  `depends_on: carqna-dev` (keeping the `service_healthy` condition, once `carqna-dev`'s healthcheck
  is updated to hit the new process's health endpoint instead of the retired `/health` route in
  `carqna_dapr.py`).
- **`opensearch` service**: unaffected by this change.
- `infrastructure/docker/dapr-config.yaml` and the `docker/carqna-copilot-ui/Dockerfile`'s
  relationship to the rest of the compose file are otherwise unaffected; `dapr-config.yaml` itself
  becomes dead and can be deleted once the `carqna-dapr` service is gone.

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

All open questions are now resolved — this plan is ready to move from review into implementation
planning whenever you want to proceed.
