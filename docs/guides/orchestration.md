# Task Orchestration

Gobby provides two orchestration modes: **pipeline-based** (v3, recommended) and **MCP tool-based** (v2, legacy).

## Pipeline-Based Orchestration (v3)

The recommended approach uses a tick-based orchestrator pipeline driven by a cron job. Each tick scans task states, dispatches agents for ready work, monitors progress, handles review, and merges results.

### Key concepts

- **Clone-based isolation** — one shared clone per epic, agents work sequentially within it
- **Tick-based loop** — cron fires every N seconds, pipeline evaluates state and dispatches
- **Provider fallback rotation** — comma-separated provider lists (e.g., `"gemini,claude"`) with auto-retry on failures
- **Stall detection** — lifecycle monitor detects provider-side stalls and triggers provider rotation
- **Agent types** — developer agents write code, QA-dev agents review AND fix, merge agents handle landing

### Running the orchestrator

```bash
# Create a cron job to tick the orchestrator every 4 minutes
gobby cron create --name orchestrator-tick \
  --interval 240 \
  --action-type pipeline \
  --pipeline orchestrator \
  --inputs '{"epic_task_id": "#100", "developer_provider": "gemini,claude"}'
```

### Agent templates

| Template | Role | Behavior |
| :--- | :--- | :--- |
| `developer` | Write code | Claim → implement → commit → needs_review |
| `qa-dev` | Review + fix | Claim → review & fix → approve or escalate |
| `merge` | Land code | Merge clone branch, resolve conflicts |

### Pipeline template

The orchestrator pipeline (`orchestrator.yaml`) handles:
1. **Setup** — resolve or create clone for the epic
2. **Scan** — check task states (open, in_progress, needs_review, review_approved)
3. **Dispatch** — spawn dev agents for open tasks, QA agents for needs_review tasks
4. **Monitor** — track agent health, detect stalls, handle failures
5. **Merge** — when all tasks are approved, merge the clone and close the epic

See [Pipelines](./pipelines.md) for concrete orchestration examples and step-by-step pipeline structure.

---

## MCP Tool-Based Orchestration (v2)

The `gobby-orchestration` MCP server provides tools for manual orchestration — spawning agents for ready subtasks, monitoring their progress, handling review, and cleaning up worktrees.

All orchestration tools live on the **`gobby-orchestration`** server (not `gobby-tasks`). Task CRUD, dependencies, and readiness queries remain on `gobby-tasks`.

## Quick Reference

| Tool | Purpose |
| :--- | :--- |
| `orchestrate_ready_tasks` | Spawn agents in worktrees for ready subtasks |
| `get_orchestration_status` | Get subtask summary (open/in_progress/review/closed) |
| `poll_agent_status` | Check spawned agents, move completed to tracking list |
| `spawn_review_agent` | Spawn a review agent for a completed task |
| `process_completed_agents` | Route completed agents to review or cleanup |
| `approve_and_cleanup` | Approve reviewed task, merge and delete worktree |
| `cleanup_reviewed_worktrees` | Merge branches and delete worktrees for reviewed agents |
| `cleanup_stale_worktrees` | Delete worktrees with no active agent |
| `wait_for_task` | Block until a single task completes |
| `wait_for_any_task` | Block until the first of multiple tasks completes |
| `wait_for_all_tasks` | Block until all tasks complete |

## Spawning Agents

`orchestrate_ready_tasks` finds open subtasks under a parent that have no unresolved dependencies, then spawns an agent in an isolated worktree for each:

```python
call_tool(server_name="gobby-orchestration", tool_name="orchestrate_ready_tasks", arguments={
    "parent_task_id": "#100",
    "session_id": "<orchestrator_session_id>"
})
```

Each agent gets its own worktree branched from the current branch. The tool tracks spawned agents in workflow state so they can be polled later.

## Monitoring

### Orchestration status

`get_orchestration_status` returns a summary of all subtasks under a parent, grouped by status:

```python
call_tool(server_name="gobby-orchestration", tool_name="get_orchestration_status", arguments={
    "parent_task_id": "#100",
    "project_path": "/path/to/project"
})
# Returns: summary, open_tasks, in_progress_tasks, review_tasks, closed_tasks
```

### Polling agents

`poll_agent_status` checks whether spawned agents have finished and updates the tracking lists:

```python
call_tool(server_name="gobby-orchestration", tool_name="poll_agent_status", arguments={
    "parent_task_id": "#100"
})
```

## Review and Completion

### Processing completed agents

`process_completed_agents` routes finished agents — spawns review agents for validation or moves directly to cleanup:

```python
call_tool(server_name="gobby-orchestration", tool_name="process_completed_agents", arguments={
    "parent_task_id": "#100"
})
```

### Approving and merging

`approve_and_cleanup` transitions a task from `needs_review` to `closed`, merges its branch, and deletes the worktree:

```python
call_tool(server_name="gobby-orchestration", tool_name="approve_and_cleanup", arguments={
    "task_id": "#101"
})
```

## Waiting

Block until tasks reach `closed` or `needs_review` status. All wait tools accept a `timeout_seconds` parameter.

```python
# Single task
call_tool(server_name="gobby-orchestration", tool_name="wait_for_task", arguments={
    "task_id": "#101",
    "timeout_seconds": 300
})

# First of multiple tasks
call_tool(server_name="gobby-orchestration", tool_name="wait_for_any_task", arguments={
    "task_ids": ["#101", "#102", "#103"],
    "timeout_seconds": 300
})

# All tasks
call_tool(server_name="gobby-orchestration", tool_name="wait_for_all_tasks", arguments={
    "task_ids": ["#101", "#102", "#103"],
    "timeout_seconds": 600
})
```

## Cleanup

```python
# Merge branches and delete worktrees for reviewed agents
call_tool(server_name="gobby-orchestration", tool_name="cleanup_reviewed_worktrees", arguments={
    "parent_task_id": "#100"
})

# Delete stale worktrees with no active agent
call_tool(server_name="gobby-orchestration", tool_name="cleanup_stale_worktrees", arguments={
    "max_age_hours": 24
})
```

## Inter-Agent Messaging

Beyond orchestration tools, agents can communicate directly using P2P messaging on `gobby-agents`:

### P2P Messages

Any two sessions in the same project can exchange messages:

```python
# Agent sends status update to parent
call_tool("gobby-agents", "send_message", {
    "from_session": "<agent_session>",
    "to_session": "<parent_session>",
    "content": "Task #101 completed. 47 tests pass.",
    "priority": "normal"
})

# Parent retrieves pending messages
call_tool("gobby-agents", "deliver_pending_messages", {
    "session_id": "<parent_session>"
})
```

### Command Coordination

Ancestors can send structured commands to descendants with tool restrictions and exit conditions:

```python
# Orchestrator sends command to worker
call_tool("gobby-agents", "send_command", {
    "from_session": "<orchestrator_session>",
    "to_session": "<worker_session>",
    "command_text": "Run the full test suite and report failures",
    "allowed_tools": ["Bash", "Read", "Grep"],
    "exit_condition": "task_complete()"
})

# Worker activates the command (sets session variables)
call_tool("gobby-agents", "activate_command", {
    "session_id": "<worker_session>",
    "command_id": "<command_id>"
})

# Worker completes the command (clears variables, sends result)
call_tool("gobby-agents", "complete_command", {
    "session_id": "<worker_session>",
    "command_id": "<command_id>",
    "result": "All tests pass. Coverage: 92%."
})
```

### Command Lifecycle

```text
send_command → activate_command → [work] → complete_command
                    ↓                            ↓
            session variables set        variables cleared
            (command_id, allowed_tools)  result sent to parent
```

Commands enforce structure: only one active command per session, ancestor validation, and automatic variable cleanup on completion.

## Related

- [MCP Tools Reference](mcp-tools.md#task-orchestration-gobby-orchestration) — full tool table
- [Agents Guide](agents.md) — agent spawning, definitions, and isolation modes
- [CLI Commands](cli-commands.md#conductor) — conductor CLI for persistent orchestration loops
