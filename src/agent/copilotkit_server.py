import logging
from contextlib import asynccontextmanager

import langsmith
from ag_ui.core.types import RunAgentInput
from ag_ui.encoder import EventEncoder
from copilotkit import LangGraphAGUIAgent
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from psycopg_pool import AsyncConnectionPool

from agent.auth import get_bearer_token, verify_token
from agent.user_tracking import track_user

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy import: keep module importable without MCP reachable
    from agent.graph import create_graph, _get_checkpointer_conn_string

    # Constructing a Client (when LANGSMITH_TRACING_MODE=otel/hybrid) is what
    # makes langsmith register its OTel TracerProvider as the process-global
    # one -- do this before any real request comes in so its spans (and every
    # LangChain span nested under them) land on the same provider/exporter,
    # instead of racing whichever LangChain call would otherwise trigger it
    # first, lazily, mid-request. (FastAPIInstrumentor itself must NOT be
    # called here -- see the module-level comment by `app = FastAPI(...)`.)
    langsmith.Client()

    conn_string = _get_checkpointer_conn_string()

    # Separate small pool for the `user_registry` table (see agent/user_tracking.py) --
    # deliberately not sharing AsyncPostgresSaver's internal pool, to keep this
    # groundwork decoupled from its (unrelated) checkpoint persistence.
    user_pool = AsyncConnectionPool(conn_string, open=False)
    await user_pool.open()

    try:
        # async-with spans the yield, so teardown runs on shutdown
        async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
            # Idempotent -- creates the checkpoint tables/runs migrations if needed.
            # Postgres requires this explicitly; AsyncSqliteSaver never did.
            await checkpointer.setup()
            graph = await create_graph(checkpointer=checkpointer)
            agent = LangGraphAGUIAgent(name="carqna_agent", graph=graph)

            # Hand-rolled equivalent of ag_ui_langgraph's add_langgraph_fastapi_endpoint
            # (ag_ui_langgraph/endpoint.py) -- that helper has no hook for auth or for
            # rewriting thread_id, so this mirrors its implementation with two
            # insertions: the verify_token dependency, and namespacing thread_id by the
            # verified user so one user can never read/continue another's conversation
            # (see .plans/004-2026-08-09-oauth2-okta-auth-plan-DONE.md's "Ownership
            # is enforced structurally" note).
            @app.post("/")
            async def langgraph_agent_endpoint(
                input_data: RunAgentInput,
                request: Request,
                user_id: str = Depends(verify_token),
            ):
                accept_header = request.headers.get("accept")
                encoder = EventEncoder(accept=accept_header)

                # Groundwork for the deferred multi-session picker feature --
                # maps the opaque user_id to a human identity (email/name) in a
                # separate `user_registry` table. Never blocks/fails the actual
                # chat request (see user_tracking.track_user's own try/except).
                await track_user(user_pool, user_id, get_bearer_token(request))

                # Never trust a client-supplied user id -- the composite key is
                # built from the verified token's `sub` claim only.
                input_data.thread_id = f"{user_id}:{input_data.thread_id}"

                # Clone the agent so each request gets its own isolated state.
                # LangGraphAgent stores per-request state in self.active_run; sharing
                # a single instance across concurrent requests corrupts that state.
                request_agent = agent.clone()

                async def event_generator():
                    async for event in request_agent.run(input_data):
                        yield encoder.encode(event)

                return StreamingResponse(
                    event_generator(), media_type=encoder.get_content_type()
                )

            @app.get("/health")
            def health():
                """Health check -- unauthenticated, for infra checks."""
                return {"status": "ok", "agent": {"name": agent.name}}

            logger.info("carqna_agent mounted at AG-UI endpoint / (auth required)")
            yield
    finally:
        await user_pool.close()


app = FastAPI(lifespan=lifespan)
# Must happen here, before uvicorn sends this app *any* ASGI scope --
# including the lifespan scope itself. Starlette builds and caches its
# middleware stack on the very first __call__ regardless of scope type
# (starlette/applications.py: `if self.middleware_stack is None:
# self.middleware_stack = self.build_middleware_stack()`), and
# FastAPIInstrumentor works by monkey-patching build_middleware_stack --
# calling it from inside lifespan() is too late, since that function body
# only runs *after* the first __call__ (the lifespan scope) already built
# and cached the (uninstrumented) stack. Confirmed the hard way: the fetch
# span from the UI showed up in Jaeger with no backend spans nested under
# it until this moved here.
FastAPIInstrumentor.instrument_app(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
