import logging
from contextlib import asynccontextmanager

import langsmith
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

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

    # async-with spans the yield, so teardown runs on shutdown
    async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        # Idempotent -- creates the checkpoint tables/runs migrations if needed.
        # Postgres requires this explicitly; AsyncSqliteSaver never did.
        await checkpointer.setup()
        graph = await create_graph(checkpointer=checkpointer)
        agent = LangGraphAGUIAgent(name="carqna_agent", graph=graph)
        add_langgraph_fastapi_endpoint(app, agent, path="/")
        logger.info("carqna_agent mounted at AG-UI endpoint /")
        yield


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
