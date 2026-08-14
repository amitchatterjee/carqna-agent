"""Request-scoped auth context, populated once per request by AuthMiddleware.

Downstream code (route handlers, service functions) reads the verified
identity via these accessors instead of threading `Request`/`Depends` through
every call site. Backed by `contextvars.ContextVar`, not `threading.local` --
a single worker interleaves many concurrent requests as async tasks, not
threads, and a value set before `await self.app(...)` in the middleware
correctly propagates into that request's task tree.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The verified identity for the request currently in flight."""

    user_id: str
    claims: dict[str, Any]
    access_token: str


_ctx: ContextVar["AuthContext | None"] = ContextVar("auth_context", default=None)


def set_auth_context(ctx: AuthContext) -> Token:
    """Set the auth context for the current task; returns a reset token."""
    return _ctx.set(ctx)


def reset_auth_context(token: Token) -> None:
    """Undo a prior `set_auth_context` call using its reset token."""
    _ctx.reset(token)


def _require_context() -> AuthContext:
    ctx = _ctx.get()
    if ctx is None:
        raise RuntimeError(
            "No authenticated request context is active -- AuthMiddleware "
            "must run before this code path, and the route must not be on "
            "the middleware's unauthenticated-path allowlist."
        )
    return ctx


def get_current_user_id() -> str:
    """Return the verified caller's user id (the JWT `sub` claim)."""
    return _require_context().user_id


def get_current_claims() -> dict[str, Any]:
    """Return the verified caller's full JWT claims."""
    return _require_context().claims


def get_current_access_token() -> str:
    """Return the caller's raw bearer token, e.g. for forwarding to `/userinfo`."""
    return _require_context().access_token
