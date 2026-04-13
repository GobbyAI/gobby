# Orchestration

Current Gobby orchestration is **pipeline-based**. There is no separate
orchestration server in the live daemon. Multi-agent flows are built by
composing pipelines, task state, agent runtime, isolation managers, and
completion events.

This guide describes the current model.

## The Current Stack

Orchestration spans several MCP servers:

| Server | Role in orchestration |
| --- | --- |
| `gobby-workflows` | Run pipelines, inspect executions, wait for completion, evaluate helper expressions |
| `gobby-tasks` | Find ready work, claim/review/close tasks, inspect dependency state |
| `gobby-agents` | Spawn workers, dispatch batches, inspect runs, message or command descendants |
| `gobby-worktrees` | Create and manage worktree isolation |
| `gobby-clones` | Create and manage clone isolation |
| `gobby-merge` | Resolve merge conflicts in landing flows |

That is the control plane today.

## Canonical Orchestration Model

The bundled `orchestrator` and `dev-orchestrator` pipelines are the reference
patterns:

1. Scan task state.
2. Determine whether the target is a standalone task or an epic tree.
3. Resolve or create isolation for the orchestration target.
4. Count currently active claims.
5. Suggest ready work.
6. Dispatch developer or reviewer agents.
7. Exit.

The loop itself is **tick-based**, not an infinite in-process agent loop. A
cron trigger or outer caller re-runs the pipeline on a schedule.

## Bundled Building Blocks

### Pipelines

Current bundled orchestration pipelines include:

- `orchestrator`
- `dev-orchestrator`

Both live under `src/gobby/install/shared/workflows/pipelines/`.

### Agent Definitions

Common orchestration-facing agent definitions include:

- `developer`
- `qa-reviewer`
- `merge`
- `conductor`

The `conductor` definition is a persona for orchestration decisions. It is not
a standalone orchestration server or a separate command surface.

## Typical Flow

Here is the current orchestration shape in practical terms:

```text
run_pipeline(orchestrator)
  -> gobby-tasks:list_tasks / list_ready_tasks / suggest_next_task
  -> gobby-worktrees or gobby-clones: get/create isolation
  -> gobby-agents:dispatch_batch or spawn_agent
  -> worker claims task and runs its step workflow
  -> parent waits on completion events or checks task state next tick
  -> review/merge work happens through task state + merge tooling
```

## Isolation Strategy

Orchestration chooses an isolation mode per flow:

| Isolation | When it fits |
| --- | --- |
| `worktree` | Default isolated development inside the same repo |
| `clone` | Full isolation when a separate clone is safer or required |
| `none` | Review, merge, or read-only helper work |

The orchestrator resolves existing isolation first and only creates a new
worktree or clone when needed.

## Dispatch Patterns

### Single Worker

Use `gobby-agents:spawn_agent` when a pipeline is handing off one bounded job.

### Batch Dispatch

Use `gobby-agents:dispatch_batch` when `suggest_next_task` or a similar task
selection flow returns multiple non-conflicting pieces of work.

### Current Session Persona

Use `gobby-agents:apply_persona` when the caller should behave like an
orchestrator or worker without spawning another child session.

## Completion And Coordination

There are two main coordination mechanisms:

### Completion Events

Pipelines and parents use completion IDs to block or resume:

- `gobby-workflows:wait_for_completion`
- pipeline `wait` steps

This is how orchestration waits on child agents or nested pipelines.

### Inter-Agent Messaging

For richer parent/child coordination, use `gobby-agents` messaging tools:

- `send_message`
- `send_command`
- `activate_command`
- `complete_command`
- `deliver_pending_messages`
- `wait_for_command`

Messaging is useful when a parent needs to redirect or constrain a descendant
without baking every branch into pipeline YAML.

## Task-Centric View

A good mental model is that orchestration is mostly task-state management plus
dispatch:

- `list_ready_tasks` finds work that is unblocked.
- `suggest_next_task` prioritizes among ready tasks.
- worker agents claim tasks themselves via `claim_task`.
- review and merge phases are represented through task statuses such as
  `needs_review`, `review_approved`, and `escalated`.

That keeps orchestration declarative: the pipeline reacts to task state instead
of storing a second shadow scheduler.

## Recommended Entry Points

Use one of these depending on what you are building:

- `gobby-workflows:run_pipeline` for orchestration runs
- `gobby-cron` or `gobby cron ...` to trigger orchestration on a schedule
- `gobby-agents:spawn_agent` or `dispatch_batch` for worker dispatch
- `gobby-workflows:wait_for_completion` for blocking callers

If you find yourself looking for a dedicated orchestration server or older
one-shot orchestration tools, you are reading an older design. The current
system does that work in pipelines and shared MCP servers instead.

## Related Guides

- [Pipelines](./pipelines.md) for execution semantics
- [Agents](./agents.md) for worker definitions and messaging
- [MCP Tools](./mcp-tools.md) for the current server/tool surface
