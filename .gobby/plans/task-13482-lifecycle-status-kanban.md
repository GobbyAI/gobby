# Post-12725 Lifecycle/Status Kanban Alignment — Strategy Plan for #13482

`plan_kind: strategy` — narrative only, no manifest.

## Context
`kind: framing`

#12725 (gobby build) ships a working state-driven dispatcher, but it leaves three structural debts behind that block the kanban model the user actually wants:

1. **Stage names are hard-coded strings** in `src/gobby/dispatch/rules.py` (`_stage_enabled(task, "plan_review")`, `stage-:<name>` labels). There is no registry, so the UI can't reach in to enumerate or render stages.
2. **Two parallel state axes that don't align.** `status` (open/in_progress/needs_review/review_approved/closed/escalated) is work-claim state on the active item; `lifecycle` (open → plan_review → … → merged) is dispatcher pipeline state; `lifecycle_stage` is a derived 3-value projection. None of them captures "stage X is in state Y" for an arbitrary stage X.
3. **No tri-state per stage.** Current model is binary: a task either *is* at a lifecycle value or isn't. There's no "this task needs holistic-qa" vs "is being holistically-qa'd" vs "passed holistic-qa." The append-only `task_lifecycle_events` table records transitions but doesn't expose per-stage state for queries.

The user's paradigm: every task carries an ordered, *task-type-specific* set of applicable stages, and each stage is in `{needs_doing, in_progress, done}`. Tasks open with a manifest of stages all at `needs_doing`; they close when the last stage hits `done`. A "simple fix" task has only `development → pr → merge` in its manifest and skips ideation/research/etc. entirely. A research spike's manifest terminates at `prd` and never reaches development. The kanban renders one column per stage with tri-state badges per task.

Agents for the new stages (ideation, research, architecture, prd, expansion-qa, code-review-qa, holistic-qa) are already speced inside the #12725 plan family — some active in 0.4.0, some deferred to post-0.4.0 follow-ups. **This epic is not about agents.** It's about replacing the dual-enum state model with a registry-backed, tri-state-per-stage manifest model, then re-pointing the dispatcher, MCP/HTTP/CLI surfaces, and web UI at it. `pr` and `merge` are part of that durable stage contract, so #13552 can later implement PR-Agent / conflict-resolution behavior directly on the new model. `escalated` is preserved as the human-in-the-loop flag; everything else in the `status` enum gets subsumed by per-stage tri-state.

This is a pre-launch clean cutover. Do not build compatibility facades or long-lived legacy write paths. During this epic, callers move to the stage manifest APIs directly; then old `lifecycle`, `lifecycle_stage`, and active `status` semantics are removed instead of preserved as a shadow model.

## Target Model
`kind: framing`

### Canonical stage names (14)
`kind: framing`

`ideation`, `research`, `architecture`, `prd`, `planning`, `adversarial_review`, `test_arch`, `expansion`, `expansion_qa`, `development`, `code_review_qa`, `holistic_qa`, `pr`, `merge`.

Stored in a new DB table `task_stages_registry` (source of truth), seeded from a bundled YAML at `src/gobby/install/shared/registry/stages.yaml` using the same template-sync pattern as workflows/rules.

Per-stage registry row: `name`, `display_label`, `description`, `category` (`discovery|design|verification|implementation|delivery`), `default_agent` (FK to agent registry, nullable for human-only), `position_hint` (kanban ordering), `requires_human` (bool), `is_terminal` (bool — true only for `merge`).

Delivery stage ownership:

- `pr` — creates or updates the pull request, records PR metadata, normalizes PR review output, stores the structured PR verdict, and escalates unresolved PR discussion.
- `merge` — lands the approved PR/branch, runs AI-assisted conflict resolution through explicit Gobby merge tools, verifies the result, stores the merge report, and closes the task when it is terminal.

### Per-task stage manifest
`kind: framing`

New table `task_stage_states`, composite PK `(task_id, stage_name)`:

- `position` (int) — task-specific ordering, may differ from registry hint when operator reorders
- `state` enum: `needs_doing | in_progress | done`
- `entered_at`, `entered_by_session_id`
- `completed_at`, `completed_by_session_id`, `completed_commit_sha`
- `attempt_count` (int) — replaces `planning-round:N` / `qa-attempts:N` labels
- `artifact_refs` (nullable JSON object of pointers into `task_artifacts`) — e.g., the plan path produced by `planning`, the expansion run id produced by `expansion`, or delivery artifacts produced by `pr` / `merge`
- `notes` (text) — QA reviewer feedback, escalation context

The set of rows for a task **is** the task's lifecycle. The "current stage" is the leftmost row by position whose state ≠ `done`. The task is closed when all rows are `done` (or operator marks the task closed terminally).

Delivery-stage artifacts are stored in `task_artifacts` and referenced from the stage row:
`pr_url`, `github_pr_number`, `pr_review_report`, `structured_pr_verdict`,
`merge_commit_sha`, and `merge_campaign_report`.

### Task type taxonomy + default manifests
`kind: framing`

Existing types: `bug`, `feature`, `task`, `epic`, `chore`, `refactor`. Adds:

- `simple_fix` — `[development, pr, merge]`
- `research_spike` — `[ideation, research, prd]` (terminal at `prd`, no merge)
- `architecture_doc` — `[research, architecture]` (terminal at `architecture`)
- `prd_doc` — `[ideation, prd]` (terminal at `prd`)

Defaults for existing types:

- `epic` — full 14-stage pipeline
- `feature` — `[planning, adversarial_review, test_arch, expansion, expansion_qa, development, code_review_qa, holistic_qa, pr, merge]`
- `bug` — `[development, code_review_qa, pr, merge]`
- `refactor` — `[planning, development, code_review_qa, pr, merge]`
- `chore` / `task` — `[development, pr, merge]`

Defaults are seeded as registry rows of a second table `task_type_default_stages (task_type, stage_name, position)` so the UI can render "what would this type's lifecycle look like" without invoking `gobby build`.

Operator overrides at build time: `gobby build <ref> --stages a,b,c` (explicit), `--add-stage <name>[@<position>]`, `--skip-stage <name>`. Profiles (`quick`, `full`, `full-yolo`) become named bundles of stage sets.

### Status retirement
`kind: framing`

Active values (`open`, `in_progress`, `needs_review`, `review_approved`) are subsumed entirely by per-stage tri-state and are dropped from the `status` enum.

`closed` is already representable by `closed_at IS NOT NULL` (column exists). The `is_closed` boolean projection on `state` becomes the canonical read; `status='closed'` writes are removed.

`escalated` is **kept** as the human-in-the-loop flag. Promoted from a value of `status` to a first-class column `is_escalated` (already exists in the projection — just promote it to a real column) plus the existing `escalated_at` / `escalation_reason` fields. Escalation is orthogonal to stage state: any stage can be in `in_progress` while the task is also `is_escalated=true`.

End state: the `status`, `lifecycle`, and `lifecycle_stage` columns are dropped entirely. The implementation must not keep old lifecycle mutation tools as compatibility shims; callers must be ported to stage-native APIs in the same epic.

### Readiness / blocking projection
`kind: framing`

Blocking remains orthogonal to stage state. `task_stage_states.state` stays strictly `needs_doing | in_progress | done`; do **not** add `blocked` as a fourth stage state. A blocked task still has a current stage, but automation and ready queues must not advance it until the blocking condition clears.

Canonical computed projection:

- `current_stage` — leftmost manifest row by `position` whose state is not `done`; `null` when every stage is done
- `is_closed` — `closed_at IS NOT NULL`
- `is_escalated` — first-class escalation flag / `escalated_at IS NOT NULL`
- `active_blocked_by` — unresolved external dependencies; excludes parent tasks blocked only by their own descendants, which are completion blocks rather than work blocks
- `is_blocked` — `is_escalated OR active_blocked_by is non-empty`
- `is_ready` — not closed, not escalated, no unresolved external blockers, and the current stage is actionable
- `owner_session_id` — current claim/session owner for UI badges and automation locks

Dependency blockers resolve by terminality, not by legacy `status`: an upstream dependency blocks downstream work while `upstream.closed_at IS NULL`. This preserves the #12725 invariant that escalated upstream work blocks dependents, without keeping `status='escalated'` as a blocking sentinel. `review_approved` disappears; any transient "approved but not terminal" state is represented by a stage being `done` while the task itself is not yet closed, so dependents still wait until `closed_at` is set.

Dispatcher candidate scans filter by `is_ready` first, then rule evaluation operates on `current_stage` and the manifest rows. LifecycleBoard renders blocked tasks in their stage column with a blocked badge/overlay and a `blocked=true` filter; it does not move them into a synthetic blocked stage.

### Dispatcher coupling
`kind: framing`

`src/gobby/dispatch/rules.py` rules switch from string checks to registry-aware queries:

- `_stage_enabled(task, "plan_review")` → `task_has_stage(task, "planning")` reading `task_stage_states`
- `_skipped_stages(task)` → deleted; "skipped" means "not in manifest"
- Stage advance helpers write `task_stage_states.state` transitions instead of mutating `lifecycle` / `status`
- `list_automation_candidates`, `list_ready_tasks`, `list_blocked_tasks`, and `suggest_next_task` read the readiness projection instead of legacy `status`
- Rule ordering still anchored on registry `position_hint`

PR/merge dispatch is stage-native:

- `holistic_qa` done advances the next manifest row, usually `pr`, to `in_progress`
- `pr` in progress spawns the PR agent once #13552 lands; before then it escalates with a concrete missing-agent/config reason
- `pr` done advances `merge` to `in_progress`
- `merge` in progress spawns `merge-orchestrator`
- `merge` done closes the task when it is the terminal manifest stage

Existing rules in `dispatch/rules.py` are renamed and refactored 1:1 onto the new stage names. New stages without active agents (e.g., post-0.4.0 deferred ones) get pass-through rules that keep them at `needs_doing` until an agent is registered or escalate if the stage is actionable but no agent exists. Rules and agents must use stage mutation APIs; they must not write `status`, `lifecycle`, or `task_stage_states` directly outside the storage manager.

### MCP / HTTP / CLI surfaces
`kind: framing`

New `gobby-tasks` MCP tools:

- `get_task_stages(task_id)` → ordered list of `(stage_name, state, attempt_count, artifact_refs)`
- `start_stage(task_id, stage_name, notes?)`
- `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?)`
- `fail_stage(task_id, stage_name, reason, needs_human?)`
- `add_stage(task_id, stage_name, position)` / `remove_stage(task_id, stage_name)`
- `record_pr_verdict(task_id, verdict, findings, report_ref?)`
- `record_merge_result(task_id, merge_sha?, report_ref?)`
- `list_stages_registry()` / `get_task_type_defaults(task_type)`

HTTP mirrors at `/api/tasks/{id}/stages`, `/api/stages/registry`, `/api/task-types/{type}/default-stages`. List endpoint gains `?stage=development&stage_state=in_progress` filters that drive kanban columns directly.

CLI:

- `gobby tasks list --stage development --state in_progress`
- `gobby tasks stages <ref>` — show per-stage manifest
- `gobby tasks advance <ref> <stage>` — mark stage `done`, auto-advance next to `in_progress` if eligible
- `gobby build <ref> --stages …` — explicit manifest at opt-in time

### Web UI: LifecycleBoard
`kind: framing`

New view `LifecycleBoard` replaces existing `KanbanBoard` in `web/src/components/tasks/`:

- Columns = registry stages, ordered by `position_hint`, filterable by category
- Cards within a column grouped by tri-state: `needs_doing` (top, pale), `in_progress` (middle, accent), `done` (bottom collapsed by default)
- Swimlanes by `task_type` so simple fixes don't render in research/ideation columns
- Stage column hidden by default if no task has it in its manifest (configurable)
- Blocked tasks stay in their current stage column with a blocked badge/overlay and blocker tooltip; board filters include `blocked=true`
- Drag a card right = advance current stage to `done`, advance next to `in_progress`
- Existing `KanbanBoard` (the 6-bucket ready/in_progress/review/blocked/merge_ready/closed view) is replaced during the cutover; do not keep a parallel `StatusBoard`.

`useTasks` fetches stage manifests in the same call (denormalized response) to avoid N+1 on board renders.

### Migration
`kind: framing`

Schema migration in `src/gobby/storage/migrations.py` runs in one direction (no down-migration; this is a structural change):

1. Create `task_stages_registry`, `task_type_default_stages`, `task_stage_states`.
2. Seed registry from bundled YAML.
3. For each existing task: write `task_stage_states` rows by mapping current `(lifecycle, status)` tuple onto the manifest derived from `task_type` defaults plus existing `stage-:<name>` skip labels. Mapping table:
   - `lifecycle=open, status=open` → all default stages at `needs_doing`
   - `lifecycle=plan_review, status=in_progress` → `planning` row at `in_progress`, prior stages `done`, later stages `needs_doing`
   - `lifecycle=plan_review, status=needs_review` → `adversarial_review` row at `in_progress`
   - `lifecycle=in_development, status=in_progress` → `development` row at `in_progress`
   - `lifecycle=merged` → all rows `done`
   - …explicit table covering every observed `(lifecycle, status)` pair, audited against a SQL census before migration runs
4. Drop `stage-:<name>` labels (now redundant with manifest).
5. Port storage, MCP, HTTP, CLI, dispatcher, agents, and web callers to the stage APIs in this epic. Do not add computed-legacy write facades.
6. Keep readiness/blocking projections behaviorally equivalent after cutover: `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, task `state.is_blocked`, and dispatcher candidate scans return the same results as the old model for equivalent fixtures.
7. Drop `lifecycle`, `lifecycle_stage`, and active `status` semantics in the same epic once callers are ported. Any short-lived local migration helpers are deleted before closure.

`task_lifecycle_events` is kept unchanged — it's still the audit trail and gains rows for stage-state transitions.

## Phasing (sub-epics for #13482 expansion)
`kind: framing`

**Phase 1 — Registry + manifest schema.** Create tables, seed from YAML, define `pr` / `merge` delivery artifacts, and write per-task stage rows for all existing tasks. The migration may read legacy columns to derive initial rows, but new writes go through stage APIs only.

**Phase 2 — Stage-native storage + API surface.** New manager modules `_stage_registry.py`, `_stage_states.py` under `src/gobby/storage/tasks/`. New MCP tools, HTTP routes, CLI subcommands. Replace old lifecycle mutation tools with stage-native operations in this phase.

**Phase 3 — Dispatcher refactor.** Rewrite `src/gobby/dispatch/rules.py` to query `task_stage_states` instead of string-matching `lifecycle`. `gobby build` (`src/gobby/build/service.py`) resolves manifest from `task_type` defaults + operator overrides at opt-in time. Stage advance helpers write to manifest.

**Phase 4 — Review / PR / merge stage cutover.** Port review, PR, and merge workflows to stage APIs. `pr` records PR metadata and verdict artifacts; `merge` records merge SHA/report artifacts and closes terminal tasks. #13552 waits for this phase before implementing PR-Agent behavior.

**Phase 5 — Task type expansion + legacy removal.** Register `simple_fix`, `research_spike`, `architecture_doc`, `prd_doc`; seed `task_type_default_stages`; update `gobby tasks create` defaults and validation. Promote `is_escalated`, remove remaining writes to old active status/lifecycle values, then drop legacy columns/tools/tests.

**Phase 6 — Web UI: LifecycleBoard.** New component, new fetch shape, swimlanes by task_type, tri-state visualization, drag-to-advance. Replace the existing `KanbanBoard` instead of keeping a compatibility board.

**Phase 7 — Cleanup.** Remove deprecated `stage-:<name>` label handling, temporary migration helpers, and dead lifecycle/status code. Documentation pass.

Phases 1–4 are blocking for #13552: PR-Agent / advanced merge behavior must target the stage contract, not the old lifecycle model. Phases 5 and 6 can run after the delivery-stage cutover. Phase 7 closes the epic.

## Critical files
`kind: framing`

- `src/gobby/storage/migrations.py` — schema migration (Phase 1, Phase 7)
- `src/gobby/storage/tasks/_models.py` — Task dataclass trim (Phase 5, Phase 7)
- `src/gobby/storage/tasks/_lifecycle_events.py` — keep, gains stage-state events
- New: `src/gobby/storage/tasks/_stage_registry.py`, `src/gobby/storage/tasks/_stage_states.py`
- `src/gobby/dispatch/rules.py` — full refactor (Phase 3)
- `src/gobby/build/service.py` — manifest resolution at build time (Phase 3, Phase 4)
- `src/gobby/tasks/state_semantics.py` — projection helpers retire (Phase 5, Phase 7)
- `src/gobby/mcp_proxy/tools/tasks/_crud.py` — new tools (Phase 2)
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py` — replace lifecycle-specific PR/merge mutations with stage-native operations (Phase 2, Phase 4)
- `src/gobby/servers/routes/tasks.py` — new endpoints + filter params (Phase 2)
- `src/gobby/cli/tasks/crud.py`, `_utils.py` — new flags, new render paths (Phase 2, Phase 4)
- `web/src/components/tasks/KanbanBoard.tsx` — replace with manifest-driven board wiring (Phase 6)
- New: `web/src/components/tasks/LifecycleBoard.tsx`, `StageColumn.tsx`, `StageCard.tsx`
- `web/src/hooks/useTasks.ts` — denormalized stage manifest in fetch (Phase 6)
- `web/src/lib/taskState.ts` — retire 3-value `TaskLifecycleStage` (Phase 7)
- New: `src/gobby/install/shared/registry/stages.yaml` — bundled seed
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

- **Unit tests**: registry CRUD; manifest mutation invariants (position uniqueness per task, stage-name FK to registry, state enum); type→default-stages resolution; build-time override merge.
- **Migration tests**: census every observed `(lifecycle, status, labels)` tuple in a fixture DB, run migration, assert per-task manifest matches expected mapping table, including `pr` / `merge` rows and delivery artifact references.
- **Readiness tests**: post-cutover results for `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and task `state.is_blocked` match equivalent old-model fixtures; cover external blockers, ancestor/descendant completion blocks, escalated upstreams, closed upstreams, and terminal non-merge task types such as `research_spike`.
- **Dispatcher tests**: each rule in `dispatch/rules.py` has a stage-native fixture; delivery flow covers `holistic_qa done → pr in_progress → pr done → merge in_progress → merge done → closed`.
- **API tests**: `GET /api/tasks?stage=development&stage_state=in_progress` returns expected set; `PATCH /api/tasks/{id}/stages/{name}` enforces tri-state transitions; MCP tool schemas match; PR verdict and merge result tools store artifacts and transition stages correctly.
- **E2E**: full `epic` task walks ideation → merge with manifest-driven dispatch on a fresh DB; `simple_fix` task only emits dev → pr → merge agents and never instantiates planning/research stages.
- **UI**: LifecycleBoard renders with seeded registry, drag-to-advance updates state via PATCH, swimlane filter by task_type hides empty rows, and pre-existing migrated tasks render from stage rows.
- **Performance**: kanban board fetch SQL keeps p99 under existing `KanbanBoard` baseline (denormalized stage manifest in single query, indexed on `(task_id, position)` and `(stage_name, state)`).
- **Dead-code regression**: grep/static tests fail if code writes old `status` / `lifecycle` values or calls removed lifecycle PR/merge tools after the cutover.
- **No regressions**: full pytest of `tests/dispatch/`, `tests/tasks/`, `tests/storage/`, `tests/servers/routes/`, `tests/mcp_proxy/tools/tasks/` plus targeted runs on web build (`pnpm test`, `pnpm build`).

## Out of scope (explicit)
`kind: framing`

- Adding or modifying agents for any stage. Agents are speced in the #12725 plan family; deferred ones land in their own follow-ups and plug into the registry's `default_agent` slot when ready.
- Implementing PR-Agent / rizzler-style behavior. This plan defines the `pr` / `merge` stage contract and artifacts; #13552 implements the PR and conflict-resolution behavior after the stage cutover.
- Cross-project / multi-tenant kanban — single project only.
- Per-stage time tracking, SLAs, due dates — followup epic if needed.
- Drag-and-drop reordering of stages within a task's manifest — initial UI is read-only on order; mutation via CLI/MCP only. Drag-to-advance state is in scope; drag-to-reorder positions is not.
