---
name: gobby-mcp
description: Use this skill when working with the gobby to manage the daemon lifecycle (start/stop/restart/status) or discover and use MCP tools. This skill prevents common mistakes like using CLI commands instead of MCP tools for daemon management, or querying live servers instead of using the progressive disclosure system for tool discovery.
---

# Gobby Daemon MCP

## Overview

The gobby exposes two categories of functionality through MCP tools:
1. **Daemon Management**: Tools to control the daemon itself (start, stop, restart, status, etc.)
2. **MCP Tool Discovery**: Progressive disclosure system to efficiently discover and use downstream MCP tools

This skill teaches the proper usage patterns for both categories and prevents common mistakes.

## Part 1: Daemon Management

### Critical Rule: Always Use MCP Tools, Never CLI Commands

The gobby provides MCP tools for all daemon management operations. **Always use these tools instead of Bash/CLI commands.**

### ❌ Common Mistakes (DON'T DO THIS)

```bash
# WRONG: Using CLI commands
Bash: uv run gobby restart
Bash: uv run gobby start
Bash: uv run gobby stop
Bash: pkill -f gobby

# WRONG: Using call_tool for daemon management
call_tool(server_name="gobby", tool_name="restart", ...)
```

**Why wrong**:
- CLI commands bypass the proper lifecycle management
- `call_tool` is for downstream MCP servers, not daemon management
- These approaches cause race conditions and state inconsistencies

### ✅ Correct Usage

Use the dedicated `mcp__gobby__*` tools:

| Operation | Correct Tool | Purpose |
|-----------|-------------|---------|
| Start daemon | `mcp__gobby__start` | Launch the daemon process |
| Stop daemon | `mcp__gobby__stop` | Gracefully shutdown daemon |
| Restart daemon | `mcp__gobby__restart` | Stop and start in one operation |
| Check status | `mcp__gobby__status` | Get daemon health and connection info |
| Authenticate | `mcp__gobby__login` | OAuth login to platform |

**Examples**:

```python
# Check if daemon is running
mcp__gobby__status()

# Restart the daemon
mcp__gobby__restart()

# Start if not running
status = mcp__gobby__status()
if not status['running']:
    mcp__gobby__start()
```

### Daemon Management vs. MCP Tool Calls

**Daemon Management Tools** (`mcp__gobby__*`):
- Control the daemon process itself
- Used for: start, stop, restart, status, login
- Direct tool invocation (not through call_tool)

**MCP Tool Calls** (`call_tool`):
- Call tools on downstream MCP servers
- Used for: context7, supabase, playwright, serena tools
- Goes through the daemon to proxy to downstream servers

### When to Use Each

```
Daemon Management:           MCP Tool Usage:
- Starting/stopping daemon   - Searching documentation (context7)
- Checking daemon status     - Querying database (supabase)
- Authentication             - Browser automation (playwright)
- Server management          - Code analysis (serena)
```

---

## Part 2: MCP Tool Discovery & Usage

### Progressive Disclosure System

The gobby implements progressive disclosure to reduce token usage:
- Tool metadata cached locally in `~/.gobby/.mcp.json` (lightweight)
- Full schemas stored in `~/.gobby/tools/` (loaded on-demand)

This enables a three-step workflow that's **96% more token-efficient** than loading all schemas upfront.

### Three-Step Workflow

#### Step 1: Discover Tools - `list_tools()`

**When**: At the start of a task when discovering what tools are available

**How**: Use the `list_tools` tool (exposed by gobby)

**Important**: This is a gobby tool, call it directly (not through call_tool proxy)

```python
# List all tools across all servers
result = mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="list_tools",
    arguments={}
)

# List tools for specific server
result = mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="list_tools",
    arguments={"server": "context7"}
)
```

**Returns**: Tool names + brief descriptions (not full schemas)

**Token cost**: ~1.5K tokens (vs. 40K for all schemas)

#### Step 2: Inspect Schema - `get_tool_schema()`

**When**: After discovering a tool, before calling it

**How**: Use the `get_tool_schema` tool (exposed by gobby)

**Important**: Reads from local filesystem (~/.gobby/tools/), not live server

```python
# Get full schema for a specific tool
result = mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="get_tool_schema",
    arguments={
        "server_name": "context7",
        "tool_name": "get-library-docs"
    }
)
```

**Returns**: Complete inputSchema with all properties, types, required fields

**Token cost**: ~2KB per tool (only when needed)

#### Step 3: Execute Tool - `call_tool()`

**When**: After understanding the schema and preparing correct arguments

**How**: Use `mcp__gobby__call_tool` with the downstream server name

```python
# Call a tool on a downstream MCP server
result = mcp__gobby__call_tool(
    server_name="context7",
    tool_name="get-library-docs",
    arguments={
        "context7CompatibleLibraryID": "/react/react",
        "topic": "hooks"
    }
)
```

**Important**: Now using the downstream server name (context7, supabase, etc.)

### Complete Example

```python
# Scenario: User asks "Find React hooks documentation"

# Step 1: Discover available tools
tools = mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="list_tools",
    arguments={"server": "context7"}
)
# Found: resolve-library-id, get-library-docs

# Step 2: Get schema for resolve-library-id
schema = mcp__gobby__call_tool(
    server_name="gobby",
    tool_name="get_tool_schema",
    arguments={
        "server_name": "context7",
        "tool_name": "resolve-library-id"
    }
)
# Learned: Takes 'packageName' parameter

# Step 3: Resolve React library ID
library_id = mcp__gobby__call_tool(
    server_name="context7",
    tool_name="resolve-library-id",
    arguments={"packageName": "react"}
)
# Got: "/react/react"

# Step 4: Get React documentation on hooks
docs = mcp__gobby__call_tool(
    server_name="context7",
    tool_name="get-library-docs",
    arguments={
        "context7CompatibleLibraryID": "/react/react",
        "topic": "hooks"
    }
)
# Got: React hooks documentation
```

### Best Practices

#### ✅ DO:
- Always start with `list_tools()` to discover available tools
- Use `get_tool_schema()` before calling unfamiliar tools
- Use the three-step workflow for efficient token usage
- Remember tool schemas are cached locally (no live queries needed)

#### ❌ DON'T:
- Skip tool discovery - always know what's available first
- Call tools without checking their schema
- Try to query live MCP servers for tool schemas (use get_tool_schema instead)
- Load all tool schemas upfront (defeats progressive disclosure)

### Understanding server_name Context

The `server_name` parameter changes meaning based on what you're calling:

| Tool | server_name value | Meaning |
|------|------------------|---------|
| `list_tools` | "gobby" | Calling gobby's own tool |
| `get_tool_schema` | "gobby" | Calling gobby's own tool |
| `call_tool` (for discovery tools) | "gobby" | Calling gobby's tool |
| `call_tool` (for downstream tools) | "context7", "supabase", etc. | Proxying to downstream server |

### Available Downstream Servers

Common downstream MCP servers accessible through gobby:
- **context7**: Documentation lookup and library resolution
- **supabase**: Database operations and schema management
- **playwright**: Browser automation and testing
- **serena**: Code analysis and symbol navigation

Use `list_tools()` to see all available servers and their tools.

### Token Efficiency Comparison

**Without progressive disclosure** (loading all schemas):
- 53 tool schemas = ~150KB = ~40,000 tokens

**With progressive disclosure** (this system):
- `list_tools()` = ~5KB = ~1,500 tokens (96% reduction)
- `get_tool_schema()` per tool = ~2KB = ~500 tokens (only when needed)

**Result**: Load only what you need, when you need it.

---

## Quick Reference

### Daemon Management
```python
# Check status
mcp__gobby__status()

# Restart daemon
mcp__gobby__restart()

# Authenticate
mcp__gobby__login()
```

### Tool Discovery
```python
# Discover → Inspect → Execute
list_tools(server="context7")
get_tool_schema(server_name="context7", tool_name="...")
call_tool(server_name="context7", tool_name="...", arguments={...})
```

### Remember
- Daemon management: Use `mcp__gobby__*` tools directly
- Tool discovery: Three-step workflow (list → inspect → execute)
- Never use CLI commands for daemon management
- Never query live servers for tool schemas (use local cache)
