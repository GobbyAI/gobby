# Gobby Architecture Documentation

> Updated: 2026-08-14 | Version: 0.5.0

## Overview

Gobby is a **local-first daemon** that unifies AI coding assistants (Claude Code, Codex, AGY, Qwen, Droid, and Grok) through a hook interface for session tracking. It provides a rule engine for declarative behavior enforcement, an MCP proxy with progressive tool discovery, agent spawning with P2P messaging, and persistent memory.

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
│  │ AgentStepInst.  │  │   AgentRunner   │  │  MemoryManager  │        │
│  │ Manager (snap)  │  │ (spawn/monitor) │  │  (recall/store) │        │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘        │
│                                                                        │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │    Adapters     │  │  LLMService     │  │ MCPClientManager│        │
│  │ Claude/Codex/   │  │  (multi-prov)   │  │ (conn pooling)  │        │
│  │ AGY/Droid/      │  │                 │  │                 │        │
│  │ Qwen/Grok       │  │                 │  │                 │        │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘        │
└────────────────────────────────┬───────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          DATA LAYER                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │   HubDatabase   │  │ LocalMCPManager │  │ File Storage    │        │
│  │  (PostgreSQL)   │  │ (tool caching)  │  │ (sync, logs)    │        │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘        │
│                    bootstrap.yaml database_url                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Core Components

### Entry Points

| Component | File | Purpose |
|-----------|------|---------|
| **CLI** | `src/gobby/cli/` | Click-based commands (50+ modules) |
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
| **RuleEngine** | `workflows/engine/core.py` | Declarative rule evaluation and enforcement |
| **HookManager** | `hooks/hook_manager.py` | Central coordinator for all hook events |
| **SessionManager** | `storage/sessions/_manager.py` | Session registration, lookup, status updates |
| **AgentStepInstanceManager** | `workflows/step_instances.py` | Per-session agent-step snapshot and progress |
| **AgentRunner** | `agents/runner.py` | Agent process spawning and lifecycle |
| **MemoryManager** | `memory/manager.py` | Persistent fact storage and recall |
| **LLMService** | `llm/service.py` | Multi-provider LLM management |
| **MCPClientManager** | `mcp_proxy/manager.py` | Connection pooling for downstream MCP servers |
| **PipelineExecutor** | `workflows/pipeline_executor.py` | Deterministic sequential pipeline execution |

### Adapter Layer

| Adapter | File | CLI |
|---------|------|-----|
| **ClaudeCodeAdapter** | `adapters/claude_code.py` | Claude Code |
| **AgyAdapter** | `adapters/agy.py` | AGY CLI |
| **CodexAdapter** | `adapters/codex_impl/app_server_adapter.py` | Codex CLI |
| **DroidAdapter** | `adapters/droid.py` | Droid |
| **QwenAdapter** | `adapters/qwen.py` | Qwen Code |
| **GrokAdapter** | `adapters/grok.py` | Grok CLI |

### Data Layer

| Component | File | Storage |
|-----------|------|---------|
| **PostgresHubDatabase** | `storage/hub/postgres.py` | PostgreSQL hub storage |
| **SessionManager** | `storage/sessions/_manager.py` | Session CRUD operations |
| **LocalTaskManager** | `storage/tasks/_manager.py` | Task CRUD with dependency graphs |
| **LocalProjectManager** | `storage/projects.py` | Project CRUD operations |
| **LocalMCPManager** | `storage/mcp.py` | MCP server and tool caching |
| **RuleDefinitionManager** | `storage/definitions/rules.py` | Typed `rule_definitions` CRUD |
| **AgentDefinitionManager** | `storage/definitions/agents.py` | Typed `agent_definitions` plus `agent_step_workflows` |
| **SessionVariableDefaultManager** | `storage/definitions/variables.py` | Typed `session_variable_defaults` CRUD |
| **PipelineDefinitionManager** | `storage/definitions/pipelines.py` | Typed `pipeline_definitions` CRUD |

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
            ├─ mcp_call: record for dispatch
            ├─ observe: record observation
            ├─ rewrite_input: rewrite the tool input
            └─ load_skill: load a skill into context
```

### Session Lifecycle

```
1. CLI Hook Invoked (SessionStart)
   └─> Hook Dispatcher Script (per CLI)
       └─> HTTP POST /api/hooks/execute (hook_type: session-start in body)
           └─> Adapter.translate_to_hook_event()
               └─> HookManager.handle()
                   └─> RuleEngine.evaluate(session_start)
                       └─> SessionManager.register_session()

2. Before each tool call
   └─> RuleEngine.evaluate(before_tool)
       └─> Block / set_variable / inject_context / mcp_call

3. Session End
   └─> HookManager.handle()
       └─> generate_session_summaries() (sessions/summarize.py, LLM)
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
| **AGY CLI** | HTTP hooks | Inbound |
| **Codex CLI** | HTTP hooks (+ app-server JSON-RPC subprocess) | Inbound |
| **Claude API** | HTTP | Outbound |
| **OpenAI API** | HTTP | Outbound |
| **Downstream MCP** | HTTP/stdio/WS | Outbound |

## Key Design Decisions

1. **Local-First**: Runtime state lives in a local PostgreSQL hub, no cloud dependency
2. **CLI-Agnostic**: Adapter pattern normalizes different CLI hook formats to unified events
3. **Rules-First Enforcement**: Declarative rules enforce behavior without relying on prompt compliance
4. **Progressive Discovery**: MCP tools loaded on-demand to reduce token usage
5. **Multi-Provider LLM**: Abstraction layer supports Claude (API + CLI), Codex, AGY, Qwen, and Droid providers plus local endpoints
6. **Event-Driven Hooks**: Hook events feed into RuleEngine for enforcement and context injection
7. **P2P Agent Messaging**: Agents communicate via target-based `send_message` without parent relay
8. **Thread-Safe Storage**: Bounded database execution and PostgreSQL transactions for concurrent access

_Last verified: 2026-08-14_
