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
9. Release Manager runs a roughly two-hour batch from the last successful release SHA to a pinned integration SHA. Its owned lease expires after ten minutes and renews at least every sixty seconds. During the window, landings and NEW DevTeam admissions wait. Existing teams continue work, testing, review and recovery; replacement seats within an admitted team remain allowed. No worker drain, quiet-fleet requirement, parking, heavy grants or new resource caps. The runbooks retire the heavy-run slot grant and cap, and no seat guidance or runbook asks for a slot (Josh via the Orchestrator, October 22). The one retained limit is a per-process Cargo job count of 4, which 5.1's launch environment sets centrally instead of per-seat instructions.
10. A release validates affected packages and relevant lint/type/build checks, then promotes coherent binaries, restarts and runs fixed smokes. Global announcements, schema preflight, version/source identity checks and new-inode promotion remain. A lost lease invalidates activation; a stale owner cannot renew or clear a successor's lease. Both holds lift on release or expiry. Failure leaves the successful baseline unchanged and creates a regression task for a new DevTeam after the window; source tasks remain closed.
11. Cargo uses one project-ID-scoped target named `cargo-target-<project_name>` and consistent `CARGO_HOME` across supported agent, sandbox, release and project-shell entry points. Deleting a task worktree never deletes the shared cache. Do not edit global shell dotfiles. Wider host-performance research remains owned by #23902.
12. Rename/rewrite `lane-manager.yaml` to `dev-manager.yaml` and `inbox-manager.yaml` to `triage.yaml`; add `test-writer.yaml`; rewrite `release-manager.yaml`. Josh's October 21 ruling supersedes the original deletion list: any agent used in runbooks stays. Keep `log-monitor.yaml`, `metrics-monitor.yaml`, `recovery-steward.yaml`, `task-close-reviewer.yaml`, and `expansion-qa.yaml`. Repurpose expansion QA for runbook-spawned post-expansion tree, dependency and plan-consistency checks. Keep `researcher.yaml`, `security-reviewer.yaml`, and `consolidator.yaml` for standalone use. Keep GitHub `triage-agent` distinct from fleet Triage and preserve current planning roles. Old manager names receive no compatibility aliases; any other runbook-used definition proposed for deletion goes to the Orchestrator first.
13. One complete pilot, then a small concurrent cohort, proves the lifecycle and release success/failure cases before old runbooks retire. No push is authorized by this plan. Operator-owned uncommitted work remains untouched.
14. The October 22 Orchestrator ruling reconciles Decisions 3–6 with Josh's generic close design (memory 3cb34179): test, implementation, review and closing are role-owned step records with pending/in_progress/approved/rejected status. Dedicated writer tools enforce role ownership. Project configuration and task type determine required records, and close checks their approval mechanically. Section 2.3 verdict recording writes the review step; the Dev Manager writes closing. Retain rejection history and independent exact-SHA approval without a second verdict authority. The per-epic DevTeam addendum remains outside #24797.

## Existing work and dependency integration
`kind: framing`

The existing `.gobby/plans/remove-gobby-build.md` and epic #23341 remain authoritative for their existing leaves. Do not duplicate or re-expand them under this plan. Finish #23850 (external dispatch drivers/callers), #23851 (build/dispatch packages), #23852–#23856 (stage-only agents and tools), #23857/#23858/#24062 (consumers and storage), #23859/#23860 (guidance) and #23861 (schema drop) under their current ownership. Preserve already-landed P1 and sections 2.1–2.3.

The prerequisite epic #23341 and all of #24317 closed on October 22 at `2fcc25043ec9c6cd3e8fb3ea3fc5bd4a76e23c0a`. Its absorbed #24799 delivered sections 1.1–1.4. The build drop uses migration 472, and migration 473 retires its stored configuration. Sections 2.1 onward execute under #24797 alone, without expansion or per-leaf claims, as Josh explicitly directed. Preserve task-close-reviewer and all other protected definitions under the later operator ruling. Numeric allocation remains mechanical: verify all refs and the live schema immediately before writing. #24812 took migration 474 on October 22 and #24816 took 475 on October 23, so this plan's migrations are 476 (review state) and 477 (task states).

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
- `crates/gcore/assets/schema/migrations/476_runbook_review_state.sql`
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

**Research context:** The field census in Constraints includes all runtime groups after stage removal. Preserve old results as read-only history before resetting open tasks to pending/count zero in a new review epoch. Closed tasks keep historical outcome attribution. Rename the three fields atomically with every live reader/writer; no dual-read aliases. Migration 476 follows the completed 472 build drop, 473 retired-config cleanup, and the 474 and 475 migrations of #24812 and #24816; recheck allocation before writing. Split `src/gobby/sync/tasks.py` by moving review payload encoding to `task_review_serialization.py`. Split `src/gobby/storage/tasks/_manager.py` by moving its update-method facade into new `src/gobby/storage/tasks/_update_facade.py`, keeping mutations in the existing update implementation. Include schema fixtures and every recursively affected shipped binary's patch bump; recheck dependencies rather than blindly bumping unrelated binaries.

**Granularity:** The cross-language rename exceeds six production files but is one atomic data contract. A split deployment would require the compatibility layer Josh rejected. Migration, adapters and UI consumers therefore ship together.

**Acceptance:**
- 2.1.1 - Migration preserves historical results and initializes the new epoch for open tasks without losing feedback history. test: `crates/gcore/src/schema/runner_tests.rs::review_epoch_migration_preserves_history_and_resets_open_tasks`.
- 2.1.2 - Storage, sync, CLI/MCP and web expose only review-prefixed fields with pending/valid/invalid/error values. test: `tests/storage/test_review_field_migration.py::test_review_contract_has_no_legacy_fields`.
- 2.1.3 - Baseline, catalog, native schema identity, Python expected identity and version pins agree. file: `src/gobby/storage/schema_expected_identity.json`.

**Verification:** Planned isolated migration/contract tests, focused task detail tests, mypy/TypeScript checks and affected Rust contract tests. Deployment uses coordinated coherent promotion.

**Implementation reconciliation:** Baseline 420 remains immutable; fresh databases apply baseline plus migration 476, while existing databases apply the same migration. The native upgrade test seeds the schema just before migration 476 and verifies preserved review rows, task-comment snapshots, new epochs and runtime read-only privileges. CLI/MCP reset arguments were already retired with the old automatic reviewer, so those adapters need no rename. Ordinary reopen preserves review failures; section 2.3 supplies the authorized audited reset. `gcode_versions.py` already derives its floor from the managed version pin. The update facade and backup codec remain in their existing owning packages.

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
- `crates/gcore/assets/schema/migrations/477_runbook_task_states.sql`
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

**Research context:** After the build removal, `projected_task_state` and migration 472's SQL function return ready/closed/escalated; the SQL update trigger omits ownership. Replace ready with open, project claimed tasks as in_progress, and refresh the mirror on claim/release. Reuse the existing external-blocker predicate, which excludes a task's own descendants as work blockers. Add only the intrinsic hold data needed for needs_human/deferred and a deferred follow-up date, with storage checks; schedule no wake or automatic release. Preserve the existing awaiting-human-review label as an input to needs_human. SQL stores the six intrinsic states; dependency blockers are projected on read with Decision 7 precedence. Migration 477 re-buckets rows and normalizes legacy close reasons while archiving their original text. Historical baseline 420 stays immutable. Reuse the shared state serializer across HTTP, MCP, CLI, web and native clients; eliminate ready/needs_review/review_approved/done as task states while keeping step progress separate. The refreshed query module is 538 lines, so no decomposition is needed. Extend existing read-only badges and filters; new hold/defer controls remain #23817.

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
- `tests/tasks/test_dev_manager_landing.py::*` — scope-reason: Dev Manager landing authority, ancestry and worktree marking
- `src/gobby/tasks/lifecycle_access.py::*` — scope-reason: resolve the landing caller's task authority
- `src/gobby/tasks/step_records.py::*` — scope-reason: independent review approval read by landing
- `src/gobby/mcp_proxy/tools/task_commits.py::*` — scope-reason: delete the record_close_receipt tool
- `src/gobby/mcp_proxy/tools/tasks/_session.py::*` — scope-reason: delete the transfer_task_authority tool
- `src/gobby/storage/tasks/_delegation.py::*` — scope-reason: delete authority transfer storage
- `src/gobby/storage/tasks/_manager.py::*` — scope-reason: delete the authority transfer method
- `src/gobby/workflows/enforcement/blocking.py::*` — scope-reason: drop retired tools from task mutation gating
- `src/gobby/servers/routes/tasks_comment_routes.py::*` — scope-reason: name land_commit as the only receipt writer
- `src/gobby/workflows/engine/seat_transitions.py::*` — scope-reason: reviewer unit ends at its own verdict; no parking or landing events
- `src/gobby/workflows/engine/seat_messages.py::*` — scope-reason: retire park/resume grammar and fields
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: delete the reviewer landing condition
- `src/gobby/workflows/step_binding.py::*` — scope-reason: delete reviewer landing recovery
- `src/gobby/workflows/step_context.py::*` — scope-reason: stop calling the deleted recovery
- `src/gobby/install/shared/workflows/agents/code-reviewer.yaml::*` — scope-reason: verdict-only reviewer
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: wait on the lander's EVENT=LANDED
- `src/gobby/install/shared/workflows/agents/inbox-manager.yaml::*` — scope-reason: lander wording
- `src/gobby/install/shared/workflows/agents/release-manager.yaml::*` — scope-reason: no activation receipt; Orchestrator lands
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/orchestrator.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/lane-manager.md`
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/task-close-reviewer.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/implementation.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/creation.md`
- `docs/guides/tasks.md`
- `docs/contracts/parity-ledger.md`
- `docs/contracts/parity-ledger-status.d/tasks.json::*` — scope-reason: unmap retired tools
- `docs/reference-audit/tasks.json::*` — scope-reason: unmap retired tools
- `tests/fixtures/landing_case.py::*` — scope-reason: shared landing fixtures
- `tests/fixtures/review_approvals.py::*` — scope-reason: shared review approval fixture
- `tests/tasks/test_close_receipts.py::*` — scope-reason: landing-only receipts
- `tests/cli/installers/test_landing_guard_hook.py::*` — scope-reason: the bound Dev Manager lands through the guard
- `tests/mcp_proxy/tools/tasks/test_cross_project_task_refs.py::*` — scope-reason: cross-project landing runs as a granted Orchestrator
- `tests/mcp_proxy/tools/tasks/test_transfer_task_authority.py::*` — operation: delete — scope-reason: deleted with the retired tool
- `tests/mcp_proxy/tools/test_tasks_list_coverage.py::*` — scope-reason: drop the retired tools from coverage
- `tests/servers/routes/test_tasks_routes.py::*` — scope-reason: landing-only receipt fixtures
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: retired tools absent from enforcement lists
- `tests/workflows/test_team_runbooks.py::*` — scope-reason: runbook admission no longer names record_close_receipt
- `tests/agents/test_review_observation_seat_definitions.py::*` — scope-reason: verdict-only reviewer
- `tests/agents/test_coordination_seat_definitions.py::*` — scope-reason: Orchestrator lander, no parked candidates
- `tests/workflows/test_seat_step_loops.py::*` — scope-reason: reviewer loop ends at its verdict
- `tests/workflows/test_step_definition_refresh.py::*` — scope-reason: reloaded reviewer ends on its own verdict

**Research context:** The cited landing evidence separates schema carriers from restart approval. The assessment notes current landing requires the caller's own reviewer receipt and refuses former claimants. Authorize the task's Dev Manager to use the distinct independent reviewer's approved review-step record for the exact task tip. Retire the old receipt as an alternative approval authority. Preserve current ancestry-safe landing and transaction/lock checks, idempotently record the landing, resolve the actual source branch/tree from Git and task binding, refresh its registry association, and call `mark_merged(cleanup_days=0)`. Do not rely on stale lane labels. Disjoint intervening integration changes permit a two-parent merge without per-task retest; overlapping paths or semantic conflicts produce a structured Triage escalation. No creator/delegator receipt transfer or activation wait remains. The Code Reviewer's LAND becomes verdict-only here; the Orchestrator lands each approved LAND with `land_commit` until 3.5 hands landings to Dev Managers.
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
- `src/gobby/agents/team_lifecycle.py::*` — scope-reason: the new module holding the team record, admission binding and seat joins
- `tests/agents/test_team_admission.py::*` — scope-reason: admission, replacement and binding-drift tests

**Research context:** Existing runbooks are ordinary pipelines, and `reserved_run_id` identifies their invocation. Reuse pipeline execution metadata as the durable team record: task ID, branch, worktree ID, project, team invocation ID, seat runs and lifecycle status. The record is the runbook's pipeline execution: its inputs name the task and its admission step output carries the binding, the pattern `runbook_seats._launches_here` already reads. Seat runs are the execution's step invocations, and lifecycle status derives from task state and live seat runs. No migration is needed: `outputs_json` is rewritten on completion and `review_json` belongs to the conductor. Register the binding before spawning seats; validate it under the project landing lock (`MainCheckoutLanding`), which 4.1's release lease also takes, so admission, landing and lease acquisition serialize. One active team per task; retries resume the same execution, whose `reserved_run_id` adoption creates no duplicate seats. A replacement must name the same team/task/worktree and seat role; it cannot smuggle in a new task. Only one active run per task may carry `task_id`, so the manager seat holds the run-level task binding that 2.4's landing authority reads; other seats join through the team record. Reject branch creation or task reassignment inside a bound team tree. Keep workspace/session bindings correct from spawn rather than asking seats to hop trees. Decomposition lives in new `team_lifecycle.py` rather than growing the runbook/factory modules. Section 3's modules, tests and team record are generic; "DevTeam" names only the definitions, the devteam runbook and the procedures (Josh via the Orchestrator, October 22). The `team-lane` lane inference stays beside the task path until 3.5 deletes that runbook. Team creation names both the branch and the worktree directory with the existing spawn default, `task-<seq>-<title-slug>` (for example `task-24815-gclient-scroll-and-type-during-daemon-ou`), never bare `task-<seq>` or a seat or lane name (Josh via the Orchestrator, October 22). Admission builds the spawn config with `task_seq_num` and `task_title` and no `branch_name`. `isolation_models.generate_branch_name` (lines 86-107, slug truncated to 40 characters, unchanged by this section) is then the single source, and `isolation_worktree.py:138` derives the directory from it. Admission accepts no caller-supplied branch or worktree name. Caller-supplied names are how bare `task-<seq>` trees, such as this plan's `task-23850`, were created.

**Acceptance:**
- 3.1.1 - Duplicate and concurrent launch attempts produce one team and one task-bound branch/tree, whose branch and worktree directory basename both equal the derived `task-<seq>-<title-slug>`. test: `tests/agents/test_team_admission.py::test_idempotent_team_admission`.
- 3.1.2 - Partial launch recovery and replacement preserve team identity and existing seats. test: `tests/agents/test_team_admission.py::test_replacement_stays_in_admitted_team`.
- 3.1.3 - Rebinding a team to another task/tree or creating a replacement branch is refused. test: `tests/agents/test_team_admission.py::test_team_binding_cannot_drift`.

**Verification:** Planned isolated pipeline/spawn/worktree tests, including concurrent admissions and provider launch failure.

### 3.2 Pass the claim through the review loop (depends: 3.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_claim_release.py::*` — scope-reason: authorize same-team manager claim handoff
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: compare-and-swap owner transfer within one team
- `src/gobby/agents/team_lifecycle.py::*` — scope-reason: a fresh-reviewer execution joins its team, and the team record merges the fresh-reviewer key instead of being overwritten
- `src/gobby/tasks/step_records.py::*` — scope-reason: grant the Sol fixer implementation step writes at the second rejection, and hold every review record to reviewer independence
- `src/gobby/mcp_proxy/tools/runbook_seat_tools.py::*` — scope-reason: `admit_team` takes the fresh-reviewer request and its exclusion list
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml::*` — scope-reason: new runbook; inputs, admission and seat steps for the review loop, including the fresh-reviewer launch
- `src/gobby/install/shared/skills/gobby/references/agents/spawning.md::*` — scope-reason: document the fresh-reviewer admission
- `tests/agents/test_team_review_loop.py::*` — scope-reason: new file holding the claim hand-off, review sequence and recovery acceptance tests
- `tests/fixtures/team_case.py::*` — scope-reason: `TeamWorld.child` launches a given runbook definition so tests can admit `devteam` executions
- `tests/workflows/test_workflows_sync.py::*` — scope-reason: the bundled pipeline set gains `devteam`
- `src/gobby/install/shared/skills/gobby/references/plan/review.md::*` — scope-reason: found work; the planning runbook's review dialogue waits for each reply with event-driven waits, as `test_removed_wait_tool_guidance` requires

**Research context:** Storage supports expected-owner handoff but the MCP layer currently conflicts unless forced/delegated, and release authority is lane-based. Use the bound Dev Manager and expected owner to transfer the single task claim atomically; never use a global force bypass. Sequence exactly: Test Writer → Developer → Sol review; rejection one → original Developer → same Sol review; rejection two → Sol reviewer becomes claimant/fixer → fresh Opus review; rejection three → Triage. Each seat writes its own step through the dedicated tool; the second rejection changes the Sol seat's authority to implementation before it edits. A seat that edits is recorded as an editor even if its role label still says reviewer. Resuming a team resumes this state, never recounts transcript messages.

The fresh reviewer is a seat that a runbook starts (Orchestrator ruling (a), October 23). A runbook step cannot wait for a review record: `renderer.py:356` `should_run_step` sees only inputs and step outputs, and 3.1's runbooks complete after admission. So no spawn comes from `record_task_step` or a manager tool. At the second rejection the Dev Manager hands the claim to the Sol reviewer with `hand_off_task_claim` (3.2.1), then sends the Orchestrator one line, `EVENT=REJECTION_2 TASK=#<seq> TEAM=<team id> COMMIT=<40-hex candidate> REVIEWERS=<session ref>[,<session ref>...]`, naming every session that recorded a review in the current epoch. The Orchestrator launches `devteam` again for the same team with `task`, `team_id=<TEAM>`, `fresh_reviewer=true`, `exclude_reviewers=<REVIEWERS>`, the team's `workspace`, `report_to=<Dev Manager ref>` and the project policy's `launch_defaults.devteam`. 3.5 copies this line format and launch step into `dev-manager.md` and `orchestrator.md`. When the fresh-reviewer launch fails, including an unavailable model, the Orchestrator escalates the task to Triage with `escalate_task` and never substitutes another identity.

3.2 creates `devteam.yaml`; 3.5 completes its seat catalogue test and retires `team-lane`. Its inputs are `task` (required), `team_id`, `workspace` (required), `report_to` (required), `seats` (default `test-writer,developer,code-reviewer,dev-manager`), `fresh_reviewer` (boolean, default false), `exclude_reviewers`, and `<seat>_provider`, `<seat>_model` and `<seat>_reasoning_effort` for `test_writer`, `developer`, `reviewer`, `fresh_reviewer` and `manager`. Steps run in order: `guard` (`check_runbook_seats` with the four-seat catalogue and `require_report_to`), `admit` (`admit_team` with `task`, `team_id`, `fresh_reviewer` and `exclude_reviewers`), then one `spawn_agent` step per seat. A fresh-reviewer execution runs only `code-reviewer`, which takes the `fresh_reviewer_*` identity instead of `reviewer_*`. Every seat joins the admitted tree with `checkout_mode: none` and `project_path` from the `admit` output; only `dev-manager` carries `task_id`, as 3.1 requires. Seats share one tab titled with the team branch from the `admit` output. A team's first `dev-manager` opens it with `{"tab": {"workspace", "title"}}`, and every other seat, including any seat of a replacement execution, splits it with 3.6's `{"split": {"workspace", "tab_title", "axis": "right", "title"}}`. The template carries no model names. Decision 5's identities (`reviewer`: codex `gpt-6.1-sol` medium; `fresh_reviewer`: claude `claude-opus-5-5` xhigh) live in `launch_defaults.devteam`, which 6.1 adds to the live policy.

`team_lifecycle.admit_team` checks a fresh-reviewer request under the project landing lock and refuses unless all of these hold: `team_id` names the team, the current epoch has exactly two rejected review rounds, `exclude_reviewers` resolves to a superset of the sessions that recorded a review in that epoch, and the task's current claimant is one of those reviewers (the hand-off happened). The admitting execution's `admit` output then gains `fresh_reviewers: {"<epoch>": "<execution id>"}`; `team_lifecycle._record` merges keys instead of overwriting the record. A second execution requesting a fresh reviewer for the same epoch is refused `fresh_reviewer_exists`. A re-run of the recorded execution returns the same team, and its `reserved_run_id` adoption spawns no second reviewer.

`step_records._writer` grants `implementation` to a definition that lacks it only for the fixer: the task's current claimant, which recorded a rejected review in the current epoch, while the epoch has exactly two rejections. The grant derives from records, so `record_task_step` reads them before resolving the writer. `_require_independent_reviewer` applies to every new review record, whatever its status, so neither a fixer nor any other claimant, implementer or editor reviews its own task. Exact replays still return the stored record first.

**Acceptance:**
- 3.2.1 - Same-team expected-owner transfers succeed once and cross-team/stale transfers fail. test: `tests/agents/test_team_review_loop.py::test_claim_handoff_is_scoped_and_atomic`.
- 3.2.2 - The three-review sequence uses the approved provider/model/reasoning identities and never lets a fixer review itself. test: `tests/agents/test_team_review_loop.py::test_review_escalation_sequence`.
- 3.2.3 - Resume and duplicated verdict delivery do not spawn extra reviewers or lose the claim. test: `tests/agents/test_team_review_loop.py::test_review_loop_recovers_idempotently`.

**Verification:** State-transition tests over the real step-record writer and team admission. Spawn-contract tests render `devteam.yaml` and record spawn launches instead of running providers. A failed fresh-reviewer launch, including an unavailable model, is the Orchestrator procedure's escalation to Triage (3.5 tests that procedure text); the runbook has no fallback identity, so the chosen review sequence never changes silently.

### 3.3 Close collapses the team and safely cleans its worktree (depends: 3.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/team_lifecycle.py::*` — scope-reason: the team record reports collapse after close
- `src/gobby/agents/team_teardown.py::*` — scope-reason: new module holding the stateless teardown pass, its grace and the refusal finding
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: reconcile closed team teardown and merged worktree cleanup
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: move the lifecycle reconciliation callback out so it can run the team teardown pass
- `src/gobby/runner_lifecycle_callbacks.py::*` — scope-reason: new module receiving the moved reconciliation callback and its teardown step
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: the callback tests follow the moved function
- `src/gobby/storage/worktrees.py::*` — scope-reason: preserve accurate merged status and safe cleanup candidates
- `src/gobby/storage/worktree_cleanup_queries.py`
- `src/gobby/worktrees/deletion.py::*` — scope-reason: validate actual branch/tree identity and preserve live/dirty/unmerged refusals
- `tests/agents/test_team_teardown.py::*` — scope-reason: new file holding the collapse, restart recovery and unsafe-tree acceptance tests

**Research context:** The post-mortem shows no close-to-worktree trigger; deletion correctly refuses a live manager standing in its own tree. The committed close is the durable due marker: a team's own record (`team.team_id` equals its execution) whose task has `closed_at` and whose admission output has no `teardown.outcome` is due, so no window exists between close and a separate marker, and a task reopened inside the grace stops being due. A daemon-owned reconciler, run from the lifecycle monitor's reconciliation callback (every 30 s, first pass 5 s after start) and skipped until subsystem init completes, waits a grace after `tasks.closed_at` so the closing manager receives its close response, ends every live run of every execution joined to the team (fresh reviewers included) through `terminate_agent_run`, which closes each tab and expires each session, and invokes safe deletion once no seat session is live. Each pass is stateless over the database, so restart is another pass: ended runs leave `list_by_parent`, a missing tree row records `cleaned`, and the finding is found by its per-team label before one is created. Deletion uses a teardown surface that locks without the expiry window, refuses a tree whose checked-out branch differs from the record (`worktree_identity_mismatch`), and passes the team's base branch as `merged_into`. The manager writes its summary/lessons before close. Startup and periodic reconciliation recover a crash between close, seat termination and cleanup. Split cleanup candidate queries from `src/gobby/storage/worktrees.py` to new `worktree_cleanup_queries.py` (baseline 852 lines) as a mixin that `LocalWorktreeManager` inherits, so its callers stay unchanged. Move `_reconcile_agent_lifecycle_state` from `src/gobby/runner_lifecycle_subsystems.py` (baseline 875 lines) to new `src/gobby/runner_lifecycle_callbacks.py` as `reconcile_agent_lifecycle_state`, which runs credential rotation, pending-run reclassification and then the team teardown pass; a teardown failure is logged and never starves the first two, and a runner without an agent runner skips it. Preserve Git's merged-ancestor, dirty and live checks; a refusal is a visible owned Triage finding, never forced deletion or silent retention. The finding is a new top-level task labelled `team-teardown` and `team-<team id>`, escalated so Triage owns it (a closed task cannot escalate); its id and the refusal code are recorded under `teardown` on the team record. Closed teams never receive another task: joins and seat spawns for a team whose task is closed are refused `team_closed`.

**Acceptance:**
- 3.3.1 - Successful close ends every team seat and tab while preserving task history, commits and lessons. test: `tests/agents/test_team_teardown.py::test_close_collapses_all_team_seats`.
- 3.3.2 - Restart at every teardown boundary converges without duplicated termination or lost cleanup. test: `tests/agents/test_team_teardown.py::test_teardown_recovers_after_restart`.
- 3.3.3 - Dirty, occupied, mismatched or unmerged trees remain intact with a visible Triage finding; eligible trees/branches are deleted. test: `tests/agents/test_team_teardown.py::test_cleanup_preserves_unsafe_trees`.

**Verification:** Planned isolated lifecycle and temporary-worktree integration tests, including close-return versus manager-termination ordering.

### 3.4 Enforce communication roles and Triage digest delivery (depends: 3.3)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: resolve recipient/sender role from durable sessions
- `src/gobby/mcp_proxy/tools/agent_messaging.py::*` — scope-reason: enforce resolved role policy at the messaging boundary
- `src/gobby/agents/team_communications.py`
- `src/gobby/install/shared/workflows/rules/worker-safety/runbook-message-authority.yaml`
- `tests/agents/test_runbook_message_authority.py`

**Research context:** Current `send_message_targets` controls selector modes, not recipient roles. Resolve the actual sender and recipient agent definitions through session/run identity; no hardcoded current session IDs or stale project ref variables. Assistant and Triage can address Orchestrator; all team seats can address their Dev Manager and Triage, and normal task-required peer coordination remains available. Triage owns comms synthesis and conflict/escalation routing; it never gates successful close. Default digest cadence is five minutes, queued with `wake=false`, including urgent findings in the next digest unless the operator explicitly requests immediate wake. Restart/relaunch follows the current role binding. Enforce direct selectors and resolved fanout recipients so alternate target modes cannot bypass the restriction.

**Acceptance:**
- 3.4.1 - Only Assistant/Triage reach Orchestrator through direct and fanout selectors; all seats can report to Triage. test: `tests/agents/test_runbook_message_authority.py::test_resolved_role_routing`.
- 3.4.2 - Relaunched role sessions replace stale refs and digest delivery remains queued at the configured cadence. test: `tests/agents/test_runbook_message_authority.py::test_triage_digest_is_queued_and_relaunch_safe`.

**Verification:** Planned mailbox/MCP role tests and scheduler-clock tests without real messages or provider wakes.

### 3.5 Ship Admin and DevTeam definitions and remove retired YAML (depends: 3.4, 3.6, 4.2, 5.1, #23855, #23859, #23860)
`kind: deliverable`
`[category: config]`

Targets:
- `src/gobby/install/shared/workflows/pipelines/team-coordination.yaml::*` — operation: delete — scope-reason: replace the complete old coordination runbook with Admin
- `src/gobby/install/shared/workflows/pipelines/team-lane.yaml::*` — operation: delete — scope-reason: replace the complete lane runbook with DevTeam
- `src/gobby/install/shared/workflows/pipelines/admin.yaml`
- `src/gobby/install/shared/workflows/pipelines/devteam.yaml::*` — scope-reason: 3.2 creates the runbook; complete and test its seat catalogue and retire `team-lane`
- `src/gobby/install/shared/workflows/agents/lane-manager.yaml::*` — operation: delete — scope-reason: rename and rewrite the complete definition as dev-manager
- `src/gobby/install/shared/workflows/agents/dev-manager.yaml`
- `src/gobby/install/shared/workflows/agents/inbox-manager.yaml::*` — operation: delete — scope-reason: rename and rewrite the complete definition as triage
- `src/gobby/install/shared/workflows/agents/triage.yaml`
- `src/gobby/install/shared/workflows/agents/test-writer.yaml`
- `src/gobby/install/shared/workflows/agents/developer.yaml::*` — scope-reason: replace lane, close relay and heavy-run slot instructions throughout the definition
- `src/gobby/install/shared/workflows/agents/code-reviewer.yaml::*` — scope-reason: enforce the independent review and security contract throughout the definition
- `src/gobby/install/shared/workflows/agents/release-manager.yaml::*` — scope-reason: replace old release and heavy-run slot grants with the complete batch release role
- `src/gobby/install/shared/workflows/agents/debugger.yaml::*` — scope-reason: drop the heavy-run slot instruction
- `src/gobby/install/shared/skills/gobby/references/agents/procedures/reporter.md::*` — scope-reason: drop the heavy-slot report section and hold state
- `src/gobby/agents/reporter_sources.py::*` — scope-reason: drop the heavy-slot evidence pattern
- `tests/agents/test_reporter_sources.py::*` — scope-reason: reporter sources carry no heavy-slot pattern
- `tests/agents/test_coordination_seat_definitions.py::*` — scope-reason: coordination definitions carry no heavy-slot text
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml::*` — scope-reason: replace lane staffing and direct seat messaging instructions
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — scope-reason: retain the runbook role and replace its retired automatic-close tool instructions
- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml::*` — scope-reason: repurpose the retained agent for runbook-spawned tree and dependency checks
- `src/gobby/install/shared/rules/build/build-agent-safety.yaml::*` — operation: delete — scope-reason: retire build-only safety rules after build removal
- `src/gobby/install/shared/workflows/rules/roles/inject-seat-common.yaml::*` — scope-reason: seat catalogue names dev-manager, triage and test-writer instead of the retired managers
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml::*` — scope-reason: seat catalogues and the manager spawn exemption keyed on the `team_manager` definition variable
- `src/gobby/install/shared/workflows/rules/roles/seat-no-ask-user-question.yaml::*` — scope-reason: seat catalogue names the renamed seats
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

**Research context:** Existing pipelines explicitly launch lane/coordination seats; the current model has no Test Writer and uses lane-based release/close procedures. Replace those launch contracts with task-bound `devteam` and five-seat `admin`. Rename managers directly, rewriting identity, authority and procedures at the destination; then retire the old registry names and source files. Retain log-monitor, metrics-monitor, recovery-steward, task-close-reviewer and expansion-qa under Josh's overriding ruling. Expansion QA checks the expanded tree against its plan and fixes dependency or consistency errors when spawned by the Orchestrator or planning team. Researcher, security-reviewer, consolidator, triage-agent and current planning definitions remain usable. Fold security review checks from the standalone security-reviewer into Code Reviewer without deleting that standalone role. Skill and rule catalogs/reference audits must match the resulting files. Keep generic templates portable; apply Gobby-specific integration branch/model defaults and live role routing through project policy. Remove lane limits, grants, quiet-worker requirements and old receipt relay text. Retire the build-specific `build-agent-block-full-pytest` rule and its obsolete template while preserving the general no-full-pytest policy. Remove every lane-manager and inbox-manager reference, including the inline seat catalogues in the `rules/roles` rules (found by #24810's writer). The lane-manager seat-spawn exemption becomes a definition variable, `team_manager: true`, which dev-manager.yaml sets (Josh via the Orchestrator, October 22). This section also deletes the `team-lane` lane inference that 3.1 left beside the task path. Heavy-run slots retire with the runbooks (Josh via the Orchestrator, October 22). No definition, procedure or reporter source keeps slot grants, slot requests, the heavy-run cap or per-seat `CARGO_BUILD_JOBS` text; 5.1's launch environment carries the job limit. `dev-manager.yaml` lists 3.6's gclient operations reference in the `_seat_references` variable from 3.6, because the manager places its team's panes.

The definitions grant only their role's step writer: Test Writer gets `record_test_step`, Developer gets `record_implementation_step`, Code Reviewer gets `record_review_step`, and Dev Manager gets `record_closing_step`. The Orchestrator keeps the four writers section 2.3 granted, for repair only after other avenues are exhausted. A manager-authorized reviewer-to-fixer transition grants implementation authority for that task and removes review approval authority. Procedures require approved records before landing/close; the reviewer record supplies the exact-SHA stamp and the manager's closing record confirms the summary and final checks.

**Granularity:** These configuration files jointly define one usable launch contract. Runtime mechanisms land first; definition and registry activation occur as one coordinated cutover so no partially renamed seat can launch.

**Acceptance:**
- 3.5.1 - Admin launches exactly Assistant/Orchestrator/Archivist/Release Manager/Triage; DevTeam launches the four approved roles in one prepared task tree. test: `tests/workflows/test_devteam_runbooks.py::test_runbook_seat_catalogues`.
- 3.5.2 - Old manager source/installed identities are absent; log-monitor, metrics-monitor, recovery-steward, task-close-reviewer, expansion-qa, the three standalone roles, triage-agent and planning roles remain callable. Expansion QA is runbook-spawnable for post-expansion checks. test: `tests/workflows/test_devteam_runbooks.py::test_agent_retirement_and_retention`.
- 3.5.3 - Procedures enforce review/security, claim handoff, summary-before-close, teardown and the release-window contract without lane/activation/TDD-proof relays. test: `tests/workflows/test_devteam_runbooks.py::test_procedures_match_runtime_authority`.
- 3.5.4 - No bundled definition, procedure or reporter source mentions heavy-run slots, the heavy-run cap or per-seat `CARGO_BUILD_JOBS`. test: `tests/workflows/test_devteam_runbooks.py::test_no_heavy_run_slot_guidance`.

**Verification:** Planned template parsing, registry sync and runbook dry-run tests; inspect installed DB rows after activation before declaring roles/rules live. Preserve existing sessions until the pilot-controlled switchover.

### 3.6 Give pane-placing seats gclient guidance and tab-title placement (depends: 3.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/install/shared/skills/gobby/references/agents/gclient.md`
- `src/gobby/install/shared/skills/gobby/catalog.json::*` — scope-reason: list the reference as a topic of the agents area
- `docs/reference-audit/agents.json::*` — scope-reason: audit entry for the new reference
- `src/gobby/install/shared/workflows/rules/skill-discovery/require-seat-procedure.yaml::*` — scope-reason: sibling gate for a definition's required references
- `src/gobby/workflows/safe_evaluator.py::*` — scope-reason: condition helper that names the first unloaded listed reference for the gate and its Jinja reason
- `src/gobby/workflows/assistant_response_matching.py::*` — scope-reason: new module that receives the assistant-response matching moved out of `safe_evaluator.py`
- `tests/workflows/test_safe_evaluator.py::*` — scope-reason: import the moved response patterns from their new module
- `tests/workflows/test_skill_discovery_rules.py::*` — scope-reason: list both seat gates and import the moved response patterns
- `src/gobby/hooks/_normalization_tools.py::*` — scope-reason: found work; a gcode search segment without an explicit bound stays broad, as the segment merge already treats it
- `tests/agents/test_project_agnostic_definitions.py::*` — scope-reason: found work; the lane-manager keeps the author-owned retest proof after the reviewer's LAND became verdict-only
- `tests/agents/test_review_observation_seat_definitions.py::*` — scope-reason: found work; the verdict-only reviewer carries no landing retest protocol
- `src/gobby/install/shared/workflows/rules/context-handoff/require-handoff-pull-before-tool.yaml::*` — scope-reason: redirect opener the block-reason corpus requires
- `src/gobby/install/shared/workflows/rules/AGENTS.md::*` — scope-reason: skill-discovery rule count and gate description
- `tests/framing_corpus.py::*` — scope-reason: classify the new gate and the live rules the corpus missed
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml::*` — scope-reason: require the gclient operations reference
- `src/gobby/install/shared/workflows/agents/assistant.yaml::*` — scope-reason: require the gclient operations reference
- `tests/agents/test_coordination_seat_definitions.py::*` — scope-reason: the Orchestrator and Assistant definitions list the gclient reference
- `src/gobby/terminals/workspace_agent_panes.py::*` — scope-reason: split placement may name its tab by title
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: document the tab-title placement form
- `tests/workflows/test_seat_definitions.py::*` — scope-reason: required-reference gate and grants
- `tests/terminals/test_workspace_agent_panes.py::*` — scope-reason: tab-title placement

**Research context:** No seat has gclient operating guidance. The Orchestrator reverse-engineered spawn placement from the `_factory.py` docstring and guessed a pane ref from another session's terminal ID to place reviewers in an operator tab (Josh via the Orchestrator, October 22). The new reference `gobby:references/agents/gclient.md` covers (it sits in the agents folder because the reference library requires each catalog topic to live in its area's folder):
- workspaces, tabs and panes, and how to read a tab or pane ref;
- the placement forms `{"tab": {"workspace", "title"}}` and `{"split": {"pane", "axis", "columns", "rows", "title"}}`, plus the tab-title form below;
- keeping operator and admin tabs free of agent panes;
- the boundaries of `capture_output` and `send_keys`.

It stays generic (memory 191ab225): no Gobby tab names or layouts. It tells seats to read the project's tab layout from project policy, and 6.1 adds that layout to the live policy.

The reference is required reading through the rule-backed gate that already enforces seat procedures. `require-seat-procedure` blocks every non-bootstrap tool until the definition's `_seat_procedure` is `skill_loaded`. That check runs once per context epoch and re-arms after compaction. A sibling rule in the same file does the same for a definition list variable, `_seat_references`, and names the first unloaded entry in its block reason. `orchestrator.yaml` and `assistant.yaml` list the reference; `dev-manager.yaml` lists it in 3.5. Other pane-placing definitions opt in by adding it to the list.

Placement today resolves only numeric refs or a workspace name (`WorkspaceManager.resolve_reference`, `src/gobby/storage/workspaces.py:371-402`). `AgentPlacement.parse` (`workspace_agent_panes.py:101`) accepts a split only beside a pane ref. The split form gains `workspace` plus `tab_title` in place of `pane`. It splits beside that tab's focused pane, the pattern `team_launch_inputs._manager_lane_placement` already uses, and refuses with `invalid_ref` unless exactly one tab in that workspace has the title. `pane` and `tab_title` are mutually exclusive. A separate listing tool is rejected: one placement call answers the need, with no ref lookup.

`safe_evaluator.py` was already above the 850-line growth threshold, so this deliverable moves the assistant-response matching (its patterns, text extraction, cited-span masking and pattern matching) from `src/gobby/workflows/safe_evaluator.py` into `src/gobby/workflows/assistant_response_matching.py`. The evaluator keeps the per-context text cache and registers the condition helper.

**Acceptance:**
- 3.6.1 - A definition that lists `_seat_references` is blocked until each listed reference loads, and the gate re-arms after compaction. The Orchestrator and Assistant definitions list the gclient reference. test: `tests/workflows/test_seat_definitions.py::test_seat_references_gate_required_reading`.
- 3.6.2 - Split placement by tab title lands beside the named tab's focused pane, and refuses missing, ambiguous and pane-plus-title inputs. test: `tests/terminals/test_workspace_agent_panes.py::test_split_placement_by_tab_title`.

**Verification:** Focused rule, definition and placement tests, plus a reference-audit check. No live pane placement in the user's workspace.

## P4: Release independently of task completion
`kind: framing`

### 4.1 One expiring release lease blocks landings and new teams (depends: 3.4)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/landing_policy.py::*` — scope-reason: replace release hold handling with owner/token/expiry checks
- `src/gobby/tasks/land_commit.py::*` — scope-reason: enforce release lease under the landing critical section
- `src/gobby/agents/team_lifecycle.py::*` — scope-reason: new team admission waits on the release window
- `src/gobby/releases/window.py::*` — scope-reason: the expiring, fenced release lease
- `src/gobby/mcp_proxy/tools/release_tools.py::*` — scope-reason: Release-Manager-only window acquire, renew and clear tools
- `src/gobby/mcp_proxy/tools/agents_registry.py::*` — scope-reason: register the release-window tools alongside runbook coordination tools
- `tests/releases/test_release_window.py::*` — scope-reason: serialized admission, existing-team continuity and fencing tests

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
- `src/gobby/mcp_proxy/tools/release_tools.py::*` — scope-reason: batch validate, promote and smoke tools beside the window tools
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

### 5.1 Share third-party crate builds through sccache
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/agents/cargo_environment.py::*` — scope-reason: replace the parked shared-target stub with the one builder of sccache and Cargo job-limit variables
- `src/gobby/agents/sandbox_run_environment.py::*` — scope-reason: sandboxed runs get the sandbox sccache directory, and a socket under the run's tmp when the launch grants run sockets
- `src/gobby/agents/spawn_cache_policy.py::*` — scope-reason: unsandboxed spawns get the agent sccache directory and its socket
- `src/gobby/agents/constants.py::*` — scope-reason: create and resolve the unsandboxed sccache directory beside its Cargo home
- `src/gobby/agents/srt_runtime.py::*` — scope-reason: decide the run-socket grant once and pass it to the sandboxed environment; import the moved settings renderer
- `src/gobby/agents/srt_settings.py::*` — scope-reason: new module holding the SRT settings renderer moved unchanged from `srt_runtime.py`
- `tests/agents/test_archivist_sandbox.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `tests/agents/test_external_write_grants.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `tests/agents/test_sandbox_policy.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `tests/agents/test_spawn_executor_providers.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `tests/agents/test_srt_filesystem_integration.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `tests/agents/test_srt_runtime.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `.gobby/plans/research/security-boundaries-22103/probe_credential_paths.py::*` — scope-reason: import `render_srt_settings` from `srt_settings`
- `tests/agents/test_cargo_target.py::*` — scope-reason: restore the per-checkout expectations parked for the retired shared-target design
- `tests/integration/test_cargo_target_isolation.py::*` — scope-reason: restore the per-checkout expectations parked for the retired shared-target design
- `tests/integration/test_project_cargo_cache.py::*` — operation: delete — scope-reason: its parked tests encode the retired shared-target design
- `tests/agents/test_cargo_environment.py::*` — scope-reason: new acceptance tests for the shared Cargo environment and the trust split

**Research context:** Josh's ruling (via the Orchestrator, October 22) replaces the shared project target with sccache for third-party crates. Josh installed sccache 0.18.0 with Homebrew, so 5.1 has no install step. Per-checkout targets in `cargo_target.py` stay as they are. The parked tests from checkpoint a46cf62c55 change those expectations and must be restored, not adapted. sccache caches only non-incremental compiles. Cargo's debug profile builds workspace members incrementally (sccache README, "Incrementally compiled crates cannot be cached"), so the cache serves registry crates and workspace crates keep incremental builds.

`cargo_environment.py` builds every variable, and both launch paths apply it:
- `RUSTC_WRAPPER` is the `shutil.which("sccache")` result, never its resolved Cellar path. The node shim folded into #24816 broke when Homebrew replaced a versioned path.
- `SCCACHE_DIR` sets the cache directory.
- `SCCACHE_SERVER_UDS` sets the server socket.
- `CARGO_BUILD_JOBS=4` is the per-process job limit. Cargo's jobserver is per process, and sccache doesn't cache incremental workspace crates. The variable replaces every per-seat `CARGO_BUILD_JOBS=3` instruction.
- When sccache doesn't resolve or the launch has no usable socket, the builder sets the three sccache variables to empty strings and builds proceed uncached. Blank values are required, because every sandboxed launch caller merges `launch.provider_env` over the inherited environment with `dict.update`, so an omitted key would keep an unsandboxed value. Cargo 1.97 builds with no wrapper when `RUSTC_WRAPPER` is empty.

The two launch paths keep the existing trust split between the Cargo homes. Sandboxed runs share `sandbox_agent_cache_dir()/cargo-home`, which "nothing unsandboxed builds from" (`sandbox_run_environment.py:40-62`). Unsandboxed spawns use `~/.gobby/cache/cargo-home` through `SPAWN_CACHE_POLICY` (`spawn_cache_policy.py:33-36`). Sandboxed runs therefore keep their cache at `cargo-home/sccache` inside the sandbox shared cache. The existing `cargo_home` write grant in the SRT runtime's policy covers it, so 5.1 needs no new write grant and leaves the sandbox policy module as it is. A sandboxed run can already rewrite that Cargo home, so the cache stays inside that trust domain. sccache makes its own directory on first use, and nothing prunes the Cargo home. Unsandboxed spawns use `~/.gobby/cache/sccache`, which no sandbox may write. Release builds by unsandboxed seats read only the unsandboxed cache, so no release-build exclusion is added.

Each sandboxed run that may open Unix sockets in its run tmp starts its own server on `<run tmp>/sccache.sock`. Only SRT launches on macOS that request run sockets get that grant (`srt_runtime.py:787-791`; spawn and resume request it at `spawn_executor_providers.py:191` and `resume_executor.py:363`). Provider-native launches return before the grant, and Linux SRT keeps its socket restrictions, so those runs get blank sccache variables. `prepare_sandbox_launch` computes the grant decision once and passes it to `SandboxRunPaths.environment`, so the environment and the grant cannot disagree. A shared TCP server on `127.0.0.1:4226` is rejected for two reasons. A server started inside one seat's sandbox can't write other seats' targets. A server started outside the sandbox would let any sandboxed seat write through it. Unsandboxed spawns share one server on `~/.gobby/cache/sccache.sock`, beside their cache rather than inside it to keep the path short, so no TCP listener exists. macOS caps socket paths at 103 bytes; when the run socket path is longer, the builder blanks the sccache variables for that run.

`src/gobby/agents/srt_runtime.py` is 952 lines, so 5.1 moves `render_srt_settings`, `_SRT_DEFAULT_WRITE_PATHS` and `_effective_deny_write` unchanged into the new `src/gobby/agents/srt_settings.py` before adding the grant decision. The six tests and the #22103 credential-path probe script that import `render_srt_settings` import it from `srt_settings`. #24357 plans the same split; it can drop that step once 5.1 lands.

Settings go in the launch environment rather than the Cargo-home `config.toml`, because sandboxed runs can write their Cargo home and could rewrite a shared config. The heavy-run slot grant and cap retire with the runbooks; 3.5 removes slot guidance from definitions and procedures. The live project policy keeps cap 6 and `CARGO_BUILD_JOBS=3` until #24797 activates, and 6.1 reconciles them.

**Acceptance:**
- 5.1.1 - Sandboxed and unsandboxed launch environments carry the wrapper, their own cache directory, a per-run or shared socket and `CARGO_BUILD_JOBS=4`. They blank the sccache variables when sccache is absent, the socket path is too long, or the launch grants no run socket. test: `tests/agents/test_cargo_environment.py::test_launch_paths_share_one_cargo_environment`.
- 5.1.2 - The sandbox policy grants write access to the sandbox sccache directory through the existing Cargo-home grant and to its run socket, and grants no access to the unsandboxed cache or socket. test: `tests/agents/test_cargo_environment.py::test_sandbox_cache_grants_keep_the_trust_split`.
- 5.1.3 - Per-checkout targets and their cleanup behave as before 5.1 (the restored file passes whole). test: `tests/agents/test_cargo_target.py::test_checkout_dirs_are_distinct`.

**Verification:** Focused Python environment and policy tests, plus a restored cargo-target isolation run. No production cache deletion or unmeasured host tuning. #23902 keeps wider performance ownership.

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

**Research context:** The post-mortem's manual relay failures require end-to-end proof beyond unit gates. First run one complete task through test commit, developer, reviewer, manager checks, independent landing, close, all-seat collapse and safe cleanup. Then run a small concurrent cohort with one release success and one injected failure: existing teams progress; a new team and landing wait; lease release/expiry restores admission; failure creates a new regression task without reopening source tasks. Use isolated automation for faults, then one operator-visible live cohort under Release Manager authority. Before live switch, inspect DB registry definitions/rules and project policy, record old/new hashes and retire old identities via normal sync. The project-policy reconcile happens at activation:
- remove the heavy-run cap, the Release Manager slot grant and the `CARGO_BUILD_JOBS=3` text, which 5.1's launch environment replaces;
- add the project's workspace tab layout, which 3.6's gclient reference tells seats to read. Supersede the conflicting per-lane worktree memory with the one-task decision, reconcile #23817 and gate-only backlog ownership, and preserve unrelated active work. Record findings and exact checks in the pilot report. Any failed safety/lifecycle case is fixed before declaring rollout complete.

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
