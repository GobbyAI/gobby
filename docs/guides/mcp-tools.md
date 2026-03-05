# Gobby MCP Tools

Complete reference for all MCP tools exposed by the Gobby daemon.

## Overview

Gobby exposes **145+ internal tools** across 11 tool registries, plus direct tools on the main MCP server. Tools are accessed via:

1. **Direct Tools** - Called directly on the Gobby MCP server
2. **Internal Tools** - Called via `call_tool()` to `gobby-*` registries

## Progressive Discovery Pattern

For token efficiency, use the three-step workflow **on-demand** when you need a tool:

```python
# 1. Discover - lightweight metadata (~100 tokens/tool)
list_tools(server_name="gobby-tasks")

# 2. Inspect - full schema when needed (~500 tokens/tool)
get_tool_schema(server_name="gobby-tasks", tool_name="create_task")

# 3. Execute - run the tool
call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "session_id": "..."})
```

This pattern is **96% more token-efficient** than loading all schemas upfront.

> **Note:** You don't need to call `list_mcp_servers()` or `list_skills()` at session start. Core skills with `alwaysApply: true` are automatically injected via workflows. Use discovery tools on-demand when you need to find a specific tool or skill.

---

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
  "port": 60887,
  "mcp_servers": [{"name": "context7", "state": "connected"}],
  "mcp_server_count": 3
}
```

#### `list_mcp_servers()`

List all configured MCP servers and their connection status.

**Returns:**

```json
{
  "servers": [
    {"name": "context7", "state": "connected", "transport": "http"},
    {"name": "gobby-tasks", "state": "connected", "transport": "internal"}
  ],
  "total_count": 12,
  "connected_count": 11
}
```

### Tool Proxy

#### `call_tool(server_name, tool_name, arguments?)`

Execute a tool on a connected MCP server or internal registry.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `server_name` | string | Yes | Server name (e.g., "context7", "gobby-tasks") |
| `tool_name` | string | Yes | Name of the tool to execute |
| `arguments` | object | No | Tool-specific arguments |

**Routing:**

- `gobby-*` servers → handled locally by internal registries
- All others → proxied to downstream MCP servers

**Example:**

```python
# Call downstream server tool
call_tool("context7", "get-library-docs", {"libraryId": "/react/react"})

# Call internal task tool
call_tool("gobby-tasks", "create_task", {
    "title": "Fix bug",
    "priority": 1,
    "session_id": "<your_session_id>"
})
```

#### `list_tools(server_name?)`

List tools with lightweight metadata for progressive discovery.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `server_name` | string | No | Server name. If omitted, returns all servers. |

**Returns:**

```json
{
  "status": "success",
  "server": "gobby-tasks",
  "tools": [
    {"name": "create_task", "brief": "Create a new task in the current project."},
    {"name": "list_tasks", "brief": "List tasks with optional filters."}
  ],
  "tool_count": 52
}
```

#### `get_tool_schema(server_name, tool_name)`

Get full inputSchema for a specific tool.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `server_name` | string | Yes | Server name |
| `tool_name` | string | Yes | Tool name |

**Returns:**

```json
{
  "success": true,
  "tool": {
    "name": "create_task",
    "description": "Create a new task in the current project.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "session_id": {"type": "string", "description": "Required - your session ID"}
      },
      "required": ["title", "session_id"]
    }
  }
}
```

### Server Management

#### `add_mcp_server(...)`

Add a new MCP server to the current project.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `name` | string | Yes | Unique server name |
| `transport` | string | Yes | "http", "stdio", or "websocket" |
| `url` | string | For http/ws | Server URL |
| `headers` | object | No | Custom HTTP headers |
| `command` | string | For stdio | Command to run |
| `args` | array | No | Command arguments |
| `env` | object | No | Environment variables |
| `enabled` | boolean | No | Whether enabled (default: true) |

**Example (HTTP):**

```python
add_mcp_server(
    name="context7",
    transport="http",
    url="https://mcp.context7.com/mcp"
)
```

**Example (stdio):**

```python
add_mcp_server(
    name="weather",
    transport="stdio",
    command="uv",
    args=["run", "weather_server.py"]
)
```

#### `remove_mcp_server(name)`

Remove an MCP server from the current project.

#### `import_mcp_server(...)`

Import MCP servers from various sources.

| Parameter | Type | Description |
| :--- | :--- | :--- |

| `from_project` | string | Source project name to import from |
| `servers` | array | Specific server names (all if omitted) |
| `github_url` | string | GitHub repository URL |
| `query` | string | Natural language search query |

### AI-Powered Tools

#### `recommend_tools(task_description, agent_id?, search_mode?)`

Get intelligent tool recommendations for a task.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `task_description` | string | Yes | What you're trying to accomplish |
| `agent_id` | string | No | Agent profile ID for filtering |
| `search_mode` | string | No | "llm" (default), "semantic", or "hybrid" |

#### `search_tools(query, top_k?, min_similarity?, server_name?)`

Search for tools using semantic similarity.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `query` | string | Yes | Natural language description |
| `top_k` | integer | No | Max results (default: 10) |
| `min_similarity` | float | No | Minimum threshold 0-1 |
| `server_name` | string | No | Filter by server |

### Session Hooks

#### `call_hook(hook_type, params?, source?)`

Trigger session hooks for non-Claude-Code CLIs.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |

| `hook_type` | string | Yes | Hook type (see below) |
| `params` | object | No | Hook-specific parameters |
| `source` | string | No | CLI source (e.g., "Codex", "Gemini") |

**Hook Types:**

- `SessionStart` - Register session, restore context
- `PromptSubmit` - Synthesize/update session title
- `Stop` - Mark session as paused
- `SessionEnd` - Generate summary

---

## Internal Tool Registries

Internal tools are accessed via `call_tool(server_name="gobby-*", ...)`.

### Quick Reference

| Registry | Tools | Purpose |
| :--- | :--- | :--- |

| `gobby-tasks` | 42 | Task management, dependencies, validation, sync |
| `gobby-orchestration` | 11 | Task orchestration, agent spawning, monitoring, waiting |
| `gobby-sessions` | 11 | Session lifecycle, handoffs, messages |
| `gobby-memory` | 11 | Persistent memory storage and retrieval |
| `gobby-workflows` | 12 | Workflow engine, step transitions |
| `gobby-agents` | 15 | Subagent spawning and management |
| `gobby-worktrees` | 13 | Git worktree management |
| `gobby-skills` | 6 | Skill discovery and management |
| `gobby-merge` | 5 | AI-powered merge conflict resolution |
| `gobby-clones` | 6 | Git clone management |
| `gobby-metrics` | 10 | Tool metrics and usage tracking |
| `gobby-hub` | 4 | Cross-project queries |

---

## Task Management (`gobby-tasks`)

42 tools for persistent task tracking with dependencies, validation, and sync.

### CRUD Operations

| Tool | Description |
| :--- | :--- |

| `create_task` | Create a new task. Use `claim=true` to auto-assign. |
| `get_task` | Get task details. Accepts `#N`, path, or UUID. |
| `update_task` | Update task fields |
| `close_task` | Close task. Pass `commit_sha` to link and close. |
| `reopen_task` | Reopen a closed task |
| `delete_task` | Delete task. `cascade=true` deletes subtasks. |
| `list_tasks` | List tasks with filters |
| `claim_task` | Claim task for your session |

### Labels

| Tool | Description |
| :--- | :--- |

| `add_label` | Add a label to a task |
| `remove_label` | Remove a label from a task |

### Dependencies

| Tool | Description |
| :--- | :--- |

| `add_dependency` | Add dependency between tasks |
| `remove_dependency` | Remove a dependency |
| `get_dependency_tree` | Get blockers/blocking tree |
| `check_dependency_cycles` | Detect circular dependencies |

### Ready Work

| Tool | Description |
| :--- | :--- |

| `list_ready_tasks` | Tasks with no unresolved blockers |
| `list_blocked_tasks` | Tasks waiting on others |
| `suggest_next_task` | AI suggests best next task |

### Session Integration

| Tool | Description |
| :--- | :--- |

| `link_task_to_session` | Associate task with session |
| `get_session_tasks` | Tasks linked to a session |
| `get_task_sessions` | Sessions that touched a task |

### Expansion

| Tool | Description |
| :--- | :--- |

| `save_expansion_spec` | Save expansion spec for later execution |
| `execute_expansion` | Execute saved expansion atomically |
| `get_expansion_spec` | Check for pending expansion |

### Validation

| Tool | Description |
| :--- | :--- |

| `validate_task` | Validate task completion (auto-gathers git context) |
| `get_validation_status` | Get validation details |
| `reset_validation_count` | Reset failure count for retry |
| `get_validation_history` | Full validation history with iterations |
| `get_recurring_issues` | Analyze recurring validation issues |
| `clear_validation_history` | Clear all validation history |
| `de_escalate_task` | Return escalated task to open status |
| `generate_validation_criteria` | Generate criteria using AI |
| `run_fix_attempt` | Spawn fix agent for validation issues |
| `validate_and_fix` | Run validation loop with auto-fix |

### Git Integration

| Tool | Description |
| :--- | :--- |

| `sync_tasks` | Import/export tasks to JSONL |
| `get_sync_status` | Get sync status |
| `link_commit` | Link git commit to task |
| `unlink_commit` | Unlink commit from task |
| `auto_link_commits` | Auto-detect commits mentioning task IDs |
| `get_task_diff` | Get combined diff for linked commits |

### Search

| Tool | Description |
| :--- | :--- |

| `search_tasks` | TF-IDF semantic search |
| `reindex_tasks` | Rebuild search index |

### Example: Task Workflow

```python
# 1. Find ready work
call_tool("gobby-tasks", "list_ready_tasks", {"limit": 5})

# 2. Create and claim a task
call_tool("gobby-tasks", "create_task", {
    "title": "Implement authentication",
    "priority": 1,
    "task_type": "feature",
    "session_id": "<your_session_id>",
    "claim": True
})

# 3. Add validation criteria
call_tool("gobby-tasks", "generate_validation_criteria", {
    "task_id": "#123"
})

# 4. Close when done (auto-validates)
call_tool("gobby-tasks", "close_task", {
    "task_id": "#123",
    "reason": "completed",
    "commit_sha": "abc123"
})
```

---

## Task Orchestration (`gobby-orchestration`)

11 tools for automated task orchestration, agent spawning, monitoring, and waiting.

### Orchestration

| Tool | Description |
| :--- | :--- |

| `orchestrate_ready_tasks` | Spawn agents for ready subtasks |
| `get_orchestration_status` | Get orchestration status for parent |
| `poll_agent_status` | Poll running agents, update tracking |
| `spawn_review_agent` | Spawn review agent for completed task |
| `process_completed_agents` | Route completed agents to review/cleanup |
| `approve_and_cleanup` | Approve reviewed task, cleanup worktree |
| `cleanup_reviewed_worktrees` | Clean up worktrees for reviewed agents |
| `cleanup_stale_worktrees` | Clean up inactive worktrees |

### Waiting

| Tool | Description |
| :--- | :--- |

| `wait_for_task` | Block until task completes |
| `wait_for_any_task` | Wait for first of multiple tasks |
| `wait_for_all_tasks` | Wait for all tasks to complete |

---

## Session Management (`gobby-sessions`)

11 tools for session lifecycle and context management.

| Tool | Description |
| :--- | :--- |

| `get_current_session` | Get YOUR current session ID (correct way to look up session) |
| `get_session` | Get session details by ID. Accepts `#N`, UUID, or prefix. |
| `list_sessions` | List sessions with filters (NOT for finding your session) |
| `session_stats` | Get session statistics for project |
| `get_session_messages` | Get messages for a session |
| `search_messages` | Search messages using FTS |
| `get_session_commits` | Get git commits made during session |
| `get_handoff_context` | Get handoff context (compact_markdown) |
| `create_handoff` | Create handoff context from transcript |
| `pickup` | Restore context from previous session's handoff |
| `mark_loop_complete` | Mark autonomous loop as complete |

### Example: Session Handoff

```python
# 1. Create handoff before ending session
call_tool("gobby-sessions", "create_handoff", {
    "session_id": "<current_session_id>"
})

# 2. In new session, pick up where you left off
call_tool("gobby-sessions", "pickup", {
    "from_session": "#42"
})
```

---

## Memory System (`gobby-memory`)

11 tools for persistent knowledge across sessions.

| Tool | Description |
| :--- | :--- |

| `create_memory` | Create a new memory |
| `search_memories` | Search with query and tag filters |
| `list_memories` | List all memories with filters |
| `get_memory` | Get specific memory by ID |
| `get_related_memories` | Get memories via cross-references |
| `update_memory` | Update content, importance, or tags |
| `delete_memory` | Delete a memory |
| `remember_with_image` | Create memory from image (uses LLM) |
| `remember_screenshot` | Create memory from base64 screenshot |
| `memory_stats` | Get memory system statistics |
| `export_memory_graph` | Export as interactive HTML graph |

### Example: Memory Operations

```python
# Store a memory
call_tool("gobby-memory", "create_memory", {
    "content": "This project uses pytest fixtures in conftest.py",
    "memory_type": "fact",
    "importance": 0.8,
    "tags": ["testing", "pytest"]
})

# Search with tag filtering
call_tool("gobby-memory", "search_memories", {
    "query": "testing setup",
    "tags_all": ["testing"],
    "tags_none": ["deprecated"]
})
```

---

## Workflow Engine (`gobby-workflows`)

Tools for rule management, variable control, and step-based workflow management.

### Rule Tools

| Tool | Description |
| :--- | :--- |
| `list_rules` | List rules with optional event/group filter |
| `list_rule_groups` | List available rule groups |
| `get_rule_detail` | Get full rule definition |
| `toggle_rule` | Enable/disable a rule for the session |

### Workflow and Variable Tools

| Tool | Description |
| :--- | :--- |
| `list_workflows` | List available workflow definitions |
| `get_workflow` | Get workflow details |
| `activate_workflow` | Activate on-demand workflow for session |
| `end_workflow` | End active workflow |
| `get_workflow_status` | Get current step and state |
| `request_step_transition` | Request transition to different step |
| `set_variable` | Set session-scoped variable |
| `get_variable` | Get variable value(s) |
| `import_workflow` | Import workflow from file |
| `reload_cache` | Clear workflow cache after edits |

### Example: Rule Management

```python
# List rules by event
call_tool("gobby-workflows", "list_rules", {
    "event": "before_tool"
})

# Toggle a rule for the session
call_tool("gobby-workflows", "toggle_rule", {
    "name": "require-task",
    "enabled": false
})
```

### Example: Workflow Activation

```python
# Activate a workflow
call_tool("gobby-workflows", "activate_workflow", {
    "workflow_name": "developer",
    "session_id": "<your_session_id>"
})

# Check current step
call_tool("gobby-workflows", "get_workflow_status", {
    "session_id": "<your_session_id>"
})
```

---

## Agent Management (`gobby-agents`)

15 tools for subagent spawning, management, and inter-agent messaging.

| Tool | Description |
| :--- | :--- |
| `spawn_agent` | Spawn subagent with isolation options |
| `get_agent_result` | Get result of completed agent |
| `list_agents` | List agent runs for session |
| `stop_agent` | Stop agent (marks cancelled) |
| `kill_agent` | Kill agent process |
| `can_spawn_agent` | Check if spawning is allowed |
| `list_running_agents` | List currently running agents |
| `get_running_agent` | Get in-memory process state |
| `unregister_agent` | Remove from running registry |
| `running_agent_stats` | Get running agent statistics |
| `send_message` | P2P message between sessions (same project) |
| `send_command` | Send command from ancestor to descendant |
| `complete_command` | Complete command, clear variables, send result |
| `deliver_pending_messages` | Fetch and mark undelivered messages |
| `activate_command` | Activate pending command, set session variables |

### Example: Agent Spawning

```python
# Spawn agent in worktree isolation
call_tool("gobby-agents", "spawn_agent", {
    "prompt": "Implement the login feature",
    "task_id": "#123",
    "session_id": "<your_session_id>",
    "isolation": "worktree"
})

# Check if can spawn
call_tool("gobby-agents", "can_spawn_agent", {
    "session_id": "<your_session_id>"
})
```

### Example: Inter-Agent Messaging

```python
# P2P message
call_tool("gobby-agents", "send_message", {
    "from_session": "<your_session>",
    "to_session": "<target_session>",
    "content": "Task completed. All tests pass."
})

# Command coordination
call_tool("gobby-agents", "send_command", {
    "from_session": "<parent>",
    "to_session": "<child>",
    "command_text": "Run test suite",
    "allowed_tools": ["Bash", "Read"]
})
```

---

## Worktree Management (`gobby-worktrees`)

13 tools for git worktree parallel development.

| Tool | Description |
| :--- | :--- |

| `create_worktree` | Create new git worktree |
| `get_worktree` | Get worktree details |
| `list_worktrees` | List worktrees with filters |
| `claim_worktree` | Claim ownership for agent session |
| `release_worktree` | Release ownership |
| `delete_worktree` | Delete worktree (git + DB) |
| `sync_worktree` | Sync with main branch |
| `mark_worktree_merged` | Mark as merged (ready for cleanup) |
| `detect_stale_worktrees` | Find inactive worktrees |
| `cleanup_stale_worktrees` | Delete stale worktrees |
| `get_worktree_stats` | Get project worktree statistics |
| `get_worktree_by_task` | Get worktree linked to task |
| `link_task_to_worktree` | Link task to existing worktree |

---

## Merge Operations (`gobby-merge`)

5 tools for AI-powered merge conflict resolution.

| Tool | Description |
| :--- | :--- |

| `merge_start` | Start merge with AI conflict resolution |
| `merge_status` | Get merge status and conflict details |
| `merge_resolve` | Resolve specific conflict (optionally with AI) |
| `merge_apply` | Apply resolved conflicts, complete merge |
| `merge_abort` | Abort merge, restore previous state |

### Example: Merge Workflow

```python
# Start merge
call_tool("gobby-merge", "merge_start", {
    "source_branch": "feature/login",
    "target_branch": "main"
})

# Check status
call_tool("gobby-merge", "merge_status", {})

# Resolve conflict with AI
call_tool("gobby-merge", "merge_resolve", {
    "file_path": "src/auth.py",
    "use_ai": True
})

# Apply and complete
call_tool("gobby-merge", "merge_apply", {})
```

---

## Clone Management (`gobby-clones`)

6 tools for git clone-based parallel development.

| Tool | Description |
| :--- | :--- |

| `create_clone` | Create new git clone |
| `get_clone` | Get clone by ID |
| `list_clones` | List clones with status filter |
| `delete_clone` | Delete clone and files |
| `sync_clone` | Sync with remote repository |
| `merge_clone` | Merge clone branch to target |

---

## Skill Management (`gobby-skills`)

6 tools for skill discovery and management.

| Tool | Description |
| :--- | :--- |

| `list_skills` | List skills with filters |
| `get_skill` | Get full skill content |
| `search_skills` | Search skills by query |
| `install_skill` | Install from path, GitHub, or ZIP |
| `update_skill` | Refresh skill from source |
| `remove_skill` | Remove installed skill |

---

## Metrics (`gobby-metrics`)

10 tools for tool usage and budget tracking.

| Tool | Description |
| :--- | :--- |

| `get_tool_metrics` | Get call count, success rate, latency |
| `get_top_tools` | Top tools by usage/success/latency |
| `get_failing_tools` | Tools with high failure rates |
| `get_tool_success_rate` | Success rate for specific tool |
| `reset_metrics` | Reset metrics for project/server/tool |
| `reset_tool_metrics` | Admin reset for specific tool |
| `cleanup_old_metrics` | Delete metrics older than retention |
| `get_retention_stats` | Metrics retention statistics |
| `get_usage_report` | Token and cost usage report |
| `get_budget_status` | Daily budget status |

---

## Hub (Cross-Project) (`gobby-hub`)

4 tools for cross-project queries.

| Tool | Description |
| :--- | :--- |

| `list_all_projects` | List all unique projects |
| `list_cross_project_tasks` | Query tasks across all projects |
| `list_cross_project_sessions` | List sessions across all projects |
| `hub_stats` | Aggregate hub statistics |

---

## Conductor (CLI-Based Orchestration)

The **Conductor** is a persistent orchestration loop that automatically manages task execution across multiple agents. Unlike the MCP orchestration tools (which are single-shot operations), the Conductor runs continuously.

The Conductor is managed via CLI commands, not MCP tools:

```bash
gobby conductor start      # Start the orchestration loop
gobby conductor stop       # Stop the loop
gobby conductor status     # Check loop status
gobby conductor chat       # Send message to conductor
gobby conductor restart    # Restart the loop
```

The Conductor uses the orchestration tools from `gobby-orchestration` internally (`orchestrate_ready_tasks`, `poll_agent_status`, `process_completed_agents`, etc.).

See [cli-commands.md](cli-commands.md#conductor) for full CLI reference.

---

## Error Handling

All tools return a consistent structure:

**Success:**

```json
{
  "success": true,
  "result": { ... }
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

- [cli-commands.md](cli-commands.md) - CLI command reference
- [tasks.md](tasks.md) - Task system guide
- [sessions.md](sessions.md) - Session management guide
- [memory.md](memory.md) - Memory system guide
- [rules.md](rules.md) - Rule engine guide
