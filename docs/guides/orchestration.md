# Orchestration

Current Gobby orchestration is **state-driven dispatch**. Task automation is
driven by task lifecycle, explicit build state, deterministic rules, and
bounded agent spawning.

Pipelines still exist for deterministic sequences and approval gates. They are
not the main task-dispatch loop.

## Current Stack

| Component | Role |
| --- | --- |
| `src/gobby/dispatch/rules.py` | Ordered lifecycle rules that decide the next action for a task |
| `src/gobby/dispatch/dispatcher.py` | Heartbeat scanner that evaluates rules and executes actions |
| `gobby-tasks` | Task lifecycle, dependencies, readiness, claims, review, close, and escalation |
| `gobby-tasks-ops` | Build, expansion-run, artifact, audit-marker, and affected-file helper tools |
| `gobby-agents` | Worker spawning, runtime inspection, messaging, and commands |
| `gobby-worktrees` / `gobby-clones` | Isolation setup, sync, merge, and cleanup |
| `gobby-merge` | Conflict-resolution flows used at the merge boundary |

## Dispatch Model

The dispatcher scans tasks that have `allow_automation=true`, then evaluates
ordered rules over:

- lifecycle stage
- task status
- labels, including `stage-:<name>` skip labels
- `yolo`
- `isolation`
- `assigned_agent` and `additional_skills`
- artifact state in `task_artifacts`
- dependency and claim state

Rules return explicit actions such as spawn an agent, start expansion, create
worktree/clone isolation, advance lifecycle, append an audit marker, or
escalate. The dispatcher does not make model calls. Reasoning happens in the
spawned agent for that stage.

## Adding A Rule

Rules live in `src/gobby/dispatch/rules.py`.

1. Add a focused predicate for one lifecycle stage or recovery case.
2. Read task state and artifacts through existing helpers.
3. Return a typed action, or `None` when the rule does not apply.
4. Register the rule in the ordered list near related lifecycle rules.
5. Add tests for the rule and for any lifecycle/storage transition it depends on.

Rule ordering matters. Earlier rules should handle gates and recovery paths;
later rules should do the side-effecting dispatch once prerequisites are true.

## Build Entry Points

`gobby build` is the operator surface that turns a plan, epic, or leaf task into
dispatchable state.

All entry points call the same shared service in `src/gobby/build/service.py`:

| Surface | Entry point |
| --- | --- |
| CLI | `gobby build <plan_file>` or `gobby build <#taskref>` |
| MCP | `gobby-tasks-ops:build_task` |
| HTTP | `POST /api/build` |

The shared service resolves profiles and flags into stored state, returns a
`BuildResult`, and kicks an immediate dispatcher tick.

## Build State

| Field | Meaning |
| --- | --- |
| `allow_automation` | Opt-in gate. Tasks without it are ignored by dispatch. |
| `yolo` | Deterministic fallback mode. Rules avoid escalation where a defined fallback exists. |
| `isolation` | `none`, `worktree`, or `clone`. Epic isolation is created before leaf dispatch. |
| `stage-:<name>` labels | Stage skips. Profiles resolve to labels at build time. |
| `assigned_agent` | Leaf agent selected by expansion or `gobby build --agent`. |
| `additional_skills` | Extra skills included when dispatching the leaf agent. |

Profiles such as `quick`, `review`, `full`, and `full-yolo` are build-time
sugar. The dispatcher reads resolved task fields and labels, not profile names.

## Dispatch Tables

### `task_dispatch_mutex`

Per-task lease table for side-effecting dispatch actions. The dispatcher
acquires a lease before acting, releases it after the action, sweeps expired
leases on startup, and uses force-release only for operator recovery.

Normal access pattern: `acquire_mutex(...)` -> execute one action ->
`release_mutex(...)`.

### `task_artifacts`

Sparse pointer table for plan paths, target branch, isolation artifacts,
expansion runs, PR URL, and future merge SHA. Most tasks have no row.

Write related fields atomically. Worktree and clone artifacts must be written
and cleared as path/ID pairs.

### `task_lifecycle_events`

Append-only lifecycle audit. Lifecycle transition helpers write rows with
`from_state`, `to_state`, `reason`, and `by_actor`. Readers use this table for
history, diagnostics, and UI timelines.

## Agent Slot Cap

The dispatcher enforces the global `max_active_agents` cap from build config
(default 10). When the cap is full, the candidate waits for the next heartbeat.
There is no separate persistent queue; task state is the queue.

## Typical Flow

```text
gobby build <plan-or-task>
  -> write allow_automation/yolo/isolation/stage labels/artifacts
  -> record initial task_lifecycle_events row
  -> kick dispatcher tick
dispatcher tick
  -> scan opted-in tasks
  -> evaluate src/gobby/dispatch/rules.py in order
  -> acquire task_dispatch_mutex
  -> execute one action
  -> release mutex
spawned agent
  -> claims/reviews/closes/escalates through task lifecycle tools
next tick
  -> re-evaluate task state and continue
```

## Retired Templates

The legacy LLM-driven conductor tick and overlapping orchestration pipelines
are retired. Their bundled templates live under `deprecated/` folders as
archival tombstones, outside the active top-level install roots that bundled
sync reads:

- `orchestrator.yaml`
- `front-half-orchestrator.yaml`
- `dev-orchestrator.yaml`
- `delivery-orchestrator.yaml`
- `conductor.yaml`

Bundled sync soft-deletes installed rows for these retired definitions. Keep
the archival YAML disabled with `enabled: false`, `deprecated: true`, a
`deprecated_reason`, and no active steps.

## PR And Merge Boundary

Task #13552 owns real PR creation and richer merge/conflict automation. Current
dispatch can reach the PR/merge lifecycle boundary and use existing merge tools,
but PR authoring and complete conflict-resolution policy are follow-up work.

## Related Guides

- [Pipelines](./pipelines.md) for deterministic sequence execution
- [Agents](./agents.md) for worker definitions and messaging
- [MCP Tools](./mcp-tools.md) for the current server/tool surface
