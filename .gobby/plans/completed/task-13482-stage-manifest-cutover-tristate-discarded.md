# Stage Manifest Cutover — Implementation Plan for #13482

`plan_kind: implementation` — deliverable manifest emitted by plan-adversary on approval.

## Plan Changelog
`kind: framing`

- **Round 1 → 2 (2026-04-30, mechanical):** `parse_plan(parse_mode='draft')` rejected the round-1 draft for two encoding errors found by `gobby-tasks-ops:validate_plan_file`. (1) Acceptance items used the literal shorthand `A<section>.<n>` (e.g., `A1.1.1`) under purely-numeric sections (e.g., `1.1`); the parser requires `item_id.startswith(f"{section_id}.")`, so items must be `1.1.1` for section `1.1`. Renamed all ~106 acceptance item IDs across all 21 deliverables and 6 inline body cross-references. (2) `## Verification` carried `kind: verification` with no canonical section ID; the parser rejects non-canonical headings tagged anything other than `kind: framing`. Renamed to `## V1 Verification`. (3) Phase headings used `## Phase N: Name` (numeric section IDs) per the plan-draft skill template, but the contract-aware expansion compiler at `src/gobby/tasks/expansion/_common.py:219` requires phase IDs to match `^P\d+$`. Renamed to `## P<N> Name` for all 7 phases so `phase_count` populates correctly and TDD wrapping works at expansion time. Three follow-ups filed during this round: #13665 (clarify `A<section>.<n>` shorthand in contract docs — closed), #13666 (wire `validate_plan_file` into planning-agent spawn path as a pre-flight gate — open), #13667 (fix plan-draft skill phase-heading syntax to teach `## P<N>` form — open). No design changes; all edits were mechanical encoding fixes.
- **Round 2 planner revisions (2026-04-30, coordinator review notes):** Coordinator surfaced 10 design/wiring gaps before adversary spawn; applied as follows. (1) `is_escalated` lives on `tasks` from migration 233 (not `task_artifacts`); backfill from `escalated_at IS NOT NULL` folded into migration 234; deliverable 5.2 reduced to `Task` dataclass + reader updates (no separate migration). (2) `record_merge_result` (Phase 4.2) reads the merge cap via Python-side fallback (`cap = artifacts.max_merge_attempts if artifacts.max_merge_attempts is not None else build_config.max_merge_attempts`, mirroring the existing `_cap()` pattern at `src/gobby/dispatch/rules.py`) against the existing nullable `task_artifacts.max_merge_attempts` column (added pre-epic by `_add_task_artifact_retry_cap_columns`); no new column add needed. (3) `attempt_count` semantics in 2.1 clarified: `start_stage` is the sole increment site; `fail_stage` transitions `in_progress → ready` with no count change. (4) Mutex invariant added to 2.1 (item 6, acceptance 2.1.6): every `StageStatesManager` mutator wraps in `with RuntimeDispatchMutex(storage=..., task_id=..., holder=..., action_kind=..., ttl_seconds=...):` to serialize concurrent transitions across all surfaces. (5) `auto_advance_ready_rule` added to 3.1 rule table (acceptance 3.1.6): promotes leftmost `ready` row to `in_progress` when prior stage is `done` or position is 1, no `requires_human`, and agent is registered/enabled. Closes the fresh-task-stalls-at-ready gap. (6) `record_pr_opened` moved from 4.1 into 2.3's tool list (now 10 tools, acceptance 2.3.5); 4.1 keeps only `pr_rule` wiring and PR-stage transitions. (7) Acceptance 5.3.7 added for pre-rewire call-site audit of `mark_task_review_approved` / `mark_task_review_rejected` / `mark_task_needs_review`. (8) Acceptance 6.1.9 added for CSS lint test on `web/src/styles/lifecycle-board.css`. (9) 5.4 test pattern clarified: integration-marked, skip on missing seed. No new design choices — all fixes close concrete gaps in the existing design.
- **Round 15 planner revisions (2026-05-01, post-round-14 adversary):** Round 14 adversary review surfaced 2 NEW blocking findings (closed anchor #13714). **F1 (traceability, acceptance 2.1.10 helper-signature spelling diverged from invariant 8 / docstring / §4.2)** — Acceptance 2.1.10 said `_close_task_in_txn` accepts `closed_commit_sha`, but the round-7/-8 invariant block, the round-14 docstring, acceptance 2.1.9, and §4.2's narrative all use `commit_sha`. The divergence makes `tests/storage/tasks/test_close_task_in_txn.py::test_helper_signature_accepts_all_params` ambiguous. **F2 (unhandled-edge, no escalation for disabled-agent ready stages)** — `auto_advance_ready_rule` (acceptance 3.1.6 condition (c)) silently skips ready stages when the registered `default_agent` is missing or `enabled: false`. For research_spike / prd_doc / architecture_doc manifests starting at discovery stages (ideation / research / architecture / prd) where the placeholder agents are `enabled: false` (Phase 1.3), tasks would stall at `<discovery>.ready` forever — no auto-start, no spawn, no escalation — contradicting the §1.3.5 "surface the gap loudly" placeholder contract and making the §5.1.4 discovery-stage walks unreachable under dispatcher control. **Both fixes applied as a coordinated edit.** (1) Acceptance 2.1.10 updated: helper signature now reads `force`, `cascade_descendants`, `closed_in_session_id`, `commit_sha` (canonical, matches invariant block / 2.1.9 / docstring / §4.2), `closed_at`, `validation_override_reason`. Public `close_task(...)` API note added: its existing `closed_commit_sha` parameter (if any) maps to `_close_task_in_txn(..., commit_sha=closed_commit_sha, ...)` at the wrapper boundary; one canonical helper-side keyword. (2) New rule `disabled_agent_escalation_rule` added to §3.1's rule table, ordered AFTER `auto_advance_ready_rule` and BEFORE the per-stage in-progress rules. Body: fires when `current_stage(task).state == 'ready'` AND `current_stage.name NOT IN {'development', 'holistic_qa'}` (those owned by other rules) AND the registry's `default_agent` is missing OR resolves to a bundled agent with `enabled: false`. Action: `EscalateAction(task_id=task.id, reason=f'{stage_name}_no_agent')`. This catches the four discovery placeholder cases (`ideation_no_agent`, `research_no_agent`, `architecture_no_agent`, `prd_no_agent`) and any other stage whose default_agent slot is later set to a disabled bundled agent. Acceptance 3.1.6 reworded to clarify auto_advance no longer claims to handle disabled-agent stages — the escalation rule does. New acceptance 3.1.23 covers the disabled-agent escalation rule with parameterized sub-tests across the four discovery stages. Acceptance 1.3.5 reworded so its assertion ("disabled placeholder treated as missing") is cross-referenced to the new rule, not a generic dispatcher behavior. Acceptance 5.1.4 (`research_spike` walks ideation→research→prd→closed) extended with a sub-test `test_research_spike_at_ideation_with_disabled_placeholder_escalates_with_ideation_no_agent` to prove the dispatcher behavior is loud, not silent. No deliverable count change (still 23); +1 acceptance item (3.1.23); +1 sub-test on 5.1.4.
- **Round 14 planner revisions (2026-05-01, post-round-13 adversary):** Round 13 adversary review surfaced 1 blocking finding (closed anchor #13713). **F1 (traceability, §2.1 stale `complete_stage` docstring contradicts invariant 8)** — The `complete_stage` method docstring inside the `StageStatesManager` code block in §2.1 still said the terminal-close branch "ALSO closes the task atomically in the same DB transaction by calling `close_task(task_id, reason='manifest_exhausted', commit_sha=commit_sha)`". That contradicts the round-7/-8 invariant-8 contract which requires `complete_stage` to call `_close_task_in_txn(...)` directly on the already-open transaction with `cascade_descendants=(stage_name == 'merge')`. The §4.2 rewrite (round 13 F3) was correct, but the docstring upstream was missed. A worker expanding §2.1 sees the docstring first and could implement the forbidden nested-public-close path. **Fix.** Updated the `complete_stage` docstring in the §2.1 method API block to call `_close_task_in_txn(conn, task_id, reason='manifest_exhausted', commit_sha=commit_sha, closed_at=now, closed_in_session_id=by_session_id, cascade_descendants=(stage_name == 'merge'))` inside the same transaction. The docstring now also states explicitly that public `close_task(...)` is only a thin wrapper around the same helper with `cascade_descendants=False`, and that `complete_stage` does NOT invoke `close_task`. The signature spelling matches §2.1 invariant 8, acceptance 2.1.10, and §4.2's narrative — one canonical helper signature across the three surfaces. No deliverable count change (still 23); no new acceptance items; just docstring alignment.
- **Round 13 planner revisions (2026-05-01, post-round-12 adversary):** Round 12 adversary review surfaced 3 NEW blocking findings (closed anchor #13712), all real code-level gaps the prior audit-scope expansions missed. **F1 (weak-testability, §5.3.9 misses dynamic sync writes)** — Round-12 §5.3.9 caught static SQL inserts and JSONL exports but missed `src/gobby/sync/tasks.py::TaskSyncManager.import_from_jsonl`'s dynamic write path: it reads `lifecycle_stage` from `tasks` (lines 245-249), reads top-level JSONL `status` / `lifecycle_stage` (lines 352-361), stores them in `synced_values` (lines 380-392), then dynamically builds `INSERT INTO tasks ({columns})` and `UPDATE tasks SET {set_clause}` (lines 469-484). An implementation could satisfy the static grep + export-shape test while leaving this importer writing dropped columns at runtime. **F2 (unhandled-edge, §7.1.5 misses expansion facade)** — Round-12 §7.1.5 covered the `src/gobby/tasks/expansion/` package but missed sibling facade `src/gobby/tasks/expansion_service.py` which imports and re-exports `_skipped_stages` from `_common` (lines 17-21, `__all__` at 24-30). Deleting `_common._skipped_stages` per §7.1.5 either breaks the facade at import time or leaves a stale public-compatibility surface advertising the deleted helper. **F3 (traceability, §4.2 stale prose contradicts round-7 helper contract)** — §2.1.9 / §2.1.10 (round 7-8) define the terminal-close path as `complete_stage(...) → _close_task_in_txn(..., cascade_descendants=(stage_name == 'merge'))` with public `close_task(...)` ALWAYS passing `cascade_descendants=False`. §4.2's narrative still said "Cascade-close behavior is preserved by routing close_task's existing cascade logic through this single generic path" and "complete_stage calls close_task" — both contradict the helper contract. The merge worker following §4.2 literally would either preserve cascade in the public `close_task` (forbidden per round-7) or fail to assert the merge-only cascade=True branch. **All three fixes applied as a coordinated edit.** (1) §5.3.9 audit extended with dynamic-write detection. New patterns: dict-key sources for legacy columns (`'status': legacy_status`, `'lifecycle_stage': lifecycle_stage`, `'lifecycle': legacy_lifecycle` literal-key dict construction in task-sync code), dynamically-built SQL where `synced_values` or equivalent dicts include legacy column keys. `src/gobby/sync/tasks.py::TaskSyncManager.import_from_jsonl` added to the named ports list: removes `status` / `lifecycle_stage` from `synced_values`, stops reading them from `tasks` rows, drops them from JSONL key recognition. New positive regression `tests/sync/test_task_jsonl_import_shape.py::test_import_does_not_write_legacy_columns` covers the import path explicitly. The audit pattern for dict-key writes is scoped to task-sync code (`src/gobby/sync/`) and task-CRUD (`src/gobby/storage/tasks/`) to avoid false-positives against unrelated `status` keys (validation status, run status, etc.). (2) §7.1.1 and §7.1.5 scope extended to include `src/gobby/tasks/expansion_service.py`. The facade is required to drop the `_skipped_stages` import and remove it from `__all__`; any test asserting the facade exposes `_skipped_stages` is updated to assert it does NOT (post-cleanup negative regression). The audit grep target list adds the facade explicitly so the deletion is checked. (3) §4.2 narrative and acceptance 4.2.2 rewritten. The success path now states: `record_merge_result(merge_sha=...)` writes `merge_commit_sha` + `merge_campaign_report`, then calls `complete_stage('merge', commit_sha=merge_sha)`. The §2.1 invariant-8 path inside `complete_stage` calls `_close_task_in_txn(..., cascade_descendants=True)` because `stage_name == 'merge'` (per round-7's caller-cascade rule); this is the cascade-aware close that replaces the legacy `mark_task_merged` cascade. Public `close_task` is NOT invoked anywhere on this path — the merge close is `complete_stage` → `_close_task_in_txn` directly. Acceptance 4.2.2 reworded to test the `complete_stage` call and the cascade=True passthrough; the `close_task` reference is removed. The "Cascade-close behavior from `mark_task_merged` MUST be preserved" paragraph is updated to point at `_close_task_in_txn(cascade_descendants=True)` as the single inheritor. No deliverable count change (still 23); +0 acceptance items (5.3.9 / 7.1.1 / 7.1.5 / 4.2.2 are all wording / scope expansions of existing items, no new IDs).
- **Round 12 planner revisions (2026-05-01, post-round-11 adversary):** Round 11 adversary review surfaced 2 NEW blocking findings (closed anchor #13710) by digging deeper into actual code paths. **F1 (weak-testability, §5.3 audit scope misses storage CRUD + expansion writers)** — Round-11 §5.3 named `_transitions.py`, MCP, HTTP, CLI as targets, and 5.3.9's grep matched `.status` comparisons / `tasks.status =` writes / `Task.status` accesses. It missed: `src/gobby/storage/tasks/_crud.py` (`INSERT INTO tasks (...)` includes `status` column; `update_task` appends `status = ?` and `lifecycle_stage = ?`; `LocalTaskManager` list/update surfaces accept lifecycle/status parameters), `src/gobby/tasks/expansion/_apply.py` (writes `UPDATE tasks SET lifecycle = 'in_development'` for dev-only expansion paths), and task sync / JSONL export paths that round-trip `status`. An implementing agent could satisfy the literal grep + MCP/HTTP/CLI response-shape tests while leaving `create_task`, `update_task`, sync export, and dev-only expansion writing the dropped columns — the migration would crash on first call after the drop. **F2 (unhandled-edge, §7.1.1 scope misses expansion + storage label readers)** — Round-11 §7.1.1 scoped to `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/cli/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/`. Adversary identified runtime label-readers OUTSIDE that scope: `src/gobby/tasks/expansion/_common.py::_skipped_stages`, `src/gobby/tasks/expansion/_compile.py::_build_prompt_context` (reads `_skipped_stages`), `src/gobby/tasks/expansion/_apply.py::_complete_dev_only_run` (dev-only expansion bypass via labels), `src/gobby/storage/tasks/_crud.py::_skipped_stages` and `cascade_build_state_to_subtree`. Bundled developer-agent instructions also reference `_skipped_stages` so the manifest-resolution chain can keep depending on legacy labels via indirection. **Both fixes applied as a coordinated edit.** (1) §5.3 target list expanded to include `src/gobby/storage/tasks/_crud.py`, `src/gobby/storage/tasks/_manager.py`, `src/gobby/storage/tasks/_queries.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/sync/` task-sync paths, plus the existing CRUD/MCP/HTTP/CLI surfaces. The narrative names each port destination concretely: `_crud.py::create_task` and `update_task` lose `status` / `lifecycle` / `lifecycle_stage` parameters and column references; `_apply.py::_complete_dev_only_run` ports its `UPDATE tasks SET lifecycle = 'in_development'` write to a `complete_stage(task_id, 'expansion')` call against the parent task's manifest (matching the dev-only expansion bypass semantics in the new model); `LocalTaskManager` list/update/filter parameter signatures drop `status` / `lifecycle` kwargs. Acceptance 5.3.9 expanded to a multi-pattern audit covering: legacy enum string-literal comparisons against `.status` / `.lifecycle` / `.lifecycle_stage`; `INSERT INTO tasks (... status ...)`, `INSERT INTO tasks (... lifecycle ...)`, `INSERT INTO tasks (... lifecycle_stage ...)` patterns; `status = ?`, `lifecycle = ?`, `lifecycle_stage = ?` SQL parameter forms; function-parameter usages of `status: str`, `lifecycle: str`, `lifecycle_stage: str` on task CRUD/list/update APIs; JSONL/sync exports that emit `status` / `lifecycle` / `lifecycle_stage` keys. Audit scoped to task-state code (`src/gobby/storage/tasks/`, `src/gobby/tasks/`, `src/gobby/sync/`, plus the existing MCP/HTTP/CLI/dispatcher/build directories) so unrelated tables with their own `status` columns (workflows, sessions, etc.) do not false-positive. Audit returns zero matches outside the historical-migration boundary post-port. (2) §7.1.1 scope expanded to include `src/gobby/tasks/expansion/` (`_common.py`, `_compile.py`, `_apply.py`), `src/gobby/storage/tasks/` (`_crud.py`'s `_skipped_stages` helper and `cascade_build_state_to_subtree`), and bundled agent instruction surfaces under `src/gobby/install/shared/workflows/agents/` and `src/gobby/install/shared/skills/` that mention `_skipped_stages` or `stage-:`. Migrations and migration-specific tests remain explicitly exempt. New acceptance 7.1.5 covers positive regression that expansion's prompt-context construction, dev-only expansion handling, and build-cascade behavior all read skipped/included stages from the resolved manifest (`task_stage_states`) rather than labels — `_compile.py::_build_prompt_context` reads `task.stages`, `_apply.py::_complete_dev_only_run` calls `complete_stage(task_id, 'expansion')`, and `cascade_build_state_to_subtree` writes manifests via `initialize_manifest` instead of `stage-:` labels. test: `tests/tasks/expansion/test_compile_uses_manifest.py::test_prompt_context_reads_stages_not_labels`, `tests/tasks/expansion/test_apply_dev_only.py::test_complete_dev_only_run_via_complete_stage`, `tests/storage/tasks/test_cascade_build_state.py::test_cascade_uses_initialize_manifest`. Bundled-agent instruction sweep: `grep -rln '_skipped_stages\|stage-:' src/gobby/install/shared/` returns zero matches post-Phase-7.1; any agent YAML/skill referencing the legacy helper is rewritten to point at the manifest read path. No deliverable count change (still 23); +1 acceptance item (7.1.5).
- **Round 11 planner revisions (2026-05-01, post-round-10 adversary):** Round 10 adversary review surfaced 2 NEW blocking findings (closed anchor #13708). **F1 (missing-requirement, §5.3 `status` column drop optional)** — Strategy required dropping `status` entirely, but §5.3 said the column "stays OR the column is dropped entirely" with the choice gated on whether the audit surfaces a hard reader. Acceptance 5.3.1 only required dropping `lifecycle` / `lifecycle_stage`, so an implementing agent could legally produce a final schema retaining `status` as a shadow model — violating the strategy's clean-cutover constraint. **F2 (weak-testability, §7.1 acceptance 7.1.1 blanket `stage-:` grep)** — Acceptance 7.1.1 demanded `test_grep_returns_empty` for all `stage-:` reads, but §7.1's own narrative explicitly preserves the migration-234 backfill helper as a frozen historical record. The blanket grep is unsatisfiable, OR pressures the implementing agent to delete migration-234's legacy-label honoring (breaking pre-cutover-DB upgrade replay). **Both fixes applied as a coordinated edit.** (1) §5.3 `status` drop made deterministic. The migration-step list rewrites step 3 to drop the `status` column unconditionally. The narrative replaces the optional-drop language with: "Pre-flight audit identifies remaining readers/writers of `tasks.status`; each is ported in this same deliverable to use `closed_at IS NOT NULL` (closure), `is_escalated` projection (Phase 5.2), or stage-state reads (everything else). The migration is blocked until the audit returns zero hard readers." `Task.status` Literal field is REMOVED unconditionally; `serialize_task_state` strips `status` from the response shape. Acceptance 5.3.1 expanded to drop all three columns; new acceptance 5.3.9 covers the `status` audit-and-port — a source-code grep for legacy status enum string-literal comparisons (`open`, `in_progress`, `needs_review`, `review_approved`, `escalated`, `closed`) and direct `tasks.status =` writes returns zero matches outside historical migrations and migration-specific tests post-implementation. (2) §7.1 acceptance 7.1.1 rescoped to runtime code only. The check is a grep for `stage-:` token across `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/cli/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/` returns zero matches (runtime/dispatcher/build/CLI/MCP/HTTP scope); migrations and migration-specific tests are explicitly exempt. New acceptance 7.1.4 adds positive regression: migration 234's `_backfill_task_stage_states_from_legacy` helper still honors `stage-:<name>` skip labels when replayed against a pre-cutover fixture DB — the backfill mapping table's outputs match the documented per-task-type defaults minus skip-label-removed stages. test: `tests/storage/test_migration_234_backfill.py::test_replay_against_pre_cutover_db_honors_legacy_skip_labels`. No deliverable count change (still 23); +2 acceptance items (5.3.9, 7.1.4).
- **Round 10 planner revisions (2026-05-01, post-round-9 adversary):** Round 9 adversary review surfaced 1 blocking finding (closed anchor #13707). **F1 (weak-testability, acceptance 3.1.10 self-matching regression regex)** — Round-9 acceptance 3.1.10 said the regression check forbids any occurrence of `EscalateAction\(.*detail=` in the plan file or `src/gobby/dispatch/rules.py`, but the plan's own changelog prose (rounds 7-9 entries discussing the bug being fixed) and the 3.1.10 assertion line itself match that pattern. A worker running the literal grep would fail before touching code, making the acceptance unsatisfiable. **Fix.** Scope the regression check to runnable source code only: `grep -nE 'EscalateAction\(.*detail=' src/gobby/dispatch/rules.py src/gobby/dispatch/actions.py` returns no matches post-implementation. The plan file is exempt because its changelog and acceptance text are historical artifacts documenting the bug; the two concrete tests `test_isolation_failure_escalates_with_reason_carrying_error_type` and `test_isolation_failure_escalation_uses_supported_signature_only` remain the canonical correctness gates. No deliverable count change (still 23); no new acceptance items; just narrowed scope on 3.1.10's plan-level regression assertion.
- **Round 9 planner revisions (2026-05-01, post-round-8 adversary):** Round 8 adversary review surfaced 2 NEW blocking findings (closed anchor #13706). **F1 (unhandled-edge, §3.1 case-(d) `EscalateAction(detail=...)` unsupported kwarg)** — Pre-existing language from round 3 still wrote `EscalateAction(task_id, reason='development_isolation_failed', detail=<error message>)`, but `EscalateAction` at `src/gobby/dispatch/actions.py:63` only accepts `task_id` and `reason`. Round 8's escalation-helper fix had explicitly removed `detail` from the close-failure path but didn't sweep this older case. **F2 (gobby-format, §1.3 / §5.4 per-row acceptance decomposition)** — Both deliverables have four-row `Stage | Agent slug` tables (`ideation→analyst`, `research→researcher`, `architecture→architect`, `prd→product-manager`) but their acceptance items aggregated across all four rows; the plan-coverage contract's table-row decomposition rule requires one acceptance item per data row with stable IDs. **Both fixes applied as a coordinated edit.** (1) §3.1 case (d) language rewritten to `EscalateAction(task_id=task.id, reason=f'development_isolation_failed:{type(error).__name__}')` — error type encoded INSIDE the supported `reason` string, no unsupported kwarg. Acceptance 3.1.10 updated to verify the reason format `development_isolation_failed:<error_type>` and add a regression sub-test that no `EscalateAction(..., detail=...)` construction appears in the plan or implementation. (2) §1.3 acceptance items 1.3.6-1.3.9 added (one per row of the Stage|Agent-slug table), each verifying the per-slug YAML's existence, banner content, escalation-reason string, FK resolution, and sync state with stable IDs. (3) §5.4 acceptance items 5.4.5-5.4.8 added (one per row), each verifying the per-stage child task's title, label set, parent linkage, and placeholder YAML reference. The aggregate items (1.3.1-1.3.5, 5.4.1-5.4.4) remain as cross-cutting checks; the per-row items are the stable-ID coverage. No deliverable count change (still 23); +9 acceptance items total (3.1.10 wording update + 4×§1.3 + 4×§5.4).
- **Round 8 planner revisions (2026-05-01, post-round-7 adversary):** Round 7 adversary review surfaced 3 NEW blocking findings (closed anchor #13705) on the round-7 fix. **F1 (bad-sequencing, missing closure-source column)** — Round 7's close-pass SQL wrote `is_closed = 1` and filtered `is_closed = 0`, but no `tasks.is_closed` column exists in the schema (`src/gobby/storage/baseline_schema.sql:265-275` only has `closed_at`, `closed_in_session_id`, `closed_commit_sha`, `status`); `is_closed` is a Python projection at `src/gobby/tasks/state_semantics.py:88-95` reading `closed_at IS NOT NULL OR status == 'closed'`. The migration would have failed with `no such column: is_closed`. **F2 (unhandled-edge, helper signature too narrow)** — `_close_task_in_txn(conn, task_id, *, reason, commit_sha, closed_at)` cannot unify the two behaviors it claims to: the public `close_task` API carries `force`, `closed_in_session_id`, `closed_commit_sha`, `validation_override_reason`, open-child checks, and bootstrap-ledger validation; the legacy cascade-close behavior lives in `mark_task_merged` via `advance_lifecycle(..., cascade_close=True)` / `_cascade_merged_close`, NOT in `close_task`. The plan never specifies when cascade is enabled vs forbidden, leaving two bad implementations possible: public `close_task` cascading unexpectedly, or merge-terminal `complete_stage` failing to cascade descendants. **F3 (unhandled-edge, escalation idempotency + signature)** — The separate-transaction escalation calls `escalate_task(reason=..., detail=str(error))`, but the existing escalation surface does not accept a `detail` kwarg, and the plan does not specify what happens when `escalate_task` itself raises or is gated. If escalation fails after a stage rollback, the task is back at `in_progress`, NOT closed, NOT escalated, so §3.2's heartbeat filter can re-attempt the same terminal close indefinitely. **All three fixes interlock as a single coordinated edit.** (1) **Closure source canonicalized to `closed_at`.** `tasks.closed_at IS NOT NULL` is the canonical SQL closure predicate; `Task.is_closed` is a read-only Python projection sourced from `is_task_closed(task)` at `src/gobby/tasks/state_semantics.py:88-95` (reads `closed_at IS NOT NULL OR status == 'closed'`). All SQL writes target `closed_at`; all SQL filters use `closed_at IS NULL` / `closed_at IS NOT NULL`. The §2.2 close-pass UPDATE rewrites to `WHERE closed_at IS NULL` and `SET closed_at = datetime('now'), closed_in_session_id = 'migration:234'`. The §2.1 invariant-8 "atomicity guarantee" paragraph rewrites `is_closed = 0/1` literals to `closed_at IS NULL / IS NOT NULL`. The `is_child_parked` predicate keeps `child.is_closed` (projection) since `Task.is_closed` is a real attribute; no read-site change. (2) **Helper signature expanded.** `_close_task_in_txn(conn, task_id, *, reason, commit_sha, closed_at, closed_in_session_id, force=False, cascade_descendants=False, validation_override_reason=None) -> None` carries all close+cascade inputs. The public `close_task(...)` wrapper passes the kwargs the existing API takes (`force`, `closed_in_session_id`, `closed_commit_sha`, `validation_override_reason`) and `cascade_descendants=False` by default, preserving open-child checks and bootstrap-ledger validation. `complete_stage`'s terminal branch passes `cascade_descendants=True` ONLY when the just-completed stage is `merge` (replaces the legacy `mark_task_merged` cascade); for non-merge terminal stages (`prd`, `architecture`, etc.) cascade is False because there are no descendants under non-merge terminal task types in this epic's scope. Open-child / bootstrap-ledger validation runs inside the helper (moved from `close_task`'s wrapper if currently there) so both callers go through the same checks; the wrapper's only added behavior is opening the transaction. (3) **Idempotent escalation helper.** New private helper `_emit_terminal_close_failed_escalation(task_id, *, stage_name, error)` is called by `complete_stage` after rollback in a SEPARATE committed transaction. The helper: (a) reads current `is_escalated` projection; if already True, returns success without writing (idempotent — if a prior failure already escalated, this attempt is a no-op); (b) otherwise writes `escalated_at = datetime('now')`, `escalation_reason = f'terminal_close_failed:{stage_name}:{type(error).__name__}'` via the existing `escalate_task` write path (no unsupported kwargs); (c) if the escalation write itself raises, the helper logs the error at ERROR level and re-raises — this is the documented last-resort failure mode where retry cannot be capped (database-write failure is a separate operational concern, surfaced via logging + the original exception still propagates to the caller). The plan documents this as the only uncapped failure mode and notes that DB-write failure is operationally distinct from logic bugs. Acceptance 2.1.9 sub-tests extended from seven to ten: added `test_close_failure_escalates_idempotently_on_already_escalated`, `test_escalation_helper_uses_supported_signature_only`, `test_escalation_helper_db_write_failure_logs_and_reraises`. New acceptance 2.1.10 covers the helper signature + cascade rules: cascade_descendants=True only for merge-terminal complete_stage path, False for all other callers; force/validation_override pass-through; open-child/bootstrap-ledger validation runs inside helper. Acceptance 2.2.31 SQL rewritten with `closed_at` predicate. No deliverable count change (still 23); +1 acceptance item (2.1.10); no schema/column adds.
- **Round 7 planner revisions (2026-05-01, post-round-6 adversary):** Round 6 adversary review surfaced 3 blocking findings (closed anchor #13704; adversary blocked from formal `mark_task_review_rejected` by upstream Gobby step enforcement, findings conveyed by user). **F1 (traceability, §2.1 invariant 8 transaction composition)** — Invariant 8 said "the close runs in the SAME DB transaction as the stage transition" without specifying HOW the existing `close_task` (its own transaction boundaries plus `mark_task_merged`-style cascade-close logic) composes inside `complete_stage`'s transaction. **F2 (unhandled-edge, §2.1 invariant 8 close-failure retry)** — On close failure, rollback leaves the highest-position row at `in_progress` with no escalation cap; subsequent heartbeats / agent retries re-attempt `complete_stage` indefinitely. **F3 (traceability, §3.1 `is_child_parked` predicate `current_stage is None` branch)** — The predicate accepts `current_stage IS NULL AND NOT is_closed`, but invariant 8 says that state is unobservable; the "defense-in-depth" comment didn't document WHEN the branch is reachable, leaving readers unsure whether it covers a real bug or stale documentation. **All three fixes interlock as a single coordinated edit.** (1) Invariant 8 expanded to specify transaction composition: extract close+cascade into a NEW transaction-aware private helper `_close_task_in_txn(conn, task_id, *, reason, commit_sha, closed_at)` that performs all close+cascade SQL on the supplied connection without opening or committing; the public `close_task(...)` becomes a thin wrapper that opens `db.transaction()` and delegates to the same helper, so both code paths share one cascade implementation. `complete_stage` calls the helper inside its own `db.transaction()`. No nested-transaction or savepoint semantics required. (2) Close-failure handling specified: on any helper exception the outer `with db.transaction()` rolls back (stage reverts to `in_progress`); AFTER rollback, in a SEPARATE committed transaction, the task is escalated via `escalate_task(task_id, reason=f'terminal_close_failed:{stage_name}:{type(error).__name__}', detail=str(error))`; the original exception re-raises to the caller. Because §3.2's `list_automation_candidates` filters `NOT is_escalated`, the escalation IS the retry cap — no separate counter, no built-in retry; an operator must `de_escalate_task` before another `complete_stage` attempt is possible. This breaks the "rollback → next heartbeat → spawn agent → retry" loop F2 identified. (3) §2.2 backfill (acceptance 2.2.31, new) adds a final close-pass: any task whose backfilled manifest is all-`done` AND `is_closed = 0` is closed in the same migration-234 transaction with `closed_at = datetime('now')`, `closed_in_session_id = 'migration:234'`. After migration 234 commits, no task satisfies `current_stage IS NULL AND is_closed = 0`. (4) `is_child_parked` predicate docstring rewritten: explicitly enumerates the two reachability windows for the `current_stage is None AND NOT is_closed` branch — (i) the migration-234 transaction before acceptance 2.2.31's close-pass commits (single bounded window per database lifetime), (ii) synthetic test fixtures that bypass `complete_stage` (test-only construct). The branch is therefore safe defense-in-depth: bounded, documented, and aligned with §2.1 invariant 8 + §2.2 acceptance 2.2.31. Acceptance 2.1.9 sub-tests extended from five to seven (added close-failure-escalates and not-re-attempted sub-tests); acceptance 3.1.18 wording updated; new test name `test_is_child_parked_synthetic_branch_is_test_only_or_migration_window` replaces the prior `test_is_child_parked_true_for_synthetic_exhausted_unclosed_leaf`. No deliverable count change (still 23); +1 acceptance item (2.2.31); no schema/column adds; no new migration row.
- **Round 6 planner revisions (2026-05-01, post-round-5 adversary):** Round 5 adversary review surfaced 2 blocking findings (closed anchor #13703). **F1 (bad-sequencing, §2.2/§3.1/§3.2 predicate vs. real manifests)** — `is_child_parked` predicate required `current_stage is None` AND highest-`done` row = `code_review_qa`, but §2.2 defaults don't produce that state for any task type: `task`/`chore` end at `merge` (no `code_review_qa`); `bug`/`refactor`/`feature` have `pr` and `merge` after `code_review_qa`. **F2 (unhandled-edge, §4.2/§5.1/§3.2 terminal-close gap)** — §5.1.4 claimed `research_spike`/`prd_doc` close at manifest exhaustion via "Phase 4.2 terminal-close logic," but Phase 4.2 only specified merge-success close via `record_merge_result`; no generic close path existed, leaving exhausted-but-open tasks stranded by §3.2's `current_stage IS NULL` filter. **Both fixes interlock.** Added a single generic terminal-close contract to §2.1 invariant 8 / acceptance 2.1.9: `complete_stage(task_id, stage_name)` calls `close_task(task_id, reason='manifest_exhausted', commit_sha=commit_sha)` atomically in the SAME DB transaction iff the just-completed row is the highest-position row in the task's manifest. With this contract: (a) all manifest-exhausted leaves auto-close regardless of terminal stage name, so `is_child_parked` simplifies to `_is_leaf AND NOT is_escalated AND (is_closed OR current_stage is None)` — body-aligned with every §2.2 default manifest's terminal stage; (b) `research_spike`/`prd_doc`/`architecture_doc` close via the same generic path as merge-terminal types; (c) §4.2's `record_merge_result` success branch delegates to the generic path (`commit_sha=merge_sha`), no merge-specific close call needed; (d) atomicity guarantees no observable `current_stage IS NULL AND is_closed=0` state, eliminating the §3.2 candidate-scan stranding risk. Updated `complete_stage` docstring in §2.1, added invariant 8, added acceptance 2.1.9 with five sub-tests (terminal closes, non-terminal doesn't, close-failure rolls back, research_spike walks to prd.done close, merge close uses same path). Updated `is_child_parked` predicate body, narrative, and acceptance 3.1.18 with parameterized "true for each default manifest terminal stage" sub-test. Rewrote §4.2's `record_merge_result` docstring + 4.2.2 acceptance to delegate to the generic close path. Updated §5.1.4 prose and acceptance to reference §2.1 invariant 8 (not Phase 4.2). No deliverable count change (still 23); no schema/column adds.
- **Round 5 planner revisions (2026-05-01, post-round-4 adversary):** Round 4 adversary review surfaced 1 blocking finding (closed anchor #13702) — a follow-on to the round-3 F6 fix. The `LeafParkedSignalAction` was transient (no durable cross-heartbeat state) and `leaf_park_rule` could never fire because §3.2's `list_automation_candidates` filter excludes manifest-exhausted leaves (`current_stage IS NULL`). Fix per the adversary's recommendation (option (a)): replace the rule + transient action with a durable `is_child_parked(child) -> bool` predicate computed from `child.stages`. **Edits applied.** (1) Deleted `leaf_park_rule` from the §3.1 rule table; the rule row is preserved as a tombstone marker pointing at the predicate. (2) Rewrote `all_leaves_holistic_rule` to gate on `_is_epic(task) AND current_stage(task) == ('holistic_qa', 'ready') AND every direct child satisfies is_child_parked(child) OR child.is_closed`, emitting `StartStageAction(task_id, 'holistic_qa')`. The parent task is the automation-candidate (its `holistic_qa.ready` keeps it in scan); the rule reads each child's denormalized `child.stages` already populated by `reload_candidate` per 3.1.3 — no extra SQL, no leaf-side rule, no transient signal. (3) Added the `is_child_parked` predicate definition between the rule table and the development-state-machine section; pure function of `child.stages` with no side effects. (4) Added `holistic_qa` to `auto_advance_ready_rule`'s exclusion list alongside `development` (the rule body now reads `current_stage.name NOT IN {'development', 'holistic_qa'}`). Without this, auto-advance would race `all_leaves_holistic_rule` and start the parent's `holistic_qa` before children parked. New "Stages excluded from auto-advance" subsection co-locates both special-cases. (5) Updated acceptance 3.1.6 to mention the holistic_qa exclusion + add `test_auto_advance_skips_holistic_qa`. (6) Replaced acceptance 3.1.18 (was: leaf_park_rule rule) with `is_child_parked` predicate tests (4 sub-tests covering true/false branches). (7) Updated acceptance 3.1.19 (`all_leaves_holistic_rule`) with 4 sub-tests including cross-heartbeat durability and mixed parked/terminal-closed children. (8) Updated §3.2 `list_automation_candidates` description with an explicit note that manifest-exhausted leaves are intentionally excluded and the parent rule reaches into children via the predicate. No deliverable count change (still 23 deliverables); no new schema or column adds; no migration changes. The fix is purely a Python-layer rewire of dispatcher rules and a predicate addition.
- **Round 4 planner revisions (2026-05-01, post-round-3 adversary):** Round 3 adversary review surfaced 6 NEW blocking findings (closed anchor #13701) cascading from round 2 fixes. Applied as follows. **F1 (bad-sequencing, §1.1/§1.2 stages.yaml authoring)** — Moved authoring of `src/gobby/install/shared/registry/stages.yaml` from §1.2 into §1.1's targets. §1.1 now owns BOTH the migration code and the bundled YAML the migration's inline seed reads — same expansion target, no forward dependency from §1.1 to §1.2. §1.2 reframed to own only the `StageRegistryLoader` and daemon startup wiring (the YAML body is still documented in §1.2 as reference for the parser). New acceptance 1.1.6a covers the YAML file's existence and 14-stage completeness. §1.2 acceptance 1.2.1 reframed to verify the parser, not the file contents. **F2 (weak-testability, §2.6 review-tool rewire acceptance)** — Two-part fix. (a) Reframed 2.6.5 to unit/contract scope (mutation-spy + SQL probe verifying `complete_stage`/`fail_stage` are called and no legacy `status` writes happen); the dispatcher heartbeat-advance smoke moves to `## V1 Verification` where `auto_advance_ready_rule` is in place. (b) Added new acceptance 2.6.6 — pre-Phase-3 bundled call-site audit using the same grep enumeration (`grep -rln 'mark_task_review_\(approved\|rejected\)\|mark_task_needs_review' src/gobby/install/shared/`) with an explicit allowlist that includes `test-architect.yaml`, `expansion-qa.yaml`, `requirements-analyst.yaml`, `qa-dev.yaml`, `nightly-linter.yaml`, `nightly-test-fixer.yaml`, `merge-orchestrator.yaml`, `backend-developer.yaml`, `frontend-developer.yaml`, `default.yaml`, `developer.yaml`, plus the bundled SKILL.md surfaces and rule YAMLs. §5.3.7 audit allowlist expanded to match (closing the F2 omission of `test-architect.yaml`). **F3 (traceability, RuntimeDispatchMutex `updated_at`)** — Added `updated_at: str` field to the `StageState` dataclass in §2.1; added invariant 7 stating every mutator (`initialize_manifest`, `add_stage`, `remove_stage`, `start_stage`, `complete_stage`, `fail_stage`) bumps `updated_at = datetime.now(UTC).isoformat()` on every affected row, including same-state cycles like `start → fail` returning `in_progress → ready`. New acceptance 2.1.8 covers the bump invariant including the same-state-cycle case (load-bearing for §3.3's mutex snapshot). §3.2 `reload_candidate` description explicitly notes it projects `task_stage_states.updated_at` into `StageState.updated_at`. **F4 (traceability, §4.1 PR cap source)** — Investigation via `grep -rn 'max_review_rounds\|max_pr_attempts\|max_pr_review' src/gobby/` confirmed `task_artifacts.max_review_rounds` already exists nullable in baseline schema (`src/gobby/storage/migrations.py:93`, `src/gobby/storage/baseline_schema.sql:389`, surfaced as `TaskArtifacts.max_review_rounds` at `src/gobby/storage/tasks/_artifacts.py:72`); `build_config.max_review_rounds = 3` exists at `src/gobby/config/build.py:73`; `src/gobby/dispatch/rules.py:43,53` already binds `max_review_rounds` for `plan_review_attempts` and `test_arch_attempts` via `_maxed_out`. Bound the PR rejection cap to this existing `max_review_rounds` column with the same Python-side `_cap()` fallback to `build_config.max_review_rounds`. NO new column add and NO new migration row to migration 233. §4.1 prose updated to name the exact cap source explicitly with file/line references; new acceptance 4.1.5 verifies the binding (under-cap returns to ready, over-cap escalates with reason `pr_review_failed:max`, NULL artifact falls back to build_config). **F5 (weak-testability, §5.3 pre-drop web audit)** — Replaced the broad `lifecycle` token grep with a multi-pattern, legacy-only grep (`\blifecycle_stage\b`, `\bLifecycle\.`, `\bTaskBucket\b`, `\bTASK_BUCKET_(LABELS|ORDER)\b`, `\bmoveTaskToBucket\b`, `\bgetTaskBucket\b`, `\bKanbanBoard\b`, `\.lifecycle_stage\b`, `\bstate\.lifecycle\b`). The new patterns explicitly do NOT match `LifecycleBoard`, `lifecycle-board`, or `lifecycle-board:hide-blocked` introduced by Phase 6 (verified by `\bLifecycle\.` requiring a literal `.` and word-boundary anchors). Wrote the exact `git grep -nE` command into §5.3 narrative for CI. §5.3.8 acceptance updated; added a positive verification that the new identifiers are NOT matched. **F6 (unhandled-edge, §3.1 `leaf_park_rule`)** — The previous `current_stage == ('code_review_qa', 'done')` predicate was unreachable by definition (`current_stage` returns leftmost non-done row, so a `done` row is excluded). Rewrote the rule's "Gates on" condition (option (a) per the recommended fix) to: `_is_leaf(task)` AND `current_stage(task) is None` (manifest exhausted) AND the highest-position completed row is `code_review_qa`. The action is now `LeafParkedSignalAction(task_id)` — a no-op for the leaf's own rows, surfacing a signal for the parent's `all_leaves_holistic_rule`. Acceptance 3.1.18 rewritten with three sub-tests (parks completed leaf, does not auto-start downstream, inert when terminal row is not `code_review_qa`). No deliverable count change (still 23 deliverables) and no manifest section changes (adversary writes the manifest on approval).
- **Round 3 planner revisions (2026-05-01, post-round-2 adversary):** First substantive design review by plan-adversary surfaced 8 blocking findings (closed anchor #13687); fixes applied as follows. **F1 (bad-sequencing, migration ordering)** — Migration 233 now seeds all 14 `task_stages_registry` rows + the six `task_type_default_stages` bundles INLINE within the same transaction as the schema creation, by reading the bundled `src/gobby/install/shared/registry/stages.yaml` directly. The startup `StageRegistryLoader.sync()` becomes a hash-drift detector for subsequent bundled-YAML edits. Acceptance 1.1.6/1.1.7/1.1.8 added; 1.2.2 reframed; 2.2.5 rewritten to acknowledge defaults are seeded by 233 (not 234). **F2 (bad-sequencing, review-tool rewire)** — New deliverable §2.6 lands the `mark_task_review_approved`/`mark_task_review_rejected`/`mark_task_needs_review` rewire to stage-native `complete_stage`/`fail_stage` BEFORE Phase 3 enables the manifest dispatcher, preserving the agent-facing API. §5.3 retains the call-site audit (now post-rewire) and the legacy-column drop. End-to-end smoke acceptance 2.6.5 verifies adversarial + holistic approvals advance manifest stages. **F3 (unhandled-edge, development_isolation_rule)** — Expanded `development_isolation_rule` description to specify the full state machine for cases (a) `isolation=none`, (b) isolation pair already present, (c) isolation pair missing, (d) isolation creation fails. Acceptance 3.1.7/3.1.8/3.1.9/3.1.10 added (one per case) at `tests/dispatch/test_development_isolation_rule.py`. **F4 (traceability, RuntimeDispatchMutex)** — New deliverable §3.3 cuts over `RuntimeDispatchMutex` from `(expected_lifecycle, expected_status)` tuple match to `(expected_stage_name, expected_stage_state, expected_stage_updated_at)` snapshot; `run_heartbeat` passes the candidate's current-stage snapshot; `StageStatesManager` mutator call sites also pass the snapshot. Acceptance 3.3.1-3.3.4 covers the API change, heartbeat call site, stale-candidate test, and mutator integration. §5.3 `depends_on` updated to include 3.3. **F5 (traceability, attempt_count contract)** — Replaced "Increment attempt or escalate" in §2.3 fail_stage purpose with the §2.1 contract; rewrote §4.1 PR-rejection path and §4.2 merge-failure path to use `fail_stage` (no count change) plus cap-escalation predicate `attempt_count >= cap`. New acceptance 2.1.7 explicitly states the contract. **F6 (bad-sequencing, web-before-column-drop)** — §5.3 `depends_on` extended with `6.3` (and the new 2.6, 3.3 from F2/F4); added pre-drop web audit acceptance 5.3.8 with grep + `pnpm tsc --noEmit` gate before migration 236. **F7 (missing-requirement, record_merge_result)** — Added `record_merge_result` to §2.3 tool table as the 11th tool; tool-count references updated from "Ten" to "Eleven"; acceptance 2.3.6 covers stub registration in 2.3 with `NotImplementedError`, with full body landing in 4.2. **F8 (gobby-format, table-row decomposition)** — Added per-row acceptance items using plain dotted-numeric IDs: §2.2 gains 17 mapping items (2.2.8–2.2.24, one per `(lifecycle, status)` tuple) + 6 task-type items (2.2.25–2.2.30, one per default-manifest row); §2.3 gains 11 per-tool registration items (2.3.7–2.3.17, one per tool table row); §3.1 gains 12 per-rule items (3.1.11–3.1.22, one per remaining rule table row not already covered by 3.1.6/3.1.7-3.1.10). Plain dotted-numeric IDs chosen over compact `M01`/`T01`/`R01` shorthand for unambiguous `item_id.startswith(f"{section_id}.")` validation. Total deliverable count rises from 21 → 23 (added 2.6, 3.3); total acceptance items roughly doubles via per-row decomposition.
- **Round 2 pre-flight fact-check (2026-04-30, mechanical drift):** Pre-adversary fact-check by mid-tier sub-agent caught drift between plan claims and current codebase state. Fixes applied silently per delegated-mode contract. (1) Schema baseline corrected: `BASELINE_VERSION = 220` is current, but migrations 221–232 are all already populated with unrelated work; new epic migrations renumbered 221→233 (Phase 1.1 schema), 222→234 (Phase 2.2 backfill), 223→235 (Phase 5.1 new task types), 224→236 (Phase 5.3 drop legacy), 225→237 (Phase 7.1 label cleanup). (2) `max_merge_attempts INTEGER` already exists nullable in `_task_artifacts_create_sql:91` from `_add_task_artifact_retry_cap_columns`; dropped the F4 column add from migration 233 — Phase 4.2 `record_merge_result` reads the cap via Python-side fallback to `build_config.max_merge_attempts` (mirroring `_cap()` at `src/gobby/dispatch/rules.py`). Acceptance 1.1.3 updated to three new TEXT columns only. (3) Mutex invariant 6 in 2.1 rewritten to use the actual `RuntimeDispatchMutex(storage=..., task_id=..., holder=..., action_kind=..., ttl_seconds=...)` context manager API (defined at `src/gobby/dispatch/mutex.py:27`, backed by `TaskDispatchMutexManager.acquire_mutex` at `src/gobby/storage/tasks/_dispatch_mutex.py:78`); the `task_dispatch_mutex.acquire(task_id)` shorthand was wrong (no such method exists). (4) `_filter_completion_blocks` helper reference in 3.2 corrected — no such Python function exists; the completion-block exclusion is SQL-inline at `_queries.py:211,237,314` and `_aggregates.py:81,103,164`. (5) `WorkflowLoader().sync(` placement-search reference in 1.2 corrected to `sync_bundled_content_to_db(runner.database)` at `runner_init.py:257-259`. (6) Count inconsistency in 2.2 reconciled — body lists 5 manifest entries; acceptance 2.2.5 names 6 task type values; rephrased to "6 task type values seeded across 5 distinct manifests; `chore` and `task` share the leaves-only manifest". (7) Line-number drift fixed across `_transitions.py` (systematic +6 shift on 6 function refs), `_models.py` (148→163), `TasksPage.tsx` (kanban branch at ~601, `moveTaskToBucket` at ~372), `dispatcher.py` (`reload_candidate` at 145-156, not "54-138 area"). All `tests/storage/test_migration_<NNN>*.py` paths renumbered to match new migration versions.

## Overview
`kind: framing`

Replace gobby's dual-enum task state model (`status` + `lifecycle` + `lifecycle_stage`) with a registry-backed, tri-state-per-stage manifest model. Every task carries an ordered, task-type-specific manifest of `(stage_name, state)` rows where `state ∈ {ready, in_progress, done}`. The 14-stage registry is bundled YAML synced to a new `task_stages_registry` table. The dispatcher, MCP/HTTP/CLI surfaces, and the web kanban all migrate to the manifest model. Legacy lifecycle/status columns and active status values are dropped in the same epic — no compatibility shims, no shadow model.

This is the implementation companion to the strategy plan at `.gobby/plans/task-13482-lifecycle-status-kanban.md`. The strategy plan defines the target model; this plan defines the executable steps. Read the strategy plan first if you need the *why* — every section here assumes that context.

## Constraints
`kind: framing`

- **Pre-launch clean cutover.** Do not build compatibility facades or long-lived legacy write paths. Callers move to the stage manifest APIs directly within this epic; old `lifecycle`, `lifecycle_stage`, and active `status` semantics are removed by Phase 5 close.
- **Tri-state vocabulary.** Per-stage state is `ready | in_progress | done` (`ready` replaces the strategy plan's draft term `needs_doing` — same semantics, cleaner reading, aligns with the existing `list_ready_tasks` projection vocabulary). Blocking and escalation are orthogonal: a blocked task still has a `current_stage` and that row stays in `ready` or `in_progress`; a blocked-or-escalated task is filtered out of `is_ready` projections by the readiness check, not by injecting a fourth stage-state value. There is **no** `blocked` value in the stage-state enum.
- **Escalation preserves stage state.** `escalate_task` flips `is_escalated=1` and writes `escalated_at`/`escalation_reason`; it does NOT mutate `task_stage_states`. `de_escalate_task` flips `is_escalated=0` and clears the escalation fields; it also does NOT mutate `task_stage_states`. A task that escalates from `development.in_progress`, then de-escalates, resumes at `development.in_progress` with the same `attempt_count` and `entered_at`. This is a load-bearing invariant — covered by acceptance 5.2.4.
- **No new agents — but placeholder shims are in scope.** Agents for `expansion_qa`, `code_review_qa`, `holistic_qa`, `merge` already exist as bundled YAMLs and only need rewiring against new stage names. Four discovery-stage agents have no surviving YAML or follow-up task after the #12725 cascade-delete (stage → agent slug mapping: `ideation → analyst`, `research → researcher`, `architecture → architect`, `prd → product-manager`), and `pr` is owned by #13552 (already open). This epic ships **disabled placeholder YAMLs** for the four missing discovery agents (clearly marked as such) and creates a parent epic plus four tracking tasks for the real implementation work. Real agent behavior remains out of scope.
- **Single project.** No cross-project / multi-tenant kanban work.
- **`escalated` is preserved** as the human-in-the-loop flag — promoted from a `status` value to first-class `is_escalated` column. Every other active `status` value is subsumed by per-stage tri-state.
- **Readiness/blocking semantics stay equivalent.** `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and task `state.is_blocked` must return the same results as the old model for equivalent fixtures after cutover.
- **Schema baseline before this epic = 232** (`BASELINE_VERSION = 220` at `src/gobby/storage/migrations.py:65`, plus the 12 in-tree migrations 221–232 already populated with unrelated work). New migrations for this epic begin at **233**.
- **No explicit test tasks anywhere in this plan.** TDD sandwiches are auto-inserted by `/gobby expand` for every `category: code` and `category: config` task.

## P1 Registry + Manifest Schema
`kind: framing`

**Goal**: Land the three new tables, the bundled stages YAML, the artifact-column extensions, and the four discovery-agent placeholder YAMLs. After Phase 1, the database can store a stage manifest, the registry seeds itself on startup, and the registry's `default_agent` slot resolves to a real (if disabled) bundled agent for every stage.

### 1.1 Schema migration + bundled stages.yaml: registry, defaults, manifest, and PR/merge artifact columns [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations.py`, `src/gobby/install/shared/registry/stages.yaml` (new — authored here so the migration's inline seed has its source on disk)

Add migration version `233` to the `MIGRATIONS` list. Migration adds three new tables and three new TEXT columns to `task_artifacts`, plus one new column on `tasks`, AND seeds the 14 canonical `task_stages_registry` rows plus the six `task_type_default_stages` bundles inline within the same transaction. Use `db.transaction()` around all schema changes and the inline seed; follow the existing `_add_task_artifact_evidence_columns` pattern (`src/gobby/storage/migrations.py:111-159`) for the artifact-column additions.

**Bundled `stages.yaml` lands in this deliverable (load-bearing for F1 sequencing).** This deliverable owns BOTH the migration code AND the bundled YAML file the migration reads. An expanded worker receiving this leaf authors `src/gobby/install/shared/registry/stages.yaml` and `src/gobby/storage/migrations.py` together; the migration cannot land without the YAML, and the YAML has no other consumer until the loader in §1.2. §1.2 retains ownership of `StageRegistryLoader` (the loader/hash-drift detector) and the daemon startup wiring; §1.2 does NOT (re-)author the YAML — it points at the file landed here. The full YAML schema and the 14 stage entries (verbatim) are documented in §1.2 below; the implementing agent for §1.1 copies the YAML body from §1.2's documentation block into the file at the path above as part of this deliverable.

**Inline registry + default-stages seed (load-bearing for migration ordering):** Migration 233 reads the bundled `src/gobby/install/shared/registry/stages.yaml` (authored in this same deliverable per the paragraph above) directly via `pathlib.Path(__file__).parent.parent / 'install/shared/registry/stages.yaml'` and inserts all 14 registry rows + the six `task_type_default_stages` bundles in the SAME transaction as the schema creation. This ensures the FK targets exist before migration 234 (Phase 2.2 backfill) writes any `task_type_default_stages` lookups or `task_stage_states.stage_name` references. The startup `StageRegistryLoader.sync()` (Phase 1.2) becomes a hash-drift detector for subsequent edits to the bundled YAML — it is NOT the only seed path. The six `task_type_default_stages` bundles seeded here are the same six bundles documented in §2.2 (`epic`, `feature`, `bug`, `refactor`, `chore`, `task` — five distinct manifests; `chore` and `task` share the leaves-only manifest). On a fresh DB, migration 233 writes registry + defaults in one transaction; migration 234 then reads them when backfilling `task_stage_states` from `(lifecycle, status, labels)` for existing tasks.

**On `category`** (called out because the field can read like decoration): the five values `discovery | design | verification | implementation | delivery` come from the strategy plan and have one functional consumer in this epic — the kanban category filter wired in Phase 6.1 (6.1.7). The dispatcher does NOT read `category`; rule routing is purely by `stage_name` and registry `position_hint`. If Phase 6.1's filter is later removed, this column should be dropped in the same change. Do not add other consumers without revisiting that decision.

Tables to create (each guarded by `IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS task_stages_registry (
    name TEXT PRIMARY KEY,
    display_label TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('discovery','design','verification','implementation','delivery')),  -- drives kanban category filter (Phase 6.1 6.1.7); not used by the dispatcher

    default_agent TEXT,
    position_hint INTEGER NOT NULL,
    requires_human INTEGER NOT NULL DEFAULT 0,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    bundled_hash TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_type_default_stages (
    task_type TEXT NOT NULL,
    stage_name TEXT NOT NULL REFERENCES task_stages_registry(name) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (task_type, stage_name)
);
CREATE INDEX IF NOT EXISTS idx_task_type_default_stages_position
    ON task_type_default_stages (task_type, position);

CREATE TABLE IF NOT EXISTS task_stage_states (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL REFERENCES task_stages_registry(name) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready','in_progress','done')),  -- see Constraints: blocking/escalation are orthogonal projections, not stage-state values
    entered_at TEXT,
    entered_by_session_id TEXT,
    completed_at TEXT,
    completed_by_session_id TEXT,
    completed_commit_sha TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    artifact_refs TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, stage_name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_stage_states_position
    ON task_stage_states (task_id, position);
CREATE INDEX IF NOT EXISTS idx_task_stage_states_state
    ON task_stage_states (stage_name, state);
CREATE INDEX IF NOT EXISTS idx_task_stage_states_open
    ON task_stage_states (task_id, position) WHERE state != 'done';
```

`artifact_refs` is a JSON-encoded object (`json.dumps`) of pointers into `task_artifacts` (e.g. `{"plan_file": "plan_file_path", "expansion_run": "expansion_run_id"}`). The unique `(task_id, position)` index enforces the position-uniqueness invariant per task. The partial index `idx_task_stage_states_open` accelerates the "leftmost non-done" current-stage projection.

Columns to add to `task_artifacts` (mirror `_add_task_artifact_evidence_columns` rebuild pattern: rename old → create new with allowlisted columns → INSERT SELECT → drop old):

- `pr_review_report TEXT`
- `structured_pr_verdict TEXT` (JSON-encoded)
- `merge_campaign_report TEXT`

`max_merge_attempts INTEGER` already exists nullable in the baseline schema (added pre-epic by `_add_task_artifact_retry_cap_columns`); Phase 4.2 `record_merge_result` reads it as `coalesce(max_merge_attempts, 3)` so the existing nullable column suffices without a column-tightening migration.

Update `_default_task_artifact_column` (`src/gobby/storage/migrations.py:177-182`) to include defaults for the three new TEXT columns (`NULL`). Update `_task_artifacts_create_sql` (`src/gobby/storage/migrations.py:75-108`) to include the three new TEXT columns so fresh installs match.

Column to add to `tasks` (separate `ALTER TABLE tasks ADD COLUMN`; SQLite tolerates this as a no-rebuild change):

- `is_escalated INTEGER NOT NULL DEFAULT 0` — first-class human-in-the-loop flag promoted from `status='escalated'`. Created here at default 0; backfilled from `escalated_at IS NOT NULL` in migration 234 (Phase 2.2). Placement on `tasks` (not `task_artifacts`) is load-bearing: escalation is task-level state read on every list, while `task_artifacts` is sparse evidence. Phase 5.2 wires `Task` dataclass and reader call sites; no migration is needed in 5.2.

**Acceptance:**

- 1.1.1 — Migration version 233 exists in `MIGRATIONS`. file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations.MIGRATIONS`.
- 1.1.2 — Three new tables created with declared schema, CHECK constraints, indexes, and partial index on open rows. test: `tests/storage/test_migration_233.py::test_creates_registry_tables`.
- 1.1.3 — `task_artifacts` gains three new TEXT columns (`pr_review_report`, `structured_pr_verdict`, `merge_campaign_report`) with `NULL` defaults; rebuild path preserves existing rows. test: `tests/storage/test_migration_233.py::test_artifact_columns_added`.
- 1.1.4 — Fresh-install `_task_artifacts_create_sql` includes the new TEXT columns so a blank DB skips the rebuild path. behavior: "fresh install schema matches migration end state" verified in `tests/storage/test_migration_233.py::test_fresh_install_matches`.
- 1.1.5 — `tasks` table gains `is_escalated INTEGER NOT NULL DEFAULT 0`; existing rows default to 0 with backfill deferred to migration 234 (Phase 2.2). test: `tests/storage/test_migration_233.py::test_tasks_is_escalated_added`.
- 1.1.6 — Migration 233 seeds all 14 `task_stages_registry` rows from the bundled `src/gobby/install/shared/registry/stages.yaml` inline in the same transaction as the schema creation; on a fresh DB the table contains exactly 14 rows after migration 233 runs and before migration 234 starts. test: `tests/storage/test_migration_233.py::test_registry_seeded_inline`.
- 1.1.6a — `src/gobby/install/shared/registry/stages.yaml` exists on disk as part of this deliverable, declaring all 14 stages (`ideation, research, architecture, prd, planning, adversarial_review, test_arch, expansion, expansion_qa, development, code_review_qa, holistic_qa, pr, merge`) with every required field set per the §1.2 YAML schema (`name`, `display_label`, `description`, `category`, `position_hint`; `default_agent`, `requires_human`, `is_terminal` where applicable). The migration's inline seed reads this exact file. file: `src/gobby/install/shared/registry/stages.yaml`. test: `tests/storage/test_migration_233.py::test_bundled_stages_yaml_present_with_14_stages`.
- 1.1.7 — Migration 233 seeds `task_type_default_stages` with six rows (`epic`, `feature`, `bug`, `refactor`, `chore`, `task`) across five distinct manifests inline in the same transaction; migration 234 (Phase 2.2 backfill) finds the rows already present when resolving per-task manifests. test: `tests/storage/test_migration_233.py::test_default_stages_seeded_inline`.
- 1.1.8 — On a fresh DB, the migration runner reaches version 234 with the registry table populated; FK references from `task_type_default_stages.stage_name` and `task_stage_states.stage_name` resolve cleanly because migration 233 seeded the parent rows in the same transaction. test: `tests/storage/test_migration_233.py::test_fresh_db_fk_resolution_into_234`.

### 1.2 Sync loader for the bundled stages.yaml [category: config] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_stage_registry_loader.py` (new), `src/gobby/runner_init.py` (wiring)

Implement the `StageRegistryLoader` that reads the bundled YAML landed by §1.1 (`src/gobby/install/shared/registry/stages.yaml`) and upserts on hash drift. Mirror the bundled-template pattern from `src/gobby/install/shared/{rules,workflows,agents}` — the file is hashed at startup, drift triggers an upsert, user overrides are detected by hash comparison.

**This deliverable does NOT author or modify `stages.yaml`** — that file is owned by §1.1 (per the F1 sequencing fix). §1.2 only adds the loader code and the daemon startup wiring, and assumes the file already exists at the path documented below. The YAML schema is reproduced here for reference (matching the file contents §1.1 lands) so the implementing agent for §1.2 sees the shape it must parse.

**Sync loader role:** Migration 233 (§1.1) seeds all 14 registry rows + six `task_type_default_stages` bundles INLINE within the schema transaction by reading the bundled YAML file directly. The startup `StageRegistryLoader.sync()` is therefore a hash-drift detector only — it upserts when the bundled YAML hash changes between releases. It is NOT the only seed path; on a fresh DB the registry is populated by migration 233 before the daemon ever calls the loader. This split keeps migration ordering atomic (FK targets exist before any backfill that references them) while still letting bundled-YAML edits propagate without a new migration.

YAML shape:

```yaml
# src/gobby/install/shared/registry/stages.yaml
version: 1
stages:
  - name: ideation
    display_label: Ideation
    description: Early problem framing; capture motivating questions and constraints.
    category: discovery
    default_agent: analyst                    # placeholder shim — Phase 1.3
    position_hint: 10
    requires_human: false
    is_terminal: false
  - name: research
    display_label: Research
    description: Targeted investigation; produce findings consumable by architecture/PRD.
    category: discovery
    default_agent: researcher                 # placeholder shim — Phase 1.3
    position_hint: 20
  - name: architecture
    display_label: Architecture
    description: Cross-cutting design decisions and component shape.
    category: design
    default_agent: architect                  # placeholder shim — Phase 1.3
    position_hint: 30
  - name: prd
    display_label: PRD
    description: Productized requirements; bridges discovery and planning.
    category: design
    default_agent: product-manager            # placeholder shim — Phase 1.3
    position_hint: 40
  - name: planning
    display_label: Planning
    description: Implementation plan authoring (interactive or autonomous).
    category: design
    default_agent: planner
    position_hint: 50
  - name: adversarial_review
    display_label: Adversarial Review
    description: Plan-adversary critiques the plan and emits the typed manifest.
    category: verification
    default_agent: plan-adversary
    position_hint: 60
  - name: test_arch
    display_label: Test Architecture
    description: Test scaffolding and contract test design before expansion.
    category: verification
    default_agent: test-architect
    position_hint: 70
  - name: expansion
    display_label: Expansion
    description: Decompose plan into TDD-wrapped leaf tasks.
    category: implementation
    position_hint: 80
  - name: expansion_qa
    display_label: Expansion QA
    description: Verify the expanded tree against the plan's coverage contract.
    category: verification
    default_agent: expansion-qa
    position_hint: 90
  - name: development
    display_label: Development
    description: Leaf implementation work; drives TDD sandwiches.
    category: implementation
    default_agent: backend-developer          # primary fallback; build-time may override per-task
    position_hint: 100
  - name: code_review_qa
    display_label: Code Review QA
    description: Automated and human code review of leaf changes.
    category: verification
    default_agent: qa-reviewer
    position_hint: 110
  - name: holistic_qa
    display_label: Holistic QA
    description: Whole-epic review after every leaf is parked.
    category: verification
    default_agent: holistic-reviewer
    position_hint: 120
  - name: pr
    display_label: Pull Request
    description: Open/update PR, capture verdict, gate on external review.
    category: delivery
    # default_agent left blank — owned by #13552 (PR/merge skill epic)
    position_hint: 130
  - name: merge
    display_label: Merge
    description: Land approved PR; resolve conflicts; close terminal task.
    category: delivery
    default_agent: merge-orchestrator
    position_hint: 140
    is_terminal: true
```

`default_agent` is populated for every stage with a real or placeholder bundled agent. The four discovery stages point at placeholder shims landed in 1.3; `pr` is left blank because #13552 owns it; `expansion` is left blank because expansion runs as a pipeline action, not an agent spawn.

Sync loader (`src/gobby/storage/tasks/_stage_registry_loader.py`):

```python
class StageRegistryLoader:
    """Sync bundled stages.yaml into task_stages_registry on startup.

    Mirrors the workflow loader's hash-drift detection. Bundled rows are
    upserted whenever the file hash changes; rows whose name is missing
    from the bundled YAML are NOT deleted (operator-added stages are
    permitted but not part of the supported contract).
    """

    BUNDLED_PATH = Path("src/gobby/install/shared/registry/stages.yaml")

    def sync(self, db: DatabaseProtocol) -> StageRegistrySyncResult: ...
    def detect_override(self, db_row: dict, bundled_row: dict) -> bool: ...
```

Wire `StageRegistryLoader().sync(db)` into the daemon startup sequence next to the existing template-sync call `sync_bundled_content_to_db(runner.database)` at `src/gobby/runner_init.py:257-259`. Sync runs after the migration applier so the table exists.

**Acceptance:**

- 1.2.1 — `StageRegistryLoader` parses the bundled `stages.yaml` landed by §1.1 (acceptance 1.1.6a) into a list of `StageRegistryEntry`-shaped records; the loader rejects malformed YAML with a typed error and surfaces missing required fields. The file-existence and 14-stage-completeness invariants are owned by §1.1 acceptance 1.1.6a; this acceptance only verifies the parser. symbol: `gobby.storage.tasks._stage_registry_loader.StageRegistryLoader`. test: `tests/storage/test_stage_registry_loader.py::test_parses_bundled_yaml`, `tests/storage/test_stage_registry_loader.py::test_malformed_yaml_raises`.
- 1.2.2 — `StageRegistryLoader.sync()` is a hash-drift detector that upserts bundled rows when the bundled YAML hash differs from the stored `bundled_hash`. On a fresh DB the registry is already populated by migration 233; the loader's first run observes the seeded rows and is a no-op (no hash drift). symbol: `gobby.storage.tasks._stage_registry_loader.StageRegistryLoader`. test: `tests/storage/test_stage_registry_loader.py::test_sync_no_op_when_hash_matches_seed`, `tests/storage/test_stage_registry_loader.py::test_sync_upserts_on_hash_drift`.
- 1.2.3 — Daemon startup wiring invokes the loader after migrations, adjacent to `sync_bundled_content_to_db(runner.database)` at `src/gobby/runner_init.py:257-259`. file: `src/gobby/runner_init.py`. test: `tests/test_startup_seeds_stage_registry.py::test_registry_populated_after_startup`.
- 1.2.4 — Operator-added stages survive bundled-YAML re-sync; bundled stages get re-upserted. behavior: "user-added stage rows persist across sync" verified in `tests/storage/test_stage_registry_loader.py::test_user_added_stage_preserved`.

### 1.3 Placeholder agent YAMLs for discovery stages [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/analyst.yaml`, `researcher.yaml`, `architect.yaml`, `product-manager.yaml` (all new)

Ship four bundled agent definitions as **disabled placeholders** so the registry's `default_agent` foreign key resolves to a real bundled row even before real agent behavior lands. Each YAML is functionally inert (`enabled: false`) and carries explicit placeholder language at the top so future maintainers cannot mistake it for a working agent.

Stage → agent slug mapping (load-bearing, used in stages.yaml from 1.2):

| Stage | Agent slug |
|-------|-----------|
| `ideation` | `analyst` |
| `research` | `researcher` |
| `architecture` | `architect` |
| `prd` | `product-manager` |

Canonical placeholder template (use verbatim, varying only `name`, `description`, target stage, and the placeholder banner specifics):

```yaml
# src/gobby/install/shared/workflows/agents/analyst.yaml
#
# PLACEHOLDER — disabled stub for the `ideation` stage. Replace with a real
# implementation before enabling. Tracked by the agent-followup task created
# by the stage-manifest-cutover plan (deliverable 5.4); see
# `agent-followup:analyst` task in gobby-tasks for ownership and context.

name: analyst
description: |
  PLACEHOLDER — Disabled stub for the `ideation` stage of the task-stage
  manifest model. This file exists so `task_stages_registry.default_agent`
  resolves to a real bundled row. The real agent must be authored in a
  follow-up plan; see `agent-followup:analyst` task for tracking.

  When this YAML is replaced with a real agent, set `enabled: true`, fill in
  `instructions`, and remove the PLACEHOLDER banners.

version: "0.1"
enabled: false                 # Load-bearing: disabled until real impl lands.
priority: 1                    # Lowest priority so any real agent overrides.
surfaces: [spawn]
provider: claude
model: haiku
isolation: none

instructions: |
  PLACEHOLDER AGENT — IDEATION STAGE

  You are a placeholder stub. The real `analyst` agent has not been
  implemented yet. If a dispatcher accidentally enabled this YAML and spawned
  you, your job is to immediately escalate the task with a clear reason so
  the operator can investigate.

  Action: call escalate_task(task_id=<your task>, reason="placeholder_agent:analyst:not_implemented")
  and exit. Do not attempt to do ideation work.
```

The other three follow the same pattern with stage-appropriate name/description/escalation reason:
- `researcher.yaml` for `research` (escalation reason: `placeholder_agent:researcher:not_implemented`)
- `architect.yaml` for `architecture` (escalation reason: `placeholder_agent:architect:not_implemented`)
- `product-manager.yaml` for `prd` (escalation reason: `placeholder_agent:product-manager:not_implemented`)

Because every placeholder is `enabled: false`, the bundled-template sync will install the row but not register the agent for spawning. The dispatcher's `_has_<stage>_agent(context)` check (Phase 4.1, 4.2) returns `False` for disabled agents and surfaces the existing `<stage>_no_agent` escalation, surfacing the gap loudly rather than silently doing nothing.

CLAUDE.md retired-agent allowlist must NOT block these names. Verify nothing in `src/gobby/workflows/loader.py` or template sync logic soft-deletes them; if a retired-name pattern matches, exempt the placeholders explicitly (the four slugs are not in the retired list, but confirm during implementation).

**Acceptance:**

- 1.3.1 — Four YAML files exist with declared `name`, `enabled: false`, `priority: 1`, and PLACEHOLDER banners. file: `src/gobby/install/shared/workflows/agents/analyst.yaml`, `researcher.yaml`, `architect.yaml`, `product-manager.yaml`.
- 1.3.2 — Each file's `instructions` block tells the agent to escalate with reason `placeholder_agent:<slug>:not_implemented` if accidentally spawned. test: `tests/agents/test_placeholder_agents.py::test_each_placeholder_escalates_on_spawn`.
- 1.3.3 — Bundled-template sync installs the rows with `enabled: false`. test: `tests/agents/test_placeholder_agents.py::test_sync_installs_disabled`.
- 1.3.4 — `task_stages_registry.default_agent` foreign-key resolves for all four discovery stages after Phase 1.2 sync. test: `tests/storage/tasks/test_stage_registry_default_agent_fk.py::test_discovery_stage_default_agents_resolve`.
- 1.3.5 — Dispatcher's missing-agent check treats `enabled: false` as missing and escalates with the stage-specific `<stage>_no_agent` reason rather than spawning the placeholder. The actual escalation is emitted by `disabled_agent_escalation_rule` (Phase 3.1, acceptance 3.1.23), which fires on `current_stage.state == 'ready'` AND `default_agent` missing/disabled. test: `tests/dispatch/test_no_agent_paths.py::test_disabled_placeholder_treated_as_missing`, `tests/dispatch/test_no_agent_paths.py::test_disabled_placeholder_routes_to_disabled_agent_escalation_rule`.

Per-row coverage (one acceptance per data row of the §1.3 Stage|Agent-slug table, per the plan-coverage contract's table-row decomposition rule):

- 1.3.6 — Stage `ideation` → agent slug `analyst`: file `src/gobby/install/shared/workflows/agents/analyst.yaml` exists with `name: analyst`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:analyst:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `ideation` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/analyst.yaml`. test: `tests/agents/test_placeholder_agents.py::test_analyst_placeholder_for_ideation_stage`.
- 1.3.7 — Stage `research` → agent slug `researcher`: file `src/gobby/install/shared/workflows/agents/researcher.yaml` exists with `name: researcher`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:researcher:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `research` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/researcher.yaml`. test: `tests/agents/test_placeholder_agents.py::test_researcher_placeholder_for_research_stage`.
- 1.3.8 — Stage `architecture` → agent slug `architect`: file `src/gobby/install/shared/workflows/agents/architect.yaml` exists with `name: architect`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:architect:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `architecture` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/architect.yaml`. test: `tests/agents/test_placeholder_agents.py::test_architect_placeholder_for_architecture_stage`.
- 1.3.9 — Stage `prd` → agent slug `product-manager`: file `src/gobby/install/shared/workflows/agents/product-manager.yaml` exists with `name: product-manager`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:product-manager:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `prd` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/product-manager.yaml`. test: `tests/agents/test_placeholder_agents.py::test_product_manager_placeholder_for_prd_stage`.

## P2 Stage-Native Storage + API Surface
`kind: framing`

**Goal**: Land the storage managers, the migration script that backfills `task_stage_states` from `(lifecycle, status, labels)`, and the MCP/HTTP/CLI surfaces. After Phase 2, every read and write of stage state goes through the new APIs; the dispatcher still uses the old code (Phase 3 swaps it).

### 2.1 Stage registry + stage states storage managers [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_stage_registry.py`, `src/gobby/storage/tasks/_stage_states.py` (both new)

Two new manager modules under the same package as `_artifacts.py`, `_lifecycle_events.py`, `_dispatch_mutex.py`. Wire both into `LocalTaskManager` (`src/gobby/storage/tasks/_manager.py`) as composed sub-managers, mirroring how `TaskArtifactManager` is exposed.

`_stage_registry.py`:

```python
@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: Literal["discovery","design","verification","implementation","delivery"]
    default_agent: str | None
    position_hint: int
    requires_human: bool
    is_terminal: bool


class StageRegistryManager:
    def __init__(self, db: DatabaseProtocol) -> None: ...

    def list_all(self) -> list[StageRegistryEntry]: ...
    def get(self, name: str) -> StageRegistryEntry | None: ...
    def upsert(self, entry: StageRegistryEntry, *, bundled_hash: str | None = None) -> None: ...
    def list_default_stages(self, task_type: str) -> list[tuple[str, int]]:
        """Return [(stage_name, position)] sorted by position from task_type_default_stages."""
    def set_default_stages(self, task_type: str, stages: Sequence[tuple[str, int]]) -> None: ...
```

`_stage_states.py`:

```python
@dataclass(frozen=True, slots=True)
class StageState:
    task_id: str
    stage_name: str
    position: int
    state: Literal["ready","in_progress","done"]
    entered_at: str | None
    entered_by_session_id: str | None
    completed_at: str | None
    completed_by_session_id: str | None
    completed_commit_sha: str | None
    attempt_count: int
    artifact_refs: dict[str, str] | None
    notes: str | None
    updated_at: str  # ISO-8601 UTC; surfaces the DB column added in migration 233 — load-bearing for the §3.3 RuntimeDispatchMutex stale-candidate snapshot check.


class StageStatesManager:
    def __init__(self, db: DatabaseProtocol, events: TaskLifecycleEventManager) -> None: ...

    # Reads
    def list_for_task(self, task_id: str) -> list[StageState]:
        """Sorted by position. Empty list if task has no manifest yet."""
    def get(self, task_id: str, stage_name: str) -> StageState | None: ...
    def current_stage(self, task_id: str) -> StageState | None:
        """Leftmost row by position whose state != 'done'. None if all done or no manifest."""
    def list_tasks_at_stage(
        self, *, stage_name: str, state: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        """Drives kanban column queries."""

    # Writes — every mutator records a task_lifecycle_events row
    def initialize_manifest(
        self, task_id: str, stages: Sequence[tuple[str, int]], *, by_session_id: str | None,
    ) -> list[StageState]:
        """Insert manifest rows; all start at ready. Idempotent only if the
        target manifest matches existing rows exactly; otherwise raises
        ManifestAlreadyInitializedError."""

    def add_stage(
        self, task_id: str, stage_name: str, position: int, *, by_session_id: str | None,
    ) -> StageState:
        """Insert a row. Reorders affected positions. Errors if stage_name not in registry
        or task already has the stage."""

    def remove_stage(
        self, task_id: str, stage_name: str, *, by_session_id: str | None,
    ) -> None:
        """Delete a row; reorder positions to remain dense (1..N)."""

    def start_stage(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, notes: str | None = None,
    ) -> StageState:
        """Transition ready → in_progress. Only allowed when this row's position
        equals current_stage().position (no skipping)."""

    def complete_stage(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, commit_sha: str | None = None,
        artifact_updates: Mapping[str, str] | None = None,
    ) -> StageState:
        """Transition in_progress → done. Persists commit_sha + artifact_refs.

        Terminal-close contract (invariant 8 / acceptance 2.1.9 / 2.1.10):
        when this row is the highest-position row in the task's manifest
        (i.e. completing it leaves `current_stage(task) is None`),
        `complete_stage` ALSO closes the task atomically in the same DB
        transaction by calling the transaction-aware private helper
        `_close_task_in_txn(conn, task_id, reason='manifest_exhausted',
        commit_sha=commit_sha, closed_at=now,
        closed_in_session_id=by_session_id,
        cascade_descendants=(stage_name == 'merge'))` directly on the
        already-open connection. Cascade=True is passed ONLY when the
        terminal stage is `merge` (replacing the legacy `mark_task_merged`
        cascade); for non-merge terminal task types (research_spike,
        prd_doc, architecture_doc) cascade=False because they have no
        descendants in this epic's scope.

        `complete_stage` does NOT invoke the public `close_task(...)` API.
        Public `close_task` is a thin wrapper around the SAME
        `_close_task_in_txn` helper that always passes
        `cascade_descendants=False`; the two callers share one cascade
        implementation by construction (acceptance 2.1.10). Phase 4.2's
        `record_merge_result` success path reaches `_close_task_in_txn`
        through `complete_stage`'s merge branch (cascade=True), not
        through public `close_task`."""

    def fail_stage(
        self, task_id: str, stage_name: str, *,
        reason: str, needs_human: bool = False, by_session_id: str | None,
    ) -> StageState:
        """Transition in_progress → ready (no attempt_count change — the
        subsequent start_stage is the sole increment site), OR triggers
        escalate_task(needs_human=True). Escalation wiring goes through the
        existing escalate_task helper; do not write is_escalated directly here."""
```

Invariants enforced in `StageStatesManager` (raise `ValueError` or a typed error class on violation; cover with tests):

1. `position` is unique per `task_id` (DB unique index plus pre-flight check for clearer errors).
2. `stage_name` must exist in `task_stages_registry`.
3. Transitions are: `ready → in_progress`, `in_progress → done`, `in_progress → ready` (fail). No skipping. No reverse from `done`.
4. `start_stage` requires the target row to be the current `current_stage()` (leftmost non-done row).
5. `attempt_count` increments only on `start_stage` (replaces `planning-round:N` / `qa-attempts:N` labels). `fail_stage` does NOT increment — a fail-then-restart cycle yields exactly one increment via the subsequent `start_stage`.
6. Every mutator (`initialize_manifest`, `add_stage`, `remove_stage`, `start_stage`, `complete_stage`, `fail_stage`) executes inside `with RuntimeDispatchMutex(storage=<TaskDispatchMutexManager>, task_id=task_id, holder=<session_id_or_"system">, action_kind="stage_state:<stage_name>:<verb>", ttl_seconds=30):` to serialize concurrent transitions across the dispatcher, MCP tools, HTTP routes, and CLI. Import `RuntimeDispatchMutex` from `gobby.dispatch.mutex` (defined at `src/gobby/dispatch/mutex.py:27`); it wraps `TaskDispatchMutexManager.acquire_mutex(task_id, holder, kind, ttl_seconds, ...)` (at `src/gobby/storage/tasks/_dispatch_mutex.py:78`) and releases on exit. Surfaces calling these methods do not need to acquire the mutex themselves — the manager owns that contract.
7. `updated_at` is bumped to `datetime.now(UTC).isoformat()` on every affected `task_stage_states` row by every mutator: `initialize_manifest` (every inserted row), `add_stage` (the inserted row + every row whose position shifts), `remove_stage` (every row whose position shifts), `start_stage`, `complete_stage`, `fail_stage` (the affected row). The DB column carries `DEFAULT (datetime('now'))` per §1.1 schema, but the manager is the canonical writer — fresh DB-default values from new rows must be overwritten with the Python-computed timestamp on the same write so callers (`StageState.updated_at` consumers, the §3.3 mutex snapshot) see a consistent ISO-8601 string. This invariant is load-bearing for the §3.3 mutex stale-candidate check: a same-state-result transition (e.g., `start_stage` then `fail_stage` returning `in_progress → ready`) must still produce a fresh `updated_at` so the mutex snapshot sees a different value and rejects a stale candidate scanned just before the cycle.
8. **Terminal-close on manifest exhaustion.** `complete_stage(task_id, stage_name)` MUST also close the task when the just-completed row is the highest-position row in the task's manifest (i.e., after the transition `current_stage(task) is None`). The close runs in the SAME DB transaction as the stage UPDATE.

    **Closure source.** `tasks.closed_at IS NOT NULL` is the canonical SQL closure predicate (`src/gobby/storage/baseline_schema.sql:265-275`); there is no `tasks.is_closed` column. `Task.is_closed` is a read-only Python projection sourced from `is_task_closed(task)` at `src/gobby/tasks/state_semantics.py:88-95` (returns `closed_at IS NOT NULL OR status == 'closed'`). Every SQL write below targets `closed_at`; every SQL filter uses `closed_at IS NULL` / `closed_at IS NOT NULL`. Python read-sites (predicates, dispatcher, projections) read `Task.is_closed`.

    **Transaction composition.** Two surfaces must compose without ambiguity: `complete_stage` (opens its own transaction for the stage UPDATE) and the existing `close_task` public API (opens its own transaction; carries open-child checks, bootstrap-ledger validation, and `force` / `validation_override_reason` semantics). The existing cascade-close behavior is NOT in `close_task` itself — it lives in `mark_task_merged` via `advance_lifecycle(..., cascade_close=True)` / `_cascade_merged_close`. Resolution: extract close+cascade into a NEW transaction-aware private helper that runs on a supplied connection without opening or committing, with explicit parameters for every behavior the two callers need:

    ```python
    def _close_task_in_txn(
        conn,
        task_id: str,
        *,
        reason: str,
        commit_sha: str | None = None,
        closed_at: str,                          # ISO-8601 UTC
        closed_in_session_id: str | None = None,
        force: bool = False,                     # bypass open-child / validation checks
        cascade_descendants: bool = False,       # invoke _cascade_merged_close-style descendant close
        validation_override_reason: str | None = None,
    ) -> None: ...
    ```

    Open-child checks and bootstrap-ledger validation run INSIDE the helper (migrated from `close_task`'s current wrapper) so both callers go through the same gate; the public-API wrapper's only added behavior is opening `db.transaction()`. The public `close_task(task_id, ...)` becomes:

    ```python
    def close_task(task_id, *, reason, commit_sha=None, closed_in_session_id=None,
                   force=False, validation_override_reason=None) -> None:
        with db.transaction() as conn:
            _close_task_in_txn(
                conn, task_id,
                reason=reason,
                commit_sha=commit_sha,
                closed_at=datetime.now(UTC).isoformat(),
                closed_in_session_id=closed_in_session_id,
                force=force,
                cascade_descendants=False,       # public close NEVER cascades by default
                validation_override_reason=validation_override_reason,
            )
    ```

    `complete_stage`'s terminal-close body is:

    ```python
    with db.transaction() as conn:
        conn.execute("UPDATE task_stage_states SET state='done', ... WHERE ...")
        if _is_highest_position_row(conn, task_id, stage_name):
            _close_task_in_txn(
                conn, task_id,
                reason="manifest_exhausted",
                commit_sha=commit_sha,
                closed_at=datetime.now(UTC).isoformat(),
                closed_in_session_id=by_session_id,
                cascade_descendants=(stage_name == "merge"),  # ONLY merge-terminal cascades
            )
    # On helper raise, outer `with` already rolled back the entire transaction.
    ```

    `cascade_descendants=True` is passed ONLY when the just-completed stage is `merge` (replacing the legacy `mark_task_merged` cascade behavior). For non-merge terminal stages (`prd` for `research_spike` / `prd_doc`, `architecture` for `architecture_doc`), cascade is False — those task types have no descendants under this epic's scope. No nested-transaction or savepoint semantics are required because the helper does not open its own. Both `close_task` and `complete_stage`'s terminal branch share one cascade-aware implementation by construction.

    **Close failure → escalate idempotently, no auto-retry.** If `_close_task_in_txn` raises (e.g., parent-blocker constraint, cascade-close validation, DB constraint), the outer `with db.transaction()` rolls back: the stage row reverts to `in_progress` and the task is NOT closed. Implementations MUST NOT swallow close failures. After rollback, in a SEPARATE committed transaction, `complete_stage` calls a new private helper:

    ```python
    def _emit_terminal_close_failed_escalation(
        task_id: str, *, stage_name: str, error: Exception,
    ) -> None:
        """Idempotent terminal-close-failure escalation. Runs in its own
        transaction. Returns success without writing if the task is already
        escalated. Otherwise writes via the existing `escalate_task` write
        path with reason='terminal_close_failed:<stage>:<error_type>' and
        the supported signature only — NO unsupported kwargs. If the
        escalation write itself raises, logs at ERROR level and re-raises;
        DB-write failure is the only documented uncapped failure mode (an
        operational concern surfaced via logging, not a logic bug)."""
        if is_task_escalated(load_task(task_id)):
            return  # idempotent: prior failure already escalated
        try:
            escalate_task(
                task_id,
                reason=f"terminal_close_failed:{stage_name}:{type(error).__name__}",
            )
        except Exception as escalation_error:
            logger.error(
                "Terminal-close-failed escalation write failed for task %s "
                "(original error: %s; escalation error: %s)",
                task_id, error, escalation_error, exc_info=True,
            )
            raise
    ```

    The helper uses ONLY the existing `escalate_task` signature (no `detail` kwarg). Idempotency means a second `complete_stage` attempt on an already-escalated task does NOT re-write escalation state — important because the original exception is then re-raised to the caller, who may re-invoke `complete_stage` after operator intervention. After successful escalation, `complete_stage` re-raises the original close-error to the caller.

    Because §3.2's `list_automation_candidates` filters `NOT is_escalated`, the dispatcher will NOT re-spawn an agent on the next heartbeat — the escalation IS the retry cap. There is no separate retry counter; an operator must `de_escalate_task` before another `complete_stage` attempt is possible. This breaks the "rollback → next heartbeat → spawn agent → retry close → fails again" loop the F2 finding identified.

    **Uncapped failure mode.** If `_emit_terminal_close_failed_escalation` itself raises (database-write failure during the second transaction), the task is left at `(stage = in_progress, closed_at IS NULL, escalated_at IS NULL)` and the next heartbeat may re-attempt close. This is a documented operational concern, not a logic bug — DB-write failure during escalation is the same failure mode any other write might hit. The error is logged at ERROR level so operators can surface it via the daemon log; full-system DB unavailability is outside this contract's scope.

    **Atomicity guarantee.** A candidate scan can never observe `current_stage IS NULL AND closed_at IS NULL` for a task that has reached invariant 8 via `complete_stage`: either the transaction commits (both stage `done` and `closed_at IS NOT NULL`) or it rolls back (stage `in_progress`, `closed_at IS NULL`, escalation flagged in the follow-up transaction). The only window where `current_stage IS NULL AND closed_at IS NULL` can briefly exist is the §2.2 migration-234 backfill itself, before the acceptance 2.2.31 close-pass commits — a single bounded transaction per database lifetime. The §3.1 `is_child_parked` predicate's `current_stage is None AND NOT child.is_closed` branch (Python read of the projection) is therefore reachable ONLY in that migration window or in synthetic test fixtures that bypass `complete_stage`.

    **Close-path reuse.** For merge-terminal task types (e.g., `feature` ending at `merge`), this is the same close path Phase 4.2's `record_merge_result` success branch delegates to (`commit_sha = merge_sha`). For non-merge terminal task types (`research_spike` ending at `prd`, `prd_doc` ending at `prd`, `architecture_doc` ending at `architecture`), this is the SOLE close path — there is no separate dispatcher rule. The cascade-close behavior of the legacy `mark_task_merged` is preserved by routing through `_close_task_in_txn`.

Every mutator emits a `task_lifecycle_events` row via the injected `TaskLifecycleEventManager`. `from_state` is `f"{stage_name}:{prev_state}"`, `to_state` is `f"{stage_name}:{new_state}"`, `reason` is the caller-supplied reason or a derived one (e.g. `"start_stage:planning"`), `by_actor` is `by_session_id` or `"system"`.

**Acceptance:**

- 2.1.1 — `_stage_registry.py` provides `StageRegistryManager` with the listed read/write methods. file: `src/gobby/storage/tasks/_stage_registry.py`. symbol: `gobby.storage.tasks._stage_registry.StageRegistryManager`.
- 2.1.2 — `_stage_states.py` provides `StageStatesManager` with the listed reads, writes, and invariants. symbol: `gobby.storage.tasks._stage_states.StageStatesManager`. test: `tests/storage/tasks/test_stage_states.py::test_position_uniqueness_enforced`.
- 2.1.3 — Every mutator emits a `task_lifecycle_events` row with the documented `from_state`/`to_state` shape. test: `tests/storage/tasks/test_stage_states.py::test_transitions_emit_events`.
- 2.1.4 — `LocalTaskManager` exposes both managers as `.stages_registry` and `.stage_states`. file: `src/gobby/storage/tasks/_manager.py`. test: `tests/storage/tasks/test_manager_exposes_stage_managers.py::test_managers_accessible`.
- 2.1.5 — Forbidden transitions (skipping, going backwards from done) raise typed errors. test: `tests/storage/tasks/test_stage_states.py::test_invalid_transitions_raise`.
- 2.1.6 — Concurrent `start_stage` calls on the same task serialize via `RuntimeDispatchMutex` (backed by `TaskDispatchMutexManager.acquire_mutex` against the `task_dispatch_mutex` table); only one wins and increments `attempt_count`, the second observes post-mutex state and either errors (row already `in_progress`) or no-ops. Same contract for `complete_stage`, `fail_stage`, and structural mutators (`initialize_manifest`, `add_stage`, `remove_stage`). test: `tests/storage/tasks/test_stage_states_concurrency.py::test_mutex_serializes_writes`.
- 2.1.7 — Attempt-count contract is explicit and uniform across all fail paths: `start_stage` is the SOLE increment site for `attempt_count`; `fail_stage` transitions `in_progress → ready` with NO attempt-count change. Cap escalation uses the `attempt_count >= cap` predicate evaluated inside `fail_stage` (the just-failed attempt's started count is compared against the cap). A `start → fail → start → fail` cycle yields `attempt_count = 2` after the second start, not 4. PR-rejection path (Phase 4.1), merge-failure path (Phase 4.2), and code-review/holistic-review rejection paths all use this single contract — no path adds `+1` outside `start_stage`. behavior: "fail_stage does not change attempt_count; cap predicate is `attempt_count >= cap`" verified in `tests/storage/tasks/test_stage_states.py::test_fail_does_not_increment`, `tests/storage/tasks/test_stage_states.py::test_cap_predicate_is_gte`.
- 2.1.8 — `StageState.updated_at` field is populated on every read (sourced from the DB column added in §1.1) and bumped by every mutator: `start_stage`, `complete_stage`, `fail_stage`, `initialize_manifest`, `add_stage`, `remove_stage`. A `start_stage` followed by a `fail_stage` (a same-state-result cycle that returns `in_progress → ready`) produces two strictly-increasing `updated_at` values on the affected row — the second value is strictly greater than the first by at least the timestamp resolution of `datetime.now(UTC).isoformat()`. This is the load-bearing invariant for the §3.3 `RuntimeDispatchMutex` snapshot: a candidate scanned at `(name, ready, T0)` whose row cycles `ready → in_progress → ready` (different transient state, same final state) must produce `(name, ready, T1)` with `T1 != T0` so the snapshot mismatches and the dispatch is correctly aborted. test: `tests/storage/tasks/test_stage_states.py::test_updated_at_bumped_on_every_mutator`, `tests/storage/tasks/test_stage_states.py::test_same_state_cycle_bumps_updated_at`.
- 2.1.9 — Terminal-close on manifest exhaustion (invariant 8): `complete_stage(task_id, stage_name)` calls the transaction-aware private helper `_close_task_in_txn(conn, task_id, reason='manifest_exhausted', commit_sha=commit_sha, closed_at=now, closed_in_session_id=by_session_id, cascade_descendants=(stage_name == 'merge'))` inside the same `db.transaction()` as the stage UPDATE iff the just-completed row is the highest-position row in the task's manifest (post-transition `current_stage(task) is None`). The public `close_task(...)` API delegates to the same helper with `cascade_descendants=False`. The close path is the SOLE terminal close for non-merge-terminal task types (`research_spike`, `prd_doc`, `architecture_doc`) AND the cascade-aware close for merge-terminal types (Phase 4.2 `record_merge_result` success branch delegates here with `commit_sha = merge_sha`, cascade=True). Close failure rolls the ENTIRE transaction back (stage reverts to `in_progress`, `closed_at` stays NULL) and then calls `_emit_terminal_close_failed_escalation(task_id, stage_name=stage_name, error=error)` in a SEPARATE committed transaction; the helper is idempotent (no-op when already escalated) and uses ONLY the existing `escalate_task` signature with `reason=f'terminal_close_failed:{stage_name}:{type(error).__name__}'`. The original close-error re-raises. Because §3.2's `list_automation_candidates` filters `NOT is_escalated`, the dispatcher does not re-spawn agents to retry — the escalation IS the cap. behavior: "completing the highest-position manifest row closes the task atomically with reason='manifest_exhausted'" verified in `tests/storage/tasks/test_stage_states.py::test_complete_terminal_row_closes_task`, `tests/storage/tasks/test_stage_states.py::test_complete_non_terminal_row_does_not_close`, `tests/storage/tasks/test_stage_states.py::test_close_failure_rolls_back_stage_transition`, `tests/storage/tasks/test_stage_states.py::test_close_failure_escalates_with_terminal_close_failed_reason`, `tests/storage/tasks/test_stage_states.py::test_close_failure_escalates_idempotently_on_already_escalated`, `tests/storage/tasks/test_stage_states.py::test_escalation_helper_uses_supported_signature_only`, `tests/storage/tasks/test_stage_states.py::test_escalation_helper_db_write_failure_logs_and_reraises`, `tests/storage/tasks/test_stage_states.py::test_escalated_task_not_re_attempted_by_heartbeat`, `tests/storage/tasks/test_stage_states.py::test_close_task_public_api_and_complete_stage_share_helper`, `tests/storage/tasks/test_stage_states.py::test_research_spike_closes_at_prd_done`, `tests/storage/tasks/test_stage_states.py::test_merge_terminal_close_via_record_merge_result_uses_same_path`.
- 2.1.10 — `_close_task_in_txn` helper signature and caller-cascade rules: helper accepts `reason`, `commit_sha`, `closed_at`, `closed_in_session_id`, `force`, `cascade_descendants`, `validation_override_reason` parameters (canonical spelling — matches §2.1 invariant 8 code block, the `complete_stage` docstring, acceptance 2.1.9, and §4.2's narrative; one canonical helper-side keyword `commit_sha`, NOT `closed_commit_sha`). The helper runs open-child checks and bootstrap-ledger validation INSIDE the helper (migrated from current `close_task` wrapper); does NOT open or commit its own transaction. Public `close_task(...)` is a thin wrapper that opens `db.transaction()` and delegates to `_close_task_in_txn(..., commit_sha=<from public arg>, cascade_descendants=False, ...)`; if the existing public API uses a `closed_commit_sha` parameter name, the wrapper maps it to the helper's `commit_sha` at the boundary so the helper-side spelling stays canonical. `complete_stage` passes `cascade_descendants=True` ONLY when `stage_name == 'merge'` (replacing legacy `mark_task_merged` cascade); non-merge terminal stages (`prd`, `architecture`) pass cascade=False because there are no descendants under those task types. symbol: `gobby.storage.tasks._stage_states._close_task_in_txn`. test: `tests/storage/tasks/test_close_task_in_txn.py::test_helper_signature_accepts_all_canonical_params`, `tests/storage/tasks/test_close_task_in_txn.py::test_helper_uses_commit_sha_keyword_not_closed_commit_sha`, `tests/storage/tasks/test_close_task_in_txn.py::test_open_child_check_runs_inside_helper`, `tests/storage/tasks/test_close_task_in_txn.py::test_bootstrap_ledger_validation_runs_inside_helper`, `tests/storage/tasks/test_close_task_in_txn.py::test_close_task_public_api_passes_cascade_false`, `tests/storage/tasks/test_close_task_in_txn.py::test_close_task_public_wrapper_maps_closed_commit_sha_to_commit_sha`, `tests/storage/tasks/test_close_task_in_txn.py::test_complete_stage_merge_terminal_passes_cascade_true`, `tests/storage/tasks/test_close_task_in_txn.py::test_complete_stage_non_merge_terminal_passes_cascade_false`, `tests/storage/tasks/test_close_task_in_txn.py::test_force_and_validation_override_pass_through`.

### 2.2 One-shot backfill: derive `task_stage_states` from existing `(lifecycle, status, labels)` [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (new helper `_backfill_task_stage_states_from_legacy`), invoked from migration version 234.

The backfill runs once during the migration to 234. For every task in `tasks`:

1. Resolve task's manifest from `task_type_default_stages` for that `task_type` minus any `stage-:<name>` skip labels (so existing skip labels are honored exactly once).
2. Walk the resolved manifest in position order and assign `(state, attempt_count)` per the mapping table below, derived from the task's current `(lifecycle, status, labels)`.
3. Populate `entered_at` / `completed_at` from the task's `updated_at` and `created_at` as a coarse approximation; `entered_by_session_id` and `completed_by_session_id` use `claimed_by_session_id` if available, else `closed_in_session_id`, else `NULL`.
4. Populate `attempt_count` from `planning-round:N` and `qa-attempts:N` labels (numeric suffix); fall back to `0`.
5. Drop the `stage-:<name>` skip labels (already encoded as "stage absent from manifest").
6. `UPDATE tasks SET is_escalated = 1 WHERE escalated_at IS NOT NULL` — column was added at default 0 in migration 233; this is the one-shot backfill so projections that read `tasks.is_escalated` (Phase 3.2 readiness rewrite) see correct values from migration 234 onward.

Mapping table (`(lifecycle, status)` → manifest result):

| lifecycle | status | Resulting per-row state |
|-----------|--------|-------------------------|
| `open` | `open` | All manifest rows `ready` |
| `open` | any other | All `ready` (`status` overrides handled below) |
| `plan_review` | `open` or `in_progress` | `planning` row `in_progress`, predecessors `done`, successors `ready` |
| `plan_review` | `needs_review` | `planning` row `done`, `adversarial_review` `in_progress`, predecessors `done`, successors `ready` |
| `plan_review` | `review_approved` | `planning` and `adversarial_review` `done`, successors `ready` |
| `test_arch` | any | `test_arch` row `in_progress`, predecessors `done`, successors `ready` |
| `expanding` | `open` or `in_progress` | `expansion` row `in_progress`, predecessors `done`, successors `ready` |
| `expanding` | `needs_review` | `expansion` row `done`, `expansion_qa` `in_progress`, predecessors `done`, successors `ready` |
| `in_development` | `open` or `in_progress` | `development` row `in_progress`, predecessors `done`, successors `ready` |
| `in_development` | `needs_review` | `development` row `done`, `code_review_qa` `in_progress`, predecessors `done`, successors `ready` |
| `in_development` | `review_approved` | `development` and `code_review_qa` `done`, successors `ready` (leaf-park; epics scan children) |
| `holistic_review` | any non-terminal | `holistic_qa` row `in_progress`, predecessors `done`, successors `ready` |
| `holistic_review` | `review_approved` | `holistic_qa` row `done`, successors `ready` |
| `pr` | `open` | `pr` row `in_progress`, predecessors `done`, `merge` `ready` |
| `pr` | `needs_review` | `pr` row `in_progress` with `pr_url` populated, predecessors `done`, `merge` `ready` |
| `merging` | any non-terminal | `merge` row `in_progress`, predecessors `done` |
| `merged` | `closed` | All rows `done` |

`status='escalated'` translates to `is_escalated=1` on the task row (the column was created on `tasks` in migration 233; step 6 above backfills it); the active stage row stays at whatever the lifecycle component dictates.

`status='closed'` with non-`merged` lifecycle: terminal-close-without-merge case (e.g., abandoned tasks). All rows up to and including the row implied by `lifecycle` are `done`; `closed_at` is already populated on the task itself.

Pre-migration audit: emit a `(lifecycle, status, count)` census to `src/gobby/storage/migrations.py` log output. If the census includes a `(lifecycle, status)` tuple not in the mapping table, fail the migration with a clear message — this forces the operator (or the implementing agent) to extend the table rather than silently produce wrong rows.

`task_type` defaults at migration time. Six task type values are seeded across five distinct manifests; `chore` and `task` share the leaves-only manifest.

| task_type | manifest |
|-----------|----------|
| `epic` | full 14-stage pipeline |
| `feature` | `[planning, adversarial_review, test_arch, expansion, expansion_qa, development, code_review_qa, holistic_qa, pr, merge]` |
| `bug` | `[development, code_review_qa, pr, merge]` |
| `refactor` | `[planning, development, code_review_qa, pr, merge]` |
| `chore` | `[development, pr, merge]` |
| `task` | `[development, pr, merge]` |

These six defaults are seeded inline by migration 233 (Phase 1.1, in the same transaction as the schema creation — see F1 fix). Migration 234 reads them when resolving per-task manifests during the backfill; it does NOT re-write them. Phase 5.1 (migration 235) adds the four new task types (`simple_fix`, `research_spike`, `architecture_doc`, `prd_doc`) to the same `task_type_default_stages` table.

After backfill, drop the `stage-:<name>` labels from every task. Do not drop `planning-round:` or `qa-attempts:` labels in this migration — they're still readable for diagnostics; Phase 7 cleans them up.

**Close-pass for all-`done` manifests (invariant-8 retroactive enforcement).** `tasks.closed_at IS NOT NULL` is the canonical SQL closure predicate (no `is_closed` column exists; `Task.is_closed` is a Python projection at `state_semantics.py:88-95`). After the row inserts and label drops, the migration runs a final SQL pass inside the same migration-234 transaction:

```sql
UPDATE tasks
   SET closed_at = datetime('now'),
       closed_in_session_id = 'migration:234'
 WHERE closed_at IS NULL
   AND EXISTS (SELECT 1 FROM task_stage_states tss WHERE tss.task_id = tasks.id)
   AND NOT EXISTS (
       SELECT 1 FROM task_stage_states tss
        WHERE tss.task_id = tasks.id AND tss.state != 'done'
   );
```

The inner `EXISTS` clause guards against tasks with no manifest rows (defensive — should not exist post-backfill). This pass retroactively enforces §2.1 invariant 8 for pre-epic tasks so the `current_stage IS NULL AND closed_at IS NULL` state never persists past the migration commit. The mapping table's `(merged, closed)` row already implies `closed_at IS NOT NULL` from the legacy close path, so this pass is mainly a safety net for any other path that would emit an all-`done` manifest (manual fixture data, direct DB edits, or future mapping-table extensions that close non-merged tasks). After migration 234 commits, the §3.1 `is_child_parked` predicate's `current_stage is None AND NOT child.is_closed` branch is unreachable in normal operation.

**Acceptance:**

- 2.2.1 — Migration version 234 in `MIGRATIONS` performs the backfill in a single transaction. file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations.MIGRATIONS` (entry 234).
- 2.2.2 — Every observed `(lifecycle, status)` tuple in a fixture DB produces rows matching the mapping table. test: `tests/storage/test_migration_234_backfill.py::test_mapping_exhaustive`.
- 2.2.3 — Unmapped `(lifecycle, status)` tuples cause migration failure with a message naming the offending tuple. test: `tests/storage/test_migration_234_backfill.py::test_unmapped_tuple_fails_loudly`.
- 2.2.4 — `attempt_count` populated from `planning-round:N` and `qa-attempts:N` labels. test: `tests/storage/test_migration_234_backfill.py::test_attempt_count_from_labels`.
- 2.2.5 — Migration 234 reads the six `task_type_default_stages` bundles seeded inline by migration 233 (Phase 1.1 acceptance 1.1.7) when resolving per-task manifests; it does not re-seed the defaults. Both fresh-DB and upgrading-DB paths produce identical resolved manifests for the same `(task_type, labels)` input. test: `tests/storage/test_migration_234_backfill.py::test_uses_233_seeded_defaults`.
- 2.2.6 — `stage-:<name>` labels removed from every task post-backfill. test: `tests/storage/test_migration_234_backfill.py::test_skip_labels_dropped`.
- 2.2.7 — `tasks.is_escalated` backfilled from `escalated_at IS NOT NULL` in migration 234; rows with `status='escalated'` map to `is_escalated=1`, all other rows to 0. test: `tests/storage/test_migration_234_backfill.py::test_is_escalated_backfilled`.

Per-row mapping coverage (one acceptance per `(lifecycle, status)` mapping table data row, per the plan-coverage contract's table-row decomposition rule):

- 2.2.8 — Mapping `(open, open)`: every manifest row `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_open_open`.
- 2.2.9 — Mapping `(open, any-other)`: every manifest row `ready`; `status`-driven overrides handled by subsequent rows. test: `tests/storage/test_migration_234_backfill.py::test_map_open_other`.
- 2.2.10 — Mapping `(plan_review, open|in_progress)`: `planning` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_plan_review_open`.
- 2.2.11 — Mapping `(plan_review, needs_review)`: `planning` `done`, `adversarial_review` `in_progress`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_plan_review_needs_review`.
- 2.2.12 — Mapping `(plan_review, review_approved)`: `planning` and `adversarial_review` `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_plan_review_approved`.
- 2.2.13 — Mapping `(test_arch, any)`: `test_arch` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_test_arch_any`.
- 2.2.14 — Mapping `(expanding, open|in_progress)`: `expansion` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_expanding_open`.
- 2.2.15 — Mapping `(expanding, needs_review)`: `expansion` `done`, `expansion_qa` `in_progress`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_expanding_needs_review`.
- 2.2.16 — Mapping `(in_development, open|in_progress)`: `development` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_in_development_open`.
- 2.2.17 — Mapping `(in_development, needs_review)`: `development` `done`, `code_review_qa` `in_progress`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_in_development_needs_review`.
- 2.2.18 — Mapping `(in_development, review_approved)`: `development` and `code_review_qa` `done`, successors `ready` (leaf-park; epics scan children). test: `tests/storage/test_migration_234_backfill.py::test_map_in_development_approved`.
- 2.2.19 — Mapping `(holistic_review, any-non-terminal)`: `holistic_qa` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_holistic_review_open`.
- 2.2.20 — Mapping `(holistic_review, review_approved)`: `holistic_qa` `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_holistic_review_approved`.
- 2.2.21 — Mapping `(pr, open)`: `pr` `in_progress`, predecessors `done`, `merge` `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_pr_open`.
- 2.2.22 — Mapping `(pr, needs_review)`: `pr` `in_progress` with `pr_url` populated, predecessors `done`, `merge` `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_pr_needs_review`.
- 2.2.23 — Mapping `(merging, any-non-terminal)`: `merge` `in_progress`, predecessors `done`. test: `tests/storage/test_migration_234_backfill.py::test_map_merging`.
- 2.2.24 — Mapping `(merged, closed)`: every manifest row `done`. test: `tests/storage/test_migration_234_backfill.py::test_map_merged_closed`.

Per-task-type default-manifest coverage (one acceptance per data row of the task-type defaults table, per the plan-coverage contract's table-row decomposition rule; these are the manifest bundles seeded by migration 233 inline per F1, validated by 234's resolution path):

- 2.2.25 — Default manifest for `epic`: full 14-stage pipeline (every registry stage in `position_hint` order). test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_epic`.
- 2.2.26 — Default manifest for `feature`: `[planning, adversarial_review, test_arch, expansion, expansion_qa, development, code_review_qa, holistic_qa, pr, merge]`. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_feature`.
- 2.2.27 — Default manifest for `bug`: `[development, code_review_qa, pr, merge]`. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_bug`.
- 2.2.28 — Default manifest for `refactor`: `[planning, development, code_review_qa, pr, merge]`. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_refactor`.
- 2.2.29 — Default manifest for `chore`: `[development, pr, merge]` (leaves-only manifest shared with `task`). test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_chore`.
- 2.2.30 — Default manifest for `task`: `[development, pr, merge]` (leaves-only manifest shared with `chore`). test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_task`.
- 2.2.31 — Backfill close-pass: any task whose post-backfill manifest is all-`done` AND `closed_at IS NULL` is closed in the same migration-234 transaction with `SET closed_at = datetime('now')`, `closed_in_session_id = 'migration:234'`. The SQL filter is `WHERE closed_at IS NULL` (canonical closure predicate; no `is_closed` column exists). After migration 234 commits, no task in the database satisfies `current_stage IS NULL AND closed_at IS NULL`. This retroactively enforces §2.1 invariant 8 for pre-epic tasks; the §3.1 `is_child_parked` predicate's `current_stage is None AND NOT child.is_closed` branch becomes unreachable in normal operation post-migration. test: `tests/storage/test_migration_234_backfill.py::test_close_pass_sets_closed_at_for_all_done_open_tasks`, `tests/storage/test_migration_234_backfill.py::test_close_pass_skips_tasks_with_no_manifest_rows`, `tests/storage/test_migration_234_backfill.py::test_close_pass_does_not_overwrite_existing_closed_at`, `tests/storage/test_migration_234_backfill.py::test_no_stranded_open_exhausted_tasks_post_migration`.

### 2.3 New gobby-tasks MCP tools for stage manifest [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks/_stages.py` (new), registered via `_factory.py` and exposed by `gobby-tasks-ops` as well where mutation is involved.

Add eleven new tools. Each tool has its own subsection — implementing agents see only one subsection at a time, so signatures must be repeated where they appear in dependent tools.

| Tool | Server | Purpose |
|------|--------|---------|
| `get_task_stages(task_id)` | `gobby-tasks` | Return manifest in position order. |
| `list_stages_registry()` | `gobby-tasks` | Return all registry entries. |
| `get_task_type_defaults(task_type)` | `gobby-tasks` | Return the default manifest for a task type. |
| `start_stage(task_id, stage_name, notes?)` | `gobby-tasks-ops` | Transition `ready → in_progress`. |
| `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?)` | `gobby-tasks-ops` | Transition `in_progress → done`. |
| `fail_stage(task_id, stage_name, reason, needs_human?)` | `gobby-tasks-ops` | Transition `in_progress → ready` (no attempt-count change), OR escalate when `attempt_count >= cap`. |
| `add_stage(task_id, stage_name, position)` | `gobby-tasks-ops` | Insert a row mid-manifest. |
| `remove_stage(task_id, stage_name)` | `gobby-tasks-ops` | Delete a row from manifest. |
| `record_pr_verdict(task_id, verdict, findings, report_ref?)` | `gobby-tasks-ops` | Store `structured_pr_verdict` + `pr_review_report` on task_artifacts; advances `pr` stage state per verdict. |
| `record_pr_opened(task_id, pr_url, github_pr_number?)` | `gobby-tasks-ops` | Persist `pr_url` and `github_pr_number` artifacts; does not change `pr` stage state. Idempotent: re-recording the same `pr_url` is a no-op. |
| `record_merge_result(task_id, merge_sha?, report_ref?, failure_reason?)` | `gobby-tasks-ops` | Persist merge outcome and advance/fail merge stage. Phase 2.3 registers the tool with stub semantics (delegates to a NotImplementedError until 4.2 wires the success/failure paths and cascade-close); 4.2 fills the body. |

Each tool delegates to `LocalTaskManager.stages_registry` or `.stage_states`. Schemas for the PR-related tools:

```python
def record_pr_verdict(
    task_id: str,
    verdict: Literal["approved", "rejected", "needs_changes"],
    findings: str,
    report_ref: str | None = None,
) -> dict[str, Any]:
    """Persist structured PR verdict on task_artifacts and advance pr stage.

    On verdict='approved': complete_stage(task_id, 'pr'). On 'rejected' or
    'needs_changes': fail_stage(task_id, 'pr', reason=findings, needs_human=False).
    Stores findings in task_artifacts.pr_review_report and a JSON-encoded
    {verdict, findings, report_ref} in task_artifacts.structured_pr_verdict.
    """


def record_pr_opened(
    task_id: str,
    pr_url: str,
    github_pr_number: int | None = None,
) -> dict[str, Any]:
    """Persist PR metadata on task_artifacts without changing pr stage state.

    The pr stage stays at in_progress; verdict capture happens via
    record_pr_verdict. Idempotent: re-recording the same pr_url is a no-op.
    Writes pr_url and (if provided) github_pr_number into task_artifacts.
    """
```

Block legacy lifecycle merge tools (`mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`) for the duration of this epic by leaving them in place AND surfacing their usage in a deprecation logger; Phase 6 / 7 deletes them. Implementing agents must use stage-native operations only.

**Acceptance:**

- 2.3.1 — Eleven new tools registered, each with `inputSchema`, `outputSchema`, and a real handler (or, for `record_merge_result` only, a stub handler that raises `NotImplementedError("wired in Phase 4.2")` so the tool surface exists at registration time but the stage-mutation path is owned by 4.2). file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. symbol: `gobby.mcp_proxy.tools.tasks._stages`.
- 2.3.2 — Tool registration adds them to the `gobby-tasks` and `gobby-tasks-ops` registries. file: `src/gobby/mcp_proxy/tools/tasks/_factory.py`, `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py`. test: `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::test_tools_visible_in_listing`.
- 2.3.3 — `record_pr_verdict` writes `structured_pr_verdict` and `pr_review_report`, then advances stage. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_completes_stage`.
- 2.3.4 — `start_stage` errors on out-of-order start (skipping ahead). test: `tests/mcp_proxy/tools/tasks/test_stage_tools.py::test_start_stage_skipping_errors`.
- 2.3.5 — `record_pr_opened` writes `pr_url` and (when provided) `github_pr_number` to `task_artifacts` without changing `pr` stage state; re-recording the same `pr_url` is a no-op. file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. test: `tests/mcp_proxy/tools/tasks/test_record_pr_opened.py::test_idempotent`.
- 2.3.6 — `record_merge_result` is registered on `gobby-tasks-ops` with the documented signature and `inputSchema` accepting `task_id` (required), `merge_sha`, `report_ref`, `failure_reason` (all optional). The stub handler raises `NotImplementedError("wired in Phase 4.2")`; tool listing on `gobby-tasks-ops` includes the tool name and schema. Phase 4.2 (acceptances 4.2.2 and 4.2.3) replaces the stub with the success/failure paths and cascade-close. file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result_stub.py::test_registered_with_stub`.

Per-tool registration coverage (one acceptance per data row of the §2.3 tool table, per the plan-coverage contract's table-row decomposition rule):

- 2.3.7 — `get_task_stages(task_id)` on `gobby-tasks` returns the manifest in position order; output schema declares `stages: list[StageStateView]`. test: `tests/mcp_proxy/tools/tasks/test_get_task_stages.py::test_returns_position_order`.
- 2.3.8 — `list_stages_registry()` on `gobby-tasks` returns all 14 registry entries; output schema declares `entries: list[StageRegistryEntry]`. test: `tests/mcp_proxy/tools/tasks/test_list_stages_registry.py::test_returns_all_14`.
- 2.3.9 — `get_task_type_defaults(task_type)` on `gobby-tasks` returns the default manifest for a known type; errors for an unknown type. test: `tests/mcp_proxy/tools/tasks/test_get_task_type_defaults.py::test_known_and_unknown_types`.
- 2.3.10 — `start_stage(task_id, stage_name, notes?)` on `gobby-tasks-ops` transitions `ready → in_progress`; increments `attempt_count`; emits a `task_lifecycle_events` row. test: `tests/mcp_proxy/tools/tasks/test_start_stage.py::test_transitions_ready_to_in_progress`.
- 2.3.11 — `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?)` on `gobby-tasks-ops` transitions `in_progress → done`; persists `commit_sha` and merges `artifact_updates`. test: `tests/mcp_proxy/tools/tasks/test_complete_stage.py::test_transitions_to_done_with_artifacts`.
- 2.3.12 — `fail_stage(task_id, stage_name, reason, needs_human?)` on `gobby-tasks-ops` transitions `in_progress → ready` (no `attempt_count` change) when `attempt_count < cap`; escalates when `attempt_count >= cap` per acceptance 2.1.7. test: `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_under_cap_returns_to_ready`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_over_cap_escalates`.
- 2.3.13 — `add_stage(task_id, stage_name, position)` on `gobby-tasks-ops` inserts a row mid-manifest; reorders affected positions to remain dense. test: `tests/mcp_proxy/tools/tasks/test_add_stage.py::test_insert_mid_manifest_reorders`.
- 2.3.14 — `remove_stage(task_id, stage_name)` on `gobby-tasks-ops` deletes a row; reorders positions to remain dense (1..N). test: `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_reorders_dense`.
- 2.3.15 — `record_pr_verdict(task_id, verdict, findings, report_ref?)` on `gobby-tasks-ops` writes `structured_pr_verdict` (JSON) and `pr_review_report`; advances `pr` stage state per verdict (approved→complete, rejected/needs_changes→fail per §2.1 contract). test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_verdict_paths`.
- 2.3.16 — `record_pr_opened(task_id, pr_url, github_pr_number?)` on `gobby-tasks-ops` persists artifacts without changing `pr` stage state; idempotent on repeated `pr_url`. test: `tests/mcp_proxy/tools/tasks/test_record_pr_opened.py::test_no_stage_change_idempotent`.
- 2.3.17 — `record_merge_result(task_id, merge_sha?, report_ref?, failure_reason?)` on `gobby-tasks-ops` is registered as a stub in Phase 2.3 (raises `NotImplementedError`); Phase 4.2 supplies the success and failure paths. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result_stub.py::test_stub_raises_notimplemented`.

### 2.4 New HTTP routes for stage manifest [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/servers/routes/tasks.py`, `src/gobby/servers/routes/stages.py` (new)

Add new endpoints and extend the list endpoint with stage filters.

```python
# stages.py
@router.get("/api/stages/registry")
async def list_stages_registry() -> StagesRegistryResponse: ...

@router.get("/api/task-types/{task_type}/default-stages")
async def get_task_type_defaults(task_type: str) -> TaskTypeDefaultsResponse: ...

# tasks.py — new sub-routes
@router.get("/api/tasks/{task_id}/stages")
async def get_task_stages(task_id: str) -> TaskStagesResponse: ...

@router.patch("/api/tasks/{task_id}/stages/{stage_name}")
async def patch_task_stage(
    task_id: str,
    stage_name: str,
    request: StagePatchRequest,
) -> TaskStageResponse: ...

# tasks.py list endpoint extension
@router.get("/api/tasks")
async def list_tasks(
    # existing params: skip, limit, status, lifecycle, ...
    stage: str | None = None,
    stage_state: str | None = None,
    # ...
) -> TaskListResponse: ...
```

`StagePatchRequest` body schema:

```python
class StagePatchRequest(BaseModel):
    action: Literal["start", "complete", "fail", "add", "remove"]
    notes: str | None = None
    reason: str | None = None  # required for action='fail'
    needs_human: bool = False
    commit_sha: str | None = None
    artifact_updates: dict[str, str] | None = None
    position: int | None = None  # required for action='add'
```

The list endpoint's `stage` and `stage_state` filters JOIN `tasks` to `task_stage_states` and filter `WHERE task_stage_states.stage_name = :stage [AND task_stage_states.state = :stage_state]`. The response gains an optional `stages` field per task containing the denormalized manifest (a single SQL query that LEFT JOINs and groups; no N+1).

`TaskListResponse.tasks[i].stages: list[StageStateView]` is added; existing fields stay backward compatible. Existing `?status=...` and `?lifecycle=...` params remain functional through Phase 5 (they're consumed by the legacy projection helpers); Phase 5 removes them.

**Acceptance:**

- 2.4.1 — Five new endpoints registered with declared paths, methods, and schemas. file: `src/gobby/servers/routes/stages.py`, `src/gobby/servers/routes/tasks.py`. test: `tests/servers/routes/test_stage_routes.py::test_routes_registered`.
- 2.4.2 — `PATCH /api/tasks/{id}/stages/{name}` action='start' moves the row to `in_progress`. test: `tests/servers/routes/test_stage_routes.py::test_patch_start_stage`.
- 2.4.3 — `GET /api/tasks?stage=development&stage_state=in_progress` returns only tasks with that exact `(stage_name, state)` row. test: `tests/servers/routes/test_stage_routes.py::test_list_filter_by_stage_state`.
- 2.4.4 — Denormalized `stages` field returned on each task in the list response when stage filters are active or when an explicit `?include_stages=1` flag is set. test: `tests/servers/routes/test_stage_routes.py::test_list_includes_denormalized_manifest`.
- 2.4.5 — `task_event` WebSocket events fire on every stage state transition. behavior: "broadcaster emits stage_changed event" verified in `tests/servers/websocket/test_stage_broadcast.py::test_stage_transition_broadcasts`.

### 2.5 New CLI commands and build flags [category: code] (depends: 2.1, 2.3)
`kind: deliverable`

Target: `src/gobby/cli/tasks/crud.py`, `src/gobby/cli/tasks/_utils.py`, `src/gobby/cli/build.py` (or wherever `gobby build` lives)

Add new `gobby tasks` subcommands and extend `gobby build` and `gobby tasks list`.

```text
gobby tasks stages <task_ref>                    # render manifest table
gobby tasks advance <task_ref> [--stage <name>]  # complete current stage; auto-start next
gobby tasks list --stage <name> [--state <state>]
gobby build <ref> --stages <a,b,c>               # explicit manifest
gobby build <ref> --add-stage <name>[@<position>]
gobby build <ref> --skip-stage <name>            # opt-out of a default-manifest stage
```

`gobby tasks list` currently has `--status` and `--lifecycle` options (`src/gobby/cli/tasks/crud.py`). Add `--stage` and `--state` flags; they call the new HTTP endpoint with the new filter params. Keep `--status` and `--lifecycle` working through Phase 5; Phase 6 removes them.

`gobby tasks advance`: if `--stage` is omitted, advance the current stage; if specified, validate it equals the current stage's name (else error). On success, automatically advance the next manifest row to `in_progress` if it's eligible (no human gate, no agent missing). This is the CLI counterpart of Phase 3's dispatcher behavior.

`gobby build` flag resolution order: `--stages` (explicit list, replaces default); else type defaults + `--add-stage` insertions + `--skip-stage` removals. Profiles (`quick`, `full`, `full-yolo`) become named bundles of `--skip-stage` arguments resolved at build time.

CLI output for `gobby tasks stages`:

```text
$ gobby tasks stages #13482
#13482  Lifecycle + status enum alignment for kanban visibility
Stage              State        Attempts  Updated
─────────────────  ───────────  ────────  ──────────
planning           done         3         2026-04-30
adversarial_review done         2         2026-04-30
expansion          in_progress  1         2026-04-30
…
```

**Acceptance:**

- 2.5.1 — `gobby tasks stages` Click command renders the manifest table sorted by position. file: `src/gobby/cli/tasks/crud.py`. test: `tests/cli/test_tasks_stages_command.py::test_renders_manifest`.
- 2.5.2 — `gobby tasks advance` advances the current stage and auto-starts the next when eligible. test: `tests/cli/test_tasks_advance_command.py::test_auto_advance_next_stage`.
- 2.5.3 — `gobby tasks list --stage development --state in_progress` filters to that exact `(stage_name, state)` row. test: `tests/cli/test_tasks_list_stage_filter.py::test_stage_state_filter`.
- 2.5.4 — `gobby build <ref> --stages a,b,c` writes exactly that manifest; `--add-stage` inserts at requested position; `--skip-stage` omits a default stage. test: `tests/cli/test_build_stage_flags.py::test_build_flag_resolution`.

### 2.6 Rewire `mark_task_review_*` tools to stage-native semantics [category: code] (depends: 2.1, 2.3)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_transitions.py`, `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`

The agent-facing review tools (`mark_task_review_approved`, `mark_task_review_rejected`, `mark_task_needs_review`) must be cut over to stage-native semantics BEFORE Phase 3 enables the manifest dispatcher. Existing planner, plan-adversary, expansion-qa, qa-reviewer, and holistic-reviewer agents call these tools today; if Phase 3 swaps the dispatcher to read `task_stage_states` while these tools still write `status='review_approved'` / `'needs_review'` / `'rejected'`, the dispatcher and those agents drift apart for one or more heartbeats and the test_arch / expansion / development / code_review_qa / holistic_qa stage chain stalls.

Cutover preserves the agent-facing API (tool names, signatures, return shapes unchanged) but routes implementation through `complete_stage` / `fail_stage` on the current stage:

- `mark_task_review_approved(task_id, ...)` → resolves `current_stage(task_id)`, calls `StageStatesManager.complete_stage(task_id, current_stage.name, by_session_id=..., commit_sha=..., artifact_updates=...)`. Errors cleanly if `current_stage` is `None` (manifest exhausted) or its state is not `in_progress`.
- `mark_task_review_rejected(task_id, reason, ...)` → resolves `current_stage`, calls `StageStatesManager.fail_stage(task_id, current_stage.name, reason=reason, needs_human=False, by_session_id=...)`.
- `mark_task_needs_review(task_id, ...)` → semantics drift here is real and load-bearing: the legacy tool wrote `status='needs_review'` to indicate "this stage is now waiting for review by the next stage's agent." In the manifest model, that is exactly `complete_stage(current_stage)` followed by the next stage's `auto_advance_ready_rule` promotion. So the rewired implementation also calls `complete_stage(current_stage.name)`. Phase 5.3 acceptance 5.3.7's call-site audit confirms no caller relies on the old "stage stays open, status becomes needs_review" shape.

This deliverable is the rewire only; legacy `status` values (`'review_approved'`, `'needs_review'`, `'rejected'`) STOP being written by these tools after this lands. Other writers of those values (if any surface in the call-site audit) are caught and rewired in Phase 5.3 before the column drop.

The `_agent_blocked_mcp_tools` rule that currently gates these tools for spawned agents stays in place — agent visibility is unchanged. The only change is the implementation behind the existing tool surface.

**Scope of §2.6 testing (sequencing per F2 fix):** §2.6 lands BEFORE Phase 3 enables the manifest dispatcher and `auto_advance_ready_rule`. Therefore §2.6's tests are unit/contract tests that prove the rewired tools call `complete_stage`/`fail_stage` correctly (no legacy status writes) — they do NOT exercise the dispatcher heartbeat or auto-advance, because those rules do not exist yet. The end-to-end heartbeat-advance smoke that validates `auto_advance_ready_rule` consuming the rewired tool's effect lives downstream in `## V1 Verification` (and is added to §3.1 acceptance — see 3.1.6 paragraph) where `auto_advance_ready_rule` is in place.

**Acceptance:**

- 2.6.1 — `mark_task_review_approved` rewired to `StageStatesManager.complete_stage(current_stage.name)`; agent-facing signature and return shape unchanged. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_approved_completes_current_stage`.
- 2.6.2 — `mark_task_review_rejected` rewired to `StageStatesManager.fail_stage(current_stage.name, reason=...)`; rejection writes a `task_lifecycle_events` row and transitions `in_progress → ready` per §2.1's contract. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_rejected_fails_current_stage`.
- 2.6.3 — `mark_task_needs_review` rewired to `StageStatesManager.complete_stage(current_stage.name)` (legacy "needs_review" semantics map onto "complete current stage and let auto-advance promote the next"). test: `tests/storage/tasks/test_review_tools_stage_native.py::test_needs_review_completes_current_stage`.
- 2.6.4 — Calling any of the three tools when `current_stage` is `None` or its `state != 'in_progress'` raises a typed error and writes no `task_stage_states` mutation. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_invalid_current_stage_errors`.
- 2.6.5 — Unit/contract verification of the rewire (no dispatcher dependency): given a fixture task at `adversarial_review.in_progress`, calling `mark_task_review_approved` invokes `StageStatesManager.complete_stage('adversarial_review', ...)` exactly once and writes no legacy `status` value (`'review_approved'` / `'needs_review'` / `'rejected'` are not written by the rewired tools). Same shape verified for `mark_task_review_rejected` calling `fail_stage` and for `mark_task_needs_review` calling `complete_stage`. Asserted via mutation spy on the storage manager and a SQL probe on the post-call `tasks.status` column. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_approved_calls_complete_stage_no_legacy_writes`, `tests/storage/tasks/test_review_tools_stage_native.py::test_rejected_calls_fail_stage_no_legacy_writes`, `tests/storage/tasks/test_review_tools_stage_native.py::test_needs_review_calls_complete_stage_no_legacy_writes`. The corresponding heartbeat-advance smoke that validates Phase 3.1's `auto_advance_ready_rule` consumption of these rewired calls lives at `## V1 Verification` (covered by the dispatcher chain assertion) once `auto_advance_ready_rule` is in place; placing it there avoids a forward dependency from §2.6 to §3.1.
- 2.6.6 — Pre-Phase-3 bundled call-site audit of `mark_task_review_approved`, `mark_task_review_rejected`, and `mark_task_needs_review`. Audit enumerates callers via `grep -rln 'mark_task_review_\(approved\|rejected\)\|mark_task_needs_review' src/gobby/install/shared/` and confirms every caller continues to use the rewired tools (agent-facing API unchanged) — no caller relies on the legacy "stage stays open, status becomes needs_review" shape. Allowlist of expected callers: `planner.yaml`, `plan-adversary.yaml`, `expansion-qa.yaml`, `qa-reviewer.yaml`, `holistic-reviewer.yaml`, `test-architect.yaml`, `requirements-analyst.yaml`, `merge-orchestrator.yaml`, `qa-dev.yaml`, `nightly-linter.yaml`, `nightly-test-fixer.yaml`, `backend-developer.yaml`, `frontend-developer.yaml`, `default.yaml`, `developer.yaml`, plus the bundled SKILL.md instruction surfaces (`automate`, `holistic-review`, `merge-expert`, `plan-draft`, `plan-review`, `plan`, `qa`, `review`, `source-control`, `task-transitions`) and the rule YAMLs (`memory-lifecycle/require-memory-review-before-status.yaml`, `task-enforcement/{block-needs-review-interactive,inject-transition-skill,require-commit-before-status,require-error-triage,require-task-transitions-skill-loaded}.yaml`). The audit is a positive-coverage check: the grep snapshot at deliverable-execution time MUST equal this allowlist (any addition fails the audit and is investigated; any removal also fails so the executing agent confirms the call site was intentionally retired). The §5.3.7 post-rewire audit covers the same surface after the column drop; both audits are needed because new callers can be added between §2.6 landing and §5.3 landing. test: `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_call_sites_match_allowlist`.

## P3 Dispatcher Refactor
`kind: framing`

**Goal**: Rewrite the dispatcher's rule evaluation, candidate scan, and build-time manifest resolution to use `task_stage_states` instead of `(lifecycle, status)` tuples. After Phase 3, the daemon dispatches purely from the manifest model.

### 3.1 Rewrite `dispatch/rules.py` to query stage manifest [category: code] (depends: 2.1, 2.2)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`

Replace string-checking helpers and rule bodies with manifest-aware reads. Existing helpers to retire or rewrite:

- `_stage_skipped(task, stage)` — delete; "skipped" means "stage absent from manifest". Replaced by `task_has_stage(task, stage)`.
- `_state(task)` (lifecycle, status tuple) — delete; rules read `current_stage(task)` and the row's `state`.
- `_advance(task, lifecycle, status, reason)` — replaced by `_complete_current_stage(task, reason)` and `_fail_current_stage(task, reason, needs_human)` helpers that delegate to `StageStatesManager`.

New helpers:

```python
def task_has_stage(task: Task, stage_name: str) -> bool:
    """True iff task.stages contains a row for stage_name (in any state)."""

def current_stage(task: Task) -> StageState | None:
    """Leftmost manifest row by position whose state != 'done'."""

def _spawn_stage_agent(
    task: Task, stage: StageState, context: RuleContext, agent_slug: str,
) -> SpawnAgentAction: ...

def _advance_to_next_stage(task: Task, reason: str) -> AdvanceStageAction: ...
```

`Task` gains a denormalized `.stages: tuple[StageState, ...]` field populated by `reload_candidate` via a single LEFT JOIN; rules never re-query.

Rule rewrite (1:1 mapping from existing rules to new stage-native form, plus one new generic auto-advance rule that runs first):

| Old rule | New rule | Gates on | Action |
|----------|----------|----------|--------|
| (new) | `auto_advance_ready_rule` | `current_stage.state == 'ready'` AND (`current_stage.position == 1` OR prior stage row is `done`) AND registry `requires_human == False` AND (`registry.default_agent IS NULL` OR the registered agent is `enabled: true`) AND `current_stage.name NOT IN {'development', 'holistic_qa'}` (those two stages are owned by their dedicated rules — see "Stages excluded from auto-advance" below) AND no other rule has produced an action this scan | `_start_current_stage(task)` — transitions `ready → in_progress` via `StageStatesManager.start_stage`. Runs FIRST in the rule list so the next heartbeat sees `in_progress` and the stage-specific rule fires. Closes the fresh-task-stalls-at-ready gap (build initializes manifest at all-`ready`; this rule promotes position 1). The auto-advance does NOT fire for stages with `requires_human == True` — those wait for an explicit `start_stage` call from a human or operator. |
| (new) | `disabled_agent_escalation_rule` | `current_stage.state == 'ready'` AND `current_stage.name NOT IN {'development', 'holistic_qa'}` AND (`registry.default_agent IS NULL` OR the registered agent is `enabled: false`) AND no other rule has produced an action this scan | emit `EscalateAction(task_id=task.id, reason=f'{current_stage.name}_no_agent')` — sets `is_escalated=1` and writes the escalation reason. Ordered AFTER `auto_advance_ready_rule` (which short-circuits the rule chain on enabled agents) and BEFORE the per-stage in-progress rules. Catches the four discovery placeholder cases (`ideation_no_agent`, `research_no_agent`, `architecture_no_agent`, `prd_no_agent`) under §1.3 placeholder shims, plus any future stage whose default_agent slot is set to a disabled bundled agent. Without this rule, ready stages with disabled placeholder agents would stall forever (no auto-start, no spawn, no escalation) — making §5.1.4's `research_spike` / `prd_doc` / `architecture_doc` discovery walks unreachable under dispatcher control. The escalation surfaces the gap to operators, who clear it once the real agent ships. |
| `plan_review_rule` | `planning_rule` | `current_stage.name == 'planning'` AND `state == 'in_progress'` | spawn `planner` (already speced) |
| (new) | `adversarial_review_rule` | `current_stage.name == 'adversarial_review'` AND `state == 'in_progress'` | spawn `plan-adversary` |
| `test_arch_rule` | `test_arch_rule` | `current_stage.name == 'test_arch'` AND `state == 'in_progress'` | spawn `test-architect` |
| `expansion_rule` | `expansion_rule` | `current_stage.name == 'expansion'` AND `state == 'in_progress'` | `StartExpansionAction` |
| (new) | `expansion_qa_rule` | `current_stage.name == 'expansion_qa'` AND `state == 'in_progress'` | escalate (no agent yet — flag with `EscalateAction(reason='expansion_qa_no_agent')`) |
| `isolation_rule` | `development_isolation_rule` | `current_stage.name == 'development'` AND `state == 'ready'` | Four-case state machine — see "Development-ready state machine" below |
| `dev_rule` | `development_rule` | `current_stage.name == 'development'` AND `state == 'in_progress'` AND `_is_leaf(task)` | spawn `dev-agent` |
| `qa_rule` | `code_review_qa_rule` | `current_stage.name == 'code_review_qa'` AND `state == 'in_progress'` AND `_is_leaf(task)` | spawn `qa-reviewer` |
| `leaf_park_rule` | **(deleted — folded into `is_child_parked` predicate; see below)** | — | — |
| `all_leaves_holistic_rule` | `all_leaves_holistic_rule` | `_is_epic(task)` AND `current_stage(task) == ('holistic_qa', 'ready')` AND every direct child satisfies `is_child_parked(child)` OR `child.is_closed` (terminal-closed) | emit `StartStageAction(task_id, stage_name='holistic_qa')` — calls `StageStatesManager.start_stage` to transition `holistic_qa.ready → in_progress`. The parent task is the automation-candidate (its `holistic_qa.ready` keeps it in the §3.2 scan); the rule reaches into each child's denormalized `child.stages` via `is_child_parked`. No leaf-side rule or signal action is required. |
| `holistic_rule` | `holistic_qa_rule` | `current_stage.name == 'holistic_qa'` AND `state == 'in_progress'` | spawn `holistic-qa` (Phase 4 wires `pr` advance) |
| `pr_rule` | (Phase 4) | — | (Phase 4) |
| `merge_rule` | (Phase 4) | — | (Phase 4) |

**Stages excluded from auto-advance** (`auto_advance_ready_rule` skips them; the dedicated rule below owns the transition):

- `development` — owned by `development_isolation_rule` (must inspect `task.isolation` and `task_artifacts` worktree/clone pair before starting).
- `holistic_qa` — owned by `all_leaves_holistic_rule` (must verify every child is parked or terminal before starting; epics may have children working through development for many heartbeats while the parent's `holistic_qa.ready` is technically the leftmost-non-done row).

Implementation choice: hardcode the two-element exclude list in the rule body (`current_stage.name NOT IN {'development', 'holistic_qa'}`). Alternative (registry sentinel `auto_advance: false` per stage) is deferred — two special-cases is still cleaner than a registry change for this epic, and the exclude list is co-located with the rule it belongs to.

**`is_child_parked(child) -> bool` predicate** (defined alongside `_is_leaf` in `src/gobby/dispatch/rules.py`):

```python
def is_child_parked(child: Task) -> bool:
    """Durable predicate: True when a leaf child has finished its work and the
    parent's holistic_qa is safe to advance. Computed entirely from the child's
    denormalized manifest (`child.stages`) and `is_closed`/`is_escalated`
    flags — no new column, no transient signal, no cross-heartbeat state.

    A child is parked when ALL of:
      * `_is_leaf(child)` is True,
      * `child.is_escalated` is False (escalated children block the parent),
      * EITHER `child.is_closed` is True (terminal-closed via the §2.1
        invariant-8 manifest-exhausted close — ANY task type, ANY terminal
        stage — or via the legacy merge cascade) OR `current_stage(child)
        is None AND NOT child.is_closed` (every manifest row is `done` but
        is_closed has not yet been written).

    Reachability of the `current_stage is None AND NOT is_closed` branch.
    Under §2.1 invariant 8, `complete_stage` writes the close in the SAME DB
    transaction as the final stage UPDATE; close-failure rolls both back and
    escalates the task. Therefore this branch is NEVER reachable for a task
    that has reached manifest exhaustion via `complete_stage` in normal
    operation. It is reachable in exactly two bounded windows:

      1. **Migration 234 backfill window.** Mid-transaction, after the
         backfill writes manifest rows but before the §2.2 acceptance 2.2.31
         close-pass commits, an all-`done`-but-not-`is_closed` row can briefly
         exist. The window is the single migration transaction; once 234
         commits, every such task is closed.
      2. **Synthetic test fixtures.** Tests that build `Task.stages` directly
         (bypassing `complete_stage`) to exercise this predicate's branches
         without driving a real terminal close.

    Outside those two windows, the branch is unreachable. The predicate
    accepts it as defense-in-depth so the parent's `all_leaves_holistic_rule`
    does not stall on a transient mid-transaction state during migration.

    The previous predicate added a third gate (highest-position done row
    `stage_name == 'code_review_qa'`) which doesn't match the §2.2 default
    manifests: `task` and `chore` end at `merge`; `bug`/`refactor`/`feature`
    have `pr` and `merge` after `code_review_qa`. The terminal-close
    contract from §2.1 invariant 8 / acceptance 2.1.9 makes that gate
    unnecessary — leaves auto-close at whatever stage their manifest ends,
    and the parent's rule treats all terminal-closed leaves as parked.
    """
```

The predicate is a pure function of `child.stages` and the two task flags — `reload_candidate` already populates `child.stages`, `is_closed`, and `is_escalated` per acceptance 3.1.3, so the parent's rule pays no extra SQL. The previous `LeafParkedSignalAction` was deleted because it required scanning manifest-exhausted leaves (which §3.2's automation-candidate filter excludes) and produced only transient state. This predicate replaces it durably without any new schema or scan-set change. With §2.1 invariant 8's terminal-close contract and §2.2 acceptance 2.2.31's backfill close-pass, the predicate's primary truth path is `_is_leaf AND is_closed AND NOT is_escalated`; the `current_stage is None AND NOT is_closed` branch is reachable in exactly two bounded windows — the migration-234 transaction (closed by 2.2.31 before commit) and synthetic test fixtures that bypass `complete_stage` — and is documented as such in the docstring above.

**Development-ready state machine (`development_isolation_rule` four cases):** The legacy isolation rule only created worktrees/clones; it did not own stage starts. Under the manifest model, the development stage stays at `development.ready` until this rule fires, so the rule MUST handle every starting condition or development-ready tasks stall forever. Cases (rule body evaluates them in order):

- **(a) `task.isolation == 'none'`** — no isolation needed. Rule emits a `StartStageAction(task_id, stage_name='development')` that calls `StageStatesManager.start_stage(task_id, 'development', by_session_id='dispatcher')`. Stage transitions `ready → in_progress` immediately; `development_rule` fires on the next heartbeat.
- **(b) Isolation required AND the worktree/clone pair already exists in `task_artifacts`** — operator pre-created the isolation, or a prior heartbeat created it but failed to start the stage (recovery case). Same action as (a): `StartStageAction`. Rule reads `task.artifacts.worktree_path` / `worktree_id` for `isolation == 'worktree'` and `task.artifacts.clone_path` / `clone_id` for `isolation == 'clone'`; both members of the pair must be present.
- **(c) Isolation required AND the worktree/clone pair is missing** — normal first-time start. Rule emits `CreateIsolationAction(task_id)`. The action handler creates the isolation, writes the artifact pair atomically, and on success enqueues a follow-up `StartStageAction` that fires on the next heartbeat (stage stays at `ready` for one heartbeat while isolation is created — same latency as the legacy isolation rule). Alternative (single-action atomic create + start) is rejected: keeps action handlers single-purpose and lets isolation creation fail without partial stage transitions.
- **(d) Isolation creation fails** — `CreateIsolationAction` returns failure (handler raises or returns a failure result). Rule emits `EscalateAction(task_id=task.id, reason=f'development_isolation_failed:{type(error).__name__}')`. The error type is encoded INSIDE the supported `reason` string; `EscalateAction` at `src/gobby/dispatch/actions.py:63` accepts only `task_id` and `reason`, no `detail` kwarg. Stage remains at `ready`; `is_escalated=1` is set on the task. Operator must investigate (the daemon log carries the full error message and stack trace), clear the escalation, and the rule re-evaluates on the next heartbeat (case (c) again unless the operator manually created the isolation, in which case case (b)).

The rule never spawns the development agent — that is `development_rule`'s job (`current_stage.state == 'in_progress'`). This separation keeps the start transition deterministic and lets retries of agent-spawn failures not re-create isolation.

For each retained rule, port the existing attempt-count helpers to read `StageState.attempt_count` instead of artifact counters (`qa_attempts`, etc.). The artifact counter columns (`max_qa_rounds`, etc.) stay as caps; only the per-attempt counter is moved into the manifest row.

`_is_unattended(task)` continues to read `task.assigned_agent`; that field is retained and still drives the unattended-fallback branch in `_fallback`.

**Acceptance:**

- 3.1.1 — `task_has_stage` and `current_stage` helpers added; `_stage_skipped` and `_state` deleted. file: `src/gobby/dispatch/rules.py`. symbol: `gobby.dispatch.rules.task_has_stage`, `gobby.dispatch.rules.current_stage`.
- 3.1.2 — Each rule in the table above is renamed and rewritten to query the manifest; old `_advance(...)` calls replaced with `StageStatesManager` writes. test: `tests/dispatch/test_rules_stage_native.py::test_rule_table_complete`.
- 3.1.3 — `Task.stages` denormalized field populated by `reload_candidate`. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_reload_candidate_includes_stages.py::test_stages_loaded`.
- 3.1.4 — Attempt-count helpers read from `StageState.attempt_count`, with `max_qa_rounds`-style caps still honored. test: `tests/dispatch/test_rules_stage_native.py::test_attempt_caps_honored`.
- 3.1.5 — Pass-through escalate-no-agent rule for `expansion_qa` until an agent is registered (no silent wait). test: `tests/dispatch/test_rules_stage_native.py::test_no_agent_stage_escalates`.
- 3.1.6 — `auto_advance_ready_rule` promotes the leftmost `ready` row to `in_progress` when (a) it is position 1 OR the prior row is `done`, (b) the stage registry entry has `requires_human == False`, (c) any registered `default_agent` is `enabled: true`, and (d) `current_stage.name NOT IN {'development', 'holistic_qa'}` (those two stages are owned by `development_isolation_rule` and `all_leaves_holistic_rule` respectively). A freshly built task with manifest `[planning, adversarial_review, ...]` advances `planning.ready → planning.in_progress` on the first heartbeat; an epic with `holistic_qa.ready` as its leftmost-non-done row does NOT auto-start `holistic_qa` even if the prior `expansion_qa` row is `done`. When condition (c) is FALSE (the `default_agent` is missing or `enabled: false`), this rule does NOT fire — the disabled-agent case is handled by `disabled_agent_escalation_rule` (acceptance 3.1.23) which escalates instead of stalling. test: `tests/dispatch/test_rules_stage_native.py::test_auto_advance_first_stage`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_skips_human_gated`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_skips_disabled_agent_yields_to_escalation_rule`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_skips_holistic_qa`.
- 3.1.7 — `development_isolation_rule` case (a): `task.isolation == 'none'` and `current_stage == ('development', 'ready')` → rule starts the stage immediately (`ready → in_progress`) without creating any isolation. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_none_starts_immediately`.
- 3.1.8 — `development_isolation_rule` case (b): isolation required AND the worktree/clone pair already exists in `task_artifacts` → rule starts the stage immediately without re-creating isolation; same `(ready → in_progress)` transition path as case (a). Verifies recovery from a prior heartbeat that created isolation but did not start the stage. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_pair_present_starts_stage`.
- 3.1.9 — `development_isolation_rule` case (c): isolation required AND pair missing → rule emits `CreateIsolationAction`; on success the artifact pair is written atomically and the next heartbeat starts the stage. Stage stays at `ready` for exactly one heartbeat after isolation creation. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_missing_creates_then_starts`.
- 3.1.10 — `development_isolation_rule` case (d): `CreateIsolationAction` fails (handler returns failure or raises) → rule emits an escalation action whose constructor receives ONLY `task_id` and `reason` kwargs (no `detail`, matching `src/gobby/dispatch/actions.py:63` signature), with the reason string carrying the error type as a colon-suffix in the form `development_isolation_failed:<error_type>`. `task.is_escalated == 1` is set; stage remains at `development.ready`; on next heartbeat the rule re-evaluates only after the escalation is cleared. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_failure_escalates_with_reason_carrying_error_type`, `tests/dispatch/test_development_isolation_rule.py::test_isolation_failure_escalation_uses_supported_signature_only`. Source-code regression (post-implementation): a grep for the unsupported-kwarg pattern across `src/gobby/dispatch/rules.py` and `src/gobby/dispatch/actions.py` returns zero matches. The plan file itself is exempt because its changelog and acceptance prose are historical documentation of the bug; only runnable code is in scope for the negative check.

Per-rule coverage (one acceptance per data row of the §3.1 rule rewrite table, per the plan-coverage contract's table-row decomposition rule). Some rules already have dedicated acceptances above (`auto_advance_ready_rule` → 3.1.6; `development_isolation_rule` → 3.1.7-3.1.10); the items below cover the remaining rule rows so every data row has its own acceptance:

- 3.1.11 — `planning_rule` fires on `current_stage == ('planning', 'in_progress')` and emits a spawn action for the `planner` agent. test: `tests/dispatch/test_rules_stage_native.py::test_planning_rule_spawns_planner`.
- 3.1.12 — `adversarial_review_rule` fires on `current_stage == ('adversarial_review', 'in_progress')` and emits a spawn action for the `plan-adversary` agent. test: `tests/dispatch/test_rules_stage_native.py::test_adversarial_review_rule_spawns_plan_adversary`.
- 3.1.13 — `test_arch_rule` fires on `current_stage == ('test_arch', 'in_progress')` and emits a spawn action for the `test-architect` agent. test: `tests/dispatch/test_rules_stage_native.py::test_test_arch_rule_spawns_test_architect`.
- 3.1.14 — `expansion_rule` fires on `current_stage == ('expansion', 'in_progress')` and emits a `StartExpansionAction` (pipeline action; no agent spawn). test: `tests/dispatch/test_rules_stage_native.py::test_expansion_rule_emits_start_expansion`.
- 3.1.15 — `expansion_qa_rule` fires on `current_stage == ('expansion_qa', 'in_progress')` and escalates with `EscalateAction(reason='expansion_qa_no_agent')` until the QA agent is registered (existing bundled YAML rewired in Phase 4.2 acceptance 4.2.4). test: `tests/dispatch/test_rules_stage_native.py::test_expansion_qa_rule_escalates_no_agent`.
- 3.1.16 — `development_rule` fires on `current_stage == ('development', 'in_progress')` AND `_is_leaf(task)` and emits a spawn action for the `dev-agent` (registry default `backend-developer`). test: `tests/dispatch/test_rules_stage_native.py::test_development_rule_spawns_dev_agent`.
- 3.1.17 — `code_review_qa_rule` fires on `current_stage == ('code_review_qa', 'in_progress')` AND `_is_leaf(task)` and emits a spawn action for the `qa-reviewer` agent. test: `tests/dispatch/test_rules_stage_native.py::test_code_review_qa_rule_spawns_qa_reviewer`.
- 3.1.18 — `is_child_parked(child)` predicate is a pure function of `child.stages` + `child.is_closed` (Python projection from `is_task_closed` reading `closed_at IS NOT NULL OR status == 'closed'`) + `child.is_escalated` flags: returns True iff `_is_leaf(child)` AND NOT `child.is_escalated` AND (`child.is_closed` OR `current_stage(child) is None`). Returns False for non-leaf, in-progress, escalated, or open-and-non-exhausted children. The predicate is body-aligned with §2.2 default manifests: `task`/`chore` (end at `merge`), `bug` (end at `merge` after `code_review_qa`), `refactor`/`feature` (end at `merge` after `code_review_qa`/`holistic_qa`/`pr`) all auto-close at manifest exhaustion via §2.1 invariant 8 (which writes `closed_at`), and the predicate fires on `child.is_closed`. The `current_stage is None AND NOT child.is_closed` branch is reachable ONLY (i) on the §2.2 migration-234 transaction boundary before the acceptance 2.2.31 close-pass commits, or (ii) inside synthetic test fixtures that bypass `complete_stage`; both windows are documented in the predicate's docstring. symbol: `gobby.dispatch.rules.is_child_parked`. test: `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_true_for_terminal_closed_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_true_for_each_default_manifest_terminal_stage` (parameterized over `[development, code_review_qa, merge, pr, holistic_qa]` as last stage), `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_false_for_in_progress_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_false_for_escalated_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_false_for_non_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_synthetic_branch_is_test_only_or_migration_window`.
- 3.1.19 — `all_leaves_holistic_rule` fires on an epic whose `current_stage == ('holistic_qa', 'ready')` AND every direct child satisfies `is_child_parked(child) OR child.is_closed`. The rule emits `StartStageAction(task_id, stage_name='holistic_qa')`, transitioning the epic's `holistic_qa` row `ready → in_progress` via `StageStatesManager.start_stage`. The rule reaches into each child's denormalized `child.stages` (already loaded by `reload_candidate` per 3.1.3) — no leaf-side rule, no transient signal, no extra SQL round-trip per heartbeat. Cross-heartbeat correctness: a child that completed `code_review_qa` in heartbeat N still satisfies `is_child_parked` at heartbeat N+M for any M≥0, so the parent advances `holistic_qa` whenever the heartbeat happens to scan it next, without any race window. The rule does NOT fire when any child is still working (some `is_child_parked` returns False AND that child is not closed). test: `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_advances_epic_when_all_children_parked`, `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_does_not_fire_when_any_child_in_progress`, `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_advances_with_mix_of_parked_and_terminal_closed_children`, `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_durable_across_heartbeats`.
- 3.1.20 — `holistic_qa_rule` fires on `current_stage == ('holistic_qa', 'in_progress')` and emits a spawn action for the `holistic-qa` agent (existing bundled `holistic-reviewer` YAML rewired). test: `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_rule_spawns_holistic_qa_agent`.
- 3.1.21 — `pr_rule` (registered placeholder in this deliverable; full body in Phase 4.1) is present in the rules list at the post-`holistic_qa_rule` position; Phase 4.1 supplies its body. test: `tests/dispatch/test_rules_stage_native.py::test_pr_rule_registered_placeholder`.
- 3.1.22 — `merge_rule` (registered placeholder in this deliverable; full body in Phase 4.2) is present in the rules list at the post-`pr_rule` position; Phase 4.2 supplies its body. test: `tests/dispatch/test_rules_stage_native.py::test_merge_rule_registered_placeholder`.
- 3.1.23 — `disabled_agent_escalation_rule` (round-14 F2): fires when `current_stage.state == 'ready'` AND `current_stage.name NOT IN {'development', 'holistic_qa'}` AND (the registry's `default_agent` is `NULL` OR resolves to a bundled agent with `enabled: false`) AND no other rule has produced an action this scan. Action: `EscalateAction(task_id=task.id, reason=f'{current_stage.name}_no_agent')` — sets `is_escalated=1`, writes the escalation reason. Ordered AFTER `auto_advance_ready_rule` (which short-circuits the chain on enabled agents) and BEFORE the per-stage in-progress rules. The four discovery-stage placeholder cases under §1.3 produce reasons `ideation_no_agent`, `research_no_agent`, `architecture_no_agent`, `prd_no_agent`. Without this rule, ready stages with disabled placeholder agents would stall forever — making §5.1.4's `research_spike` / `prd_doc` / `architecture_doc` walks unreachable under dispatcher control. symbol: `gobby.dispatch.rules.disabled_agent_escalation_rule`. test: `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_fires_on_ideation_with_disabled_analyst` (parameterized over the four (stage, slug) pairs from §1.3: ideation/analyst, research/researcher, architecture/architect, prd/product-manager), `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_skips_when_agent_enabled`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_skips_development_and_holistic_qa`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_does_not_fire_when_state_in_progress`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_emits_correct_reason_per_stage`.

### 3.2 Manifest resolution at build time + readiness projections rewrite [category: code] (depends: 2.1, 2.2, 3.1)
`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/storage/tasks/_blocking.py`, `src/gobby/storage/tasks/_aggregates.py`, `src/gobby/dispatch/dispatcher.py`

`gobby build` flow rewrite. Build resolves the task's manifest before the dispatcher ever sees it:

1. Read `task.task_type`, fetch defaults via `StageRegistryManager.list_default_stages(task_type)`.
2. Apply CLI/MCP/HTTP flag overrides (`--stages`, `--add-stage`, `--skip-stage`, profiles `quick|full|full-yolo`).
3. Call `StageStatesManager.initialize_manifest(task_id, resolved_stages, ...)`.
4. Set `allow_automation=True`, `yolo` per profile, `isolation` per profile.
5. Return `BuildResult` with the resolved manifest in `manifest` field for caller display.

Profile → flag bundle resolution:

```python
PROFILE_BUNDLES: dict[str, ProfileBundle] = {
    "quick":     ProfileBundle(skip=["adversarial_review", "expansion_qa", "holistic_qa"]),
    "review":    ProfileBundle(skip=[]),  # default
    "full":      ProfileBundle(skip=[]),
    "full-yolo": ProfileBundle(skip=[], yolo=True),
}
```

Profile bundles resolve to `--skip-stage` arguments that get applied alongside any explicit `--skip-stage` flags.

Readiness projections rewrite:

- `list_ready_tasks` (storage layer) — old: `WHERE status='open' AND ...`. New: `WHERE NOT is_closed AND NOT is_escalated AND NO unresolved blocker AND current_stage IS NOT NULL AND current_stage.state IN ('ready','in_progress')`. Implementation: subquery against `task_stage_states` for `current_stage`; filter by `closed_at IS NULL`, `is_escalated = 0` (Phase 5 backfills the column), and the existing blocker join.
- `list_blocked_tasks` — old: relied on `status='escalated'`-as-block plus dependency checks. New: `is_escalated = 1 OR active_blocked_by IS NOT EMPTY`. Excludes parent tasks blocked only by their own descendants — preserve the existing SQL-inline completion-block exclusion (the `parent_task_id` carve-out at `src/gobby/storage/tasks/_queries.py:211,237,314` and `src/gobby/storage/tasks/_aggregates.py:81,103,164`; there is no Python helper named `_filter_completion_blocks`, the carve-out is a `WHERE` clause clause repeated across queries).
- `suggest_next_task` — same readiness criteria as `list_ready_tasks`, sorted by priority + age.
- `list_automation_candidates` — old: `WHERE allow_automation=true AND status IN ('open','in_progress','needs_review','review_approved')`. New: `WHERE allow_automation=true AND NOT is_closed AND NOT is_escalated AND current_stage.state IN ('ready','in_progress')`. Manifest-exhausted leaves (`current_stage IS NULL`) are intentionally excluded — their work is complete and they need no dispatcher attention. The parent epic remains automation-eligible because its own `holistic_qa.ready` keeps `current_stage` populated; `all_leaves_holistic_rule` reaches into each child's denormalized `child.stages` via `is_child_parked(child)` (acceptance 3.1.18) to gate the parent's transition. No leaf scan is required for parking detection.

For each rewritten projection, write a contract test that runs the OLD model on a fixture DB, runs the NEW model on the same fixture DB after backfill, and asserts identical task ID sets. This is the load-bearing equivalence guarantee from the strategy plan.

`reload_candidate` (`src/gobby/dispatch/dispatcher.py:145-156`) loads `Task.stages` via a JOIN against `task_stage_states ORDER BY position` and packs into `Task.stages` tuple. The SELECT projects `task_stage_states.updated_at` into each `StageState.updated_at` field per the §2.1 dataclass shape (acceptance 2.1.8); the §3.3 mutex snapshot reads it from `Task.stages[<current_idx>].updated_at`.

**Acceptance:**

- 3.2.1 — `gobby build` writes the resolved manifest via `initialize_manifest` and returns it in `BuildResult`. file: `src/gobby/build/service.py`. test: `tests/build/test_build_resolves_manifest.py::test_default_manifest`.
- 3.2.2 — Profile bundles `quick`, `review`, `full`, `full-yolo` resolve to declared skip lists. test: `tests/build/test_build_profiles.py::test_quick_skips_adversarial_and_qa`.
- 3.2.3 — `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates` rewritten to manifest reads. file: `src/gobby/storage/tasks/_blocking.py`, `src/gobby/storage/tasks/_aggregates.py`. test: `tests/storage/tasks/test_readiness_equivalence.py::test_old_vs_new_identical`.
- 3.2.4 — `reload_candidate` populates `Task.stages` in a single SQL round-trip. test: `tests/dispatch/test_reload_candidate_n1.py::test_no_n1_query`.

### 3.3 Cut over `RuntimeDispatchMutex` candidate-snapshot check from `(lifecycle, status)` to `(stage_name, stage_state, updated_at)` [category: code] (depends: 3.1, 3.2)
`kind: deliverable`

Target: `src/gobby/dispatch/mutex.py`, `src/gobby/dispatch/dispatcher.py`

`RuntimeDispatchMutex` (defined at `src/gobby/dispatch/mutex.py:27`) currently carries `expected_lifecycle` and `expected_status` fields plus a `candidate_tuple_matches()` method that the heartbeat (`run_heartbeat` in `dispatcher.py`) uses to detect when a candidate's state changed between scan and mutex acquisition. After Phase 5.3 drops `lifecycle`, `lifecycle_stage`, and active `status` from `tasks`, this race check is either broken (FK reads return `NULL`) or silently disabled (default-tuple match). The dispatcher's correctness guarantee — "skip a candidate whose state changed under us" — vanishes.

Cutover: replace the legacy tuple with a stage manifest snapshot. The new fields are `expected_stage_name`, `expected_stage_state`, and `expected_stage_updated_at` (the `task_stage_states.updated_at` value of the candidate's `current_stage` at scan time). The renamed method `candidate_stage_snapshot_matches(current_stage_name, current_stage_state, current_stage_updated_at)` returns True iff all three match. The heartbeat passes `candidate.stages[<current_stage_idx>].updated_at` as the third arg.

```python
@dataclass(frozen=True, slots=True)
class RuntimeDispatchMutex:
    storage: TaskDispatchMutexManager
    task_id: str
    holder: str
    action_kind: str
    ttl_seconds: int = 30
    expected_stage_name: str | None = None
    expected_stage_state: Literal["ready","in_progress","done"] | None = None
    expected_stage_updated_at: str | None = None

    def candidate_stage_snapshot_matches(
        self,
        current_stage_name: str | None,
        current_stage_state: str | None,
        current_stage_updated_at: str | None,
    ) -> bool:
        """True iff the candidate's current_stage row is unchanged from scan time.

        All three expected_* fields must be non-None and equal to the passed
        values. Any field None on either side is a mismatch (forces re-scan).
        """
```

The heartbeat call site (`run_heartbeat` in `dispatcher.py`) passes the candidate's current stage snapshot when constructing the mutex:

```python
current_stage = candidate.current_stage  # leftmost non-done row, or None
mutex = RuntimeDispatchMutex(
    storage=storage,
    task_id=candidate.id,
    holder=holder,
    action_kind=f"dispatch:{action.kind}",
    ttl_seconds=30,
    expected_stage_name=current_stage.name if current_stage else None,
    expected_stage_state=current_stage.state if current_stage else None,
    expected_stage_updated_at=current_stage.updated_at if current_stage else None,
)
```

When the dispatcher acquires the mutex, it re-reads the candidate's current stage and calls `candidate_stage_snapshot_matches`; if it returns False, the dispatch is aborted for this heartbeat (next heartbeat re-scans).

Phase 2.1 (acceptance 2.1.6) already requires `StageStatesManager` mutators to wrap their writes in `RuntimeDispatchMutex`. Those call sites pre-acquired the mutex via the wrapper class but did not yet check the candidate snapshot — they relied on the row-level lock for serialization. After this deliverable, those call sites pass `expected_stage_*` fields too, giving the same staleness check as the dispatcher heartbeat.

This deliverable depends on Phase 3.1 (manifest-native rules supply `Task.stages`) and Phase 3.2 (`reload_candidate` populates `Task.stages.updated_at`). Phase 5.3 cannot drop `lifecycle`/`status` from `tasks` until this deliverable lands; §5.3's `depends_on` already includes `3.1, 3.2` and the F4 fix re-pins it to include this deliverable explicitly.

**Acceptance:**

- 3.3.1 — `RuntimeDispatchMutex` API exposes `expected_stage_name`, `expected_stage_state`, `expected_stage_updated_at` fields and `candidate_stage_snapshot_matches()` method; legacy `expected_lifecycle`, `expected_status`, and `candidate_tuple_matches()` are removed. file: `src/gobby/dispatch/mutex.py`. symbol: `gobby.dispatch.mutex.RuntimeDispatchMutex.candidate_stage_snapshot_matches`. test: `tests/dispatch/test_runtime_dispatch_mutex.py::test_snapshot_match_api`.
- 3.3.2 — `run_heartbeat` constructs the mutex with the candidate's current-stage snapshot (`name`, `state`, `updated_at`); on mutex acquisition the heartbeat re-reads the row and compares. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_runtime_dispatch_mutex.py::test_heartbeat_passes_snapshot`.
- 3.3.3 — Stale-candidate test: candidate is scanned at `(development, in_progress, T0)`; before mutex acquisition, another writer transitions the row to `(development, ready, T1)` (a `fail_stage` from concurrent rejection). The mutex acquires the row-lock, calls `candidate_stage_snapshot_matches`, returns False, and the dispatch action is dropped without any side effect; next heartbeat re-scans and produces a new candidate. test: `tests/dispatch/test_runtime_dispatch_mutex.py::test_stale_snapshot_aborts_dispatch`.
- 3.3.4 — `StageStatesManager` mutator call sites (Phase 2.1 acceptance 2.1.6) pass `expected_stage_*` fields when constructing the mutex; concurrent transitions on the same task observe each other's state changes via the snapshot check rather than only the row lock. test: `tests/storage/tasks/test_stage_states_concurrency.py::test_mutex_snapshot_check_on_mutators`.

## P4 PR / Merge / Review Stage Cutover
`kind: framing`

**Goal**: Land the stage-native PR and merge rules with their delivery artifacts. After Phase 4, #13552 (PR-Agent) and #13560-class merge work can target the new stage contract.

### 4.1 PR stage rule + delivery artifacts [category: code] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/storage/tasks/_artifacts.py`, `src/gobby/mcp_proxy/tools/tasks/_stages.py`

Add `pr_rule`. Until #13552 lands the PR-Agent, this rule escalates `pr.state == 'in_progress'` with reason `pr_no_agent` so the work surfaces to a human; once the agent is wired (out of scope here), the rule spawns it.

Stage transitions during PR work:

1. `holistic_qa.state` becomes `done` → `pr.state` transitions `ready → in_progress` (via `_advance_to_next_stage`).
2. PR opened: agent or operator calls `record_pr_opened(task_id, pr_url, github_pr_number?)` (registered in Phase 2.3) to write `pr_url` and `github_pr_number` artifacts without changing stage state.
3. PR review verdict: `record_pr_verdict(task_id, verdict='approved'|'rejected'|'needs_changes', findings, report_ref?)`. Writes `structured_pr_verdict` (JSON) and `pr_review_report`. On `approved`, completes `pr` stage; on `rejected` or `needs_changes`, fails the stage via §2.1's contract — `fail_stage` transitions `in_progress → ready` with no attempt-count change. The next `start_stage` (auto-advance promoting the row back to `in_progress` for a retry) is the sole `attempt_count` increment site. Cap source for the PR stage is the existing `task_artifacts.max_review_rounds` column (nullable INTEGER, present in baseline schema at `src/gobby/storage/migrations.py:93` and `src/gobby/storage/baseline_schema.sql:389`, surfaced as `TaskArtifacts.max_review_rounds: int | None` in `src/gobby/storage/tasks/_artifacts.py:72`); when NULL, `record_pr_verdict` falls back to `build_config.max_review_rounds` (default `3`, defined at `src/gobby/config/build.py:73`) via the existing Python-side `_cap()` fallback pattern (`src/gobby/dispatch/rules.py`'s `_maxed_out` helper which already binds `max_review_rounds` for `plan_review_attempts` and `test_arch_attempts` at `src/gobby/dispatch/rules.py:43,53`). Cap escalation uses the `attempt_count >= max_review_rounds` predicate (after fallback) evaluated AT `fail_stage` time (i.e., on the just-failed attempt's started count); when over the cap, `fail_stage` escalates instead of transitioning back to `ready`. No new column add or build_config field is required — `max_review_rounds` is the single canonical PR-review cap shared across plan_review, test_arch, and PR stages, matching the existing dispatcher pattern.
4. `pr.state` becomes `done` → `merge.state` transitions `ready → in_progress`.

`pr_rule` body:

```python
def pr_rule(task: Task, context: RuleContext) -> Action | None:
    stage = current_stage(task)
    if stage is None or stage.name != "pr":
        return None
    if stage.state != "in_progress":
        return None
    if not _has_pr_agent(context):
        return EscalateAction(task_id=task.id, reason="pr_no_agent")
    return _spawn_stage_agent(task, stage, context, "pr-agent")
```

`_has_pr_agent` checks the agent registry for a stage-aware `pr-agent`; if missing, escalates so #13552's owner can pick up the work.

**Acceptance:**

- 4.1.1 — `pr_rule` registered in the rules list at the right position. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_pr_rule.py::test_pr_rule_in_list`.
- 4.1.2 — `record_pr_verdict` with `verdict='approved'` completes `pr` stage and writes both artifacts. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_writes_artifacts`.
- 4.1.3 — `pr.state == done` triggers `merge.state == ready → in_progress` advance via the rule chain on next heartbeat. test: `tests/dispatch/test_pr_to_merge_advance.py::test_pr_done_advances_merge`.
- 4.1.4 — Without a registered `pr-agent`, `pr_rule` escalates with reason `pr_no_agent`. test: `tests/dispatch/test_pr_rule.py::test_escalates_when_no_agent`.
- 4.1.5 — PR rejection cap source is the existing `task_artifacts.max_review_rounds` column with Python-side fallback to `build_config.max_review_rounds` (default `3`); no new column add or build_config field is introduced for PR rejection caps. `record_pr_verdict` with `verdict='rejected'` (or `'needs_changes'`) calls `fail_stage('pr', reason=findings)`; under the cap (`attempt_count < max_review_rounds`) the stage transitions `in_progress → ready` and `is_escalated` stays `0`; at-or-over the cap (`attempt_count >= max_review_rounds`) `fail_stage` escalates with reason `pr_review_failed:max` and `is_escalated` flips to `1`. behavior: "PR cap binds to existing max_review_rounds with build_config fallback" verified in `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_under_cap_returns_to_ready`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_over_cap_escalates`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_null_artifact_falls_back_to_build_config`.

### 4.2 Merge stage rule + delivery artifacts [category: code] (depends: 4.1)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/mcp_proxy/tools/tasks/_stages.py`

Add `merge_rule`. Mirrors `pr_rule`'s escalate-without-agent fallback. The terminal close is NOT the merge rule's responsibility — it is owned by `StageStatesManager.complete_stage` per §2.1 invariant 8 / acceptance 2.1.9 (the generic manifest-exhausted close path). When `record_merge_result` calls `complete_stage('merge', commit_sha=merge_sha)`, the merge row becomes `done`, the manifest is exhausted, and `complete_stage` closes the task atomically in the same transaction. The merge stage uses the same generic close path as `research_spike`/`prd_doc`/`architecture_doc`; the only merge-specific behavior is the `commit_sha = merge_sha` argument and the artifact writes (`merge_commit_sha`, `merge_campaign_report`).

```python
def merge_rule(task: Task, context: RuleContext) -> Action | None:
    stage = current_stage(task)
    if stage is None or stage.name != "merge":
        return None
    if stage.state != "in_progress":
        return None
    if not _has_merge_agent(context):
        return EscalateAction(task_id=task.id, reason="merge_no_agent")
    return _spawn_stage_agent(task, stage, context, "merge-orchestrator")
```

`record_merge_result` tool (Phase 2.3 stub; full implementation here):

```python
def record_merge_result(
    task_id: str,
    *,
    merge_sha: str | None = None,
    report_ref: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Persist merge outcome and advance/fail the merge stage.

    Success path: merge_sha required. Writes merge_commit_sha and
    merge_campaign_report to task_artifacts; calls
    complete_stage('merge', commit_sha=merge_sha). Per §2.1 invariant 8 /
    acceptance 2.1.9 / 2.1.10, completing the highest-position manifest
    row (the merge row, which is terminal for merge-bearing manifests)
    atomically closes the task in the same DB transaction by calling
    `_close_task_in_txn(..., cascade_descendants=True, reason='manifest_exhausted', commit_sha=merge_sha)`.
    The cascade=True is passed because `stage_name == 'merge'` (per the
    round-7/-8 caller-cascade rule); this is the cascade-aware close that
    replaces the legacy `mark_task_merged` / `_cascade_merged_close`
    behavior. The public `close_task(...)` API is NOT invoked anywhere
    on this path — the merge close is `complete_stage` →
    `_close_task_in_txn` directly, with cascade enabled by the merge
    branch. There is NO merge-specific close call beyond the generic
    helper invocation.

    Failure path: failure_reason required. Writes merge_campaign_report;
    calls fail_stage('merge', reason=failure_reason). Per §2.1's contract,
    fail_stage transitions in_progress → ready WITHOUT changing attempt_count
    (the just-failed attempt is already counted by the start_stage that
    started it; the next retry's start_stage is the next increment). Cap
    escalation: fail_stage compares attempt_count to max_merge_attempts
    (read via Python-side fallback to build_config.max_merge_attempts when
    the artifact column is NULL); on attempt_count >= cap, fail_stage
    escalates with reason 'merge_failed:max' instead of transitioning back
    to ready. The cap predicate is `>=`, not `>`: a task that has started
    the merge stage `cap` times and just failed the cap-th attempt
    escalates without another retry.
    """
```

The cascade-close behavior from `mark_task_merged` (`src/gobby/storage/tasks/_transitions.py:661-680`) MUST be preserved and is reused via the §2.1 generic terminal-close path: `complete_stage(stage_name='merge', ...)` calls `_close_task_in_txn(..., cascade_descendants=True, ...)` per the round-7/-8 caller-cascade rule (acceptance 2.1.10). The cascade implementation lives ONCE in `_close_task_in_txn`; both the legacy `mark_task_merged` cascade behavior and any future cascade-needing terminal stage inherit from there. The public `close_task` API always passes `cascade_descendants=False` and is NOT invoked on the merge-close path — Phase 4.2 reaches `_close_task_in_txn` exclusively through `complete_stage`'s merge branch. No new close path is added by Phase 4.2; the merge close is one application of the §2.1 invariant with cascade=True.

`expansion_qa_rule`, `code_review_qa_rule`, `holistic_qa_rule` — each checks for its agent in the context and either spawns or escalates with `<stage>_no_agent`. These rules already exist in skeleton form from Phase 3.1; this section extends them to use the same `_has_<stage>_agent(context)` pattern as `pr_rule`/`merge_rule` so missing agents surface uniformly.

**Acceptance:**

- 4.2.1 — `merge_rule` registered in the rules list. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_merge_rule.py::test_merge_rule_in_list`.
- 4.2.2 — `record_merge_result(merge_sha=...)` writes `merge_commit_sha` + `merge_campaign_report`, then calls `complete_stage('merge', commit_sha=merge_sha)`. The §2.1 invariant 8 generic terminal-close path (acceptance 2.1.9 / 2.1.10) closes the task atomically in the same transaction by calling `_close_task_in_txn(..., reason='manifest_exhausted', commit_sha=merge_sha, cascade_descendants=True)`; cascade=True comes from the round-7/-8 caller-cascade rule that selects `stage_name == 'merge'`. Public `close_task(...)` is NOT invoked on this path. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_closes_task_via_terminal_close`, `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_close_uses_manifest_exhausted_reason_and_merge_sha`, `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_close_uses_cascade_descendants_true`, `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_does_not_invoke_public_close_task`.
- 4.2.3 — `record_merge_result(failure_reason=...)` fails the stage; over the cap, escalates. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_failure_path`.
- 4.2.4 — `expansion_qa_rule`, `code_review_qa_rule`, `holistic_qa_rule` all surface missing-agent escalations with stage-specific reason codes. test: `tests/dispatch/test_qa_rules_no_agent.py::test_each_qa_rule_escalates_specifically`.
- 4.2.5 — End-to-end stage chain `holistic_qa.done → pr.in_progress → pr.done → merge.in_progress → merge.done → task closed` walks correctly across heartbeats. test: `tests/dispatch/test_delivery_chain.py::test_full_delivery_chain`.

## P5 Task Type Expansion + Legacy Removal
`kind: framing`

**Goal**: Add the new task types, promote `is_escalated` to a first-class column, and rip out the legacy `lifecycle`/`status`/`lifecycle_stage` columns and the projection helpers. This phase closes the legacy model.

### 5.1 New task types + default-stages seed [category: code] (depends: 2.1, 2.2)
`kind: deliverable`

Target: `src/gobby/install/shared/registry/stages.yaml` (extension), `src/gobby/storage/tasks/_models.py`, `src/gobby/cli/tasks/crud.py`, `src/gobby/storage/migrations.py`

Add four new task types and their default-stages bundles. Migration version 235:

```python
NEW_TASK_TYPE_DEFAULTS = {
    "simple_fix":       ["development", "pr", "merge"],
    "research_spike":   ["ideation", "research", "prd"],            # terminal at prd, no merge
    "architecture_doc": ["research", "architecture"],               # terminal at architecture
    "prd_doc":          ["ideation", "prd"],                        # terminal at prd
}
```

Update `Task.task_type` validation in `_models.py` to accept the new values. The current inline comment (`src/gobby/storage/tasks/_models.py:163`) lists `bug, feature, task, epic, chore, refactor`; extend with the four new types. Add a `VALID_TASK_TYPES` module constant alongside existing validations (or inline as a frozenset literal — match nearby patterns).

Update `gobby tasks create --type <type>` Click choices in `src/gobby/cli/tasks/crud.py` to include the new types. Same for `TaskCreateRequest.task_type` validation in HTTP route models and the `create_task` MCP tool's `inputSchema`.

For research-terminal types (`research_spike`, `prd_doc`, `architecture_doc`): terminal-close is the §2.1 invariant 8 generic manifest-exhausted path (acceptance 2.1.9), NOT a Phase 4.2 dispatcher rule. When `complete_stage` is called on the highest-position row of any manifest (whatever its stage name — `prd` for research_spike/prd_doc, `architecture` for architecture_doc, `merge` for merge-bearing types), the task closes atomically in the same transaction with `reason='manifest_exhausted'`. There is no separate close path for research-terminal types. Add unit test fixtures for each: `research_spike` walks `ideation → research → prd → closed`; `prd_doc` walks `ideation → prd → closed`; `architecture_doc` walks `research → architecture → closed`.

**Acceptance:**

- 5.1.1 — Four new task types accepted by `Task.task_type` validation. file: `src/gobby/storage/tasks/_models.py`. test: `tests/storage/tasks/test_task_type_validation.py::test_new_types_accepted`.
- 5.1.2 — `task_type_default_stages` seeded with the four new defaults via migration 235. test: `tests/storage/test_migration_235.py::test_new_task_type_defaults`.
- 5.1.3 — CLI, HTTP, and MCP creation surfaces all accept the new types. test: `tests/cli/test_tasks_create_new_types.py::test_create_simple_fix`, `tests/servers/routes/test_tasks_create.py::test_post_simple_fix`, `tests/mcp_proxy/tools/tasks/test_create_task.py::test_simple_fix_type`.
- 5.1.4 — A `research_spike` task with manifest `[ideation, research, prd]` walks to `prd.done`; the §2.1 invariant 8 generic terminal-close path (acceptance 2.1.9) closes the task atomically in the same transaction with `reason='manifest_exhausted'`. The same path closes a `prd_doc` task at `prd.done` and an `architecture_doc` task at `architecture.done`. No Phase 4.2 dispatcher rule is involved. While the §1.3 placeholder agents remain disabled, the dispatcher escalates the `<discovery>.ready` row via `disabled_agent_escalation_rule` (acceptance 3.1.23) with reason `ideation_no_agent` / `research_no_agent` / `architecture_no_agent` / `prd_no_agent` — surfacing the gap loudly to operators rather than stalling silently. The terminal-close walks become reachable under dispatcher control once the real discovery agents ship (parent epic from §5.4); until then operators advance the discovery stages manually after clearing each escalation. test: `tests/dispatch/test_terminal_non_merge.py::test_research_spike_closes_at_prd`, `tests/dispatch/test_terminal_non_merge.py::test_prd_doc_closes_at_prd`, `tests/dispatch/test_terminal_non_merge.py::test_architecture_doc_closes_at_architecture`, `tests/dispatch/test_terminal_non_merge.py::test_research_spike_at_ideation_with_disabled_placeholder_escalates_with_ideation_no_agent`.

### 5.2 Wire `is_escalated` first-class column through dataclass + readers [category: code] (depends: 1.1, 2.2)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/tasks/state_semantics.py`

The column itself is created in migration 233 (Phase 1.1, on `tasks` directly) and backfilled from `escalated_at IS NOT NULL` in migration 234 (Phase 2.2 step 6). This deliverable wires the dataclass and read paths to use it; no migration is created here.

Rationale for placement: escalation is task-level, not artifact-level. `task_artifacts` is sparse evidence; `tasks` is the row that gets read on every list. Hosting `is_escalated` on `tasks` from migration 233 onward avoids the previous design's two-migration column-relocation churn.

Update `Task` dataclass: add `is_escalated: bool = False` field. Update every read site that currently calls `is_task_escalated(task)` (`src/gobby/tasks/state_semantics.py:98-105`) to read `task.is_escalated` directly. Keep the old helper only as a one-line `return task.is_escalated`; Phase 5.3 deletes it.

`escalate_task` and `de_escalate_task` (`src/gobby/storage/tasks/_transitions.py:397-457`) update `is_escalated` alongside `escalated_at` / `escalation_reason`. Atomic single transaction.

**Acceptance:**

- 5.2.1 — `Task.is_escalated` field present and populated on read from `tasks.is_escalated`. file: `src/gobby/storage/tasks/_models.py`. test: `tests/storage/tasks/test_task_dataclass.py::test_is_escalated_field`.
- 5.2.2 — `escalate_task` sets `is_escalated=1`; `de_escalate_task` sets `is_escalated=0`; both write `escalated_at` / `escalation_reason` in the same transaction. test: `tests/storage/tasks/test_transitions_is_escalated.py::test_escalate_round_trip`.
- 5.2.3 — Readers in dispatcher, projections, and HTTP responses use `task.is_escalated` directly. test: `tests/dispatch/test_is_escalated_first_class.py::test_no_helper_calls`.
- 5.2.4 — `escalate_task` and `de_escalate_task` do NOT touch `task_stage_states`; a task that escalates from `(stage='development', state='in_progress', attempt_count=2, entered_at=T)` and then de-escalates returns to the same row values exactly. behavior: "stage state survives escalate/de-escalate round-trip with attempt_count and entered_at preserved" verified in `tests/storage/tasks/test_escalation_preserves_stage.py::test_round_trip_preserves_row`.

### 5.3 Drop `lifecycle`, `lifecycle_stage`, active `status` semantics [category: code] (depends: 2.6, 3.1, 3.2, 3.3, 4.1, 4.2, 5.2, 6.3)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (migration 236), `src/gobby/storage/tasks/_models.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/storage/tasks/_crud.py` (round-11 F1: `create_task` / `update_task` lose `status` / `lifecycle` / `lifecycle_stage` parameters and column references), `src/gobby/storage/tasks/_manager.py` (round-11 F1: `LocalTaskManager` list/update/filter signatures drop legacy kwargs), `src/gobby/storage/tasks/_queries.py`, `src/gobby/tasks/state_semantics.py`, `src/gobby/storage/tasks/_lifecycle.py`, `src/gobby/tasks/expansion/_apply.py` (round-11 F1: `_complete_dev_only_run` ports its `UPDATE tasks SET lifecycle='in_development'` write to a `complete_stage(task_id, 'expansion')` call against the parent task's manifest), `src/gobby/sync/` task-sync paths (round-11 F1: JSONL exports drop `status` / `lifecycle` / `lifecycle_stage` keys), `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py`, `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`, `src/gobby/cli/tasks/crud.py`, `src/gobby/servers/routes/tasks.py`

Migration 236 drops legacy columns. Pre-flight: assert no rule, MCP tool, HTTP route, or CLI command writes `lifecycle`, `lifecycle_stage`, or active `status` values (Phases 2.6, 3, 4 must have completed) AND no web source reads them (Phase 6.3 must have completed). The migration runs in a transaction:

**Pre-drop web audit (load-bearing for cross-phase ordering — see F6 fix):** Phase 6.3 retires `KanbanBoard`, `taskState.ts` legacy types, and `TasksPage` kanban-branch reads of `state.lifecycle_stage`. This deliverable cannot ship until 6.3 has shipped because the web bundle would crash on a stripped column. The audit step (acceptance 5.3.8) runs a multi-pattern, legacy-only grep over `web/src/` (excluding the test files that intentionally regression-grep for absence) and a `pnpm tsc --noEmit` build; both must succeed (grep returns no source matches in non-test files, tsc compiles clean) before the migration is unblocked. The dependency chain is therefore: 6.1 → 6.2 → 6.3 → audit → 5.3 migration. Document order in the plan keeps phases 5 then 6 for readability, but task expansion respects the explicit `depends_on` annotation.

**Audit grep patterns (narrowed per F5 fix):** The grep MUST NOT use the bare token `lifecycle`, because the new Phase 6 board introduces intentional `LifecycleBoard` / `lifecycle-board` / `lifecycle-board:hide-blocked` identifiers and a broad token would false-positive on the new naming. The audit is a multi-pattern check that targets only legacy symbols. Use `git grep -nE` (or `grep -rnE` rooted at `web/src/`) with each of the patterns below combined as alternation in a single invocation:

- `\blifecycle_stage\b` — the legacy task field name
- `\bLifecycle\.` — legacy enum-style member access (matches `Lifecycle.Open`, `Lifecycle.PlanReview`, etc.; does NOT match `LifecycleBoard` because the next char is `B`, not `.`)
- `\bTaskBucket\b` — legacy bucket type
- `\bTASK_BUCKET_(LABELS|ORDER)\b` — legacy bucket constants
- `\bmoveTaskToBucket\b` — legacy mover function
- `\bgetTaskBucket\b` — legacy getter
- `\bKanbanBoard\b` — the legacy board component name (does NOT match `LifecycleBoard`)
- `\.lifecycle_stage\b` — direct field reads on task state
- `\bstate\.lifecycle\b` (without `_stage`) — legacy projection reads (does NOT match `state.lifecycle_stage`, which is caught by the first pattern, and does NOT match `LifecycleBoard`-related identifiers)

The combined invocation, expressed as a single shell command for CI:

```bash
git grep -nE '\blifecycle_stage\b|\bLifecycle\.|\bTaskBucket\b|\bTASK_BUCKET_(LABELS|ORDER)\b|\bmoveTaskToBucket\b|\bgetTaskBucket\b|\bKanbanBoard\b|\.lifecycle_stage\b|\bstate\.lifecycle\b' -- 'web/src/' ':(exclude)web/src/**/*test_legacy_symbols_removed*' ':(exclude)web/src/**/*lifecycle-board-css-lint*'
```

The audit passes iff this command returns zero matches (exit 1 / no output for `git grep`). New `LifecycleBoard`, `StageColumn`, `StageCard`, `lifecycle-board.css`, and `lifecycle-board:hide-blocked` identifiers introduced in Phase 6.1/6.2/6.3 are intentionally NOT matched by any of the patterns above (verified via the patterns' word-boundary anchors and the `\bLifecycle\.` pattern requiring a literal `.` after `Lifecycle`).

1. `ALTER TABLE tasks DROP COLUMN lifecycle;`
2. `ALTER TABLE tasks DROP COLUMN lifecycle_stage;`
3. `ALTER TABLE tasks DROP COLUMN status;`

The `status` column drop is unconditional, matching the strategy plan's clean-cutover constraint (no shadow model, no compatibility shim). Pre-flight audit (covered by acceptance 5.3.9) identifies every remaining reader/writer of `tasks.status` in runtime code (MCP tools, HTTP routes, CLI commands, dispatcher rules, projections, web bundle); each is ported in THIS SAME deliverable to: `closed_at IS NOT NULL` for closure checks, `tasks.is_escalated` (Phase 5.2 column) for escalation, and stage-state reads (`current_stage(task).name/state`) for everything else. The migration is blocked until the audit returns zero hard readers; the audit is a tooling step that runs before the `ALTER TABLE` statements execute (failing the migration with a named-readers diagnostic if any remain).

`Task.status` Literal field is removed unconditionally from `_models.py`; `serialize_task_state` strips `status` from the response shape (only `current_stage`, `is_closed`, `is_escalated`, `is_blocked`, `owner_session_id` remain). MCP/HTTP responses that previously surfaced `status` strings now surface `current_stage.name` plus `current_stage.state` plus the boolean flags. The `is_closed` projection (already a Python derivation at `state_semantics.py:88-95`) keeps reading `closed_at IS NOT NULL` post-drop — the helper's `OR status == 'closed'` clause is removed when the column is dropped (one-line change in `state_semantics.py`).

Tools and helpers to delete (after final rule-rewrite checks):

- `mark_task_pr_opened` (storage `_transitions.py:642-658`, MCP `_lifecycle_merge.py:23-34`)
- `mark_task_merged` (storage `_transitions.py:661-680`, MCP `_lifecycle_merge.py:60-80`)
- `mark_task_merge_failed` (storage `_transitions.py:683-726` area, MCP `_lifecycle_merge.py:115-138`)
- `advance_lifecycle` (`_transitions.py:220-286`)
- `Lifecycle` StrEnum (`_models.py:42-51`)
- `TaskLifecycleStage` Literal (`state_semantics.py:7`)
- `lifecycle_stage_from_status` (`state_semantics.py:45-49`)
- `normalize_lifecycle_stage` (`state_semantics.py:52-63`)
- `project_legacy_status` (`state_semantics.py:66-85`)
- `_coerce_task_lifecycle_stage` (`state_semantics.py:175-192`)
- `serialize_task_state` returns no `lifecycle_stage` field; rewrite to expose `current_stage`, `is_closed`, `is_escalated`, `is_blocked`, `owner_session_id` only.

CLI flag removals: `gobby tasks list --status` and `--lifecycle` are deleted (Phase 2.5 added their replacements; the old flags are now removed). HTTP filter param removals: `?status=...` and `?lifecycle=...` query params are deleted from the list endpoint.

`mark_task_review_approved`, `mark_task_review_rejected`, `mark_task_needs_review` MCP tools — Phase 2.6 already rewired these to stage-native `complete_stage` / `fail_stage` calls before the dispatcher cutover. This deliverable retains acceptance 5.3.7's pre-rewire call-site audit (now reframed as a post-rewire audit: confirm no remaining caller writes legacy `status` values like `'review_approved'`/`'needs_review'`/`'rejected'` for these review transitions). Any stragglers found by the audit are removed here so migration 236 can drop the active `status` enum values cleanly.

`escalated` is preserved as a first-class column (Phase 5.2's `tasks.is_escalated`). `closed` is no longer represented by a `status` value at all; closure is `closed_at IS NOT NULL` (canonical SQL) and `task.is_closed` (Python projection). The `status` column drop in step 3 above is unconditional.

**Acceptance:**

- 5.3.1 — Migration 236 drops `lifecycle`, `lifecycle_stage`, AND `status` from `tasks` in a single transaction. The `status` drop is unconditional (no audit-gated optionality); the pre-flight audit covered by 5.3.9 must pass before the migration proceeds. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_236_drop_legacy.py::test_lifecycle_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_lifecycle_stage_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_status_column_dropped`.
- 5.3.2 — `Lifecycle` StrEnum, `TaskLifecycleStage` Literal, and the projection helpers in `state_semantics.py` are deleted. file: `src/gobby/tasks/state_semantics.py`, `src/gobby/storage/tasks/_models.py`. test: grep-based regression `tests/test_legacy_symbols_removed.py::test_no_lifecycle_imports`.
- 5.3.3 — Legacy lifecycle MCP tools (`mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`, `advance_lifecycle`) are removed. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py`, `src/gobby/storage/tasks/_transitions.py`. test: `tests/mcp_proxy/tools/tasks/test_legacy_tools_removed.py::test_tools_absent`.
- 5.3.4 — Post-rewire verification: `mark_task_review_approved` / `mark_task_review_rejected` / `mark_task_needs_review` (rewired in Phase 2.6) write no legacy `status` values; their stage-native paths are the only path. test: `tests/storage/tasks/test_review_tools_no_legacy_writes.py::test_no_status_writes_after_rewire`.
- 5.3.5 — CLI `--status`/`--lifecycle` flags and HTTP `?status=`/`?lifecycle=` filters are removed. test: `tests/cli/test_legacy_flags_removed.py::test_status_flag_unknown`, `tests/servers/routes/test_legacy_filters_removed.py::test_status_filter_400`.
- 5.3.6 — `serialize_task_state` returns the new shape without `lifecycle_stage`. file: `src/gobby/tasks/state_semantics.py`. test: `tests/tasks/test_serialize_task_state.py::test_new_shape`.
- 5.3.7 — Post-rewire call-site audit: every existing caller of `mark_task_review_approved`, `mark_task_review_rejected`, and `mark_task_needs_review` (rewired in Phase 2.6) is invoked from a context where `current_stage.name IN {planning, adversarial_review, test_arch, expansion_qa, code_review_qa, holistic_qa}`. The audit grep is `grep -rln 'mark_task_review_\(approved\|rejected\)\|mark_task_needs_review' src/gobby/install/shared/`; the resulting caller list MUST be a subset of the allowlist named in §2.6.6 plus any caller added during the §2.6 → §5.3 window. The allowlist explicitly includes `test-architect.yaml` (test_arch stage), `expansion-qa.yaml` (expansion_qa stage), `requirements-analyst.yaml`, `qa-dev.yaml`, `nightly-linter.yaml`, `nightly-test-fixer.yaml`, `backend-developer.yaml`, `frontend-developer.yaml`, `default.yaml`, `developer.yaml`, `merge-orchestrator.yaml`, the bundled SKILL.md instruction surfaces, and the rule YAMLs (same list as §2.6.6) — closing the F2 gap where `test-architect.yaml` and several others were previously omitted. Off-stage callers (if any surface) are updated to call `complete_stage` / `fail_stage` directly with an explicit stage name rather than relying on the rewired tool's "advance current stage" behavior. This prevents silent behavior drift now that the tool surface no longer writes legacy `status` values. test: `tests/storage/tasks/test_review_tools_call_site_audit.py::test_no_off_stage_callers`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_allowlist_includes_test_architect_and_expansion_qa`.
- 5.3.8 — Pre-drop web audit: the multi-pattern, legacy-only grep documented in the §5.3 narrative ("Audit grep patterns") returns zero source matches across `web/src/` (excluding the legacy-removal regression tests `test_legacy_symbols_removed.test.ts` and the CSS lint test `lifecycle-board-css-lint.test.ts`); `pnpm tsc --noEmit` compiles clean against the post-Phase-6.3 web bundle; running this audit before the migration runs is enforced by a CI step gating migration 236. The grep MUST NOT use the bare token `lifecycle` (would false-positive on `LifecycleBoard`, `lifecycle-board`, and `lifecycle-board:hide-blocked` introduced by Phase 6); the patterns are anchored on `\blifecycle_stage\b`, `\bLifecycle\.`, `\bTaskBucket\b`, `\bTASK_BUCKET_(LABELS|ORDER)\b`, `\bmoveTaskToBucket\b`, `\bgetTaskBucket\b`, `\bKanbanBoard\b`, `\.lifecycle_stage\b`, and `\bstate\.lifecycle\b` (full single-shell-line invocation in the §5.3 narrative). behavior: "web bundle has no legacy reads before column drop" verified in `tests/migrations/test_pre_drop_web_audit.py::test_no_legacy_web_reads` (the test executes the documented `git grep -nE` command and asserts zero output); the new `LifecycleBoard` family of identifiers is asserted to be ignored by the patterns in `tests/migrations/test_pre_drop_web_audit.py::test_grep_does_not_match_new_lifecycle_board_identifiers`. Both tests carry `pytest.mark.integration` for local runs and execute unconditionally in CI.
- 5.3.9 — Pre-`status`-drop audit-and-port: every remaining runtime reader/writer of `tasks.status`, `tasks.lifecycle`, and `tasks.lifecycle_stage` is identified and ported in this deliverable to one of the post-cutover sources (`closed_at IS NOT NULL` for closure, `tasks.is_escalated` for escalation, `current_stage(task)` / `task.stages` for everything else) BEFORE migration 236 executes. The audit is multi-pattern, scoped to task-state code (`src/gobby/storage/tasks/`, `src/gobby/tasks/`, `src/gobby/sync/`, `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/cli/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/`; explicitly excludes `src/gobby/storage/migrations.py` and `tests/storage/test_migration_*.py`). Patterns matched: (a) comparisons of `.status` / `.lifecycle` / `.lifecycle_stage` against legacy enum string literals (`open`, `in_progress`, `needs_review`, `review_approved`, `escalated`, `closed`, `plan_review`, `expanding`, `in_development`, `holistic_review`, `pr`, `merging`, `merged`, `test_arch`); (b) unqualified SQL inserts/updates referencing legacy columns: `INSERT INTO tasks (... status ...)`, `INSERT INTO tasks (... lifecycle ...)`, `INSERT INTO tasks (... lifecycle_stage ...)`, `status = ?`, `lifecycle = ?`, `lifecycle_stage = ?`; (c) function-parameter usages on task CRUD/list/update APIs declaring `status:` / `lifecycle:` / `lifecycle_stage:` typed parameters; (d) JSONL/sync export key emissions for `status` / `lifecycle` / `lifecycle_stage`; (e) **(round-12 F1)** dynamic-write sources where dict construction places legacy columns into a write-bound dict: literal dict keys `'status':`, `'lifecycle':`, `'lifecycle_stage':` inside `synced_values`-style dicts that flow into dynamically-built `INSERT INTO tasks ({columns})` or `UPDATE tasks SET {set_clause}` SQL. Pattern (e) is scoped to task-sync code (`src/gobby/sync/`) and task-CRUD (`src/gobby/storage/tasks/`) to avoid false-positives against unrelated `status` keys (validation status, run status, workflow status, etc.). The scoping by directory ensures unrelated tables (workflows, sessions) with their own `status` columns do not false-positive — those directories are excluded from the audit. The named runtime ports include: `_crud.py::create_task` and `update_task` lose legacy column references; `_apply.py::_complete_dev_only_run` ports to `complete_stage(task_id, 'expansion')`; `LocalTaskManager` list/update/filter signatures lose `status` / `lifecycle` kwargs; task sync EXPORT drops legacy keys; **(round-12 F1)** `src/gobby/sync/tasks.py::TaskSyncManager.import_from_jsonl` ports its dynamic write path: stops reading `lifecycle_stage` from `tasks` rows, stops recognizing top-level JSONL `status` / `lifecycle_stage` keys, removes `status` / `lifecycle_stage` from `synced_values` dict construction, so the dynamically-built `INSERT INTO tasks ({columns})` and `UPDATE tasks SET {set_clause}` no longer reference dropped columns. Audit returns zero matches outside the historical-migration boundary post-port. The MCP `get_task` / `list_tasks` response shape no longer includes `status` / `lifecycle` / `lifecycle_stage`; the HTTP `/api/tasks` GET endpoints no longer include those keys; the CLI `gobby tasks list` output no longer includes those columns; task JSONL exports AND imports no longer carry/process those keys. test: `tests/storage/test_migration_236_drop_legacy.py::test_legacy_column_audit_grep_returns_zero_runtime_matches`, `tests/storage/test_migration_236_drop_legacy.py::test_dynamic_dict_write_audit_returns_zero_matches`, `tests/storage/tasks/test_crud_no_legacy_columns.py::test_create_task_no_status_param`, `tests/storage/tasks/test_crud_no_legacy_columns.py::test_update_task_no_lifecycle_param`, `tests/tasks/expansion/test_apply_dev_only.py::test_complete_dev_only_run_via_complete_stage`, `tests/sync/test_task_jsonl_export_shape.py::test_no_legacy_keys`, `tests/sync/test_task_jsonl_import_shape.py::test_import_does_not_write_legacy_columns`, `tests/sync/test_task_jsonl_import_shape.py::test_import_ignores_top_level_legacy_keys`, `tests/mcp_proxy/tools/tasks/test_get_task_response_shape.py::test_no_legacy_fields`, `tests/servers/routes/test_tasks_list_response_shape.py::test_no_legacy_fields`, `tests/cli/test_tasks_list_columns.py::test_no_legacy_columns`.

### 5.4 Discovery-stage agent follow-up tracking [category: manual] (depends: 1.3)
`kind: deliverable`

Target: gobby-tasks (no source files; this deliverable creates real tracked tasks via the gobby-tasks MCP server during plan execution)

The four placeholder agents from 1.3 are explicit shims; real implementations are out of scope for this epic. Create one parent epic plus four child tracking tasks so the work is visible in the task tree, picked up by future planning sessions, and re-discoverable from the registry's `default_agent` slot.

Parent epic:

```text
title: "Discovery-stage agent registry"
task_type: epic
category: planning
priority: 2
labels:
  - "deferred-from:task-13482-stage-manifest-cutover:5.4"
description: |
  Owns the four discovery-stage agents (analyst, researcher, architect,
  product-manager) shipped as disabled placeholders by the stage-manifest
  cutover (#13482). Each child task replaces one placeholder YAML with a real
  implementation. Stages affected:

  - ideation     → analyst         (placeholder at src/gobby/install/shared/workflows/agents/analyst.yaml)
  - research     → researcher      (placeholder at src/gobby/install/shared/workflows/agents/researcher.yaml)
  - architecture → architect       (placeholder at src/gobby/install/shared/workflows/agents/architect.yaml)
  - prd          → product-manager (placeholder at src/gobby/install/shared/workflows/agents/product-manager.yaml)

  Acceptance for the parent: every child closed; every placeholder YAML
  replaced with `enabled: true` real impl; `tests/dispatch/test_no_agent_paths.py`
  no longer needs the placeholder fixture.
```

Each of the four children has the shape:

```text
title:       "Implement <agent-slug> agent for <stage> stage"
task_type:   feature
category:    planning
priority:    3
parent_task_id: <parent epic ref>
labels:
  - "deferred-from:task-13482-stage-manifest-cutover:5.4"
  - "agent-followup:<agent-slug>"
description: |
  Replace the disabled placeholder at
  `src/gobby/install/shared/workflows/agents/<agent-slug>.yaml` with a real
  agent implementation. Acceptance:

  - YAML has `enabled: true` and a real `instructions` block.
  - PLACEHOLDER banners removed.
  - Stage `<stage>` no longer escalates with reason
    `<stage>_no_agent` or `placeholder_agent:<slug>:not_implemented` when
    a task reaches it.
  - At least one fixture or e2e test exercises the agent end-to-end.
```

The four `(stage, agent-slug)` pairs to create:

| Stage | Agent slug |
|-------|-----------|
| `ideation` | `analyst` |
| `research` | `researcher` |
| `architecture` | `architect` |
| `prd` | `product-manager` |

Implementation note for the executing agent: use the `create_task` MCP tool on `gobby-tasks-ops` for each task. Create the parent first, capture its ref/id, then create each child with `parent_task_id` set. Apply labels via `add_label` (or include in initial creation if the schema supports it). Verify all five tasks land in the same project (`d45545c5-ded5-4335-b115-0245752edacf`) and surface the parent ref to the operator on completion.

This deliverable does **not** open or implement any of the agents — only creates the tracking tasks. Real implementation work happens in later planning rounds spawned from the new parent epic.

Test pattern for 5.4.1–5.4.4: tests query the live gobby-tasks DB to verify seeded tasks exist. Mark each test with `@pytest.mark.integration` and gate behind a fixture that calls `gobby-tasks:list_tasks(label="agent-followup:")` once per test session — if the result is empty, `pytest.skip("agent-followup tasks not yet seeded; run deliverable 5.4 first")`. This prevents flakiness when the test file runs before the deliverable has executed (e.g., during a fresh checkout test run) without losing the value of the post-execution check.

**Acceptance:**

- 5.4.1 — One parent epic exists in gobby-tasks titled `Discovery-stage agent registry` with the declared labels and description. behavior: "epic exists with deferred-from label and references all four placeholders" verified in `tests/dispatch/test_agent_followup_tasks.py::test_parent_epic_exists` (post-execution fixture seeded by the executing agent).
- 5.4.2 — Four child tasks exist under the parent, one per `(stage, agent-slug)` pair, each carrying the `agent-followup:<slug>` and `deferred-from:` labels. test: `tests/dispatch/test_agent_followup_tasks.py::test_four_children_with_labels`.
- 5.4.3 — Every child task references the exact placeholder YAML path in its description. behavior: "each child description names src/gobby/install/shared/workflows/agents/<slug>.yaml verbatim" verified in `tests/dispatch/test_agent_followup_tasks.py::test_descriptions_reference_placeholders`.
- 5.4.4 — Children are open (`is_closed=false`) so future planning rounds can pick them up. test: `tests/dispatch/test_agent_followup_tasks.py::test_children_open`.

Per-row coverage (one acceptance per data row of the §5.4 Stage|Agent-slug table, per the plan-coverage contract's table-row decomposition rule):

- 5.4.5 — Stage `ideation` → agent slug `analyst`: a child task exists under the parent epic with title `Implement analyst agent for ideation stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:analyst` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/analyst.yaml` verbatim. behavior: "ideation/analyst follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_ideation_analyst`.
- 5.4.6 — Stage `research` → agent slug `researcher`: a child task exists under the parent epic with title `Implement researcher agent for research stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:researcher` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/researcher.yaml` verbatim. behavior: "research/researcher follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_research_researcher`.
- 5.4.7 — Stage `architecture` → agent slug `architect`: a child task exists under the parent epic with title `Implement architect agent for architecture stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:architect` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/architect.yaml` verbatim. behavior: "architecture/architect follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_architecture_architect`.
- 5.4.8 — Stage `prd` → agent slug `product-manager`: a child task exists under the parent epic with title `Implement product-manager agent for prd stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:product-manager` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/product-manager.yaml` verbatim. behavior: "prd/product-manager follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_prd_product_manager`.

## P6 Web UI — LifecycleBoard
`kind: framing`

**Goal**: Replace the 6-bucket `KanbanBoard` with a stage-manifest-driven `LifecycleBoard`. After Phase 6, the kanban view renders one column per registry stage with tri-state badges per task and drag-to-advance hooked into `PATCH /api/tasks/{id}/stages/{name}`.

**Design prerequisite (every Phase 6 deliverable)**: before producing or modifying any UI surface, the implementing agent MUST call `get_skill(name="impeccable")` on `gobby-skills` and read `.impeccable.md` at the project root (per CLAUDE.md "Design Context"). All visual decisions — column layout, tri-state visualization, badge palette, swimlane styling, drag-feedback animations, blocked-overlay styling, focus rings, type ramp — must conform to that skill's deutan-safe color constraints, WCAG 2.2 AA contrast targets, aesthetic references, and the per-surface variation rules for `./web/`. Freehand color, typography, or spacing choices are not permitted; if the skill is silent on a specific case, surface the gap to the operator rather than guess.

### 6.1 New `LifecycleBoard.tsx` + `StageColumn.tsx` + `StageCard.tsx` [category: code] (depends: 2.4)
`kind: deliverable`

Target: `web/src/components/tasks/LifecycleBoard.tsx`, `web/src/components/tasks/StageColumn.tsx`, `web/src/components/tasks/StageCard.tsx` (all new)

Replicate the props pattern of `KanbanBoard` (`web/src/components/tasks/KanbanBoard.tsx`) but driven by registry stages instead of fixed buckets. Use the existing `@atlaskit/pragmatic-drag-and-drop` library — it's already wired (`web/package.json` v1.7.7) and handles draggable cards + drop targets in `KanbanBoard`.

```typescript
// LifecycleBoard.tsx
interface LifecycleBoardProps {
  tasks: GobbyTask[]
  registry: StageRegistryEntry[]
  onSelectTask: (id: string) => void
  onAdvanceStage?: (taskId: string, stageName: string) => void
  onFailStage?: (taskId: string, stageName: string, reason: string) => void
}

export function LifecycleBoard({
  tasks, registry, onSelectTask, onAdvanceStage, onFailStage,
}: LifecycleBoardProps) {
  // Filter columns to those any visible task has in its manifest (configurable
  // via showAllColumns prop; default off).
  const visibleStages = useMemo(
    () => registry.filter(s => tasks.some(t => t.stages?.some(r => r.stage_name === s.name))),
    [tasks, registry],
  )
  return (
    <div className="lifecycle-board">
      {visibleStages.map(stage => (
        <StageColumn
          key={stage.name}
          stage={stage}
          tasks={tasks.filter(t => taskAtStage(t, stage.name))}
          onSelectTask={onSelectTask}
          onAdvanceStage={onAdvanceStage}
        />
      ))}
    </div>
  )
}
```

```typescript
// StageColumn.tsx
function StageColumn({ stage, tasks, ... }: StageColumnProps) {
  // Group tasks by tri-state within the column.
  const grouped = {
    ready: tasks.filter(t => taskStateAt(t, stage.name) === 'ready'),
    in_progress: tasks.filter(t => taskStateAt(t, stage.name) === 'in_progress'),
    done:        tasks.filter(t => taskStateAt(t, stage.name) === 'done'),
  }
  // ready top (pale), in_progress middle (accent), done bottom (collapsed
  // by default via toggle).
}
```

```typescript
// StageCard.tsx
function StageCard({ task, stageName, columnState, ... }: StageCardProps) {
  const isBlocked = task.state?.is_blocked
  // Blocked tasks render with a blocked badge/overlay; they stay in their
  // current stage column and do NOT move to a synthetic blocked column.
  // The badge shows whether the block is from an open dependency or from
  // is_escalated, with a tooltip naming the blocker.
  // Drag right = onAdvanceStage(task.id, stageName); blocked tasks are
  // drag-disabled (the badge tooltip explains why) so the user can't accidentally
  // advance over a blocker.
}
```

Blocked-task default visibility: blocked tasks are **shown by default** in their current stage column. A "Hide blocked" toggle in the board toolbar removes them from the rendered set; toggle state persists per user via `localStorage` keyed by `lifecycle-board:hide-blocked`. The default is "show" because the kanban's primary value is workflow visibility — `list_ready_tasks` and `suggest_next_task` already serve the actionable-only view for users who want filtered "what should I work on" output. Hiding by default risks a silently-growing stalled backlog, which the visibility-first design avoids.

Stage state and blocked-ness are orthogonal projections (see Constraints): a task can be `(stage='development', state='ready', is_blocked=true)` or `(stage='development', state='in_progress', is_blocked=true)`, and these mean meaningfully different things. The card's column position tells you the pipeline stage; the tri-state group within the column tells you work progress; the blocked badge tells you about external blockers. None of these axes collapses into the others.

Helpers (in `web/src/lib/taskState.ts` — extending it; Phase 6.3 retires the legacy parts):

```typescript
export function taskAtStage(task: GobbyTask, stageName: string): boolean {
  return task.stages?.some(r => r.stage_name === stageName) ?? false
}

export function taskStateAt(task: GobbyTask, stageName: string): StageRowState | undefined {
  return task.stages?.find(r => r.stage_name === stageName)?.state
}

export function currentStage(task: GobbyTask): { name: string; state: StageRowState } | null {
  // Leftmost row by position whose state != 'done'.
}
```

Swimlanes by `task_type`: render one row per distinct `task_type` in the visible task set. Within each lane, render the columns. Empty lanes are hidden.

The `done` group within each column collapses by default to one summary row showing the count; click to expand. Reuses the `details/summary` HTML pattern or a small toggle component — match nearby disclosure patterns in `web/src/components/`.

**Acceptance:**

- 6.1.1 — Three new components exist with the declared prop shapes. file: `web/src/components/tasks/LifecycleBoard.tsx`, `web/src/components/tasks/StageColumn.tsx`, `web/src/components/tasks/StageCard.tsx`. symbol: `LifecycleBoard`, `StageColumn`, `StageCard`.
- 6.1.2 — Columns render only the stages present in any visible task's manifest. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_visible_stage_filtering`.
- 6.1.3 — Tri-state grouping renders within each column with `done` collapsed by default. test: `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_tri_state_grouping`.
- 6.1.4 — Blocked tasks render in their current column with a blocked badge by default; the badge tooltip names the blocker (open upstream dep or escalation reason). test: `web/src/components/tasks/__tests__/StageCard.test.tsx::test_blocked_badge_default_visible`.
- 6.1.4a — A "Hide blocked" toolbar toggle removes blocked tasks from the rendered set; toggle state persists in `localStorage['lifecycle-board:hide-blocked']`. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_hide_blocked_toggle_persists`.
- 6.1.4b — Drag-to-advance is disabled on blocked cards; attempting to drag surfaces the badge tooltip. test: `web/src/components/tasks/__tests__/StageCard.test.tsx::test_blocked_drag_disabled`.
- 6.1.5 — Drag right on a card calls `onAdvanceStage(task.id, stageName)`. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_drag_advance`.
- 6.1.6 — Swimlanes by `task_type` render with empty lanes hidden. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_swimlanes`.
- 6.1.7 — Category filter (toolbar control) hides columns whose `task_stages_registry.category` is not in the active selection; deselecting all categories renders no columns. behavior: "category filter drives column visibility" verified in `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_category_filter_hides_columns`.
- 6.1.8 — Visual surfaces (column header chrome, tri-state palette, badge ramp, blocked overlay, swimlane dividers, drag-preview shadow) conform to the `impeccable` skill's tokens; `.impeccable.md` was consulted before authoring CSS/JSX. behavior: "design tokens consumed from impeccable" verified by code review notes in PR description and `web/src/styles/lifecycle-board.css` referencing only token variables (no raw hex values).
- 6.1.9 — Automated CSS lint test asserts that `web/src/styles/lifecycle-board.css` (and any sibling stylesheet introduced for `LifecycleBoard`/`StageColumn`/`StageCard`) contains no raw color literals — the test fails on any line outside a CSS comment matching `/#[0-9a-fA-F]{3,8}\b/` or RGB/HSL function form with literal numeric channels. Enforces token-only authoring without relying on PR-description review. file: `web/src/styles/lifecycle-board.css`. test: `web/src/__tests__/lifecycle-board-css-lint.test.ts::test_no_raw_color_literals`.

### 6.2 useTasks denormalized stage manifest + new filters [category: code] (depends: 2.4, 6.1)
`kind: deliverable`

Target: `web/src/hooks/useTasks.ts`, `web/src/hooks/useStagesRegistry.ts` (new)

Extend `GobbyTask` (`web/src/hooks/useTasks.ts:10-45` area) with:

```typescript
export interface StageStateView {
  stage_name: string
  position: number
  state: 'ready' | 'in_progress' | 'done'
  attempt_count: number
  artifact_refs: Record<string, string> | null
}

export interface GobbyTask {
  // existing fields...
  stages?: StageStateView[]  // populated by GET /api/tasks?include_stages=1
}
```

Update `fetchTasks` in `useTasks` to pass `include_stages=1` whenever the kanban view is mounted. Add `stage` and `stage_state` query params to `buildParams` for filtered fetches. Mutation helpers gain:

```typescript
async function advanceStage(taskId: string, stageName: string): Promise<void> {
  await fetch(`${baseUrl}/api/tasks/${taskId}/stages/${stageName}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'complete' }),
  })
}

async function failStage(taskId: string, stageName: string, reason: string): Promise<void> { ... }
async function startStage(taskId: string, stageName: string): Promise<void> { ... }
```

New hook `useStagesRegistry` fetches `GET /api/stages/registry` once on mount, caches in module-level state, and returns `{registry, isLoading, error}`.

WebSocket `task_event` handler (`useTasks` line ~280-295 area) re-fetches on `stage_changed` events as well as existing `task_event` types. Backend already broadcasts these per 2.4.5.

**Acceptance:**

- 6.2.1 — `GobbyTask.stages` field populated when `include_stages=1` query param is set. file: `web/src/hooks/useTasks.ts`. test: `web/src/hooks/__tests__/useTasks.test.ts::test_stages_populated`.
- 6.2.2 — `advanceStage`, `failStage`, `startStage` mutators call the correct PATCH endpoints. test: `web/src/hooks/__tests__/useTasks.test.ts::test_stage_mutators`.
- 6.2.3 — `useStagesRegistry` fetches once and caches. file: `web/src/hooks/useStagesRegistry.ts`. test: `web/src/hooks/__tests__/useStagesRegistry.test.ts::test_caches_response`.
- 6.2.4 — `stage_changed` WS events trigger task re-fetch. test: `web/src/hooks/__tests__/useTasks.test.ts::test_ws_stage_changed_refetches`.

### 6.3 Mount LifecycleBoard, retire `taskState.ts` legacy types [category: code] (depends: 6.1, 6.2)
`kind: deliverable`

Target: `web/src/components/tasks/TasksPage.tsx`, `web/src/lib/taskState.ts`, `web/src/components/tasks/KanbanBoard.tsx`, `web/src/components/tasks/__tests__/KanbanBoard.test.tsx`

Replace the `viewMode === 'kanban'` branch in `TasksPage.tsx` (around line 601 — search the literal `viewMode === 'kanban'`) with `LifecycleBoard`. Pass `tasks`, the registry from `useStagesRegistry`, and the new `advanceStage`/`failStage` mutators from `useTasks`.

```typescript
} : viewMode === 'kanban' ? (
  <LifecycleBoard
    tasks={subtreeRootId ? kanbanTasks : displayTasks}
    registry={registry}
    onSelectTask={setSelectedTaskId}
    onAdvanceStage={advanceStage}
    onFailStage={failStage}
  />
) : ...
```

Delete from `web/src/lib/taskState.ts`:

- `TaskLifecycleStage` (line ~1)
- `TaskBucket` (lines ~28-34)
- `TASK_BUCKET_LABELS` (lines ~48-55)
- `TASK_BUCKET_ORDER` (lines ~36-46)
- `getTaskBucket` (lines ~163-173)
- `_resolveLifecycleStage` and any other lifecycle helpers

Keep `CanonicalTaskState` minus the `lifecycle_stage` field; rename to make the omission obvious if useful (`TaskState` works). Keep `getCanonicalTaskState` for reading task state for badges.

Delete `web/src/components/tasks/KanbanBoard.tsx` and its test file `KanbanBoard.test.tsx`. The new `LifecycleBoard` test file from 6.1 replaces them.

The `moveTaskToBucket` function in `TasksPage.tsx` (around line 372 — search the literal `function moveTaskToBucket`) is replaced by inline `advanceStage` / `failStage` calls bound to drag handlers in `LifecycleBoard`. Existing per-bucket transition functions (`reopenTask`, `deEscalateTask`, `claimTask`, `markTaskNeedsReview`, `markTaskReviewApproved`, `escalateTask`, `closeTask`) are no longer wired to drag — their other callers stay (sidebar buttons, modals, etc.).

**Acceptance:**

- 6.3.1 — `TasksPage.tsx` mounts `LifecycleBoard` for `viewMode === 'kanban'`. file: `web/src/components/tasks/TasksPage.tsx`. test: `web/src/components/tasks/__tests__/TasksPage.test.tsx::test_kanban_mode_renders_lifecycle_board`.
- 6.3.2 — `taskState.ts` legacy symbols deleted. file: `web/src/lib/taskState.ts`. test: TypeScript compile passes; grep regression `web/src/__tests__/test_legacy_symbols_removed.test.ts::test_no_task_bucket_imports`.
- 6.3.3 — `KanbanBoard.tsx` and `KanbanBoard.test.tsx` are deleted. behavior: "old kanban component absent" verified by `git status`/`grep -r "KanbanBoard"` returning no source matches in `web/src/`.
- 6.3.4 — `pnpm build` succeeds; `pnpm test` runs `LifecycleBoard.test.tsx` instead of the deleted `KanbanBoard.test.tsx`. test: CI pipeline output shows new file in coverage.

## P7 Cleanup
`kind: framing`

**Goal**: Remove deprecated `stage-:<name>` label handling, temporary migration helpers, and dead lifecycle/status code. Documentation pass.

### 7.1 Remove `stage-:<name>` label handling and migration helpers [category: refactor] (depends: 5.3, 6.3)
`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/dispatch/rules.py`, `src/gobby/storage/migrations.py`, anywhere else `stage-:` appears

Grep `stage-:` across the codebase. Every read site that interpreted these labels (build profile resolution, dispatcher skip checks, CLI/HTTP introspection) must be deleted; the data was migrated to `task_stage_states` in Phase 2.2 and the labels were dropped per 2.2.6.

Specific call sites to scrub (results from current grep, point of departure for the implementing agent):

- `src/gobby/dispatch/rules.py:20` (`_SKIP_PREFIX = "stage-:"`) — delete the constant; ripple through.
- Any helper in `src/gobby/build/service.py` that translated profiles to labels — replace with manifest skip lists (Phase 3.2 already does this; this task removes the legacy fallback).
- Migration helpers in `src/gobby/storage/migrations.py` that read `stage-:` labels (only the backfill helper from Phase 2.2 — keep that one as it's a frozen historical record).

`planning-round:N` and `qa-attempts:N` labels are now redundant (replaced by `attempt_count`). Drop these from every task in a final cleanup migration (version 237). Read sites: any `_front_half.py` references to `PLANNING_ROUND_LABEL_PREFIX` are deleted.

**Acceptance:**

- 7.1.1 — `_SKIP_PREFIX` constant and all `stage-:` / `_skipped_stages` reads deleted from runtime code. The audit grep is scoped to `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/cli/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/`, `src/gobby/tasks/expansion/` (round-11 F2: `_common.py`, `_compile.py`, `_apply.py`), `src/gobby/tasks/expansion_service.py` (round-12 F2: facade currently re-exports `_skipped_stages`; the import and `__all__` entry are removed), `src/gobby/storage/tasks/` (round-11 F2: `_crud.py::_skipped_stages` and `cascade_build_state_to_subtree`), and bundled agent/skill instruction surfaces under `src/gobby/install/shared/workflows/agents/` and `src/gobby/install/shared/skills/` (any YAML/SKILL.md text mentioning `_skipped_stages` or `stage-:`). Combined audit returns zero matches across all listed paths. Migrations (`src/gobby/storage/migrations.py`) and migration-specific tests (`tests/storage/test_migration_*.py`) are explicitly EXEMPT — the migration-234 backfill helper preserves `stage-:<name>` label reads as a frozen historical record so pre-cutover databases replay correctly (acceptance 7.1.4 covers the positive regression; acceptance 7.1.5 covers manifest-based replacements for the deleted runtime readers). file: `src/gobby/dispatch/rules.py`, `src/gobby/tasks/expansion/_common.py`, `src/gobby/tasks/expansion/_compile.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/tasks/expansion_service.py`, `src/gobby/storage/tasks/_crud.py`. test: `tests/test_no_stage_skip_labels.py::test_grep_returns_empty_for_full_runtime_scope`, `tests/test_no_stage_skip_labels.py::test_grep_returns_empty_for_bundled_agent_instructions`, `tests/test_no_stage_skip_labels.py::test_expansion_service_facade_does_not_export_skipped_stages`, `tests/test_no_stage_skip_labels.py::test_migration_234_helper_intact_in_historical_scope`.
- 7.1.2 — Migration 237 drops `planning-round:N` and `qa-attempts:N` labels from every task. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_237_label_cleanup.py::test_legacy_labels_dropped`.
- 7.1.3 — `PLANNING_ROUND_LABEL_PREFIX` constant deleted; readers updated to read `attempt_count`. file: `src/gobby/mcp_proxy/tools/tasks/_front_half.py`. test: `tests/mcp_proxy/tools/tasks/test_front_half_attempt_count.py::test_no_label_reads`.
- 7.1.4 — Migration 234 backfill replay preservation: `_backfill_task_stage_states_from_legacy` STILL honors `stage-:<name>` skip labels when replayed against a pre-cutover fixture DB post-Phase-7.1 cleanup. The historical helper is a frozen migration record; its label-reading code paths are not deleted by 7.1.1's runtime cleanup. Test fixture: a synthetic pre-cutover task carrying labels like `stage-:test_arch` and `stage-:expansion_qa` produces a backfilled manifest equal to the task-type default minus those two stages. file: `src/gobby/storage/migrations.py` (helper preserved). test: `tests/storage/test_migration_234_backfill.py::test_replay_against_pre_cutover_db_honors_legacy_skip_labels`, `tests/storage/test_migration_234_backfill.py::test_skip_label_reads_in_helper_survive_phase_7_1_cleanup`.
- 7.1.5 — Manifest-based replacements for deleted runtime label readers (round-11 F2 / round-12 F2 positive regression). The runtime code paths that previously read `stage-:<name>` labels are ported to read the manifest (`task.stages` / `task_stage_states`) directly, with no behavior change in skipped-stage handling: (a) `src/gobby/tasks/expansion/_compile.py::_build_prompt_context` reads the parent task's `task.stages` to decide which stages to include in the expansion prompt context, no longer references `_skipped_stages` or `stage-:` labels. (b) `src/gobby/tasks/expansion/_apply.py::_complete_dev_only_run` calls `complete_stage(task_id, 'expansion')` on the parent task's manifest to advance the dev-only expansion bypass, no longer writes legacy `lifecycle = 'in_development'` (covered alongside acceptance 5.3.9). (c) `src/gobby/storage/tasks/_crud.py::cascade_build_state_to_subtree` writes child manifests via `StageStatesManager.initialize_manifest` rather than emitting `stage-:` labels. (d) **(round-12 F2)** `src/gobby/tasks/expansion_service.py` facade drops its `_skipped_stages` import and removes it from `__all__`; any test asserting the facade exposes `_skipped_stages` is updated to assert it does NOT (post-cleanup negative regression). The `_skipped_stages` helpers in both `_common.py` and `_crud.py` are deleted (covered by 7.1.1's grep). Bundled agent/skill instruction surfaces that reference `_skipped_stages` are rewritten to point at the manifest read path (or removed if the reference is no longer accurate). file: `src/gobby/tasks/expansion/_compile.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/tasks/expansion_service.py`, `src/gobby/storage/tasks/_crud.py`. test: `tests/tasks/expansion/test_compile_uses_manifest.py::test_prompt_context_reads_stages_not_labels`, `tests/tasks/expansion/test_apply_dev_only.py::test_complete_dev_only_run_via_complete_stage`, `tests/tasks/test_expansion_service_facade.py::test_facade_does_not_export_skipped_stages`, `tests/storage/tasks/test_cascade_build_state.py::test_cascade_uses_initialize_manifest`, `tests/storage/tasks/test_cascade_build_state.py::test_cascade_no_legacy_label_writes`.

### 7.2 Documentation pass [category: docs] (depends: 7.1)
`kind: deliverable`

Target: `CLAUDE.md`, `src/gobby/install/shared/skills/plan-draft/SKILL.md`, `docs/contracts/plan-coverage.md`, `docs/guides/dispatch.md` (new or extend)

Update written documentation to reflect the manifest model.

`CLAUDE.md` "Dispatch Architecture" section: replace any mention of `lifecycle` / `status` axes with stage-manifest semantics. Specifically the list of fields (`allow_automation`, `yolo`, `isolation`) gains `stages` (manifest) as a peer. Profile bundles documented as Phase 3.2.

`plan-draft/SKILL.md`: refresh the canonical stage list in its "Phasing" guidance to match the registry's 14 stages.

`docs/contracts/plan-coverage.md`: no changes for the coverage contract grammar itself, but if the doc references retired status values (`needs_review` → `code_review_qa.in_progress`, etc.), update those examples to match.

`docs/guides/dispatch.md` (new file if absent; extend if present): one-page architecture diagram + prose covering: registry → manifest → rule → action chain. Include the canonical stage list and the readiness/blocking projection definition. The doc lives under `guides/` (operator-facing how-to) rather than `architecture/` to match the project's documentation convention.

Update tests to read documentation references (no tests for prose, but the verification phase below cross-checks).

**Acceptance:**

- 7.2.1 — `CLAUDE.md` "Dispatch Architecture" section reflects the manifest model with no remaining `lifecycle`/`status` semantics. file: `CLAUDE.md`. behavior: "doc names task_stage_states and registry; no `(lifecycle, status)` tuple references in dispatcher prose" verified by manual review noted in PR description.
- 7.2.2 — `plan-draft` skill canonical stage list matches the registry. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`. behavior: "skill stage list = registry stage list" verified in `tests/skills/test_plan_draft_stage_list.py::test_matches_registry`.
- 7.2.3 — `docs/guides/dispatch.md` exists and covers registry, manifest, rule chain, readiness projection. file: `docs/guides/dispatch.md`.
- 7.2.4 — `docs/guides/dispatch.md` (or a sibling under `docs/guides/`) calls out the orthogonality of `is_escalated` / `is_blocked` projections vs. stage state, with a worked example of escalate-then-de-escalate preserving the manifest row. file: `docs/guides/dispatch.md`.

## V1 Verification
`kind: verification`

End-to-end acceptance covers:

- **Schema integrity**: every migration runs forward on a fresh DB and on a fixture DB with representative `(lifecycle, status, labels)` tuples; resulting `task_stage_states` rows match the mapping table 2.2.2.
- **Storage invariants**: position uniqueness, registry FK, transition state machine, attempt-count semantics — all enforced by `StageStatesManager` tests.
- **Type→default-stages resolution**: every existing and new task type resolves to declared defaults via `get_task_type_defaults`.
- **Build-time override merge**: `--stages`, `--add-stage`, `--skip-stage`, profile bundles all compose as documented in 3.2 and 2.5.
- **Readiness equivalence**: contract tests run old vs. new `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and `state.is_blocked` against the same fixture DB and assert identical task ID sets.
- **Dispatcher chain**: full delivery walk `holistic_qa.done → pr.in_progress → pr.done → merge.in_progress → merge.done → task closed` covered end-to-end on a fresh DB.
- **API surface**: `GET /api/tasks?stage=development&stage_state=in_progress` returns expected set; `PATCH /api/tasks/{id}/stages/{name}` enforces tri-state transitions; PR verdict and merge result tools store artifacts and transition stages correctly.
- **Terminal non-merge types**: `research_spike` and `prd_doc` walk to their terminal stage and close cleanly without ever reaching `merge`.
- **UI**: LifecycleBoard renders with seeded registry, drag-to-advance updates state via PATCH, swimlane filter by task_type hides empty rows, and pre-existing migrated tasks render from their stage rows; blocked tasks render with badges in their current column.
- **Performance**: kanban board fetch SQL keeps p99 under existing `KanbanBoard` baseline (denormalized stage manifest in single query, indexed on `(task_id, position)` and `(stage_name, state)`).
- **Dead-code regression**: grep/static tests fail if code writes old `status` / `lifecycle` values or calls removed lifecycle PR/merge tools after the cutover.
- **No regressions**: targeted runs of `tests/dispatch/`, `tests/tasks/`, `tests/storage/`, `tests/servers/routes/`, `tests/mcp_proxy/tools/tasks/`, plus `pnpm test` and `pnpm build` for the web bundle.

## Out of scope
`kind: framing`

- **Real agent behavior for the four discovery stages.** This epic ships disabled placeholder YAMLs (1.3) and tracking tasks (5.4); it does NOT author working `analyst`, `researcher`, `architect`, or `product-manager` agents. That work is owned by the `Discovery-stage agent registry` epic created in 5.4.
- **PR-Agent / rizzler-style PR review behavior** — owned by #13552, which targets the stage contract this epic delivers.
- **Re-implementing existing agents.** `planner`, `plan-adversary`, `test-architect`, `expansion-qa`, `qa-reviewer`, `holistic-reviewer`, `merge-orchestrator`, `merge-worker`, `backend-developer`, `frontend-developer`, `default`, `developer` already exist; they are referenced by the registry's `default_agent` slot but their YAMLs are not modified beyond, at most, comment updates referencing the new stage names.
- Cross-project / multi-tenant kanban.
- Per-stage time tracking, SLAs, due dates.
- Drag-and-drop reordering of stages within a task's manifest. Drag-to-advance state is in scope; drag-to-reorder positions is not.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: "1.1 Schema migration + bundled stages.yaml: registry, defaults, manifest, and PR/merge artifact columns"
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "All acceptance items for section 1.1 pass."
  labels:
    - "covers:unknown:1.1:1.1.1"
    - "covers:unknown:1.1:1.1.2"
    - "covers:unknown:1.1:1.1.3"
    - "covers:unknown:1.1:1.1.4"
    - "covers:unknown:1.1:1.1.5"
    - "covers:unknown:1.1:1.1.6"
    - "covers:unknown:1.1:1.1.6a"
    - "covers:unknown:1.1:1.1.7"
    - "covers:unknown:1.1:1.1.8"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.1"
- title: "1.2 Sync loader for the bundled stages.yaml"
  category: config
  task_type: chore
  depends_on:
    - "1.1"
  validation_criteria: "All acceptance items for section 1.2 pass."
  labels:
    - "covers:unknown:1.2:1.2.1"
    - "covers:unknown:1.2:1.2.2"
    - "covers:unknown:1.2:1.2.3"
    - "covers:unknown:1.2:1.2.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.2"
- title: "1.3 Placeholder agent YAMLs for discovery stages"
  category: config
  task_type: chore
  depends_on: []
  validation_criteria: "All acceptance items for section 1.3 pass."
  labels:
    - "covers:unknown:1.3:1.3.1"
    - "covers:unknown:1.3:1.3.2"
    - "covers:unknown:1.3:1.3.3"
    - "covers:unknown:1.3:1.3.4"
    - "covers:unknown:1.3:1.3.5"
    - "covers:unknown:1.3:1.3.6"
    - "covers:unknown:1.3:1.3.7"
    - "covers:unknown:1.3:1.3.8"
    - "covers:unknown:1.3:1.3.9"
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.3"
- title: "2.1 Stage registry + stage states storage managers"
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: "All acceptance items for section 2.1 pass."
  labels:
    - "covers:unknown:2.1:2.1.1"
    - "covers:unknown:2.1:2.1.2"
    - "covers:unknown:2.1:2.1.3"
    - "covers:unknown:2.1:2.1.4"
    - "covers:unknown:2.1:2.1.5"
    - "covers:unknown:2.1:2.1.6"
    - "covers:unknown:2.1:2.1.7"
    - "covers:unknown:2.1:2.1.8"
    - "covers:unknown:2.1:2.1.9"
    - "covers:unknown:2.1:2.1.10"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.1"
- title: "2.2 One-shot backfill: derive `task_stage_states` from existing `(lifecycle, status, labels)`"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
  validation_criteria: "All acceptance items for section 2.2 pass."
  labels:
    - "covers:unknown:2.2:2.2.1"
    - "covers:unknown:2.2:2.2.2"
    - "covers:unknown:2.2:2.2.3"
    - "covers:unknown:2.2:2.2.4"
    - "covers:unknown:2.2:2.2.5"
    - "covers:unknown:2.2:2.2.6"
    - "covers:unknown:2.2:2.2.7"
    - "covers:unknown:2.2:2.2.8"
    - "covers:unknown:2.2:2.2.9"
    - "covers:unknown:2.2:2.2.10"
    - "covers:unknown:2.2:2.2.11"
    - "covers:unknown:2.2:2.2.12"
    - "covers:unknown:2.2:2.2.13"
    - "covers:unknown:2.2:2.2.14"
    - "covers:unknown:2.2:2.2.15"
    - "covers:unknown:2.2:2.2.16"
    - "covers:unknown:2.2:2.2.17"
    - "covers:unknown:2.2:2.2.18"
    - "covers:unknown:2.2:2.2.19"
    - "covers:unknown:2.2:2.2.20"
    - "covers:unknown:2.2:2.2.21"
    - "covers:unknown:2.2:2.2.22"
    - "covers:unknown:2.2:2.2.23"
    - "covers:unknown:2.2:2.2.24"
    - "covers:unknown:2.2:2.2.25"
    - "covers:unknown:2.2:2.2.26"
    - "covers:unknown:2.2:2.2.27"
    - "covers:unknown:2.2:2.2.28"
    - "covers:unknown:2.2:2.2.29"
    - "covers:unknown:2.2:2.2.30"
    - "covers:unknown:2.2:2.2.31"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.2"
- title: "2.3 New gobby-tasks MCP tools for stage manifest"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
  validation_criteria: "All acceptance items for section 2.3 pass."
  labels:
    - "covers:unknown:2.3:2.3.1"
    - "covers:unknown:2.3:2.3.2"
    - "covers:unknown:2.3:2.3.3"
    - "covers:unknown:2.3:2.3.4"
    - "covers:unknown:2.3:2.3.5"
    - "covers:unknown:2.3:2.3.6"
    - "covers:unknown:2.3:2.3.7"
    - "covers:unknown:2.3:2.3.8"
    - "covers:unknown:2.3:2.3.9"
    - "covers:unknown:2.3:2.3.10"
    - "covers:unknown:2.3:2.3.11"
    - "covers:unknown:2.3:2.3.12"
    - "covers:unknown:2.3:2.3.13"
    - "covers:unknown:2.3:2.3.14"
    - "covers:unknown:2.3:2.3.15"
    - "covers:unknown:2.3:2.3.16"
    - "covers:unknown:2.3:2.3.17"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.3"
- title: "2.4 New HTTP routes for stage manifest"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
  validation_criteria: "All acceptance items for section 2.4 pass."
  labels:
    - "covers:unknown:2.4:2.4.1"
    - "covers:unknown:2.4:2.4.2"
    - "covers:unknown:2.4:2.4.3"
    - "covers:unknown:2.4:2.4.4"
    - "covers:unknown:2.4:2.4.5"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.4"
- title: "2.5 New CLI commands and build flags"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
    - "2.3"
  validation_criteria: "All acceptance items for section 2.5 pass."
  labels:
    - "covers:unknown:2.5:2.5.1"
    - "covers:unknown:2.5:2.5.2"
    - "covers:unknown:2.5:2.5.3"
    - "covers:unknown:2.5:2.5.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.5"
- title: "2.6 Rewire `mark_task_review_*` tools to stage-native semantics"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
    - "2.3"
  validation_criteria: "All acceptance items for section 2.6 pass."
  labels:
    - "covers:unknown:2.6:2.6.1"
    - "covers:unknown:2.6:2.6.2"
    - "covers:unknown:2.6:2.6.3"
    - "covers:unknown:2.6:2.6.4"
    - "covers:unknown:2.6:2.6.5"
    - "covers:unknown:2.6:2.6.6"
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.6"
- title: "3.1 Rewrite `dispatch/rules.py` to query stage manifest"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
    - "2.2"
  validation_criteria: "All acceptance items for section 3.1 pass."
  labels:
    - "covers:unknown:3.1:3.1.1"
    - "covers:unknown:3.1:3.1.2"
    - "covers:unknown:3.1:3.1.3"
    - "covers:unknown:3.1:3.1.4"
    - "covers:unknown:3.1:3.1.5"
    - "covers:unknown:3.1:3.1.6"
    - "covers:unknown:3.1:3.1.7"
    - "covers:unknown:3.1:3.1.8"
    - "covers:unknown:3.1:3.1.9"
    - "covers:unknown:3.1:3.1.10"
    - "covers:unknown:3.1:3.1.11"
    - "covers:unknown:3.1:3.1.12"
    - "covers:unknown:3.1:3.1.13"
    - "covers:unknown:3.1:3.1.14"
    - "covers:unknown:3.1:3.1.15"
    - "covers:unknown:3.1:3.1.16"
    - "covers:unknown:3.1:3.1.17"
    - "covers:unknown:3.1:3.1.18"
    - "covers:unknown:3.1:3.1.19"
    - "covers:unknown:3.1:3.1.20"
    - "covers:unknown:3.1:3.1.21"
    - "covers:unknown:3.1:3.1.22"
    - "covers:unknown:3.1:3.1.23"
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.1"
- title: "3.2 Manifest resolution at build time + readiness projections rewrite"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
    - "2.2"
    - "3.1"
  validation_criteria: "All acceptance items for section 3.2 pass."
  labels:
    - "covers:unknown:3.2:3.2.1"
    - "covers:unknown:3.2:3.2.2"
    - "covers:unknown:3.2:3.2.3"
    - "covers:unknown:3.2:3.2.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.2"
- title: "3.3 Cut over `RuntimeDispatchMutex` candidate-snapshot check from `(lifecycle, status)` to `(stage_name, stage_state, updated_at)`"
  category: code
  task_type: feature
  depends_on:
    - "3.1"
    - "3.2"
  validation_criteria: "All acceptance items for section 3.3 pass."
  labels:
    - "covers:unknown:3.3:3.3.1"
    - "covers:unknown:3.3:3.3.2"
    - "covers:unknown:3.3:3.3.3"
    - "covers:unknown:3.3:3.3.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.3"
- title: "4.1 PR stage rule + delivery artifacts"
  category: code
  task_type: feature
  depends_on:
    - "3.1"
  validation_criteria: "All acceptance items for section 4.1 pass."
  labels:
    - "covers:unknown:4.1:4.1.1"
    - "covers:unknown:4.1:4.1.2"
    - "covers:unknown:4.1:4.1.3"
    - "covers:unknown:4.1:4.1.4"
    - "covers:unknown:4.1:4.1.5"
  assigned_agent: backend-developer
  tdd: true
  source_section: "4.1"
- title: "4.2 Merge stage rule + delivery artifacts"
  category: code
  task_type: feature
  depends_on:
    - "4.1"
  validation_criteria: "All acceptance items for section 4.2 pass."
  labels:
    - "covers:unknown:4.2:4.2.1"
    - "covers:unknown:4.2:4.2.2"
    - "covers:unknown:4.2:4.2.3"
    - "covers:unknown:4.2:4.2.4"
    - "covers:unknown:4.2:4.2.5"
  assigned_agent: backend-developer
  tdd: true
  source_section: "4.2"
- title: "5.1 New task types + default-stages seed"
  category: code
  task_type: feature
  depends_on:
    - "2.1"
    - "2.2"
  validation_criteria: "All acceptance items for section 5.1 pass."
  labels:
    - "covers:unknown:5.1:5.1.1"
    - "covers:unknown:5.1:5.1.2"
    - "covers:unknown:5.1:5.1.3"
    - "covers:unknown:5.1:5.1.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "5.1"
- title: "5.2 Wire `is_escalated` first-class column through dataclass + readers"
  category: code
  task_type: feature
  depends_on:
    - "1.1"
    - "2.2"
  validation_criteria: "All acceptance items for section 5.2 pass."
  labels:
    - "covers:unknown:5.2:5.2.1"
    - "covers:unknown:5.2:5.2.2"
    - "covers:unknown:5.2:5.2.3"
    - "covers:unknown:5.2:5.2.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "5.2"
- title: "5.3 Drop `lifecycle`, `lifecycle_stage`, active `status` semantics"
  category: code
  task_type: feature
  depends_on:
    - "2.6"
    - "3.1"
    - "3.2"
    - "3.3"
    - "4.1"
    - "4.2"
    - "5.2"
    - "6.3"
  validation_criteria: "All acceptance items for section 5.3 pass."
  labels:
    - "covers:unknown:5.3:5.3.1"
    - "covers:unknown:5.3:5.3.2"
    - "covers:unknown:5.3:5.3.3"
    - "covers:unknown:5.3:5.3.4"
    - "covers:unknown:5.3:5.3.5"
    - "covers:unknown:5.3:5.3.6"
    - "covers:unknown:5.3:5.3.7"
    - "covers:unknown:5.3:5.3.8"
    - "covers:unknown:5.3:5.3.9"
  assigned_agent: backend-developer
  tdd: true
  source_section: "5.3"
- title: "5.4 Discovery-stage agent follow-up tracking"
  category: manual
  task_type: chore
  depends_on:
    - "1.3"
  validation_criteria: "All acceptance items for section 5.4 pass."
  labels:
    - "covers:unknown:5.4:5.4.1"
    - "covers:unknown:5.4:5.4.2"
    - "covers:unknown:5.4:5.4.3"
    - "covers:unknown:5.4:5.4.4"
    - "covers:unknown:5.4:5.4.5"
    - "covers:unknown:5.4:5.4.6"
    - "covers:unknown:5.4:5.4.7"
    - "covers:unknown:5.4:5.4.8"
  assigned_agent: planner
  tdd: false
  source_section: "5.4"
- title: "6.1 New `LifecycleBoard.tsx` + `StageColumn.tsx` + `StageCard.tsx`"
  category: code
  task_type: feature
  depends_on:
    - "2.4"
  validation_criteria: "All acceptance items for section 6.1 pass."
  labels:
    - "covers:unknown:6.1:6.1.1"
    - "covers:unknown:6.1:6.1.2"
    - "covers:unknown:6.1:6.1.3"
    - "covers:unknown:6.1:6.1.4"
    - "covers:unknown:6.1:6.1.4a"
    - "covers:unknown:6.1:6.1.4b"
    - "covers:unknown:6.1:6.1.5"
    - "covers:unknown:6.1:6.1.6"
    - "covers:unknown:6.1:6.1.7"
    - "covers:unknown:6.1:6.1.8"
    - "covers:unknown:6.1:6.1.9"
  assigned_agent: backend-developer
  tdd: true
  source_section: "6.1"
- title: "6.2 useTasks denormalized stage manifest + new filters"
  category: code
  task_type: feature
  depends_on:
    - "2.4"
    - "6.1"
  validation_criteria: "All acceptance items for section 6.2 pass."
  labels:
    - "covers:unknown:6.2:6.2.1"
    - "covers:unknown:6.2:6.2.2"
    - "covers:unknown:6.2:6.2.3"
    - "covers:unknown:6.2:6.2.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "6.2"
- title: "6.3 Mount LifecycleBoard, retire `taskState.ts` legacy types"
  category: code
  task_type: feature
  depends_on:
    - "6.1"
    - "6.2"
  validation_criteria: "All acceptance items for section 6.3 pass."
  labels:
    - "covers:unknown:6.3:6.3.1"
    - "covers:unknown:6.3:6.3.2"
    - "covers:unknown:6.3:6.3.3"
    - "covers:unknown:6.3:6.3.4"
  assigned_agent: backend-developer
  tdd: true
  source_section: "6.3"
- title: "7.1 Remove `stage-:<name>` label handling and migration helpers"
  category: refactor
  task_type: refactor
  depends_on:
    - "5.3"
    - "6.3"
  validation_criteria: "All acceptance items for section 7.1 pass."
  labels:
    - "covers:unknown:7.1:7.1.1"
    - "covers:unknown:7.1:7.1.2"
    - "covers:unknown:7.1:7.1.3"
    - "covers:unknown:7.1:7.1.4"
    - "covers:unknown:7.1:7.1.5"
  assigned_agent: backend-developer
  tdd: false
  source_section: "7.1"
- title: "7.2 Documentation pass"
  category: docs
  task_type: chore
  depends_on:
    - "7.1"
  validation_criteria: "All acceptance items for section 7.2 pass."
  labels:
    - "covers:unknown:7.2:7.2.1"
    - "covers:unknown:7.2:7.2.2"
    - "covers:unknown:7.2:7.2.3"
    - "covers:unknown:7.2:7.2.4"
  assigned_agent: default
  tdd: false
  source_section: "7.2"
```
