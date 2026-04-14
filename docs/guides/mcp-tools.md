# Gobby MCP Tools

Complete reference for all MCP tools exposed by the Gobby daemon.

## Overview

Gobby exposes direct MCP proxy tools plus a set of internal `gobby-*`
registries. The exact live surface changes over time, so treat
`list_mcp_servers()` and `list_tools()` as the source of truth.

Tools are accessed via:

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
call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "category": "docs"})
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
    "category": "docs"
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
        "category": {"type": "string"}
      },
      "required": ["title", "category"]
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

Trigger session hooks for external integrations that are not using a native
Gobby hook adapter.

Use native hook names here, not semantic workflow event names. For example,
pass `SessionStart`, `stop`, or `user-prompt-submit` depending on the source;
do not pass `turn_start` or `turn_end`.

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

| Registry | Purpose |
| :--- | :--- |
| `gobby-tasks` | Task lifecycle, dependencies, readiness, and review state |
| `gobby-tasks-ops` | Expansion, affected-file wiring, GitHub issue import, and task reindexing |
| `gobby-sessions` | Session lifecycle, handoffs, transcripts, and messaging |
| `gobby-memory` | Persistent memory and recall |
| `gobby-workflows` | Rules, variables, agent definitions, pipelines, and pipeline execution |
| `gobby-agents` | Agent spawning, runtime inspection, persona application, messaging, and commands |
| `gobby-worktrees` | Worktree isolation lifecycle |
| `gobby-clones` | Clone isolation lifecycle |
| `gobby-merge` | Merge conflict resolution |
| `gobby-skills` | Skill discovery and loading |
| `gobby-metrics` | Metrics and budget tracking |
| `gobby-hub` | Cross-project queries |
| `gobby-cron` | Scheduled triggers |

There is no separate orchestration server in the current daemon. Orchestration
is built across `gobby-workflows`, `gobby-tasks`, `gobby-agents`,
`gobby-worktrees`, `gobby-clones`, and `gobby-merge`.

---

## Task Management (`gobby-tasks`)

~30 tools for persistent task tracking with dependencies and lifecycle management.

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
| `suggest_next_task` | AI suggests best next task. Use `count` param (default 1) for batch suggestions with conflict avoidance. |

### Session Integration

| Tool | Description |
| :--- | :--- |

| `link_task_to_session` | Associate task with session |
| `get_session_tasks` | Tasks linked to a session |
| `get_task_sessions` | Sessions that touched a task |

### Lifecycle

| Tool | Description |
| :--- | :--- |

| `de_escalate_task` | Return escalated task to open status |
| `generate_validation_criteria` | Generate criteria using AI |
| `run_fix_attempt` | Spawn fix agent for validation issues |
| `validate_and_fix` | Run validation loop with auto-fix |

> **Note:** Validation tools (`validate_task`, `get_validation_status`, `reset_validation_count`, `get_validation_history`, `get_recurring_issues`, `clear_validation_history`) are internal-only Python functions, not available via MCP. Sync tools (`sync_tasks`, `sync_import`, `sync_export`, `get_sync_status`) are CLI-only, not available via MCP.

### Git Integration

| Tool | Description |
| :--- | :--- |

| `link_commit` | Link git commit to task |
| `unlink_commit` | Unlink commit from task |
| `auto_link_commits` | Auto-detect commits mentioning task IDs |
| `get_task_diff` | Get combined diff for linked commits |

### Search

| Tool | Description |
| :--- | :--- |

| `search_tasks` | TF-IDF semantic search |

### Example: Task Workflow

```python
# 1. Find ready work
call_tool("gobby-tasks", "list_ready_tasks", {"limit": 5})

# 2. Create and claim a task
call_tool("gobby-tasks", "create_task", {
    "title": "Implement authentication",
    "priority": 1,
    "task_type": "feature",
    "category": "code",
    "validation_criteria": "Login flow works and has targeted test coverage.",
    "claim": True
})

# 3. Inspect the task
call_tool("gobby-tasks", "get_task", {
    "task_id": "#123"
})

# 4. Close when done
call_tool("gobby-tasks", "close_task", {
    "task_id": "#123"
})
```

---

## Task Operations (`gobby-tasks-ops`)

13 tools for expansion, affected files, GitHub issues, and search reindexing.

### Expansion

| Tool | Description |
| :--- | :--- |

| `save_expansion_spec` | Save expansion spec for later execution |
| `execute_expansion` | Execute saved expansion atomically |
| `get_expansion_spec` | Check for pending expansion |
| `validate_expansion_spec` | Validate spec structure and dependencies |
| `save_expansion_qa_result` | Save QA result for expansion |
| `check_expansion_qa_result` | Check QA result for expansion |

### Affected Files

| Tool | Description |
| :--- | :--- |

| `set_affected_files` | Set affected files for a task |
| `get_affected_files` | Get affected files for a task |
| `find_file_overlaps` | Find file contention across tasks |
| `wire_affected_files_from_spec` | Wire affected files from expansion spec |

### GitHub Integration

| Tool | Description |
| :--- | :--- |

| `import_github_issues` | Import issues from GitHub |
| `link_task_to_github_issue` | Link a task to a GitHub issue |

### Search

| Tool | Description |
| :--- | :--- |

| `reindex_tasks` | Rebuild search index |

---

## Orchestration Surface

Current orchestration is pipeline-based. The main tools involved are:

| Server | Key tools |
| :--- | :--- |
| `gobby-workflows` | `run_pipeline`, `get_pipeline_status`, `wait_for_completion`, `pipeline_eval` |
| `gobby-tasks` | `list_ready_tasks`, `suggest_next_task`, `list_tasks`, `claim_task`, review lifecycle tools |
| `gobby-agents` | `spawn_agent`, `dispatch_batch`, `apply_persona`, messaging and command tools |
| `gobby-worktrees` / `gobby-clones` | isolation lookup, creation, sync, cleanup |
| `gobby-merge` | conflict-resolution flows |

See [orchestration.md](./orchestration.md) for the current model.

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

`gobby-workflows` is the umbrella server for workflow definitions and pipeline
execution.

### Definitions

| Tool | Description |
| :--- | :--- |
| `list_workflows` / `get_workflow` | Inspect workflow and step-workflow definitions |
| `create_workflow` / `update_workflow` / `delete_workflow` / `restore_workflow` / `export_workflow` | Manage generic workflow definitions |
| `list_pipelines` / `get_pipeline` | Inspect pipeline definitions |
| `create_pipeline` / `update_pipeline` / `delete_pipeline` / `export_pipeline` | Manage pipeline definitions |
| `list_agent_definitions` / `get_agent_definition` | Inspect agent definitions |
| `create_agent_definition`, `toggle_agent_definition`, `delete_agent_definition` | Manage agent definitions |
| `update_agent_rules`, `update_agent_variables`, `update_agent_steps` | Edit parts of an agent definition |
| `list_rules`, `get_rule`, `create_rule`, `update_rule`, `toggle_rule`, `delete_rule` | Manage standalone rules |
| `list_variables`, `get_variable_definition`, `create_variable`, `update_variable`, `delete_variable`, `export_variable` | Manage variable definitions |

### Execution

| Tool | Description |
| :--- | :--- |
| `get_workflow_status` | Show current session workflow/runtime state |
| `evaluate_workflow` | Validate a workflow definition without executing it |
| `run_pipeline` | Start a pipeline and return an `execution_id` immediately |
| `get_pipeline_status` | Inspect a pipeline execution and its steps |
| `list_pipeline_executions` / `search_pipeline_executions` | Query pipeline history |
| `wait_for_completion` | Block on an agent or pipeline completion event |
| `resume_pipeline` | Resume a failed pipeline execution |
| `approve_pipeline` / `reject_pipeline` | Resolve approval gates |
| `cancel_pipeline` | Cancel a running pipeline execution |
| `pipeline_eval` | Evaluate data expressions inside orchestration flows |
| `fail_pipeline` | Mark the current pipeline as failed from inside a run |
| `import_workflow` / `reload_cache` | Import definitions or refresh the synced cache |

There is **no public `activate_workflow` MCP tool** in the current surface.
`activate_workflow` is a pipeline step type used internally during execution.

### Example: Run And Wait For A Pipeline

```python
result = call_tool("gobby-workflows", "run_pipeline", {
    "name": "orchestrator",
    "inputs": {"task_id": "#100"}
})

call_tool("gobby-workflows", "wait_for_completion", {
    "completion_id": result["execution_id"],
    "timeout": 1200
})
```

---

## Agent Management (`gobby-agents`)

Current `gobby-agents` tools cover agent runs plus inter-agent coordination.

### Runtime

| Tool | Description |
| :--- | :--- |
| `spawn_agent` | Spawn a worker session with optional isolation |
| `dispatch_batch` | Spawn multiple workers from task suggestions |
| `apply_persona` | Apply an agent definition to the current session |
| `get_agent_result` | Read the final result of a completed run |
| `list_agents` | List runs for a parent session |
| `list_running_agents` / `get_running_agent` | Inspect live runtime state |
| `stop_agent` | Mark a run cancelled without killing the process |
| `kill_agent` | Terminate the process/terminal |
| `can_spawn_agent`, `evaluate_spawn`, `running_agent_stats` | Spawn validation and runtime stats |
| `unregister_agent` | Internal registry cleanup helper |

### Messaging And Commands

| Tool | Description |
| :--- | :--- |
| `send_message` | P2P session messaging |
| `send_command` | Send a constrained command to a descendant |
| `activate_command` | Activate a pending command in the target session |
| `complete_command` | Mark a command complete and send the result back |
| `deliver_pending_messages` | Fetch and mark pending messages delivered |
| `wait_for_command` | Block until a pending command arrives |
| `get_inter_session_messages` | Read message history |

### Example: Agent Spawning

```python
# Spawn agent in worktree isolation
call_tool("gobby-agents", "spawn_agent", {
    "prompt": "Implement the login feature",
    "task_id": "#123",
    "parent_session_id": "<your_session_id>",
    "isolation": "worktree"
})

# Apply a persona to the current session instead of spawning
call_tool("gobby-agents", "apply_persona", {
    "agent": "developer"
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

14 tools for git worktree parallel development.

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

## Orchestration Note

The current daemon does not expose a separate conductor CLI or orchestration
server.

Use:

- `gobby-workflows:run_pipeline` for orchestration runs
- `gobby-workflows:wait_for_completion` for blocking callers
- `gobby-agents` runtime tools for worker dispatch
- `gobby cron ...` or `gobby-cron` for scheduled ticks

See [orchestration.md](./orchestration.md) for the current design.

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
