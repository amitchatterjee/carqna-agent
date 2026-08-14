# Centralize JWT auth via ASGI middleware + contextvars

## Context

Before this change, `copilotkit_server.py` verified the caller's identity independently on each of its
three protected routes (`POST /`, `GET /sessions`, `POST /sessions`), each repeating the same triplet:
`user_id: str = Depends(verify_token)`, then `get_bearer_token(request)` to re-extract the raw token
for `track_user`'s `/userinfo` call. Nothing enforced that a new route added later would remember this
pattern — the risk was a future route that's silently unauthenticated.

Goal: a single central interceptor that validates the token on every incoming HTTP request (so a new
route can't opt out by omission) and exposes the decoded identity to the rest of the service via a
context-local accessor, rather than threading `Request`/`Depends(verify_token)` through every function
signature that needs `user_id`.

## Design

**Mechanism**: raw ASGI middleware (not FastAPI's `@app.middleware("http")` / `BaseHTTPMiddleware`) +
`contextvars.ContextVar`. Raw ASGI, not `BaseHTTPMiddleware`, because `POST /` returns a
`StreamingResponse` (SSE, via AG-UI's `EventEncoder`) and `BaseHTTPMiddleware` is known to buffer/break
streaming responses in some Starlette versions — a raw ASGI middleware class has no such caveat.
`contextvars.ContextVar` (not `threading.local`) because a single worker interleaves many concurrent
requests as async tasks, not threads; a value set in the middleware before `await self.app(...)`
correctly propagates into the request's task tree (the direction this design needs — the known
Starlette contextvar gotcha is only about propagating values *back up* from the endpoint into
middleware after the call returns, which isn't used here).

**New file `src/agent/auth_context.py`** — the "thread-local" accessor layer: a frozen `AuthContext`
dataclass (`user_id`, `claims`, `access_token`) held in a module-level `ContextVar`, with
`set_auth_context`/`reset_auth_context` (called only by the middleware) and three read accessors —
`get_current_user_id()`, `get_current_claims()`, `get_current_access_token()` — each raising
`RuntimeError` if called with no context set.

**New file `src/agent/auth_middleware.py`** — `AuthMiddleware`, a raw ASGI middleware class:
- Passes non-HTTP scopes and an explicit allowlist (`_UNAUTHENTICATED_PATHS = {"/health", "/docs",
  "/redoc", "/openapi.json"}`) straight through. `/docs`/`/redoc`/`/openapi.json` are FastAPI's default
  doc routes, previously unauthenticated by omission (no route ever declared `Depends(verify_token)`
  for them) — the middleware now makes that exclusion explicit.
- Otherwise: builds a `starlette.requests.Request` from the scope, calls the existing
  `get_bearer_token(request)` (unchanged, reused as-is) then the new `authenticate_request(token)`.
  On `HTTPException`, builds and sends a `JSONResponse` directly (middleware sits outside the layer
  that turns a `Depends`-raised `HTTPException` into JSON, so this can't just re-raise).
  On success, calls `set_auth_context(...)`, runs `await self.app(scope, receive, send)`, and resets
  the context in a `finally`.

**`src/agent/auth.py`** — refactor, no behavior change: replaced `verify_token(request) -> str` (the
`Depends`-shaped function, no longer needed anywhere once the three routes below stopped declaring it)
with `authenticate_request(token: str) -> dict[str, Any]`, doing the same `PyJWKClient` +
`jwt.decode(...)` work but taking the already-extracted token string and returning the full claims
dict instead of just `sub`. `get_bearer_token(request)` is unchanged.

**`src/agent/copilotkit_server.py`**:
- `app.add_middleware(AuthMiddleware)` immediately after `app = FastAPI(lifespan=lifespan)` and before
  `FastAPIInstrumentor.instrument_app(app)` — required by the same middleware-stack-caching ordering
  constraint already documented at that call site (Starlette builds/caches its stack on first `__call__`
  regardless of scope type).
- On the three protected routes: dropped `user_id: str = Depends(verify_token)` and the
  `get_bearer_token(request)` calls; replaced with `get_current_user_id()` /
  `get_current_access_token()` from `auth_context`. Dropped the now-unused `request: Request` parameter
  from `GET /sessions` and `POST /sessions` (it was only ever used for `get_bearer_token`); kept it on
  `POST /` (still needed for the `accept` header).
- `GET /health` unaffected — already unauthenticated, now covered by the middleware's explicit
  allowlist instead of by omission.
- `user_tracking.track_user` and `sessions.{list_sessions,create_session,touch_session}` unchanged —
  kept their existing plain-param signatures (`user_id: str`, `access_token: str` passed in
  explicitly); `copilotkit_server.py` is the only place that talks to `auth_context`, keeping those two
  modules agnostic of how the caller obtained the identity (same boundary that already existed).

## Explicitly out of scope

- No change to the checkpoint `thread_id` composite-key scheme, session/user-tracking DB logic, or the
  frontend — this is purely how the backend obtains and threads through `user_id`/`access_token`.
- No test suite changes — none currently exist for `copilotkit_server.py`'s routes. Noted for later:
  once tests are added, FastAPI's `dependency_overrides` no longer applies (there's no `Depends` left
  to override) — tests will need to either send a real/fake bearer token through the middleware, or
  monkeypatch `agent.auth.authenticate_request`.
- No change to WebSocket handling — none exists today; the middleware only special-cases
  `scope["type"] == "http"` and passes anything else straight through unauthenticated, since there's
  nothing else to gate yet.

## Roadmap addition (doc-only)

Added a bullet to `ROADMAP.md`'s existing `## Postgres connection pooling/configuration` section:
`user_pool`'s `min_size`/`max_size`/`timeout`/`max_idle`/`max_lifetime` should be made configurable via
environment variables (today's implicit defaults kept as fallback), so pool sizing can be tuned per
environment without a code change once real traffic patterns are known.

## Status: DONE (closed 2026-08-14)

Code changes complete (`auth_context.py`, `auth_middleware.py`, `auth.py`, `copilotkit_server.py`,
`ROADMAP.md`) and manually reviewed line-by-line. `ast.parse` on all four Python files passed.

Live-verified end-to-end by the user:
- App runs and chat/session list/create all work through the real UI (proves a valid bearer token is
  flowing through the middleware on every request, since there's no fallback path left if it weren't).
- `curl` against the backend directly (port 8000) with no `Authorization` header, and with a garbage
  token, both returned 401 from `AuthMiddleware` itself (not a 500 or FastAPI's default validation
  error) — confirmed the middleware intercepts before the route handler runs.
- Clarified the two-hop auth architecture: browser↔Next.js uses the Auth0 session cookie (why no
  `Authorization` header appears in browser devtools); Next.js server↔`carqna-agent` is the
  server-to-server hop that carries the bearer token `AuthMiddleware` verifies (`route.ts`'s
  `callAgent`/`tracedFetch`, via `auth0.getAccessToken()`).

`mypy --strict` / import-check pass explicitly skipped per user instruction ("Skip mypy") — the file
was repeatedly blocked by a transient Bash tool-availability outage during this session, and the
manual code review plus live end-to-end pass above were judged sufficient.

**Known gap at close** (deliberately deferred, not forgotten):
- `mypy --strict` never actually ran clean on these four files — skipped, not verified.
- `/docs`, `/redoc`, `/openapi.json` allowlist entries not explicitly curl-tested (only `/health` and
  the protected routes were).
- Expired-token behavior not explicitly tested (only missing-header and garbage-token cases).
- Cross-user isolation on `/sessions` not re-verified under the new middleware specifically (same gap
  `.plans/006` left open for the pre-middleware version).

## Verification

1. ~~Import-check + `mypy --strict`~~ — skipped per user instruction, see Status above.
2. Manually traced: a request to `/health` hits the allowlist and skips the middleware's token check; a
   request to `/` or `/sessions` without a token gets a 401 JSON body from the middleware (not a 500);
   a request with a valid token reaches the route handler with `get_current_user_id()` returning the
   same value `Depends(verify_token)` used to. Confirmed live via curl and the real UI.
3. Live end-to-end testing (real login, chat message, session create/list) confirmed working by the
   user.
