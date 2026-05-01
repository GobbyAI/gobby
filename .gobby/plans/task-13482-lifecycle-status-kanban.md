# Post-12725 Lifecycle/Status Kanban Alignment — Strategy Plan for #13482

`plan_kind: strategy` — narrative only, no manifest.

## Context
`kind: framing`

#12725 (gobby build) ships a working state-driven dispatcher, but it leaves three structural debts behind that block the kanban model the user actually wants:

1. **Stage names are hard-coded strings** in `src/gobby/dispatch/rules.py` (`_stage_enabled(task, "plan_review")`, `stage-:<name>` labels). There is no registry, so the UI can't reach in to enumerate or render stages.
2. **Two parallel state axes that don't align.** `status` (open/in_progress/needs_review/review_approved/closed/escalated) is work-claim state on the active item; `lifecycle` (open → plan_review → … → merged) is dispatcher pipeline state; `lifecycle_stage` is a derived 3-value projection. None of them captures "stage X is in state Y" for an arbitrary stage X.
3. **No durable per-stage state.** Current model is binary: a task either *is* at a lifecycle value or isn't. There's no "this task needs holistic-qa" vs "is being holistically-qa'd" vs "passed holistic-qa." The append-only `task_lifecycle_events` table records transitions but doesn't expose per-stage state for queries.

The user's paradigm: every task carries an ordered, *task-type-specific* set of applicable stages, and each stage carries its own work + review state. Tasks open with a manifest of stages all at `ready`; they close (task-level, via `closed_at`) when the last manifest row reaches `done`. A "simple fix" task has only `development → pr → merge` in its manifest and skips ideation/research/etc. entirely. A research spike's manifest terminates at `prd` and never reaches development. The kanban renders one column per stage with state badges per task.

Agents for the new stages (ideation, research, architecture, prd, holistic-qa) are already speced inside the #12725 plan family — some active in 0.4.0, some deferred to post-0.4.0 follow-ups. **This epic is not about agents.** It's about replacing the dual-enum state model with a registry-backed, **5-state-per-stage** manifest model with explicit per-stage `review_policy`, then re-pointing the dispatcher, MCP/HTTP/CLI surfaces, and web UI at it. `pr` and `merge` are part of that durable stage contract, so #13552 can later implement PR-Agent / conflict-resolution behavior directly on the new model. `escalated` is preserved as the human-in-the-loop flag (promoted to a first-class column); everything else in the `status` enum gets subsumed by per-stage state.

This is a pre-launch clean cutover. Do not build compatibility facades or long-lived legacy write paths. During this epic, callers move to the stage manifest APIs directly; then old `lifecycle`, `lifecycle_stage`, and active `status` semantics are removed instead of preserved as a shadow model.

## Target Model
`kind: framing`

### Per-stage state model: 5 values, policy-driven legality
`kind: framing`

The state enum on `task_stage_states.state` is **global** — every stage row's CHECK constraint accepts the same five values:

```text
ready | in_progress | needs_review | review_approved | done
```

Whether a given transition is *legal* on a given row is determined by the row's `review_policy` (sourced from the registry / default manifest at manifest-init time and stored on the stage state row for stability across registry edits).

`closed` is **never** a stage state. Task closure is task-level, fired when the terminal manifest row reaches `done`; `closed_at IS NOT NULL` is the canonical closure read.

This separation — global state enum, per-row policy controls legality — means adding review to a previously-non-reviewed stage later is a registry change + a migration decision for in-flight manifests; the API contract and CHECK constraint stay stable because reviewability is modeled as data, not as schema.

### Canonical stage names (11)
`kind: framing`

`ideation`, `research`, `architecture`, `prd`, `planning`, `test_arch`, `expansion`, `development`, `holistic_qa`, `pr`, `merge`.

The previous draft listed 14 stages including `adversarial_review`, `expansion_qa`, and `code_review_qa` as dedicated review stages paired with their work stages. Those are collapsed: review is now a state on the work stage itself (`needs_review` and `review_approved`), not a separate row in the manifest. `holistic_qa` survives as a single stage because it reviews an *aggregate* (the epic plus all leaves' diffs) rather than a single prior stage's output; the agent itself produces the `in_progress → review_approved` transition internally.

Stored in a new DB table `task_stages_registry` (source of truth), seeded from a bundled YAML at `src/gobby/install/shared/registry/stages.yaml` using the same template-sync pattern as workflows/rules.

Per-stage registry row: `name`, `display_label`, `description`, `category` (`discovery|design|verification|implementation|delivery`), `default_agent` (FK to agent registry, nullable for human-only or external review), `reviewer_agent` (FK, nullable; populated only when `review_policy != none`), `review_policy` (enum: `none | required | optional`, default `none`), `position_hint` (kanban ordering), `requires_human` (bool), `is_terminal` (bool — true only for `merge`), `default_max_work_attempts` (int, default 3), `default_max_review_rounds` (int, default 5).

#### Per-stage `review_policy` assignment
`kind: framing`

| Stage | `review_policy` | `reviewer_agent` | Notes |
|---|---|---|---|
| `ideation` | `none` | — | Discovery work; no automated reviewer |
| `research` | `none` | — | Discovery work; no automated reviewer |
| `architecture` | `none` | — | Design work; no automated reviewer |
| `prd` | `none` | — | Design work; no automated reviewer |
| `planning` | `required` | `plan-adversary` | Real reviewer exists |
| `test_arch` | `none` | — | No test-architecture reviewer agent exists; one-shot work stage |
| `expansion` | `required` | `expansion-qa` | Real reviewer exists |
| `development` | `required` | `qa-reviewer` | Real reviewer exists |
| `holistic_qa` | `required` | `holistic-reviewer` | **Epic-level only** — leaf manifests omit this stage. Aggregate review; agent produces `review_approved` internally |
| `pr` | `required` | *(blank — owned by #13552)* | External/PR-Agent reviewer; `pr_no_agent` escalation until #13552 lands |
| `merge` | `none` | — | Terminal automation stage |

Delivery stage ownership:

- `pr` — creates or updates the pull request, records PR metadata, normalizes PR review output, stores the structured PR verdict, and escalates unresolved PR discussion. The `needs_review`/`review_approved`/`review_rejected` transitions on `pr` represent external (human or PR-Agent) review state.
- `merge` — lands the approved PR/branch, runs AI-assisted conflict resolution through explicit Gobby merge tools, verifies the result, stores the merge report, and closes the task when it is terminal.

### Per-task stage manifest
`kind: framing`

New table `task_stage_states`, composite PK `(task_id, stage_name)`:

- `position` (int) — task-specific ordering, may differ from registry hint when operator reorders
- `state` enum: `ready | in_progress | needs_review | review_approved | done` (global CHECK; per-row legality via `review_policy`)
- `review_policy` (enum, mirrored from registry at manifest-init): `none | required | optional`
- `reviewer_agent` (text, mirrored from registry at manifest-init): nullable
- `entered_at`, `entered_by_session_id`
- `completed_at`, `completed_by_session_id`, `completed_commit_sha`
- `work_attempt_count` (int, default 0) — increments on each `start_stage` (entry or fail-loop reentry)
- `review_round_count` (int, default 0) — increments on each `mark_task_review_rejected`
- `max_work_attempts` (int, nullable) — null inherits `task_stages_registry.default_max_work_attempts` at evaluation time
- `max_review_rounds` (int, nullable) — null inherits `task_stages_registry.default_max_review_rounds`
- `artifact_refs` (nullable JSON object of pointers into `task_artifacts`) — e.g., the plan path produced by `planning`, the expansion run id produced by `expansion`, or delivery artifacts produced by `pr` / `merge`
- `notes` (text) — QA reviewer feedback, escalation context

The set of rows for a task **is** the task's lifecycle. The "current stage" is the leftmost row by position whose state is not `done`. The task is closed when all rows are `done` (or operator marks the task closed terminally) — `closed_at IS NOT NULL` becomes the canonical closure predicate.

Mirroring `review_policy` and `reviewer_agent` onto each row at manifest-init is load-bearing: a later registry edit (e.g., flipping `research.review_policy: none → required`) does **not** retroactively change the legality of transitions on already-created rows. The new policy applies only to manifests created after the registry edit, unless an explicit migration backfills existing rows.

Delivery-stage artifacts are stored in `task_artifacts` and referenced from the stage row:
`pr_url`, `github_pr_number`, `pr_review_report`, `structured_pr_verdict`,
`merge_commit_sha`, and `merge_campaign_report`.

### Transitions
`kind: framing`

Each transition's legality depends on the row's `review_policy`. A typed `IllegalStageTransitionError` (carrying `(stage_name, current_state, attempted_transition, review_policy)`) fires on any violation; callers can inspect the fields to surface the constraint clearly.

- `start_stage`: `ready → in_progress`. Increments `work_attempt_count`. Legal on all policies.
- `mark_task_needs_review` (`submit_for_review`): `in_progress → needs_review`. Legal only on `policy ∈ {required, optional}`. **Rejected** on `policy=none`.
- `mark_task_review_approved` (`approve_review`): `needs_review → review_approved`. Legal only when current state is `needs_review`. **Rejected** on `policy=none`.
- `mark_task_review_rejected` (`reject_review`): `needs_review → ready`. Increments `review_round_count`. **Rejected** on `policy=none`. Stage loops until escalation cap (`max_review_rounds`) hit.
- `complete_stage` (admin/dispatcher): direct `in_progress → done` for `policy ∈ {none, optional}`; `review_approved → done` for `policy ∈ {required, optional}`. Direct `in_progress → done` on `policy=required` is rejected except via an explicit `validation_override_reason`.
- `fail_stage`: terminal failure path; transitions current row to `ready` with `work_attempt_count++` (work failure) or escalates the task when `max_work_attempts` is hit. Legal on all policies.

The `review_approved → done` advance on `policy=required` rows is dispatcher-driven for automated stages and operator-driven for human-gated stages. `review_approved` is intentionally a **durable** holding state — not a transient event — because the dispatcher needs that boundary as a real queue position (PR approved-before-merge, expansion-QA approved-before-dev-fanout, planning-approved-before-test-arch).

### Task type taxonomy + default manifests
`kind: framing`

Existing types: `bug`, `feature`, `task`, `epic`, `chore`, `refactor`. Adds:

- `simple_fix` — `[development, pr, merge]`
- `research_spike` — `[ideation, research, prd]` (terminal at `prd`, no merge)
- `architecture_doc` — `[research, architecture]` (terminal at `architecture`)
- `prd_doc` — `[ideation, prd]` (terminal at `prd`)

Defaults for existing types:

- `epic` — full pipeline: `[ideation, research, architecture, prd, planning, test_arch, expansion, development, holistic_qa, pr, merge]`
- `feature` — `[planning, test_arch, expansion, development, pr, merge]`
- `bug` — `[development, pr, merge]`
- `refactor` — `[planning, development, pr, merge]`
- `chore` / `task` — `[development, pr, merge]`

`holistic_qa` is **epic-level only**. Leaf manifests (every type except `epic`) omit it; the dispatcher's all-leaves-parked rule advances the epic's `holistic_qa` row to `in_progress` once every leaf is `done` or terminal. Putting holistic review on every leaf would be redundant work since holistic review is aggregate by definition.

Defaults are seeded as registry rows of a second table `task_type_default_stages (task_type, stage_name, position)` so the UI can render "what would this type's lifecycle look like" without invoking `gobby build`.

Operator overrides at build time: `gobby build <ref> --stages a,b,c` (explicit), `--add-stage <name>[@<position>]`, `--skip-stage <name>`, `--stage <name>:max_review_rounds=N` (per-stage cap override). Profiles (`quick`, `full`, `full-yolo`) become named bundles of stage sets and policy overrides.

### Status retirement
`kind: framing`

Active values (`open`, `in_progress`, `needs_review`, `review_approved`) are subsumed entirely by per-stage state and are dropped from the `status` enum. The `status` column itself is dropped if the audit confirms no remaining readers; otherwise it stays storing only `'closed'` for closed tasks (with `closed_at IS NOT NULL` as the canonical read).

`escalated` is **kept** as the human-in-the-loop flag, promoted from a value of `status` to a first-class column `is_escalated` (already exists in the projection — just promote it to a real column) plus the existing `escalated_at` / `escalation_reason` fields. Escalation is orthogonal to stage state: any stage can be in `in_progress` (or `needs_review`, etc.) while the task is also `is_escalated=true`. `escalate_task` and `de_escalate_task` do **not** mutate `task_stage_states`; a task that escalates from `(stage='development', state='in_progress', work_attempt_count=2)` and then de-escalates returns to the same row values exactly.

End state: the `status`, `lifecycle`, and `lifecycle_stage` columns are dropped entirely. The implementation must not keep old lifecycle mutation tools as compatibility shims; callers must be ported to stage-native APIs in the same epic.

### Tool surface
`kind: framing`

The three review tools — `mark_task_needs_review`, `mark_task_review_approved`, `mark_task_review_rejected` — are preserved as **first-class stage-axis tools**, not lossy shims composing `complete_stage` / `fail_stage`. Each maps directly to a transition above and enforces its precondition (current state must match the legal source state, and `review_policy` must permit the transition).

Agent and operator workflows that already call these tools continue to work, gaining stage-native semantics. The audit (Phase 5) walks every bundled agent YAML's call sites and verifies their stage context satisfies the precondition. The most likely offender is `test-architect`: its YAML may call `mark_task_needs_review` after writing test architecture, but the new contract sets `test_arch.review_policy=none`, so that call site must be rewritten to `complete_stage` (or its dispatcher equivalent).

### Future extensibility
`kind: framing`

Adding a review loop to a previously-non-reviewed stage (e.g., attaching review to `research` later) is a two-step registry-driven change, not a schema change:

1. Update `stages.yaml` to flip `review_policy: none → required` on the stage row; provide a `reviewer_agent`.
2. Decide on a one-off migration for already-created manifest rows — either backfill `review_policy='required'` on existing rows (forces review going forward; risk: stalled in-flight stages need to traverse `needs_review → review_approved`), or leave existing rows at the manifest-init-time policy and only apply the new policy to manifests created after the change.

This is a documented extension path, not part of this epic's deliverable scope.

### Readiness / blocking projection
`kind: framing`

Blocking remains orthogonal to stage state. `task_stage_states.state` always carries one of the five values; do **not** add `blocked` as a sixth value. A blocked task still has a current stage in any state, but automation and ready queues must not advance it until the blocking condition clears.

Canonical computed projection:

- `current_stage` — leftmost manifest row by `position` whose state is not `done`; `null` when every stage is done
- `is_closed` — `closed_at IS NOT NULL`
- `is_escalated` — first-class escalation flag / `escalated_at IS NOT NULL`
- `active_blocked_by` — unresolved external dependencies; excludes parent tasks blocked only by their own descendants, which are completion blocks rather than work blocks
- `is_blocked` — `is_escalated OR active_blocked_by is non-empty`
- `is_ready` — not closed, not escalated, no unresolved external blockers, and the current stage is actionable (`state ∈ {ready, in_progress}` or `review_approved` awaiting dispatcher advance)
- `owner_session_id` — current claim/session owner for UI badges and automation locks

Dependency blockers resolve by terminality, not by legacy `status`: an upstream dependency blocks downstream work while `upstream.closed_at IS NULL`. This preserves the #12725 invariant that escalated upstream work blocks dependents, without keeping `status='escalated'` as a blocking sentinel. Stages in `review_approved` are not yet `done` and so do not satisfy upstream-completion checks; the dispatcher's tail rule advances them to `done` once review is satisfied.

Dispatcher candidate scans filter by `is_ready` first, then rule evaluation operates on `current_stage` and the manifest rows. LifecycleBoard renders blocked tasks in their stage column with a blocked badge/overlay and a `blocked=true` filter; it does not move them into a synthetic blocked stage.

### Dispatcher coupling
`kind: framing`

`src/gobby/dispatch/rules.py` rules switch from string checks to registry-aware queries. Each rule reads `(current_stage.name, current_stage.state, current_stage.review_policy)`:

- `_stage_enabled(task, "plan_review")` → `task_has_stage(task, "planning")` reading `task_stage_states`
- `_skipped_stages(task)` → deleted; "skipped" means "not in manifest"
- Stage advance helpers write `task_stage_states.state` transitions instead of mutating `lifecycle` / `status`
- `list_automation_candidates`, `list_ready_tasks`, `list_blocked_tasks`, and `suggest_next_task` read the readiness projection instead of legacy `status`
- Rule ordering still anchored on registry `position_hint`

Per-stage rule fan-out for stages with `review_policy=required`:

- `<stage>_work_rule`: fires when `current_stage.name == X AND current_stage.state == 'in_progress'`. Spawns `default_agent`.
- `<stage>_review_rule`: fires when `current_stage.name == X AND current_stage.state == 'needs_review'`. Spawns `reviewer_agent` (or escalates with `<stage>_no_reviewer` if blank).
- `<stage>_advance_rule`: tail rule fires when `current_stage.name == X AND current_stage.state == 'review_approved'`. Calls `complete_stage` to land at `done` and advance the next manifest row.

PR/merge dispatch is stage-native:

- `holistic_qa.review_approved` advances the epic's next manifest row, usually `pr`, to `ready`
- `pr` `in_progress` spawns the PR agent once #13552 lands; before then it escalates with a concrete missing-agent reason. Verdict via `record_pr_verdict(approved|rejected|needs_changes)` maps to `approve_review` / `reject_review` on the `pr` row.
- `pr.review_approved` advances `merge` to `ready`
- `merge.in_progress` spawns `merge-orchestrator`
- `merge.done` closes the task when it is the terminal manifest stage

Existing rules in `dispatch/rules.py` are renamed and refactored 1:1 onto the new stage names. New stages without active agents (e.g., post-0.4.0 deferred discovery agents) get pass-through rules that keep them at `ready` until an agent is registered or escalate if the stage is actionable but no agent exists. Rules and agents must use stage mutation APIs; they must not write `status`, `lifecycle`, or `task_stage_states` directly outside the storage manager.

### MCP / HTTP / CLI surfaces
`kind: framing`

New `gobby-tasks` MCP tools:

- `get_task_stages(task_id)` → ordered list of `(stage_name, state, review_policy, work_attempt_count, review_round_count, artifact_refs)`
- `start_stage(task_id, stage_name, notes?)`
- `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?, validation_override_reason?)`
- `fail_stage(task_id, stage_name, reason, needs_human?)`
- `add_stage(task_id, stage_name, position)` / `remove_stage(task_id, stage_name)`
- `record_pr_verdict(task_id, verdict, findings, report_ref?)` — maps to `approve_review` / `reject_review` on `pr`
- `record_pr_opened(task_id, pr_url, github_pr_number?)` — writes PR metadata without changing stage state
- `record_merge_result(task_id, merge_sha?, report_ref?)` — completes `merge` and closes the task
- `list_stages_registry()` / `get_task_type_defaults(task_type)`

The three review tools (`mark_task_needs_review`, `mark_task_review_approved`, `mark_task_review_rejected`) are preserved with their existing signatures, rewired to first-class stage transitions per the **Tool surface** section above. They raise `IllegalStageTransitionError` when called against a `policy=none` row or against the wrong source state.

HTTP mirrors at `/api/tasks/{id}/stages`, `/api/stages/registry`, `/api/task-types/{type}/default-stages`. List endpoint gains `?stage=development&stage_state=in_progress` filters that drive kanban columns directly.

CLI:

- `gobby tasks list --stage development --state in_progress`
- `gobby tasks stages <ref>` — show per-stage manifest with state, policy, attempts, and rounds
- `gobby tasks advance <ref> [--stage <name>]` — complete the current stage; auto-start the next when eligible per policy
- `gobby build <ref> --stages …` — explicit manifest at opt-in time

### Web UI: LifecycleBoard
`kind: framing`

New view `LifecycleBoard` replaces existing `KanbanBoard` in `web/src/components/tasks/`:

- Columns = registry stages, ordered by `position_hint`, filterable by category
- Cards within a column grouped by state, in this top-to-bottom order: `ready`, `in_progress`, `needs_review`, `review_approved`, `done`. Stages with `review_policy=none` show only `ready / in_progress / done` (no `needs_review` or `review_approved` group). `done` collapsed by default.
- Swimlanes by `task_type` so simple fixes don't render in research/ideation columns
- Stage column hidden by default if no task has it in its manifest (configurable)
- Blocked tasks stay in their current stage column with a blocked badge/overlay and blocker tooltip; board filters include `blocked=true`
- Drag a card right = advance per the row's `review_policy`. On `policy=none` and `policy=optional`, drag advances `ready → in_progress → done`. On `policy=required`, drag advances along the full chain `ready → in_progress → needs_review → review_approved → done`. Drag attempts that violate legality surface a tooltip with the constraint reason.
- Existing `KanbanBoard` (the 6-bucket ready/in_progress/review/blocked/merge_ready/closed view) is replaced during the cutover; do not keep a parallel `StatusBoard`.

`useTasks` fetches stage manifests in the same call (denormalized response) to avoid N+1 on board renders.

### Migration
`kind: framing`

Schema migration in `src/gobby/storage/migrations.py` runs in one direction (no down-migration; this is a structural change):

1. Create `task_stages_registry` (with `review_policy`, `reviewer_agent`, `default_max_work_attempts`, `default_max_review_rounds`), `task_type_default_stages`, and `task_stage_states` (with the global 5-value CHECK, the `review_policy` mirror column, `work_attempt_count`, `review_round_count`, `max_work_attempts`, `max_review_rounds`).
2. Seed registry from bundled YAML.
3. For each existing task: write `task_stage_states` rows by mapping current `(lifecycle, status)` tuple onto the manifest derived from `task_type` defaults plus existing `stage-:<name>` skip labels. Mapping table (every observed pair audited against a SQL census before migration runs; unmapped tuples fail loudly):

   | lifecycle | status | Manifest result |
   |---|---|---|
   | `open` | `open` / any non-terminal | All rows `ready` |
   | `plan_review` | `open` / `in_progress` | `planning.in_progress`, predecessors `done`, successors `ready` |
   | `plan_review` | `needs_review` | `planning.needs_review`, predecessors `done`, successors `ready` |
   | `plan_review` | `review_approved` | `planning.review_approved`, predecessors `done`, successors `ready` |
   | `test_arch` | `open` / `in_progress` | `test_arch.in_progress`, predecessors `done`, successors `ready` |
   | `test_arch` | `needs_review` | `test_arch.in_progress`, predecessors `done`, successors `ready` (test_arch.review_policy=none — `needs_review` collapses to `in_progress` since the legacy state has no policy-aware home) |
   | `test_arch` | `review_approved` | `test_arch.done`, predecessors `done`, successors `ready` |
   | `expanding` | `open` / `in_progress` | `expansion.in_progress`, predecessors `done`, successors `ready` |
   | `expanding` | `needs_review` | `expansion.needs_review`, predecessors `done`, successors `ready` |
   | `expanding` | `review_approved` | `expansion.review_approved`, predecessors `done`, successors `ready` |
   | `in_development` | `open` / `in_progress` | `development.in_progress`, predecessors `done`, successors `ready` |
   | `in_development` | `needs_review` | `development.needs_review`, predecessors `done`, successors `ready` |
   | `in_development` | `review_approved` | `development.review_approved`, predecessors `done`, successors `ready` (leaf-park; epics scan children) |
   | `holistic_review` | any non-terminal | `holistic_qa.in_progress`, predecessors `done`, successors `ready` |
   | `holistic_review` | `review_approved` | `holistic_qa.review_approved`, predecessors `done`, successors `ready` |
   | `pr` | `open` / `in_progress` | `pr.in_progress`, predecessors `done`, `merge.ready` |
   | `pr` | `needs_review` | `pr.needs_review` with `pr_url` populated, predecessors `done`, `merge.ready` |
   | `pr` | `review_approved` | `pr.review_approved`, predecessors `done`, `merge.ready` |
   | `merging` | any non-terminal | `merge.in_progress`, predecessors `done` |
   | `merged` | `closed` | All rows `done` |

   `status='escalated'` overrides the per-row state mapping with `is_escalated=true`; the active stage row stays at whatever the lifecycle component dictates.

   `status='closed'` with non-`merged` lifecycle: terminal-close-without-merge case (e.g., abandoned tasks). All rows up to and including the row implied by `lifecycle` are `done`; `closed_at` is already populated on the task itself.

   Migrate `work_attempt_count` from `planning-round:N` and `qa-attempts:N` labels (numeric suffix); `review_round_count` starts at 0 since no legacy column tracks it directly. Migrate per-stage caps from `task_artifacts.max_planning_rounds`, `max_qa_rounds`, `max_merge_attempts` into `task_stage_states.max_review_rounds` / `max_work_attempts` on the corresponding rows.

4. Drop `stage-:<name>` labels (now redundant with manifest).
5. Port storage, MCP, HTTP, CLI, dispatcher, agents, and web callers to the stage APIs in this epic. Do not add computed-legacy write facades.
6. Keep readiness/blocking projections behaviorally equivalent after cutover: `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, task `state.is_blocked`, and dispatcher candidate scans return the same results as the old model for equivalent fixtures.
7. Drop `lifecycle`, `lifecycle_stage`, and active `status` semantics in the same epic once callers are ported. Drop `task_artifacts.max_planning_rounds`, `max_qa_rounds`, `max_merge_attempts` since per-stage caps replace them. Any short-lived local migration helpers are deleted before closure.

`task_lifecycle_events` is kept unchanged — it's still the audit trail and gains rows for stage-state transitions.

## Phasing (sub-epics for #13482 expansion)
`kind: framing`

**Phase 1 — Registry + manifest schema.** Create tables, seed from YAML, define `pr` / `merge` delivery artifacts, and write per-task stage rows for all existing tasks. The migration may read legacy columns to derive initial rows, but new writes go through stage APIs only.

**Phase 2 — Stage-native storage + API surface.** New manager modules `_stage_registry.py`, `_stage_states.py` under `src/gobby/storage/tasks/`. Five-state machine with policy-aware legality and `IllegalStageTransitionError`. Counter split (`work_attempt_count` + `review_round_count`) with per-stage caps. New MCP tools, HTTP routes, CLI subcommands. Replace old lifecycle mutation tools with stage-native operations in this phase. The three review tools are rewired to first-class state transitions, not shims.

**Phase 3 — Dispatcher refactor.** Rewrite `src/gobby/dispatch/rules.py` to query `task_stage_states` instead of string-matching `lifecycle`. Each work stage spawns `default_agent` on `state == 'in_progress'`; each `policy=required` stage spawns `reviewer_agent` on `state == 'needs_review'`; the tail rule advances `review_approved → done` for review-required stages. `gobby build` (`src/gobby/build/service.py`) resolves manifest from `task_type` defaults + operator overrides at opt-in time. Stage advance helpers write to manifest.

**Phase 4 — PR / merge stage cutover.** Port PR and merge workflows to stage APIs. `pr.review_policy=required` (with reviewer left blank pending #13552); `record_pr_verdict` maps `approved → approve_review`, `rejected/needs_changes → reject_review`. `merge.review_policy=none` records merge SHA/report artifacts and closes terminal tasks. #13552 waits for this phase before implementing PR-Agent behavior.

**Phase 5 — Task type expansion + legacy removal + agent YAML audit.** Register `simple_fix`, `research_spike`, `architecture_doc`, `prd_doc`; seed `task_type_default_stages`; update `gobby tasks create` defaults and validation. Promote `is_escalated`, remove remaining writes to old active status/lifecycle values, then drop legacy columns/tools/tests. Audit every bundled agent YAML's review-tool call sites against the per-stage policy table; rewrite any call sites that violate the contract (e.g., `test-architect` calling `mark_task_needs_review` on `test_arch.review_policy=none`).

**Phase 6 — Web UI: LifecycleBoard.** New component, new fetch shape, swimlanes by task_type, 5-state visualization (with policy-aware grouping per column), drag-to-advance with policy-aware legality. Replace the existing `KanbanBoard` instead of keeping a compatibility board.

**Phase 7 — Cleanup.** Remove deprecated `stage-:<name>` label handling, temporary migration helpers, and dead lifecycle/status code. Documentation pass.

Phases 1–4 are blocking for #13552: PR-Agent / advanced merge behavior must target the stage contract, not the old lifecycle model. Phases 5 and 6 can run after the delivery-stage cutover. Phase 7 closes the epic.

## Critical files
`kind: framing`

- `src/gobby/storage/migrations.py` — schema migration (Phase 1, Phase 7)
- `src/gobby/storage/tasks/_models.py` — Task dataclass trim (Phase 5, Phase 7)
- `src/gobby/storage/tasks/_lifecycle_events.py` — keep, gains stage-state events
- New: `src/gobby/storage/tasks/_stage_registry.py`, `src/gobby/storage/tasks/_stage_states.py` (with 5-state machine + `IllegalStageTransitionError`)
- `src/gobby/dispatch/rules.py` — full refactor (Phase 3)
- `src/gobby/build/service.py` — manifest resolution at build time (Phase 3, Phase 4)
- `src/gobby/tasks/state_semantics.py` — projection helpers retire (Phase 5, Phase 7)
- `src/gobby/mcp_proxy/tools/tasks/_crud.py` — new tools (Phase 2)
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py` — replace lifecycle-specific PR/merge mutations with stage-native operations (Phase 2, Phase 4)
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py` — rewire `mark_task_needs_review`, `mark_task_review_approved`, `mark_task_review_rejected` to first-class stage transitions (Phase 2)
- `src/gobby/install/shared/workflows/agents/*.yaml` — audit + rewrite review-tool call sites against the per-stage policy table (Phase 5)
- `src/gobby/servers/routes/tasks.py` — new endpoints + filter params (Phase 2)
- `src/gobby/cli/tasks/crud.py`, `_utils.py` — new flags, new render paths (Phase 2, Phase 4)
- `web/src/components/tasks/KanbanBoard.tsx` — replace with manifest-driven board wiring (Phase 6)
- New: `web/src/components/tasks/LifecycleBoard.tsx`, `StageColumn.tsx`, `StageCard.tsx`
- `web/src/hooks/useTasks.ts` — denormalized stage manifest in fetch (Phase 6)
- `web/src/lib/taskState.ts` — retire 3-value `TaskLifecycleStage` (Phase 7)
- New: `src/gobby/install/shared/registry/stages.yaml` — bundled seed (11 stages)
- `src/gobby/install/shared/skills/plan-draft/SKILL.md` — refresh stage list (Phase 4)

## Reuse
`kind: framing`

- Template-sync pattern from `src/gobby/install/shared/{rules,workflows,agents}` for stage registry seeding (`src/gobby/workflows/loader.py`).
- `task_artifacts` table for per-stage artifact pointers and delivery artifacts — no new artifact storage needed.
- `task_lifecycle_events` for audit trail — gains rows, no schema change.
- `_dispatch_failure_count` and existing per-task mutex (`task_dispatch_mutex`) keep working unchanged.
- `task_type_default_stages` follows the same seed-on-first-startup convention as bundled rules/workflows; drift detected by hash compare.

## Verification
`kind: framing`

- **Unit tests**: registry CRUD; manifest mutation invariants (position uniqueness per task, stage-name FK to registry, global state enum); type→default-stages resolution; build-time override merge; `IllegalStageTransitionError` raised on every illegal `(state, transition, policy)` triple.
- **Migration tests**: census every observed `(lifecycle, status, labels)` tuple in a fixture DB, run migration, assert per-task manifest matches expected mapping table, including `pr` / `merge` rows and delivery artifact references; assert `work_attempt_count` populated from `planning-round:N` / `qa-attempts:N` labels; assert per-stage caps populated from legacy artifact columns.
- **Readiness tests**: post-cutover results for `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and task `state.is_blocked` match equivalent old-model fixtures; cover external blockers, ancestor/descendant completion blocks, escalated upstreams, closed upstreams, and terminal non-merge task types such as `research_spike`.
- **Dispatcher tests**: each rule in `dispatch/rules.py` has a stage-native fixture; per-stage rule fan-out tested for each `policy=required` stage (`<stage>_work_rule`, `<stage>_review_rule`, `<stage>_advance_rule`); delivery flow covers `holistic_qa.review_approved → pr.ready → pr.in_progress → pr.needs_review → pr.review_approved → pr.done → merge.ready → merge.done → closed`.
- **API tests**: `GET /api/tasks?stage=development&stage_state=in_progress` returns expected set; `PATCH /api/tasks/{id}/stages/{name}` enforces 5-state transitions with `review_policy`-aware legality; MCP tool schemas match; PR verdict and merge result tools store artifacts and transition stages correctly; review tools (`mark_task_needs_review` etc.) raise `IllegalStageTransitionError` on policy mismatch.
- **E2E**: full `epic` task walks ideation → merge with manifest-driven dispatch on a fresh DB, including `policy=required` stages traversing the full `ready → in_progress → needs_review → review_approved → done` chain; `simple_fix` task only emits dev → pr → merge agents and never instantiates planning/research stages.
- **Escalation orthogonality**: `escalate_task` and `de_escalate_task` round-trip preserves `(stage, state, work_attempt_count, review_round_count, entered_at)` exactly.
- **UI**: LifecycleBoard renders with seeded registry, drag-to-advance updates state via PATCH per `review_policy`, swimlane filter by task_type hides empty rows, and pre-existing migrated tasks render from stage rows with the right state group.
- **Performance**: kanban board fetch SQL keeps p99 under existing `KanbanBoard` baseline (denormalized stage manifest in single query, indexed on `(task_id, position)` and `(stage_name, state)`).
- **Dead-code regression**: grep/static tests fail if code writes old `status` / `lifecycle` values or calls removed lifecycle PR/merge tools after the cutover.
- **No regressions**: full pytest of `tests/dispatch/`, `tests/tasks/`, `tests/storage/`, `tests/servers/routes/`, `tests/mcp_proxy/tools/tasks/` plus targeted runs on web build (`pnpm test`, `pnpm build`).

## Out of scope (explicit)
`kind: framing`

- Adding or modifying agents for any stage. Agents are speced in the #12725 plan family; deferred ones land in their own follow-ups and plug into the registry's `default_agent` / `reviewer_agent` slots when ready.
- Implementing PR-Agent / rizzler-style behavior. This plan defines the `pr` / `merge` stage contract and artifacts; #13552 implements the PR and conflict-resolution behavior after the stage cutover.
- Authoring a `test_arch` reviewer agent. The current design sets `test_arch.review_policy=none`; adding review later is a documented extension path (registry change + migration decision) but is not part of this epic.
- Cross-project / multi-tenant kanban — single project only.
- Per-stage time tracking, SLAs, due dates — followup epic if needed.
- Drag-and-drop reordering of stages within a task's manifest — initial UI is read-only on order; mutation via CLI/MCP only. Drag-to-advance state is in scope; drag-to-reorder positions is not.
