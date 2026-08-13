"""Automobile Help Application with Multi-Agent Orchestration.

Implements an automobile assistance system using DeepAgents where specialized
subagents handle car price lookups and auto insurance questions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent, SubAgent
from deepagents.backends import FilesystemBackend
from langchain_mcp_adapters.client import MultiServerMCPClient
from dotenv import load_dotenv
import httpx

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Module-level MCP client and tools (initialized once)
_mcp_client = None
_mcp_tools = []


def _load_prompt_from_file(filename: str) -> str:
    """Load a prompt from a markdown file in the prompts directory.
    
    Args:
        filename: Name of the prompt file (e.g., 'car_price_agent.md')
        
    Returns:
        The contents of the prompt file (trimmed of markdown front matter).
        
    Raises:
        FileNotFoundError: If the prompt file is not found.
    """
    prompts_dir = os.getenv("PROMPTS_DIR", ".")
    prompt_path = os.path.join(prompts_dir, filename)
    
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(
            f"Prompt file not found at {prompt_path}. "
            f"Set PROMPTS_DIR environment variable if prompts are in a different directory."
        )
    
    with open(prompt_path, "r") as f:
        content = f.read().strip()
    
    return content


def _load_mcp_config() -> dict:
    """Load MCP configuration from file."""
    mcp_config_path = os.path.expanduser(
        os.getenv("MCP_CONFIG_PATH")
    )

    if not os.path.exists(mcp_config_path):
        raise FileNotFoundError(f"MCP config not found at {mcp_config_path}")

    with open(mcp_config_path, "r") as f:
        config = json.load(f)

    if not config:
        raise ValueError("MCP config is empty")

    logger.info(f'Loaded mcp config file from {mcp_config_path}')
    return config


def _create_httpx_factory():
    """Create httpx client factory for proper TLS handling with self-signed certs."""
    def httpx_client_factory(headers=None, timeout=None, auth=None):
        client_headers = headers.copy() if headers else {}
        return httpx.AsyncClient(verify=False, headers=client_headers, timeout=timeout, auth=auth)
    return httpx_client_factory


def _get_checkpointer_conn_string() -> str:
    """Get the Postgres checkpointer connection string.

    For local development use only. LangGraph API handles persistence automatically.

    Returns:
        Connection string for AsyncPostgresSaver.from_conn_string()
        Reads from CHECKPOINT_POSTGRES_URI environment variable.
        Defaults to the local `carqna` database (see
        infrastructure/docker/postgres/initdb.d/init_user.sh) if not set.
    """
    default_uri = "postgresql://carqna:carqna@localhost:5432/carqna"
    conn_string = os.getenv("CHECKPOINT_POSTGRES_URI", default_uri)
    logger.info(f"Using checkpoint database at: {conn_string.split('@')[-1]}")
    return conn_string


async def _initialize_mcp_tools():
    """Initialize MCP client and load tools once."""
    global _mcp_client, _mcp_tools

    if _mcp_tools:
        return _mcp_tools  # Already initialized

    try:
        config = _load_mcp_config()
        httpx_factory = _create_httpx_factory()

        # Configure httpx for streamable_http transports
        for v in config.values():
            if isinstance(v, dict) and v.get("transport") in ("streamable_http", "sse"):
                v["httpx_client_factory"] = httpx_factory

        # Initialize MCP client - KEEP IT ALIVE
        _mcp_client = MultiServerMCPClient(config)
        logger.info(
            "MCP client created and stored globally to keep session alive")

        # Get tools
        _mcp_tools = await _mcp_client.get_tools()

        if not _mcp_tools:
            raise RuntimeError("No MCP tools available")

        # Log available tools for debugging
        tool_names = [t.name for t in _mcp_tools]
        logger.info(f"MCP client initialized with tools: {tool_names}")

        return _mcp_tools

    except Exception as e:
        logger.error(f"Failed to initialize MCP tools: {e}", exc_info=True)
        raise


async def create_graph(checkpointer=None) -> Any:
    """Create the automobile help application graph.

    Args:
        checkpointer: Optional checkpointer for state persistence. 
                     If None, LangGraph API will handle persistence.

    Returns:
        DeepAgent graph with car pricing via OpenSearch MCP and insurance info via filesystem.
    """
    # Initialize OpenSearch MCP tools at graph creation time (once, cached for all agents)
    try:
        tools = await _initialize_mcp_tools()
    except Exception as e:
        logger.error(f"Failed to initialize MCP tools: {e}")
        raise

    # Initialize read-only filesystem backend for insurance data
    default_filesystem_root = "~/git/knowledgexpert/data/linux-exec/insurance-docs"
    filesystem_root = os.path.expanduser(
        os.getenv("INSURANCE_DOCS_ROOT", default_filesystem_root)
    )
    if not os.path.exists(filesystem_root):
        raise FileNotFoundError(
            f"Filesystem root not found: {filesystem_root}")

    filesystem_backend = FilesystemBackend(
        root_dir=filesystem_root,
        # Prevents path traversal (blocks .., ~, absolute paths)
        virtual_mode=True,
    )
    logger.info(
        f"Initialized read-only filesystem backend with root: {filesystem_root}")

    # Define specialized subagents
    car_price_agent = SubAgent(
        name="car_price_expert",
        description="Expert at finding and comparing car prices using Autogeek OpenSearch service.",
        system_prompt=_load_prompt_from_file("car_price_agent.md"),
        tools=tools,
    )

    insurance_agent = SubAgent(
        name="insurance_expert",
        description="Expert at answering auto insurance questions using state driver's license handbooks from the filesystem.",
        system_prompt=_load_prompt_from_file("insurance_agent.md"),
        permissions=[FilesystemPermission(
            operations=["read",],
            paths=['/'],
            mode="allow"
        ), FilesystemPermission(
            operations=["write", "delete"],
            paths=['/'],
            mode="deny"
        )]
    )

    # Create the main deep agent with subagents
    # Main agent has MCP tools for pricing; subagents inherit what they need
    # Note: LangGraph API handles persistence automatically, so we don't pass a checkpointer there
    llm_model = os.getenv("LLM_MODEL", "claude-sonnet-4-5-20250929")
    
    main_agent = create_deep_agent(
        model=llm_model,
        tools=tools,
        backend=filesystem_backend,
        system_prompt=_load_prompt_from_file("main_agent.md"),
        subagents=[car_price_agent, insurance_agent],
        name="Automobile Help Assistant",
        checkpointer=checkpointer,
    )

    return main_agent


# For compatibility with both LangGraph API (sync) and carqna.py (async)
_graph_instance = None


def get_graph() -> Any:
    """Get the graph instance, creating it if needed (synchronous)."""
    global _graph_instance
    if _graph_instance is None:
        # Try to create graph synchronously if possible
        try:
            # Check if we're in an event loop
            asyncio.get_running_loop()
            # We're in an async context - can't use asyncio.run()
            raise RuntimeError(
                "Graph not yet initialized. Call create_graph() in async context instead."
            )
        except RuntimeError as e:
            if "asyncio.run()" in str(e):
                raise
            # No event loop, safe to use asyncio.run()
            _graph_instance = asyncio.run(create_graph())
    return _graph_instance


# For LangGraph API - lazy create graph
graph = None
try:
    graph = get_graph()
except Exception:
    # Will be created later in async context (or if MCP fails at import time)
    logger.debug("Graph not initialized at import time (expected for services like copilotkit_server)")
    pass
