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
- `src/mcp/server.py` - FastMCP server with daemon control tools (status, call_tool, list_tools, etc.)
- `src/mcp/stdio.py` - Stdio MCP server for Claude Code integration

**MCP Client Management:**

- `src/mcp/manager.py` - MCPClientManager handles connections to downstream MCP servers (context7, supabase, etc.) with multiple transport support (HTTP, stdio, WebSocket)
- `src/config/mcp.py` - MCP configuration management, loads from `~/.gobby/.mcp.json`
- `src/tools/filesystem.py` - Tool schema storage in `~/.gobby/tools/`

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
- `src/storage/migrations.py` - Database migration system

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
- Database: `~/.gobby/gobby.db`
- MCP server config: `~/.gobby/.mcp.json`
- Tool schemas: `~/.gobby/tools/<server_name>/<tool_name>.json`
- Logs: `~/.gobby/logs/`
- Session summaries: `~/.gobby/session_summaries/`

## MCP Tool Progressive Disclosure

The daemon implements progressive tool discovery to reduce token usage:

1. **list_tools()** - Returns lightweight tool metadata (name + brief description)
2. **get_tool_schema()** - Returns full inputSchema for a specific tool from filesystem cache
3. **call_tool()** - Executes the tool on the downstream MCP server

Tool schemas are cached in `~/.gobby/tools/` and metadata in `~/.gobby/.mcp.json`.

## Testing

Tests use pytest with asyncio support. Key test configuration in `pyproject.toml`:

- `asyncio_mode = "auto"` - Automatic async test detection
- Coverage threshold: 80%
- Markers: `slow`, `integration`, `e2e`


**Note**: This project uses [bd (beads)](https://github.com/steveyegge/beads)
for issue tracking. Use `bd` commands instead of markdown TODOs.
See AGENTS.md for workflow details.
