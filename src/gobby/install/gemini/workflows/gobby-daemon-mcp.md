---
description: How to manage the Gobby Daemon and discover/use downstream MCP tools
---

# Gobby Daemon & MCP Tool Workflow

This workflow guides you through managing the Gobby Daemon and using its proxied MCP tools.

## 1. Tool Discovery (Progressive Disclosure)

To save tokens, use this 3-step process instead of loading all schemas at once.

### Step A: List Servers & Tools

Discover what is available.

- **List Servers**: Call `mcp_list_mcp_servers()` to see connected downstream servers.
- **List Tools**: Call `mcp_list_tools(server="<server_name>")` to see tools on a specific server.
  - For downstream servers: `mcp_list_tools(server="context7")`
  - For internal tools: `mcp_list_tools(server="gobby-tasks")`

### Step B: Get Tool Schema

Get the full definition for a specific tool before using it.

- **Tool**: `mcp_get_tool_schema`
- **Arguments**:
  - `server_name`: Server name (e.g., "context7" or "gobby-tasks")
  - `tool_name`: The tool you want to use

### Step C: Call Tool

Execute the tool on the appropriate server.

- **Tool**: `mcp_call_tool`
- **Arguments**:
  - `server_name`: Server name (downstream or internal)
  - `tool_name`: The tool name
  - `arguments`: The actual arguments for the tool

**Routing:**

- `gobby-*` servers → handled locally by internal registries
- All others → proxied to downstream MCP servers

## 2. Internal Task Management

Use `gobby-tasks` for persistent task tracking.

```python
# Create a task
mcp_call_tool(
    server_name="gobby-tasks",
    tool_name="create_task",
    arguments={"title": "Fix auth bug", "priority": 1}
)

# Find ready work (no blocking dependencies)
mcp_call_tool(
    server_name="gobby-tasks",
    tool_name="list_ready_tasks",
    arguments={"limit": 5}
)

# Close a task
mcp_call_tool(
    server_name="gobby-tasks",
    tool_name="close_task",
    arguments={"task_id": "gt-abc123", "reason": "completed"}
)
```

Available task tools: `create_task`, `get_task`, `update_task`, `close_task`, `delete_task`, `list_tasks`, `add_dependency`, `remove_dependency`, `list_ready_tasks`, `list_blocked_tasks`, `sync_tasks`.

## 3. Daemon Management

**CRITICAL**: Always use these MCP tools. **NEVER** use CLI commands (like `gobby start`) directly.

| Operation | Tool Name | Description |
|-----------|-----------|-------------|
| **Check Status** | `mcp_status` | Check if daemon is running and healthy. |
| **Start Daemon** | `mcp_start` | Start the daemon if it's stopped. |
| **Restart** | `mcp_restart` | Restart the daemon (fix connection issues). |

**Example**:

1. Call `mcp_status`.
2. If `running` is false, call `mcp_start`.

> [!IMPORTANT]
> **Daemon Tools vs. Proxy Tools**
>
> - **Daemon Tools** (e.g., `status`, `restart`, `add_mcp_server`): Called directly.
> - **Proxy Tools** (via `call_tool`): For downstream servers and internal tools.

## 4. Troubleshooting

- **Tools failing with `FileNotFoundError`?**
  - Check `~/.gobby/logs/gobby.log`.
  - Verify MCP config has absolute paths to executables (e.g., `/opt/homebrew/bin/uv`).
- **Daemon unhealthy?**
  - Call `mcp_restart`.
