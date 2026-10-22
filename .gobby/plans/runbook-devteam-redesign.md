Plan artifact: `.gobby/plans/runbook-devteam-redesign.md`

# Runbook redesign: single-task DevTeams and independent releases

**Plan ID:** runbook-devteam-redesign

## Context
`kind: framing`

This implements Josh's decisions from October 20–21, 2026. The evidence inputs are `.gobby/plans/research/runbook-redesign-assessment-2026-10-20.md` and `.gobby/plans/research/runbook-cohort-postmortem-2026-10-20.md`. The first inventories mechanisms and authority gaps; the second explains why landed work waited days for close and why closed tasks retained worktrees. Their proposals are evidence, not authority where the decisions below supersede them.

The post-mortem's structural findings are the execution priorities: remove the close relay and transcript-proof machinery; give one Dev Manager the complete task lifecycle; bind each team to one task, branch and tree; make successful close end the entire team; separate task completion from batch activation. The same failure appeared during implementation preparation on October 21: task #23850's four named pytest assertions failed, yet the retained-RED write gate refused all four. That is current evidence of the friction being removed, not permission to bypass the current gate.

Josh approved implementing the complete redesign, including finishing removal of `gobby build`. This file materializes the approved decisions. It is initially narrative-only: base validation must pass before a server-derived M1 manifest is applied. Expansion validation has not run until that manifest exists. No task, rollout, or test is described as completed merely because it appears here.

## Decision Record
`kind: framing`

1. Admin contains Assistant, Orchestrator, Archivist, Release Manager and Triage. Only Assistant and Triage may message the Orchestrator. Agents may message Triage directly; Triage sends a synthesized digest every five minutes by default with `wake=false`. Dev Managers still own team execution and handoffs.
2. A DevTeam contains Test Writer, Developer, Code Reviewer and Dev Manager. It serves exactly one task in one registered branch/worktree. Successful close collapses every seat and its tab, including the manager. There is no recycling or next-task handoff. Commits, task history and lessons survive.
3. The Test Writer commits tests before implementation. The reviewer checks the stored criteria, diff and applicable security boundaries, independently runs the tests, and verifies the test-first history. The Dev Manager performs final checks, writes the summary and durable lessons, lands, and closes. Transcript-parsed validation and edit-time TDD enforcement are deleted.
4. Close retains four data invariants: children are closed; a linked commit or explicit no-work disposition exists; no task-attributed edits remain uncommitted; and a work-bearing linked commit is an ancestor of the configured integration branch, `0.5.0` here. It also mechanically checks the approved role-owned records required by Decision 14. Summary, scope, criteria, test evidence, acceptance evidence and review judgment belong to the responsible seats; close never reconstructs them from transcripts.
5. The first reviewer is Codex `gpt-6.1-sol`, medium reasoning. First rejection returns to the original Developer, then the same reviewer reviews again. On the second rejection that Sol reviewer takes the claim and fixes the task; a fresh Claude `claude-opus-5-5`, xhigh reasoning, reviews that work. Third rejection escalates to Triage. An editor or former claimant can never approve its own work.
6. The dedicated review-step writer atomically records full feedback, the review record and task rejection count. Replaying the same review of the same SHA does not increment the count. Triage can authorize an audited reset; history remains. Section 2.1 renames `validation_status`, `validation_feedback`, and `validation_fail_count` to `review_status`, `review_feedback`, and `review_fail_count`, without aliases. The pending/valid/invalid/error task summary remains a projection for existing readers; section 2.3 makes the role-owned review record the sole review authority. Infrastructure errors may update the error summary but cannot approve a step or increment rejections. Archive old close-review results; open tasks start the new review epoch at pending/count zero.
7. The October 22 Orchestrator ruling on #24797 applies Josh's approved generic slate now: open, in_progress, blocked, needs_human, escalated, deferred and closed. Closed tasks require a reason from done, moot, superseded and wont_do; deferred tasks require a follow-up date. Store intrinsic state and project dependency blockers on read; claim changes refresh the SQL mirror. Closure and escalation take precedence over holds, holds over dependency blockers, and blockers over ownership. Human-review routing, follow-up scheduling or wake, and new UI controls remain owned by #23817; this task supplies the values, storage invariants and consistent read contract. Review progress remains separate from task state.
8. Dev Manager lands using another reviewer's exact-SHA approval. Disjoint integration changes may land without rerunning every task's tests; preserve ancestry with a two-parent merge when needed. Overlap/conflict goes to Triage. Semantic keep/discard decisions go to Josh through Assistant. Schema carriers and required project synchronization stay enforced. Restart approval, activation receipts and creator/delegator authority-transfer relays retire.
9. Release Manager runs a roughly two-hour batch from the last successful release SHA to a pinned integration SHA. Its owned lease expires after ten minutes and renews at least every sixty seconds. During the window, landings and NEW DevTeam admissions wait. Existing teams continue work, testing, review and recovery; replacement seats within an admitted team remain allowed. No worker drain, quiet-fleet requirement, parking, heavy grants or new resource caps.
10. A release validates affected packages and relevant lint/type/build checks, then promotes coherent binaries, restarts and runs fixed smokes. Global announcements, schema preflight, version/source identity checks and new-inode promotion remain. A lost lease invalidates activation; a stale owner cannot renew or clear a successor's lease. Both holds lift on release or expiry. Failure leaves the successful baseline unchanged and creates a regression task for a new DevTeam after the window; source tasks remain closed.
11. Cargo uses one project-ID-scoped target named `cargo-target-<project_name>` and consistent `CARGO_HOME` across supported agent, sandbox, release and project-shell entry points. Deleting a task worktree never deletes the shared cache. Do not edit global shell dotfiles. Wider host-performance research remains owned by #23902.
12. Rename/rewrite `lane-manager.yaml` to `dev-manager.yaml` and `inbox-manager.yaml` to `triage.yaml`; add `test-writer.yaml`; rewrite `release-manager.yaml`. Josh's October 21 ruling supersedes the original deletion list: any agent used in runbooks stays. Keep `log-monitor.yaml`, `metrics-monitor.yaml`, `recovery-steward.yaml`, `task-close-reviewer.yaml`, and `expansion-qa.yaml`. Repurpose expansion QA for runbook-spawned post-expansion tree, dependency and plan-consistency checks. Keep `researcher.yaml`, `security-reviewer.yaml`, and `consolidator.yaml` for standalone use. Keep GitHub `triage-agent` distinct from fleet Triage and preserve current planning roles. Old manager names receive no compatibility aliases; any other runbook-used definition proposed for deletion goes to the Orchestrator first.
13. One complete pilot, then a small concurrent cohort, proves the lifecycle and release success/failure cases before old runbooks retire. No push is authorized by this plan. Operator-owned uncommitted work remains untouched.
14. The October 22 Orchestrator ruling reconciles Decisions 3–6 with Josh's generic close design (memory 3cb34179): test, implementation, review and closing are role-owned step records with pending/in_progress/approved/rejected status. Dedicated writer tools enforce role ownership. Project configuration and task type determine required records, and close checks their approval mechanically. Section 2.3 verdict recording writes the review step; the Dev Manager writes closing. Retain rejection history and independent exact-SHA approval without a second verdict authority. The per-epic DevTeam addendum remains outside #24797.

## Existing work and dependency integration
`kind: framing`

The existing `.gobby/plans/remove-gobby-build.md` and epic #23341 remain authoritative for their existing leaves. Do not duplicate or re-expand them under this plan. Finish #23850 (external dispatch drivers/callers), #23851 (build/dispatch packages), #23852–#23856 (stage-only agents and tools), #23857/#23858/#24062 (consumers and storage), #23859/#23860 (guidance) and #23861 (schema drop) under their current ownership. Preserve already-landed P1 and sections 2.1–2.3.

The prerequisite epic #23341 and all of #24317 closed on October 22 at `2fcc25043ec9c6cd3e8fb3ea3fc5bd4a76e23c0a`. Its absorbed #24799 delivered sections 1.1–1.4. The build drop uses migration 472, and migration 473 retires its stored configuration. Sections 2.1 onward execute under #24797 alone, without expansion or per-leaf claims, as Josh explicitly directed. Preserve task-close-reviewer and all other protected definitions under the later operator ruling. Numeric allocation remains mechanical: verify all refs and the live schema immediately before writing; the next candidate migrations are 474 and 475.

To avoid a cycle, #23861 waits on this plan's 1.4, while 1.1–1.4 wait only on the needed code/storage removals, not #23861 or epic #23341. Sections 2.1 onward that need the schema wait on #23861. Task #23817 remains open for human-review routing, deferred follow-up behavior and their UI; the approved seven-state values and minimum invariants belong here. Verify #23433's snapshot-only worktree indexing delivery before changing its status. Do not adopt #23902 or create competing performance work.

## Constraints and evidence
`kind: framing`

Evidence was checked at integration commit `2dc56d7077b7623bf286b55d15ceabc937ad1056`. `gcode evidence` for `src/gobby/tasks/land_commit.py:430-465` returned excerpt hash `3b9d4fcd73b476676580a691212ed49c4c4e3bc55b379647672ea4eb8fc70a43`: `453| missing_carriers = missing_schema_carriers(paths)` and `462| if activation_class in {"restart", "cutover"}:` / `463| required.add("restart")`. Preserve the former and remove the latter receipt prerequisite. The supplied research provides the remaining baseline code map; refresh only references changed by prerequisite leaves.

Bounded consumer inventories were run with `gcode grep -w validation_fail_count src/ web/ crates/ -m 100`, `gcode grep -w TASK_CLOSE_REVIEWER_AGENT src/ -m 50`, and `gcode outline src/gobby/tasks/state_semantics.py`. Slow worktree index navigation required direct bounded filename/literal inventories. The rename reaches task creation/model/manager/transitions/updates/de-escalation, CLI reset/status, sync serialization, web task types/detail/snapshots and fixtures. Close-review retirement reaches resume, spawn guards/factory, runner reconciliation, MCP request context, agent queries, delegation and receipts. Sections below own those consumer groups.

For every implementation leaf, refresh its literal/import sweep after prerequisites land, including tests, string-based patches, templates, reference audits and generated consumers. Historical migrations and completed research remain immutable. Files at 850 lines require the named decomposition in the same leaf; production files stay below 1,000. Schema/config edits carry generated contracts, schema identity, catalog/tests, integration-tip patch bumps for every shipped dependent, Cargo.lock and managed pins. Use the repository's generator and promotion functions, never hand-copy binaries.

Use isolated tests: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused targets>`. No full pytest suite. Run relevant Ruff, mypy, frontend/Rust checks, test-quality/types and suppression audits while those are required by the active workflow. New flow enforcement is exercised with isolated state before live registry activation. Design edits in task UI load impeccable and the project design contract.

## P1: Delivered prerequisites
`kind: framing`

Sections 1.1–1.4 were completed under #24799 within #23341's first landing. Their original acceptance criteria remain in those closed tasks and the landing evidence; they are not new implementation deliverables under #24797.

- 1.1 delivered the shared four-invariant close/preview evaluator, atomic finalization and idempotent close.
- 1.2 removed retained-RED write enforcement and transcript-based TDD close gates while keeping tests-first reviewer guidance.
- 1.3 retired automatic close-review launch/recovery and retained immutable historical review access. The task-close-reviewer definition remains protected by the later operator ruling.
- 1.4 removed close-only evidence evaluators while preserving found-work and general transcript consumers.

Delivery evidence: `0911dcc603` and its linked correction commits; final #23341 tip `2fcc25043ec9c6cd3e8fb3ea3fc5bd4a76e23c0a`; schema-473 activation receipt `5dcd7e32`. The final 20-module acceptance run passed 276 tests. The complete child/criteria and exact-command record is `.gobby/session-context/17257-landing1-candidate.md`, supplemented by the #23341 close summary and its independent landing receipts.

## P2: Task review and landing authority
`kind: framing`

### 2.1 Rename review fields and archive old results (depends: #24799, #23861)
`kind: deliverable`
`[category: code]`

Targets:
- `crates/gcore/assets/schema/migrations/474_runbook_review_state.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the complete schema catalog carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: carry regenerated schema identity
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify schema and review data migration
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: verify the matching schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: replace generated expected schema identity
- `src/gobby/storage/review_history.py::*` — scope-reason: read archived close-review results from the historical table
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: replace review fields in deserialization and both response shapes
- `src/gobby/storage/tasks/_creation.py::*` — scope-reason: initialize renamed fields
- `src/gobby/storage/tasks/_manager.py::*` — scope-reason: rename update contract
- `src/gobby/storage/tasks/_update_facade.py::*` — scope-reason: extract the cohesive task metadata-update facade
- `src/gobby/storage/tasks/_updates.py::*` — scope-reason: rename SQL updates and arguments
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: rename/reset counters and preserve atomic transition primitive
- `src/gobby/storage/tasks/_transitions_facade.py::*` — scope-reason: expose renamed transition contract
- `src/gobby/storage/tasks/_de_escalation.py::*` — scope-reason: audited review reset naming
- `src/gobby/storage/tasks/_lifecycle.py::*` — scope-reason: update reopen/reset behavior
- `src/gobby/cli/tasks/_crud_detail.py::*` — scope-reason: replace old review reset fields
- `src/gobby/cli/tasks/crud.py::*` — scope-reason: update CLI descriptions and reset parameters
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py::*` — scope-reason: update reset API contract
- `src/gobby/sync/tasks.py::*` — scope-reason: rename review serialization without aliases
- `src/gobby/failure_categories.py::*` — scope-reason: retire close-review-only failure vocabulary
- `web/src/types/tasks.ts::*` — scope-reason: replace task review field names
- `web/src/components/activity/TasksTab.tsx::*` — scope-reason: replace snapshot review-field mapping
- `web/src/components/activity/TasksTabDetailPanel.tsx::*` — scope-reason: display independent review status feedback and count
- `web/src/components/activity/taskdetail/TaskDetailKV.tsx::*` — scope-reason: render the four independent review outcomes
- `web/src/components/activity/__tests__/fixtures.ts::*` — scope-reason: update task fixtures
- `web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx::*` — scope-reason: test renamed review fields
- `web/tests/fixtures/webChatRestorePlan.ts::*` — scope-reason: update restored task review fields
- `src/gobby/sync/task_review_serialization.py::*` — scope-reason: own review backup encoding and validation
- `tests/storage/test_review_field_migration.py::*` — scope-reason: verify all live review outcomes and retired-field refusals
- `Cargo.lock`
- `crates/gcore/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: align managed binary pins with schema carrier versions
- `src/gobby/code_index/gcode_versions.py::*` — scope-reason: align gcode capability floors with schema carrier versions

**Research context:** The field census in Constraints includes all runtime groups after stage removal. Preserve old results as read-only history before resetting open tasks to pending/count zero in a new review epoch. Closed tasks keep historical outcome attribution. Rename the three fields atomically with every live reader/writer; no dual-read aliases. Migration 474 follows the completed 472 build drop and 473 retired-config cleanup; recheck allocation before writing. Split `src/gobby/sync/tasks.py` by moving review payload encoding to `task_review_serialization.py`. Split `src/gobby/storage/tasks/_manager.py` by moving its update-method facade into new `src/gobby/storage/tasks/_update_facade.py`, keeping mutations in the existing update implementation. Include schema fixtures and every recursively affected shipped binary's patch bump; recheck dependencies rather than blindly bumping unrelated binaries.

**Granularity:** The cross-language rename exceeds six production files but is one atomic data contract. A split deployment would require the compatibility layer Josh rejected. Migration, adapters and UI consumers therefore ship together.

**Acceptance:**
- 2.1.1 - Migration preserves historical results and initializes the new epoch for open tasks without losing feedback history. test: `crates/gcore/src/schema/runner_tests.rs::review_epoch_migration_preserves_history_and_resets_open_tasks`.
- 2.1.2 - Storage, sync, CLI/MCP and web expose only review-prefixed fields with pending/valid/invalid/error values. test: `tests/storage/test_review_field_migration.py::test_review_contract_has_no_legacy_fields`.
- 2.1.3 - Baseline, catalog, native schema identity, Python expected identity and version pins agree. file: `src/gobby/storage/schema_expected_identity.json`.

**Verification:** Planned isolated migration/contract tests, focused task detail tests, mypy/TypeScript checks and affected Rust contract tests. Deployment uses coordinated coherent promotion.

**Implementation reconciliation:** Baseline 420 remains immutable; fresh databases apply baseline plus migration 474, while existing databases apply the same migration. The native upgrade test seeds schema 473 and verifies preserved review rows, task-comment snapshots, new epochs and runtime read-only privileges. CLI/MCP reset arguments were already retired with the old automatic reviewer, so those adapters need no rename. Ordinary reopen preserves review failures; section 2.3 supplies the authorized audited reset. `gcode_versions.py` already derives its floor from the managed version pin. The update facade and backup codec remain in their existing owning packages.

### 2.2 Seven task state values agree on every surface (depends: 2.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/state_semantics.py::*` — scope-reason: implement the approved seven-state projection
- `src/gobby/storage/tasks/_queries.py::*` — scope-reason: align list/filter/count state queries
- `src/gobby/storage/tasks/_aggregates.py::*` — scope-reason: align aggregate state counts
- `src/gobby/storage/tasks/_blocking.py::*` — scope-reason: align dependency and hold blockers with the approved state semantics
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: make claim/release transitions agree with state projection
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: unify serialized state
- `src/gobby/mcp_proxy/tools/tasks/_listing.py::*` — scope-reason: expose the same state/filter contract
- `src/gobby/cli/tasks/_utils/rendering.py::*` — scope-reason: render the same seven-state vocabulary in CLI task lists
- `src/gobby/servers/routes/tasks.py::*` — scope-reason: match HTTP state serialization
- `web/src/types/tasks.ts::*` — scope-reason: use the seven-state union
- `web/src/lib/taskState.ts::*` — scope-reason: share canonical state projection and read-only filters
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: render the approved task state values
- `web/src/components/activity/TasksTabFilters.tsx::*` — scope-reason: filter the approved state vocabulary without adding lifecycle controls
- `crates/gcore/assets/schema/migrations/475_runbook_task_states.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the complete state schema catalog carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: carry state schema identity
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify claim-trigger state computation
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: verify matching state schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: replace generated expected state schema identity
- `tests/tasks/test_task_states.py::*` — scope-reason: verify canonical state precedence SQL parity and storage constraints
- `Cargo.lock`
- `crates/gcore/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: align state migration binary pins
- `src/gobby/code_index/gcode_versions.py::*` — scope-reason: align gcode capability floors with state migration versions
- `src/gobby/storage/tasks/__init__.py::*` — scope-reason: export the shared SQL projection to aggregate consumers
- `src/gobby/storage/tasks/_manager.py::*` — scope-reason: forward the canonical state filter
- `src/gobby/storage/tasks/_close.py::*` — scope-reason: validate required canonical close reasons
- `src/gobby/storage/tasks/_lifecycle.py::*` — scope-reason: preserve immutable legacy review matching through migration snapshots
- `src/gobby/servers/routes/tasks_comment_routes.py::*` — scope-reason: refuse ordinary creation or deletion of migration snapshots used by historical close repair
- `src/gobby/sync/tasks.py::*` — scope-reason: validate and round-trip holds dates and canonical close reasons
- `src/gobby/mcp_proxy/tools/hub.py::*` — scope-reason: align cross-project filtering serialization and counts
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py::*` — scope-reason: expose only canonical close reasons
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::*` — scope-reason: canonicalize no-work finalization
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: expose all seven state counts
- `src/gobby/servers/routes/admin/_stats.py::*` — scope-reason: reuse the SQL projection for admin counts
- `src/gobby/servers/routes/tasks_lifecycle_routes.py::*` — scope-reason: validate the HTTP close reason enum
- `src/gobby/cli/tasks/crud.py::*` — scope-reason: expose state filtering and canonical close reasons
- `src/gobby/cli/tasks/_crud_listing.py::*` — scope-reason: forward state filtering and align list labels
- `src/gobby/tasks/close_invariants.py::*` — scope-reason: apply the canonical no-work reason set
- `src/gobby/plans/_task_record_state.py::*` — scope-reason: normalize only canonical task states
- `src/gobby/plans/_task_store.py::*` — scope-reason: own task record loading and canonical state coercion extracted from coverage
- `src/gobby/plans/coverage.py::*` — scope-reason: use done coverage and delegate record loading to the existing task store
- `src/gobby/plans/deferral.py::*` — scope-reason: treat done as delivered and canonical no-work reasons as unowned
- `src/gobby/tasks/expansion/_deferrals.py::*` — scope-reason: align deferral owner closure semantics
- `src/gobby/tasks/expansion/_reconcile.py::*` — scope-reason: align preserved and replaced closure semantics
- `src/gobby/tasks/expansion/_reset.py::*` — scope-reason: use canonical no-work closure for reset
- `src/gobby/tasks/expansion/_validate.py::*` — scope-reason: validate canonical deferral ownership
- `src/gobby/workflows/memory_review_conditions.py::*` — scope-reason: exclude canonical no-work dispositions from delivery review
- `src/gobby/agents/watchdog/recovery.py::*` — scope-reason: extract diagnostic operations and retain the narrowed task-state type
- `src/gobby/agents/watchdog/recovery_diagnostics.py::*` — scope-reason: own recovery evidence queries and audit logging with an explicit event-state type
- `tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py::*` — scope-reason: preserve closed-task recovery precedence with a canonical close reason
- `src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml::*` — scope-reason: align no-work exemptions with the close contract
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: replace retired close reasons before the full role rewrite
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`
- `src/gobby/install/shared/skills/gobby/references/plan/coverage.md`
- `docs/guides/tasks.md`
- `docs/contracts/plan-coverage.md`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: register the new task-state migration
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: prove legacy closure archival and state rebucketing on upgrade
- `crates/gcore/src/grant/presentation/tests.rs::*` — scope-reason: align grant identity expectations
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: regenerate signed schema-identity golden
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: regenerate signed schema-identity golden
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: regenerate signed schema-identity golden
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: regenerate signed schema-identity golden
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: regenerate signed schema-identity golden
- `tests/cli/test_tasks_cli.py::*` — scope-reason: verify state filter and close-reason CLI contracts
- `tests/mcp_proxy/tools/test_hub.py::*` — scope-reason: verify aggregate state parity with real storage
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py::*` — scope-reason: use canonical closure fixtures
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::*` — scope-reason: preserve cleanliness coverage under canonical reasons
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: preserve close behavior coverage under canonical reasons
- `tests/plans/test_coverage.py::*` — scope-reason: characterize canonical task record loading and coverage
- `tests/plans/test_covers.py::*` — scope-reason: align coverage label fixtures with canonical reasons
- `tests/plans/test_deferral.py::*` — scope-reason: verify canonical deferral owner states and closure
- `tests/servers/routes/admin/test_stats.py::*` — scope-reason: verify seven-state aggregates
- `tests/servers/routes/test_tasks_routes.py::*` — scope-reason: verify state filters totals and canonical close input
- `tests/storage/tasks/test_claim_task_errors.py::*` — scope-reason: retain claim refusal fixtures under the close constraint
- `tests/storage/tasks/test_close_eligible_ancestors.py::*` — scope-reason: repair the stale update-facade concurrency seam
- `tests/storage/tasks/test_closed_candidate_repair.py::*` — scope-reason: preserve archived candidate reason identity and reject drift
- `tests/storage/tasks/test_closed_parent_guard.py::*` — scope-reason: align parent closure fixtures
- `tests/storage/tasks/test_legacy_validation_criteria_close.py::*` — scope-reason: align historical criteria fixtures
- `tests/storage/tasks/test_readiness_equivalence.py::*` — scope-reason: distinguish dependency blockers from escalation and holds
- `tests/storage/tasks/test_sweep_stale_claims.py::*` — scope-reason: preserve stale-claim coverage with canonical closures
- `tests/storage/tasks/test_validation_exit_ramp.py::*` — scope-reason: align no-work exit fixtures
- `tests/sync/test_task_jsonl_restore_shape.py::*` — scope-reason: verify hold backup restore and malformed input refusal
- `tests/tasks/expansion/test_reexpand_reconciliation.py::*` — scope-reason: verify canonical closure preservation and replacement
- `tests/tasks/expansion/test_tasks_expansion_apply.py::*` — scope-reason: verify canonical deferral ownership
- `tests/tasks/test_close_invariants.py::*` — scope-reason: verify canonical no-work disposition invariants
- `tests/tasks/test_serialize_task_state.py::*` — scope-reason: verify serialized state authority
- `tests/tasks/test_task_backup.py::*` — scope-reason: align backup closure fixtures
- `tests/workflows/test_memory_review_conditions.py::*` — scope-reason: verify no-work review exclusion
- `tests/workflows/test_seat_step_loops.py::*` — scope-reason: verify canonical developer close instructions
- `tests/workflows/test_step_binding.py::*` — scope-reason: verify canonical no-work candidate guard
- `web/src/components/activity/ActivityRowStatusDot.tsx::*` — scope-reason: export distinct glyphs for canonical task states
- `web/src/components/activity/TaskTreeRow.tsx::*` — scope-reason: use canonical state glyphs
- `web/src/components/activity/TasksTabModel.ts::*` — scope-reason: align task filter labels
- `web/src/components/activity/taskdetail/TaskDetailRelationships.tsx::*` — scope-reason: render canonical relation states
- `web/src/components/activity/taskdetail/__tests__/taskDetailFormat.test.ts::*` — scope-reason: align detail format expectations
- `web/src/components/tasks/__tests__/TaskBadges.test.tsx::*` — scope-reason: verify seven states without color-only distinctions
- `web/src/lib/__tests__/taskNormalization.test.ts::*` — scope-reason: verify canonical open normalization
- `web/src/lib/__tests__/taskState.test.ts::*` — scope-reason: verify holds filters and partial payload consistency

**Research context:** After the build removal, `projected_task_state` and migration 472's SQL function return ready/closed/escalated; the SQL update trigger omits ownership. Replace ready with open, project claimed tasks as in_progress, and refresh the mirror on claim/release. Reuse the existing external-blocker predicate, which excludes a task's own descendants as work blockers. Add only the intrinsic hold data needed for needs_human/deferred and a deferred follow-up date, with storage checks; schedule no wake or automatic release. Preserve the existing awaiting-human-review label as an input to needs_human. SQL stores the six intrinsic states; dependency blockers are projected on read with Decision 7 precedence. Migration 475 re-buckets rows and normalizes legacy close reasons while archiving their original text. Historical baseline 420 stays immutable. Reuse the shared state serializer across HTTP, MCP, CLI, web and native clients; eliminate ready/needs_review/review_approved/done as task states while keeping step progress separate. The refreshed query module is 538 lines, so no decomposition is needed. Extend existing read-only badges and filters; new hold/defer controls remain #23817.

**Granularity:** One state contract spans SQL, Python and clients; all authorities must change together to prevent contradictory task lists. Package-specific tests remain in this leaf.

**Acceptance:**
- 2.2.1 - State precedence, dependency unblock and claim/release transitions match the approved matrix. test: `tests/tasks/test_task_states.py::test_state_precedence_and_claim_transitions`.
- 2.2.2 - SQL filtering/counts and public serialization agree, including blocked claimed tasks. test: `tests/tasks/test_task_states.py::test_query_and_surface_state_parity`.
- 2.2.3 - Retired task state values are rejected, deferred requires a follow-up date, and closed requires a canonical reason. test: `tests/tasks/test_task_states.py::test_state_storage_invariants`.

**Verification:** Planned isolated trigger/migration tests, state contract tests and task UI/native serialization checks.

**Implementation evidence:** The shared SQL projection now supplies list, hub, admin and CLI counts. CLI statistics include all seven states and size row queries from the total rather than truncating at 10,000. Coverage task loading moved into the existing `_task_store.py`, reducing `coverage.py` from 990 to 844 lines without another abstraction. Archived close-candidate repair matches the original reason by closure timestamp and refuses changed reasons or missing snapshots. Ordinary comment endpoints cannot forge or delete the system migration snapshots. Backup exports intrinsic holds so removing an awaiting-human-review label still releases a restored task. The state helpers preserve canonical names on partial frontend payloads. The narrower task-state type exposed a watchdog lifecycle-event fallback annotation; its observational diagnostics moved from `src/gobby/agents/watchdog/recovery.py` to `src/gobby/agents/watchdog/recovery_diagnostics.py`, with mutable recovery state staying in the coordinator. The result is 825 and 197 lines respectively, with unchanged diagnostic call surfaces and log channel.

Observed checks include 91 watchdog tests passing before and after extraction, 166 plan tests, 41 backend regressions, 63 frontend tests, four focused CLI statistics tests and ten backup/restore tests. The broader workflow/expansion run initially had 428 passes and 21 stale close-review expectation failures. Attribution approval was received, all expectations were corrected, and the focused rerun had 135 passes with two response-shape assertion failures; both corrected assertions passed their final rerun. No workflow failure remains unresolved. TypeScript checking, full-source mypy, frontend lint and test quality/type/suppression ratchets passed before the final review corrections; final checks include those corrections. Four isolated browser captures verified the seven state badges in dark/light themes at desktop/mobile widths. The native client has no task-state projection to update; its version moves only because its shared native dependency changed.

### 2.3 Role-owned step records and atomic independent review (depends: 2.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/close_receipts.py::*` — scope-reason: retire independent-review receipts as an alternate approval writer
- `src/gobby/tasks/step_records.py::*` — scope-reason: new module owning record storage, current-record selection, policy requirements, the atomic review counter and landing approval
- `src/gobby/mcp_proxy/tools/tasks/_step_records.py::*` — scope-reason: new module defining the four dedicated role-owned writer tools
- `src/gobby/servers/routes/tasks_comment_routes.py::*` — scope-reason: reserve step-record authorship and immutability from ordinary comment creation and deletion
- `src/gobby/mcp_proxy/tools/tasks/_factory.py::*` — scope-reason: register four dedicated role-owned step writer tools and stop registering the MCP restore tool
- `src/gobby/workflows/agent_models.py::*` — scope-reason: add the `task_step_writes` definition grant field
- `src/gobby/config/tasks.py::*` — scope-reason: define the step-record enforcement switch and the task categories that need test and review records
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: verify the generated carrier stays unchanged, because the task close policy is project config outside the daemon contract
- `.gobby/project.json::*` — scope-reason: turn on step-record enforcement in Gobby's task close policy
- `src/gobby/tasks/close_invariants.py::*` — scope-reason: mechanically check required approved records in preview and close
- `src/gobby/mcp_proxy/tools/tasks/_close_context.py::*` — scope-reason: resolve project policy and current task step records for close
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::*` — scope-reason: evaluate required and missing step records with the close gates
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py::*` — scope-reason: count the step-record gate among the evaluated close gates
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::*` — scope-reason: return missing step records as a typed close refusal
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py::*` — scope-reason: describe the step-record gate in the close tool contract
- `src/gobby/storage/tasks/_close.py::*` — scope-reason: enforce the required approved records inside the close transaction under the final task lock
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: add the closing error that names missing approved step records
- `src/gobby/storage/tasks/__init__.py::*` — scope-reason: export the closing error that names missing approved step records
- `src/gobby/mcp_proxy/tools/tasks/_crud.py::*` — scope-reason: show the current epoch's step records in the full task view
- `src/gobby/sync/task_review_serialization.py::*` — scope-reason: round-trip role-owned record history without restoring approval through user comments
- `src/gobby/sync/tasks.py::*` — scope-reason: read step-record history into backups, validate it before restore and preserve linked-commit order
- `src/gobby/mcp_proxy/tools/tasks/_backup.py::*` — scope-reason: remove the MCP restore tool and fix backups to the default path
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-gobby-tasks-cli.yaml::*` — scope-reason: block agents from the operator-only task restore command
- `src/gobby/workflows/enforcement/blocking.py::*` — scope-reason: classify the step writers as task mutations and drop the restore tool
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: atomic failure increment and third-rejection escalation
- `src/gobby/storage/tasks/_de_escalation.py::*` — scope-reason: audited Triage reset
- `src/gobby/storage/tasks/_updates.py::*` — scope-reason: let the audited reset write the review epoch
- `src/gobby/tasks/land_commit.py::*` — scope-reason: land only for a session holding an approved review record for the SHA the current review approves
- `src/gobby/workflows/engine/seat_transitions.py::*` — scope-reason: drive the reviewer land gate and stale-approval clearing from the same review record
- `src/gobby/mcp_proxy/tools/task_commits.py::*` — scope-reason: share checkout machine resolution with the step writers and retire the independent-review receipt kind
- `src/gobby/install/shared/workflows/agents/code-reviewer.yaml::*` — scope-reason: grant the review step writer and gate LAND on the reviewer's approved review record
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml::*` — scope-reason: grant all four step writers for exhausted-avenue record repair
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/backups.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/task-close-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md`
- `docs/guides/tasks.md`
- `docs/design/task-authority-audit.md`
- `tests/tasks/test_step_records.py::*` — scope-reason: new storage tests for record atomicity, independence, rejection counting and policy selection
- `tests/mcp_proxy/tools/tasks/test_step_record_authority.py::*` — scope-reason: new MCP tests for role-owned writers, definition grants and task views
- `tests/fixtures/review_approvals.py::*` — scope-reason: new shared fixture that writes an approved review record at the task tip
- `tests/tasks/test_close_invariants.py::*` — scope-reason: verify project and task-category record requirements
- `tests/servers/routes/test_tasks_routes.py::*` — scope-reason: verify ordinary comments cannot forge or delete role-owned step records
- `tests/tasks/test_land_commit.py::*` — scope-reason: landing accepts only an approved review record for the exact approved SHA
- `tests/tasks/test_close_receipts.py::*` — scope-reason: the retired independent-review receipt kind is refused
- `tests/workflows/test_seat_step_loops.py::*` — scope-reason: the reviewer land gate follows the reviewer's own review record
- `tests/mcp_proxy/tools/tasks/test_cross_project_task_refs.py::*` — scope-reason: cross-project step writes and landing resolve through lifecycle authority
- `tests/cli/installers/test_landing_guard_hook.py::*` — scope-reason: seed landing approval through a review record
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py::*` — scope-reason: close refuses missing policy-selected records
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: close and preview report missing policy-selected records
- `tests/storage/tasks/test_closed_parent_guard.py::*` — scope-reason: pass the database the close transaction now reads policy from
- `tests/e2e/test_task_close_checklist_e2e.py::*` — scope-reason: end-to-end closes write the required step records
- `tests/e2e/test_parallel_clones.py::*` — scope-reason: end-to-end clone closes write the required step records
- `tests/sync/test_task_step_record_backup.py::*` — scope-reason: new backup and restore round-trip tests for step-record history
- `tests/storage/test_review_field_migration.py::*` — scope-reason: backups carry an empty step-record history for migrated tasks
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py::*` — scope-reason: task views list step records
- `tests/mcp_proxy/tools/test_tasks_list_coverage.py::*` — scope-reason: receipts use the surviving kinds
- `tests/mcp_proxy/tools/tasks/test_handed_off_claims.py::*` — scope-reason: drop the review receipt that claim logic never read
- `tests/mcp_proxy/tools/test_internal_action_tools.py::*` — scope-reason: restore is the operator's CLI only
- `tests/workflows/test_gobby_tasks_cli_rule.py::*` — scope-reason: agents are blocked from the task restore command
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: step writers count as task mutations and restore does not
- `tests/agents/test_coordination_seat_definitions.py::*` — scope-reason: the orchestrator holds all four step writers
- `tests/agents/test_review_observation_seat_definitions.py::*` — scope-reason: the code reviewer records review steps

**Research context:** The October 22 ruling replaces the proposed parallel verdict subsystem with four records: test, implementation, review and closing. Each has pending/in_progress/approved/rejected status, server-derived agent definition/version/session identity, timestamps, full feedback and an epoch; work-bearing evidence also binds the exact commit SHA. Reuse durable task comments as the append-only record store, with a reserved author type unavailable to ordinary comment writers and task-row locking for latest-record selection, sequence and idempotency. No new review table or second verdict ledger is needed. Expose `record_test_step`, `record_implementation_step`, `record_review_step` and `record_closing_step`; each tool fixes its step server-side, verifies the resolved session's definition grants (`task_step_writes`) and rejects writes for another role. This section grants `record_review_step` to the code reviewer and all four writers to the Orchestrator, whose procedure delegates first and writes records directly only when other avenues are exhausted; section 3.5 grants the remaining seats. A writer in another project is accepted when `resolve_lifecycle_task` grants lineage or operator authority, preserving #24629 cross-project review and landing. Restore is operator-only: the MCP `restore_tasks` tool is removed, agents are blocked from `gobby tasks restore`, and `backup_tasks` writes only the default path, so no agent can restore approvals from a crafted backup. The reviewer who becomes a fixer can write implementation only after the manager's role transition; former claimants/editors still cannot write an approved review.

The earlier atomic failure helper was retired with build removal; the current transition facade only forwards the review counter. Implement the review-record transaction under the task-row lock: record feedback and review result, increment the current epoch's rejection count once, clear the claim and escalate on the third rejection. Replaying the same reviewer/SHA/round/epoch returns the original record and never increments; contradictory replays fail. Infrastructure errors leave the review step unapproved and do not count as rejection. Audited Triage resets begin a new epoch without erasing history. Task-level review fields summarize these records for readers and are not independent approval inputs. Landing consumes an approved review record for the exact SHA the current review approves; any session holding its own such record may land, replay or recover it. The independent-review receipt grants nothing, and its kind is retired.

When the project enables step-record enforcement (`enforce_step_records` in `task_close_policy`, on in Gobby's `.gobby/project.json`), close of a work-bearing task category always requires approved implementation and closing records, requires test approval for the categories the project lists for testing (none in Gobby's policy), and requires review approval for the categories the project configures (code and docs in Gobby's policy). Epics need no records; their children carry them. The test record can precede or follow implementation; the DevTeam runbook chooses test-first. Evaluate record requirements alongside the four existing data invariants through the shared preview/final-close evaluator, and enforce them in storage `_close_task_in_txn` under the final task lock. For no-work closure the responsible role records the disposition; absence of a linked commit does not bypass required records. Project policy remains generic for non-code tasks. This section does not implement #23817 routing or the per-epic variant.

**Granularity:** Record storage, dedicated writers and mechanical close checks form one authority boundary. The review counter and landing stamp use that same record, so separate authorities would allow contradictory approvals. Backup round-trip and operator-only restore stay in this section because restore is the only other path that writes those records.

**Acceptance:**
- 2.3.1 - Each dedicated writer can update only its authorized role's record; records preserve statuses provenance timestamps and full feedback. test: `tests/mcp_proxy/tools/tasks/test_step_record_authority.py::test_role_owned_writers`.
- 2.3.2 - Concurrent/replayed review records count once and retain full feedback atomically. test: `tests/tasks/test_step_records.py::test_review_record_is_atomic_and_idempotent`.
- 2.3.3 - Editors and former claimants cannot approve; stale-SHA approval cannot authorize landing. test: `tests/tasks/test_step_records.py::test_independent_exact_sha_review`.
- 2.3.4 - The third rejection escalates and blocks ordinary rounds until an audited reset; infrastructure errors do not increment. test: `tests/tasks/test_step_records.py::test_three_rejections_and_audited_reset`.
- 2.3.5 - Preview and close require exactly the approved records selected by project policy and task category, including non-code tasks and optional test steps. test: `tests/tasks/test_close_invariants.py::test_required_step_records_follow_project_policy`.

**Verification:** Planned concurrent storage tests, MCP role-forgery refusals, project-policy matrix, no-work closure, replay/epoch behavior, snapshot restore and exact-SHA landing tests with isolated sessions/tasks. No transcript parsing supplies approval.

### 2.4 Dev Manager lands approved work and marks its tree merged (depends: 2.3)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/land_commit.py::*` — scope-reason: Dev Manager authority ancestry-preserving merge and worktree marking
- `src/gobby/tasks/close_receipts.py::*` — scope-reason: remove activation and landing-approval/authority-transfer receipt contracts
- `src/gobby/mcp_proxy/tools/tasks/_landing.py::*` — scope-reason: expose the new landing authority and refusals
- `src/gobby/tasks/landing_policy.py::*` — scope-reason: remove restart approval prerequisite while retaining carriers and sync policy
- `tests/tasks/test_land_commit.py::*` — scope-reason: cover manager landing and ancestry/disjoint-update behavior
- `tests/tasks/test_dev_manager_landing.py`

**Research context:** The cited landing evidence separates schema carriers from restart approval. The assessment notes current landing requires the caller's own reviewer receipt and refuses former claimants. Authorize the task's Dev Manager to use the distinct independent reviewer's approved review-step record for the exact task tip. Retire the old receipt as an alternative approval authority. Preserve current ancestry-safe landing and transaction/lock checks, idempotently record the landing, resolve the actual source branch/tree from Git and task binding, refresh its registry association, and call `mark_merged(cleanup_days=0)`. Do not rely on stale lane labels. Disjoint intervening integration changes permit a two-parent merge without per-task retest; overlapping paths or semantic conflicts produce a structured Triage escalation. No creator/delegator receipt transfer or activation wait remains.

**Acceptance:**
- 2.4.1 - Bound Dev Manager can land an independently approved exact SHA; unrelated manager, missing approval and self-review fail. test: `tests/tasks/test_dev_manager_landing.py::test_manager_landing_authority`.
- 2.4.2 - Disjoint integration updates preserve the task tip as an ancestor; overlaps reach Triage and never silently overwrite. test: `tests/tasks/test_dev_manager_landing.py::test_disjoint_and_overlapping_landings`.
- 2.4.3 - Landing retries mark the correct task worktree merged once, preserve carrier/project-sync checks and need no activation receipt. test: `tests/tasks/test_dev_manager_landing.py::test_landing_marks_bound_worktree`.

**Verification:** Planned temporary-Git-repository landing tests, independent-review refusal tests and carrier regressions.

## P3: One-task teams with enforceable authority
`kind: framing`

### 3.1 Bind and admit a single-use DevTeam (depends: 2.4)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/runbook_seats.py::*` — scope-reason: validate task-bound team invocation and seat reuse within that team
- `src/gobby/agents/team_launch_inputs.py::*` — scope-reason: replace lane input inference with task/team binding
- `src/gobby/mcp_proxy/tools/runbook_seat_tools.py::*` — scope-reason: expose task/team preparation and idempotent admission
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: validate durable team identity at every seat spawn
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: bind one task branch and worktree to team creation
- `src/gobby/agents/devteam_lifecycle.py`
- `tests/agents/test_devteam_admission.py`

**Research context:** Existing runbooks are ordinary pipelines, and `reserved_run_id` identifies their invocation. Reuse pipeline execution metadata as the durable team record: task ID, branch, worktree ID, project, team invocation ID, seat runs and lifecycle status. Register the binding before spawning seats; validate it under the existing task spawn serialization. One active team per task; retries repair a partial launch without creating duplicate seats. A replacement must name the same team/task/worktree and seat role; it cannot smuggle in a new task. Reject branch creation or task reassignment inside a bound team tree. Keep workspace/session bindings correct from spawn rather than asking seats to hop trees. Decomposition lives in new `devteam_lifecycle.py` rather than growing the runbook/factory modules.

**Acceptance:**
- 3.1.1 - Duplicate and concurrent launch attempts produce one team and one task-bound branch/tree. test: `tests/agents/test_devteam_admission.py::test_idempotent_team_admission`.
- 3.1.2 - Partial launch recovery and replacement preserve team identity and existing seats. test: `tests/agents/test_devteam_admission.py::test_replacement_stays_in_admitted_team`.
- 3.1.3 - Rebinding a team to another task/tree or creating a replacement branch is refused. test: `tests/agents/test_devteam_admission.py::test_team_binding_cannot_drift`.

**Verification:** Planned isolated pipeline/spawn/worktree tests, including concurrent admissions and provider launch failure.

### 3.2 Pass the claim through the review loop (depends: 3.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_claim_release.py::*` — scope-reason: authorize same-team manager claim handoff
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: compare-and-swap owner transfer within one team
- `src/gobby/agents/devteam_lifecycle.py`
- `src/gobby/agents/devteam_review.py`
- `tests/agents/test_devteam_review_loop.py`

**Research context:** Storage supports expected-owner handoff but the MCP layer currently conflicts unless forced/delegated, and release authority is lane-based. Use the bound Dev Manager and expected owner to transfer the single task claim atomically; never use a global force bypass. Persist the review round and seat transition in team metadata, using 2.3's review-step record as the durable trigger. Sequence exactly: Test Writer → Developer → Sol review; rejection one → original Developer → same Sol review; rejection two → Sol reviewer becomes claimant/fixer → fresh Opus review; rejection three → Triage. Each seat writes its own step through the dedicated tool; the second rejection changes the Sol seat's authority to implementation before it edits. A seat that edits is recorded as an editor even if its role label still says reviewer. Resuming a team resumes this state, never recounts transcript messages.

**Acceptance:**
- 3.2.1 - Same-team expected-owner transfers succeed once and cross-team/stale transfers fail. test: `tests/agents/test_devteam_review_loop.py::test_claim_handoff_is_scoped_and_atomic`.
- 3.2.2 - The three-review sequence uses the approved provider/model/reasoning identities and never lets a fixer review itself. test: `tests/agents/test_devteam_review_loop.py::test_review_escalation_sequence`.
- 3.2.3 - Resume and duplicated verdict delivery do not spawn extra reviewers or lose the claim. test: `tests/agents/test_devteam_review_loop.py::test_review_loop_recovers_idempotently`.

**Verification:** Planned state-transition and spawn-contract tests with fake provider launches; model availability failure goes to Triage without silently changing the chosen review sequence.

### 3.3 Close collapses the team and safely cleans its worktree (depends: 3.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::*` — scope-reason: enqueue durable team teardown after committed close
- `src/gobby/agents/devteam_lifecycle.py`
- `src/gobby/agents/devteam_teardown.py`
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: reconcile closed team teardown and merged worktree cleanup
- `src/gobby/storage/worktrees.py::*` — scope-reason: preserve accurate merged status and safe cleanup candidates
- `src/gobby/storage/worktree_cleanup_queries.py`
- `src/gobby/worktrees/deletion.py::*` — scope-reason: validate actual branch/tree identity and preserve live/dirty/unmerged refusals
- `tests/agents/test_devteam_teardown.py`

**Research context:** The post-mortem shows no close-to-worktree trigger; deletion correctly refuses a live manager standing in its own tree. Close commits first, records teardown due in the durable team metadata, then a daemon-owned reconciler ends all team runs/tabs and invokes existing safe deletion after occupancy clears. The manager writes its summary/lessons before close. Startup and periodic reconciliation recover a crash between close, seat termination and cleanup. Split cleanup candidate queries from `src/gobby/storage/worktrees.py` to new `worktree_cleanup_queries.py` (baseline 852 lines). Preserve Git's merged-ancestor, dirty and live checks; a refusal is a visible owned Triage finding, never forced deletion or silent retention. Closed teams never receive another task.

**Acceptance:**
- 3.3.1 - Successful close ends every team seat and tab while preserving task history, commits and lessons. test: `tests/agents/test_devteam_teardown.py::test_close_collapses_all_team_seats`.
- 3.3.2 - Restart at every teardown boundary converges without duplicated termination or lost cleanup. test: `tests/agents/test_devteam_teardown.py::test_teardown_recovers_after_restart`.
- 3.3.3 - Dirty, occupied, mismatched or unmerged trees remain intact with a visible Triage finding; eligible trees/branches are deleted. test: `tests/agents/test_devteam_teardown.py::test_cleanup_preserves_unsafe_trees`.

**Verification:** Planned isolated lifecycle and temporary-worktree integration tests, including close-return versus manager-termination ordering.

### 3.4 Enforce communication roles and Triage digest delivery (depends: 3.3)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: resolve recipient/sender role from durable sessions
- `src/gobby/mcp_proxy/tools/agent_messaging.py::*` — scope-reason: enforce resolved role policy at the messaging boundary
- `src/gobby/agents/devteam_communications.py`
- `src/gobby/install/shared/workflows/rules/worker-safety/runbook-message-authority.yaml`
- `tests/agents/test_runbook_message_authority.py`

**Research context:** Current `send_message_targets` controls selector modes, not recipient roles. Resolve the actual sender and recipient agent definitions through session/run identity; no hardcoded current session IDs or stale project ref variables. Assistant and Triage can address Orchestrator; all team seats can address their Dev Manager and Triage, and normal task-required peer coordination remains available. Triage owns comms synthesis and conflict/escalation routing; it never gates successful close. Default digest cadence is five minutes, queued with `wake=false`, including urgent findings in the next digest unless the operator explicitly requests immediate wake. Restart/relaunch follows the current role binding. Enforce direct selectors and resolved fanout recipients so alternate target modes cannot bypass the restriction.

**Acceptance:**
- 3.4.1 - Only Assistant/Triage reach Orchestrator through direct and fanout selectors; all seats can report to Triage. test: `tests/agents/test_runbook_message_authority.py::test_resolved_role_routing`.
- 3.4.2 - Relaunched role sessions replace stale refs and digest delivery remains queued at the configured cadence. test: `tests/agents/test_runbook_message_authority.py::test_triage_digest_is_queued_and_relaunch_safe`.

**Verification:** Planned mailbox/MCP role tests and scheduler-clock tests without real messages or provider wakes.

### 3.5 Ship Admin and DevTeam definitions and remove retired YAML (depends: 3.4, 4.2, 5.1, #23855, #23859, #23860)
`kind: deliverable`
`[category: config]`

Targets:
- `src/gobby/install/shared/workflows/pipelines/team-coordination.yaml::*` — operation: delete — scope-reason: replace the complete old coordination runbook with Admin
- `src/gobby/install/shared/workflows/pipelines/team-lane.yaml::*` — operation: delete — scope-reason: replace the complete lane runbook with DevTeam
- `src/gobby/install/shared/workflows/pipelines/admin.yaml`
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml`
- `src/gobby/install/shared/workflows/agents/lane-manager.yaml::*` — operation: delete — scope-reason: rename and rewrite the complete definition as dev-manager
- `src/gobby/install/shared/workflows/agents/dev-manager.yaml`
- `src/gobby/install/shared/workflows/agents/inbox-manager.yaml::*` — operation: delete — scope-reason: rename and rewrite the complete definition as triage
- `src/gobby/install/shared/workflows/agents/triage.yaml`
- `src/gobby/install/shared/workflows/agents/test-writer.yaml`
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: replace lane and close relay instructions throughout the definition
- `src/gobby/install/shared/workflows/agents/code-reviewer.yaml::*` — scope-reason: enforce the independent review and security contract throughout the definition
- `src/gobby/install/shared/workflows/agents/release-manager.yaml::*` — scope-reason: replace old release grants with the complete batch release role
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml::*` — scope-reason: replace lane staffing and direct seat messaging instructions
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — scope-reason: retain the runbook role and replace its retired automatic-close tool instructions
- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml::*` — scope-reason: repurpose the retained agent for runbook-spawned tree and dependency checks
- `src/gobby/install/shared/rules/build/build-agent-safety.yaml::*` — operation: delete — scope-reason: retire build-only safety rules after build removal
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/lane-manager.md::*` — operation: delete — scope-reason: rename and rewrite the complete procedure as dev-manager.md
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/dev-manager.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/triage.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/task-close-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/runbooks.md`
- `src/gobby/install/shared/AGENTS.md`
- `AGENTS.md`
- `docs/guides/runbooks.md`
- `tests/workflows/test_devteam_runbooks.py`

**Research context:** Existing pipelines explicitly launch lane/coordination seats; the current model has no Test Writer and uses lane-based release/close procedures. Replace those launch contracts with task-bound `devteam` and five-seat `admin`. Rename managers directly, rewriting identity, authority and procedures at the destination; then retire the old registry names and source files. Retain log-monitor, metrics-monitor, recovery-steward, task-close-reviewer and expansion-qa under Josh's overriding ruling. Expansion QA checks the expanded tree against its plan and fixes dependency or consistency errors when spawned by the Orchestrator or planning team. Researcher, security-reviewer, consolidator, triage-agent and current planning definitions remain usable. Fold security review checks from the standalone security-reviewer into Code Reviewer without deleting that standalone role. Skill and rule catalogs/reference audits must match the resulting files. Keep generic templates portable; apply Gobby-specific integration branch/model defaults and live role routing through project policy. Remove lane limits, grants, quiet-worker requirements and old receipt relay text. Retire the build-specific `build-agent-block-full-pytest` rule and its obsolete template while preserving the general no-full-pytest policy.

The definitions grant only their role's step writer: Test Writer gets `record_test_step`, Developer gets `record_implementation_step`, Code Reviewer gets `record_review_step`, and Dev Manager gets `record_closing_step`. The Orchestrator keeps the four writers section 2.3 granted, for repair only after other avenues are exhausted. A manager-authorized reviewer-to-fixer transition grants implementation authority for that task and removes review approval authority. Procedures require approved records before landing/close; the reviewer record supplies the exact-SHA stamp and the manager's closing record confirms the summary and final checks.

**Granularity:** These configuration files jointly define one usable launch contract. Runtime mechanisms land first; definition and registry activation occur as one coordinated cutover so no partially renamed seat can launch.

**Acceptance:**
- 3.5.1 - Admin launches exactly Assistant/Orchestrator/Archivist/Release Manager/Triage; DevTeam launches the four approved roles in one prepared task tree. test: `tests/workflows/test_devteam_runbooks.py::test_runbook_seat_catalogues`.
- 3.5.2 - Old manager source/installed identities are absent; log-monitor, metrics-monitor, recovery-steward, task-close-reviewer, expansion-qa, the three standalone roles, triage-agent and planning roles remain callable. Expansion QA is runbook-spawnable for post-expansion checks. test: `tests/workflows/test_devteam_runbooks.py::test_agent_retirement_and_retention`.
- 3.5.3 - Procedures enforce review/security, claim handoff, summary-before-close, teardown and the release-window contract without lane/activation/TDD-proof relays. test: `tests/workflows/test_devteam_runbooks.py::test_procedures_match_runtime_authority`.

**Verification:** Planned template parsing, registry sync and runbook dry-run tests; inspect installed DB rows after activation before declaring roles/rules live. Preserve existing sessions until the pilot-controlled switchover.

## P4: Release independently of task completion
`kind: framing`

### 4.1 One expiring release lease blocks landings and new teams (depends: 3.4)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/landing_policy.py::*` — scope-reason: replace release hold handling with owner/token/expiry checks
- `src/gobby/tasks/land_commit.py::*` — scope-reason: enforce release lease under the landing critical section
- `src/gobby/agents/devteam_lifecycle.py`
- `src/gobby/releases/window.py`
- `src/gobby/mcp_proxy/tools/release_tools.py`
- `src/gobby/mcp_proxy/tools/agents_registry.py::*` — scope-reason: register the release-window tools alongside runbook coordination tools
- `tests/releases/test_release_window.py`

**Research context:** Existing landing policy has a machine-local Git-common-directory freeze file. Extend that repository coordination surface with an owned release lease, atomic file replacement and the same cross-process lock used for landing/admission; do not add a second scheduler or task state machine. Lease identity contains owner session, random fencing token, pinned candidate, start/expiry and generation. Acquisition atomically closes both admissions, then pins integration HEAD. Ten-minute expiry and at-most-sixty-second renewal are checked against the recorded generation. Missing/expired lease permits progress; malformed coordination state fails visibly and goes to Triage. Replacement/resume within an existing admitted team checks its stored identity and is allowed. All mutation paths use the same check, including direct spawn attempts that would admit a new team.

**Acceptance:**
- 4.1.1 - Acquisition races with landing/new-team admission have one serialized outcome, with no candidate drift. test: `tests/releases/test_release_window.py::test_atomic_window_admission`.
- 4.1.2 - Existing teams and their replacement/recovery seats keep working while new teams and landings wait. test: `tests/releases/test_release_window.py::test_existing_team_work_continues`.
- 4.1.3 - Expiry releases both holds; stale owners cannot renew, clear or activate a successor's window. test: `tests/releases/test_release_window.py::test_expiry_and_fencing`.

**Verification:** Planned multi-process lock/lease tests with controlled clocks; no live daemon hold during tests.

### 4.2 Validate, promote and smoke a pinned release batch (depends: 4.1, 5.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/releases/batch.py`
- `src/gobby/releases/smoke.py`
- `src/gobby/mcp_proxy/tools/release_tools.py`
- `src/gobby/cli/cutover.py::*` — scope-reason: require the release batch token for agent-driven promotion/restart
- `src/gobby/cli/daemon_lifecycle.py::*` — scope-reason: route agent restarts through the authorized release batch
- `src/gobby/cli/daemon_start.py::*` — scope-reason: refuse out-of-batch agent activation through stop/start
- `src/gobby/mcp_proxy/daemon_control.py::*` — scope-reason: enforce the same release authority for MCP restart entry points
- `src/gobby/servers/routes/admin/_lifecycle.py::*` — scope-reason: enforce the same release authority for admin restart entry points
- `src/gobby/install/bin_set_coherence.py::*` — scope-reason: bind activation preflight to the pinned batch source identity
- `src/gobby/install/shared/workflows/rules/worker-safety/release-manager-authority.yaml`
- `tests/releases/test_release_batch.py`

**Research context:** Current code already provides schema preflight, coherent new-inode promotion and source/version guards. Reuse those primitives. Record each batch in an existing pipeline execution with base SHA, pinned candidate, changed-path-derived check plan, results, token and final status. Compute the affected package closure and run its tests plus applicable lint/type/frontend/native build checks; this is not a full-suite policy. The last successful baseline advances only after promotion, restart, fixed smokes and provider reconnect checks all succeed. Smokes cover daemon health, expected schema, MCP discovery/task reads, session hook delivery and native provider reconnect. Recheck lease ownership immediately before each irreversible activation step. Agent-driven restarts outside the batch are refused, including Python-only activation from the main checkout. Operator recovery commands retain an explicit audited recovery path. On failure, record diagnostics, release both holds and create one idempotent regression task; admit its new team after the hold clears. Do not reopen successfully closed source tasks or automatically roll back schema.

**Acceptance:**
- 4.2.1 - A successful batch validates a pinned SHA, preserves coherent promotion guards and advances the release baseline only after smokes. test: `tests/releases/test_release_batch.py::test_successful_pinned_batch`.
- 4.2.2 - Check/build/restart/smoke/lease-loss failures retain the prior baseline, clear only the owned hold and create one regression task. test: `tests/releases/test_release_batch.py::test_failed_batch_preserves_baseline`.
- 4.2.3 - Agent restart bypasses and stale-token activation are refused, while existing teams reconnect without draining. test: `tests/releases/test_release_batch.py::test_release_authority_and_worker_reconnect`.

**Verification:** Planned isolated daemon/provider adapter tests and fake promotion failures, followed by the controlled pilot in 6.1. Global BEFORE/DAEMON BACK notices remain mandatory for live activation.

## P5: Shared project build cache
`kind: framing`

### 5.1 Reuse Cargo artifacts across task worktrees (depends: 3.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/cargo_target.py::*` — scope-reason: use one project-ID-scoped Cargo target across checkouts
- `src/gobby/agents/isolation_worktree.py::*` — scope-reason: seed consistent target and Cargo home for task trees
- `src/gobby/agents/constants.py::*` — scope-reason: align agent Cargo-home resolution with the approved shared project build policy
- `src/gobby/agents/sandbox_run_environment.py::*` — scope-reason: use the same Cargo home in sandbox environment construction
- `src/gobby/agents/cargo_environment.py`
- `scripts/gobby-project-shell.sh`
- `tests/agents/test_cargo_target.py::*` — scope-reason: replace per-checkout cache expectations
- `tests/integration/test_cargo_target_isolation.py::*` — scope-reason: test divergent checkout reuse and cleanup safety
- `tests/integration/test_project_cargo_cache.py`

**Research context:** The assessment identifies per-checkout target directories in `cargo_target.py` and reports hundreds of gigabytes of duplicated caches. Use the project UUID as the stable parent namespace and sanitized `cargo-target-<project_name>` below it, shared by worktrees, agent subprocesses, sandbox binds, Release Manager and the supported project-shell wrapper. Resolve project identity before constructing the path; same names in different projects must not collide. Centralize environment construction in `cargo_environment.py`, including one Cargo home. Update every import/literal consumer of target and cleanup helpers, and keep emitted artifact provenance tied to the exact source SHA/version despite shared storage. Worktree cleanup removes only its symlink/registration. Old checkout caches are not destructively bulk-deleted by this change. Cargo's build-directory lock supplies serialization; do not add heavy grants or nextest resource caps.

**Acceptance:**
- 5.1.1 - All supported launch paths resolve the same target/Cargo home for a project and different projects remain isolated. test: `tests/integration/test_project_cargo_cache.py::test_project_cache_environment_parity`.
- 5.1.2 - Concurrent divergent branches build the correct source artifacts and preserve release source identity. test: `tests/integration/test_project_cargo_cache.py::test_divergent_checkout_artifact_provenance`.
- 5.1.3 - Task cleanup cannot delete the shared cache or another worktree's artifacts. test: `tests/integration/test_project_cargo_cache.py::test_worktree_cleanup_preserves_project_cache`.

**Verification:** Planned focused Python environment tests and a small isolated two-checkout Cargo fixture; no production-wide cache deletion or unmeasured host tuning. #23902 retains wider performance ownership.

## P6: Activate and prove the complete workflow
`kind: framing`

### 6.1 Pilot, reconcile live policy, and retire old runbooks (depends: 3.5, 4.2, 5.1, #23860, #23861)
`kind: deliverable`
`[category: test]`

Targets:
- `tests/e2e/test_devteam_lifecycle.py`
- `tests/e2e/test_runbook_release_cohort.py`
- `docs/guides/runbooks.md`
- `.gobby/plans/research/runbook-redesign-pilot-results.md`

**Research context:** The post-mortem's manual relay failures require end-to-end proof beyond unit gates. First run one complete task through test commit, developer, reviewer, manager checks, independent landing, close, all-seat collapse and safe cleanup. Then run a small concurrent cohort with one release success and one injected failure: existing teams progress; a new team and landing wait; lease release/expiry restores admission; failure creates a new regression task without reopening source tasks. Use isolated automation for faults, then one operator-visible live cohort under Release Manager authority. Before live switch, inspect DB registry definitions/rules and project policy, record old/new hashes and retire old identities via normal sync. Supersede the conflicting per-lane worktree memory with the one-task decision, reconcile #23817 and gate-only backlog ownership, and preserve unrelated active work. Record findings and exact checks in the pilot report. Any failed safety/lifecycle case is fixed before declaring rollout complete.

**Acceptance:**
- 6.1.1 - The single-task lifecycle preserves review independence and ends with closed task, ended seats and safely removed merged tree/branch. test: `tests/e2e/test_devteam_lifecycle.py::test_complete_single_task_team`.
- 6.1.2 - Concurrent release success/failure/expiry preserves existing work and the two admission holds. test: `tests/e2e/test_runbook_release_cohort.py::test_concurrent_release_cohort`.
- 6.1.3 - Installed registry/policy, current guidance and superseded memories/task ownership match the decisions; retained standalone roles still launch. behavior: "live rollout reconciliation and recorded evidence" in `.gobby/plans/research/runbook-redesign-pilot-results.md`.

**Verification:** Planned focused isolated e2e runs and a bounded live pilot with global release notices. No full pytest suite, push, destructive Git override, or discard of unique work.

## V1 Execution status and verification
`kind: verification`

October 22 reconciliation: #23341, #24799 and #24317 are closed. Sections 1.1–1.4 are delivered and schema 473 is active. Orchestrator #17281 authorized #24797 as the sole claim for sections 2.1 onward and one independent landing review for that completed work. The plan remains deliberately unexpanded by operator choice. The retained-agent ruling supersedes Decision 12 and section 3.5's former deletion list. The per-epic DevTeam variant #24796 remains a later addendum. TDD enforcement stays off through this cohort's landing; focused behavior tests and independent review remain required. The historical execution entry below describes the earlier state, not current completion.

Section 2.1 checkpoint, October 22: migration 474, the review-prefixed storage/API/UI contract, backup epoch validation, historical archive adapter, and planned module extractions are implemented in 901ba9788c. Isolated upgrade/catalog and 102 native grant/schema tests passed; the daemon identity contract passed separately. The changed Python tests produced 601 passes and two stale expectations, both corrected and covered by an 87-test rerun. The post-extraction storage run passed 154 tests; task-detail UI passed 16 tests. Full source mypy, TypeScript, ESLint, scoped Ruff/format, Rust format/Clippy, changed-test quality/type ratchets and the Python suppression ratchet passed. The close-error child count defect found by this run is fixed in the shared exception formatting. Independent checkpoint review covered all 64 files with no outstanding findings. Section 2.2 is in progress under the October 22 seven-state ruling. Later sections and live activation remain pending; this is not the final #24797 landing candidate.

Observed execution on October 21: Josh waived test-first write ordering for #23850, relayed by Assistant #15976 and applied by Orchestrator #17281. The session projection refreshed and production writes succeeded. Implementation and focused verification are underway in the existing task worktree. Passing checks include maintenance, cron scheduling, messaging, stage transitions and terminal cleanup; final validation, commit, review and landing remain pending. The earlier worktree-root authorization defect remains tracked as #24779. This narrative is still a draft without M1 or expansion; it does not claim that the broader redesign has shipped. The Orchestrator owns installs, restarts and pushes.

Project-aware base validation passed on October 21: `uv run gobby plans validate .gobby/plans/runbook-devteam-redesign.md -p /Users/josh/.gobby/worktrees/gobby/task-23850` exited zero and reported all six phases. The narrative contains seventeen deliverables. Rerun base validation after every accepted revision. Expansion validation remains unrun until a server-derived manifest exists. Execution completion requires the existing build-removal leaves, all deliverables above, focused verification, coherent live activation, and the recorded pilot. This file is the sole narrative authority; later displays and handoffs link to it.
