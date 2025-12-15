# Gobby Architecture Documentation

> Generated: 2025-12-15 | Scan Level: Exhaustive | Version: 0.1.0

## Overview

Gobby is a **local-first daemon** that unifies AI coding assistants (Claude Code, Gemini CLI, Codex) through a hook interface for session tracking and provides an MCP proxy with progressive tool discovery for efficient access to downstream servers.

### Key Characteristics

| Property | Value |
|----------|-------|
| **Repository Type** | Monolith |
| **Primary Language** | Python 3.11+ |
| **Project Type** | Backend + CLI (Daemon) |
| **Framework** | FastAPI + FastMCP + Click |
| **Database** | SQLite (local-first) |
| **Architecture Pattern** | Layered Service Architecture with Event-Driven Hooks |

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           CLI ENTRY POINTS                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐   │
│  │ gobby start │  │ gobby stop  │  │gobby status │  │gobby install │   │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬───────┘   │
│         └────────────────┴────────┬───────┴─────────────────┘           │
│                                   ▼                                      │
│                           cli.py (Click)                                 │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           DAEMON LAYER                                   │
│                         runner.py                                        │
│  ┌──────────────┐      ┌──────────────┐       ┌──────────────┐         │
│  │ HTTP Server  │      │  WebSocket   │       │  MCP Server  │         │
│  │  (FastAPI)   │      │   Server     │       │  (FastMCP)   │         │
│  │  :8765       │      │   :8766      │       │  (stdio)     │         │
│  └──────┬───────┘      └──────────────┘       └──────┬───────┘         │
└─────────┼────────────────────────────────────────────┼──────────────────┘
          │                                            │
          ▼                                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         SERVICE LAYER                                    │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │   HookManager   │  │ SessionManager  │  │  LLMService     │         │
│  │  (coordinator)  │  │  (registration) │  │  (multi-prov)   │         │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘         │
│           ▼                    │                    ▼                   │
│  ┌─────────────────┐           │           ┌─────────────────┐         │
│  │    Adapters     │           │           │   LLM Providers │         │
│  │ Claude/Gemini/  │           │           │ Claude/Codex/   │         │
│  │     Codex       │           │           │ Gemini/LiteLLM  │         │
│  └─────────────────┘           │           └─────────────────┘         │
└────────────────────────────────┼────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          DATA LAYER                                      │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │  LocalDatabase  │  │  MCPDBManager   │  │ File Storage    │         │
│  │   (SQLite)      │  │ (tool caching)  │  │ (summaries)     │         │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘         │
│                    ~/.gobby/gobby.db                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

## Core Components

### Entry Points

| Component | File | Purpose |
|-----------|------|---------|
| **CLI** | `src/cli.py` | Click-based commands (start, stop, status, install, uninstall, init) |
| **Daemon Runner** | `src/runner.py` | Main daemon process, starts all servers |

### Server Layer

| Component | File | Protocol | Port |
|-----------|------|----------|------|
| **HTTP Server** | `src/servers/http.py` | HTTP REST + MCP | 8765 |
| **WebSocket Server** | `src/servers/websocket.py` | WebSocket | 8766 |
| **MCP Server** | `src/mcp_proxy/server.py` | MCP (JSON-RPC) | - |
| **Stdio MCP** | `src/mcp_proxy/stdio.py` | stdio transport | - |

### Service Layer

| Component | File | Responsibility |
|-----------|------|----------------|
| **HookManager** | `src/hooks/hook_manager.py` | Central coordinator for all hook events |
| **SessionManager** | `src/sessions/manager.py` | Session registration, lookup, status updates |
| **SummaryGenerator** | `src/sessions/summary.py` | LLM-powered session summaries |
| **LLMService** | `src/llm/service.py` | Multi-provider LLM management |
| **MCPClientManager** | `src/mcp_proxy/manager.py` | Connection pooling for downstream MCP servers |

### Adapter Layer

| Adapter | File | CLI | Hook Format |
|---------|------|-----|-------------|
| **ClaudeCodeAdapter** | `src/adapters/claude_code.py` | Claude Code | JSON payload via HTTP |
| **GeminiAdapter** | `src/adapters/gemini.py` | Gemini CLI | JSON payload via HTTP |
| **CodexAdapter** | `src/adapters/codex.py` | Codex CLI | JSON-RPC (app-server) + notify |

### Data Layer

| Component | File | Storage |
|-----------|------|---------|
| **LocalDatabase** | `src/storage/database.py` | SQLite with thread-local connections |
| **LocalSessionManager** | `src/storage/sessions.py` | Session CRUD operations |
| **LocalProjectManager** | `src/storage/projects.py` | Project CRUD operations |
| **MCPDatabaseManager** | `src/storage/mcp.py` | MCP server and tool caching |

## Data Models

### Database Schema (v7)

```sql
-- Projects: Container for sessions and MCP servers
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT,
    github_url TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- Sessions: AI coding assistant sessions
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cli_key TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    source TEXT NOT NULL,           -- "claude", "gemini", "codex"
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT,
    status TEXT DEFAULT 'active',   -- active, paused, expired, archived, handoff_ready
    jsonl_path TEXT,
    summary_path TEXT,
    summary_markdown TEXT,
    git_branch TEXT,
    parent_session_id TEXT REFERENCES sessions(id),
    created_at TEXT,
    updated_at TEXT
);

-- MCP Servers: Downstream server configurations
CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    transport TEXT NOT NULL,        -- "http", "stdio", "websocket"
    url TEXT,
    command TEXT,
    args TEXT,                      -- JSON array
    env TEXT,                       -- JSON object
    headers TEXT,                   -- JSON object
    enabled INTEGER DEFAULT 1,
    description TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- Tools: Cached tool schemas from MCP servers
CREATE TABLE tools (
    id TEXT PRIMARY KEY,
    mcp_server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    input_schema TEXT,              -- JSON object
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(mcp_server_id, name)
);
```

## API Contracts

### HTTP Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/admin/status` | GET | Daemon status |
| `/mcp` | POST | MCP protocol (JSON-RPC) |
| `/api/v1/hooks/{hook_type}` | POST | CLI hook execution |
| `/api/v1/sessions` | GET/POST | Session management |
| `/api/v1/sessions/{id}` | GET/PATCH | Individual session |

### MCP Tools

| Tool | Category | Description |
|------|----------|-------------|
| `status` | Monitoring | Daemon status and health |
| `list_mcp_servers` | Discovery | List configured MCP servers |
| `call_tool` | Proxy | Execute tool on downstream server |
| `read_mcp_resource` | Proxy | Read resource from downstream server |
| `add_mcp_server` | Management | Add new MCP server |
| `remove_mcp_server` | Management | Remove MCP server |
| `import_mcp_server` | Management | Import from project/GitHub/query |
| `list_tools` | Discovery | List tools from downstream servers |
| `get_tool_schema` | Discovery | Get full tool schema |
| `execute_code` | Execution | Run code in Claude sandbox |
| `process_large_dataset` | Execution | Token-optimized data processing |
| `recommend_tools` | AI | LLM-powered tool recommendations |
| `call_hook` | Integration | Trigger hooks for non-Claude CLIs |
| `codex` | Integration | Direct Codex interaction |

## Data Flows

### Session Lifecycle

```
1. CLI Hook Invoked (SessionStart)
   └─> Hook Dispatcher Script (per CLI)
       └─> HTTP POST /api/v1/hooks/session-start
           └─> Adapter.translate_to_hook_event()
               └─> HookManager.handle()
                   └─> SessionManager.register_session()
                       └─> LocalDatabase INSERT
                           └─> HookResponse with session_id

2. User Prompt (UserPromptSubmit)
   └─> Hook Dispatcher Script
       └─> HTTP POST /api/v1/hooks/user-prompt-submit
           └─> HookManager.handle()
               └─> Title synthesis (LLM)
                   └─> SessionManager.update_title()

3. Session End (SessionEnd)
   └─> Hook Dispatcher Script
       └─> HTTP POST /api/v1/hooks/session-end
           └─> HookManager.handle()
               └─> SummaryGenerator.generate() (LLM)
                   └─> SessionManager.update_status("handoff_ready")
                       └─> Summary stored for next session
```

### MCP Progressive Tool Discovery

```
1. List Available Tools (lightweight)
   └─> list_tools(server="context7")
       └─> Returns: [{name, brief}] from cached config

2. Get Full Schema (on-demand)
   └─> get_tool_schema(server="context7", tool="get-library-docs")
       └─> Reads from ~/.gobby/tools/ cache
           └─> Returns: {name, description, inputSchema}

3. Execute Tool
   └─> call_tool(server="context7", tool="get-library-docs", args={...})
       └─> MCPClientManager.call_tool()
           └─> HTTP/stdio/WebSocket to downstream server
               └─> Returns: tool execution result
```

## External Integrations

| Integration | Protocol | Direction | Authentication |
|-------------|----------|-----------|----------------|
| **Claude Code** | HTTP hooks | Inbound | None (local) |
| **Gemini CLI** | HTTP hooks | Inbound | None (local) |
| **Codex CLI** | Notify script | Inbound | None (local) |
| **Claude API** | HTTP | Outbound | Subscription (Agent SDK) |
| **OpenAI API** | HTTP | Outbound | API Key (BYOK) |
| **Gemini API** | HTTP | Outbound | ADC credentials |
| **Downstream MCP** | HTTP/stdio/WS | Outbound | Per-server config |

## Configuration

### Main Config (`~/.gobby/config.yaml`)

```yaml
daemon_port: 8765
database_path: "~/.gobby/gobby.db"

websocket:
  enabled: true
  port: 8766
  ping_interval: 30

logging:
  level: info
  client: "~/.gobby/logs/gobby.log"

session_summary:
  enabled: true
  provider: claude
  model: claude-haiku-4-5

code_execution:
  enabled: true
  model: claude-sonnet-4-5

llm_providers:
  claude:
    models: claude-haiku-4-5,claude-sonnet-4-5,claude-opus-4-5
  codex:
    models: gpt-4o-mini,gpt-5
    auth_mode: subscription
```

### MCP Server Config (`~/.gobby/.mcp.json`)

```json
{
  "servers": [
    {
      "name": "context7",
      "transport": "http",
      "url": "https://mcp.context7.com/mcp",
      "project_id": "...",
      "enabled": true,
      "tools": [...]
    }
  ]
}
```

## Key Design Decisions

1. **Local-First**: All data stored in SQLite (`~/.gobby/gobby.db`), no cloud dependency
2. **CLI-Agnostic**: Adapter pattern normalizes different CLI hook formats to unified events
3. **Progressive Discovery**: MCP tools loaded on-demand to reduce token usage
4. **Multi-Provider LLM**: Abstraction layer supports Claude, Codex, Gemini, and LiteLLM
5. **Event-Driven Hooks**: 14 hook event types with central HookManager coordinator
6. **Thread-Safe Storage**: Thread-local SQLite connections for concurrent access
