# Gobby Dashboard UI

> **Last Updated:** January 2026
> **Related:** [Design System](../design/design-system.md) | [UI Approach](../design/ui-approach.md) | [Tailwind Config](../design/tailwind.config.ts)

## Overview

A multi-tier UI for Gobby providing visual management of multi-CLI agents, tasks, worktrees, MCP servers, and cross-session context. The UI is a thin client connecting to the existing daemon infrastructure via REST API and MCP tools.

**Key Differentiator:** The only dashboard that unifies Claude Code, Gemini CLI, and Codex agents in a single view with shared task tracking, memory, workflow orchestration, and MCP tool access.

## Build Order

```text
Phase 0: TUI Dashboard (Textual - Python)           ← NEW: MVP priority
    ↓
Phase 1: Web Dashboard Foundation (Next.js + shadcn/ui)
    ↓
Phase 2: Real-time Updates (WebSocket events)
    ↓
Phase 3: Task Graph Visualization (Cytoscape.js)
    ↓
Phase 4: Agent Orchestrator (spawning, monitoring, autonomous loops)
    ↓
Phase 5: MCP Observatory (tool analytics, semantic search)
    ↓
Phase 6: Mobile PWA (remote agent access)           ← NEW: Core requirement
    ↓
Phase 7: Tauri Wrapper (native app, system tray)
```

## Core Design Principles

1. **Daemon-first** - UI is a client to the existing daemon; all state lives in daemon
2. **Real-time** - WebSocket-driven updates, no polling
3. **Progressive enhancement** - Works as web app, optional native wrapper
4. **Multi-CLI native** - First-class support for Claude, Gemini, Codex views
5. **Keyboard-friendly** - Full keyboard navigation, vim-style shortcuts
6. **Offline-capable** - Works when daemon is running, graceful degradation
7. **Active Intervention** - "Human-on-the-loop" controls to pause, edit, and redirect agents mid-flight

## Interaction Model

The UI is designed to be an **Active Command Center**, not just a passive dashboard.

- **Intervention**: Users can "Pause" any active agent to inspect its state or inject new instructions.
- **Redirection**: Edit a task description while an agent is working on it; the agent receives a "Plan Update" signal.
- **Assistance**: "Help Wanted" state where an agent pauses and requests human feedback (e.g., on a UI design choice).

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                              UI Layer                                   │
│  ┌───────────────────────────────────────────────────────────────────┐ │
│  │                     Web App (React/Vite)                          │ │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ │ │
│  │  │  Dashboard  │ │   Tasks     │ │  Worktrees  │ │    MCP      │ │ │
│  │  │    View     │ │    View     │ │    View     │ │    View     │ │ │
│  │  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ │ │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ │ │
│  │  │  Sessions   │ │   Memory    │ │             │ │  Settings   │ │ │
│  │  │    View     │ │    View     │ │    View     │ │    View     │ │ │
│  │  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ │ │
│  └───────────────────────────────────────────────────────────────────┘ │
│                                  │                                      │
│                    ┌─────────────┴─────────────┐                       │
│                    │      State Management     │                       │
│                    │   (Zustand + React Query) │                       │
│                    └─────────────┬─────────────┘                       │
│                                  │                                      │
│         ┌────────────────────────┼────────────────────────┐            │
│         │                        │                        │            │
│         ▼                        ▼                        ▼            │
│  ┌─────────────┐          ┌─────────────┐          ┌─────────────┐    │
│  │  REST API   │          │  WebSocket  │          │   Static    │    │
│  │   Client    │          │   Client    │          │   Assets    │    │
│  └──────┬──────┘          └──────┬──────┘          └──────┬──────┘    │
└─────────┼────────────────────────┼────────────────────────┼────────────┘
          │                        │                        │
          │        HTTP/WS         │                        │
          │                        │                        │
┌─────────┼────────────────────────┼────────────────────────┼────────────┐
│         ▼                        ▼                        ▼            │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │                      Gobby Daemon                                │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │  │
│  │  │ FastAPI     │  │  WebSocket  │  │  Static File Server     │  │  │
│  │  │ REST API    │  │  Events     │  │  (UI assets)            │  │  │
│  │  └─────────────┘  └─────────────┘  └─────────────────────────┘  │  │
│  │                                                                  │  │
│  │  ┌─────────────────────────────────────────────────────────────┐│  │
│  │  │                    Core Services                            ││  │
│  │  │  Sessions │ Tasks │ Worktrees │ MCP │ Memory          ││  │
│  │  └─────────────────────────────────────────────────────────────┘│  │
│  │                                                                  │  │
│  │  ┌─────────────────────────────────────────────────────────────┐│  │
│  │  │                    PostgreSQL Storage                           ││  │
│  │  └─────────────────────────────────────────────────────────────┘│  │
│  └──────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology | Rationale |
| ----- | ---------- | --------- |
| **UI Framework** | React 18 | Large ecosystem, good TypeScript support |
| **Build Tool** | Vite | Fast HMR, excellent DX |
| **Styling** | Tailwind CSS | Rapid prototyping, consistent design |
| **State** | Zustand + React Query | Simple global state + server state caching |
| **Charts** | Recharts | Simple, React-native charts |
| **Graphs** | Cytoscape.js | Powerful graph visualization for task deps |
| **Icons** | Lucide React | Clean, consistent iconography |
| **Native Wrapper** | Tauri (Phase 7) | Lightweight (~5MB vs Electron's 150MB) |

## UI Views

### 1. Dashboard (Home)

The main overview showing system status at a glance.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  GOBBY                              🔍 Search...     ⚙️  👤  ⚡ Running │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─ ACTIVE AGENTS ──────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │  │
│  │  │ 🟣 Claude    │  │ 🔵 Gemini    │  │ 🟢 Codex     │           │  │
│  │  │              │  │              │  │              │           │  │
│  │  │ 2 active     │  │ 1 active     │  │ 0 active     │           │  │
│  │  │ wt-abc, def  │  │ main         │  │              │           │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘           │  │
│  │                                                                   │  │
│  │  [+ Spawn Agent]                                                  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ QUICK STATS ────────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │   📋 Tasks          🌿 Worktrees       🔧 MCP Servers            │  │
│  │   ━━━━━━━━━━━━━    ━━━━━━━━━━━━━      ━━━━━━━━━━━━━             │  │
│  │   12 open          3 active           14 connected              │  │
│  │   5 ready          1 stale            186 tools                 │  │
│  │   3 in progress    2 merged today     1.2k calls today          │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ READY WORK ─────────────────────┐  ┌─ RECENT ACTIVITY ──────────┐  │
│  │                                   │  │                            │  │
│  │  P1  gt-a1b2c3  Fix auth bug     │  │  10:32  Task closed         │  │
│  │      └─ Assigned: Claude #1      │  │         gt-xyz789           │  │
│  │                                   │  │                            │  │
│  │  P1  gt-d4e5f6  Add rate limit   │  │  10:28  Worktree merged     │  │
│  │      └─ Unassigned [Assign]      │  │         wt-merge01          │  │
│  │                                   │  │                            │  │
│  │  P2  gt-g7h8i9  Update SDK       │  │  10:15  Agent spawned       │  │
│  │      └─ Unassigned [Assign]      │  │         Claude in wt-def    │  │
│  │                                   │  │                            │  │
│  │  [View All Tasks →]              │  │  [View All Activity →]     │  │
│  └───────────────────────────────────┘  └────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2. Tasks View

Full task management with dependency graph.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  TASKS                                          [+ Create] [↻ Sync]    │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─ FILTERS ────────────────────────────────────────────────────────┐  │
│  │  Status: [All ▾]  Priority: [All ▾]  Type: [All ▾]  [Ready Only] │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ VIEW: [List] [Graph] [Kanban] ──────────────────────────────────┐  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐ │  │
│  │  │                    DEPENDENCY GRAPH                         │ │  │
│  │  │                                                             │ │  │
│  │  │                    ┌─────────┐                              │ │  │
│  │  │                    │ Epic-1  │                              │ │  │
│  │  │                    │ ░░░░░░░ │                              │ │  │
│  │  │                    └────┬────┘                              │ │  │
│  │  │           ┌─────────────┼─────────────┐                     │ │  │
│  │  │           ▼             ▼             ▼                     │ │  │
│  │  │      ┌─────────┐   ┌─────────┐   ┌─────────┐               │ │  │
│  │  │      │ Task-1  │   │ Task-2  │   │ Task-3  │               │ │  │
│  │  │      │ ✓ Done  │   │ 🔄 Work │   │ ⏸ Block │               │ │  │
│  │  │      └─────────┘   └────┬────┘   └─────────┘               │ │  │
│  │  │                         │                                   │ │  │
│  │  │                         ▼                                   │ │  │
│  │  │                    ┌─────────┐                              │ │  │
│  │  │                    │ Sub-2.1 │                              │ │  │
│  │  │                    │ ⏳ Pend │                              │ │  │
│  │  │                    └─────────┘                              │ │  │
│  │  │                                                             │ │  │
│  │  │  [Zoom +] [Zoom -] [Fit] [Export PNG]                      │ │  │
│  │  └─────────────────────────────────────────────────────────────┘ │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ TASK DETAIL (gt-d4e5f6) ────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  Add rate limiting to API                              P1  task  │  │
│  │  ─────────────────────────────────────────────────────────────── │  │
│  │  Status: open          Created: 2h ago                           │  │
│  │  Blocked by: gt-a1b2c3 (Fix auth bug)                           │  │
│  │  Labels: api, security                                           │  │
│  │                                                                   │  │
│  │  Description:                                                     │  │
│  │  Implement rate limiting middleware for all API endpoints...     │  │
│  │                                                                   │  │
│  │  [Edit] [Expand with AI] [Assign to Worktree] [Close] [Delete]  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3. Worktrees View

Parallel agent management and coordination.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  WORKTREES                                      [+ Create] [🧹 Cleanup] │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─ ACTIVE WORKTREES ───────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐ │  │
│  │  │  🟢 wt-abc123                                    [Actions ▾] │ │  │
│  │  │  ──────────────────────────────────────────────────────────  │ │  │
│  │  │  Branch: agent/fix-auth-bug                                  │ │  │
│  │  │  Task: gt-a1b2c3 "Fix auth bug"                             │ │  │
│  │  │  Agent: Claude Code (session-789)                            │ │  │
│  │  │  Status: 3 commits ahead, 0 behind main                      │ │  │
│  │  │  Last activity: 2 minutes ago                                │ │  │
│  │  │                                                              │ │  │
│  │  │  [Open Terminal] [Sync from Main] [Preview Merge] [Merge]   │ │  │
│  │  │  [⏸ Pause Agent] [🛑 Interrupt] [✏️ Edit Instructions]       │ │  │
│  │  └─────────────────────────────────────────────────────────────┘ │  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐ │  │
│  │  │  🟢 wt-def456                                    [Actions ▾] │ │  │
│  │  │  ──────────────────────────────────────────────────────────  │ │  │
│  │  │  Branch: agent/add-user-api                                  │ │  │
│  │  │  Task: gt-x9y8z7 "Add user API"                             │ │  │
│  │  │  Agent: Claude Code (session-456)                            │ │  │
│  │  │  Status: 5 commits ahead, 2 behind main ⚠️                   │ │  │
│  │  │  Last activity: 5 minutes ago                                │ │  │
│  │  │                                                              │ │  │
│  │  │  [Open Terminal] [Sync from Main] [Preview Merge] [Merge]   │ │  │
│  │  └─────────────────────────────────────────────────────────────┘ │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ MERGE PREVIEW (wt-def456 → main) ───────────────────────────────┐  │
│  │                                                                   │  │
│  │  ⚠️  2 conflicts detected                                        │  │
│  │                                                                   │  │
│  │  src/api/users.py ────────────────────────── CONFLICT            │  │
│  │  src/api/auth.py ─────────────────────────── CONFLICT            │  │
│  │  src/models/user.py ──────────────────────── Auto-merge OK       │  │
│  │  tests/test_users.py ─────────────────────── Auto-merge OK       │  │
│  │                                                                   │  │
│  │  [Resolve with AI] [Manual Resolve] [Abort]                      │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4. Sessions View

Cross-CLI session tracking and history.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  SESSIONS                                              [Export History] │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─ ACTIVE SESSIONS ────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  🟣 Claude Code  session-abc123   wt-abc123   Fix auth    2m ago │  │
│  │  🟣 Claude Code  session-def456   wt-def456   Add API     5m ago │  │
│  │  🔵 Gemini CLI   session-ghi789   main        Research   12m ago │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ SESSION TIMELINE ───────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  Today                                                            │  │
│  │  ══════════════════════════════════════════════════════════════  │  │
│  │                                                                   │  │
│  │  10:45 ──●── 🟣 Session start (Claude)                           │  │
│  │          │   session-abc123 in wt-abc123                         │  │
│  │          │                                                        │  │
│  │  10:30 ──●── 🟣 Session end (Claude)                             │  │
│  │          │   session-xyz789 - /compact triggered                  │  │
│  │          │   → Handoff context saved (4 todos, 2 files)          │  │
│  │          │                                                        │  │
│  │  10:15 ──●── 🔵 Session start (Gemini)                           │  │
│  │          │   session-ghi789 on main                               │  │
│  │          │                                                        │  │
│  │  09:45 ──●── 🟣 Task created                                      │  │
│  │          │   gt-a1b2c3 "Fix auth bug"                            │  │
│  │          │                                                        │  │
│  │  09:30 ──●── 🟣 Worktree spawned                                  │  │
│  │              wt-abc123 for gt-a1b2c3                              │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘

  ┌─ SESSION LINEAGE ─────────────────────────────────────────────────┐
  │                                                                   │
  │  [Session A] (Planning)                                           │
  │       │                                                           │
  │       ├── gt-a1b2 (Auth) ────── [Session B] (wt-auth)             │
  │       │                         │                                 │
  │       │                         ├── gt-a1b2-1 (Subtask)           │
  │       │                         └── p123 (Paused for Review)      │
  │       │                                                           │
  │       └── gt-c3d4 (API) ─────── [Session C] (wt-api)              │
  │                                 │                                 │
  │                                 └── [Session D] (Fix Agent)       │
  │                                                                   │
  │  [Zoom In/Out] [Filter Active]                                    │
  │  [Select Node] → Shows details in side panel                      │
  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ SESSION DETAIL (session-xyz789) ────────────────────────────────┐  │
│  │                                                                   │  │
│  │  CLI: Claude Code          Duration: 45 minutes                   │  │
│  │  Started: 09:45            Ended: 10:30                           │  │
│  │  Worktree: wt-abc123       Branch: agent/fix-auth-bug            │  │
│  │                                                                   │  │
│  │  Tasks touched: gt-a1b2c3 (worked_on), gt-xyz789 (discovered)    │  │
│  │  Tool calls: 47            Tokens: ~12,000                        │  │
│  │                                                                   │  │
│  │  Handoff Context:                                                 │  │
│  │  ┌─────────────────────────────────────────────────────────────┐ │  │
│  │  │  ## In-Progress Work                                        │ │  │
│  │  │  - [x] Read auth.py                                         │ │  │
│  │  │  - [x] Identify token validation bug                        │ │  │
│  │  │  - [ ] Fix validation logic                                 │ │  │
│  │  │  - [ ] Add tests                                            │ │  │
│  │  └─────────────────────────────────────────────────────────────┘ │  │
│  │                                                                   │  │
│  │  [View Transcript] [Resume in New Session]                       │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5. MCP Observatory

Real-time MCP server and tool monitoring.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  MCP OBSERVATORY                                    [+ Add Server]      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─ SERVER STATUS ──────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  Server            Transport   Tools   Status      Calls (24h)   │  │
│  │  ─────────────────────────────────────────────────────────────── │  │
│  │  🟢 context7       http        42      Connected   523           │  │
│  │  🟢 supabase       stdio       18      Connected   89            │  │
│  │  🟢 gobby-tasks    internal    22      Connected   234           │  │
│  │  🟢 gobby-memory   internal    7       Connected   45            │  │
│  │  🟡 sequential...  http        12      Reconnect   0             │  │
│  │  ⚪ filesystem     stdio       8       Disabled    -             │  │
│  │                                                                   │  │
│  │  Total: 14 servers, 186 tools                                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ TOOL ANALYTICS ─────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  Most Used Tools (24h)                                           │  │
│  │  ════════════════════════════════════════════════════════════    │  │
│  │                                                                   │  │
│  │  context7:get-library-docs     ████████████████████████  312     │  │
│  │  gobby-tasks:update_task       ████████████████          198     │  │
│  │  gobby-tasks:list_ready_tasks  ████████████              156     │  │
│  │  supabase:execute_sql          ████████                  102     │  │
│  │  gobby-memory:recall           ██████                    78      │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ RECENT TOOL CALLS ──────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  Time     Server          Tool                    Session        │  │
│  │  ─────────────────────────────────────────────────────────────── │  │
│  │  10:45:32 context7        get-library-docs        session-abc    │  │
│  │  10:45:28 gobby-tasks     update_task             session-abc    │  │
│  │  10:45:15 supabase        list_tables             session-def    │  │
│  │  10:44:59 gobby-memory    recall                  session-abc    │  │
│  │  10:44:45 context7        resolve-library-id      session-ghi    │  │
│  │                                                                   │  │
│  │  [View All] [Export Logs]                                        │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ SERVER DETAIL (context7) ───────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  URL: https://mcp.context7.com                                   │  │
│  │  Transport: HTTP (StreamableHTTP)                                │  │
│  │  Connected: 2h 15m                                               │  │
│  │  Cache: 42 tool schemas cached                                   │  │
│  │                                                                   │  │
│  │  Tools:                                                          │  │
│  │  ┌─────────────────────────────────────────────────────────────┐│  │
│  │  │  resolve-library-id    Resolve library name to Context7 ID  ││  │
│  │  │  get-library-docs      Get documentation for a library      ││  │
│  │  │  search-libraries      Search for libraries by query        ││  │
│  │  │  ...                                                        ││  │
│  │  └─────────────────────────────────────────────────────────────┘│  │
│  │                                                                   │  │
│  │  [Refresh Tools] [Disconnect] [Remove]                           │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 6. Memory Browser

Search and manage cross-session memories.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  MEMORY                                             [+ Remember]        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─ SEARCH ─────────────────────────────────────────────────────────┐  │
│  │  🔍 [Search memories...                                        ] │  │
│  │  Type: [All ▾]  Project: [All ▾]  Min Importance: [0.5 ━━━●━━] │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ MEMORIES ───────────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐ │  │
│  │  │  📝 fact                                     Importance: 0.9 │ │  │
│  │  │  ──────────────────────────────────────────────────────────  │ │  │
│  │  │  This project uses pytest with conftest.py fixtures for     │ │  │
│  │  │  dependency injection. Tests are in tests/ directory.       │ │  │
│  │  │                                                              │ │  │
│  │  │  Project: gobby    Created: 3 days ago    Recalls: 12       │ │  │
│  │  │  Tags: testing, pytest                                       │ │  │
│  │  │                                                              │ │  │
│  │  │  [Edit] [Forget]                                            │ │  │
│  │  └─────────────────────────────────────────────────────────────┘ │  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐ │  │
│  │  │  ⚙️ preference                               Importance: 0.8 │ │  │
│  │  │  ──────────────────────────────────────────────────────────  │ │  │
│  │  │  User prefers functional programming patterns. Avoid        │ │  │
│  │  │  classes when functions with closures work.                 │ │  │
│  │  │                                                              │ │  │
│  │  │  Project: global   Created: 1 week ago   Recalls: 8         │ │  │
│  │  │  Tags: style, preferences                                    │ │  │
│  │  │                                                              │ │  │
│  │  │  [Edit] [Forget]                                            │ │  │
│  │  └─────────────────────────────────────────────────────────────┘ │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─ STATS ──────────────────────────────────────────────────────────┐  │
│  │                                                                   │  │
│  │  Total: 47 memories                                              │  │
│  │                                                                   │  │
│  │  By Type:           By Project:                                  │  │
│  │  fact      23       gobby     31                                 │  │
│  │  pattern   12       global    16                                 │  │
│  │  preference 8                                                    │  │
│  │  context    4       Avg Importance: 0.72                         │  │
│  │                                                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## API Architecture

### Design Pattern: REST + MCP Tools

Gobby uses a hybrid API approach:

- **REST endpoints** for admin/status, session management, hooks
- **MCP tools** for all CRUD operations (tasks, agents, worktrees, memory, workflows)

The UI calls MCP tools via `/mcp/tools/call` endpoint or the unified proxy `/mcp/{server}/{tool}`.

### REST Endpoints (Current Implementation)

```python
# Admin
GET  /admin/status              # Health, uptime, memory, MCP health, session/task stats
GET  /admin/metrics             # Prometheus-compatible metrics
GET  /admin/config              # Daemon configuration
POST /admin/shutdown            # Graceful shutdown

# Sessions
POST /sessions/register         # Register new session
GET  /sessions                  # List sessions with filters (project_id, status, source, limit)
GET  /sessions/{id}             # Session details
GET  /sessions/{id}/messages    # Session messages with pagination
POST /sessions/find_current     # Find by external_id + machine_id + source
POST /sessions/find_parent      # Find parent with handoff_ready status
POST /sessions/update_status    # Update session status
POST /sessions/{id}/stop        # Send stop signal to session
GET  /sessions/{id}/stop        # Check for pending stop signal
DELETE /sessions/{id}/stop      # Clear stop signal

# MCP Tool Discovery & Execution
GET  /mcp/servers               # List all MCP servers with status
GET  /mcp/{server}/tools        # List tools from specific server
GET  /mcp/tools                 # List all tools (optionally with metrics)
POST /mcp/tools/schema          # Get full inputSchema for a tool
POST /mcp/tools/call            # Execute a tool
POST /mcp/{server}/tools/{tool} # Unified proxy endpoint
POST /mcp/tools/recommend       # AI-powered tool recommendations
POST /mcp/tools/search          # Semantic similarity search
POST /mcp/tools/embed           # Generate embeddings for tools
POST /mcp/refresh               # Refresh tools, detect schema changes

# MCP Server Management
POST /mcp/servers               # Add new MCP server
DELETE /mcp/servers/{name}      # Remove MCP server
POST /mcp/servers/import        # Import from project/GitHub

# Hooks
POST /hooks/execute             # Execute CLI hook (claude/gemini/codex)

# Plugins & Webhooks
GET  /plugins                   # List loaded plugins
POST /plugins/reload            # Reload plugin by name
GET  /webhooks                  # List webhook endpoints
POST /webhooks/test             # Test webhook
```

### MCP Tools (Internal Servers)

All CRUD operations go through internal MCP servers. Total: **106+ tools**

| Server | Tools | Key Operations |
| ------ | ----- | -------------- |
| `gobby-tasks` | 35 | create, update, close, expand, validate, dependencies, sync |
| `gobby-agents` | 9 | start, cancel, list, get_result, can_spawn |
| `gobby-worktrees` | 14 | create, claim, release, sync, spawn_agent_in_worktree |
| `gobby-memory` | 9 | create, recall, update, delete, get_related, export_graph |
| `gobby-workflows` | 10 | activate, end, get_status, set_variable, request_transition |
| `gobby-sessions` | 6 | get, list, get_messages, search, get_handoff_context |
| `gobby-artifacts` | 4 | search, list, get, get_timeline |
| `gobby-metrics` | 4 | get_tool_metrics, get_top_tools, get_failing_tools |
| `gobby-hub` | 4 | Cross-project queries (list_all_projects, cross_project_tasks) |

### Example: Creating a task via MCP

```typescript
const result = await fetch('/mcp/tools/call', {
  method: 'POST',
  body: JSON.stringify({
    server_name: 'gobby-tasks',
    tool_name: 'create_task',
    arguments: { title: 'Fix bug', task_type: 'bug', priority: 1 }
  })
});
```

### WebSocket Events (Current Implementation)

Real-time updates via WebSocket on port 60888.

```typescript
// Client subscribes to specific events
ws.send({ type: "subscribe", events: ["session-start", "session-end", "agent_started"] });

// Unsubscribe
ws.send({ type: "unsubscribe", events: ["session-start"] });
```

#### Hook Events (via HookEventBroadcaster)

```typescript
interface HookEvent {
  type: "hook_event";
  event_type: "session-start" | "session-end" | "pre-tool-use" | "post-tool-use"
            | "pre-compact" | "stop" | "user-prompt-submit"
            | "subagent-start" | "subagent-stop" | "notification";
  timestamp: string;
  data: Record<string, any>;
  session_id: string | null;
  task_id: string | null;
  result: { continue: boolean; message?: string };
}
```

#### Agent Lifecycle Events

```typescript
interface AgentEvent {
  type: "agent_event";
  event: "agent_started" | "agent_completed" | "agent_failed" | "agent_cancelled" | "agent_timeout";
  run_id: string;
  parent_session_id: string;
  timestamp: string;
  session_id: string | null;
  mode: "terminal" | "embedded" | "headless";
  provider: string;
  pid?: number;
}
```

#### Autonomous Execution Events

```typescript
interface AutonomousEvent {
  type: "autonomous_event";
  event: "loop_started" | "loop_stopped" | "progress_recorded"
       | "task_started" | "stuck_detected" | "stop_requested";
  session_id: string;
  timestamp: string;
  // Additional fields vary by event type
  task_id?: string;
  progress_type?: string;
  layer?: string;
  reason?: string;
  final_summary?: string;
}
```

#### Session Message Streaming

```typescript
interface SessionMessageEvent {
  type: "session_message";
  session_id: string;
  message: {
    index: number;
    role: "user" | "assistant" | "tool";
    content: string;
    content_type: string;
    tool_name?: string;
    timestamp: string;
  };
}
```

#### Missing Events (Implemented in Phase 2)

The following domain events require the `DataEventBus` wiring from Phase 2:

- `task.created`, `task.updated`, `task.closed`, `task.expanded`
- `worktree.created`, `worktree.merged` (methods defined but not emitted)
- `mcp.server_connected`, `mcp.server_disconnected`, `mcp.tool_called`

**Architecture:** See Phase 2 spec (`docs/specs/ui/phase-2-realtime.md`) section 2.2.0 for the event bus architecture. MCP tools emit events via an injected `DataEventBus` singleton that connects to the WebSocket broadcast.

**Workaround (before Phase 2):** Poll `/admin/status` or use React Query with short intervals.

## Data Models (TypeScript)

> **Schema Version:** 47 (as of January 2026)

```typescript
// ============================================
// SESSION (13 new fields since original spec)
// ============================================
interface Session {
  id: string;
  external_id: string;              // Was: cli_type
  machine_id: string;
  source: "claude_code" | "gemini" | "codex" | "antigravity";
  project_id: string;
  title: string | null;
  status: "active" | "ended" | "handoff_ready";
  git_branch: string | null;

  // Agent tracking (NEW)
  parent_session_id: string | null;
  agent_depth: number;              // 0 = human, 1+ = spawned agent
  spawned_by_agent_id: string | null;
  agent_run_id: string | null;
  workflow_name: string | null;

  // Context (NEW)
  summary_markdown: string | null;
  compact_markdown: string | null;  // Handoff context
  context_injected: boolean;
  original_prompt: string | null;

  // Token usage (NEW)
  usage_input_tokens: number;
  usage_output_tokens: number;
  usage_cache_creation_tokens: number;
  usage_cache_read_tokens: number;
  usage_total_cost_usd: number;

  // Terminal pickup (NEW)
  terminal_context: {
    tty: string;
    parent_pid: number;
    term_session_id: string;
    tmux_pane?: string;
  } | null;

  created_at: string;
  updated_at: string;
}

// ============================================
// TASK (20 new fields since original spec)
// ============================================
interface Task {
  id: string;  // gt-xxxxxx
  project_id: string;
  parent_task_id: string | null;
  title: string;
  description: string | null;
  status: "open" | "in_progress" | "closed" | "failed" | "escalated" | "needs_decomposition";
  priority: 0 | 1 | 2 | 3 | 4;  // 0=critical, 4=backlog
  task_type: "bug" | "feature" | "task" | "epic" | "chore";
  assignee: string | null;
  labels: string[];
  closed_reason: string | null;

  // Session tracking (NEW)
  created_in_session_id: string | null;
  closed_in_session_id: string | null;

  // Commit tracking (NEW)
  commits: string[];                // Array of commit SHAs
  closed_commit_sha: string | null;
  closed_at: string | null;

  // Validation system (NEW)
  validation_status: "pending" | "passed" | "failed" | "skipped" | null;
  validation_feedback: string | null;
  validation_criteria: string | null;
  use_external_validator: boolean;
  validation_fail_count: number;
  validation_override_reason: string | null;

  // Task expansion (NEW)
  category: string | null;  // code, config, docs, test, refactor, research, planning, manual
  complexity_score: number | null;
  estimated_subtasks: number | null;
  expansion_context: string | null;

  // Workflow integration (NEW)
  workflow_name: string | null;
  verification: string | null;
  sequence_order: number | null;

  // Escalation (NEW)
  escalated_at: string | null;
  escalation_reason: string | null;

  created_at: string;
  updated_at: string;
}

// Dependencies now in separate table
interface TaskDependency {
  id: number;
  task_id: string;
  depends_on: string;
  dep_type: "blocks" | "related" | "discovered-from";
  created_at: string;
}

// ============================================
// AGENT RUN (NEW - tracks spawned agents)
// ============================================
interface AgentRun {
  id: string;
  parent_session_id: string;
  child_session_id: string | null;
  workflow_name: string | null;
  provider: "claude" | "gemini" | "codex";
  model: string | null;
  status: "pending" | "running" | "success" | "error" | "timeout" | "cancelled";
  prompt: string;
  result: string | null;
  error: string | null;
  tool_calls_count: number;
  turns_used: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

// ============================================
// WORKTREE (simplified - removed commits_ahead/behind)
// ============================================
interface Worktree {
  id: string;
  project_id: string;
  task_id: string | null;
  branch_name: string;
  worktree_path: string;
  base_branch: string;
  agent_session_id: string | null;
  status: "active" | "stale" | "merged" | "abandoned";
  merged_at: string | null;        // NEW
  merge_state: "pending" | "resolved" | null;  // NEW
  created_at: string;
  updated_at: string;
}

// ============================================
// MEMORY (added source tracking, cross-refs)
// ============================================
interface Memory {
  id: string;
  content: string;
  memory_type: "fact" | "preference" | "pattern" | "context";
  importance: number;  // 0.0 - 1.0
  project_id: string | null;
  tags: string[];

  // Source tracking (NEW)
  source_type: "user" | "session" | "inferred";
  source_session_id: string | null;

  // Usage analytics (NEW)
  access_count: number;
  last_accessed_at: string | null;

  created_at: string;
  updated_at: string;
}

interface MemoryCrossRef {
  source_id: string;
  target_id: string;
  similarity: number;  // 0.0 - 1.0
  created_at: string;
}

// ============================================
// SESSION ARTIFACTS (NEW - code snippets, diffs)
// ============================================
interface SessionArtifact {
  id: string;
  session_id: string;
  artifact_type: "code" | "diff" | "error" | "test" | "plan";
  content: string;
  metadata_json: Record<string, any> | null;
  source_file: string | null;
  line_start: number | null;
  line_end: number | null;
  created_at: string;
}

// ============================================
// MCP & TOOLS
// ============================================
interface MCPServer {
  name: string;
  transport: "http" | "stdio" | "websocket";
  url: string | null;
  command: string | null;
  args: string[] | null;
  enabled: boolean;
  status: "connected" | "disconnected" | "reconnecting";
  tools_count: number;
  description: string | null;
}

interface ToolMetrics {
  server_name: string;
  tool_name: string;
  call_count: number;
  success_count: number;
  failure_count: number;
  avg_latency_ms: number;
  p99_latency_ms: number;
}

// ============================================
// WORKFLOW STATE
// ============================================
interface WorkflowState {
  session_id: string;
  workflow_name: string;
  current_step: string;
  step_action_count: number;
  total_action_count: number;
  artifacts: string[];  // JSON array of completed artifacts
  variables: Record<string, any>;
  reflection_pending: boolean;
  task_list: string | null;
  current_task_index: number | null;
  updated_at: string;
}
```

## Keyboard Shortcuts

```text
Global:
  Cmd/Ctrl + K         Command palette
  Cmd/Ctrl + /         Search
  Cmd/Ctrl + 1-6       Switch views (Dashboard, Tasks, Worktrees, etc.)
  Cmd/Ctrl + N         New (context-dependent: task, worktree, memory)
  Esc                  Close modal/panel

Tasks View:
  j/k                  Navigate up/down
  Enter                Open task detail
  e                    Edit selected task
  x                    Expand with AI
  c                    Close task
  a                    Assign to worktree
  g                    Toggle graph view

Worktrees View:
  j/k                  Navigate up/down
  Enter                Open worktree detail
  s                    Spawn agent
  m                    Start merge
  d                    Delete worktree
  r                    Sync from main

Graph View:
  +/-                  Zoom in/out
  f                    Fit to screen
  Click + Drag         Pan
  Click node           Select task
  Double-click         Open task detail
```

## Configuration

```yaml
# ~/.gobby/config.yaml

ui:
  enabled: true
  port: 3001                              # Separate from API port (3000)
  theme: "system"                         # system, light, dark
  default_view: "dashboard"

  # Keyboard shortcuts (override defaults)
  shortcuts:
    command_palette: "Cmd+K"
    search: "Cmd+/"

  # Dashboard customization
  dashboard:
    show_activity_feed: true
    activity_limit: 20
    show_mcp_stats: true

  # Task graph settings
  task_graph:
    layout: "dagre"                       # dagre, force, hierarchical
    show_closed: false
    animation: true

  # Auto-refresh intervals (ms)
  refresh:
    sessions: 5000
    tasks: 10000
    mcp: 30000
```

## Implementation Checklist

### Phase 0: TUI Dashboard (MVP Priority)

> **Framework:** [Textual](https://textual.textualize.io/) (Python)
> **Goal:** Fast, keyboard-driven task management without leaving terminal

#### 0.1: Project Setup

- [ ] Create `src/gobby/tui/` package
- [ ] Add Textual dependency to pyproject.toml
- [ ] Create `gobby ui` CLI command to launch TUI
- [ ] Set up hot-reload for development

#### 0.2: Core Layout

- [ ] Main app with header (daemon status, shortcuts)
- [ ] Sidebar navigation (Tasks, Sessions, Agents, Memory, Metrics)
- [ ] Footer with command hints
- [ ] Keyboard navigation (j/k, Tab, Enter, Esc)

#### 0.3: Task View (Critical)

- [ ] Task list with status columns (Ready, In Progress, Blocked)
- [ ] Task detail panel (side drawer)
- [ ] Quick actions: Start task (sets session_task), expand, close
- [ ] Copy task ID to clipboard on Enter
- [ ] Filter by type, priority, parent

#### 0.4: Session View

- [ ] Session timeline (newest first)
- [ ] Provider badge (Claude/Gemini/Codex)
- [ ] Token/cost display
- [ ] Pickup action (copies handoff context)

#### 0.5: Agent View

- [ ] Running agents list with status
- [ ] Agent tree (parent/child hierarchy)
- [ ] Cancel action
- [ ] Progress indicators

#### 0.6: Connect to Daemon

- [ ] REST API client for data fetching
- [ ] WebSocket client for real-time updates
- [ ] Auto-reconnect on daemon restart

### Phase 1: Web Dashboard Foundation

> **Framework:** Next.js 14+ with shadcn/ui
> **Goal:** Full-featured web UI with mobile support

#### 1.1: Project Setup

- [ ] Create `ui/` directory in gobby repo
- [ ] Initialize Next.js 14 with App Router
- [ ] Configure Tailwind CSS with design tokens (see docs/design/tailwind.config.ts)
- [ ] Set up ESLint + Prettier
- [ ] Add build script to package UI assets

#### 1.2: Static File Serving

- [ ] Add static file route to FastAPI (`/ui/*`)
- [ ] Serve built UI assets from daemon
- [ ] Add `gobby ui` CLI command to open browser
- [ ] Handle SPA routing (all routes → index.html)

#### 1.3: Core Layout

- [ ] Implement main layout with sidebar navigation
- [ ] Add header with daemon status indicator
- [ ] Create placeholder views for all sections
- [ ] Implement basic routing

#### 1.4: API Client

- [ ] Create typed API client with fetch
- [ ] Set up React Query for data fetching
- [ ] Add error handling and retry logic
- [ ] Create Zustand store for global state

#### 1.5: Dashboard View (MVP)

- [ ] Implement active agents summary cards
- [ ] Add quick stats section
- [ ] Create ready work list
- [ ] Add recent activity feed

### Phase 2: Real-time Updates

#### 2.1: WebSocket Infrastructure

- [ ] Add WebSocket endpoint to daemon (`/ws/ui`)
- [ ] Implement event subscription system
- [ ] Create WebSocket client in UI
- [ ] Handle reconnection logic

#### 2.2: Event Integration

- [ ] Emit session events from SessionManager
- [ ] Emit task events from TaskManager
- [ ] Emit worktree events from WorktreeManager
- [ ] Emit MCP events from MCPClientManager

#### 2.3: UI Updates

- [ ] Update React Query cache on WebSocket events
- [ ] Add toast notifications for important events
- [ ] Implement optimistic updates
- [ ] Add loading states and skeletons

### Phase 3: Task Graph Visualization

#### 3.1: Graph Library Setup

- [ ] Install and configure Cytoscape.js
- [ ] Create TaskGraph React component
- [ ] Implement graph data transformation
- [ ] Add basic zoom/pan controls

#### 3.2: Graph Features

- [ ] Color-code nodes by status
- [ ] Add node shapes by task type
- [ ] Implement click-to-select
- [ ] Add edge labels for dependency types
- [ ] Implement node hover tooltips

#### 3.3: Graph Interactions

- [ ] Double-click to open task detail
- [ ] Right-click context menu
- [ ] Drag to rearrange (manual layout)
- [ ] Layout algorithm selection (dagre, force)

#### 3.4: Task Detail Panel

- [ ] Create slide-out task detail panel
- [ ] Add edit form
- [ ] Implement expand with AI button
- [ ] Add close/delete actions

### Phase 4: Worktree Orchestrator

#### 4.1: Worktree List

- [ ] Create worktree list view
- [ ] Show status, branch, linked task
- [ ] Display ahead/behind counts
- [ ] Add conflict warning indicators

#### 4.2: Worktree Actions

- [ ] Implement create worktree form
- [ ] Add spawn agent action (calls daemon API)
- [ ] Implement sync from main action
- [ ] Add delete worktree with confirmation

#### 4.3: Merge Preview

- [ ] Create merge preview modal
- [ ] Show conflicting files list
- [ ] Display file diff preview
- [ ] Add "Resolve with AI" button
- [ ] Implement manual resolve option

#### 4.4: Agent Spawning

- [ ] Add terminal selection (Ghostty, iTerm, Terminal)
- [ ] Pass initial prompt option
- [ ] Show spawned agent in active sessions
- [ ] Link spawned session to worktree

### Phase 5: MCP Observatory

#### 5.1: Server List

- [ ] Create MCP servers list view
- [ ] Show connection status
- [ ] Display tool counts
- [ ] Add call counts (24h)

#### 5.2: Tool Analytics

- [ ] Implement tool call tracking in daemon
- [ ] Create analytics API endpoint
- [ ] Build bar chart for most-used tools
- [ ] Add time-range selector

#### 5.3: Server Detail

- [ ] Create server detail panel
- [ ] List all tools with descriptions
- [ ] Show tool schema on click
- [ ] Add disconnect/reconnect actions

#### 5.4: Tool Call Log

- [ ] Implement tool call logging in daemon
- [ ] Create recent calls list
- [ ] Add call detail modal (args, result, duration)
- [ ] Add export functionality

### Phase 6: Memory Browser

#### 6.1: Memory View

- [ ] Create memory list with search (semantic via `/mcp/tools/search`)
- [ ] Add filters (type, project, importance, tags)
- [ ] Implement memory detail view with cross-references
- [ ] Add create/edit memory modal
- [ ] Add forget (delete) action
- [ ] Integrate existing vis.js graph (export_memory_graph)

#### 6.2: Memory Stats

- [ ] Create stats summary card
- [ ] Show counts by type
- [ ] Show counts by project
- [ ] Display average importance
- [ ] Show cross-reference count

### Phase 6.5: Mobile PWA (Core Requirement)

> **Goal:** Remote agent monitoring from phone

#### 6.5.1: PWA Setup

- [ ] Configure service worker for offline support
- [ ] Add web app manifest
- [ ] Enable push notifications (via Firebase or similar)
- [ ] Configure Cloudflare tunnel for remote access

#### 6.5.2: Mobile-Optimized Views

- [ ] Agent status dashboard (running/completed)
- [ ] One-tap cancel agent
- [ ] Task quick-view (ready tasks, tap to copy ID)
- [ ] Session cost summary

#### 6.5.3: Remote Access

- [ ] `gobby tunnel start` command
- [ ] QR code for mobile setup
- [ ] Token-based authentication
- [ ] Connection status indicator

### Phase 7: Tauri Wrapper

#### 7.1: Tauri Setup

- [ ] Add Tauri to project
- [ ] Configure build for macOS, Windows, Linux
- [ ] Set up auto-updater
- [ ] Configure app signing (macOS)

#### 7.2: Native Features

- [ ] Add system tray icon
- [ ] Show daemon status in tray
- [ ] Add tray menu (Open, Start Daemon, Stop, Quit)
- [ ] Implement global hotkey (Cmd+Shift+G)

#### 7.3: Native Notifications

- [ ] Replace web toasts with native notifications
- [ ] Add notification preferences
- [ ] Implement notification click handlers

#### 7.4: Distribution

- [ ] Create DMG for macOS
- [ ] Create MSI/EXE for Windows
- [ ] Create AppImage/Flatpak for Linux
- [ ] Set up release workflow

## Scope Boundaries: TUI vs Web

The TUI and Web UI serve different purposes and should NOT duplicate complex features.

| Feature | TUI (Phase 0) | Web (Phase 1+) | Notes |
| ------- | ------------- | -------------- | ----- |
| Task list | ✅ DataTable | ✅ DataTable | Both, universal |
| Task graph | ❌ | ✅ Cytoscape.js | Web only, requires canvas |
| Kanban board | ❌ | ✅ Drag-drop | Web only, requires DOM |
| Session timeline | ✅ DataTable | ✅ Visual timeline | Both work |
| Agent tree | ✅ Text indentation | ✅ Visual tree | TUI simpler |
| Memory search | ✅ Text list | ✅ Card grid | Both work |
| Dependency tree | ✅ Text expansion | ✅ Interactive graph | TUI inline text |
| Mobile/touch | ❌ | ✅ PWA | Web only |

**Principle:** TUI stays lean and keyboard-focused. Web handles visual complexity.

## Decisions

| # | Question | Decision | Rationale |
| --- | -------- | -------- | --------- |
| 1 | **TUI Framework** | Textual (Python) | Native Python, hot reload, CSS-like styling |
| 2 | **Web Framework** | Next.js 14 + shadcn/ui | SSR, App Router, accessible components |
| 3 | **Styling** | Tailwind CSS | Design tokens in `docs/design/tailwind.config.ts` |
| 4 | **State Management** | TanStack Query | Built-in caching, real-time sync |
| 5 | **Graph Library** | Cytoscape.js | Powerful, well-documented, good performance |
| 6 | **Icons** | Phosphor | Per design-principles skill |
| 7 | **Native Wrapper** | Tauri | 10-30x smaller than Electron |
| 8 | **Deployment** | Static files served by daemon | Zero additional infrastructure |
| 9 | **Initial Focus** | TUI-first, then Web | Validates patterns before web investment |
| 10 | **Mobile Access** | PWA + Cloudflare Tunnel | Works offline, remote access, push notifications |
| 11 | **API Pattern** | REST + MCP Tools | REST for admin, MCP for CRUD operations |
| 12 | **Real-time** | WebSocket on port 60888 | Already implemented, event subscriptions |
| 13 | **Remote Auth** | **MANDATORY** token auth | Remote = RCE risk, no "trust tunnel" option |
| 14 | **Event Wiring** | DataEventBus singleton | MCP tools emit via injected event bus |

## Future Enhancements

### Terminal Embedding (P3)

- Embed xterm.js terminal in Web UI
- Connect to worktree shells
- View agent output in real-time
- Send commands to running agents

### Collaborative Features (P4)

- Multi-user session viewing (via hub database)
- Real-time cursors in task graph
- Comment threads on tasks
- Activity notifications

### AI Assistant (PARTIAL - via MCP tools)

Already available via MCP:

- `suggest_next_task` - "What should I work on?"
- `expand_task` - AI-powered task breakdown
- `recommend_tools` - Tool suggestions for tasks
Future:
- Dedicated chat interface in UI
- Natural language task creation

### Cross-Project Dashboard (via gobby-hub)

Already available:

- `list_all_projects` - All projects in hub
- `list_cross_project_tasks` - Tasks across projects
- `hub_stats` - Aggregate statistics
Future:
- Visual cross-project view in UI
- Project switching without restart

## Appendix A: Agent Interrupt Mechanism (IMPLEMENTED)

The daemon now tracks and manages agent lifecycle with full stop signal support.

### Current Implementation

1. **Agent Registry** (`src/gobby/agents/registry.py`):
   - `RunningAgentRegistry` tracks in-memory agent processes
   - Stores `run_id`, `session_id`, `mode`, `provider`, `pid`
   - Emits WebSocket events on lifecycle changes

2. **Stop Signal System** (`session_stop_signals` table):
   - `POST /sessions/{id}/stop` - Send stop request
   - `GET /sessions/{id}/stop` - Check for pending signal
   - `DELETE /sessions/{id}/stop` - Clear signal
   - Autonomous agents poll for stop signals

3. **Agent Cancellation** (via MCP tools):

   ```typescript
   // Cancel a running agent
   await callTool('gobby-agents', 'cancel_agent', { run_id: 'xxx' });
   ```

4. **WebSocket Events**:
   - `agent_started`, `agent_completed`, `agent_failed`, `agent_cancelled`, `agent_timeout`
   - `autonomous_event` with `stop_requested` event

### UI Integration Points

| Action | API Call | WebSocket Event |
| ------ | -------- | --------------- |
| Cancel agent | `POST /sessions/{id}/stop` | `agent_cancelled` |
| View running | `gobby-agents.list_running_agents` | `agent_started` |
| Monitor progress | Subscribe to `agent_event` | Real-time updates |

### Stuck Detection

The autonomous workflow system (`src/gobby/workflows/actions.py`) includes stuck detection:

- Task selection loops
- Validation loops
- Tool repetition
- Emits `stuck_detected` event with suggested actions
