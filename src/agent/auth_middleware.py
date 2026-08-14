"""Central auth interceptor: verifies the bearer token on every HTTP request.

Raw ASGI middleware, not FastAPI's `@app.middleware("http")` /
`BaseHTTPMiddleware` -- `POST /` in `copilotkit_server.py` returns a
`StreamingResponse` (AG-UI SSE), and `BaseHTTPMiddleware` is known to
buffer/break streaming responses in some Starlette versions. A raw ASGI
middleware class has no such caveat.

Any route not in `_UNAUTHENTICATED_PATHS` must have a verified token or the
request never reaches it -- unlike a per-route `Depends(...)`, a new route
can't opt out of auth by simply forgetting to declare it.
"""

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from agent.auth import authenticate_request, get_bearer_token
from agent.auth_context import AuthContext, reset_auth_context, set_auth_context

# FastAPI's default doc routes were previously unauthenticated by omission
# (no route ever declared Depends(verify_token) for them); /health likewise.
# The middleware now needs that exclusion made explicit.
_UNAUTHENTICATED_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


class AuthMiddleware:
    """ASGI middleware that verifies the bearer token and populates auth context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _UNAUTHENTICATED_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            token = get_bearer_token(request)
            claims = authenticate_request(token)
        except HTTPException as exc:
            # Middleware sits outside the layer that turns a Depends-raised
            # HTTPException into a JSON response, so build the response here
            # directly rather than re-raising.
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        reset_token = set_auth_context(
            AuthContext(user_id=claims["sub"], claims=claims, access_token=token)
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_auth_context(reset_token)
