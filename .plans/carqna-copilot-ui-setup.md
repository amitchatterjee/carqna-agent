# CarQnA Copilot Kit UI Setup Plan

**Objective:** Create a standalone React + Copilot Kit web UI for the carqna-dapr service.

**Status:** Phase 1 Complete ✅ | Phase 2-3 In Progress

**Companion To:**
- [carqna-dapr-integration.md](./carqna-dapr-integration.md) - Backend HTTP service (Phases 1-4 Complete ✅)

**Frontend Scope:**
- ✅ Standalone React app with Copilot Kit integration
- ✅ Real-time streaming chat via SSE
- ✅ Session management with thread_id
- ✅ Event trace visualization
- ✅ Deployment ready (Next.js 🎯)

**Setup Progress:**
- ✅ Framework selected: Next.js with TypeScript + Tailwind
- ✅ Prerequisites confirmed: Node.js 18+
- ✅ Project scaffolding complete
- ✅ Dependencies installed
- ✅ Type definitions created
- ✅ Backend service layer implemented
- ✅ Custom hooks created
- ✅ UI components built (EventTraceViewer, ChatInterface)

---

## 1. Project Setup

### Prerequisites
- Node.js 18+
- npm or yarn

### Create Project

**Selected: Option B - Next.js ✅**

```bash
cd /home/amit/git
npx create-next-app@latest carqna-copilot-ui --typescript --tailwind --eslint
cd carqna-copilot-ui
```

**Why Next.js:**
- Built-in routing and server-side capabilities
- Tailwind CSS pre-configured
- Easy deployment to Vercel
- Better performance with built-in optimizations
- TypeScript support out of the box

### Install Dependencies
```bash
# Copilot Kit packages
npm install @copilotkit/react-core @copilotkit/react-ui
npm install @copilotkit/sdk-js

# Note: Tailwind, ESLint, and TypeScript already included with create-next-app
```

---

## 2. Type Definitions ✅ COMPLETE

**File:** `src/types/carqna.ts` - [Created]

Type definitions for all backend response formats and frontend state models.

**Interfaces:**
- `GraphEvent` - Agent execution events (tool_call, tool_result, llm_thinking, final_response, error, done)
- `ChatResponse` - Complete response from `/invoke/chat` or `/invoke/chat/stream`
- `InitResponse` - Response from `/invoke/init` endpoint
- `ChatSession` - Frontend session management

---

## 3. Backend Service Layer ✅ COMPLETE

**File:** `src/services/carqnaService.ts` - [Created]

**Exported functions:**
- `initConversation(userId)` - Creates new session with `/invoke/init`
- `streamChat(message, threadId, onEvent)` - Streams SSE events from `/invoke/chat/stream`
- `getHealth()` - Health check from `/health` endpoint

---

## 4. Custom Hooks ✅ COMPLETE

**File:** `src/hooks/useCarqnaChat.ts` - [Created]

**Hook interface:**
- `threadId` - Current session ID
- `isLoading` - Request in progress
- `events` - Streamed GraphEvents
- `error` - Error message
- `startSession(userId)` - Initialize new conversation
- `sendMessage(message)` - Send message and stream events

---

## 5. UI Components ✅ COMPLETE

**File 1:** `src/components/EventTraceViewer.tsx` - [Created]
- Displays final response with green highlight
- Expandable "Tools Used" section for tool_call/tool_result events
- Expandable "Agent Reasoning" section for llm_thinking events
- Full trace JSON in collapsible details

**File 2:** `src/components/ChatInterface.tsx` - [Created]
- Header with session info
- Session start button
- Message display area (user vs assistant styling)
- Real-time "Thinking..." indicator
- Event trace viewer integration
- Error display
- Message input footer with send button

---

## 6. Main App Component

**File:** `src/app/page.tsx`

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
NEXT_PUBLIC_API_BASE=http://localhost:5001

# Copilot Kit (optional, for production)
NEXT_PUBLIC_COPILOT_KIT_PUBLIC_API_KEY=

# App Config
NEXT_PUBLIC_APP_NAME=CarQnA Assistant
NEXT_PUBLIC_DEFAULT_USER_ID=user-123
```

---

## 8. CSS Styling

**File:** `src/app/globals.css`

Tailwind CSS is pre-configured by `create-next-app`. No additional setup needed—components use Tailwind utility classes directly.

Optional: Add custom styles for Copilot Kit components in globals.css

---

## 9. Build Scripts

**File:** `package.json` (pre-configured by create-next-app)

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
│   └── app/
│       ├── page.tsx                   # Main page (Next.js)
│       └── globals.css                # Global styles (Tailwind)
├── .env.local                         # Environment config
├── next.config.ts                     # Next.js config
├── package.json
├── tsconfig.json
└── README.md
```

---

## 11. Development Workflow

### Terminal 1: Start Backend
```bash
cd /home/amit/git/carqna-agent
python -m agent.carqna_dapr
# Output: ✓ carqna-dapr ready on port 5001
```

### Terminal 2: Start Frontend
```bash
cd /home/amit/git/carqna-copilot-ui
npm run dev
# Output: ▲ Next.js 15.x.x
#         ✓ Ready in 2.5s
#         ➜  Local:   http://localhost:3000/
```

### Test the UI
1. Open http://localhost:3000
2. Click "Start New Session"
3. Type: "What is the price of a Toyota Prius 2025?"
4. Watch real-time events stream in
5. Expand trace to see tool calls and reasoning

---

## 12. Deployment Options

### Option A: Vercel (Recommended)
```bash
npm install -g vercel
vercel
```

Environment variables:
```
NEXT_PUBLIC_API_BASE=https://carqna-dapr.example.com
```

### Option B: Docker
**Dockerfile:**
```dockerfile
FROM node:18-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

FROM node:18-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:18-alpine
WORKDIR /app
RUN npm install -g serve
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package*.json ./
RUN npm ci --production
EXPOSE 3000
CMD ["npm", "run", "start"]
```

**docker-compose.yml:**
```yaml
version: "3.9"
services:
  carqna-ui:
    build: .
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_BASE=http://carqna-dapr:5001
```

---

## 13. Implementation Steps

### Phase 1: Project Setup ✅ COMPLETE
1. [x] Create Vite or Next.js project
2. [x] Install dependencies
3. [x] Set up `.env.local`
4. [x] Create folder structure
5. [x] Create type definitions (carqna.ts)

### Phase 2: Core Components ✅ COMPLETE
5. [x] Create type definitions (`types/carqna.ts`)
6. [x] Implement backend service (`services/carqnaService.ts`)
7. [x] Create custom hook (`hooks/useCarqnaChat.ts`)
8. [x] Build ChatInterface component
9. [x] Build EventTraceViewer component

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
npx create-next-app@latest carqna-copilot-ui --typescript --tailwind --eslint
cd carqna-copilot-ui

# 2. Install Copilot Kit deps
npm install @copilotkit/react-core @copilotkit/react-ui

# 3. Copy components/hooks/types from this plan

# 4. Create .env.local
echo "NEXT_PUBLIC_API_BASE=http://localhost:5001" > .env.local

# 5. Start frontend
npm run dev

# 6. In another terminal, start backend
cd /home/amit/git/carqna-agent
python -m agent.carqna_dapr

# 7. Open http://localhost:3000
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
