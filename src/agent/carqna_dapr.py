"""Dapr service wrapper for CarQnA agent - exposes to Copilot Kit via HTTP.

Run from project root:
    python -m agent.carqna_dapr
    
Environment variables:
    DAPR_SERVICE_PORT: Port to listen on (default: 5001)
    DAPR_SERVICE_NAME: Service name for Dapr (default: carqna-dapr)
    CHECKPOINT_DB_PATH: SQLite database for conversation history
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from aiohttp import web
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

# Ensure we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent))

# Note: We delay importing create_graph until initialize_agent() to avoid
# eager MCP initialization at module import time. This allows carqna_dapr.py
# to be imported without requiring MCP services to be running.

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Global agent instance (initialized once at startup)
_agent = None
_checkpointer = None
_checkpointer_cm = None  # Store the context manager for cleanup


# ============================================================================
# Pydantic Models
# ============================================================================

class ChatRequest(BaseModel):
    """Request for chat endpoint."""
    message: str = Field(..., description="User query/message")
    thread_id: Optional[str] = Field(None, description="Conversation thread ID (auto-generated if missing)")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional metadata")
    include_trace: bool = Field(True, description="Include event trace in response")


class ChatResponse(BaseModel):
    """Response for chat endpoint."""
    response: str = Field(..., description="Agent's response")
    thread_id: str = Field(..., description="Conversation thread ID")
    timestamp: str = Field(..., description="ISO format response time")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Response metadata")
    events: list[dict[str, Any]] = Field(default_factory=list, description="Event trace (if requested)")
    event_count: int = Field(0, description="Number of events captured")


class InitRequest(BaseModel):
    """Request to initialize a new conversation."""
    user_id: Optional[str] = Field(None, description="Optional user identifier")
    session_name: Optional[str] = Field(None, description="Optional session name")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional metadata")


class InitResponse(BaseModel):
    """Response for init endpoint."""
    thread_id: str = Field(..., description="New conversation thread ID")
    status: str = Field("initialized", description="Status")
    timestamp: str = Field(..., description="ISO format creation time")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Response metadata")


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field("healthy", description="Health status")
    service: str = Field("carqna-dapr", description="Service name")
    timestamp: str = Field(..., description="ISO format check time")


class MetadataResponse(BaseModel):
    """Service metadata response."""
    name: str = Field("carqna-dapr", description="Service name")
    version: str = Field("0.0.1", description="Service version")
    description: str = Field(..., description="Service description")
    endpoints: list[str] = Field(..., description="Available endpoints")


class GraphEvent(BaseModel):
    """Formatted graph event for frontend consumption."""
    type: str = Field(..., description="Event type: tool_call, tool_result, llm_thinking, final_response, error")
    step: int = Field(..., description="Step number in execution")
    timestamp: str = Field(..., description="ISO format timestamp")
    tool: Optional[str] = Field(None, description="Tool name (for tool events)")
    input: Optional[dict[str, Any]] = Field(None, description="Tool input")
    output: Optional[str] = Field(None, description="Tool or LLM output")
    content: Optional[str] = Field(None, description="Reasoning or response content")
    error: Optional[str] = Field(None, description="Error message (if error event)")


# ============================================================================
# Event Formatting
# ============================================================================

def _format_graph_event(event: dict, step: int) -> Optional[GraphEvent]:
    """Convert LangGraph event to frontend-friendly format.
    
    Args:
        event: Raw LangGraph event
        step: Step counter
        
    Returns:
        Formatted GraphEvent or None if not relevant
    """
    event_type = event.get('event')
    timestamp = datetime.now().isoformat()
    
    try:
        if event_type == 'on_tool_start':
            data = event.get('data', {})
            tool_name = data.get('tool')
            if not tool_name:
                tool_name = event.get('metadata', {}).get('tool_name', 'unknown')
            
            return GraphEvent(
                type='tool_call',
                step=step,
                timestamp=timestamp,
                tool=tool_name,
                input=data.get('input', {})
            )
        
        elif event_type == 'on_tool_end':
            data = event.get('data', {})
            tool_name = data.get('tool')
            if not tool_name:
                tool_name = event.get('metadata', {}).get('tool_name', 'unknown')
            
            output = data.get('output', '')
            if isinstance(output, dict):
                output = json.dumps(output)
            elif not isinstance(output, str):
                output = str(output)
            
            return GraphEvent(
                type='tool_result',
                step=step,
                timestamp=timestamp,
                tool=tool_name,
                output=output[:500]  # Limit output size for readability
            )
        
        elif event_type == 'on_llm_start':
            return GraphEvent(
                type='llm_thinking',
                step=step,
                timestamp=timestamp,
                content='LLM processing...'
            )
        
        elif event_type == 'on_llm_end':
            data = event.get('data', {})
            output = data.get('output', {})
            
            # Extract message content
            if isinstance(output, dict):
                content = output.get('content', '')
                if isinstance(content, list) and content:
                    content = content[0].get('text', '') if isinstance(content[0], dict) else str(content[0])
                else:
                    content = str(content)
            else:
                content = str(output)
            
            if content:
                return GraphEvent(
                    type='llm_thinking',
                    step=step,
                    timestamp=timestamp,
                    content=content[:300]  # Limit for readability
                )
        
        elif event_type == 'on_chain_end':
            # Capture final result from chain completion
            data = event.get('data', {})
            output = data.get('output', {})
            
            if isinstance(output, dict) and 'messages' in output:
                messages = output['messages']
                if messages and hasattr(messages[-1], 'content'):
                    return GraphEvent(
                        type='final_response',
                        step=step,
                        timestamp=timestamp,
                        content=str(messages[-1].content)
                    )
    
    except Exception as e:
        logger.warning(f"Failed to format event: {e}", exc_info=True)
    
    return None


def _extract_response(result: dict) -> str:
    """Extract final response text from agent result.
    
    Args:
        result: Result dict from agent.ainvoke()
        
    Returns:
        Response text
    """
    if isinstance(result, dict) and "messages" in result:
        messages = result["messages"]
        if messages:
            last_msg = messages[-1]
            if hasattr(last_msg, "content"):
                return str(last_msg.content)
    
    return str(result)


# ============================================================================
# Request Handlers
# ============================================================================

async def health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    response = HealthResponse(timestamp=datetime.now().isoformat())
    return web.json_response(response.model_dump())


async def metadata(request: web.Request) -> web.Response:
    """Service metadata endpoint."""
    response = MetadataResponse(
        description="Automobile Help Assistant (CarQnA) via Dapr",
        endpoints=[
            "POST /invoke/chat",
            "POST /invoke/chat/stream",
            "POST /invoke/init",
            "GET /health",
            "GET /metadata"
        ]
    )
    return web.json_response(response.model_dump())


async def init_conversation(request: web.Request) -> web.Response:
    """Initialize a new conversation thread.
    
    POST /invoke/init
    Request: InitRequest (JSON)
    Response: InitResponse (JSON)
    """
    try:
        data = await request.json()
        init_req = InitRequest(**data)
        
        thread_id = str(uuid.uuid4())
        metadata_resp = {
            "user_id": init_req.user_id,
            "session_name": init_req.session_name,
            **init_req.metadata
        }
        
        logger.info(f"Initialized new conversation thread: {thread_id}")
        
        response = InitResponse(
            thread_id=thread_id,
            timestamp=datetime.now().isoformat(),
            metadata=metadata_resp
        )
        return web.json_response(response.model_dump())
    
    except Exception as e:
        logger.error(f"Error initializing conversation: {e}", exc_info=True)
        return web.json_response(
            {"error": str(e), "type": "initialization_error"},
            status=500
        )


async def chat(request: web.Request) -> web.Response:
    """Chat endpoint with optional event trace.
    
    POST /invoke/chat
    Request: ChatRequest (JSON)
    Response: ChatResponse (JSON) with optional events array
    """
    try:
        data = await request.json()
        chat_req = ChatRequest(**data)
        
        # Generate thread ID if not provided
        thread_id = chat_req.thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        
        logger.info(f"Chat request (thread: {thread_id}): {chat_req.message[:50]}...")
        
        # Collect events if trace is requested
        events = []
        response_text = ""
        
        if chat_req.include_trace:
            step_counter = 0
            try:
                # Stream events and capture final response
                async for event in _agent.astream_events(
                    {"messages": [HumanMessage(content=chat_req.message)]},
                    config=config,
                    version="v2"
                ):
                    formatted_event = _format_graph_event(event, step_counter)
                    if formatted_event:
                        events.append(formatted_event.model_dump())
                        # Capture final response from final_response event
                        if formatted_event.type == 'final_response':
                            response_text = formatted_event.content or response_text
                        step_counter += 1
                        logger.debug(f"Event {step_counter}: {formatted_event.type}")
            except Exception as e:
                logger.warning(f"Event streaming failed: {e}", exc_info=True)
                response_text = f"Error during streaming: {str(e)}"
        else:
            # No trace requested - just invoke once
            try:
                result = await _agent.ainvoke(
                    {"messages": [HumanMessage(content=chat_req.message)]},
                    config=config
                )
                response_text = _extract_response(result)
            except Exception as e:
                logger.error(f"Error invoking agent: {e}", exc_info=True)
                response_text = f"Error: {str(e)}"
        
        logger.info(f"Chat response (thread: {thread_id}): {response_text[:50]}...")
        
        response = ChatResponse(
            response=response_text,
            thread_id=thread_id,
            timestamp=datetime.now().isoformat(),
            metadata=chat_req.metadata,
            events=events,
            event_count=len(events)
        )
        
        return web.json_response(response.model_dump())
    
    except ValueError as e:
        logger.error(f"Invalid request: {e}")
        return web.json_response(
            {"error": str(e), "type": "validation_error"},
            status=400
        )
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}", exc_info=True)
        return web.json_response(
            {"error": str(e), "type": "chat_error"},
            status=500
        )


async def chat_stream(request: web.Request) -> web.StreamResponse:
    """Chat endpoint with Server-Sent Events (SSE) streaming.
    
    POST /invoke/chat/stream
    Request: ChatRequest (JSON)
    Response: Stream of GraphEvent JSON objects (SSE format)
    
    Example:
        data: {"type": "tool_call", "tool": "opensearch", ...}
        data: {"type": "tool_result", "output": "..."}
        data: {"type": "final_response", "content": "..."}
        data: {"type": "done"}
    """
    try:
        data = await request.json()
        chat_req = ChatRequest(**data)
        
        # Generate thread ID if not provided
        thread_id = chat_req.thread_id or str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        
        logger.info(f"Stream chat request (thread: {thread_id}): {chat_req.message[:50]}...")
        
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        await response.prepare(request)
        
        step_counter = 0
        try:
            # Stream events from graph execution
            async for event in _agent.astream_events(
                {"messages": [HumanMessage(content=chat_req.message)]},
                config=config,
                version="v2"
            ):
                formatted_event = _format_graph_event(event, step_counter)
                if formatted_event:
                    event_json = formatted_event.model_dump()
                    await response.write(
                        f"data: {json.dumps(event_json)}\n\n".encode()
                    )
                    step_counter += 1
                    logger.debug(f"Streamed event {step_counter}: {formatted_event.type}")
            
            # Send completion marker
            await response.write(
                b"data: {\"type\": \"done\", \"event_count\": " + 
                f"{step_counter}".encode() + 
                b"}\n\n"
            )
            
            logger.info(f"Stream completed (thread: {thread_id}, events: {step_counter})")
        
        except asyncio.CancelledError:
            logger.info(f"Stream cancelled (thread: {thread_id})")
        except Exception as e:
            logger.error(f"Error streaming events: {e}", exc_info=True)
            error_event = {
                "type": "error",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }
            await response.write(f"data: {json.dumps(error_event)}\n\n".encode())
        
        await response.write_eof()
        return response
    
    except ValueError as e:
        logger.error(f"Invalid stream request: {e}")
        return web.json_response(
            {"error": str(e), "type": "validation_error"},
            status=400
        )
    except Exception as e:
        logger.error(f"Error in stream endpoint: {e}", exc_info=True)
        return web.json_response(
            {"error": str(e), "type": "stream_error"},
            status=500
        )


# ============================================================================
# App Factory and Initialization
# ============================================================================

async def initialize_agent():
    """Initialize the agent and checkpointer (called on server startup)."""
    global _agent, _checkpointer, _checkpointer_cm
    
    if _agent is not None:
        return  # Already initialized
    
    try:
        logger.info("Initializing CarQnA agent...")
        
        # Import create_graph here (lazy import) to avoid eager MCP initialization
        from agent.graph import create_graph
        
        # Get database connection string
        default_db_path = "./.db.sqlite3"
        db_path = os.getenv("CHECKPOINT_DB_PATH", default_db_path)
        db_path = os.path.expanduser(db_path)
        logger.info(f"Using checkpoint database: {db_path}")
        
        # Create and enter the checkpointer context manager
        # This gives us the actual AsyncSqliteSaver object
        _checkpointer_cm = AsyncSqliteSaver.from_conn_string(db_path)
        _checkpointer = await _checkpointer_cm.__aenter__()
        
        # Create graph with checkpointer
        _agent = await create_graph(checkpointer=_checkpointer)
        
        logger.info("✓ CarQnA agent initialized successfully")
    
    except Exception as e:
        logger.error(f"Failed to initialize agent: {e}", exc_info=True)
        raise


async def cleanup_agent():
    """Cleanup agent and checkpointer (called on server shutdown)."""
    global _agent, _checkpointer, _checkpointer_cm
    
    if _checkpointer_cm is not None:
        try:
            await _checkpointer_cm.__aexit__(None, None, None)
            logger.info("Checkpointer closed")
        except Exception as e:
            logger.error(f"Error closing checkpointer: {e}", exc_info=True)
    
    _agent = None
    _checkpointer = None
    _checkpointer_cm = None
    logger.info("Agent cleaned up")


def create_app() -> web.Application:
    """Create and configure the aiohttp application.
    
    Returns:
        Configured web.Application with all routes and handlers
    """
    app = web.Application()
    
    # Register routes
    app.router.add_get('/health', health)
    app.router.add_get('/metadata', metadata)
    app.router.add_post('/invoke/init', init_conversation)
    app.router.add_post('/invoke/chat', chat)
    app.router.add_post('/invoke/chat/stream', chat_stream)
    
    # Register startup and cleanup handlers
    app.on_startup.append(lambda app: initialize_agent())
    app.on_cleanup.append(lambda app: cleanup_agent())
    
    logger.info("Application configured with routes")
    
    return app


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Run the Dapr service."""
    service_port = int(os.getenv("DAPR_SERVICE_PORT", "5001"))
    service_name = os.getenv("DAPR_SERVICE_NAME", "carqna-dapr")
    
    logger.info(f"Starting {service_name} on port {service_port}")
    
    app = create_app()
    runner = web.AppRunner(app)
    
    try:
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', service_port)
        await site.start()
        
        logger.info(f"✓ {service_name} ready on port {service_port}")
        logger.info("Available endpoints:")
        logger.info("  POST /invoke/chat - Chat with event trace")
        logger.info("  POST /invoke/chat/stream - Chat with SSE streaming")
        logger.info("  POST /invoke/init - Initialize conversation")
        logger.info("  GET /health - Health check")
        logger.info("  GET /metadata - Service metadata")
        
        # Keep running
        while True:
            await asyncio.sleep(3600)
    
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
