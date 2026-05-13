# Dispatch Guide

Gobby dispatch is a deterministic heartbeat that turns a task's current stage
manifest row into one bounded action.

```text
task_stages_registry
        |
        v
task_stage_states for one task
        |
        v
current row + task fields + artifacts
        |
        v
ordered rules in src/gobby/dispatch/rules.py
        |
        v
one action executed under a task dispatch mutex
```

## Entry Points

`gobby build` is the main user-facing entry point. It accepts a plan file, epic,
or automated leaf task, resolves build options, initializes the stage manifest,
sets `allow_automation=true`, and kicks a bounded dispatcher heartbeat.

```bash
uv run gobby build '#14354' --quick --max-active-agents 1
uv run gobby build '#14354' --profile submit --isolation worktree --no-unattended
```

The same build service is exposed through MCP and HTTP:

```json
{
  "server": "gobby-tasks-ops",
  "tool": "build_task",
  "arguments": {
    "input_ref": "#14354",
    "profile": "submit",
    "quick": true,
    "isolation": "worktree",
    "stage": ["development:max_review_rounds=4"],
    "max_active_agents": 1
  }
}
```

```http
POST /api/build
Content-Type: application/json

{
  "input_ref": "#14354",
  "profile": "submit",
  "quick": true,
  "isolation": "worktree",
  "stage": ["development:max_review_rounds=4"],
  "max_active_agents": 1
}
```

`gobby build stop`, `gobby build resume`, `gobby build clean`, and
`gobby build restart` map to `POST /api/build/stop`,
`POST /api/build/resume`, `POST /api/build/clean`, and
`POST /api/build/restart`. The task-scoped forms accept an `input_ref`; the
project-wide stop and resume forms omit it.

Build profiles are DB-backed presets over `skip_stages`, `isolation`,
`unattended`, and delivery intent. `delivery_mode` is `auto` or
`pull_request`; `delivery_target_repo` is an optional PR base repository in
`owner/repo` form. Bundled `submit` uses `pull_request`; other bundled profiles
use `auto`. Omitted profile input resolves to `default`; explicit request fields
override profile values. Disabled profiles fail immediately instead of falling
through to a lower-priority row. Existing manifests keep their current stage rows
and task isolation on resume; profile `skip_stages` and profile isolation only
shape new or rebuilt manifests. Profile rows are editable through `gobby
profiles`, `gobby-profiles`, `/api/profiles`, and the Workflows Profiles tab.

## Stage Registry

The bundled registry in `src/gobby/install/shared/registry/stages.yaml` defines
the active stage vocabulary. Build resolves profile options and explicit stage
caps against this vocabulary before a task becomes dispatchable.

Active stages, in order:

1. `ideation`
2. `research`
3. `architecture`
4. `prd`
5. `planning`
6. `expansion`
7. `development`
8. `holistic_qa`
9. `pr`
10. `merge`

Each registry row can define display labels, category, default agent,
`review_policy`, reviewer selection, dispatch type, dispatch target, and
terminal behavior. `expansion` is pipeline-backed through `dispatch_type:
pipeline` and `dispatch_target: expand-task`.

Stage names are immutable in the editable registry. Operators can update
metadata, restore bundled rows, soft-delete unused rows, and reorder task-type
default manifests through `gobby stages`, `gobby-tasks` stage registry tools,
`/api/stages`, and the Workflows Stages tab. Deleted stages are hidden from
default manifest validation and normal registry listing.

## Manifest Rows

`gobby build` materializes selected stages into `task_stage_states`. Each row has
`stage_name`, `position`, `state`, `review_policy`, optional reviewer agent,
attempt counters, optional caps, artifact references, notes, and timestamps.

The five stage states are:

- `ready`
- `in_progress`
- `needs_review`
- `review_approved`
- `done`

The current stage is the first row whose state is not `done`, ordered by
`position`. Structural manifest changes go through stage-state helpers so
completed rows and position ordering remain stable.

Operators can inspect a manifest through the CLI, MCP, or HTTP:

```bash
uv run gobby tasks stages '#14354'
```

```json
{
  "server": "gobby-tasks",
  "tool": "get_task_stages",
  "arguments": {
    "task_id": "#14354"
  }
}
```

```http
GET /api/tasks/{task_id}/stages
```

Review agents are stored on manifest rows when the rows are created. The current
registry selector routes docs-category `development` reviews to `doc-reviewer`;
other development reviews default to `qa-reviewer`. Existing rows keep the
reviewer stored in `task_stage_states`.

## Rule Chain

`src/gobby/dispatch/rules.py` owns the ordered rule list. A rule checks the
current manifest row plus supporting task fields and artifacts, then returns one
action or `None`. The first non-null action wins for that heartbeat.

Common action classes:

- `StartStageAction`: move a `ready` row to `in_progress`
- `SpawnAgentAction`: start the configured worker or reviewer
- `StartPipelineAction`: run a stage pipeline such as expansion
- `AdvanceStageAction`: complete or approve a row through the stage-state manager
- `MergeWorkspaceAction`: merge an isolated workspace into its target branch
- `EscalateAction`: hand the task to a human intervention path

Prompting belongs in spawned agents. Dispatch rules route stage state; they do
not draft plans, review docs, or repair artifacts inline.

## Build State

Build state is resolved before dispatch:

- `allow_automation` is the opt-in gate. Backlog tasks stay invisible until
  `gobby build` enables automation.
- `isolation` is explicit task state: `none`, `worktree`, or `clone`.
- `assigned_agent` and `additional_skills` route leaf work.
- `target_branch` and workspace IDs live in task artifacts.
- Stage caps come from `--stage`, the MCP `stage` array, or HTTP `stage` array.
- `max_active_agents` limits the immediate heartbeat burst.
- Pull-request delivery profile state lives in `task_delivery_campaigns` as
  `delivery_mode`, `source_repo`, and `target_repo`.

The dispatcher scans tasks with `allow_automation=true`, no claim, no closure, no
escalation, no open blocking dependency, and a current stage in `ready`,
`in_progress`, `needs_review`, or `review_approved`. Each action runs under a
short-lived task dispatch mutex so another heartbeat cannot perform competing
side effects for the same task.

The default active-agent cap is 10. When the cap is reached, the heartbeat stops
early and the next heartbeat re-evaluates the same manifest state. There is no
separate persistent queue for skipped work.

## Isolation

Build owns integration workspace setup. For epic builds with `worktree` or
`clone` isolation, Gobby ensures each open epic in the subtree has a reusable
integration workspace or clone, records the workspace ID pair in artifacts, and
cascades the nearest integration branch to descendants as `target_branch`.

Leaf docs work can therefore run inside the parent epic's isolation context. A
docs leaf may have its own worktree branch while its `target_branch` points at
the nearest parent integration branch. Merge-stage dispatch uses the recorded
workspace or clone IDs plus `target_branch` to produce a workspace merge action.

For PR-stage dispatch, the merge orchestrator reads delivery campaign state and
calls `gobby-tasks-ops:open_delivery_pr`. That tool pushes the source branch,
reuses or opens the GitHub PR, and persists `task_delivery_units.pr_url`, `repo`,
`source_branch`, `target_branch`, `github_pr_number`, and `pr_state`.

For `development.ready` leaves, the current rule starts the stage when isolation
is `none`, `worktree`, or `clone`; invalid isolation values escalate. Missing or
stale integration workspace metadata is treated as unsafe build state and should
be repaired with task-scoped build clean or restart.

## Reviews

Development review is stage-native:

1. A development agent commits its changes.
2. If the development row requires review, the agent calls
   `submit_for_review(stage_name="development")`.
3. The row moves to `needs_review` and releases ownership.
4. The next dispatcher heartbeat spawns the reviewer stored on the row.
5. Approval moves the row to `review_approved`.
6. Dispatch advances the approved row to `done`.

For docs-category development work, the reviewer stored on the row is
`doc-reviewer`. Do not assume docs review uses `qa-reviewer`; inspect the row or
the stage registry selector.

## Lifecycle Events

Workflow rules are authored against semantic events such as `turn_start` and
`turn_end`. Provider/runtime events such as `before_agent`, `after_agent`, and
`stop` are adapter details below that authoring layer.

Ending an agent run is separate from end-of-turn rule evaluation. Workflow agents
that are instructed to terminate must call `gobby-agents:end_agent_run`; relying
on a provider stop event is not the same lifecycle transition.

## Readiness

Readiness is a projection over closure, escalation, dependency blockers, parent
readiness, claims, automation, and the current stage row. `list_ready_tasks`
returns task work that is open and dependency-ready; dispatcher automation uses
a narrower candidate query that also requires `allow_automation=true` and no
current claim.

Blocked and escalated state is orthogonal to the manifest. A task can have
`development.in_progress` stored in `task_stage_states` while escalation or
dependency projection keeps it out of ready queues and dispatcher candidates.

_Last verified: 2026-05-07_
