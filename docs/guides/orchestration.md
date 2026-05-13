# Orchestration

Gobby orchestration is the runtime coordination layer around stage-manifest
dispatch. It turns built tasks into agent work, keeps isolation and review state
attached to those tasks, and relies on lifecycle events rather than ad hoc queues.

For the rule chain itself, read [Dispatch](./dispatch.md). This guide explains
how dispatch, tasks, agents, isolation, and review fit together during automated
task development.

## Build Lifecycle

```mermaid
flowchart LR
    Input[Plan file / task ref] --> Build[gobby build]
    Build --> State[Build state]
    State --> Manifest[Stage manifest]
    Manifest --> Queue[Dependency-ready queue]
    Queue --> Tick[Dispatcher heartbeat]
    Tick --> Action[One selected action]
    Action --> Work[Agent / pipeline / transition]
    Work --> Review{Review gate}
    Review -->|yes| Reviewer[Reviewer agent]
    Review -->|no| Complete[Complete stage]
    Reviewer --> Decision{Approved}
    Decision -->|yes| Complete
    Decision -->|no| Retry[Retry or escalate]
    Complete --> Next{More stages}
    Next -->|yes| Tick
    Next -->|no| Delivery[PR / merge / close]
```

`gobby build` is the opt-in boundary. Backlog tasks are inert until build state
is written onto a plan file, epic, or leaf task. Task refs that begin with `#`
should be quoted in shells, for example `gobby build '#14168'`.

After build state exists, the dispatcher heartbeat scans opted-in tasks, filters
out claimed, leased, closed, escalated, and dependency-blocked work, reads the
first manifest row whose state is not `done`, and lets ordered rules choose one
action for that heartbeat.

The dispatcher does not prompt models or repair artifacts inline. Prompting and
implementation happen inside the agent selected for the current stage.

During the 0.4.0 docs audit, `#14168` shows the intended shape: the epic is in
`development.in_progress`, ordinary guide leaves can run independently, and the
final guide-index task stays `development.ready` but dependency-blocked until
its sibling guide tasks close.

## Active Surfaces

| Surface | Current role |
| --- | --- |
| `gobby build [INPUT|ACTION] [REF]` | CLI entry point for starting or controlling build automation |
| `gobby-tasks-ops:build_task` | MCP entry point for starting lifecycle automation; requires `input_ref` |
| `POST /api/build` | HTTP entry point for the same shared build service |
| `POST /api/build/{stop,resume,clean,restart}` | HTTP control actions for project-wide ticks or task-scoped automation |
| `gobby profiles` / `gobby-profiles` / `/api/profiles` | Build profile registry editing for reusable build presets |
| `gobby stages` / `gobby-tasks` stage tools / `/api/stages` | Stage registry metadata and task-type default manifest editing |
| `src/gobby/dispatch/dispatcher.py` | Heartbeat scanner, mutex handling, and action executor |
| `src/gobby/dispatch/rules.py` | Ordered deterministic rules that map task state to actions |
| `gobby-tasks` | Task lifecycle, dependencies, claims, close, review state, and escalation |
| `gobby-tasks-ops` | Build, stage, artifact, expansion-run, review, and affected-file helpers |
| `gobby-agents` | Agent runs, runtime inspection, persona application, messaging, and commands |
| `gobby-worktrees` / `gobby-clones` | Isolation setup and cleanup for built work |
| `gobby-merge` | Merge-boundary and conflict-resolution tools |

The CLI, MCP, and HTTP build surfaces all resolve to the same build service and
return the same build-result shape. Build control actions such as `stop`,
`resume`, `clean`, and `restart` are exposed under `gobby build`.

## Build State

Build profiles and flags are convenience input. Profiles resolve before initial
manifest creation, then dispatch reads the resolved task fields and manifest
rows:

| Field | Meaning |
| --- | --- |
| `allow_automation` | Opt-in gate; tasks without it are invisible to dispatcher scans |
| `unattended` | Stored task flag for automation posture; build writes the resolved profile or explicit value |
| `isolation` | Execution isolation: `none`, `worktree`, or `clone` |
| Stage manifest rows | Ordered lifecycle stages in `task_stage_states` |
| `assigned_agent` | Leaf-stage agent chosen by expansion or build input |
| `additional_skills` | Extra skills loaded into the dispatched worker |
| `target_branch` | Artifact used as the base for isolated work and workspace merges |
| `task_delivery_campaigns.delivery_mode` | Delivery intent resolved from the build profile |
| `task_delivery_campaigns.source_repo` | GitHub source repo for PR delivery |
| `task_delivery_campaigns.target_repo` | GitHub base repo for PR delivery |

The current stage is the first manifest row whose state is not `done`. Blocked
and escalated are projections around that row: they change queue visibility and
human handoff behavior, but they do not replace the manifest state.

On resume, existing manifest rows stay authoritative. Profile `skip_stages` is
ignored with a warning and profile isolation does not replace the task's current
isolation; explicit build isolation still applies. CLI uses
`--isolation none|worktree|clone`, MCP `build_task` uses `isolation`, and HTTP
`POST /api/build` accepts the same values.

The bundled `submit` profile sets `delivery_mode=pull_request`. If a project
override sets `delivery_target_repo`, submit builds open PRs against that
repository while using the current project repository as the source.

## Dispatch Actions

A heartbeat executes at most one side-effecting action for a task. Common
actions include:

- starting a ready stage
- spawning the configured stage agent
- starting an expansion run
- creating worktree or clone isolation
- advancing a reviewed or completed stage
- appending an audit marker
- escalating when policy requires human intervention

Per-task leases in `task_dispatch_mutex` prevent duplicate side effects. The
dispatcher acquires a lease before action execution, releases it after the
action, and sweeps expired leases on startup.

## Agent Work

Dispatched workers operate through MCP lifecycle tools, not direct database
edits. A typical leaf agent:

1. Claims the assigned task through `gobby-tasks`.
2. Loads any task-required skills.
3. Edits and verifies the work in the task's isolation context when one exists.
   Docs leaf tasks may run inside the parent epic's isolation context; the
   current worktree or clone is still the authority for file edits.
4. Commits changes before lifecycle handoff.
5. Calls `close_task` when no review gate exists, or `submit_for_review` when
   the current stage requires review.
6. Calls `end_agent_run` to terminate the agent run.

Ending a chat turn is separate from ending the agent run. Agent definitions make
that separation explicit so task ownership and runtime resources are released in
the expected order.

## Stage Reviews

Review gates are stored on manifest rows. When a stage has a review policy, the
worker submits that stage for review rather than closing the task directly. The
reviewer then approves or rejects the same manifest row, and the next heartbeat
continues from the updated stage state.

This keeps review state attached to the task lifecycle instead of hiding it in a
pipeline run. Pipelines still exist for deterministic multi-step automation and
approval gates, but task automation uses stage manifests as its primary state.

## Runtime Tables

| Table | Purpose |
| --- | --- |
| `task_stage_states` | Ordered manifest rows for each built task |
| `task_dispatch_mutex` | Short-lived per-task leases for side-effecting dispatch |
| `task_artifacts` | Sparse pointers such as plan paths, isolation IDs, expansion runs, PR URL, and target branch |
| `task_lifecycle_events` | Append-only transition audit for task and stage changes |

Write related artifact fields atomically. Worktree and clone fields are pairs,
so path and ID values must be written or cleared together.

## Lifecycle Events

Rules are authored against semantic workflow events such as `turn_start`,
`turn_end`, `before_tool`, and `after_tool`. Raw provider/runtime hooks such as
`before_agent`, `after_agent`, and `stop` are normalized into that event model
for compatibility across supported CLIs.

Task dispatch is adjacent to this event system. Hook rules enforce local
session behavior, while dispatch rules route task state on heartbeat ticks.

## Agent Slot Cap

The dispatcher enforces the configured `max_active_agents` cap, which defaults
to 10. When all slots are occupied, no persistent queue is needed. The next
heartbeat scans the same task state and tries again.

## Related Guides

- [Dispatch](./dispatch.md) for manifest rows, readiness projection, and rule
  actions
- [Workflows Overview](./workflows-overview.md) for rules, agents, pipelines,
  and runtime state
- [Pipelines](./pipelines.md) for deterministic sequence execution and approval
  gates
- [Agents](./agents.md) for agent definitions, persona application, and runtime
  tools
- [MCP Tools](./mcp-tools.md) for current server and tool signatures

_Last verified: 2026-05-07_
