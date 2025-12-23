# Gobby MCP Tools

Complete reference for all MCP tools exposed by the Gobby daemon.

## Overview

Gobby exposes MCP tools in two categories:

1. **Direct Tools** - Called directly on the Gobby MCP server
2. **Proxied Tools** - Called via `call_tool()` to internal or downstream servers

## Direct Tools

### Daemon Status

#### `status()`

Get current daemon status and health information.

**Returns:**

```json
{
  "status": "running",
  "uptime": "2h 15m 30s",
  "uptime_seconds": 8130,
  "pid": 12345,
  "port": 8765,
  "mcp_servers": [{"name": "context7", "state": "connected", ...}],
  "mcp_server_count": 3,
  "formatted_message": "..."
}
```

#### `list_mcp_servers()`

List all configured MCP servers and their connection status.

**Returns:**

```json
{
  "servers": [
    {"name": "context7", "project_id": "...", "description": "...", "connected": true}
  ]
}
```

### Tool Proxy

#### `call_tool(server_name, tool_name, arguments?)`

Execute a tool on a connected MCP server or internal registry.

**Parameters:**

| Name          | Type   | Required | Description                                   |
| ------------- | ------ | -------- | --------------------------------------------- |
| `server_name` | string | Yes      | Server name (e.g., "context7", "gobby-tasks") |
| `tool_name`   | string | Yes      | Name of the tool to execute                   |
| `arguments`   | object | No       | Tool-specific arguments                       |

**Routing:**

- `gobby-*` servers → handled locally by internal registries
- All others → proxied to downstream MCP servers

**Example:**

```python
# Call downstream server tool
call_tool("context7", "get-library-docs", {"libraryId": "/react/react"})

# Call internal task tool
call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "priority": 1})
```

#### `list_tools(server?)`

List tools with lightweight metadata for progressive disclosure.

**Parameters:**

| Name     | Type   | Required | Description                                   |
| -------- | ------ | -------- | --------------------------------------------- |
| `server` | string | No       | Server name. If omitted, returns all servers. |

**Returns:**

```json
{
  "success": true,
  "server": "gobby-tasks",
  "tools": [
    {"name": "create_task", "brief": "Create a new task in the current project."},
    {"name": "list_tasks", "brief": "List tasks with optional filters."}
  ]
}
```

#### `get_tool_schema(server_name, tool_name)`

Get full inputSchema for a specific tool.

**Parameters:**

| Name          | Type   | Required | Description |
| ------------- | ------ | -------- | ----------- |
| `server_name` | string | Yes      | Server name |
| `tool_name`   | string | Yes      | Tool name   |

**Returns:**

```json
{
  "success": true,
  "server": "gobby-tasks",
  "tool": {
    "name": "create_task",
    "description": "Create a new task in the current project.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "title": {"type": "string", "description": "Task title"},
        "priority": {"type": "integer", "default": 2}
      },
      "required": ["title"]
    }
  }
}
```

#### `read_mcp_resource(server_name, resource_uri)`

Read a resource from a downstream MCP server.

**Parameters:**

| Name          | Type   | Required | Description         |
| ------------- | ------ | -------- | ------------------- |
| `server_name` | string | Yes      | Server name         |
| `resource_uri`| string | Yes      | URI of the resource |

### Server Management

#### `add_mcp_server(...)`

Add a new MCP server to the current project.

**Parameters:**

| Name        | Type    | Required    | Description                     |
| ----------- | ------- | ----------- | ------------------------------- |
| `name`      | string  | Yes         | Unique server name              |
| `transport` | string  | Yes         | "http", "stdio", or "websocket" |
| `url`       | string  | For http/ws | Server URL                      |
| `headers`   | object  | No          | Custom HTTP headers             |
| `command`   | string  | For stdio   | Command to run                  |
| `args`      | array   | No          | Command arguments               |
| `env`       | object  | No          | Environment variables           |
| `enabled`   | boolean | No          | Whether enabled (default: true) |

**Example (HTTP):**

```python
add_mcp_server(
    name="context7",
    transport="http",
    url="https://mcp.context7.com/mcp",
    headers={"CONTEXT7_API_KEY": "ctx7sk-..."}
)
```

**Example (stdio):**

```python
add_mcp_server(
    name="weather",
    transport="stdio",
    command="uv",
    args=["run", "weather_server.py"],
    env={"API_KEY": "secret"}
)
```

#### `remove_mcp_server(name)`

Remove an MCP server from the current project.

**Parameters:**

| Name   | Type   | Required | Description           |
| ------ | ------ | -------- | --------------------- |
| `name` | string | Yes      | Server name to remove |

#### `import_mcp_server(...)`

Import MCP servers from various sources.

**Parameters (one required):**

| Name           | Type   | Description                                      |
| -------------- | ------ | ------------------------------------------------ |
| `from_project` | string | Source project name to import from               |
| `servers`      | array  | Specific server names to import (all if omitted) |
| `github_url`   | string | GitHub repository URL                            |
| `query`        | string | Natural language search query                    |

**Returns:**

- Success: `{"success": true, "imported": ["server1", "server2"]}`
- Needs secrets: `{"status": "needs_configuration", "config": {...}, "missing": ["API_KEY"]}`

### Code Execution

#### `execute_code(code, language?, context?, timeout?)`

Execute code in Claude's sandbox.

**Parameters:**

| Name       | Type    | Required | Description                   |
| ---------- | ------- | -------- | ----------------------------- |
| `code`     | string  | Yes      | Code to execute               |
| `language` | string  | No       | Language (default: "python")  |
| `context`  | string  | No       | Instructions for Claude       |
| `timeout`  | integer | No       | Max execution time in seconds |

**Returns:**

```json
{
  "success": true,
  "result": "333283335000",
  "language": "python",
  "execution_time": 1.25
}
```

#### `process_large_dataset(data, operation, parameters?, timeout?)`

Process large datasets for token optimization.

**Parameters:**

| Name         | Type         | Required | Description                            |
| ------------ | ------------ | -------- | -------------------------------------- |
| `data`       | array/object | Yes      | Dataset to process                     |
| `operation`  | string       | Yes      | Natural language operation description |
| `parameters` | object       | No       | Parameters for the operation           |
| `timeout`    | integer      | No       | Max execution time in seconds          |

**Returns:**

```json
{
  "success": true,
  "result": [...],
  "original_size": 10000,
  "processed_size": 100,
  "reduction_percent": 99.0,
  "execution_time": 2.5
}
```

### AI-Powered

#### `recommend_tools(task_description, agent_id?)`

Get intelligent tool recommendations for a task.

**Parameters:**

| Name               | Type   | Required | Description                      |
| ------------------ | ------ | -------- | -------------------------------- |
| `task_description` | string | Yes      | What you're trying to accomplish |
| `agent_id`         | string | No       | Agent profile ID for filtering   |

**Returns:**

```json
{
  "success": true,
  "task": "Find React hooks documentation",
  "recommendation": "I recommend using context7 tools...",
  "available_servers": ["context7", "supabase"],
  "total_tools": 25
}
```

### Session Hooks

#### `call_hook(hook_type, params?, source?)`

Trigger session hooks for non-Claude-Code CLIs.

**Parameters:**

| Name        | Type   | Required | Description                         |
| ----------- | ------ | -------- | ----------------------------------- |
| `hook_type` | string | Yes      | Hook type (see below)               |
| `params`    | object | No       | Hook-specific parameters            |
| `source`    | string | No       | CLI source (e.g., "Codex", "Gemini")|

**Hook Types:**

- `SessionStart` - Register session, restore context
- `PromptSubmit` - Synthesize/update session title
- `Stop` - Mark session as paused
- `SessionEnd` - Generate summary

**Example:**

```python
call_hook(
    hook_type="SessionStart",
    params={"session_id": "codex-abc123", "source": "startup"},
    source="Codex"
)
```

### Codex Integration (Optional)

Available when Codex client is configured.

#### `codex(prompt, thread_id?, ...)`

Run Codex with automatic session tracking.

**Parameters:**

| Name              | Type   | Required | Description             |
| ----------------- | ------ | -------- | ----------------------- |
| `prompt`          | string | Yes      | User prompt             |
| `thread_id`       | string | No       | Continue existing thread|
| `cwd`             | string | No       | Working directory       |
| `model`           | string | No       | Model override          |
| `sandbox`         | string | No       | Sandbox mode            |
| `approval_policy` | string | No       | Approval policy         |

#### `codex_list_threads(limit?, cursor?)`

List Codex conversation threads.

#### `codex_archive_thread(thread_id)`

Archive a Codex thread.

---

## Internal Tools (via `call_tool`)

Internal tools are accessed via `call_tool(server_name="gobby-*", ...)`.

### Task Management (`gobby-tasks`)

17 tools for persistent task tracking with dependencies and git sync.

#### CRUD Operations

| Tool          | Description                       |
| ------------- | --------------------------------- |
| `create_task` | Create a new task                 |
| `get_task`    | Get task details with dependencies|
| `update_task` | Update task fields                |
| `close_task`  | Close a task with reason          |
| `delete_task` | Delete a task (optional cascade)  |
| `list_tasks`  | List tasks with filters           |

#### Dependency Management

| Tool                     | Description                   |
| ------------------------ | ----------------------------- |
| `add_dependency`         | Add dependency between tasks  |
| `remove_dependency`      | Remove a dependency           |
| `get_dependency_tree`    | Get blockers/blocking tasks   |
| `check_dependency_cycles` | Detect circular dependencies |

#### Ready Work

| Tool                 | Description                      |
| -------------------- | -------------------------------- |
| `list_ready_tasks`    | List unblocked tasks             |
| `list_blocked_tasks`  | List blocked tasks with blockers |

#### Session Integration

| Tool                   | Description                      |
| ---------------------- | -------------------------------- |
| `link_task_to_session` | Link task to session             |
| `get_session_tasks`    | Get tasks for a session          |
| `get_task_sessions`    | Get sessions that touched a task |

#### Git Sync

| Tool              | Description                  |
| ----------------- | ---------------------------- |
| `sync_tasks`      | Import/export tasks to JSONL |
| `get_sync_status` | Get sync status              |

### Example: Task Workflow

```python
# 1. List available task tools
list_tools(server="gobby-tasks")

# 2. Get schema for create_task
get_tool_schema(server_name="gobby-tasks", tool_name="create_task")

# 3. Create a task
call_tool("gobby-tasks", "create_task", {
    "title": "Implement authentication",
    "priority": 1,
    "task_type": "feature"
})

# 4. Find ready work
call_tool("gobby-tasks", "list_ready_tasks", {"limit": 5})

# 5. Claim a task
call_tool("gobby-tasks", "update_task", {
    "task_id": "gt-abc123",
    "status": "in_progress"
})

# 6. Close when done
call_tool("gobby-tasks", "close_task", {
    "task_id": "gt-abc123",
    "reason": "completed"
})
```

---

## Progressive Disclosure Pattern

For token efficiency, use the three-step workflow:

1. **Discover**: `list_tools(server="...")` - lightweight metadata (~1.5K tokens)
2. **Inspect**: `get_tool_schema(server_name, tool_name)` - full schema (~2K tokens per tool)
3. **Execute**: `call_tool(server_name, tool_name, arguments)` - run the tool

This pattern is **96% more token-efficient** than loading all schemas upfront.

---

## Error Handling

All tools return a consistent structure:

**Success:**

```json
{
  "success": true,
  "result": ...
}
```

**Failure:**

```json
{
  "success": false,
  "error": "Error message",
  "error_type": "ValueError"
}
```

---

## See Also

- [CLI_COMMANDS.md](CLI_COMMANDS.md) - CLI command reference
- [README.md](README.md) - Project overview
- [docs/plans/TASKS.md](docs/plans/TASKS.md) - Task system design
