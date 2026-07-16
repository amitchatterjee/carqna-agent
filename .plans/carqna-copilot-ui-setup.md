# CarQnA Copilot Kit UI Setup Plan

**Objective:** Create a standalone React + Copilot Kit web UI for the carqna-dapr service.

**Status:** Ready for Implementation | Phase 1-2 Pending

**Companion To:**
- [carqna-dapr-integration.md](./carqna-dapr-integration.md) - Backend HTTP service (Phases 1-4 Complete ✅)

**Frontend Scope:**
- ✅ Standalone React app with Copilot Kit integration
- ✅ Real-time streaming chat via SSE
- ✅ Session management with thread_id
- ✅ Event trace visualization
- ⏳ Deployment ready (Next.js or Vite)

---

## 1. Project Setup

### Prerequisites
- Node.js 18+
- npm or yarn

### Create Project (Choose One)

**Option A: Vite + React (Recommended for lighter UI)**
```bash
cd /home/amit/git
npm create vite@latest carqna-copilot-ui -- --template react-ts
cd carqna-copilot-ui
npm install
```

**Option B: Next.js (Recommended for full-stack features)**
```bash
cd /home/amit/git
npx create-next-app@latest carqna-copilot-ui --typescript --tailwind
cd carqna-copilot-ui
```

### Install Dependencies
```bash
npm install @copilotkit/react-core @copilotkit/react-ui
npm install @copilotkit/sdk-js
npm install -D tailwindcss postcss autoprefixer  # If not included
```

---

## 2. Type Definitions

**File:** `src/types/carqna.ts`

```typescript
export interface GraphEvent {
  type: "tool_call" | "tool_result" | "llm_thinking" | "final_response" | "error" | "done";
  step: number;
  timestamp: string;
  tool?: string;
  input?: Record<string, unknown>;
  output?: string;
  content?: string;
  error?: string;
  event_count?: number;
}

export interface ChatResponse {
  response: string;
  thread_id: string;
  timestamp: string;
  metadata: Record<string, unknown>;
  events: GraphEvent[];
  event_count: number;
}

export interface InitResponse {
  thread_id: string;
  status: string;
  timestamp: string;
  metadata: Record<string, unknown>;
}

export interface ChatSession {
  threadId: string;
  userId: string;
  sessionName?: string;
  startedAt: string;
}
```

---

## 3. Backend Service Layer

**File:** `src/services/carqnaService.ts`

```typescript
import type { GraphEvent, InitResponse, ChatResponse } from "../types/carqna";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:5001";

export async function initConversation(userId: string): Promise<InitResponse> {
  const res = await fetch(`${API_BASE}/invoke/init`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId })
  });

  if (!res.ok) throw new Error(`Init failed: ${res.statusText}`);
  return res.json();
}

export async function streamChat(
  message: string,
  threadId: string,
  onEvent: (event: GraphEvent) => void
): Promise<void> {
  const res = await fetch(`${API_BASE}/invoke/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, thread_id: threadId })
  });

  if (!res.ok) throw new Error(`Stream failed: ${res.statusText}`);

  const reader = res.body?.getReader();
  const decoder = new TextDecoder();

  if (!reader) throw new Error("No response body");

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const lines = decoder.decode(value).split("\n");
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const event = JSON.parse(line.slice(6));
          onEvent(event);
        } catch {
          // Skip malformed lines
        }
      }
    }
  }
}

export async function getHealth(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/health`);
  return res.json();
}
```

---

## 4. Custom Hooks

**File:** `src/hooks/useCarqnaChat.ts`

```typescript
import { useState, useCallback } from "react";
import { streamChat, initConversation } from "../services/carqnaService";
import type { GraphEvent } from "../types/carqna";

export function useCarqnaChat() {
  const [threadId, setThreadId] = useState<string>("");
  const [isLoading, setIsLoading] = useState(false);
  const [events, setEvents] = useState<GraphEvent[]>([]);
  const [error, setError] = useState<string>("");

  const startSession = useCallback(async (userId: string) => {
    try {
      setIsLoading(true);
      const { thread_id } = await initConversation(userId);
      setThreadId(thread_id);
      setEvents([]);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to init session");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const sendMessage = useCallback(
    async (message: string) => {
      if (!threadId) {
        setError("No active session");
        return;
      }

      try {
        setIsLoading(true);
        setError("");
        setEvents([]);

        await streamChat(message, threadId, (event) => {
          setEvents((prev) => [...prev, event]);
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to send message");
      } finally {
        setIsLoading(false);
      }
    },
    [threadId]
  );

  return {
    threadId,
    isLoading,
    events,
    error,
    startSession,
    sendMessage
  };
}
```

---

## 5. UI Components

### EventTraceViewer Component

**File:** `src/components/EventTraceViewer.tsx`

```typescript
import type { GraphEvent } from "../types/carqna";

interface EventTraceViewerProps {
  events: GraphEvent[];
}

export function EventTraceViewer({ events }: EventTraceViewerProps) {
  const finalEvent = events.find((e) => e.type === "final_response");
  const toolEvents = events.filter((e) => e.type === "tool_call" || e.type === "tool_result");
  const thinkingEvents = events.filter((e) => e.type === "llm_thinking");

  return (
    <div className="space-y-4 bg-gray-50 p-4 rounded-lg">
      {finalEvent && (
        <div className="bg-white p-3 rounded border-l-4 border-green-500">
          <p className="text-sm text-gray-600">Response</p>
          <p className="text-base text-gray-900">{finalEvent.content}</p>
        </div>
      )}

      {toolEvents.length > 0 && (
        <details className="bg-white p-3 rounded border-l-4 border-blue-500">
          <summary className="font-semibold cursor-pointer text-blue-900">
            Tools Used ({toolEvents.length})
          </summary>
          <div className="mt-2 space-y-2">
            {toolEvents.map((event, i) => (
              <div key={i} className="text-xs font-mono bg-gray-100 p-2 rounded">
                <strong>{event.tool}:</strong> {event.type === "tool_call" ? "called" : "returned"}
                {event.input && <pre>{JSON.stringify(event.input, null, 2)}</pre>}
                {event.output && <p className="text-gray-700">{event.output.slice(0, 200)}...</p>}
              </div>
            ))}
          </div>
        </details>
      )}

      {thinkingEvents.length > 0 && (
        <details className="bg-white p-3 rounded border-l-4 border-purple-500">
          <summary className="font-semibold cursor-pointer text-purple-900">
            Agent Reasoning ({thinkingEvents.length})
          </summary>
          <div className="mt-2 space-y-2">
            {thinkingEvents.map((event, i) => (
              <div key={i} className="text-xs bg-gray-100 p-2 rounded italic text-gray-700">
                💭 {event.content}
              </div>
            ))}
          </div>
        </details>
      )}

      <details className="bg-white p-3 rounded border-l-4 border-gray-400">
        <summary className="font-semibold cursor-pointer text-gray-700">
          Full Trace ({events.length} events)
        </summary>
        <pre className="mt-2 text-xs overflow-auto bg-gray-100 p-2 rounded">
          {JSON.stringify(events, null, 2)}
        </pre>
      </details>
    </div>
  );
}
```

### ChatInterface Component

**File:** `src/components/ChatInterface.tsx`

```typescript
import { useState } from "react";
import { useCarqnaChat } from "../hooks/useCarqnaChat";
import { EventTraceViewer } from "./EventTraceViewer";

export function ChatInterface() {
  const { threadId, isLoading, events, error, startSession, sendMessage } = useCarqnaChat();
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState<Array<{ role: "user" | "assistant"; content: string }>>([]);
  const [sessionStarted, setSessionStarted] = useState(false);

  const handleStartSession = async () => {
    await startSession("user-123");
    setSessionStarted(true);
    setMessages([]);
  };

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!message.trim()) return;

    setMessages((prev) => [...prev, { role: "user", content: message }]);
    setMessage("");

    await sendMessage(message);

    // Extract final response from events
    const finalEvent = events.find((e) => e.type === "final_response");
    if (finalEvent?.content) {
      setMessages((prev) => [...prev, { role: "assistant", content: finalEvent.content }]);
    }
  };

  return (
    <div className="flex flex-col h-screen bg-gray-100">
      {/* Header */}
      <div className="bg-white border-b p-4">
        <h1 className="text-2xl font-bold text-gray-900">CarQnA Assistant</h1>
        <p className="text-sm text-gray-600">
          {sessionStarted ? `Session: ${threadId?.slice(0, 8)}...` : "Ready to start"}
        </p>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {!sessionStarted ? (
          <div className="flex items-center justify-center h-full">
            <button
              onClick={handleStartSession}
              disabled={isLoading}
              className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-400"
            >
              {isLoading ? "Starting..." : "Start New Session"}
            </button>
          </div>
        ) : (
          <>
            {messages.map((msg, i) => (
              <div
                key={i}
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-xs lg:max-w-md xl:max-w-lg px-4 py-2 rounded-lg ${
                    msg.role === "user"
                      ? "bg-blue-600 text-white"
                      : "bg-gray-200 text-gray-900"
                  }`}
                >
                  {msg.content}
                </div>
              </div>
            ))}

            {isLoading && (
              <div className="flex justify-start">
                <div className="bg-gray-200 text-gray-900 px-4 py-2 rounded-lg animate-pulse">
                  Thinking...
                </div>
              </div>
            )}

            {events.length > 0 && <EventTraceViewer events={events} />}

            {error && (
              <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded">
                {error}
              </div>
            )}
          </>
        )}
      </div>

      {/* Input Footer */}
      {sessionStarted && (
        <div className="bg-white border-t p-4">
          <form onSubmit={handleSendMessage} className="flex gap-2">
            <input
              type="text"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="Ask about cars, insurance, pricing..."
              disabled={isLoading}
              className="flex-1 px-4 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
            />
            <button
              type="submit"
              disabled={isLoading || !message.trim()}
              className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-400"
            >
              {isLoading ? "..." : "Send"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
```

---

## 6. Main App Component

**File:** `src/App.tsx` (Vite) or `src/app/page.tsx` (Next.js)

### Vite Version:
```typescript
import { CopilotKit } from "@copilotkit/react-core";
import "@copilotkit/react-ui/styles.css";
import { ChatInterface } from "./components/ChatInterface";
import "./App.css";

export default function App() {
  return (
    <CopilotKit publicApiKey="">
      <ChatInterface />
    </CopilotKit>
  );
}
```

### Next.js Version:
```typescript
"use client";

import { CopilotKit } from "@copilotkit/react-core";
import "@copilotkit/react-ui/styles.css";
import { ChatInterface } from "@/components/ChatInterface";

export default function Home() {
  return (
    <CopilotKit publicApiKey="">
      <ChatInterface />
    </CopilotKit>
  );
}
```

---

## 7. Environment Configuration

**File:** `.env.local`

```bash
# Backend API
VITE_API_BASE=http://localhost:5001

# Copilot Kit (optional, for production)
VITE_COPILOT_KIT_PUBLIC_API_KEY=

# App Config
VITE_APP_NAME=CarQnA Assistant
VITE_DEFAULT_USER_ID=user-123
```

---

## 8. CSS Styling (Optional)

**File:** `src/App.css` (or use Tailwind directly)

```css
/* Basic styling if not using Tailwind */
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Roboto", "Oxygen",
    "Ubuntu", "Cantarell", "Fira Sans", "Droid Sans", "Helvetica Neue",
    sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

* {
  box-sizing: border-box;
}
```

---

## 9. Build Scripts

**File:** `package.json` (add/update scripts)

### Vite:
```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview",
    "lint": "eslint src --ext ts,tsx"
  }
}
```

### Next.js:
```json
{
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "lint": "next lint"
  }
}
```

---

## 10. File Structure

```
carqna-copilot-ui/
├── src/
│   ├── components/
│   │   ├── ChatInterface.tsx          # Main chat UI
│   │   └── EventTraceViewer.tsx       # Event visualization
│   ├── hooks/
│   │   └── useCarqnaChat.ts          # Chat logic hook
│   ├── services/
│   │   └── carqnaService.ts          # Backend API calls
│   ├── types/
│   │   └── carqna.ts                 # TypeScript types
│   ├── App.tsx                        # Main component (Vite)
│   ├── main.tsx                       # Entry point (Vite)
│   └── App.css
├── .env.local                         # Environment config
├── package.json
├── tsconfig.json
├── vite.config.ts                     # (Vite only)
└── README.md
```

---

## 11. Development Workflow

### Terminal 1: Start Backend
```bash
cd /home/amit/git/test-langgraph
python -m agent.carqna_dapr
# Output: ✓ carqna-dapr ready on port 5001
```

### Terminal 2: Start Frontend
```bash
cd /home/amit/git/carqna-copilot-ui
npm run dev
# Output: VITE v5.x.x ready in XXX ms
#         ➜  Local:   http://localhost:5173/
```

### Test the UI
1. Open http://localhost:5173
2. Click "Start New Session"
3. Type: "What is the price of a Toyota Prius 2025?"
4. Watch real-time events stream in
5. Expand trace to see tool calls and reasoning

---

## 12. Deployment Options

### Option A: Vercel (Next.js)
```bash
npm install -g vercel
vercel
```

Environment variables:
```
NEXT_PUBLIC_API_BASE=https://carqna-dapr.example.com
```

### Option B: Docker (Vite)
**Dockerfile:**
```dockerfile
FROM node:18-alpine as build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:18-alpine
RUN npm install -g serve
WORKDIR /app
COPY --from=build /app/dist ./dist
EXPOSE 5173
CMD ["serve", "-s", "dist", "-l", "5173"]
```

**docker-compose.yml:**
```yaml
version: "3.9"
services:
  carqna-ui:
    build: .
    ports:
      - "5173:5173"
    environment:
      - VITE_API_BASE=http://carqna-dapr:5001
```

---

## 13. Implementation Steps

### Phase 1: Project Setup ⏳ TODO
1. [ ] Create Vite or Next.js project
2. [ ] Install dependencies
3. [ ] Set up `.env.local`
4. [ ] Create folder structure

### Phase 2: Core Components ⏳ TODO
5. [ ] Create type definitions (`types/carqna.ts`)
6. [ ] Implement backend service (`services/carqnaService.ts`)
7. [ ] Create custom hook (`hooks/useCarqnaChat.ts`)
8. [ ] Build ChatInterface component
9. [ ] Build EventTraceViewer component

### Phase 3: App Setup ⏳ TODO
10. [ ] Create main App component
11. [ ] Integrate Copilot Kit provider
12. [ ] Set up styling (Tailwind or CSS)
13. [ ] Configure environment variables

### Phase 4: Testing & Polish ⏳ TODO
14. [ ] Test streaming chat with running backend
15. [ ] Verify event trace display
16. [ ] Test session persistence with thread_id
17. [ ] Add error handling and loading states
18. [ ] Test with multiple concurrent sessions

### Phase 5: Deployment ⏳ TODO
19. [ ] Build optimized bundle
20. [ ] Test production build locally
21. [ ] Configure deployment target (Vercel/Docker)
22. [ ] Set up backend URL for production

---

## 14. Testing Checklist

### Local Development
- [ ] Backend service running on port 5001
- [ ] Frontend running on port 5173 (or 3000 for Next.js)
- [ ] Session creation works
- [ ] Single message sends and displays response
- [ ] Event trace shows all tool calls
- [ ] Multi-turn conversations maintain thread_id
- [ ] Expandable trace sections work

### Integration
- [ ] Backend and frontend in same docker-compose
- [ ] CORS properly configured (if needed)
- [ ] Environment variables load correctly
- [ ] API Base URL configurable

---

## 15. Troubleshooting

### "Failed to fetch from http://localhost:5001"
- Verify backend is running: `curl http://localhost:5001/health`
- Check VITE_API_BASE in .env.local
- Check browser console for CORS errors

### "No active session"
- Click "Start New Session" button first
- Check that `/invoke/init` endpoint returns valid thread_id

### Events not displaying
- Check browser Network tab for SSE stream
- Verify events in raw response: `data: {...}\n\n` format
- Check console for parsing errors

### TypeScript errors
- Regenerate types if backend schema changed
- Ensure GraphEvent interface matches backend

---

## 16. Next Steps

1. Choose between Vite (lightweight) or Next.js (full-stack)
2. Run project setup steps from Section 1
3. Implement components in order (Sections 4-6)
4. Start both backend and frontend servers
5. Test basic chat flow
6. Iterate on UI/UX as needed

---

## Quick Start (TL;DR)

```bash
# 1. Create frontend
cd /home/amit/git
npm create vite@latest carqna-copilot-ui -- --template react-ts
cd carqna-copilot-ui

# 2. Install deps
npm install @copilotkit/react-core @copilotkit/react-ui

# 3. Copy components/hooks/types from this plan

# 4. Create .env.local
echo "VITE_API_BASE=http://localhost:5001" > .env.local

# 5. Start frontend
npm run dev

# 6. In another terminal, start backend
cd /home/amit/git/test-langgraph
python -m agent.carqna_dapr

# 7. Open http://localhost:5173
```

---

## Integration with Backend Plan

**Relates to:** [carqna-dapr-integration.md](./carqna-dapr-integration.md)

**Backend Requirements Met:**
- ✅ `/invoke/init` - Creates sessions
- ✅ `/invoke/chat/stream` - Streams SSE events
- ✅ `/health` - Health checks
- ✅ Thread ID persistence - Multi-turn conversations

**Future Enhancements:**
- Batch endpoint (`/invoke/chat`) with full trace download
- Conversation history with persisted thread_id
- User authentication layer
- Session analytics dashboard
