# Gobby Architecture Documentation

> Updated: 2026-02-22 | Version: 0.2.21

## Overview

Gobby is a **local-first daemon** that unifies AI coding assistants (Claude Code, Gemini CLI, Codex) through a hook interface for session tracking. It provides a rule engine for declarative behavior enforcement, an MCP proxy with progressive tool discovery, agent spawning with P2P messaging, and persistent memory.

### Key Characteristics

| Property | Value |
|----------|-------|
| **Repository Type** | Monolith |
| **Primary Language** | Python 3.13+ |
| **Project Type** | Backend + CLI (Daemon) + Web UI |
| **Framework** | FastAPI + FastMCP + Click |
| **Database** | PostgreSQL local runtime hub |
| **Architecture Pattern** | Layered Service Architecture with Event-Driven Hooks and Declarative Rules |

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           CLI ENTRY POINTS                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  │
│  │ gobby start │  │ gobby stop  │  │gobby status │  │gobby install │  │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬───────┘  │
│         └────────────────┴────────┬───────┴─────────────────┘          │
│                                   ▼                                     │
│                           cli/ (Click)                                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           DAEMON LAYER                                  │
│                         runner.py                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                  │
│  │ HTTP Server  │  │  WebSocket   │  │  MCP Server  │                  │
│  │  (FastAPI)   │  │   Server     │  │  (FastMCP)   │                  │
│  │  :60887      │  │   :60888     │  │  (stdio)     │                  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                  │
└─────────┼─────────────────┼─────────────────┼──────────────────────────┘
          │                 │                 │
          ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         SERVICE LAYER                                   │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │   RuleEngine    │  │   HookManager   │  │ SessionManager  │        │
│  │  (enforcement)  │  │  (coordinator)  │  │  (registration) │        │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘        │
│           │                    │                     │                  │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │  WorkflowEngine │  │   AgentRunner   │  │  MemoryManager  │        │
│  │  (state machine)│  │ (spawn/monitor) │  │  (recall/store) │        │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘        │
│                                                                        │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │    Adapters     │  │  LLMService     │  │ MCPClientManager│        │
│  │ Claude/Gemini/  │  │  (multi-prov)   │  │ (conn pooling)  │        │
│  │ Codex Adapters  │  │                 │  │                 │        │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘        │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          DATA LAYER                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │   HubDatabase   │  │  MCPDBManager   │  │ File Storage    │        │
│  │  (PostgreSQL)   │  │ (tool caching)  │  │ (sync, logs)    │        │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘        │
│                    bootstrap.yaml database_url                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Core Components

### Entry Points

| Component | File | Purpose |
|-----------|------|---------|
| **CLI** | `src/gobby/cli/` | Click-based commands (~25 modules) |
| **Daemon Runner** | `src/gobby/runner.py` | Main daemon process, starts all servers |

### Server Layer

| Component | File | Protocol | Port |
|-----------|------|----------|------|
| **HTTP Server** | `servers/http.py` | HTTP REST | 60887 |
| **HTTP Routes** | `servers/routes/` | REST API | - |
| **WebSocket Server** | `servers/websocket/` | WebSocket | 60888 |
| **MCP Server** | `mcp_proxy/server.py` | MCP (JSON-RPC) | - |

### Service Layer

| Component | File | Responsibility |
|-----------|------|----------------|
| **RuleEngine** | `workflows/rule_engine.py` | Declarative rule evaluation and enforcement |
| **HookManager** | `hooks/hook_manager.py` | Central coordinator for all hook events |
| **SessionManager** | `sessions/manager.py` | Session registration, lookup, status updates |
| **WorkflowEngine** | `workflows/engine.py` | On-demand step-based state machines |
| **AgentRunner** | `agents/runner.py` | Agent process spawning and lifecycle |
| **MemoryManager** | `memory/manager.py` | Persistent fact storage and recall |
| **LLMService** | `llm/service.py` | Multi-provider LLM management |
| **MCPClientManager** | `mcp_proxy/manager.py` | Connection pooling for downstream MCP servers |
| **PipelineExecutor** | `workflows/pipeline_executor.py` | Deterministic sequential pipeline execution |

### Adapter Layer

| Adapter | File | CLI |
|---------|------|-----|
| **ClaudeCodeAdapter** | `adapters/claude_code.py` | Claude Code |
| **GeminiAdapter** | `adapters/gemini.py` | Gemini CLI |
| **CodexAdapter** | `adapters/codex.py` | Codex CLI |

### Data Layer

| Component | File | Storage |
|-----------|------|---------|
| **PostgresHubDatabase** | `storage/hub/postgres.py` | PostgreSQL hub storage |
| **LocalSessionManager** | `storage/sessions.py` | Session CRUD operations |
| **LocalTaskManager** | `storage/tasks.py` | Task CRUD with dependency graphs |
| **LocalProjectManager** | `storage/projects.py` | Project CRUD operations |
| **MCPDatabaseManager** | `storage/mcp.py` | MCP server and tool caching |

## Data Flows

### Rule Evaluation

```
Hook event fired (e.g., before_tool)
  │
  ├─ 1. Load enabled rules matching this event type
  ├─ 2. Apply session overrides (per-session enable/disable)
  ├─ 3. Filter by agent_scope (if applicable)
  ├─ 4. Sort by priority ascending (10 → 20 → 100)
  └─ 5. Evaluate each rule:
        ├─ Check `when` condition → skip if false
        └─ Apply effect:
            ├─ block: check tool matching → if match, STOP
            ├─ set_variable: mutate variable immediately
            ├─ inject_context: append to context list
            └─ mcp_call: record for dispatch
```

### Session Lifecycle

```
1. CLI Hook Invoked (SessionStart)
   └─> Hook Dispatcher Script (per CLI)
       └─> HTTP POST /api/v1/hooks/session-start
           └─> Adapter.translate_to_hook_event()
               └─> HookManager.handle()
                   └─> RuleEngine.evaluate(session_start)
                       └─> SessionManager.register_session()

2. Before each tool call
   └─> RuleEngine.evaluate(before_tool)
       └─> Block / set_variable / inject_context / mcp_call

3. Session End
   └─> HookManager.handle()
       └─> SummaryGenerator.generate() (LLM)
           └─> SessionManager.update_status("handoff_ready")
```

### MCP Progressive Tool Discovery

```
1. list_tools(server_name="...")     → Names and descriptions (~200 tokens)
2. get_tool_schema(server, tool)     → Full inputSchema on demand
3. call_tool(server, tool, args)     → Execute via downstream transport
```

## External Integrations

| Integration | Protocol | Direction |
|-------------|----------|-----------|
| **Claude Code** | HTTP hooks | Inbound |
| **Gemini CLI** | HTTP hooks | Inbound |
| **Codex CLI** | WebSocket events | Inbound |
| **Claude API** | HTTP | Outbound |
| **OpenAI API** | HTTP | Outbound |
| **Gemini API** | HTTP | Outbound |
| **Downstream MCP** | HTTP/stdio/WS | Outbound |

## Key Design Decisions

1. **Local-First**: Runtime state lives in a local PostgreSQL hub, no cloud dependency
2. **CLI-Agnostic**: Adapter pattern normalizes different CLI hook formats to unified events
3. **Rules-First Enforcement**: Declarative rules enforce behavior without relying on prompt compliance
4. **Progressive Discovery**: MCP tools loaded on-demand to reduce token usage
5. **Multi-Provider LLM**: Abstraction layer supports Claude, Gemini, OpenAI, and LiteLLM
6. **Event-Driven Hooks**: Hook events feed into RuleEngine for enforcement and context injection
7. **P2P Agent Messaging**: Agents communicate via target-based `send_message` without parent relay
8. **Thread-Safe Storage**: Bounded database execution and PostgreSQL transactions for concurrent access
