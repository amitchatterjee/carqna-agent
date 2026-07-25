"""Step 1 spike: expose graph.py's agent over the AG-UI protocol.

Validates the actual chosen backend integration path -- the `copilotkit`
Python SDK mounted directly on carqna_dev -- before any real UI work. See
.plans/copilot-ui-remediation-plan.md.

Run from project root (after `pip install -e .` in the venv):
    python -m agent.copilotkit_server

No Docker, no Dapr -- run directly on the host for the fastest iteration
loop. Requires `opensearch` reachable (see MCP_CONFIG_PATH in .env).
"""

import logging
import os
from contextlib import asynccontextmanager

from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy import: keep module importable without MCP reachable, matching
    # carqna_dapr.py's convention.
    from agent.graph import create_graph

    db_path = os.path.expanduser(os.getenv("CHECKPOINT_DB_PATH", "./.db.sqlite3"))
    logger.info(f"Using checkpoint database: {db_path}")

    # async-with spans the yield, so teardown runs on shutdown -- same
    # AsyncSqliteSaver lifetime as carqna_dapr.py, without the manual
    # __aenter__/__aexit__ + module-global juggling that aiohttp's split
    # startup/cleanup hooks required there.
    async with AsyncSqliteSaver.from_conn_string(db_path) as checkpointer:
        graph = await create_graph(checkpointer=checkpointer)
        agent = LangGraphAGUIAgent(name="carqna_agent", graph=graph)
        add_langgraph_fastapi_endpoint(app, agent, path="/")
        logger.info("carqna_agent mounted at AG-UI endpoint /")
        yield


app = FastAPI(lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
