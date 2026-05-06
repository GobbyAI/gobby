# Dispatch Guide

Gobby dispatch is a deterministic chain from a registry-defined stage vocabulary
to one action selected by ordered rules.

```text
task_stages_registry
        |
        v
task_stage_states manifest for one task
        |
        v
current manifest row + artifacts + automation fields
        |
        v
ordered dispatch rules
        |
        v
StartStageAction | SpawnAgentAction | StartExpansionAction
CreateIsolationAction | AdvanceStageAction | EscalateAction
```

## Stage Registry

The stage registry defines the allowed active stage names, labels, default
agents, human gates, and terminal behavior. Build resolves profile bundles and
explicit stage options against that vocabulary before a task becomes
dispatchable.

Active manifest stages, in order:

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

## Manifest Rows

`gobby build` projects the selected stages into `task_stage_states`. Those rows
are the task's manifest: each row has a `stage_name`, `position`, five-state
stage value (`ready`, `in_progress`, `needs_review`, `review_approved`, `done`),
review policy, attempt counters, optional caps, and artifact references.

The dispatcher never recomputes a route from task buckets. It reads the current
stage as the first manifest row whose state is not `done`, then evaluates rules
against that row. Structural manifest changes go through stage-state helpers so
position ordering and completed rows remain stable.

## Rule Chain

`src/gobby/dispatch/rules.py` owns the ordered rule list. A rule should be small:
check the current row and supporting artifacts, then return exactly one action or
`None`. The first non-null action wins for that heartbeat.

Examples:

- a `ready` discovery row can emit `StartStageAction`
- an `in_progress` row with a default agent can emit `SpawnAgentAction`
- a `development.ready` leaf with missing isolation can emit `CreateIsolationAction`
- a stage that exceeds policy can emit `EscalateAction`

Prompting belongs in spawned agents. Dispatch rules route state; they do not
author plans, review code, or repair artifacts inline.

## Readiness Projection

Readiness is a projection over task closure, escalation, blockers, parent
readiness, and the current manifest row. `list_ready_tasks` includes tasks that:

- are not closed
- are not escalated
- have no open external blocking dependency
- have a ready parent chain
- have a current manifest row in `ready` or `in_progress`

Blocked listings use the same task graph but project external blockers and
escalation separately from stage rows. A task can be blocked or escalated while
its manifest row still says `development.in_progress`.

## Blocked And Escalated Are Orthogonal

`is_blocked` and `is_escalated` are projections around the manifest, not
replacement stages. They answer whether work should be hidden from ready queues
or handed to a human; they do not rewrite the current row.

Worked example:

1. A leaf has `task_stage_states` row
   `development / position 7 / in_progress / work_attempt_count 2`.
2. The agent hits a credential problem and calls `escalate_task`.
3. Ready queues exclude the task because `is_escalated` projects true.
4. The manifest row remains `development.in_progress` with the same counters and
   `entered_at`.
5. After the credential is fixed, `de_escalate_task` clears the projection.
6. The next heartbeat sees the same `development.in_progress` row and resumes the
   normal rule chain.
