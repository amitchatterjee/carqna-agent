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
def _extract_command_final_text(output: Any) -> Optional[str]:
    """Extract clean final-answer text from a LangGraph Command update.

    Used both when a chain ends and when a tool (e.g. a subagent invoked via
    a task/delegation tool) returns a Command carrying the final assistant
    message directly, instead of surfacing it through an on_chain_end event.
    """
    raw_content = None

    # Track A: Handle native 'Command' objects directly
    if output.__class__.__name__ == 'Command' or hasattr(output, 'update'):
        update_dict = getattr(output, 'update', {}) or {}
        if isinstance(update_dict, dict) and 'messages' in update_dict:
            messages = update_dict.get('messages') or []
            if messages:
                raw_content = messages[-1].content

    # Track B: Standard dictionary fallback
    elif isinstance(output, dict) and 'messages' in output:
        messages = output.get('messages') or []
        if messages:
            raw_content = messages[-1].content

    # Track C: Handle a leaked string representation of a Command object
    elif isinstance(output, str) and "Command(update=" in output:
        try:
            import re
            match = re.search(r"'(?:text|content)':\s*[\"'](.*?)[\"']", output)
            if match:
                raw_content = match.group(1).encode().decode('unicode_escape')
        except Exception:
            pass

    if not raw_content:
        return None

    # Unpack the validated content structure
    clean_text_output = ""
    if isinstance(raw_content, list) and len(raw_content) > 0:
        first_item = raw_content
        if isinstance(first_item, dict):
            if first_item.get('type') == 'tool_use':
                return None
            clean_text_output = first_item.get('text', '')
        else:
            clean_text_output = str(first_item)

    # Check for stringified JSON arrays
    elif isinstance(raw_content, str) and raw_content.strip().startswith('['):
        try:
            parsed_list = json.loads(raw_content)
            if isinstance(parsed_list, list) and len(parsed_list) > 0:
                first_item = parsed_list
                if isinstance(first_item, dict):
                    if first_item.get('type') == 'tool_use':
                        return None
                    clean_text_output = first_item.get('text', '')
        except Exception:
            clean_text_output = raw_content
    elif isinstance(raw_content, str):
        clean_text_output = raw_content

    # Prevent tool routing updates from triggering a final response
    if "tool_use" in clean_text_output or "subagent_type" in clean_text_output:
        return None

    if clean_text_output and clean_text_output.strip() and not clean_text_output.startswith("Command(update="):
        return clean_text_output

    return None


def _format_graph_event(event: dict, step_counter: int):
    """How your code looked when it outputted the raw JSON strings."""
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
                step=step_counter,
                timestamp=timestamp,
                tool=tool_name,
                content=str(data.get('input', {}))
            )

        elif event_type == 'on_tool_end':
            data = event.get('data', {})
            tool_name = data.get('tool')
            if not tool_name:
                tool_name = event.get('metadata', {}).get('tool_name', 'unknown')
            output = data.get('output', '')

            # A tool (e.g. a subagent invoked via a task/delegation tool) may
            # return a Command carrying the final assistant message directly.
            # Classify that as the final response instead of a truncated,
            # generic tool result.
            final_text = _extract_command_final_text(output)
            if final_text:
                return GraphEvent(
                    type='final_response',
                    step=step_counter,
                    timestamp=timestamp,
                    tool=tool_name,
                    content=final_text
                )

            if isinstance(output, dict):
                output = json.dumps(output)
            elif not isinstance(output, str):
                output = str(output)
            return GraphEvent(
                type='tool_result',
                step=step_counter,
                timestamp=timestamp,
                tool=tool_name,
                content=output[:500]
            )

        elif event_type == 'on_llm_start':
            return GraphEvent(
                type='llm_thinking',
                step=step_counter,
                timestamp=timestamp,
                content='LLM processing...'
            )
            
        elif event_type == 'on_llm_end':
            data = event.get('data', {})
            output = data.get('output', {})
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
                    step=step_counter,
                    timestamp=timestamp,
                    content=content[:300]
                )
                
        elif event_type == 'on_chain_end':
            chain_name = event.get('name', '')
            
            # 1. Skip intermediate indexing tool steps
            if 'tool' in chain_name.lower() or 'opensearch' in chain_name.lower():
                return None

            data = event.get('data', {})
            output = data.get('output') or {}

            final_text = _extract_command_final_text(output)
            if final_text:
                logger.info(f"✨ Successfully isolated pure text payload length: {len(final_text)}")
                return GraphEvent(
                    type='final_response',
                    step=step_counter,
                    timestamp=timestamp,
                    tool=chain_name or 'LangGraph',
                    content=final_text
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
    try:
        # 1. Read the raw text body first instead of crashing on .json()
        body_text = await request.text()
        logger.info(f"Received raw initialization body text: '{body_text}'")

        # 2. Assign default values safely if the body is empty or blank
        if not body_text.strip():
            logger.warning("Received an empty initialization payload. Using fallback configurations.")
            data = {"user_id": "default_user"}
        else:
            data = json.loads(body_text)

        # 3. Process your existing conversation setup logic using 'data'
        user_id = data.get("user_id", "default_user")
        thread_id = data.get("thread_id") or str(uuid.uuid4())
        logger.info(f"Initializing active workspace conversation session for user: {user_id}")
        
        # Return a valid JSON back to the frontend so it satisfies your TypeScript fetch
        return web.json_response({"status": "initialized", 
                                  "user_id": user_id, 
                                  "thread_id": thread_id})

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse payload string: {e}")
        return web.json_response({"error": "Malformed JSON layout profile"}, status=400)
    except Exception as e:
        logger.error(f"Error initializing conversation: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


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
    """Chat endpoint with Server-Sent Events (SSE) streaming."""
    try:
        data = await request.json()

        if data.get("method") == "info" or request.path.endswith('/info'):
            logger.info("Intercepted CopilotKit discovery packet. Returning agent metadata.")
            return await copilot_info(request)

        chat_req = ChatRequest(**data)
        thread_id = chat_req.thread_id or str(uuid.uuid4())

        config = {"configurable": {"thread_id": thread_id}}
        logger.info(f"Stream chat request (thread: {thread_id}): {chat_req.message[:50]}...")

        # 1. Fetch the origin captured dynamically by your middleware
        origin = request.get('cors_origin', '*')

        # 2. Define ALL headers inside the StreamResponse initialization payload
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': origin,
                'Access-Control-Allow-Credentials': 'true',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS, PUT, DELETE',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization, x-copilotkit-runtime-client-version'
            }
        )
        
        await response.prepare(request)
        
        step_counter = 0
        try:
            # Stream events from graph execution
            async for event in _agent.astream_events(
                {"messages": [HumanMessage(content=chat_req.message)]}, 
                config=config, 
                version="v2"
            ):
                graph_event = _format_graph_event(event, step_counter)
                
                if graph_event:
                    # Serialize the whole GraphEvent object instance directly using model_dump
                    event_json = graph_event.model_dump()
                    
                    # Wrap in your custom 'data: ' prefix format string
                    sse_line = f"data: {json.dumps(event_json)}\n\n"
                    await response.write(sse_line.encode('utf-8'))
                    
                    step_counter += 1
                    logger.info(f"Piped graph event line {step_counter}: {graph_event.type}")
            
            # Send your standard done token message payload
            done_payload = {"type": "done", "step": step_counter, "timestamp": datetime.now().isoformat(), "tool": "", "content": ""}
            await response.write(f"data: {json.dumps(done_payload)}\n\n".encode('utf-8'))
            logger.info(f"Stream completed smoothly for thread: {thread_id}")
            
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
            {"error": str(e), "type": "validation_error"}, status=400
        )
    except Exception as e:
        logger.error(f"Error in stream endpoint: {e}", exc_info=True)
        return web.json_response(
            {"error": str(e), "type": "stream_error"}, status=500
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


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    """CORS middleware compatible with standard endpoints and streaming responses."""
    # 1. Dynamically read the incoming origin (Fixes the '*' wildcard crash with credentials)
    origin = request.headers.get('Origin', '*')
    
    # 2. Short-circuit browser OPTIONS preflight handshakes immediately
    if request.method == 'OPTIONS':
        return web.Response(
            status=200,
            headers={
                'Access-Control-Allow-Origin': origin,
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS, PUT, DELETE',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization, x-copilotkit-runtime-client-version',
                'Access-Control-Allow-Credentials': 'true',
            }
        )

    # 3. Store the origin on the request context so your streaming endpoints can access it
    request['cors_origin'] = origin

    # 4. Execute the underlying route handler
    response = await handler(request)

    # 5. Only modify headers if they haven't been sent to the network socket yet
    if not response.prepared:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, x-copilotkit-runtime-client-version'
        response.headers['Access-Control-Allow-Credentials'] = 'true'

    return response


async def copilot_info(request: web.Request) -> web.Response:
    """Returns agent configuration info to satisfy CopilotKit discovery."""
    origin = request.get('cors_origin', '*')
    data = {
        "agents": [
            {
                "id": "carqna-agent",
                "description": "CarQnA Assistant",
                "inputs": [],
                "outputs": []
            }
        ]
    }
    return web.json_response(
        data,
        headers={
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Credentials': 'true'
        }
    )

async def copilot_threads_handler(request: web.Request) -> web.Response:
    """Returns an empty thread history profile to clear CopilotKit session checks."""
    # CopilotKit expects a list array of active threads or an empty shell block
    data = {
        "threads": []
    }
    return web.json_response(data)

def create_app() -> web.Application:
    """Create and configure the aiohttp application.
    
    Returns:
        Configured web.Application with all routes and handlers
    """
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get('/invoke/info', copilot_info)   # <--- FIX: Catches the exact discovery request
    app.router.add_post('/invoke/info', copilot_info)

    # Add this route to catch CopilotKit's main direct endpoint traffic:
    app.router.add_post('/invoke/', chat_stream)
    app.router.add_get('/invoke/', chat_stream)

    # Register routes
    app.router.add_get('/health', health)
    app.router.add_get('/metadata', metadata)
    app.router.add_post('/init', init_conversation)
    app.router.add_get('/init', init_conversation)
    app.router.add_post('/invoke/init', init_conversation)

    app.router.add_post('/chat', chat)
    app.router.add_get('/chat', chat)
    app.router.add_post('/invoke/chat', chat)

    app.router.add_post('/chat/stream', chat_stream)
    app.router.add_get('/chat/stream', chat_stream)
    app.router.add_post('/invoke/chat/stream', chat_stream)

    app.router.add_post('/agent/chat/stream', chat_stream)

    app.router.add_post('/agent/init', init_conversation)
    app.router.add_get('/agent/init', init_conversation)

    app.router.add_get('/agent/threads', copilot_threads_handler)
    app.router.add_post('/agent/threads', copilot_threads_handler)

    app.router.add_post('/agent', chat_stream)
    app.router.add_get('/agent', chat_stream)
    
    # Register startup and cleanup handlers
    app.on_startup.append(lambda app: initialize_agent())
    app.on_cleanup.append(lambda app: cleanup_agent())
    
    logger.info("Application configured with routes")
    
    return app


# ============================================================================
# Main Entry Point
# ============================================================================

logging.basicConfig(level=logging.DEBUG)
http_logger = logging.getLogger("aiohttp.access")
http_logger.setLevel(logging.DEBUG)

async def main():
    """Run the Dapr service."""
    service_port = int(os.getenv("DAPR_SERVICE_PORT", "5001"))
    service_name = os.getenv("DAPR_SERVICE_NAME", "carqna-dapr")
    
    logger.info(f"Starting {service_name} on port {service_port}")
    
    app = create_app()
    runner = web.AppRunner(app,
        access_log=http_logger, 
        access_log_format='%a "%r" %s %b')
    
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
