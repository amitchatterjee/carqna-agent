# OAuth2/OIDC auth (Okta/Auth0) across carqna-agent and carqna-copilot-ui

Status: **DONE** — approved 2026-08-09, implemented and verified 2026-08-10. Backend and frontend
code both live on `feature/okta` in both repos. End-to-end verification passed: login redirects to
Auth0, a real chat message round-trips through the authenticated `POST /` route, and the Postgres
`checkpoints.thread_id` column confirms the composite-key design works
(`auth0|6a78d5504c69cc8f16465b81:61d7d9f3-68b3-4bcf-aeff-f15b6e2a79cb` — verified `sub` claim +
client-supplied thread id, exactly as designed). One dashboard prerequisite not called out explicitly
enough in the original plan: the Auth0 Application must be explicitly authorized against the API via
a **client grant** (Auth0 Dashboard → APIs → the API → "Machine to Machine Applications" tab — despite
the name, this is where any application type, including this Regular Web App, gets authorized against
an audience), or the callback fails with `Client "..." is not authorized to access resource server
"..."`. This file is renamed to this plan's final `-DONE` form per this project's naming convention.

**Progress on prerequisites — all done**: Auth0 API created, Identifier/audience is
`https://carqna-agent/api`. Application's Allowed Callback/Logout URLs set to
`http://localhost:3000/auth/callback` / `http://localhost:3000/auth/logout`. Both repos' env files
populated — `carqna-agent/.env`: `AUTH0_DOMAIN`, `AUTH0_AUDIENCE`; `carqna-copilot-ui/.env.local`:
`AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_SECRET`,
`APP_BASE_URL=http://localhost:3000`, `AUTH0_AUDIENCE` (same value as the agent's). All prerequisites
from the "Prerequisites" section below are now satisfied — remaining work is the actual code
(Backend/Frontend sections), not yet started, still gated on explicit go-ahead.

**Two more dashboard gotchas found post-implementation (2026-08-11), on top of the client-grant one
above** — both now documented in `readme-developmment.md`'s "One-time setup of Auth0/Okta" section:
- **A database connection must exist and be associated with the Application**, or `carqna` has no
  connection to actually authenticate users against. Auth0 dashboard → Authentication → Database →
  Create DB Connection (name `Username-Password-Authentication`, defaults otherwise) → open its
  Applications tab → select `carqna`.
- **Allowed Logout URLs needs the bare app base URL** (`http://localhost:3000`), not
  `/auth/callback` or `/auth/logout` — `@auth0/nextjs-auth0`'s `/auth/logout` route defaults
  `returnTo` to `appBaseUrl` itself when no explicit `returnTo` query param is passed
  (`auth-client.js`: `const returnTo = req.nextUrl.searchParams.get("returnTo") || appBaseUrl;`).
  Registering only a path-suffixed URL there produces Auth0's generic hosted "Oops, something went
  wrong" error page on logout, with no useful detail — the mismatch is the whole story.

## Context

Today, `copilotkit_server.py`'s AG-UI endpoint is completely open — `ag_ui_langgraph` pulls
`threadId` straight out of the request body and uses it directly as the LangGraph checkpoint key
(`ag_ui_langgraph/agent.py:183-204`), with no concept of "who is asking." Anyone who knows (or
guesses) a `threadId` can read or continue that conversation.

This plan adds real login (via Okta/Auth0) on the frontend, has the backend verify the resulting
token on every request, and changes conversation storage so a user can only ever reach their own
threads — structurally, not just by a permission check. It also lays groundwork (not the UI) for
letting a user have multiple named sessions later, the way Claude Code does.

**Decisions made while researching** (confirmed with the project owner before writing this):

- **`auth0-server-python` (already added to `carqna-agent`) is Python-only** — it can't run in
  `carqna-copilot-ui` at all. The frontend's actual login redirect uses a separate package,
  **`@auth0/nextjs-auth0`** (Auth0's real Next.js SDK).
- **`auth0-server-python`'s `ServerClient` turns out not to be the right tool for the backend's job
  either.** Its JWT verification (`_verify_and_decode_jwt`) is a private method used only internally
  during *its own* login/logout flow — not a general "verify this incoming Bearer token" utility.
  Auth0 treats "log a user in" (Application/Client role) and "verify an incoming API token"
  (Resource Server role) as different jobs with different tooling. The backend only needs the
  latter, so it'll use direct JWT/JWKS verification (`pyjwt` + `PyJWKClient` — `pyjwt` is already a
  transitive dependency via `auth0-server-python`). **Open question, decide later**: since
  `ServerClient` ends up unused by this plan, decide whether to drop `auth0-server-python` from
  `pyproject.toml` or keep it around for something else down the line (e.g. calling Auth0's
  Management API from the backend) — not a decision this plan needs to make now.
- **Needs a prerequisite step in the Auth0/Okta dashboard, not just code**: token verification
  requires a registered **API** (with an audience/identifier), separate from the Application already
  created for `AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET`. The frontend's login must request an access
  token for that specific audience, or the backend has nothing meaningful to verify.
- **Ownership is enforced structurally**, not via a separate ACL table: the actual LangGraph
  checkpoint key becomes `{verified_user_id}:{client_supplied_thread_id}`, built server-side from
  the *verified* token's `sub` claim — never trusted from the client. A user can't construct a
  request that reads another user's thread even if they know its raw ID, because the full composite
  key differs. This also happens to be exactly the shape needed later for "list all of user X's
  sessions" (prefix-scan checkpoints by `{user_id}:`), so it sets up the deferred multi-session
  feature without building it now.

**Assumed, not asked**: no migration path for existing conversations (the `debug-*`/test threads
created so far under the old unprefixed scheme) — this is still pre-launch dev data, fine to orphan.
Flag if that's wrong.

## Prerequisites (project owner does this, not code)

1. In the Auth0/Okta dashboard: create an **API** (Identifier/audience, e.g.
   `https://carqna-agent/api`), separate from the existing Application.
2. Register `carqna-copilot-ui`'s callback URL (e.g. `http://localhost:3000/auth/callback`) as an
   **Allowed Callback URL** on the Application.
3. New env vars needed — note this is **not symmetric** between the two repos, since only the
   frontend actually performs the login/token-exchange flow:
   - `carqna-copilot-ui`: `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `AUTH0_SECRET`,
     `APP_BASE_URL` (this app's own base URL, e.g. `http://localhost:3000` — *not* the agent's),
     and the API audience identifier from step 1 (so login requests an access token scoped to it) —
     plus whatever else `@auth0/nextjs-auth0`'s current setup docs specify (v4 vs older major
     versions of this SDK have different required vars/setup shape, worth checking against the
     currently-published version at implementation time rather than assuming).
   - `carqna-agent/.env`: **only** the API audience identifier from step 1, so the backend knows
     what audience to require when verifying tokens (`AUTH0_DOMAIN` is already there for the JWKS
     URL). The agent does **not** need `AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET`/`AUTH0_SECRET`/
     `APP_BASE_URL` at all — those are only needed by whichever side performs the interactive login
     (the frontend), not by a pure token verifier. (Confirmed 2026-08-09: these were originally
     present in `carqna-agent/.env` from following Okta's generic setup instructions before this
     plan's split-responsibility design was settled — now commented out there and populated in
     `carqna-copilot-ui/.env.local` instead, where they're actually used.)

## Backend (`carqna-agent`)

- **New module, e.g. `src/agent/auth.py`**: JWT verification using `pyjwt`'s `PyJWKClient`
  (fetches/caches Auth0's JWKS from `https://{AUTH0_DOMAIN}/.well-known/jwks.json`), validating
  signature, issuer, audience (the new API identifier), and expiry. Exposes something like a FastAPI
  dependency that extracts the `Authorization: Bearer <token>` header, verifies it, and returns the
  verified `user_id` (the token's `sub` claim) — raising `401` on anything invalid/missing.

- **`src/agent/copilotkit_server.py`**: `add_langgraph_fastapi_endpoint` (the current one-liner
  mounting the AG-UI route) has no hook for auth or for rewriting `threadId`, so it needs to be
  replaced with an equivalent hand-rolled route — its actual implementation
  (`ag_ui_langgraph/endpoint.py`) is short (~30 lines: parse `RunAgentInput`, `agent.clone()`,
  stream `request_agent.run(input_data)` through `StreamingResponse`), so this is a small, faithful
  copy with two insertions: (1) the auth dependency from `auth.py`, (2) rewriting
  `input_data.threadId` to `f"{user_id}:{input_data.threadId}"` before calling `.run()`. Keep the
  `GET {path}/health` route the original helper also registers — unauthenticated, for infra health
  checks.

- **`carqna_cli.py`**: unaffected. It never goes through the HTTP/AG-UI route this plan changes, so
  it keeps using its own plain `thread_id = "carqna-local-session"` untouched.

- **`pyproject.toml`**: add `pyjwt` explicitly (currently only a transitive dependency) —
  everything else needed (`PyJWKClient` is part of `pyjwt` itself) is already present.

## Frontend (`carqna-copilot-ui`)

- **`package.json`**: add `@auth0/nextjs-auth0`.

- **Auth wiring**: `@auth0/nextjs-auth0`'s current setup (middleware + auth route handlers +
  session-reading helpers) — verify the exact current shape against the installed version rather
  than assuming a specific past API, same lesson as everything else in this repo's Next.js
  integration so far (this project's `AGENTS.md` already warns this Next.js version drifts from
  training-data assumptions, and Auth0's SDK has had major setup changes across versions too).

- **Protect the main page** (`app/page.tsx`): require a session, redirecting to login if absent —
  exact mechanism (middleware-based vs. page-level check) to follow whatever the SDK's current
  setup guide recommends.

- **`app/api/copilotkit/route.ts`**: extend the existing custom `fetch` wrapper (`tracedFetch`,
  already added for OTel trace propagation) to also read the current user's access token
  (server-side, via the SDK's session helper) and attach `Authorization: Bearer <token>` to the
  outbound call to `copilotkit_server.py` — same pattern already established for injecting the
  `traceparent` header, just one more header alongside it.

## Provider portability — separate reference document

Per the project owner's question about Okta/Auth0 lock-in: the **backend has none** (plain JWT/JWKS
verification per the OIDC spec, works against any compliant provider by changing env vars). The
**frontend does** — `@auth0/nextjs-auth0` is Auth0-specific, and swapping providers later means
replacing that package and its login/session/callback wiring, not just config.

Rather than fold "how to swap providers" into this implementation plan (this plan is proceeding with
Okta/Auth0 as decided), it gets its own standalone reference document (not yet created — will be
added as `.plans/005-2026-08-09-oauth2-provider-portability-guide.md` when asked for):
- Exactly what's Auth0-specific today (the frontend package/session format) vs. what already isn't
  (the backend's verification logic, which needs zero code changes for a provider swap — only
  updated issuer/JWKS/audience env vars).
- The recommended target for a swap: **Auth.js (NextAuth.js)**, chosen specifically because it's
  built around pluggable providers (Okta, Auth0, Google, generic OIDC, etc. all plug into the same
  session machinery) — a future swap becomes a provider-config change rather than an SDK rewrite.
- High-level steps for the swap itself (replace the frontend package, reconfigure the provider,
  update `app/api/copilotkit/route.ts`'s token-attachment code, update the backend's issuer/JWKS/
  audience env vars) and the one real caveat: existing sessions get invalidated on cutover (cookie
  format differs between SDKs), so users will need to log in again after a switch.
- Explicitly not a step-by-step implementation (there's nothing to implement — it's a reference for
  a decision not yet made), and not numbered as a dependency of this auth plan; it stands alone.

## Explicitly deferred (not part of this plan)

- Any UI for creating/listing/switching multiple named sessions per user (the Claude-Code-like
  feature) — the composite `{user_id}:{thread_id}` checkpoint key is chosen specifically so this can
  be added later (list threads by prefix) without another storage migration, but no session-list UI
  is being built now.
- Migrating/preserving existing pre-auth conversation threads.
- Any change to `langgraph dev`/LangGraph Studio's own graph exposure (`graph.py`'s module-level
  `graph` object) — Studio manages its own auth/access separately and isn't part of this request
  path.

## Verification

1. Confirm the Auth0 API/audience prerequisite is actually set up (step 1 above) before writing any
   code against it.
2. Frontend: visiting `/` while logged out redirects to Okta; after login, lands back on `/` with an
   active session.
3. Backend: a request to `copilotkit_server.py` with no `Authorization` header gets `401`; with a
   valid token, succeeds.
4. End-to-end: log in as user A, have a conversation, note the `threadId` CopilotKit generated. Open
   a private/incognito window, log in as a *different* user B, and confirm B cannot see/continue A's
   conversation even if the raw `threadId` is somehow known (check the actual Postgres
   `checkpoints.thread_id` column shows the `{user_id}:...` composite form, not the raw client ID).
5. `GET /health` still works without a token (infra check unaffected by the auth change).
