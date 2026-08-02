import logging
from contextlib import asynccontextmanager

from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy import: keep module importable without MCP reachable
    from agent.graph import create_graph, _get_checkpointer_conn_string

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
