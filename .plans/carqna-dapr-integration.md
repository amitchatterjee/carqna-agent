# CarQnA Dapr Integration Plan

**Objective:** Wrap the carqna agent into a Dapr module to serve a Copilot Kit-based UI.

**Status:** Phases 1-4 Complete ✅ | Phase 5-6 Pending

**Completed:**
- ✅ `src/agent/carqna_dapr.py` - Full HTTP service with both chat endpoints (batch + SSE)
- ✅ `pyproject.toml` - Dependencies added and installed (aiohttp, pydantic)
- ✅ `.env.example` & `.env` - Dapr configuration integrated
- ✅ Comprehensive documentation with examples and frontend integration patterns
- ✅ Service running and tested locally - all endpoints functional

**Next Steps:**
1. Test streaming endpoint (`/invoke/chat/stream`)
2. Write integration tests
3. Deploy with Dapr sidecar
4. Connect Copilot Kit UI

---

## 1. Dependencies to Add

Add to `pyproject.toml` under `[project] dependencies`:

```python
"aiohttp>=3.8.0",          # Async HTTP server for Dapr endpoints
"pydantic>=2.0.0",         # Data validation for request/response schemas
```

**Note:** `dapr-sdk` is NOT required because:
- Our service is a **plain HTTP service** that Dapr calls via its HTTP proxy
- Dapr runtime runs as a sidecar and invokes endpoints on port 5001
- We only need aiohttp for the HTTP server and Pydantic for request validation
- If in the future we need to call other Dapr services or use state stores from our service, we can add `dapr-client` at that time

---

## 2. New Module: `carqna_dapr.py` (✅ IMPLEMENTED)

**Location:** `src/agent/carqna_dapr.py`

**Purpose:** HTTP service wrapper that exposes carqna as Dapr-invokable endpoints with full event tracing.

### Key Components:

#### A. Data Models (Pydantic)

**ChatRequest:**
```python
{
  "message": str,               # User question (required)
  "thread_id": str,            # Conversation session ID (auto-generated if missing)
  "metadata": dict,            # Additional context (optional)
  "include_trace": bool        # Include event trace (default: true)
}
```

**ChatResponse:**
```python
{
  "response": str,             # Agent's answer
  "thread_id": str,            # Conversation thread ID
  "timestamp": str,            # ISO format response time
  "metadata": dict,            # Additional context
  "events": [GraphEvent],      # Full execution trace (if requested)
  "event_count": int           # Number of events captured
}
```

**GraphEvent (in trace):**
```python
{
  "type": str,                 # "tool_call" | "tool_result" | "llm_thinking" | "final_response" | "error"
  "step": int,                 # Step number in execution
  "timestamp": str,            # ISO format
  "tool": str,                 # Tool name (for tool events)
  "input": dict,               # Tool input (for tool_call)
  "output": str,               # Tool or LLM output (for tool_result)
  "content": str,              # Reasoning or response (for llm_thinking/final_response)
  "error": str                 # Error message (for error events)
}
```

**InitRequest:**
```python
{
  "user_id": str,              # Optional user identifier
  "session_name": str,         # Optional session name
  "metadata": dict             # Optional metadata
}
```

**InitResponse:**
```python
{
  "thread_id": str,            # New conversation thread ID
  "status": str,               # "initialized"
  "timestamp": str,            # ISO format creation time
  "metadata": dict             # Response metadata
}
```

#### B. aiohttp Web Application
- `create_app()` - Factory for aiohttp Application
- `initialize_agent()` - Startup handler (initializes graph and checkpointer)
- `cleanup_agent()` - Shutdown handler (closes database connections)
- `_format_graph_event()` - Converts LangGraph events to frontend-friendly format
- `_extract_response()` - Extracts final response from agent result

#### C. Endpoints

**POST /invoke/chat** (Batch with Event Trace)
- Request: `ChatRequest` (JSON)
- Response: `ChatResponse` (JSON) with optional `events` array
- Logic:
  1. Parse and validate request (Pydantic)
  2. Generate thread_id if not provided
  3. If `include_trace=true`:
     - Stream events via `astream_events(version="v2")`
     - Format each event and collect in array
  4. Call `agent.ainvoke()` to get final result
  5. Extract response text and return with full event trace
- Use Cases: Detailed debugging, inspection UI, replay conversations

**POST /invoke/chat/stream** (Server-Sent Events)
- Request: `ChatRequest` (JSON)
- Response: Server-Sent Events (text/event-stream)
- Logic:
  1. Parse and validate request (Pydantic)
  2. Generate thread_id if not provided
  3. Set up SSE response headers (Content-Type, Cache-Control)
  4. Stream events in real-time via `astream_events(version="v2")`
  5. Format and send each event as `data: {json}\n\n`
  6. Send final `done` marker with event count
  7. Handle cancellation and errors gracefully
- Use Cases: Real-time UI updates, live agent reasoning display, progressive disclosure

**POST /invoke/init** (Initialize Conversation)
- Request: `InitRequest` (JSON)
- Response: `InitResponse` (JSON)
- Logic:
  1. Parse and validate request
  2. Generate UUID for thread_id
  3. Optionally validate with MCP config
  4. Return initialized thread info with metadata
- Use Cases: Creating new conversation sessions from UI

**GET /health** (Health Check)
- Response: `{"status": "healthy", "service": "carqna-dapr", "timestamp": "..."}`
- Logic: Simple health check for Dapr liveness probe
- Use Cases: Kubernetes/Dapr container health monitoring

**GET /metadata** (Service Metadata)
- Response: Service metadata with list of available endpoints
  ```json
  {
    "name": "carqna-dapr",
    "version": "0.0.1",
    "description": "Automobile Help Assistant (CarQnA) via Dapr",
    "endpoints": [
      "POST /invoke/chat",
      "POST /invoke/chat/stream",
      "POST /invoke/init",
      "GET /health",
      "GET /metadata"
    ]
  }
  ```
- Use Cases: Service discovery, capability detection

#### D. Event Types and Formatting

| Event Type | Triggered By | Payload | Frontend Display |
|-----------|--------------|---------|------------------|
| `tool_call` | `on_tool_start` | tool name + input | "🔧 Calling [tool] with [input]..." |
| `tool_result` | `on_tool_end` | tool name + output (truncated) | "✓ [tool] returned [output]" |
| `llm_thinking` | `on_llm_start`/`on_llm_end` | reasoning content | "💭 [reasoning]..." |
| `final_response` | `on_chain_end` | final message content | Agent displays full response |
| `error` | Exception handler | error message + type | "❌ Error: [message]" |
| `done` | Stream completion | event count | Stream ended, X events processed |

#### E. Startup/Shutdown Lifecycle

**Startup (`initialize_agent`):**
1. Create AsyncSqliteSaver from connection string
2. Call `create_graph(checkpointer=...)` to initialize agent
3. Store global references: `_agent`, `_checkpointer`
4. Log success message

**Shutdown (`cleanup_agent`):**
1. Close AsyncSqliteSaver connection
2. Clear global references
3. Release resources

**App Factory (`create_app`):**
1. Create aiohttp.Application
2. Register routes with handlers
3. Attach startup/cleanup handlers
4. Return configured app

---

## 3. Example Requests and Responses

### Example 1: Chat with Event Trace (Batch)

**Request:**
```bash
curl -X POST http://localhost:5001/invoke/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is the price of a Toyota Prius 2025 model?",
    "thread_id": "user-123-session-1",
    "include_trace": true
  }'
```

**Response:**
```json
{
  "response": "Based on my research, the Toyota Prius 2025 model starts at approximately $28,500 for the base configuration...",
  "thread_id": "user-123-session-1",
  "timestamp": "2026-07-16T10:30:05.123Z",
  "metadata": {},
  "events": [
    {
      "type": "tool_call",
      "step": 1,
      "timestamp": "2026-07-16T10:30:00.100Z",
      "tool": "opensearch_lookup",
      "input": {"query": "Toyota Prius 2025 model price MSRP"}
    },
    {
      "type": "tool_result",
      "step": 2,
      "timestamp": "2026-07-16T10:30:01.500Z",
      "tool": "opensearch_lookup",
      "output": "Found 8 listings: Toyota Prius 2025 model base price $28,500, $30,200 with hybrid plus, ..."
    },
    {
      "type": "llm_thinking",
      "step": 3,
      "timestamp": "2026-07-16T10:30:02.000Z",
      "content": "The search returned recent hybrid vehicle pricing data. I should now check for insurance information on eco-friendly vehicles..."
    },
    {
      "type": "tool_call",
      "step": 4,
      "timestamp": "2026-07-16T10:30:02.100Z",
      "tool": "filesystem_read",
      "input": {"path": "/insurance-faqs.md"}
    },
    {
      "type": "tool_result",
      "step": 5,
      "timestamp": "2026-07-16T10:30:02.800Z",
      "tool": "filesystem_read",
      "output": "Toyota Prius hybrid vehicles typically have lower insurance costs due to fuel efficiency and safety ratings..."
    },
    {
      "type": "final_response",
      "step": 6,
      "timestamp": "2026-07-16T10:30:04.500Z",
      "content": "Based on my research, the Toyota Prius 2025 model starts at approximately $28,500..."
    }
  ],
  "event_count": 6
}
```

### Example 2: Chat with SSE Streaming

**Request:**
```bash
curl -X POST http://localhost:5001/invoke/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the price of a Toyota Prius 2025 model?", "thread_id": "user-123-session-2"}'
```

**Response (Server-Sent Events):**
```
data: {"type": "tool_call", "step": 1, "timestamp": "2026-07-16T10:30:00.100Z", "tool": "opensearch_lookup", "input": {"query": "Toyota Prius 2025 model price"}}

data: {"type": "tool_result", "step": 2, "timestamp": "2026-07-16T10:30:01.500Z", "tool": "opensearch_lookup", "output": "Found 8 listings: Toyota Prius 2025 model base price $28,500..."}

data: {"type": "llm_thinking", "step": 3, "timestamp": "2026-07-16T10:30:02.000Z", "content": "The search returned hybrid vehicle pricing data..."}

data: {"type": "tool_call", "step": 4, "timestamp": "2026-07-16T10:30:02.100Z", "tool": "filesystem_read", "input": {"path": "/insurance-faqs.md"}}

data: {"type": "tool_result", "step": 5, "timestamp": "2026-07-16T10:30:02.800Z", "tool": "filesystem_read", "output": "Toyota Prius hybrid vehicle insurance costs..."}

data: {"type": "final_response", "step": 6, "timestamp": "2026-07-16T10:30:04.500Z", "content": "Based on my research..."}

data: {"type": "done", "event_count": 6}
```

### Example 3: Initialize Conversation

**Request:**
```bash
curl -X POST http://localhost:5001/invoke/init \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "session_name": "Tesla pricing inquiry",
    "metadata": {"source": "copilot-kit-ui"}
  }'
```

**Response:**
```json
{
  "thread_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "initialized",
  "timestamp": "2026-07-16T10:30:00.000Z",
  "metadata": {
    "user_id": "user-123",
    "session_name": "Tesla pricing inquiry",
    "source": "copilot-kit-ui"
  }
}
```

### Example 4: Health Check

**Request:**
```bash
curl -X GET http://localhost:5001/health
```

**Response:**
```json
{
  "status": "healthy",
  "service": "carqna-dapr",
  "timestamp": "2026-07-16T10:30:00.123Z"
}
```

---

## 4. Frontend Integration Examples

### Option A: Real-Time Streaming (React with Copilot Kit)

```typescript
async function streamChatResponse(message: string, threadId?: string) {
  const response = await fetch('http://localhost:5001/invoke/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      thread_id: threadId,
    })
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const lines = decoder.decode(value).split('\n');
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const event = JSON.parse(line.slice(6));
        
        if (event.type === 'done') {
          // Stream complete
          console.log(`Completed with ${event.event_count} events`);
          break;
        }

        // Render event in UI
        displayEvent(event);
      }
    }
  }
}

function displayEvent(event: GraphEvent) {
  switch (event.type) {
    case 'tool_call':
      console.log(`🔧 Calling ${event.tool} with:`, event.input);
      break;
    case 'tool_result':
      console.log(`✓ ${event.tool} returned:`, event.output);
      break;
    case 'llm_thinking':
      console.log(`💭 Reasoning: ${event.content}`);
      break;
    case 'final_response':
      console.log(`📝 Response: ${event.content}`);
      break;
    case 'error':
      console.error(`❌ Error: ${event.error}`);
  }
}
```

### Option B: Batch Response with Trace (Expandable UI)

```typescript
async function getChatWithTrace(message: string, threadId?: string) {
  const response = await fetch('http://localhost:5001/invoke/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      thread_id: threadId,
      include_trace: true
    })
  });

  const data = await response.json();

  // Display main response immediately
  copilotKit.appendMessage({
    role: 'assistant',
    content: data.response
  });

  // Store trace for expandable details
  return {
    response: data.response,
    threadId: data.thread_id,
    trace: data.events,
    eventCount: data.event_count
  };
}

// In Copilot Kit action
const chatResult = await getChatWithTrace(userQuery, sessionId);

// Optionally show trace in expandable section
<details>
  <summary>Show execution trace ({chatResult.eventCount} steps)</summary>
  <pre>{JSON.stringify(chatResult.trace, null, 2)}</pre>
</details>
```

### Option C: Hybrid (Progress + Expandable Trace)

```typescript
async function hybridChat(message: string, threadId?: string) {
  // Use streaming for real-time progress
  const progressDiv = document.getElementById('progress');
  const traceEvents = [];
  
  const response = await fetch('http://localhost:5001/invoke/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, thread_id: threadId })
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const lines = decoder.decode(value).split('\n');
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const event = JSON.parse(line.slice(6));
        
        if (event.type !== 'done') {
          traceEvents.push(event);
          
          // Show progress
          if (event.type === 'tool_call') {
            progressDiv.innerHTML = `<em>Calling ${event.tool}...</em>`;
          } else if (event.type === 'llm_thinking') {
            progressDiv.innerHTML = `<em>Thinking...</em>`;
          }
        }
      }
    }
  }

  // Also get batch response for cleanest final output
  const batchResponse = await fetch('http://localhost:5001/invoke/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, thread_id: threadId, include_trace: true })
  });

  const data = await batchResponse.json();

  copilotKit.appendMessage({
    role: 'assistant',
    content: data.response,
    metadata: { trace: data.events, threadId: data.thread_id }
  });
}
```

---

## 3. Environment Configuration

### `.env` additions:

```bash
# Dapr Configuration
DAPR_SERVICE_NAME=carqna-dapr
DAPR_SERVICE_PORT=5001
DAPR_RUNTIME_PORT=3500

# Carqna Graph Configuration (existing, ensure present)
CHECKPOINT_DB_PATH=./.db.sqlite3
PROMPTS_DIR=.
INSURANCE_DOCS_ROOT=~/git/knowledgexpert/data/linux-exec/insurance-docs
MCP_CONFIG_PATH=~/.knowledgexpert/conf/mcp/config.json
LLM_MODEL=claude-sonnet-4-5-20250929

# Optional Dapr State Store (for persistence)
DAPR_STATE_STORE=statestore
```

---

## 4. Integration with Existing carqna Code

### Changes to `carqna_dapr.py`:
- Import and reuse `create_graph()` from `agent/graph.py`
- Initialize graph once at server startup
- Maintain single agent instance with checkpointer for state persistence

### No changes needed to:
- `src/agent/carqna.py` - Keep as-is (local CLI runner)
- `src/agent/graph.py` - Keep as-is (core agent logic)

---

## 5. Dapr Sidecar Configuration

### Option A: Local Development (docker-compose)
Create/update `docker-compose.yml` in infrastructure/docker/:

```yaml
services:
  carqna:
    build:
      context: .
      dockerfile: ../knowledgexpert-base/Dockerfile  # Reuse or create new
    ports:
      - "5001:5001"  # Service port
    environment:
      - DAPR_SERVICE_PORT=5001
      - CHECKPOINT_DB_PATH=./.db.sqlite3
      - PROMPTS_DIR=.
    volumes:
      - ~/.knowledgexpert:/root/.knowledgexpert  # MCP config
      - ~/git/knowledgexpert/data:/data:ro       # Insurance docs
    networks:
      - dapr-network

  carqna-dapr:
    image: daprio/daprd:latest
    command:
      - ./daprd
      - -app-id=carqna-dapr
      - -app-port=5001
      - -dapr-http-port=3500
    ports:
      - "3500:3500"  # Dapr runtime port
    depends_on:
      - carqna
    networks:
      - dapr-network

networks:
  dapr-network:
    driver: bridge
```

### Option B: Kubernetes (dapr-config.yaml)
```yaml
apiVersion: dapr.io/v1alpha1
kind: Configuration
metadata:
  name: carqna-config
spec:
  mtls:
    enabled: false
  features:
    - name: ServiceInvocation
      enabled: true
---
apiVersion: v1
kind: Pod
metadata:
  name: carqna
  annotations:
    dapr.io/enabled: "true"
    dapr.io/app-id: "carqna-dapr"
    dapr.io/app-port: "5001"
    dapr.io/config: "carqna-config"
spec:
  containers:
  - name: carqna
    image: carqna:latest
    ports:
    - containerPort: 5001
```

---

## 6. Copilot Kit UI Integration

The Copilot Kit frontend calls the Dapr service via:

```typescript
// Example in Copilot Kit action
const response = await daprClient.invoker.invoke(
  "carqna-dapr",
  "invoke/chat",
  "POST",
  {
    message: userQuery,
    thread_id: sessionId,
    metadata: { source: "copilot-kit-ui" }
  }
);
```

Or if using Dapr HTTP proxy:

```bash
curl -X POST http://localhost:3500/v1.0/invoke/carqna-dapr/method/invoke/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the price of a Tesla?", "thread_id": "user-123"}'
```

---

## 7. State Management Strategy

### Option A: SQLite Checkpointer (current)
- ✅ Maintain existing SQLite checkpointer from `graph.py`
- ✅ Conversation state stored locally in `.db.sqlite3`
- ✅ Thread ID passed by UI to resume conversations

### Option B: Dapr State Store (future enhancement)
- Optional: Use Dapr state store for distributed persistence
- Key: `{thread_id}:state`
- Value: Serialized conversation history
- Would require updating `create_graph()` to accept Dapr state backend

---

## 11. Implementation Steps

### Phase 1: Foundation ✅ COMPLETE
1. [x] Add dependencies to `pyproject.toml` (aiohttp, pydantic - dapr-sdk not needed)
2. [x] Create `src/agent/carqna_dapr.py` with:
   - [x] Pydantic models (ChatRequest, ChatResponse, GraphEvent, etc.)
   - [x] aiohttp app setup with startup/cleanup handlers
   - [x] `/health`, `/metadata` endpoints
   - [x] Event formatting and extraction helpers
3. [x] Update `.env.example` with Dapr configuration
4. [x] Update `.env` with Dapr configuration

### Phase 2: Core Endpoints ✅ IMPLEMENTED
5. [x] Implement `/invoke/init` endpoint
6. [x] Implement `/invoke/chat` endpoint with optional trace
7. [x] Implement `/invoke/chat/stream` endpoint (SSE)
8. [x] Add request validation and error handling
9. [x] Full Pydantic validation for all request/response types

### Phase 4: Local Testing & Validation ✅ COMPLETE
14. [x] Test locally: `python -m agent.carqna_dapr`
15. [x] Test batch endpoint: `POST /invoke/chat` with event trace
16. [x] Fixed AsyncSqliteSaver lifecycle issue (context manager handling)
17. [ ] Test streaming endpoint: `POST /invoke/chat/stream` with SSE
18. [ ] Test multi-turn conversations with same thread_id
19. [ ] Write unit tests for event formatting
20. [ ] Write integration tests in `tests/integration_tests/test_carqna_dapr.py`
18. [ ] Create Dockerfile for carqna service (if needed)
19. [ ] Update docker-compose.yml with Dapr sidecar
20. [ ] Test with `docker-compose up`
21. [ ] Verify service invocation via Dapr runtime port (3500)

### Phase 5: Copilot Kit Integration
22. [ ] Connect Copilot Kit UI to `/invoke/chat/stream` endpoint (real-time)
23. [ ] Validate response format and timing
24. [ ] Implement UI state management with `thread_id`
25. [ ] Add expandable trace UI for batch responses

### Phase 6: Polish & Production
26. [ ] Load test with concurrent requests
27. [ ] Add rate limiting to Dapr config
28. [ ] Implement request/response logging
29. [ ] Create monitoring dashboard for event types

---

## 12. File Structure After Implementation

```
carqna-agent/
├── .plans/
│   └── carqna-dapr-integration.md          # ✅ This plan (CREATED)
├── src/
│   └── agent/
│       ├── carqna.py                       # Local CLI (unchanged)
│       ├── carqna_dapr.py                  # ✅ Dapr HTTP wrapper (CREATED)
│       └── graph.py                        # Core agent (unchanged)
├── infrastructure/
│   └── docker/
│       └── docker-compose.yml              # To be updated with Dapr sidecar
├── tests/
│   └── integration_tests/
│       └── test_carqna_dapr.py             # To be created
├── .env.example                            # ✅ Updated with Dapr config
├── .env                                    # ✅ Updated with Dapr config
└── pyproject.toml                          # ✅ Updated with aiohttp, pydantic (UPDATED)
```

---

## 10. Testing Strategy

### Unit Tests
- Request/response model validation
- Error handling for invalid inputs
- Thread ID generation

### Integration Tests
- `/health` endpoint accessibility
- `/invoke/init` returns valid thread_id
- `/invoke/chat` with valid request returns response
- Conversation continuity across multiple requests (same thread_id)
- MCP tool invocation through agent

### Dapr Runtime Tests
- Service discovery and registration
- Service invocation through Dapr port (3500)
- Metadata endpoint visible to Dapr

---

## 11. Error Handling

### Expected Errors to Handle
- **400 Bad Request:** Invalid request format, missing required fields
- **404 Not Found:** Unknown thread_id (create new or error?)
- **500 Internal Server Error:** Agent crash, MCP connection lost
- **503 Service Unavailable:** Graph not initialized, MCP tools unavailable

### Logging
- All requests logged with thread_id and timestamp
- Agent errors logged with stack trace
- MCP tool invocations logged for debugging

---

## 12. Performance Considerations

- **Connection Pooling:** Reuse MCP client (already done in `graph.py`)
- **Concurrency:** aiohttp handles multiple concurrent requests
- **Streaming:** Agent responses could be streamed via Server-Sent Events (SSE) for faster UI updates
- **Caching:** Consider caching MCP tool results if appropriate

---

## 13. Security Considerations

- **Input Validation:** Pydantic validates all request fields
- **Thread ID Isolation:** Ensure users can only access their own thread IDs
- **API Authentication:** Add API key or OAuth layer if exposing to external Copilot Kit
- **Rate Limiting:** Consider Dapr rate limiting policies
- **Secrets Management:** MCP config and API keys via environment variables

---

## 15. Next Steps

1. Review this plan with team
2. Validate Dapr and aiohttp dependency versions against current stack
3. Clarify Copilot Kit integration requirements
4. Decide on state management strategy (SQLite vs Dapr state store)
5. Begin Phase 3 implementation (testing and local validation)

---

## 16. Quick Start (Local Development)

### Prerequisites
- Python 3.10+
- Dependencies installed: `pip install -e .`
- MCP config available at `~/.knowledgexpert/conf/mcp/config.json`

### Run Locally (Without Dapr sidecar)

```bash
# 1. Copy environment template
cp .env.dapr.example .env

# 2. Update .env with your paths (adjust paths as needed):
#    - INSURANCE_DOCS_ROOT
#    - MCP_CONFIG_PATH
#    - CHECKPOINT_DB_PATH

# 3. Start the service
python -m agent.carqna_dapr

# Output:
# Starting carqna-dapr on port 5001
# ✓ carqna-dapr ready on port 5001
# Available endpoints:
#   POST /invoke/chat - Chat with event trace
#   POST /invoke/chat/stream - Chat with SSE streaming
#   POST /invoke/init - Initialize conversation
#   GET /health - Health check
#   GET /metadata - Service metadata
```

### Test Endpoints

**Easy Copy-Paste Version (with env variable):**

```bash
# Set the thread ID as an environment variable for easy reuse
THREAD_ID="<your-thread-id>"

# Test 1: Chat with event trace (batch)
curl -X POST http://localhost:5001/invoke/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the price of a Toyota Prius 2025 model?","thread_id":"$THREAD_ID","include_trace":true}' | jq .

# Test 2: Chat with SSE streaming (real-time events)
curl -X POST http://localhost:5001/invoke/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the insurance for a Toyota Prius hybrid vehicle?","thread_id":"$THREAD_ID"}'

# Test 3: Get metadata
curl -X GET http://localhost:5001/metadata | jq .

# Test 4: Create a new conversation
curl -X POST http://localhost:5001/invoke/init \
  -H "Content-Type: application/json" \
  -d '{"user_id":"new-user"}' | jq .

# Test 5: Health check
curl -X GET http://localhost:5001/health | jq .
```

**Individual Tests:**

**1. Health check:**
```bash
curl -X GET http://localhost:5001/health
# Expected: {"status": "healthy", "service": "carqna-dapr", "timestamp": "..."}
```

**2. Get metadata:**
```bash
curl -X GET http://localhost:5001/metadata
# Expected: Service info and endpoint list
```

**3. Initialize conversation:**
```bash
curl -X POST http://localhost:5001/invoke/init \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test-user"}'
# Expected: {"thread_id": "550e8400-...", "status": "initialized", ...}
```

**4. Chat with trace (batch):**
```bash
# Copy thread_id from init response above
curl -X POST http://localhost:5001/invoke/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is the price of a Toyota Prius 2025 model?",
    "thread_id": "550e8400-...",
    "include_trace": true
  }'
# Expected: Response with events array containing tool calls and reasoning
```

**5. Chat with streaming (SSE):**
```bash
# Use thread_id from above
curl -X POST http://localhost:5001/invoke/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is the price of a Toyota Prius 2025 model?",
    "thread_id": "550e8400-..."
  }'
# Expected: Stream of SSE events (data: {...}\n\n)
```

### Debugging

Enable debug logging:
```bash
export LOG_LEVEL=DEBUG
python -m agent.carqna_dapr
```

Monitor events in real-time using jq (for pretty JSON):
```bash
curl -X POST http://localhost:5001/invoke/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the price of a Toyota Prius 2025 model?"}' | grep "^data:" | sed 's/^data: //' | jq .
```

### Common Issues

1. **MCP config not found:** Verify `~/.knowledgexpert/conf/mcp/config.json` exists
2. **Insurance docs not found:** Check `INSURANCE_DOCS_ROOT` path in `.env`
3. **Database locked:** Remove `.db.sqlite3` and restart (will create fresh)
4. **Port 5001 already in use:** Change `DAPR_SERVICE_PORT` in `.env`

---

## 14. Next Steps
