# Orchestration

Gobby orchestration is the runtime coordination around spawned agent work:
agents claim tasks, work in isolation and close through the task lifecycle.
This guide explains how tasks, agents, isolation and the spawn cap fit together.

## Agent Work

Spawned workers operate through MCP lifecycle tools, not direct database
edits. A typical leaf agent:

1. Claims the assigned task through `gobby-tasks`.
2. Loads any task-required skills.
3. Edits and verifies the work in the task's isolation context when one exists.
   Docs leaf tasks may run inside the parent epic's isolation context; the
   current worktree or clone is still the authority for file edits.
4. Commits changes before lifecycle handoff.
5. Calls `close_task` with the commit SHA.
6. Calls `end_agent_run` with structured current state and next steps to
   terminate the agent run; ending a provider turn does not replace it.

Ending a chat turn is separate from ending the agent run. Agent definitions make
that separation explicit so task ownership and runtime resources are released in
the expected order.

## Runtime Tables

| Table | Purpose |
| --- | --- |
| `task_dispatch_mutex` | Short-lived per-task leases that serialize task-bound spawns |
| `task_artifacts` | Sparse pointers such as plan paths, isolation IDs, expansion runs, PR URL, and target branch |
| `task_lifecycle_events` | Append-only audit of task lifecycle changes |

Write related artifact fields atomically. Worktree and clone fields are pairs,
so path and ID values must be written or cleared together.

## Lifecycle Events

Rules are authored against semantic workflow events such as `turn_start`,
`turn_end`, `before_tool`, and `after_tool`. Raw provider/runtime hooks such as
`before_agent`, `after_agent`, and `stop` are normalized into that event model
for compatibility across supported CLIs.

## Agent Slot Cap

`spawn_agent` refuses a spawn when the project already has `max_active_agents`
active agents. The daemon config key defaults to 20; see
[Configuration](./configuration.md#spawn-cap). `can_spawn_agent` reports the same
check, and `dispatch_batch` bounds its concurrency with the same value.

## Related Guides

- [Workflows Overview](./workflows-overview.md) for rules, agents, pipelines,
  and runtime state
- [Pipelines](./pipelines.md) for deterministic sequence execution and approval
  gates
- [Agents](./agents.md) for agent definitions, persona application, and runtime
  tools
- [MCP Tools](./mcp-tools.md) for current server and tool signatures

_Last verified: 2026-09-12_
