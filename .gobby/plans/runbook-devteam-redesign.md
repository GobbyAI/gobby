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
4. Close retains four data invariants: children are closed; a linked commit or explicit no-work disposition exists; no task-attributed edits remain uncommitted; and a work-bearing linked commit is an ancestor of the configured integration branch, `0.5.0` here. Summary, scope, criteria, test evidence, acceptance evidence and review judgment belong to the reviewer and Dev Manager.
5. The first reviewer is Codex `gpt-6.1-sol`, medium reasoning. First rejection returns to the original Developer, then the same reviewer reviews again. On the second rejection that Sol reviewer takes the claim and fixes the task; a fresh Claude `claude-opus-5-5`, xhigh reasoning, reviews that work. Third rejection escalates to Triage. An editor or former claimant can never approve its own work.
6. Verdict recording atomically writes the full feedback, durable verdict and task count. Replaying the same review of the same SHA does not increment the count. Triage can authorize an audited reset; history remains. Rename `validation_status`, `validation_feedback`, and `validation_fail_count` to `review_status`, `review_feedback`, and `review_fail_count` everywhere, without aliases. Keep pending/valid/invalid/error status values. Archive old close-review results; open tasks start the new review epoch at pending/count zero.
7. Task states are open, blocked, in_progress, closed and escalated. The stored bucket has open/in_progress/closed/escalated; blocked is computed from blocking dependencies/holds on read. Precedence is closed, escalated, blocked, claimed/in_progress, open. Claims update the SQL mirror. Review state is separate from task state.
8. Dev Manager lands using another reviewer's exact-SHA approval. Disjoint integration changes may land without rerunning every task's tests; preserve ancestry with a two-parent merge when needed. Overlap/conflict goes to Triage. Semantic keep/discard decisions go to Josh through Assistant. Schema carriers and required project synchronization stay enforced. Restart approval, activation receipts and creator/delegator authority-transfer relays retire.
9. Release Manager runs a roughly two-hour batch from the last successful release SHA to a pinned integration SHA. Its owned lease expires after ten minutes and renews at least every sixty seconds. During the window, landings and NEW DevTeam admissions wait. Existing teams continue work, testing, review and recovery; replacement seats within an admitted team remain allowed. No worker drain, quiet-fleet requirement, parking, heavy grants or new resource caps.
10. A release validates affected packages and relevant lint/type/build checks, then promotes coherent binaries, restarts and runs fixed smokes. Global announcements, schema preflight, version/source identity checks and new-inode promotion remain. A lost lease invalidates activation; a stale owner cannot renew or clear a successor's lease. Both holds lift on release or expiry. Failure leaves the successful baseline unchanged and creates a regression task for a new DevTeam after the window; source tasks remain closed.
11. Cargo uses one project-ID-scoped target named `cargo-target-<project_name>` and consistent `CARGO_HOME` across supported agent, sandbox, release and project-shell entry points. Deleting a task worktree never deletes the shared cache. Do not edit global shell dotfiles. Wider host-performance research remains owned by #23902.
12. Rename/rewrite `lane-manager.yaml` to `dev-manager.yaml` and `inbox-manager.yaml` to `triage.yaml`; add `test-writer.yaml`; rewrite `release-manager.yaml`. Delete `log-monitor.yaml`, `metrics-monitor.yaml`, `recovery-steward.yaml`, and `task-close-reviewer.yaml`. Keep `researcher.yaml`, `security-reviewer.yaml`, and `consolidator.yaml` for standalone use. Keep GitHub `triage-agent` distinct from fleet Triage and preserve current planning roles. Old names receive no compatibility aliases.
13. One complete pilot, then a small concurrent cohort, proves the lifecycle and release success/failure cases before old runbooks retire. No push is authorized by this plan. Operator-owned uncommitted work remains untouched.

## Existing work and dependency integration
`kind: framing`

The existing `.gobby/plans/remove-gobby-build.md` and epic #23341 remain authoritative for their existing leaves. Do not duplicate or re-expand them under this plan. Finish #23850 (external dispatch drivers/callers), #23851 (build/dispatch packages), #23852–#23856 (stage-only agents and tools), #23857/#23858/#24062 (consumers and storage), #23859/#23860 (guidance) and #23861 (schema drop) under their current ownership. Preserve already-landed P1 and sections 2.1–2.3.

Reconcile those task descriptions before execution: remove the old instruction to retain task-close-reviewer from #23855; preserve this plan's retained standalone agents; replace stale migration-number text. Migration 471 is occupied by `471_code_overlay_pins.sql`. The build drop takes the next free number at implementation; this plan's schema migration follows it. Numeric allocation is mechanical, verified against all refs and the live schema immediately before writing; no number is reserved by this document.

To avoid a cycle, #23861 waits on this plan's 1.4, while 1.1–1.4 wait only on the needed code/storage removals, not #23861 or epic #23341. Sections 2.1 onward that need the schema wait on #23861. Reconcile superseded state task #23817 against Decision 7. Verify #23433's snapshot-only worktree indexing delivery before changing its status. Do not adopt #23902 or create competing performance work.

## Constraints and evidence
`kind: framing`

Evidence was checked at integration commit `2dc56d7077b7623bf286b55d15ceabc937ad1056`. `gcode evidence` for `src/gobby/tasks/land_commit.py:430-465` returned excerpt hash `3b9d4fcd73b476676580a691212ed49c4c4e3bc55b379647672ea4eb8fc70a43`: `453| missing_carriers = missing_schema_carriers(paths)` and `462| if activation_class in {"restart", "cutover"}:` / `463| required.add("restart")`. Preserve the former and remove the latter receipt prerequisite. The supplied research provides the remaining baseline code map; refresh only references changed by prerequisite leaves.

Bounded consumer inventories were run with `gcode grep -w validation_fail_count src/ web/ crates/ -m 100`, `gcode grep -w TASK_CLOSE_REVIEWER_AGENT src/ -m 50`, and `gcode outline src/gobby/tasks/state_semantics.py`. Slow worktree index navigation required direct bounded filename/literal inventories. The rename reaches task creation/model/manager/transitions/updates/de-escalation, CLI reset/status, sync serialization, web task types/detail/snapshots and fixtures. Close-review retirement reaches resume, spawn guards/factory, runner reconciliation, MCP request context, agent queries, delegation and receipts. Sections below own those consumer groups.

For every implementation leaf, refresh its literal/import sweep after prerequisites land, including tests, string-based patches, templates, reference audits and generated consumers. Historical migrations and completed research remain immutable. Files at 850 lines require the named decomposition in the same leaf; production files stay below 1,000. Schema/config edits carry generated contracts, schema identity, catalog/tests, integration-tip patch bumps for every shipped dependent, Cargo.lock and managed pins. Use the repository's generator and promotion functions, never hand-copy binaries.

Use isolated tests: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused targets>`. No full pytest suite. Run relevant Ruff, mypy, frontend/Rust checks, test-quality/types and suppression audits while those are required by the active workflow. New flow enforcement is exercised with isolated state before live registry activation. Design edits in task UI load impeccable and the project design contract.

## P1: Remove the close relay
`kind: framing`

### 1.1 Close enforces four invariants (depends: #23858)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::*` — scope-reason: replace the complete close gate evaluation with four invariants
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::*` — scope-reason: remove evidence/reviewer finalization while preserving atomic close effects
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py::*` — scope-reason: preview the same four-invariant result
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py::*` — scope-reason: update close tool contract and output
- `src/gobby/tasks/close_checklist.py::*` — scope-reason: narrow checklist serialization and gate names
- `src/gobby/tasks/close_invariants.py`
- `tests/tasks/test_close_invariants.py`
- `tests/e2e/test_task_close_checklist_e2e.py::*` — scope-reason: replace old checklist expectations at the public boundary

**Research context:** The assessment locates thirteen close checks in `_lifecycle_close.py`; the post-mortem records repeated claimant-window revalidation after landing. Keep authorization, task lookup and valid repository input as ordinary API preconditions, not extra workflow proof gates. Split `_lifecycle_close.py` by moving the four invariant evaluators to new `close_invariants.py`; delete the old gate branches. Preview and close must call the same evaluator. Retain no-work reasons, epic child checks, hook attribution and atomic task finalization. A linked commit for work-bearing completion must resolve and be an ancestor of the project's integration tip. A no-work disposition cannot hide dirty task edits. Existing close callers receive a simple result without asynchronous review jobs.

**Acceptance:**
- 1.1.1 - The close/preview matrix enforces precisely the four approved invariants, including no-work and epic cases. test: `tests/tasks/test_close_invariants.py::test_close_invariant_matrix`.
- 1.1.2 - A landed, clean task closes without validation transcripts, activation, acceptance-artifact proof or spawned review. test: `tests/tasks/test_close_invariants.py::test_close_needs_no_evidence_relay`.
- 1.1.3 - A repeat close is idempotent and failed invariants leave ownership and closure unchanged. test: `tests/tasks/test_close_invariants.py::test_close_is_atomic_and_idempotent`.

**Verification:** Planned focused close unit/MCP/e2e tests plus type/lint checks. No passing result is claimed yet.

### 1.2 Retire transcript and edit-time TDD enforcement (depends: 1.1, #23858)
`kind: deliverable`
`[category: refactor]`

Targets:
- `src/gobby/tasks/tdd_evidence.py::*` — operation: delete — scope-reason: retire transcript RED gate
- `src/gobby/tasks/tdd_python_evidence.py::*` — operation: delete — scope-reason: retire gate-only Python evidence parser
- `src/gobby/tasks/tdd_pytest_provenance.py::*` — operation: delete — scope-reason: retire gate-only provenance parsing
- `src/gobby/tasks/tdd_write_authorization.py::*` — operation: delete — scope-reason: retire edit-time named RED authorization
- `src/gobby/tasks/tdd_retained_output.py::*` — operation: delete — scope-reason: retire TDD-only retained proof
- `src/gobby/tasks/tdd_compacted_output.py::*` — operation: delete — scope-reason: retire TDD-only compaction proof
- `src/gobby/tasks/tdd_stub_revert.py::*` — operation: delete — scope-reason: retire gate-only stub proof
- `src/gobby/workflows/tdd_paths.py::*` — operation: delete — scope-reason: retire edit-time TDD path authorization
- `src/gobby/workflows/condition_helpers.py::*` — scope-reason: remove TDD authorization bindings and keep unrelated evaluator helpers
- `src/gobby/workflows/claimed_task_extra_skills.py::*` — scope-reason: decouple useful test-first skill loading from removed gate imports
- `src/gobby/workflows/condition_helpers_core.py`
- `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-block.yaml::*` — operation: delete — scope-reason: retire the whole edit-time RED barrier rule
- `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-track-tests.yaml::*` — operation: delete — scope-reason: retire the whole gate-only test tracking rule
- `src/gobby/install/shared/skills/test-driven-development/SKILL.md`
- `tests/workflows/test_tdd_gate_retirement.py`

**Research context:** The assessment distinguishes close-time RED from the separate edit-time rules; deleting only gate 12 leaves writes blocked. Remove both callers and their exclusive proof modules. Preserve general transcript search/history and any test-path utilities with non-gate consumers. Move surviving general helpers out of `condition_helpers.py` into new `condition_helpers_core.py` if needed to keep its production size bounded. The TDD skill teaches test-first commits and independent reruns without transcript authentication. Registry sync must retire installed rules as well as template files.

**Granularity:** This is one removal boundary across more than six files. The parser modules cannot be safely removed independently of their shared write/close consumers; 1.1 first detaches close, and this leaf removes the edit barrier and its dead imports together.

**Acceptance:**
- 1.2.1 - Production edits have no retained-RED authorization hook; installed TDD enforcement rows and template references are absent. test: `tests/workflows/test_tdd_gate_retirement.py::test_retired_tdd_rules_are_not_installed`.
- 1.2.2 - Task skill loading and unrelated transcript consumers import and work without removed modules. test: `tests/workflows/test_tdd_gate_retirement.py::test_surviving_consumers_do_not_import_tdd_gates`.
- 1.2.3 - The TDD skill specifies tests-first commits and reviewer verification with no gate-proof requirement. file: `src/gobby/install/shared/skills/test-driven-development/SKILL.md`.

**Verification:** Planned rule-sync/import tests and bounded surviving-consumer tests; remove gate-exclusive tests with their production modules and audit their parent packages.

### 1.3 Retire automatic close review and proof-only runtime (depends: 1.1, 1.2, #23858)
`kind: deliverable`
`[category: refactor]`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_orchestration.py::*` — operation: delete — scope-reason: retire automatic close reviewer lifecycle
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_review_gate.py::*` — operation: delete — scope-reason: retire gate 13
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py::*` — operation: delete — scope-reason: retire close-evidence accounting
- `src/gobby/tasks/agentic_close_review.py::*` — operation: delete — scope-reason: retire reviewer-only runtime
- `src/gobby/tasks/close_review_delivery.py::*` — operation: delete — scope-reason: retire automatic reviewer deliveries
- `src/gobby/tasks/close_verdict.py::*` — operation: delete — scope-reason: retire old close verdict model
- `src/gobby/tasks/validation.py::*` — operation: delete — scope-reason: retire gate-only validation coordinator
- `src/gobby/storage/task_close_reviews.py::*` — operation: delete — scope-reason: replace active review queue with historical reads
- `src/gobby/storage/review_history.py`
- `src/gobby/agents/resume_executor.py::*` — scope-reason: remove close-review special resume handling
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: remove reserved automatic-review launch exceptions
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_guards.py::*` — scope-reason: remove close-review-only guard exceptions
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: remove close-review reconciliation
- `src/gobby/runner_lifecycle_reconcile.py::*` — scope-reason: remove close-review-only recovery
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: remove startup calls to the retired close-review reconciler
- `src/gobby/runner_background_services.py`
- `src/gobby/servers/routes/mcp/endpoints/request_context.py::*` — scope-reason: remove close-review session impersonation exceptions
- `src/gobby/storage/agents/_queries.py::*` — scope-reason: remove close-review-only session query handling
- `src/gobby/storage/tasks/_delegation.py::*` — scope-reason: remove special reviewer delegation authority
- `src/gobby/tasks/close_receipts.py::*` — scope-reason: remove automatic-review identity imports while preserving independent reviews
- `src/gobby/agents/spawn_guard_policy.py`
- `src/gobby/agents/lifecycle_recovery.py`
- `tests/agents/test_close_review_retirement.py`

**Research context:** The complete `TASK_CLOSE_REVIEWER_AGENT` sweep is listed in Constraints. Remove queue launches, recovery, exception authority and terminal subscriptions as one lifecycle retirement. Preserve old result bytes through a read-only `review_history.py` adapter until 2.1 moves their storage to the historical namespace. Never resume old queued reviewers. Delete exclusive proof modules only after their importer sweep is empty; general session transcripts remain. Split surviving spawn-guard policy into `spawn_guard_policy.py` and runner recovery into `lifecycle_recovery.py` where size requires it. Split `runner_lifecycle_subsystems.py` by moving background-service wiring into new `runner_background_services.py`. The definition/procedure deletion is owned by 3.5, after no runtime needs it.

**Granularity:** More than six files share one retired lifecycle and constant. Deleting that lifecycle atomically prevents startup and spawn imports from referring to missing review machinery; ordinary close behavior is independently delivered in 1.1.

**Acceptance:**
- 1.3.1 - Startup, spawn, resume and close launch no automatic task-close reviewer and retain their ordinary agent behavior. test: `tests/agents/test_close_review_retirement.py::test_no_close_reviewer_runtime`.
- 1.3.2 - Historical close-review results remain readable and immutable; pending legacy jobs cannot run. test: `tests/agents/test_close_review_retirement.py::test_old_reviews_are_history_only`.
- 1.3.3 - No live importer or rule references a deleted gate-only module. test: `tests/agents/test_close_review_retirement.py::test_retired_review_imports_are_absent`.

**Verification:** Planned focused startup/spawn/resume/close tests and import sweeps. Section 1.4 completes the evidence retirement before #23861's schema drop.

### 1.4 Remove close-only evidence code without breaking shared consumers (depends: 1.3)
`kind: deliverable`
`[category: refactor]`

Targets:
- `src/gobby/tasks/close_checklist.py::*` — scope-reason: remove validation-command evaluation after retaining the four-invariant checklist
- `src/gobby/tasks/close_integration_credit.py::*` — scope-reason: remove close-credit algorithms while preserving any shared evidence value types
- `src/gobby/tasks/close_pytest_collection.py::*` — scope-reason: retire close-only collection evidence
- `src/gobby/tasks/close_test_coverage.py::*` — operation: delete — scope-reason: retire close-only test coverage proof after its checklist and confinement callers
- `src/gobby/tasks/acceptance_artifacts.py::*` — scope-reason: retire close-only artifact resolution while preserving any plan consumers
- `src/gobby/tasks/acceptance_references.py`
- `src/gobby/tasks/validation_evidence.py::*` — scope-reason: retire close-review diff evidence construction
- `src/gobby/tasks/validation_diagnostics.py::*` — scope-reason: remove close-review proof diagnostics
- `src/gobby/tasks/validation_confinement.py::*` — operation: delete — scope-reason: retire close-only foreign-path proof credit with its coverage dependency
- `src/gobby/tasks/validation_history.py::*` — scope-reason: retain historical reads without active close-validation accounting
- `src/gobby/tasks/criteria_contract.py::*` — scope-reason: remove Live-prefix and close operational proof while preserving useful criteria text validation
- `src/gobby/tasks/criteria_text.py`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::*` — scope-reason: delete obsolete close evidence preparation
- `src/gobby/mcp_proxy/tools/tasks/_close_context.py`
- `src/gobby/tasks/transcript_evidence.py::*` — scope-reason: remove close-specific evidence branches and preserve the found-work stop consumer
- `src/gobby/tasks/transcript_git_sources.py::*` — scope-reason: remove retired close-credit dependency and retain shared source evidence
- `src/gobby/tasks/transcript_evidence_sources.py`
- `src/gobby/workflows/found_work_gate.py::*` — scope-reason: preserve the existing found-work policy while adapting shared evidence imports
- `src/gobby/workflows/validation_cover.py::*` — scope-reason: preserve found-work validation coverage independent of close gates
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: remove automatic close reconciliation and retain only shared evidence pool startup
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: retire close-only shutdown resources without breaking shared transcript use
- `src/gobby/runner_background_services.py`
- `tests/tasks/test_close_evidence_retirement.py`

**Research context:** A direct import sweep found that `src/gobby/workflows/found_work_gate.py` still calls `derive_transcript_evidence` and `validation_cover.py` uses its models. Those are real consumers outside close/TDD, so deleting the entire general parser would break the still-required found-work policy. Delete close-only functions, tests and modules once their imports are removed, while preserving shared parsing, normal transcript search and history. Split reusable criteria text operations from `criteria_contract.py` into `criteria_text.py`; the `Live:` special meaning and `pending_external` close behavior end. Split reusable parsing helpers from `acceptance_artifacts.py` into new `acceptance_references.py`, preserving plan expansion, claimed-skill loading and task-creation callers. Split reusable source helpers from `transcript_evidence.py` into `transcript_evidence_sources.py`. Split background-service startup/shutdown wiring from `runner_lifecycle_subsystems.py` and `runner_lifecycle_shutdown.py` into new `runner_background_services.py`. Split `_close_evaluation_support.py` by moving the repository and candidate context still needed by the four invariants into new `_close_context.py`, then remove the old evidence preparation. Every surviving evidence function must have an identified non-close consumer; dead proof code is not retained as a compatibility layer. Preserve validation detection configuration used by found-work checks.

**Granularity:** This is the cleanup boundary after the independently testable close, TDD and automatic-review removals. Multiple files implement one evidence consumer graph; the leaf must keep shared found-work behavior green throughout removal.

**Acceptance:**
- 1.4.1 - Close uses no validation-command, artifact, operational-evidence or transcript-credit evaluator. test: `tests/tasks/test_close_evidence_retirement.py::test_close_has_no_evidence_dependencies`.
- 1.4.2 - General transcript search/history and found-work validation still work through their retained shared helpers. test: `tests/tasks/test_close_evidence_retirement.py::test_shared_transcript_consumers_survive`.
- 1.4.3 - Live-prefixed criteria have no deferred close semantics, and old results remain historical. test: `tests/tasks/test_close_evidence_retirement.py::test_live_prefix_has_no_close_semantics`.

**Verification:** Planned focused close/found-work/transcript regression tests and a complete import/literal sweep of removed symbols. #23861 waits for this retirement boundary, not for the new review schema.

## P2: Task review and landing authority
`kind: framing`

### 2.1 Rename review fields and archive old results (depends: 1.4, #23861)
`kind: deliverable`
`[category: code]`

Targets:
- `crates/gcore/assets/schema/migrations/473_runbook_review_state.sql`
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the complete schema catalog carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: carry regenerated schema identity
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify schema and review data migration
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: verify the matching schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: replace generated expected schema identity
- `src/gobby/storage/review_history.py`
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: replace review fields in deserialization and both response shapes
- `src/gobby/storage/tasks/_creation.py::*` — scope-reason: initialize renamed fields
- `src/gobby/storage/tasks/_manager.py::*` — scope-reason: rename update contract
- `src/gobby/storage/tasks/_update_facade.py`
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
- `web/src/components/activity/__tests__/fixtures.ts::*` — scope-reason: update task fixtures
- `web/src/components/activity/__tests__/TasksTabDetailPanel.test.tsx::*` — scope-reason: test renamed review fields
- `web/tests/fixtures/webChatRestorePlan.ts::*` — scope-reason: update restored task review fields
- `src/gobby/sync/task_review_serialization.py`
- `tests/storage/test_review_field_migration.py`
- `Cargo.lock`
- `crates/gcore/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: align managed binary pins with schema carrier versions
- `src/gobby/code_index/gcode_versions.py::*` — scope-reason: align gcode capability floors with schema carrier versions

**Research context:** The field census in Constraints includes all runtime groups after stage removal. Preserve old results as read-only history before resetting open tasks to pending/count zero in a new review epoch. Closed tasks keep historical outcome attribution. Rename the three fields atomically with every live reader/writer; no dual-read aliases. Migration 473 is the concrete candidate path following a 472 build drop; allocate the next free number and update this path before writing if concurrent migrations advance it. Split `src/gobby/sync/tasks.py` by moving review payload encoding to `task_review_serialization.py`. Split `src/gobby/storage/tasks/_manager.py` by moving its update-method facade into new `src/gobby/storage/tasks/_update_facade.py`, keeping mutations in the existing update implementation. Include schema fixtures and every recursively affected shipped binary's patch bump; recheck dependencies rather than blindly bumping unrelated binaries.

**Granularity:** The cross-language rename exceeds six production files but is one atomic data contract. A split deployment would require the compatibility layer Josh rejected. Migration, adapters and UI consumers therefore ship together.

**Acceptance:**
- 2.1.1 - Migration preserves historical results and initializes the new epoch for open tasks without losing feedback history. test: `tests/storage/test_review_field_migration.py::test_review_epoch_migration`.
- 2.1.2 - Storage, sync, CLI/MCP and web expose only review-prefixed fields with pending/valid/invalid/error values. test: `tests/storage/test_review_field_migration.py::test_review_contract_has_no_legacy_fields`.
- 2.1.3 - Baseline, catalog, native schema identity, Python expected identity and version pins agree. file: `src/gobby/storage/schema_expected_identity.json`.

**Verification:** Planned isolated migration/contract tests, focused task detail tests, mypy/TypeScript checks and affected Rust contract tests. Deployment uses coordinated coherent promotion.

### 2.2 Five task states agree on every surface (depends: 2.1)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/state_semantics.py::*` — scope-reason: replace stage-derived state with the five-state projection
- `src/gobby/storage/tasks/_queries.py::*` — scope-reason: align list/filter/count state queries
- `src/gobby/storage/tasks/_aggregates.py::*` — scope-reason: align aggregate state counts
- `src/gobby/storage/tasks/_ancestor_gate.py::*` — scope-reason: align parent blockers with the five-state semantics
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: make claim/release transitions agree with state projection
- `src/gobby/storage/tasks/_models.py::*` — scope-reason: unify serialized state
- `src/gobby/mcp_proxy/tools/tasks/_listing.py::*` — scope-reason: expose the same state/filter contract
- `src/gobby/cli/tasks/_utils/rendering.py::*` — scope-reason: render the same five-state vocabulary in CLI task lists
- `src/gobby/servers/routes/tasks.py::*` — scope-reason: match HTTP state serialization
- `web/src/types/tasks.ts::*` — scope-reason: use the five-state union
- `web/src/components/tasks/TaskBadges.tsx::*` — scope-reason: render five task states
- `web/src/components/activity/TasksTabFilters.tsx::*` — scope-reason: filter the five-state vocabulary
- `crates/gcore/assets/schema/migrations/474_runbook_task_states.sql`
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the complete state schema catalog carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: carry state schema identity
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: verify claim-trigger state computation
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: verify matching state schema identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: replace generated expected state schema identity
- `src/gobby/storage/tasks/state_queries.py`
- `tests/tasks/test_five_task_states.py`
- `Cargo.lock`
- `crates/gcore/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/gdaemon/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `crates/gclient/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: align state migration binary pins
- `src/gobby/code_index/gcode_versions.py::*` — scope-reason: align gcode capability floors with state migration versions

**Research context:** `state_semantics.py` currently projects stage state; the assessment identifies SQL `compute_task_state_bucket` and its trigger as a separate authority. Remove stage state as an input. SQL stores four stable states and recomputes on claim, release, escalation and close; query projection computes blocked with precedence from Decision 7. Re-bucket existing rows in the migration. Allocate the next free migration number after 2.1. Split state SQL predicates from `_queries.py` to new `state_queries.py` if its refreshed size reaches the decomposition threshold. Reuse the shared state serializer across HTTP, MCP, CLI, web and native client consumers; eliminate ready/needs_review/review_approved/done as task states while keeping review status separate.

**Granularity:** One state contract spans SQL, Python and clients; all authorities must change together to prevent contradictory task lists. Package-specific tests remain in this leaf.

**Acceptance:**
- 2.2.1 - State precedence, dependency unblock and claim/release transitions match the approved matrix. test: `tests/tasks/test_five_task_states.py::test_state_precedence_and_claim_transitions`.
- 2.2.2 - SQL filtering/counts and public serialization agree, including blocked claimed tasks. test: `tests/tasks/test_five_task_states.py::test_query_and_surface_state_parity`.
- 2.2.3 - The old stage state vocabulary is absent from live task APIs and UI filters. test: `tests/tasks/test_five_task_states.py::test_removed_task_states_are_rejected`.

**Verification:** Planned isolated trigger/migration tests, state contract tests and task UI/native serialization checks.

### 2.3 Atomic independent review verdicts and escalation (depends: 2.2)
`kind: deliverable`
`[category: code]`

Targets:
- `src/gobby/tasks/close_receipts.py::*` — scope-reason: independent approval/rejection transaction and exact-SHA authorization
- `src/gobby/mcp_proxy/tools/tasks/_landing.py::*` — scope-reason: expose review verdict recording and retire obsolete receipt kinds
- `src/gobby/storage/tasks/_transitions.py::*` — scope-reason: atomic failure increment and third-rejection escalation
- `src/gobby/storage/tasks/_de_escalation.py::*` — scope-reason: audited Triage reset
- `src/gobby/tasks/review_verdicts.py`
- `tests/tasks/test_review_verdicts.py`

**Research context:** The assessment corrected an earlier mistaken claim: `increment_validation_failure` already atomically increments, clears the claim and escalates at a threshold. Reuse that transaction after the rename, with threshold three. Store full feedback and reviewer identity/SHA/round/epoch in durable task comments, with a unique logical verdict identity under a task lock. Both approval and rejection reject editors/former claimants. Replays return the original result; contradictory same-identity submissions fail. Infrastructure errors report error without counting a rejected code review. Triage resets begin a new audited epoch without erasing comments. Runtime authorization limits resets to the active authorized Triage/operator route.

**Acceptance:**
- 2.3.1 - Concurrent/replayed verdicts count once and retain full feedback atomically. test: `tests/tasks/test_review_verdicts.py::test_verdict_is_atomic_and_idempotent`.
- 2.3.2 - Editors and former claimants cannot approve; stale-SHA approval cannot authorize landing. test: `tests/tasks/test_review_verdicts.py::test_independent_exact_sha_review`.
- 2.3.3 - The third rejection escalates and blocks further ordinary rounds until an audited reset; errors do not increment. test: `tests/tasks/test_review_verdicts.py::test_three_rejections_and_audited_reset`.

**Verification:** Planned concurrent storage tests and MCP authorization tests with isolated sessions/tasks.

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

**Research context:** The cited landing evidence separates schema carriers from restart approval. The assessment notes current landing requires the caller's own reviewer receipt and refuses former claimants. Authorize the task's Dev Manager to use a distinct independent reviewer's current receipt. Preserve current ancestry-safe landing and transaction/lock checks, idempotently record the landing, resolve the actual source branch/tree from Git and task binding, refresh its registry association, and call `mark_merged(cleanup_days=0)`. Do not rely on stale lane labels. Disjoint intervening integration changes permit a two-parent merge without per-task retest; overlapping paths or semantic conflicts produce a structured Triage escalation. No creator/delegator receipt transfer or activation wait remains.

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

**Research context:** Storage supports expected-owner handoff but the MCP layer currently conflicts unless forced/delegated, and release authority is lane-based. Use the bound Dev Manager and expected owner to transfer the single task claim atomically; never use a global force bypass. Persist the review round and seat transition in team metadata, using 2.3's verdict as the durable trigger. Sequence exactly: Test Writer → Developer → Sol review; rejection one → original Developer → same Sol review; rejection two → Sol reviewer becomes claimant/fixer → fresh Opus review; rejection three → Triage. A seat that edits is recorded as an editor even if its role label still says reviewer. Resuming a team resumes this state, never recounts transcript messages.

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
- `src/gobby/install/shared/workflows/agents/log-monitor.yaml::*` — operation: delete — scope-reason: retire the complete unused runbook role
- `src/gobby/install/shared/workflows/agents/metrics-monitor.yaml::*` — operation: delete — scope-reason: retire the complete unused runbook role
- `src/gobby/install/shared/workflows/agents/recovery-steward.yaml::*` — operation: delete — scope-reason: retire the complete unused runbook role
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — operation: delete — scope-reason: retire the complete automatic close review role
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

**Research context:** Existing pipelines explicitly launch lane/coordination seats; the current model has no Test Writer and uses lane-based release/close procedures. Replace those launch contracts with task-bound `devteam` and five-seat `admin`. Rename managers directly, rewriting identity, authority and procedures at the destination; then retire the old registry names and source files. Delete only the four explicitly retired definitions. Researcher, security-reviewer, consolidator, triage-agent and current planning definitions remain usable. Fold security review checks from the standalone security-reviewer into Code Reviewer without deleting that standalone role. Skill and rule catalogs/reference audits must match the resulting files. Keep generic templates portable; apply Gobby-specific integration branch/model defaults and live role routing through project policy. Remove lane limits, grants, quiet-worker requirements and old receipt relay text.

**Granularity:** These configuration files jointly define one usable launch contract. Runtime mechanisms land first; definition and registry activation occur as one coordinated cutover so no partially renamed seat can launch.

**Acceptance:**
- 3.5.1 - Admin launches exactly Assistant/Orchestrator/Archivist/Release Manager/Triage; DevTeam launches the four approved roles in one prepared task tree. test: `tests/workflows/test_devteam_runbooks.py::test_runbook_seat_catalogues`.
- 3.5.2 - Retired and renamed source/installed registry identities are absent, and the three retained standalone roles plus triage-agent/planning roles remain callable. test: `tests/workflows/test_devteam_runbooks.py::test_agent_retirement_and_retention`.
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

Observed execution on October 21: Josh waived test-first write ordering for #23850, relayed by Assistant #15976 and applied by Orchestrator #17281. The session projection refreshed and production writes succeeded. Implementation and focused verification are underway in the existing task worktree. Passing checks include maintenance, cron scheduling, messaging, stage transitions and terminal cleanup; final validation, commit, review and landing remain pending. The earlier worktree-root authorization defect remains tracked as #24779. This narrative is still a draft without M1 or expansion; it does not claim that the broader redesign has shipped. The Orchestrator owns installs, restarts and pushes.

Project-aware base validation passed on October 21: `uv run gobby plans validate .gobby/plans/runbook-devteam-redesign.md -p /Users/josh/.gobby/worktrees/gobby/task-23850` exited zero and reported all six phases. The narrative contains seventeen deliverables. Rerun base validation after every accepted revision. Expansion validation remains unrun until a server-derived manifest exists. Execution completion requires the existing build-removal leaves, all deliverables above, focused verification, coherent live activation, and the recorded pilot. This file is the sole narrative authority; later displays and handoffs link to it.
