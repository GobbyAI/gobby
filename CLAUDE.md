# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Gobby is a local daemon that unifies Claude Code, Gemini CLI, and Codex through a hook interface for session tracking, and provides an MCP proxy with progressive tool discovery for efficient access to downstream servers.

## Development Commands

```bash
# Install dependencies (Python 3.11+)
uv sync

# Run the daemon in development
uv run gobby start --verbose

# Stop the daemon
uv run gobby stop

# Check daemon status
uv run gobby status

# Install hooks to current project for all detected CLIs
uv run gobby install

# Install hooks for specific CLI
uv run gobby install --claude
uv run gobby install --gemini
uv run gobby install --codex

# Initialize a new project
uv run gobby init

# Run tests
uv run pytest

# Run single test file
uv run pytest tests/test_example.py -v

# Run tests with specific marker
uv run pytest -m "not slow"

# Linting and formatting
uv run ruff check src/
uv run ruff format src/

# Type checking
uv run mypy src/
```

## Architecture

### Core Components

**Daemon Entry Points:**

- `src/cli.py` - Click-based CLI commands (`gobby start`, `gobby stop`, etc.)
- `src/runner.py` - Main daemon process that runs HTTP server, WebSocket server, and MCP connections

**Server Layer:**

- `src/servers/http.py` - FastAPI HTTP server with REST endpoints and MCP server
- `src/servers/websocket.py` - WebSocket server for real-time communication
- `src/mcp_proxy/server.py` - FastMCP server with daemon control tools (status, call_tool, list_tools, etc.)
- `src/mcp_proxy/stdio.py` - Stdio MCP server wrapper

**MCP Proxy & Internal Tools:**

- `src/mcp_proxy/manager.py` - MCPClientManager handles connections to downstream MCP servers (context7, supabase, etc.) with multiple transport support (HTTP, stdio, WebSocket)
- `src/mcp_proxy/tools/internal.py` - InternalToolRegistry and InternalRegistryManager for `gobby-*` prefixed servers
- `src/mcp_proxy/tools/tasks.py` - Task tool registry (create_task, list_ready_tasks, etc.)
- `src/config/mcp.py` - MCP configuration management
- `src/storage/mcp.py` - LocalMCPManager for MCP server and tool storage in SQLite

**Hook System:**

- `src/hooks/hook_manager.py` - Central coordinator that delegates to subsystems
- `src/hooks/events.py` - HookEvent and HookEventType definitions
- `src/install/claude/hooks/hook_dispatcher.py` - Claude Code hook dispatcher script
- `src/install/gemini/hooks/hook_dispatcher.py` - Gemini CLI hook dispatcher script

**Session Management:**

- `src/sessions/manager.py` - SessionManager for registration, lookup, and status updates
- `src/sessions/summary.py` - SummaryGenerator for LLM-powered session summaries
- `src/sessions/transcripts/` - Transcript parsers (claude.py, base.py)

**Storage:**

- `src/storage/database.py` - SQLite database manager with thread-local connections
- `src/storage/sessions.py` - LocalSessionManager for session CRUD operations
- `src/storage/projects.py` - LocalProjectManager for project CRUD operations
- `src/storage/tasks.py` - LocalTaskManager for task CRUD operations
- `src/storage/task_dependencies.py` - TaskDependencyManager for dependency relationships
- `src/storage/session_tasks.py` - SessionTaskManager for session-task linking
- `src/storage/migrations.py` - Database migration system
- `src/sync/tasks.py` - TaskSyncManager for JSONL import/export

**Configuration:**

- `src/config/app.py` - DaemonConfig with YAML-based configuration (`~/.gobby/config.yaml`)
- Configuration hierarchy: CLI args > YAML file > Defaults

**LLM Providers:**

- `src/llm/service.py` - LLMService for multi-provider management
- `src/llm/claude.py`, `src/llm/gemini.py`, `src/llm/codex.py`, `src/llm/litellm.py` - Provider implementations

### Data Flow

1. **Hook Invocation**: CLI (Claude Code/Gemini/Codex) triggers hook via dispatcher script
2. **Hook Processing**: HookManager coordinates subsystems (DaemonClient, SessionManager, SummaryGenerator)
3. **Session Tracking**: Sessions stored in SQLite (`~/.gobby/gobby.db`)
4. **MCP Proxy**: Requests flow through MCPClientManager to downstream servers

### Key File Locations

- Config: `~/.gobby/config.yaml`
- Database: `~/.gobby/gobby.db` (sessions, projects, tasks, MCP servers, tools)
- Logs: `~/.gobby/logs/`
- Session summaries: `~/.gobby/session_summaries/`
- Project config: `.gobby/project.json`
- Task sync: `.gobby/tasks.jsonl`, `.gobby/tasks_meta.json`

## MCP Tool Progressive Disclosure

The daemon implements progressive tool discovery to reduce token usage:

1. **list_tools()** - Returns lightweight tool metadata (name + brief description)
2. **get_tool_schema()** - Returns full inputSchema for a specific tool from SQLite cache
3. **call_tool()** - Executes the tool on the appropriate server

Tool schemas are cached in SQLite (`mcp_servers` and `tools` tables) via `LocalMCPManager`.

## Internal Tool Registry Pattern

Internal tools use a `gobby-*` prefix for server names and are handled locally:

```python
# List internal task tools
list_tools(server="gobby-tasks")

# Get schema for a specific tool
get_tool_schema(server_name="gobby-tasks", tool_name="create_task")

# Call an internal tool
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={"title": "Fix bug"})
```

**Routing logic:**

- `gobby-*` servers → handled locally by `InternalRegistryManager`
- All others → proxied to downstream MCP servers via `MCPClientManager`

**Available internal servers:**

- `gobby-tasks` - Task CRUD, dependencies, ready work detection, git sync

## Task Management with gobby-tasks

Use the `gobby-tasks` MCP tools for persistent task tracking (requires daemon running):

1. **Start of session**: Call `list_ready_tasks` to see unblocked work
2. **New requests**: Create tasks with `create_task(title="...", description="...")`
3. **Complex work**: Break into subtasks with `parent_task_id` parameter
4. **Track progress**: Use `update_task` to change status (`open` -> `in_progress` -> `done`)
5. **End of session**: Close completed tasks with `close_task(task_id="...")`

```python
# Example MCP tool calls via daemon
call_tool(server_name="gobby-tasks", tool_name="list_ready_tasks", arguments={})
call_tool(server_name="gobby-tasks", tool_name="create_task", arguments={"title": "Fix auth bug"})
```

If tools fail, check daemon status: `uv run gobby status`

## Testing

Tests use pytest with asyncio support. Key test configuration in `pyproject.toml`:

- `asyncio_mode = "auto"` - Automatic async test detection
- Coverage threshold: 80%
- Markers: `slow`, `integration`, `e2e`
