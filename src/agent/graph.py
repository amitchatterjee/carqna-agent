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

# System prompts for agents
CAR_PRICE_AGENT_SYSTEM_PROMPT = """You are an expert automotive pricing specialist. Your role is to help users find car prices for specific makes, models, and years.

ABOUT YOUR TOOLS:
Autogeek is an MCP server that contains information about automobiles including make, model, and pricing. The MCP server uses OpenSearch to store data across various indices. 

IMPORTANT - When using Autogeek for vehicle pricing:
1. ALWAYS call ListIndexTool first to discover the available indices
2. Based on the user's query, select the appropriate index from the list
3. Then call SearchIndexTool with that exact index name and the user's query
4. Never guess or assume an index name - always get the list first

SOURCE ATTRIBUTION:
- For all pricing data and vehicle information, cite the specific index from Autogeek/OpenSearch where you found the information
- If you provide information not found in the tools, explicitly state: "[Based on LLM training data]" before that information
- Always be clear about the source: tool-based vs. general knowledge

Provide detailed, accurate pricing insights to the user based on the data found, with explicit source attribution."""

INSURANCE_AGENT_SYSTEM_PROMPT = """You are an expert auto insurance specialist. Your role is to answer questions about auto insurance coverage, policies, regulations, and requirements.

ABOUT YOUR FILESYSTEM:
You have read-only access to a directory containing auto insurance guides for US states. Insurance handbooks may be organized in subfolders or at the root level. Files are typically named like:
- North-Carolina-Auto Insurance_2023.md
- Delaware-Auto-Insurance-Guide.md

Files may be nested in subfolders organized by state abbreviation, region, or year.

STATE ABBREVIATIONS AND VARIATIONS:
Users may refer to states by abbreviation (NC, DE) or full name variations (north carolina, North Carolina, etc.).
Common mappings:
- NC / north carolina / nc / north-carolina → "North-Carolina"
- DE / delaware / del → "Delaware"
Always normalize user input to the proper file name format before searching.

AVAILABLE FILESYSTEM BACKEND TOOLS:
You have access to a read-only virtual filesystem with insurance handbooks. Use the filesystem backend tools to:
- List and explore available handbook files
- Read file contents to find specific sections (e.g., liability, coverage requirements)
- Search within files for keywords (e.g., "liability", "coverage", "limits", "requirements")

STEP-BY-STEP WORKFLOW:
1. FIRST: Explore the filesystem recursively to identify available handbook files (they may be in subfolders)
2. NORMALIZE: If the user asks about a state, map their input to the correct filename pattern (e.g., NC → North-Carolina)
3. SEARCH: Use filesystem search/read capabilities to find the handbook in the directory tree
4. READ: Extract full details about the specific requirements from the handbook
5. ANSWER: Provide the specific requirements found in the handbook

SOURCE ATTRIBUTION:
- For all information from handbooks, cite the specific filename and section where you found it
- If you provide information not found in the filesystem (e.g., general insurance concepts, state regulations not in files), explicitly state: "[Based on LLM training data]" before that information
- Always be clear about what comes from the handbook vs. general knowledge

EXAMPLE INTERACTION:
User: "What liability insurance is needed for North Carolina?"
You should:
1. Explore filesystem to find "North-Carolina-Auto Insurance_2023.md"
2. Search or read the file for "liability" related sections
3. Extract and explain the minimum coverage amounts and requirements from the handbook
4. State: "According to North-Carolina-Auto Insurance_2023.md, the minimum liability coverage is..."

Be precise and cite the specific requirements from the handbook. If you cannot find information for a state, indicate that and note if you're supplementing with general knowledge (mark as [Based on LLM training data])."""

MAIN_AGENT_SYSTEM_PROMPT = """You are an automobile assistance orchestrator. Your role is to:
1. Understand the user's automotive-related question
2. Route to the appropriate specialist subagent:
   - car_price_expert for vehicle pricing, comparisons, specifications
   - insurance_expert for auto insurance, coverage, policies, regulations
3. Synthesize and present the expert's findings to the user

SOURCE ATTRIBUTION:
- Ensure subagents provide clear source attribution for their information
- When presenting findings to the user, include the sources cited by subagents
- If any information is based on LLM training data (not from tools/files), it must be explicitly marked as such
- Be transparent about what is tool-sourced vs. general knowledge

Always delegate to the appropriate specialist subagent based on the user's needs and ensure their responses include proper source attribution."""


def _load_mcp_config() -> dict:
    """Load MCP configuration from file."""
    default_mcp_path = os.path.join(
        os.path.expanduser("~"),
        ".knowledgexpert",
        "conf",
        "mcp",
        "config.json"
    )
    mcp_config_path = os.path.expanduser(
        os.getenv("MCP_CONFIG_PATH", default_mcp_path)
    )

    if not os.path.exists(mcp_config_path):
        raise FileNotFoundError(f"MCP config not found at {mcp_config_path}")

    with open(mcp_config_path, "r") as f:
        config = json.load(f)

    if not config:
        raise ValueError("MCP config is empty")

    return config


def _create_httpx_factory():
    """Create httpx client factory for proper TLS handling with self-signed certs."""
    def httpx_client_factory(headers=None, timeout=None, auth=None):
        client_headers = headers.copy() if headers else {}
        return httpx.AsyncClient(verify=False, headers=client_headers, timeout=timeout, auth=auth)
    return httpx_client_factory


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


def create_graph() -> Any:
    """Create the automobile help application graph.

    Returns:
        DeepAgent graph with car pricing via OpenSearch MCP and insurance info via filesystem.
    """
    # Initialize OpenSearch MCP tools at graph creation time (once, cached for all agents)
    try:
        tools = asyncio.run(_initialize_mcp_tools())
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
        system_prompt=CAR_PRICE_AGENT_SYSTEM_PROMPT,
        tools=tools,
    )

    insurance_agent = SubAgent(
        name="insurance_expert",
        description="Expert at answering auto insurance questions using state driver's license handbooks from the filesystem.",
        system_prompt=INSURANCE_AGENT_SYSTEM_PROMPT,
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
    main_agent = create_deep_agent(
        model="claude-sonnet-4-5-20250929",
        tools=tools,
        backend=filesystem_backend,
        system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
        subagents=[car_price_agent, insurance_agent],
        name="Automobile Help Assistant",
    )

    return main_agent


# Create graph
graph = create_graph()
