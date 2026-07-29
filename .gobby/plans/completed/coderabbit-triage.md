# CodeRabbit Triage — `.gobby/plans/coderabbit.md` (382 findings)

Authoritative disposition record for epic **#19191**. Leaf tasks: T1 #19192,
T2 #19194, T3 #19195, T4 #19196, T5 #19197, T6 #19198, T7 #19199, T8 #19200,
T9 #19201, T10 #19202, T11 #19203, T12 #19204, T13 #19205. `T#` in the
Reason column of every `fix` row names the owning task.

The raw input `.gobby/plans/coderabbit.md` is gitignored (`.gitignore:243`)
and is deleted once every finding is fixed or documented `no-fix`. This file
is the surviving record of both decisions.

## Context

`.gobby/plans/coderabbit.md` holds the raw output of **three** concatenated
CodeRabbit CLI runs (batch markers at lines 1, 58, 651) against the unshipped
`0.5.0` branch: **382 findings across 252 paths**.

The file cannot be applied as-written, for four independent reasons:

1. **A forward-port already landed today.** On 2026-07-28 a CodeRabbit
   forward-port closed 277 finding dispositions across 239 source paths
   (integration commits `da44e2fa9`, `1242097f2`, `9c34494c6`, `c3c13e0ed`,
   `086d75a1d`; audit tip `18a043f58`). Part of this file duplicates that work.
2. **Whole subsystems were deleted after the review ran.** The verification-receipt
   subsystem is gone (`763c5fc8d`; table dropped by migration 344), taking
   `storage/verification_receipts.py`, `workflows/verification_receipt_ingestion.py`,
   `tasks/validation_verdict.py`, `admit_task_evidence`, and
   `build_verification_receipt_packet` with it. Findings against them have no target.
3. **CodeRabbit contradicts project convention in a repeatable, breaking way.**
   Nine findings demand `$1..$N` SQL placeholders. Gobby's hub is **psycopg**:
   `HubDatabase.transaction()` passes SQL straight through, so `%s` is correct.
   `gcode grep -F 'VALUES ($1' tests/` returns **zero** hits repo-wide. Applying
   those nine would break currently-passing tests. The reviewer is not
   hallucinating this — a completed plan document still on disk declares `$1`
   the standard. T13 removes that source.
4. **The file self-duplicates.** 206 of 382 findings share a target path with a
   sibling; 17 are verbatim restatements of the same defect from different passes.

Outcome: one verified disposition per finding, a deduplicated work breakdown
sized into leaf tasks under one epic, and deletion of the raw file once every
finding is fixed or documented as `no-fix`.

## Verification method

382 findings were split into 12 path-coherent packets (~30 each) and verified by
read-only `Explore` subagents against current code with `gcode` — never trusting
the stale line numbers in the report. Each returned `VALID`, `STALE`, `INVALID`,
or `DUPLICATE:<n>` with concrete evidence (symbol name plus an observed line).

**Result: 272 `fix` · 110 `no-fix`** — 272 VALID, 51 STALE, 42 INVALID, 17 DUPLICATE.
Valid findings size to 165 × S, 100 × M, 7 × L.

Two premises were corrected mid-flight and pushed to in-flight agents:

- `pyproject.toml:176` sets `asyncio_mode = "auto"`. Every "add
  `@pytest.mark.asyncio` so the coroutine is actually awaited" finding is
  **invalid** — the described defect cannot occur.
- `pytest.mark.unit` outnumbers `integration` **~1269 to ~50** at module level,
  including `temp_db` modules throughout `tests/storage/`. The reviewer's
  "DB-backed ⇒ integration" rule contradicts the codebase.

A third premise cleared itself: `src/gobby/workflows/summary_actions.py` and
`src/gobby/sessions/` were uncommitted and owned by another session when triage
began. That session committed mid-review (`98b0d6a4e`, `f031fd323`); the working
tree is now clean and no path is excluded.

## Decisions taken

| # | Decision | Consequence |
|---|----------|-------------|
| D1 | **Edit all four open plan documents now** | `herdr-terminal-client.md`, `wiki-codewiki-restructure.md`, `split-workflow-definition-storage.md` each hold a bound plan-review evidence row at round 5. Editing their bytes invalidates the round-5 section hashes. Task 8 must call `expire_plan_review_evidence` for each after editing, so the next `prepare_plan_review_round` starts from a fresh snapshot instead of failing on source drift. `m0-shared-datastores-bridge.md` has no evidence row and edits freely. |
| D2 | **Test markers match directory siblings** | In practice `pytest.mark.unit`. The affected modules currently carry *no* category marker, so they run in neither `-m unit` nor `-m integration`; `unit` puts them where their siblings live. Finding 176 (flip an existing `unit` → `integration`) becomes **no-fix**. A repo-wide convention decision is deferred to the post-0.5.0 epic. |
| D3 | **One epic, 12 domain leaf tasks + T13** | T1–T12 mirror the verification packets so each commits and validates independently against a coherent test subset. T13 fixes the root cause of the `$N` false-positive class, which lives outside the 382 findings. |
| D4 | **Approved/built plan documents are not edited** | `adversary-convergence-improvements.md` converged at round 22 `approved` with a populated `## M1 Task Manifest`, and its work is already shipping (migration 345, tasks #19069/#19080/#19082). Editing approved plan prose cannot reach built code. All 12 findings against it, plus 2 against the nonexistent `coderabbit-fixes/11-new-batch.md`, are `no-fix`. |

## Rejected findings worth naming

Five reviewer claims would have caused damage, not just wasted effort:

- **`_plan_gate.py` (219, 220)** — asks to degrade the consumer-inventory gate so
  spawns proceed when the code index is unavailable. `adversary-convergence-improvements.md:750-755`
  and acceptance 5.2.4 explicitly require the opposite: *"no spawn may proceed."*
  Also, all three `ConsumerInventoryError` raises use code `inventory_unavailable` —
  the "genuine incomplete-inventory" variant the finding wants preserved does not exist.
- **`analyzer.py` (128)** — asks to restore "previewed close" wording. That
  wording was deliberately changed in `0cda20504` (#18989) because `preview=true`
  now genuinely closes when ready. Reverting is a regression.
- **`rules-tab.css` (380)** — asks to restore a `calc(100% + 0.35rem)` offset.
  The panel is a direct child of a full-height tab root, so that would push it
  below the entire tab. `top: 0.25rem` is the deliberate #19159 anchor.
- **The nine `$N` placeholder findings** — would break passing tests.
- **Migration 342 lock-splitting (10)** — asks to split an `ACCESS EXCLUSIVE`
  backfill across rollout steps. The table it alters, `verification_receipts`, is
  `DROP`ped two migrations later (`344_transcript_close_checklist.sql:5`), and
  0.5.0 is unshipped, so no deployment ever holds it under load. This one was
  reported VALID by verification and rejected on the surrounding facts.

Two findings rest on a false premise about a real bug: 331 and 339 ask for
`if task is None` guards, but `LocalTaskManager.get_task` (`storage/tasks/_read.py:20-53`)
**raises `ValueError`** and never returns `None`. Those guards would be dead code.
The genuine gap — unhandled `ValueError` — is captured instead.

## Findings requiring ordered application

- **245 before 246** (`memory/dream/related.py`). 245 moves semaphore acquisition
  outside the `asyncio.timeout` scope; 246 as written re-introduces it inside.
  Apply 245, then 246 as cleanup on top.
- **Production guard before test (184)** — `tests/.../test_failure_cleanup.py`
  cannot pass until `start_run_or_cleanup` (`_failure_cleanup.py:88`) actually
  wraps `runner.run_storage.get(run_id)` in try/except. Ship the guard first.

## Work breakdown

One epic, twelve leaf tasks mirroring the verification packets. Each commits and
validates independently. `T#` appears in the Reason column of every `fix` row.

| Task | Scope | Fixes | Focused validation |
|---|---|---|---|
| **T1** | `src/gobby/plans/` — review ledger, sweeps, evidence, repair, manifest | 28 | `pytest tests/plans/ -m unit` + `mypy src/gobby/plans` |
| **T2** | `src/gobby/storage/` — sessions, tasks, agents, agent_resume | 24 | `pytest tests/storage/ tests/utils/` |
| **T3** | `web/` — UI primitives, activity panel, skills, memory, style ratchet | 27 | `npm test` (web Vitest) + `tsc --noEmit` + eslint |
| **T4** | `src/gobby/agents/`, `autonomous/`, `code_index/`, `adapters/` | 21 | `pytest tests/agents/ tests/code_index/ tests/autonomous/` |
| **T5** | `src/gobby/mcp_proxy/` + `src/gobby/sessions/` | 20 | `pytest tests/mcp_proxy/ tests/sessions/` |
| **T6** | `src/gobby/hooks/`, `workflows/`, `sync/` | 20 | `pytest tests/hooks/ tests/workflows/ tests/sync/` |
| **T7** | `src/gobby/servers/`, `cli/`, `tasks/`, `dispatch/`, `memory/`, `utils/`, `communications/`, `runner_lifecycle_*` | 24 | `pytest tests/servers/ tests/cli/ tests/dispatch/ tests/memory/` |
| **T8** | Four plan documents (see D1) | 17 | `uv run gobby plans validate <file>` for each, then `expire_plan_review_evidence` on the three bound plans |
| **T9** | `tests/plans/`, `tests/agents/` | 29 | the touched modules + `gobby test-types audit` |
| **T10** | `tests/storage/`, `tests/workflows/`, `tests/hooks/` | 16 | the touched modules + `gobby test-types audit` |
| **T11** | `tests/mcp_proxy/`, `tests/utils/`, `tests/servers/`, `tests/sessions/`, `tests/*.py` | 24 | the touched modules + `gobby test-types audit` |
| **T12** | remaining tests + `docs/`, `AGENTS.md`, bundled templates, `crates/` | 22 | touched modules; `cargo test -p gobby-code -p gobby-wiki` for the two Rust fixes |
| **T13** | Root-cause fix for the `$N` false-positive class (see below) | 0 of 382 | `coderabbit review --dir <small scoped path>` and confirm no `$N` placeholder finding is emitted |

### T13 — stop the `$N` false-positive class at its source

Not one of the 382 findings; it is the reason nine of them exist and will keep
regenerating on every review.

**Root cause.** `.gobby/plans/completed/task-12761-postgres-hub-migration.md:32`
states *"Parameter style standardized on `$1`."* and line 23 lists *"`$1`
parameter placeholders"* among the project's portability choices, with `$1`
example SQL at line 156 and a ~170-line `$N`→`?` translation-shim spec at
lines 1458-1623. That standardization was aspirational — chosen so migration
artifacts would survive a future Rust/`sqlx` port. **Line 29 of the same plan
selects psycopg v3 as the driver**, which is `%s`-native, so the `$1` rule was
never implementable in the Python that shipped. The document still reads as an
authoritative standard.

`.coderabbit.yaml` provides no counter-signal: its `src/**/*.py` and
`tests/**/*.py` `path_instructions` cover type hints, async correctness, and SQL
*injection* — nothing about parameter style.

This was a known recurring false positive:
`docs/plans/completed/review-signal-learning.md:91` names it as the worked
example, and the recalled `%s` memory (task #19002) is what caught all nine here.

**Work**

1. Add to `.coderabbit.yaml` `path_instructions` for `src/**/*.py` and
   `tests/**/*.py`: hub database access uses psycopg v3 with `%s` placeholders
   and parameter tuples; never suggest `$1`/`$N` positional placeholders.
2. Annotate `task-12761-postgres-hub-migration.md:32` recording that the `$1`
   standardization was superseded by the psycopg v3 driver choice and applies
   only to a future Rust/`sqlx` port.
3. Note for the operator: the CodeRabbit CLI has **no** learnings-management
   command — `auth`, `review`, `stats`, `update`, `feedback`, `doctor` only, and
   `coderabbit feedback` sends a message to CodeRabbit's team rather than
   mutating the knowledge base. Repo config is therefore the governing lever for
   local CLI reviews, which is how this report was produced. `.coderabbit.yaml`
   is persistent; `coderabbit review -c <file>` layers extra instructions for a
   single run. Whether app.coderabbit.ai exposes deletable learnings is
   unverified and is a lower-value path for CLI-sourced findings.

**Ordering constraints**

- **T8 last among source tasks.** Editing the three bound plan documents forces
  evidence expiry; do it once, after no further plan-doc churn is expected.
- **T7 before T11.** Finding 184's test cannot pass until T7 ships the
  `start_run_or_cleanup` try/except around `runner.run_storage.get`.
- **Within T7: 245 before 246** (`memory/dream/related.py`) — 246 as written
  re-introduces the semaphore-inside-timeout bug that 245 removes.
- **T3 note.** Finding 231 (style-ratchet allowlist must diff against the target
  branch) is the only `L` in the packet and reshapes how the ratchet runs in CI;
  sequence it after the other 26 so a ratchet change does not mask them.

**Cross-cutting fixes that collapse several findings**

- One shared `is_sha256` validator replaces five duplicated `_SHA256_RE`
  definitions (`review_ledger`, `review_coverage`, `review_requirements`,
  `servers/routes/attention`, inline `review_sweeps._required_sha256`) — T1.
- One shared rate-limit helper replaces the two divergent implementations in
  `sync/external_coordinator.py` and `sync/github_issue_sync.py` — T6.
- One shared GitHub issue-number validator plus a single
  `_MAX_GITHUB_ISSUE_NUMBER` replaces the duplicate pair — T6.
- One shared `_link_child_session_to_run` test helper replaces three copies — T9.
- Shared test helpers move to `tests/review_coverage_helpers.py` /
  `tests/review_telemetry_helpers.py`, removing cross-test-module imports of
  private names from four modules — T9 and T12.

## Finding-by-finding table

All 382 findings, one row each. `fix` rows carry effort (S/M/L) and owning task.

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1 | no-fix | `adapters/codex_impl/execution_chain.py:_set_pending` | 0.5.0 forward-port (18a043f58) | **STALE** — Already fixed: dict[str,None] L377-378, FIFO pop L583-585 guarded by `key not in ambiguous` |
| 2 | no-fix | `adapters/codex_impl/execution_chain.py:extract_yielded_cell_id` | 0.5.0 forward-port (18a043f58) | **STALE** — Already fixed: loops all blocks/lines into yielded_cells, returns only on unique match L302-314 |
| 3 | no-fix | `cli/tasks/ai.py` | receipt subsystem deleted (763c5fc8d) | **STALE** — admit_task_evidence / build_verification_receipt_packet have ZERO hits in src/; ai.py now holds only validate_task_cmd and suggest_cmd |
| 4 | no-fix | `code_index/sync_worker.py graph-sync` | — | **INVALID** — Premise wrong: vector sync uses `failed=(vector_breaker,)` L389, never fails gateway_breaker on transport errors. REAL gap: graph path catches only GcodeTimeoutError L477 (never GcodeUnavailableError), and both it and generic handler L491 call `_record_breaker_outcomes(armed)` → record_success() … |
| 5 | no-fix | `hooks/event_handlers/_tool.py:handle_before_tool` | bounded async DB ops (#19006) | **STALE** — Already imports FUNCTIONS_EXEC_NAMES from adapters.codex_impl.execution_chain; no inline set |
| 6 | no-fix | `install/shared/prompts/validation/validate.md` | bounded async DB ops (#19006) | **STALE** — File is 56 lines; contract already `"status":"valid"\\|"invalid"` only — "pending" appears nowhere |
| 7 | no-fix | `mcp_proxy/tools/tasks/_lifecycle_close.py` | 0.5.0 forward-port (18a043f58) | **STALE** — Rewritten by e66782d56 (checklist gate, 588 lines); `should_skip` does not exist — only `evaluation.skip_leaf_checks` L133/135 and L436 |
| 8 | no-fix | `mcp_proxy/tools/tasks/_lifecycle_validation.py` | 0.5.0 forward-port (18a043f58) | **STALE** — `criterion_results` appears nowhere in file; survives repo-wide only in tests/tasks/contract_validator.py |
| 9 | fix | `sessions/processor_usage.py:_persist_usage_events` | — | **VALID** · M · T5 — L62 `if not has_usage and not has_window_metadata: return` omits has_context_occupancy (computed L34) → occupancy-only batch returns before per-message snapshot path L88+ |
| 10 | no-fix | `src/gobby/storage/migrations/342_task_validation_epoch.sql` | receipt subsystem deleted (763c5fc8d) | **INVALID** — The lock-splitting concern applies to a live production rollout. `verification_receipts` is DROPped two migrations later (`344_transcript_close_checklist.sql:5`) and 0.5.0 is unshipped, so no deployment ever holds this table under load. Splitting the DDL would add rollout machinery for a table that no longer exists. |
| 11 | fix | `storage/tasks/_manager.py:update_task` | — | **VALID** · S · T2 — `current_task = self.get_task(task_id)` L355 unconditional; trailing `return self.get_task(task_id)` L417 already re-checks |
| 12 | no-fix | `storage/tasks/_manager.py:update_task` | 0.5.0 forward-port (18a043f58) | **STALE** — Already fixed L363-368: `effective_criteria = require_validation_criteria(...)` then assigned back |
| 13 | no-fix | `tasks/criteria_contract.py:split_validation_criteria` | D2 sibling-marker rule | **STALE** — Already fixed L52-56: `if not saw_list_marker: items = []; current = []` on first _LIST_ITEM_RE match |
| 14 | no-fix | `tasks/validation_verdict.py` | 0.5.0 forward-port (18a043f58) | **STALE** — FILE DOES NOT EXIST; by_criterion/expected_set have no hits in src/ |
| 15 | no-fix | `tests/cli/test_pack.py L190` | D2 sibling-marker rule | **INVALID** — Module-level `pytestmark = pytest.mark.unit` L14 already marks every test |
| 16 | fix | `tests/config/test_mcp_config.py:test_load_servers_skips_without_name L236` | test-types ratchet (#18781/#18783) | **VALID** · S · T12 — `tmp_path,` unannotated while `caplog: pytest.LogCaptureFixture` is typed |
| 17 | no-fix | `tests/hooks/test_session_coordinator.py L351` | D2 sibling-marker rule | **STALE** — Module already has `pytestmark = pytest.mark.unit` L35 |
| 18 | no-fix | `tests/integration/test_hub_query.py` | psycopg `%s` hub contract (#19002) | **INVALID** — `hub_db.execute` L316/323 uses psycopg `%s`; Gobby never uses `$N` |
| 19 | fix | `tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py:create_task_registry L36-49` | — | **VALID** · L · T11 — Injects a default AsyncMock validator returning CloseVerdict(status="valid") into all ~20 registries; real-validator coverage only in siblings test_close_task_flow.py:56 and test_lifecycle_validation_feedback.py:50 |
| 20 | fix | `tests/servers/routes/test_admin.py:test_reload_workflows_manager_exception L1093` | test-types ratchet (#18781/#18783) | **VALID** · S · T11 — `(self, client, mock_server, caplog: pytest.LogCaptureFixture) -> None` — client/mock_server untyped; mock_arm_cls untyped at L1219/1278/1296 |
| 21 | no-fix | `tests/sessions/test_codex_nested_exec_outcomes.py L489` | 0.5.0 forward-port (18a043f58) | **STALE** — Already parametrized with expected_outcomes and asserts against it; no if/else on tool_input anywhere |
| 22 | fix | `tests/storage/test_migration_contract.py:test_transcript_close_checklist_migration_removes_receipt_contract L72` | — | **VALID** · S · T10 — Asserts only tasks_require_validation_criteria and task_type='epic'; migration 342 L9/L11 contains `NULLIF(BTRIM(validation_criteria),'') IS NOT NULL` and `NOT VALID`, both unasserted. Receipt-deletion asserts L82-90 still match migration 344 |
| 23 | no-fix | `tests/tasks/test_validation.py` | 0.5.0 forward-port (18a043f58) | **STALE** — File does not exist (only test_validation_history/_logging/_prompt_budget.py); named test absent repo-wide |
| 24 | no-fix | `tests/utils/test_db_validation.py:test_invalid_projects L95-110` | psycopg `%s` hub contract (#19002) | **INVALID** — psycopg `%s` per convention; `$1-$6` raises ProgrammingError |
| 25 | no-fix | `tests/utils/test_validation.py L200-215, L416-420` | psycopg `%s` hub contract (#19002) | **INVALID** — Both INSERTs use `%s`; `$N` breaks them |
| 26 | no-fix | `tests/workflows/test_condition_helpers.py:_task L34-47` | 0.5.0 forward-port (18a043f58) | **STALE** — Already does `kwargs.setdefault("validation_criteria", ...)` then forwards `**kwargs` once |
| 27 | no-fix | `tests/workflows/test_memory_lifecycle_rules.py` | test-types ratchet (#18781/#18783) | **STALE** — Both already annotated `db: HubDatabase, manager: LocalWorkflowDefinitionManager` → None (L149-151, L175-179) |
| 28 | no-fix | `install/shared/workflows/agents/plan-adversary.yaml` | — | **INVALID** — L148-157 already state the 15-min deadline is best-effort with per-lane sequential fallback; `timeout: 2700` L15 is "the only enforced wall-clock bound". A YAML prompt cannot runtime-enforce subagent deadlines |
| 29 | fix | `sessions/summarize.py:generate_session_summaries` | — | **VALID** · M · T5 — L509 `result = dict(core_result.result)` shallow; nested `session_wiki_file` + `context_summary` (L425-431) shared across joiners. Docstring L451-474 never states dependency inheritance. Docstring half overlaps 37 |
| 30 | fix | `storage/sessions/_field_update.py:revive_expired_terminal_session` | — | **VALID** · S · T2 — `reset_target_transcript = current.status == "expired"` L208 reads pre-lock snapshot from L152, not FOR UPDATE rows L187-198 |
| 31 | fix | `tests/sessions/test_summarize.py L504-505` | — | **VALID** · S · T11 — Unbounded waits survive 98b0d6a4e; also unbounded at L688, L758, L854. The 1s pattern exists at L542 |
| 32 | fix | `tests/skills/test_plan_review_skill.py L205-209` | — | **VALID** · M · T12 — Asserts bare substrings that also match the NEGATED wording in the skill body |
| 33 | no-fix | `tests/tasks/test_validation_verdict.py` | bounded async DB ops (#19006) | **STALE** — File deleted; `validation_result_from_data` has zero hits in src/ |
| 34 | fix | `tests/test_failure_categories.py:test_persisted_validation_status L16-31` | — | **VALID** · S · T11 — 5 cases; neither ("invalid",None,"invalid") nor ("pending",None,"pending") present; "pending" is a real ValidationStatus literal (failure_categories.py:29) |
| 35 | no-fix | `crates/gcode/src/commands/status/prune.rs mod serial_db` | 0.5.0 forward-port (18a043f58) | **STALE** — Helpers exist only in prune.rs; invalidate.rs:280 uses its own insert/delete_indexed_project — nothing duplicated to extract |
| 36 | fix | `crates/gwiki/src/commands/session_sync.rs:run_persistent_write_phases` | — | **VALID** · M · T12 — L91 positional tuple `(&mut conn, &search_scope, &mut progress)` destructured positionally by three closures L94/111/115 |
| 37 | fix | `sessions/summarize.py docstring` | — | **VALID** · S · T5 — Says only "share load, generation, persistence, and wiki output"; never states joiners reuse originator's llm_service/session_summary_config/db/session_manager (L493-494 only logs debug) |
| 38 | fix | `sessions/summarize.py:_summary_tasks` | bounded async DB ops (#19006) | **VALID** · M · T5 — Plain module global keyed by session_id only. Concrete cross-loop path: hooks/session_summary_dispatcher.py:86-94 runs `asyncio.run(coro)` on a fresh thread loop while daemon loop also runs it → `await asyncio.shield(task)` L496 can hit a foreign-loop task |
| 39 | fix | `storage/external_issue_sync.py:from_row` | — | **VALID** · S · T2 — `except json.JSONDecodeError: raw_statistics = {}` L49-50 silent; module imports no logger |
| 40 | fix | `storage/sessions/_field_update.py` | — | **VALID** · S · T2 — `reset_transcript = reset_target_transcript and candidate.id == session_id` L220 lacks `current.id == owner.id` guard |
| 41 | fix | `storage/sessions/_field_update.py` | — | **VALID** · S · T2 — `if not matching: return self.get(session_id)` L204-205 runs inside open transaction L186 with FOR UPDATE locks held |
| 42 | fix | `storage/tasks/_plan_enhancement.py:record_plan_enhancement` | — | **VALID** · S · T2 — Bare `get_task(db, task_id)` L131 discards result, undocumented |
| 43 | no-fix | `storage/tasks/_stage_state_transitions.py:transition` | — | **INVALID** — `_transaction_mutation` is NOT public — all refs inside gobby.storage.tasks; underscore is accurate |
| 44 | fix | `sync/external_coordinator.py:run/_guard_run` | — | **VALID** · M · T6 — run()'s finally cancels tasks; _guard_run/_run_linear/_run_github catch only Exception, so CancelledError (BaseException) skips every terminal _write_status → row stuck "running" (L259/357) |
| 45 | fix | `sync/external_coordinator.py:_is_rate_limit` | D2 sibling-marker rule | **VALID** · S · T6 — `re.search(r"\b429\b", ...)` with no HTTP/status context → "Issue #429 not found" flips project to rate_limited with 30s backoff |
| 46 | fix | `sync/github_issue_sync.py:recover_project` | — | **VALID** · S · T6 — `for page in range(1, _MAX_GITHUB_RECOVERY_PAGES+1) ... else: stats["errors"] += 1` fires when 100 full pages are legitimately consumed |
| 47 | fix | `tests/mcp_proxy/tools/test_memory_tools.py L655-659` | test-types ratchet (#18781/#18783) | **VALID** · S · T11 — memory_registry and mock_memory_manager bare; only `-> None` present |
| 48 | fix | `tests/servers/routes/test_memory_routes.py L547` | test-types ratchet (#18781/#18783) | **VALID** · S · T11 — `-> None` present; client/mock_server untyped |
| 49 | no-fix | `tests/sessions/test_codex_nested_exec_outcomes.py L342` | test-types ratchet (#18781/#18783) | **STALE** — Already `-> None` |
| 50 | no-fix | `tests/sessions/test_summarize.py` | — | **DUPLICATE** — Identical ask |
| 51 | fix | `tests/storage/sessions/test_lifecycle.py L507/L552/L771` | — | **VALID** · S · T10 — `== 0` vs `is False` used at L240/L328 in same file; psycopg returns bool |
| 52 | fix | `tests/sync/test_external_coordinator.py:test_wait_for_idle_drains_dispatched_work` | bounded async DB ops (#19006) | **VALID** · S · T12 — L260-261 assert `not draining.done()` immediately after create_task with no `await asyncio.sleep(0)` — trivially true |
| 53 | fix | `tests/sync/test_github_issue_sync.py:record_to_thread L294` | — | **VALID** · S · T12 — `getattr(func,"_mock_name",None) or func.__name__` — a plain MagicMock raises AttributeError; latent |
| 54 | no-fix | `tests/workflows/test_skill_discovery_rules.py L3422` | psycopg `%s` hub contract (#19002) | **INVALID** — `VALUES (%s, %s)` is correct psycopg; `$N` breaks the test |
| 55 | fix | `cli/github.py:_check_github_access_result/_gather_github_access` | — | **VALID** · M · T7 — L84 catches only GitHubRepositoryReadinessError; L94 gather() lacks return_exceptions=True → any other exception aborts all sibling project checks |
| 56 | no-fix | `install/shared/workflows/rules/memory-lifecycle/digest-on-response.yaml` | 0.5.0 forward-port (18a043f58) | **STALE** — Rule renamed to digest-catch-up-on-turn-start; L1 already carries the required tags incl. gobby and default |
| 57 | fix | `memory/digest.py:_read_undigested_turns L255-279` | — | **VALID** · S · T7 — prior_turn_only backward task_started scan + truncation still inline; no helper. Pure readability, no behavior change |
| 58 | no-fix | `runner_lifecycle_periodic.py:_has_enabled_external_issue_integration` | — | **INVALID** — `MCPClientManager.get_server_config` is `manager._configs.get(name)` (cannot raise); `MCPServerConfig.enabled: bool = True` is a dataclass field → try/except is a no-op. Predicate already guards callable() and is-not-None. Real (cosmetic) gap: `mcp_manager: Any` annotation |
| 59 | fix | `servers/provider_model_defaults.py L307/315` | — | **VALID** · S · T7 — "GLM-5.2 (Droid Core)" / "GLM-5.2 Fast (Droid Core)" vs sibling convention "Droid Core (<model>)" used by kimi-k2.6, glm-5.1, glm-5, minimax-m2.5 |
| 60 | no-fix | `sessions/summarize.py` | bounded async DB ops (#19006) | **DUPLICATE** — Extra clause already satisfied: `_remove_summary_task` L76-83 already does `if _summary_tasks.get(session_id) is task` |
| 61 | no-fix | `sessions/summarize.py docstring` | — | **DUPLICATE** — Identical docstring ask |
| 62 | fix | `storage/external_issue_sync.py:upsert` | — | **VALID** · S · T2 — `cast(Mapping,...fetchone(...))` L121-159 no None guard before `from_row` L160; no RuntimeError path exists (finding premise partly wrong) |
| 63 | no-fix | `storage/external_issue_sync.py` | — | **DUPLICATE** — Same handler; "established logger" premise wrong — module has none |
| 64 | fix | `storage/sessions/_field_update.py` | — | **VALID** · S · T2 — FOR UPDATE query L187-198 missing `AND status != 'deleted'` used at L84 and L118 |
| 65 | fix | `storage/sessions/_field_update.py` | — | **VALID** · S · T2 — L259-260 emits "session_updated" for all incl. expired; update_status L62 / expire_if_active L141 emit "session_expired" |
| 66 | no-fix | `storage/sessions/_field_update.py` | — | **DUPLICATE** — Same no-match branch L204-205 |
| 67 | fix | `external_coordinator._is_rate_limit + github_issue_sync._is_rate_limit_error` | D2 sibling-marker rule | **VALID** · M · T6 — Two divergent impls: coordinator `\b429\b` + 6 markers, no attr check; sync bare substring "429" + retry_after attrs. Neither imports the other |
| 68 | fix | `sync/github_issue_sync.py:_normalize_issue_number` | — | **VALID** · S · T6 — `_MAX_GITHUB_ISSUE_NUMBER = 2_147_483_647` defined at github_issue_sync.py:24 AND task_github_import.py:25; same check duplicated inline at task_github_import.py:177-181 |
| 69 | no-fix | `sync/task_github_import.py:25` | — | **DUPLICATE** — Same consolidation, other call site |
| 70 | no-fix | `tests/dispatch/test_skill_composition.py` | asyncio_mode="auto" (pyproject:176) | **INVALID** — asyncio_mode="auto" and module has `pytestmark = pytest.mark.integration` L22 — decorator is a no-op |
| 71 | no-fix | `tests/sessions/test_summarize.py` | — | **DUPLICATE** — Identical ask with explicit timeout=1 |
| 72 | fix | `tests/storage/test_storage_tasks.py L1590-1631` | — | **VALID** · S · T10 — CREATE FUNCTION L1590-1601 and CREATE TRIGGER L1602-1609 execute BEFORE `try:` L1611 → leak past finally DROPs L1630-1631 |
| 73 | fix | `tests/sync/test_external_coordinator.py:test_run_survives_recoverable_refresh_failure` | — | **VALID** · S · T12 — L282 `await coordinator.run(shutdown)` unbounded while sibling L259 uses wait_for(timeout=1.0) |
| 74 | fix | `tests/sync/test_linear_sync.py:_replace_for_test L113-115` | — | **VALID** · M · T12 — `object.__setattr__` with no restore; 20+ call sites leak mutations across tests |
| 75 | fix | `tests/tasks/test_diff_paging.py L139-140` | — | **VALID** · S · T12 — Indexes `emitted[0]` with no `len(emitted) == 1` assertion |
| 76 | no-fix | `adversary § 6.2` | bounded async DB ops (#19006) | **STALE** — Approved round 22 + M1 manifest + shipped (migration 345) \| — |
| 77 | no-fix | `adversary § 2.2` | 0.5.0 forward-port (18a043f58) | **STALE** — Defect text survives but plan approved/expanded; prose edit cannot reach built code \| — |
| 78 | no-fix | `adversary § 3.1` | 0.5.0 forward-port (18a043f58) | **STALE** — Same \| — |
| 79 | no-fix | `adversary § 2.3` | 0.5.0 forward-port (18a043f58) | **STALE** — Same; overlaps 144 \| — |
| 80 | no-fix | `adversary § 6.5` | 0.5.0 forward-port (18a043f58) | **STALE** — Same \| — |
| 81 | no-fix | `adversary § 6.1` | test-types ratchet (#18781/#18783) | **STALE** — Already built beyond plan: verify_index_token/IndexToken/last_indexed_at shipped in #19080, #19082 \| — |
| 82 | no-fix | `coderabbit-fixes/11-new-batch.md` | 0.5.0 forward-port (18a043f58) | **STALE** — File does not exist \| — |
| 83 | no-fix | `coderabbit-fixes/11-new-batch.md` | 0.5.0 forward-port (18a043f58) | **STALE** — File does not exist \| — |
| 84 | fix | `herdr § 4.1 L687, acceptance 4.1.8 L699` | — | **VALID** · M · T8 — Protocol has Welcome{host_epoch} L499 but frame-client prose/4.1.8 never require comparing to stored host_epoch before AttachTerminal |
| 85 | fix | `herdr § 4.1 L681` | — | **VALID** · S · T8 — 3.2 corrected (L514 "two producers") but 4.1 still asserts "the only place… ever produced" in same paragraph as "one of the two producers" |
| 86 | fix | `herdr § 3.1 L474` | — | **VALID** · M · T8 — Still kills unknown terminal_id after grace with no requirement the DB inventory read succeeded; no indeterminate/retry treatment |
| 87 | fix | `herdr § 2.1 L185, § 2.4 L395, acceptance 2.4.10 L409` | — | **VALID** · M · T8 — Partially repaired ({action_key, at} + origin) but still ONE JSONB latch per terminal; second action overwrites first's suppression |
| 88 | fix | `herdr § 4.1 heading L673` | — | **VALID** · S · T8 — 4.1 is `(depends: 2.2, 3.2)` but 3.4 (L567) changes AttachTerminal + frame payload + regenerates golden corpus (3.4.10). 4.3 already depends on 3.4; 4.1 does not. Adding 4.1→3.4 stays acyclic |
| 89 | fix | `herdr § 3.2 L510, acceptance 3.2.10 L532` | test-types ratchet (#18781/#18783) | **VALID** · S · T8 — Ledger defines seq>high_seq/retained/evicted_below and claims totality but is silent on concurrent/out-of-order arrivals; L505 serialization is about PTY bytes |
| 90 | no-fix | `herdr § 2.1 L250` | psycopg `%s` hub contract (#19002) | **INVALID** — Plan text ("psycopg `%s` placeholders") is CORRECT; `$N` contradicts project convention \| — |
| 91 | no-fix | `herdr § 3.4 L579-607` | 0.5.0 forward-port (18a043f58) | **STALE** — Already weakened in round 4: L593 cites 3,000-poll repro, 3.4.9 says "sampling fidelity rather than stream fidelity" \| — |
| 92 | fix | `m0 § 2.4 L281-288, acceptance 2.4.1` | — | **VALID** · S · T8 — Draft, nothing built. Still "machine_id = local OR machine_id IS NULL" → B sweeps A's legacy-NULL cron runs. Given no-backcompat, stronger fix is NOT NULL from the start |
| 93 | fix | `m0 § 4.1 L361-388` | — | **VALID** · M · T8 — Harness is two daemons on one temp Postgres, but item 3 requires vector+graph memory round trip and 4.1.1 claims automated coverage; no Qdrant/FalkorDB provisioning |
| 94 | fix | `m0 § 1.2 L112-119` | — | **VALID** · S · T8 — "--bind accepts an IP or 0.0.0.0" + Qdrant API key explicitly deferred = unauthenticated ports; only mitigation is Tailscale ACL prose |
| 95 | fix | `m0 § 2.1 L177-196` | — | **VALID** · S · T8 — Backfill still falls back to "most-recent machines row" for worktrees/clones/agent_runs |
| 96 | fix | `.gobby/plans/split-workflow-definition-storage.md` | — | **VALID** · S · T8 — Bullet reviews a workflow_instances migration described in the split plan; no such migration exists in src/gobby/storage/migrations. Belongs with the open-draft plan-doc edits (P04 bucket), not src |
| 97 | no-fix | `split acceptance 3.2.1a L1036` | — | **INVALID** — Letter-suffixed ids are this plan's convention (3.2.3a/b, 3.2.13a/b/c, 3.2.14a/b), survived 5 rounds; 3.2.1a is unique, not a collision \| — |
| 98 | no-fix | `split § 1.x L259-260` | psycopg `%s` hub contract (#19002) | **INVALID** — `db.transaction()` + `%s` is correct for Gobby \| — |
| 99 | fix | `split acceptance 3.2.13a L1051 vs 3.2.13c L1053` | — | **VALID** · S · T8 — Prose L831-836 deliberately excludes resume_executor.py:192 and 3.2.13c pins inline prepare_terminal_spawn, yet 3.2.13a still says "and the resume path". Fix = delete four words |
| 100 | fix | `split § 3.2 L797-836, acceptance 3.2.8 L1045` | — | **VALID** · M · T8 — Hoisting prepare_terminal_spawn creates child session + agent_runs row before snapshot save; 3.2.8 only covers "before any child process starts"; pre-launch save-failure leaks both rows |
| 101 | fix | `wiki § 4.2 L1104-1111, § 4.7 L1558-1569` | — | **VALID** · M · T8 — Admission records scope on executing + queued row, but monotone merge and 4.7.8 cover only the queued successor; scope admitted after step 0 planned items mutates metadata only |
| 102 | fix | `wiki § 5.1 acceptance 5.1.5 L1664, § 5.3 L1724-1728` | — | **VALID** · M · T8 — Round 5 fixed only 5.3 (move + link-rewrite journal). 5.1 still replaces six shared deterministic pages with expected-hash precondition and NO byte backup |
| 103 | fix | `wiki § 4.3 L1229-1263, acceptance 4.3.4` | — | **VALID** · M · T8 — Five finalizer mutations ordered but no per-step progress persisted; 4.3.4 only asserts vault consistency after crash injection |
| 104 | fix | `wiki § 3.5 L775-792, § 3.6 L859-863` | — | **VALID** · M · T8 — Preconditions are --expected-hash/--expected-absent against filesystem state only; no per-path generation or tombstone fence, no delete-then-delayed-create acceptance |
| 105 | no-fix | `wiki § 4.7 L1585-1602` | 0.5.0 forward-port (18a043f58) | **STALE** — Explicitly ADJUDICATED AND DECLINED in round 5 with three named guards and resolution note L2189-2193 \| — |
| 106 | no-fix | `agents/idle_check_handler.py` | 0.5.0 forward-port (18a043f58) | **STALE** — File is 374 lines; no isEnabledFor, no logger.log(, no log_args anywhere |
| 107 | fix | `agents/lifecycle_monitor.py:_check_loop` | — | **VALID** · S · T4 — L365-368 callbacks inside shared tick try L363 whose except L397 aborts remaining checks; stale-sweep L387-394 has its own guard |
| 108 | fix | `agents/recovery_state.py:is_daemon_stop_parked` | — | **VALID** · S · T4 — Missing `daemon_stop_orphan_reap_started_at` check that finalize_daemon_resume rejects on (agent_resume.py:134) |
| 109 | fix | `agents/resume_executor.py:_INHERITED_PROTOCOL_KEYS` | — | **VALID** · S · T4 — L48-55 use constants; four raw strings remain L56-59 |
| 110 | no-fix | `agents/resume_executor.py L473` | — | **INVALID** — 105 chars but pyproject ruff `ignore` includes E501 (L243); ruff format won't split a string literal — no violation |
| 111 | fix | `agents/resume_finalization.py:notify_parent_of_recovery` | — | **VALID** · M · T4 — L146-160 SELECT 1 then create_message; postgres_baseline_schema.sql:1045-1064 has only non-unique indexes → concurrent double-notify |
| 112 | fix | `agents/resume_finalization.py:finalize_resume_handoff_threadsafe` | — | **VALID** · S · T4 — L120-121 call_soon_threadsafe + result(timeout) unguarded after is_running() check L99 |
| 113 | fix | `agents/terminal_prompt_monitor.py:_is_expected_prompt_probe_error` | D2 sibling-marker rule | **VALID** · S · T4 — `_VANISHED_TMUX_ERROR_MARKERS` L21-27 includes bare "no such file or directory" → missing tmux binary downgraded to debug L53-58 |
| 114 | fix | `agents/tmux/output_reader.py:_log_command_result` | — | **VALID** · S · T4 — Hardcoded "tmux pipe-pane" L117/126/134/141; `_run` passes only target/result L178. Cosmetic today (both call sites L222, L265 are pipe-pane) |
| 115 | fix | `agents/tmux/output_reader.py:_run` | — | **VALID** · S · T4 — L169 `await proc.communicate()` after kill has no timeout |
| 116 | fix | `autonomous/progress_tracker.py:_is_passive_wait_tool` | — | **VALID** · S · T4 — L172 `compact.endswith(leaf.replace("_",""))` no namespace boundary → "no_wait_agent"/"hardwaitagent" misclassified |
| 117 | fix | `autonomous/stuck_detector.py:detect_tool_loop` | — | **VALID** · M · T4 — L292-293 unconditional continue on is_passive_wait, no counter; record_event still records it as progress L422/452 |
| 118 | fix | `dispatch/daemon_resume.py:try_resume_daemon_stop_run` | — | **VALID** · S · T7 — L59 function-body import of increment_daemon_resume_failure_count (fn starts L41), used L71 and L100; no circular-import reason |
| 119 | fix | `dispatch/daemon_resume.py:_handle_resume_failure` | — | **VALID** · M · T7 — L146-152 schedule_dispatcher_tick_for_task with no delay even for deterministic errors like "services_missing:agent_runner,session_manager" (L62) → tight retry loop to _MAX_RESUME_FAILURES |
| 120 | fix | `hooks/agent_run_ingress.py:validate_managed_agent_hook` | — | **VALID** · S · T6 — `getattr(run,"resume_metadata_json",None) or {}` then `.get(...)` — truthy non-dict raises AttributeError |
| 121 | fix | `install/shared/workflows/agents/tech-writer.yaml L46, L137` | — | **VALID** · S · T12 — Both say "close_task with preview=true and the commit_sha" — no changes_summary, yet _lifecycle_close.py:160-165 hard-gates "Leaf tasks require changes_summary"; siblings nightly-linter/nightly-test-fixer/wiki-researcher all pass it |
| 122 | no-fix | `install/shared/workflows/agents/tech-writer.yaml L150-156` | 0.5.0 forward-port (18a043f58) | **STALE** — Already has `tool_input.get('task_id') == vars.get('assigned_task_id')` plus both closed/result.closed checks |
| 123 | no-fix | `mcp_proxy/tools/tasks/_lifecycle_close.py` | 0.5.0 forward-port (18a043f58) | **STALE** — Neither `_close_task_once` nor `run_once` exists; preview goes through CloseEvaluation.response (_lifecycle_close_preview.py:90-121) which sets `"success": closed` default False |
| 124 | fix | `runner_lifecycle_agents.py:_cleanup_missing_tmux_agent_run` | — | **VALID** · S · T7 — L678-685 logs warning on `not result.success` then still `return True`. Fix: `return result.success` |
| 125 | fix | `runner_lifecycle_agents.py:_reclassify_reconciliation_pending_runs` | — | **VALID** · M · T7 — L581-582 clears reconciliation_pending for the whole pending list regardless of what _reconcile_agent_runs_after_restart (L580, returns only a count) resolved |
| 126 | fix | `runner_lifecycle_agents.py (three fns)` | bounded async DB ops (#19006) | **VALID** · M · T7 — `_run_db` helper exists L33 and is used at 57/68/100/604/632/644, but update_runtime(348), transition_resume_phase(354), start(360), list_reconciliation_pending(569), merge_resume_metadata(582), session_manager.get(543), run_storage.get(549), merge_resume_metadata(552) run sync on the loop |
| 127 | no-fix | `runner_lifecycle_subsystems.py L788` | — | **INVALID** — Degraded path already logs: `_run_agent_hook_replay_barrier` emits logger.warning at agents.py:554 before returning False. Caller-side log duplicates it |
| 128 | no-fix | `sessions/analyzer.py:_format_tool_description` | — | **INVALID** — Wording deliberately changed to "Conditionally closed task" in 0cda20504 (#18989) because preview=true DOES close when ready. Reverting is regressive. Real gap: `_format_tool_description` only receives the tool-use block (L304-317), so confirming completion from output needs a design change |
| 129 | fix | `storage/agent_resume.py:finalize_daemon_resume` | — | **VALID** · M · T2 — Successor UPDATE L200-216 ignores `cursor.rowcount`; original already consumed, subscribers read L217-225 regardless |
| 130 | fix | `storage/agent_resume.py:increment_daemon_resume_failure_count` | — | **VALID** · M · T2 — `return ... else 0` L449 feeds dispatch/daemon_resume.py:71,100 → infinite reschedule for deleted run |
| 131 | fix | `storage/agents/_cleanup.py` | — | **VALID** · M · T2 — Unconditional `AND COALESCE({pending_flag_sql},'false') != 'true'` at L79 and L177 is a separate AND-term from provisional-phase clause |
| 132 | fix | `storage/agents/_runtime.py:transition_resume_phase` | — | **VALID** · S · T2 — Inline "daemon_stop_resume_phase" literals L88, L97 duplicate `daemon_resume_keys.RESUME_PHASE_KEY`; sibling `_queries.py:156` already uses constant |
| 133 | fix | `workflows/observer_commits.py:detect_mcp_commit_link` | — | **VALID** · M · T6 — `close_result = result if isinstance(result, dict) else tool_output` — a dict of unrelated metadata (MCP content/structuredContent envelope) never consults top-level tool_output["closed"] |
| 134 | fix | `workflows/observers.py:detect_task_claim_release` | — | **VALID** · M · T6 — Same defect: `close_result = tool_output.get("result", tool_output)` ignores top-level closed → claimed_tasks never cleared |
| 135 | fix | `tests/agents/test_lifecycle_monitor.py:_make_terminal_run/_make_dispatched_stage_run/_make_autonomous_run` | — | **VALID** · S · T9 — Same `UPDATE sessions SET agent_run_id = %s WHERE id = %s` at L501-505, L556-559, L598-602 |
| 136 | fix | `tests/agents/test_resume_executor.py (module)` | D2 sibling-marker rule | **VALID** · S · T9 — No pytestmark; pure mock-based (no temp_db) → unit, matching test_runtime_cleanup.py:14 |
| 137 | fix | `tests/hooks/test_session_handoff_handlers.py:_make_row L52-67` | — | **VALID** · S · T10 — L56 default None + L67 ternary makes None unreachable; L247-248 shows the workaround. Survives the 98b0d6a4e rewrite |
| 138 | fix | `tests/hooks/test_session_handoff_handlers.py L198` | — | **VALID** · S · T10 — `"source" not in input_data or input_data.get("source") != "compact"` is tautologically weaker |
| 139 | fix | `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py L241` | D2 sibling-marker rule | **VALID** · S · T11 — Module has `pytest.mark.unit` L35; test takes `db: HubDatabase` from hub_db L57 and does real workflow/session persistence |
| 140 | fix | `tests/servers/test_mcp_routes.py L3780` | D2 sibling-marker rule | **VALID** · S · T11 — Module `pytest.mark.unit` L69 yet test consumes `session_storage: SessionManager` built from temp_db L98-100 |
| 141 | fix | `tests/storage/test_agent_resume.py L1-16` | D2 sibling-marker rule | **VALID** · S · T10 — No `import pytest`, no pytestmark, every test takes temp_db. Sibling convention here is `unit`, not integration |
| 142 | fix | `tests/test_runner_lifecycle_restart_replay.py:_runner L430-470` | — | **VALID** · M · T11 — Derives `parked` from run_storage.list_active and mutates caller fixtures (`parked.resume_metadata_json = {}`, `parked.child_session_id = "child-1"`); no parked_run/provisional params |
| 143 | no-fix | `.gobby/plans/adversary-convergence-improvements.md` | 0.5.0 forward-port (18a043f58) | **STALE** — Plan approved at round 22 with populated M1 manifest; work already shipping (migration 345, tasks #19069/#19080/#19082). Same basis as 76-81. |
| 144 | no-fix | `.gobby/plans/adversary-convergence-improvements.md` | 0.5.0 forward-port (18a043f58) | **STALE** — Plan approved at round 22 with populated M1 manifest; work already shipping (migration 345, tasks #19069/#19080/#19082). Same basis as 76-81. |
| 145 | no-fix | `.gobby/plans/adversary-convergence-improvements.md` | bounded async DB ops (#19006) | **STALE** — Plan approved at round 22 with populated M1 manifest; work already shipping (migration 345, tasks #19069/#19080/#19082). Same basis as 76-81. |
| 146 | fix | `agents/sandbox_policy.py:mcp_config_read_exceptions` | — | **VALID** · M · T4 — L178-184 iterate ALL servers.values(), granting read roots for any absolute dir arg of any MCP server; docstring scopes to gobby entry |
| 147 | fix | `agents/spawn_executor_support.py:_session_manager_validation_error` | — | **VALID** · S · T4 — required_methods L165-169 omits update_sandbox_policy_hash, called unguarded at L230 (contrast resume_executor.py:284-290) |
| 148 | no-fix | `agents/watchdog/recovery.py:_idle_reprompt_message` | — | **INVALID** — `context_resolved: bool = False` gate exists L296-300; supplied context never overwritten. Discarding _lookup_succeeded is a no-op — every failure path returns (None, False) and format_reprompt_message(None) yields the fallback |
| 149 | fix | `agents/watchdog/recovery.py:_record_watchdog_task_event` | — | **VALID** · S · T4 — Bare except Exception L387-394 and L419-425. NOTE: get_task raises ValueError for missing row, so the broad handler swallows it and `if task is None: return` L395 is DEAD CODE |
| 150 | no-fix | `agents/watchdog/recovery.py:_complete_if_step_workflow_finished` | — | **INVALID** — No-op: `_load_step_workflow_context` returns (None, False) on all failure paths L272/280/288; existing `if step_context is None` L479 already short-circuits |
| 151 | no-fix | `agents/watchdog/transcript_resolver.py` | 0.5.0 forward-port (18a043f58) | **STALE** — Already fixed: L7 imports public `find_transcript_on_disk` (transcript_paths.py:13) |
| 152 | fix | `agents/watchdog/transcript_resolver.py:TranscriptResolver.resolve` | bounded async DB ops (#19006) | **VALID** · S · T4 — Three blocking stats on the loop: is_current_file L42, L46 (os.path.isfile+getmtime L33-37), os.path.isfile L57; only find_transcript_on_disk offloaded L52 |
| 153 | fix | `cli/daemon.py:_start_dependency_errors` | — | **VALID** · S · T7 — L76 hardcodes managed_services=True while status() L793 computes it from `(gobby_home/"services"/"docker-compose.yml").is_file()`; caller L422 passes nothing |
| 154 | fix | `cli/install.py L476-480/500-507 + _install_daemon.py:_run_install_preflight` | — | **VALID** · M · T7 — With --all on a git repo and no CLIs, L477 sets install_hooks=True so no_supported_cli stays False, but preflight still runs with is_full_install=True and empty detected_clis → hard error _install_daemon.py:91-95. Hooks-only install blocked |
| 155 | fix | `communications/adapters/telegram_inbound.py:_normalized_reaction et al` | — | **VALID** · S · T7 — L122/139/146/149/157/159/212/217/225 use isinstance(..., dict) while _mentions_telegram_bot/_telegram_media_attachment use Mapping (L32/35/65/87) |
| 156 | fix | `communications/adapters/telegram_inbound.py:parse_telegram_update` | — | **VALID** · M · T7 — L272 json.loads result passed straight to _telegram_reaction_message (typed Mapping) with no shape check; msg_data L289 unchecked; `chat = msg_data.get("chat", {})` L302 yields None for explicit JSON null → AttributeError L303. Caller telegram.py:595-604 does not catch JSONDecodeError |
| 157 | fix | `hooks/event_handlers/_session_start/handoff.py:prepare_compact_continuation_variables` | — | **VALID** · S · T6 — `auto_inject = current_vars.get("auto_inject_handoff", True)` used raw; session vars are JSON free-form so "false"/"0" are truthy |
| 158 | fix | `hooks/session_coordinator.py:_terminate_agent_run` | — | **VALID** · S · T6 — `future.result(timeout=5)` while inline path runs three tmux subprocesses each timeout=5 plus DB work; one except clause logs "database executor was unavailable" for all three error types |
| 159 | fix | `mcp_proxy/tools/sessions/_terminal_tmux.py` | — | **VALID** · M · T5 — `_CODEX_INTERRUPT_SETTLE_SECONDS = 1.0` L32 is the unconditional default L154, passed for every source by _terminal.py:224; cli_source only selects the interrupt KEY (L29-41). Reviewer premise wrong — no existing shorter delay exists; a non-Codex default must be chosen |
| 160 | fix | `mcp_proxy/tools/spawn_agent/_health.py:_deferred_tmux_health_check` | — | **VALID** · M · T5 — L119-121 embed raw 4096-char pane tail verbatim into `error`; logger.error L122 and run_storage.fail L133-136 persist it to daemon logs and agent_runs.error. No redaction |
| 161 | fix | `mcp_proxy/tools/spawn_agent/_health.py:_check_tmux_session_alive` | — | **VALID** · S · T5 — L82 wraps get_session in wait_for(5.0); L87 `capture_pane` has no timeout; L88 bare except Exception (codebase elsewhere catches (TimeoutError, OSError, RuntimeError) — _terminal_tmux.py:67) |
| 162 | fix | `runner_lifecycle_processes.py:_expand_preserved_agent_processes` | — | **VALID** · M · T7 — L181-189 `psutil_module.Process(pid)` then `.children(recursive=True)` with no create_time comparison → recycled root PID preserves an unrelated tree |
| 163 | fix | `sessions/compact_continuation.py` | D2 sibling-marker rule | **VALID** · M · T5 — L348 `deadline = loop.time() + fresh_seconds` ignores marker created_at; `_take_compact_self_continuation_pending` L161-197 re-checks age and returns None, swallowed by bare return L395-396 with no warning |
| 164 | fix | `sessions/lifecycle.py:_expire_stale_sessions` | — | **VALID** · S · T5 — L300 discards prune_stale_compact_workflow_instances int return; L315 aggregates only paused+orphaned+expired+fast_expired+pruned. CAVEAT: counts workflow instances, not sessions — folding it in mixes units |
| 165 | fix | `storage/daemon_resume_keys.py` | — | **VALID** · S · T2 — `column: str` L22/L28 interpolated via json_text_expr with no allowlist; all 4 callers pass a literal — hardening only |
| 166 | no-fix | `migrations/343_...sql` | receipt subsystem deleted (763c5fc8d) | **INVALID** — Migrations applied exactly once in version order via schema_migrations; constraint deterministically created in 334:27-28 — DROP cannot fail |
| 167 | fix | `storage/tasks/_build_cascade.py` | — | **VALID** · M · T2 — `if specs: stage_states.initialize_manifest(...)` L131-132 skips reconciliation when specs empty, leaving stale child stage rows |
| 168 | no-fix | `storage/verification_receipts.py` | receipt subsystem deleted (763c5fc8d) | **STALE** — MODULE REMOVED (commit 763c5fc8d); table dropped in migration 344 |
| 169 | no-fix | `storage/verification_receipts.py` | receipt subsystem deleted (763c5fc8d) | **STALE** — Same removal; no `resolve_attribution` anywhere in src/gobby |
| 170 | fix | `utils/status.py L308, L368-369` | — | **VALID** · S · T7 — `_dependency_sections` L145-159 guards with isinstance dict, but `.get("integrations",{}).get("tailscale")`, `.get("services",{})`, `.get("integrations",{})` break on a present-but-null value. No `_mapping_section` helper |
| 171 | fix | `workflows/step_context.py` | — | **VALID** · M · T6 — `_get_active_step_workflow_context` and `first_incomplete_step_workflow` duplicate the whole scan; `first_incomplete_step_workflow` has NO try/except around the JSON parse, so a malformed definition raises there but is skipped in the other |
| 172 | no-fix | `workflows/verification_receipt_ingestion.py` | receipt subsystem deleted (763c5fc8d) | **STALE** — File does not exist; resolve_attribution gone with the deleted subsystem |
| 173 | fix | `tests/agents/test_lifecycle_monitor.py _check_loop ordering test` | — | **VALID** · M · T9 — Hard-coded 15-name tuple L4591-4607 fed to monkeypatch.setattr L4608 |
| 174 | fix | `tests/agents/test_lifecycle_monitor_stage_native.py` | — | **VALID** · M · T9 — Literal source-string asserts: L58 `'if terminal_reason != "daemon_stop":' in source`; L75/77 "Watchdog idle diagnostic". Behavioral test exists at tests/agents/test_agent_cleanup.py:62 |
| 175 | fix | `tests/agents/test_sandbox.py` | — | **VALID** · S · T9 — L1023-1024 compare raw str(); siblings L826/873/971 use str(...resolve()) → vacuous pass under symlinked tmp roots |
| 176 | no-fix | `tests/agents/test_spawn_prepare_resume.py (module)` | D2 sibling-marker rule | **INVALID** — D2: module already carries pytest.mark.unit; flipping to integration contradicts ~1269 unit vs ~50 integration repo-wide. Deferred to the post-0.5.0 convention epic. |
| 177 | fix | `tests/agents/test_srt_runtime.py` | — | **VALID** · M · T9 — parametrize L383 but both cases share `match="claude executable\\|Failed to resolve"` L408 |
| 178 | fix | `tests/agents/test_srt_runtime.py shutil.which stub` | — | **VALID** · S · T9 — L401-405 lambda lacks `mode`; correct form at L318-322 same file |
| 179 | fix | `tests/agents/tmux/test_pane_monitor.py` | — | **VALID** · S · T9 — L277 `!= [event_loop_thread_id]` also passes when list is empty or has extra entries |
| 180 | fix | `tests/dispatch/test_dispatcher.py:controlled_wait_for L846-848` | — | **VALID** · S · T12 — First call raises TimeoutError without awaiting/closing `awaitable` → "coroutine was never awaited" warning |
| 181 | fix | `tests/mcp_proxy/services/test_tool_proxy_coverage.py L476-478` | test-types ratchet (#18781/#18783) | **VALID** · S · T11 — Both params untyped, no return annotation |
| 182 | no-fix | `tests/mcp_proxy/test_mcp_proxy_stdio.py L1267` | test-types ratchet (#18781/#18783) | **STALE** — Already `(self) -> None`; all siblings L1001/1034/1081/1128/1155/1182/1211/1239 also annotated |
| 183 | fix | `tests/mcp_proxy/tools/spawn_agent/test_execution.py L1083, L1159` | — | **VALID** · M · T11 — Both patches return (True, None); the `not alive` branch in _implementation.py:759-765 that appends "Pane output:\n{pane_output}" is uncovered |
| 184 | fix | `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py L15-107` | — | **VALID** · M · T11 — No test raises from run_storage.get. NOTE: production `start_run_or_cleanup` (_failure_cleanup.py:88) calls it outside any try/except — the production guard must land first |
| 185 | fix | `tests/servers/test_mcp_routes.py L3780 + L3831` | — | **VALID** · M · T11 — Bodies byte-identical apart from raised error, envelope id, and reason string |
| 186 | no-fix | `tests/storage/tasks/test_cascade_build_state.py L301` | test-types ratchet (#18781/#18783) | **STALE** — Already annotated L302-304 |
| 187 | fix | `tests/storage/test_agent_resume.py:test_increment_failure_count_increments_numeric_value L457` | — | **VALID** · S · T10 — L464 seeds the STRING "2", same type as the non-numeric sibling at L449 → numeric JSONB path never exercised |
| 188 | no-fix | `tests/storage/test_checkpoints.py L31-51` | psycopg `%s` hub contract (#19002) | **INVALID** — `VALUES (%s, %s, ...)` correct; `$N` breaks |
| 189 | no-fix | `tests/storage/test_manager_surface_parity.py L206-224` | psycopg `%s` hub contract (#19002) | **INVALID** — executemany uses `%s`; `$1-$6` breaks |
| 190 | no-fix | `tests/storage/test_task_affected_files.py L27` | psycopg `%s` hub contract (#19002) | **INVALID** — `%s` fixture; `$1-$6` contradicts the Hub contract |
| 191 | fix | `tests/test_runner_lifecycle.py:_serve_mock_until_should_exit` | — | **VALID** · S · T11 — Defined L4303 (last symbol in a 4311-line file), first used L216 |
| 192 | fix | `tests/utils/test_dependency_requirements.py L137, L173` | — | **VALID** · S · T11 — Both monkeypatch with `lambda: (_ for _ in ()).throw(SrtRuntimeError(...))` |
| 193 | fix | `tests/utils/test_dependency_requirements.py L251-268` | — | **VALID** · M · T11 — Single test sequentially re-monkeypatches os.name ("nt" then "posix") and platform.system in one body |
| 194 | fix | `tests/utils/test_dependency_requirements.py L183, L215` | — | **VALID** · M · T11 — healthy DependencyStatus, compose_minimums, command_status closure, and four monkeypatch.setattr calls duplicated verbatim |
| 195 | fix | `tests/utils/test_dependency_requirements.py L100-110` | — | **VALID** · S · T11 — L105 passes `expected_version=SRT_RELEASE.version` but L110 asserts literal "0.0.66"; SRT_RELEASE already imported and used L127/128/151 |
| 196 | fix | `tests/utils/test_status.py L131, L146` | — | **VALID** · S · T11 — Hardcodes 119.999 and 120.0; `STARTING_GRACE_SECONDS = 120.0` exists at utils/dependency_requirements.py:24, unimported here |
| 197 | fix | `tests/workflows/test_agent_workflow_runtime_cleanup.py L102` | asyncio_mode="auto" (pyproject:176) | **VALID** · S · T10 — asyncio half INVALID twice: marker already at L101 AND asyncio_mode="auto". Annotation half stands: temp_db/sample_project L103-104 unannotated, as is the adjacent test L42-43 |
| 198 | fix | `AGENTS.md rule 8 L21-22` | — | **VALID** · S · T12 — "Failures confined to excluded dirty paths do not block your task's validation or close gates" with no requirement to demonstrate confinement via a passing scoped rerun |
| 199 | fix | `docs/guides/frontend-style-guide.md L599` | — | **VALID** · S · T12 — Still lists `chat/ └── ui/ # Shared UI primitives`; that directory does not exist and L250 says all shared primitives live in components/ui/ |
| 200 | fix | `docs/guides/frontend-style-guide.md + web/src/styles/dropdown-caret.css` | — | **VALID** · M · T12 — Stylesheet exists, imported by web/src/main.tsx:7, allowlisted at styleRatchet.allowlist.ts:241 — contradicting guide L648 "New CSS \\| Nowhere — new stylesheets are banned" |
| 201 | fix | `agents/code_index.py:settle_indexed_value` | test-types ratchet (#18781/#18783) | **VALID** · S · T4 — Normalization try L236-245 wraps only index_operation()/read_last_indexed_at(); `value = derive()` at L263 is outside it |
| 202 | fix | `agents/code_index.py:repository_source_digest` | — | **VALID** · L · T4 — L172 read_bytes() on every git-visible file; called twice per settle attempt (L235, L251) plus once in verify_index_token L187 = 3x full-repo reads. CAVEAT: prescribed size/mtime_ns/inode swap weakens the docstring "exact" contract and is racy at coarse mtime granularity |
| 203 | fix | `agents/code_index.py:settle_indexed_value` | — | **VALID** · M · T4 — L251-254 pins post-index digest to before.source_files even when caller passed None; verify_index_token reuses canonical.source_files L189 → a file created during indexing is never enumerated |
| 204 | fix | `dispatch/spawn.py:spawn_agent (async, L180)` | bounded async DB ops (#19006) | **VALID** · S · T7 — L392 stage_states.get(...) and L399 consume_plan_review_submission(...) are blocking sync DB calls on the loop; same fn already uses asyncio.to_thread at 226/235/283/293/301/311/329 |
| 205 | no-fix | `install/shared/workflows/agents/plan-adversary-taskless.yaml` | 0.5.0 forward-port (18a043f58) | **STALE** — The "sibling" plan-adversary.yaml has only claim/load_skill/review/terminate — no deliver_result, and L394 says "Do not send_message"; terminal cleanup relays the verdict |
| 206 | no-fix | `install/shared/workflows/agents/plan-adversary.yaml` | 0.5.0 forward-port (18a043f58) | **STALE** — No deliver_result step, no send_message in any tool list, `relay_backfill_failure` has zero hits under workflows/ |
| 207 | fix | `consumer_sweep.py:_direct_file_consumers` | — | **VALID** · S · T1 — L464 `fake = getattr(storage, "find_direct_file_consumers", None)`; callable/invoke L465-466; fallback L468 |
| 208 | fix | `consumer_sweep.py:derive_candidate_site_inventory` | — | **VALID** · S · T1 — L254 tuple from unordered set; repeats L304 in _sweep_section → nondeterministic SQL arg order |
| 209 | fix | `consumer_sweep.py:_target_language` | — | **VALID** · S · T1 — L560-567 ordered endswith scan instead of PurePosixPath(...).suffix lookup |
| 210 | fix | `review_coverage.py:_reject_unchanged_dismissal_reopens L496-527` | — | **VALID** · M · T1 — No `disposition == "emitted_finding"` filter; repeated dismissed disposition raises unchanged_dismissal_reopened L523 |
| 211 | no-fix | `review_findings.py:_validate_citation_list L257-275` | 0.5.0 forward-port (18a043f58) | **STALE** — Delegates to validate_source_citation → `_require_exact_citation_fields` (review_requirements.py:300-305, 314-319) |
| 212 | fix | `review_ledger.py:_merge_carry_resolutions` | test-types ratchet (#18781/#18783) | **VALID** · M · T1 — L334-335 silent `continue` on missing section_hashes; no stale mark, no typed refusal |
| 213 | fix | `review_ledger.py:_freshened_entry L477-497` | — | **VALID** · M · T1 — `_new_entry` L462-472 hardcodes first_seen_round=round_number, rounds_carried=1; only aliases restored L496 |
| 214 | fix | `review_manifest_service.py:apply_plan_review_manifest` | — | **VALID** · M · T1 — Pre-CAS L239-252 and post-CAS L274-292 duplicate digest/payload/revoked/applied checks; no `_assert_manifest_intent` |
| 215 | fix | `review_sweeps.py:validate_sweep_records` | — | **VALID** · L · T1 — L78-83 `combinations(candidates.values(), 2)` unscoped, unlike derive_repair_universe (review_repair.py:209-212) |
| 216 | fix | `servers/routes/mcp/endpoints/execution.py:_bind_agent_run_context` | — | **VALID** · S · T7 — L261 `db: Any`; call site L238-249 passes server.session_manager.db, L266 does LocalAgentRunManager(db). Concrete `HubDatabase \\| None` available |
| 217 | fix | `storage/tasks/_review_transitions.py:submit_for_review` | — | **VALID** · M · T2 — Separate `db.transaction()` L76-88 strips label after stages.submit_for_review L59-66 + update_task L70-75 |
| 218 | fix | `storage/tasks/_review_transitions.py:reject_review` | — | **VALID** · S · T2 — L398-406 rebuilds regex already in `_replace_round_section` L566-579 |
| 219 | no-fix | `tasks/expansion/_plan_gate.py L23` | — | **INVALID** — Module-level import of ConsumerInventoryError already loads the module at import time and does not break → no circular-import problem; moving it is a no-op and it must be bound BEFORE the except clause. Real inconsistency: the now-pointless lazy run_consumer_sweep import at L108 should be hoisted |
| 220 | no-fix | `tasks/expansion/_plan_gate.py:validate_plan_for_agent_spawn L46-59` | — | **INVALID** — Contradicts documented design: adversary-convergence-improvements.md:750-755 states that when the code index is unavailable "no spawn may proceed"; acceptance 5.2.4 pins it. Also all three ConsumerInventoryError raises (consumer_sweep.py:171/176/219) use code inventory_unavailable — no "genuine i… |
| 221 | fix | `tests/cli/installers/test_git_hooks_installer.py L815-824` | — | **VALID** · M · T12 — Measures time.monotonic() and asserts elapsed < 1, redundant with subprocess.run(timeout=1); waiter_file/curl_file checked with no settle window |
| 222 | fix | `tests/plans/test_review_coverage.py:test_sweep_universe_fixtures` | — | **VALID** · M · T9 — Single fn L423-503 mutates shared records/adjacent across five contracts |
| 223 | fix | `tests/plans/test_review_evidence_models.py (module)` | D2 sibling-marker rule | **VALID** · S · T9 — L1-15 no `import pytest`, no pytestmark; pure in-memory |
| 224 | fix | `tests/plans/test_review_evidence_store.py (module)` | D2 sibling-marker rule | **VALID** · S · T9 — No pytestmark; fixture takes temp_db L18, real Postgres inserts L33-40 |
| 225 | fix | `tests/plans/test_review_repair.py` | — | **VALID** · S · T9 — CWD-relative reads L894, L1039, L962, L1040; no ROOT constant (unlike test_review_coverage.py:28) |
| 226 | fix | `tests/review_learning/test_no_fix_policy_lesson.py L15-29` | — | **VALID** · M · T12 — Imports private `_finding`/`_round_result`/`_row`/StubReviewLearningService from sibling test modules; tests/review_coverage_helpers.py exists and hosts coverage_attestation |
| 227 | fix | `tests/review_learning/test_round_diff.py L85-90` | — | **VALID** · M · T12 — Asserts on the locally-built `_finding("F1")` dict — tests the test helper, not the production validator |
| 228 | fix | `tests/storage/test_stage_review_findings.py:_apply_round_one_repairs L290-298` | D2 sibling-marker rule | **VALID** · S · T10 — Blind `.replace("Implemented.", ...)` with no presence assertion; consumers L628, L802 pass vacuously if the template L92 changes |
| 229 | fix | `tests/tasks/test_validation_prompt_budget.py` | — | **VALID** · S · T12 — Parametrization `[(None, 32_001), (8_000, 8_001)]` is over-limit only; no equal-to-limit success case guarding a `>=` regression |
| 230 | no-fix | `tests/workflows/test_rule_engine_task_helper_wiring.py L168` | — | **INVALID** — Equivalent negative control already exists: test_require_epic_tree_close_uses_real_task_manager L43-76 asserts block + "Epic tree not complete" |
| 231 | fix | `web/src/__tests__/styleRatchet.test.ts:ratchet` | — | **VALID** · L · T3 — ratchet() compares scan to in-repo allowlist only; nothing diffs allowlist/CSS_TOTAL_LINE_CEILING vs target branch |
| 232 | fix | `web/src/components/activity/RulesTab.tsx` | — | **VALID** · M · T3 — L145-153 replaces selectedName when a filter hides it though it still exists in data.rules; bypasses guardedRun/confirmLeaveRef |
| 233 | fix | `web/src/components/activity/fields/FieldPrimitives.tsx TagsField` | — | **VALID** · M · T3 — L270-286 chip `inline-flex h-5`, raw remove button, no pointer-coarse:min-h-11 (style guide L306 = 44px floor) |
| 234 | fix | `web/src/components/activity/rules/RulesDetailPanel.tsx` | — | **VALID** · S · T3 — c908d3b9a deleted the Enabled SwitchField, replaced by Source row L281-284; no enabled control in form view |
| 235 | fix | `web/src/components/ui/Input.tsx` | — | **VALID** · S · T3 — L17 `border-destructive-foreground`; `--color-destructive` bridged in tailwind-theme.css:10 |
| 236 | fix | `web/src/components/ui/Tooltip.tsx:TooltipContent` | — | **VALID** · S · T3 — Content L10-19 rendered inline; TooltipPortal exported but zero call sites → clipped by .activity-panel overflow |
| 237 | no-fix | `.impeccable.md L293-300` | 0.5.0 forward-port (18a043f58) | **STALE** — States "raw `<button>` only inside components/ui"; the 378-line file has no ToolCallCard or artifact-panel guidance to contradict it |
| 238 | no-fix | `docs/guides/frontend-style-guide.md` | — | **DUPLICATE** — Same defect, same lines |
| 239 | fix | `docs/guides/ghook-user-guide.md L226, L247` | — | **VALID** · S · T12 — Reports ghook_version 0.7.2 and a v0.7.2 asset URL, but crates/ghook/Cargo.toml:3 is version = "0.7.3" |
| 240 | fix | `code_index/maintenance.py reindex loop` | — | **VALID** · M · T4 — L159-160 `except Exception: daemon_config_breaker.record_success()` closes a HALF_OPEN probe and resets backoff (sync_breaker.py:94); no record_inconclusive() exists |
| 241 | fix | `dispatch/spawn.py:spawn_agent L391-404` | — | **VALID** · M · T7 — Post-spawn block re-reads current_stage.artifact_refs[REPAIR_SUBMISSION_ARTIFACT_KEY] rather than the value `_prepare_plan_adversary_evidence` (L78, called L329) attested → compare-and-clear can match a submission written after evidence creation (TOCTOU). Distinct from 204 |
| 242 | no-fix | `install/shared/workflows/agents/plan-adversary-taskless.yaml` | — | **DUPLICATE** — Same ask, same stale premise |
| 243 | fix | `mcp_proxy/tools/agents_lifecycle_tools.py:kill_agent` | — | **VALID** · M · T5 — end_agent_run gates on `_review_completion_error` L192-198; kill_agent's success branch L312-324 calls `_complete_self_terminated_run` with no gate |
| 244 | fix | `mcp_proxy/tools/tasks/_live_session_label.py` | — | **VALID** · S · T5 — L32 and L43 catch only (KeyError, LookupError, ValueError); psycopg.Error escapes instead of failing closed |
| 245 | fix | `memory/dream/related.py:run_call` | — | **VALID** · S · T7 — L224 `async with asyncio.timeout(...)` wraps L226 `async with self._db_semaphore` (4 permits, L167) → contention wait counts against the 5s budget |
| 246 | fix | `memory/dream/related.py:run_call L225-237` | — | **VALID** · S · T7 — Branches duplicate create_task + return _CallOutcome(value=await task); {"keyword","hydration"} inline with no _DB_BACKED_CHANNELS constant. NOTE: conflicts with 245 as written — apply 245 first, then this as cleanup |
| 247 | fix | `consumer_sweep.py:derive_candidate_site_inventory` | — | **VALID** · S · T1 — L247, L261 `_target_language(consumer) or "unknown"` redundant; fn already returns "unknown" L563/567 |
| 248 | fix | `consumer_sweep.py:run_consumer_sweep` | — | **VALID** · M · T1 — Storage resolved/validated L169-179 then re-checked in derive_candidate_site_inventory L217-222 |
| 249 | fix | `review_coverage.py` | — | **VALID** · S · T1 — L517 `dismissal["reopenable"] is not section_hash_changed` identity comparison on deserialized value |
| 250 | fix | `review_evidence_models.py:_validate_non_attested_result` | — | **VALID** · S · T1 — `reason_code = reason.get(...)` L264 before needs_requirements early return L265-271 |
| 251 | no-fix | `review_evidence_store.py:get_by_dispatch_run` | 0.5.0 forward-port (18a043f58) | **STALE** — Unique partial index already in migrations/338_plan_review_evidence.sql:67-69 and postgres_baseline_schema.sql:588-590 |
| 252 | fix | `review_evidence_store.py:write_preparation_context` | — | **VALID** · M · T1 — UPDATE L300-316 requires finalized_at/expired_at NULL; fallback L327-329 returns current on match → silent success on finalized row |
| 253 | no-fix | `review_findings.py:_validate_citation_list` | — | **DUPLICATE** — Primary 211 is STALE |
| 254 | fix | `review_ledger.py:_merge_finding` | — | **VALID** · M · T1 — L375 `_canonical_key` raises on missing section (L620), aborting whole merge; inconsistent with silent continue L334 |
| 255 | no-fix | `review_ledger.py:_freshened_entry` | — | **DUPLICATE** — Primary 213 |
| 256 | no-fix | `review_manifest_service.py` | 0.5.0 forward-port (18a043f58) | **STALE** — derive_plan_review_manifest invoked once L194 and memoized on (evidence_id, routing_digest) L99-102,154, init L81 |
| 257 | fix | `review_manifest_service.py` | — | **VALID** · S · T1 — `resolved.read_bytes()` L254 unguarded → raw FileNotFoundError. TOCTOU half already fixed: L254 and atomic_write_bytes L293 both inside transaction_immediate(PlanReviewEvidenceMutation) |
| 258 | fix | `review_repair.py:derive_repair_universe` | — | **VALID** · S · T1 — L239-242 adjacent_variant_ids digest lacks prior_finding_id → collision across findings sharing a check_key |
| 259 | fix | `review_repair.py:_is_sha256 L902` | — | **VALID** · M · T1 — Duplicated hex-64 validators: review_ledger.py:22, review_coverage.py:31, review_requirements.py:26, servers/routes/attention.py:30, inline review_sweeps._required_sha256:610-614. review_findings.py no longer defines one (that part stale) |
| 260 | fix | `review_repair.py:derive_repair_universe` | — | **VALID** · L · T1 — L211 guard satisfied by universe-global contracts/targets (L195-196); L217-218/226-227 copy globals onto every edge |
| 261 | fix | `servers/_app_lifecycle.py L67-74` | — | **VALID** · S · T7 — `if gcode_gateway is not None:` has no else → CodeIndexTrigger silently unregistered; only log in the block is the except warning L75-76 |
| 262 | fix | `storage/tasks/_live_session_recovery.py` | — | **VALID** · M · T2 — task_dirty_paths spawns 1 git subprocess per path (task_dirty_state.py:16-21), called per task L82; scan L44-51 uses limit=0 |
| 263 | fix | `storage/tasks/_live_session_recovery.py` | — | **VALID** · M · T2 — Identical raw probe `SELECT session_id FROM session_variables WHERE session_id = %s` at L114-117 and L161-164 |
| 264 | fix | `storage/tasks/_live_session_recovery.py` | — | **VALID** · S · T2 — `session_manager.get(owner)` L60 unguarded; contrast guarded call L64-72 |
| 265 | no-fix | `storage/tasks/_review_transitions.py` | — | **DUPLICATE** — Same block L76-88 |
| 266 | no-fix | `storage/tasks/_review_transitions.py` | — | **DUPLICATE** — Same regex L398-406 |
| 267 | fix | `storage/tasks/_transitions.py:release_task_claim_if_owned` | — | **VALID** · S · T2 — `if cursor.rowcount` L220 (and escalate_task_if_owned L345) treats rowcount == -1 as success; escalate_task L309 uses explicit `== 0` |
| 268 | fix | `utils/session_context.py:reset_seeded_contexts L468-472` | — | **VALID** · S · T7 — Bare `except Exception` swallows everything; ContextVar.reset() only raises RuntimeError/ValueError. Same pattern repeats L473-477 |
| 269 | fix | `workflows/task_dirty_state.py:task_dirty_paths` | — | **VALID** · M · T6 — `committable_task_paths` runs one `git check-ignore -q` per path (utils/git.py:194), then task_dirty_paths runs one `git status --porcelain=v1 -- <path>` per survivor = 2N subprocesses |
| 270 | fix | `tests/agents/test_isolation_project_json.py → src/gobby/code_index/trigger.py` | — | **VALID** · S · T9 — `_flush` pops without `.cancel()` (trigger.py:143); call_later handles from `_schedule_file` (trigger.py:84) survive manual _flush |
| 271 | no-fix | `tests/cli/installers/test_git_hooks_installer.py` | D2 sibling-marker rule | **INVALID** — All 25 modules in tests/cli/installers/ use pytest.mark.unit (this one L29); mark.slow has zero hits anywhere in tests/cli |
| 272 | no-fix | `tests/cli/installers/test_git_hooks_installer.py L815-824` | — | **DUPLICATE** — Same removal; `time` imported L7 and used only at 815/817 → becomes unused |
| 273 | fix | `tests/code_index/test_sync_worker_breaker.py` | — | **VALID** · S · T12 — L302 `assert shutdown.is_set()` tautological (fake_sync_pass sets it L285); L294 `await sync_worker_loop(...)` has no timeout → a missed _sync_pass hangs the suite |
| 274 | no-fix | `tests/hooks/test_hook_manager.py L1591/L1618-1626` | — | **INVALID** — No shared constant exists: message is inline at hook_manager.py:383, reason inline at agent_run_ingress.py:86; the cited sibling test also hardcodes it and test_mcp_routes.py never references either |
| 275 | no-fix | `tests/hooks/test_runtime_compat.py` | D2 sibling-marker rule | **STALE** — Module already sets `pytestmark = pytest.mark.unit` L18 |
| 276 | fix | `tests/mcp_proxy/tools/test_live_session_label.py L71-95` | — | **VALID** · S · T11 — Each pytest.param eagerly builds a MagicMock-backed SimpleNamespace (helper L22-55) at collection time |
| 277 | fix | `tests/memory/test_dream_related.py` | bounded async DB ops (#19006) | **VALID** · S · T12 — L588 `assert elapsed < 0.2` wall-clock and L585 opaque asyncio.to_thread flush remain alongside the meaningful assertions |
| 278 | no-fix | `tests/plans/test_consumer_sweep.py:test_index_verifier_wrapper_registered L753` | asyncio_mode="auto" (pyproject:176) | **INVALID** — asyncio_mode="auto" — coroutine already runs; no @pytest.mark.asyncio in module |
| 279 | fix | `tests/skills/test_live_session_skill.py L20` | — | **VALID** · S · T12 — `load_skill(LIVE_SKILL_DIR, validate=False)` skips schema validation of the shipped SKILL.md |
| 280 | no-fix | `tests/storage/tasks/test_live_session_recovery.py L119` | — | **INVALID** — Bare `git init` matches ~25 other call sites, none isolated; test never commits or asserts branch names. Re-marking integration contradicts module `pytest.mark.unit` L20 and uniform `unit` across tests/storage/ |
| 281 | no-fix | `tests/workflows/test_rule_engine_task_helper_wiring.py` | — | **DUPLICATE** — Same request, phrasing differs |
| 282 | fix | `web/src/__tests__/cssTokenIntegrity.test.ts` | — | **VALID** · S · T3 — L146-153 reads only tailwind-theme.css; tokens.css never read despite the test name |
| 283 | no-fix | `web/src/components/activity/ActivityPanel.tsx:ActivityDropdown` | — | **INVALID** — tabs is always ACTIVITY_PANEL_DROPDOWN_TABS, deliberately alphabetized (ActivityPanelTabs.tsx:228); ActivityPanel.test.tsx:260 asserts that order |
| 284 | fix | `web/src/components/ui/Button.tsx:handleClick` | — | **VALID** · M · T3 — L44-51 guards asChild+disabled, but Radix Slot composes child handler first |
| 285 | fix | `web/src/components/ui/ScrollArea.tsx` | — | **VALID** · S · T3 — L9-19 tabIndex={0} + outline-none, no focus-visible ring |
| 286 | fix | `web/src/components/ui/Tooltip.tsx` | — | **VALID** · S · T3 — L14 z-50 vs Dialog.tsx:13,30 z-[250] |
| 287 | fix | `crates/gcode/src/index/security.rs:has_secret_extension L148-156` | — | **VALID** · S · T12 — For .token/.credentials/.api_key/.apikey `extension()` is None → suffix == "" and the full dotted name goes to is_plaintext_secret_name, which matches only undotted names → hidden secret files missed |
| 288 | fix | `hooks/_normalization_canonical.py:_is_structured_file_mutation` | — | **VALID** · S · T6 — mcp_tool branch's second clause `_compact_tool_name(leaf_name) in CANONICAL_WRITE_TOOL_NAMES` matches generic MCP leaf names (create, edit, replace) that are not file mutations |
| 289 | fix | `hooks/_normalization_paths.py:_extract_payload_paths` | D2 sibling-marker rule | **VALID** · M · T6 — `_PATCH_TEXT_FIELDS = ("command","patch","content","text","diff")` all go through _parse_apply_patch_paths → a Write whose content contains `*** Update File: x` injects phantom paths into record_edited_files/notify_file_changed |
| 290 | fix | `hooks/_normalization_tools.py L74-78` | — | **VALID** · S · T6 — Inlines `"".join(c for c in tool_name.lower() if c.isalnum())` while `_normalization_canonical._compact_tool_name` uses `.casefold()` — duplicated with divergent case-folding |
| 291 | fix | `hooks/event_handlers/_session_start/flow.py compact branch ~L514-562` | — | **VALID** · M · T6 — Compact branch calls only reconcile_compact_session_activity + cache_session_mapping; register_session only reachable via the elif, so terminal_context/project_id/workflow_name/agent_depth/sandbox_enabled are computed then discarded |
| 292 | fix | `hooks/event_handlers/_session_start/handoff.py:resolve_session_start_identity` | — | **VALID** · S · T6 — find_by_external_id lookups are guarded; the immediately following resolve_compact_continuation is unguarded — DB error escapes and blocks session start |
| 293 | fix | `hooks/event_handlers/_tool.py:_record_file_mutation ~L248-263` | — | **VALID** · S · T6 — `_notify_code_index` outside the `not in committable_paths` guard → duplicate notifications for inputs normalizing to the same path; `is_path_gitignored` import executed inside the loop |
| 294 | fix | `hooks/session_lookup.py:_resolve_session_id` | — | **VALID** · M · T6 — Method spans L255-434 (~180 lines); compact block ~6 indent levels deep. Working tree now clean |
| 295 | fix | `mcp_proxy/tools/sessions/_registration.py:register_session` | — | **VALID** · S · T5 — Ambient block L93-139 sits OUTSIDE the try/except L141-164; resolve_session_reference raises ValueError on not-found/ambiguous (storage/session_resolution.py:54-57) → the `ambient_session_id is not None` check L100 and ambient_session_not_found payload are partly unreachable |
| 296 | fix | `mcp_proxy/tools/tasks/_stage_review.py:approve_review` | — | **VALID** · M · T5 — L346 get_evidence guarded; post-approval fetches L405 (replay) and L463 unguarded — raise surfaces after approve_review committed. L405 also re-fetches what L346 loaded |
| 297 | fix | `review_ledger.py:validate_candidate_dispositions L230-296` | — | **VALID** · L · T1 — Duplicates review_sweeps._validate_dispositions L203-263; `_finding_details` L508-519 re-implements review_findings._validate_finding L180-191 |
| 298 | fix | `review_sweeps.py:_validate_dispositions` | — | **VALID** · S · T1 — L218-221 identical message for `in seen` and `not in candidates` cases |
| 299 | fix | `sessions/compact_identity.py:resolve_compact_continuation` | — | **VALID** · M · T5 — db.fetchall L36-48 has no recency predicate and no LIMIT; established pattern is _discovery.py:310-320 (ORDER BY updated_at DESC LIMIT with MAX_ACTIVE_SESSION_SCAN). LIMIT must not break `len(matching) > 1` ambiguity semantics |
| 300 | fix | `storage/session_activity.py:_notify_session_change` | test-types ratchet (#18781/#18783) | **VALID** · S · T2 — `getattr(manager,"_notify_session_change",None)` L198-200 untyped private lookup, no try/except; aborts ghost loop L120-132 post-commit |
| 301 | fix | `storage/session_activity.py:reconcile_compact_session_activity` | — | **VALID** · M · T2 — FOR UPDATE excludes current row (`AND id != %s` L76); reactivation UPDATE L105-115 ignores rowcount unlike `_activate_without_competitors` L154-158 |
| 302 | fix | `storage/tasks/_review_transitions.py:_recorded_approval_replay` | — | **VALID** · S · T2 — `approval_result.pop("quality_ledger", None)` L318 compared to `(evidence.quality_ledger or [])` L319 → None != [] raises approval_result_conflict |
| 303 | no-fix | `sessions/tmux_window_naming.py:_resolve_tmux_pane_ownership` | — | **INVALID** — Code moved out of summary_actions.py. `PaneOwnershipDecision(None, session_id, None, "invalid_identity")` already matches field order (terminal_ownership.py:39-47) and that module builds it positionally in 6 places — kwargs is a pure no-op |
| 304 | fix | `sessions/tmux_window_naming.py:_resolve_tmux_pane_ownership` | — | **VALID** · S · T6 — `candidates = [session]` unconditionally overwritten by find_by_terminal_identity; empty/drifted result → candidates == [] → resolve_pane_ownership returns invalid_identity (len(identities) != 1) instead of letting the requested session own the pane |
| 305 | fix | `tests/agents/test_plan_adversary_internal_research_definition.py` | — | **VALID** · S · T9 — L56 asserts exact compact JSON; semantic checks L53/55 already cover it |
| 306 | fix | `tests/hooks/test_provider_edit_attribution.py:_contract_records L20-21` | — | **VALID** · S · T10 — Raw json.loads per line, no substitution; every fixture record carries `"cwd":"<WORKSPACE>"`. CAVEAT: evaluated at collection time inside parametrize L24-28 → tmp_path unavailable, only the concrete-path option works |
| 307 | fix | `tests/hooks/test_provider_edit_attribution.py L29` | D2 sibling-marker rule | **VALID** · M · T10 — No category marker anywhere; L34 calls private `GrokAdapter()._normalize_event_data` while public `translate_to_hook_event` (grok.py:48-65) wraps it |
| 308 | no-fix | `tests/integration/test_edit_history.py` | — | **INVALID** — utils/git.py:187-194 documents that check-ignore misses AND errors (incl. non-git dirs) return False — the non-repo tmp_path result is already deterministic |
| 309 | fix | `tests/sessions/test_liveness_monitor.py L445` | — | **VALID** · M · T11 — Builds only range(3) records; `_LOG_SAMPLE_LIMIT = 10` (liveness_monitor.py:43) truncation at L187 never exercised |
| 310 | fix | `tests/storage/sessions/test_compact_identity_reconciliation.py L201` | — | **VALID** · S · T10 — L227 order-dependent tuple comparison on IDs with no ordering contract |
| 311 | fix | `tests/storage/sessions/test_lifecycle.py L653` | — | **VALID** · S · T10 — All three sessions use machine_id="machine" (L667/674/682); no machine-2 session → machine-boundary isolation untested |
| 312 | fix | `tests/test_runner_maintenance_tmux_repair.py L216` | — | **VALID** · S · T11 — Name unchanged; no test_repair_loop_enforces_resolved_owner* exists anywhere in tests/ |
| 313 | fix | `tests/test_terminal_ownership.py (module)` | D2 sibling-marker rule | **VALID** · M · T11 — No pytestmark; the 7 tests L64-156 never pass a requested_session_id absent from the candidate list |
| 314 | fix | `tests/workflows/test_block_rendering.py L25-32, L139` | — | **VALID** · M · T10 — Test-local regexes are byte-identical copies of blocked_tool_recovery.py L22-29; `_balanced_call_end` reimplemented L139 despite existing at blocked_tool_recovery.py:91 |
| 315 | no-fix | `tests/workflows/test_plan_mode_resolution.py L75` | D2 sibling-marker rule | **STALE** — Module already sets `pytestmark = pytest.mark.unit` L18 |
| 316 | no-fix | `tests/workflows/test_summary_actions.py` | 0.5.0 forward-port (18a043f58) | **STALE** — Rewritten by 98b0d6a4e to 32 lines; named test now at tests/sessions/test_tmux_window_naming.py:160. Underlying gap persists there: L202-205 bare `return_value=ownership` with no call-arg assertion |
| 317 | fix | `tests/workflows/test_task_claim_state.py:TestTargetTaskHasEdits L133-140` | — | **VALID** · S · T10 — Only two tests (L134, L137); no task_id=None short-circuit case, no non-list-value case |
| 318 | no-fix | `web/src/components/activity/RulesTab.tsx` | — | **DUPLICATE** — Same effect L145-153 |
| 319 | no-fix | `web/src/components/activity/fields/FieldPrimitives.tsx` | — | **DUPLICATE** — Same chip L270-286 |
| 320 | fix | `web/src/components/activity/memory/MemoryDetailPanel.tsx` | — | **VALID** · M · T3 — L175-183 `void onPromote(memory)` no .catch; Restore L122-134 sets restoreError |
| 321 | fix | `web/src/components/chat/styles/activity-panel.css` | — | **VALID** · M · T3 — L21-28 `.activity-panel *` strips scrollbars from every descendant incl. textareas/CodeMirror |
| 322 | no-fix | `web/src/components/chat/styles/rules-tab.css` | — | **INVALID** — No-op: all call sites compose .activity-chip which already sets white-space: nowrap (activity-panel.css:172) |
| 323 | fix | `agents/resume_executor.py:_park_unlaunched_successor` | bounded async DB ops (#19006) | **VALID** · S · T4 — async def L563-570 but calls sync terminalize_plan_review_run L594 and run_storage.cancel L601 on the loop |
| 324 | fix | `agents/run_completion.py:complete_and_deliver` | bounded async DB ops (#19006) | **VALID** · S · T4 — L37 bare sync `runner.get_run(run_id)` on the loop; later read L60-64 correctly wraps in read_terminal_run + run_terminal_delivery_offload |
| 325 | fix | `agents/runner_queries.py:complete_run` | — | **VALID** · S · T4 — L135-144 when review_outcome.handled the caller's `result=` is silently dropped; same in run_completion.py:51-52 |
| 326 | fix | `mcp_proxy/tools/agent_cancellation.py:terminalize_killed_agent_run` | bounded async DB ops (#19006) | **VALID** · S · T5 — L119-128 call sync terminalize_plan_review_run + run_storage.fail on the loop while run_terminal_delivery_offload is imported L114 and used at L149 |
| 327 | fix | `mcp_proxy/tools/agent_messaging.py:send_message` | — | **VALID** · S · T5 — `payload = validate_round_result(...)` L212 shadows response dict `payload = send_result.to_dict()` L321-325 in same scope. Readability only |
| 328 | fix | `mcp_proxy/tools/agent_messaging.py → sessions/mailbox.py` | — | **VALID** · M · T5 — L276 `await mailbox._wake(...)`; no public `wake` exists. MailboxService.send wraps _wake in gather(return_exceptions=True) + _normalize_wake_result L180-192, so tool layer bypasses normalization and a wake exception propagates after the message was sent and round result persisted |
| 329 | fix | `mcp_proxy/tools/plans/review_evidence.py:get_plan_review_snapshot` | — | **VALID** · S · T5 — L249-259 catch only ReviewEvidenceError; snapshot_page → _snapshot_document → review_manifest_service.snapshot_document L313-317 does TemporaryDirectory/write_bytes (OSError) and parse_plan (ValueError/UnicodeDecodeError). `_error_payload` L578-581 already handles generic exceptions |
| 330 | fix | `review_evidence.py:PlanReviewEvidenceService.__init__` | — | **VALID** · S · T1 — Function-local LocalTaskManager import L56 (used L61) and validate_convergence_telemetry L592; no cycle comment |
| 331 | no-fix | `review_evidence.py:_assemble_requirements_bundle` | — | **INVALID** — `get_task` (storage/tasks/_read.py:20-53) RAISES ValueError, never returns None → None guard is dead code. Real gap: unstructured ValueError |
| 332 | fix | `review_evidence_io.py L493-510` | — | **VALID** · S · T1 — Five record_type branches, no trailing `else: raise ReviewEvidenceError("invalid_snapshot_record")` |
| 333 | fix | `review_evidence_preparation.py:prepare_review_round_context` | — | **VALID** · S · T1 — Plain `RuntimeError` at L122 and L150 instead of ReviewEvidenceError |
| 334 | fix | `review_evidence_preparation.py:_index_repository L161-176` | bounded async DB ops (#19006) | **VALID** · L · T1 — `subprocess.run([...], timeout=120)` L164 sync on caller thread; chain from review_evidence.py:163/220 is sync → MCP thread blocks up to 120s |
| 335 | fix | `review_evidence_preparation.py` | — | **VALID** · S · T1 — L131 bare `next(...)` → StopIteration with no context |
| 336 | fix | `review_requirements.py constraints parser L127-158` | D2 sibling-marker rule | **VALID** · M · T1 — `stripped` computed L128 but L142 tests raw `line.startswith(...)` → indented/list markers silently skipped |
| 337 | fix | `review_terminal.py:terminalize_plan_review_run` | — | **VALID** · S · T1 — Duplicate `if evidence.task_id is not None:` guards L197 and L204 |
| 338 | fix | `review_terminal.py:_commit_staged_verdict` | — | **VALID** · M · T1 — L339-350 require findings+coverage_attestation before verdict check; non-attested inconclusive/needs_requirements raise invalid_staged_round_result before L243 handling |
| 339 | no-fix | `review_verdict_effects.py:apply_staged_verdict_effects` | — | **INVALID** — `get_task` raises ValueError, never returns None (storage/tasks/_read.py:46-49) → proposed None guard is a no-op. Real gap: unhandled ValueError |
| 340 | fix | `servers/routes/memory.py:entity_graph L347-362` | — | **VALID** · S · T7 — `Query(500, ge=0)` / `Query(2000, ge=0)` with no `le=` ceiling, and 0 documented as "no limit" → caller can request an unbounded FalkorDB query |
| 341 | fix | `servers/routes/providers.py:_local_generation_provider_entries` | — | **VALID** · M · T7 — L422 derives codex_installed purely from binary path presence while `_provider_health` L252-271 (already called L378/407) returns real health.available + startup_error; L223 `elif not codex_installed` marks LM Studio/Ollama available with an unhealthy Codex runtime |
| 342 | fix | `servers/routes/skills.py:write_skill_file L727-741` | bounded async DB ops (#19006) | **VALID** · S · T7 — `update_skill_file` called synchronously in an async handler; run_in_threadpool imported L18 and used by siblings at 183/322/619/636 |
| 343 | fix | `sessions/summary_context.py:_build_summary_prompt_context` | bounded async DB ops (#19006) | **VALID** · S · T5 — L188-197 call get_file_changes + get_git_diff_summary synchronously in an async def; both subprocess.run with timeout=5 (workflows/git_utils.py:96-111, :129) → up to ~20s loop blocking. Sibling calls already use `await run_db_fn(...)` |
| 344 | fix | `sessions/summary_context.py:_scoped_git_status` | — | **VALID** · M · T5 — L33 `line.endswith(path)` against raw `session_paths` L173 (may be absolute) while git_status was built from repo-relative paths via workspace_context._session_git_paths L54/102-114 → absolute entries never match; reconstructed `f"{prefix}{path}"` L35 discards rename/quoted forms. Same un-normali… |
| 345 | fix | `tests/agents/test_adversary_timeout.py` | D2 sibling-marker rule | **VALID** · S · T9 — No marker; takes temp_db L14, drives LocalAgentRunManager |
| 346 | fix | `tests/agents/test_adversary_timeout.py L10` | — | **VALID** · M · T9 — Cross-module import `from tests.agents.test_terminal_paths import _bound_review`; shared helper modules exist and are used by tests/plans/test_review_coverage.py:25-26 |
| 347 | fix | `tests/agents/test_terminal_paths.py (module)` | D2 sibling-marker rule | **VALID** · S · T9 — No pytestmark; temp_db used at L58/175/222/225… |
| 348 | fix | `tests/agents/test_terminal_paths.py:test_all_terminalizing_call_sites_route_through_helper` | — | **VALID** · M · T9 — L562 substring assert over raw read_text() of 9 modules — a comment satisfies it |
| 349 | no-fix | `tests/agents/test_terminal_paths.py:test_deferred_health_check_respects_evidence_bind L828` | asyncio_mode="auto" (pyproject:176) | **INVALID** — asyncio_mode="auto" → already awaited; stated defect false |
| 350 | fix | `tests/plans/test_convergence_regression.py:test_compaction_isolation_under_concurrent_editors` | — | **VALID** · M · T9 — L214-271 only two relative paths (L25-26); no absolute-path or suffix-colliding vendor/<own_path> case |
| 351 | fix | `tests/plans/test_repair_gate_e2e.py` | — | **VALID** · M · T9 — `_SpawnProbe` L32-36 test-local, `.spawn()` only at L275 after prepare_plan_review_round → assert L294 tautological once pytest.raises L285 fires |
| 352 | fix | `tests/plans/test_review_coverage.py:test_citation_union_repository_and_requirement` | — | **VALID** · M · T9 — Both source_digest values only truthiness-checked (L352, L363), never compared; no lane carries both citation forms |
| 353 | no-fix | `tests/plans/test_review_evidence.py` | D2 sibling-marker rule | **STALE** — Module already has `pytestmark = pytest.mark.integration` at L47 |
| 354 | fix | `tests/plans/test_review_evidence.py ~L960-975` | — | **VALID** · S · T9 — Asserts `len(source.splitlines()) < 1_000` L971 plus four call-count checks L972-975 |
| 355 | fix | `tests/plans/test_review_evidence.py` | — | **VALID** · S · T9 — `raising=False` on monkeypatch.setattr at L1119 and L1264 |
| 356 | fix | `tests/plans/test_review_findings.py:test_failure_trace_accepts_bound_requirement_citation` | — | **VALID** · S · T9 — L77-107 accepting path only; no tampered-hash negative case |
| 357 | fix | `tests/plans/test_review_repair.py fixtures` | — | **VALID** · S · T9 — `"a" * 64` duplicated L237, L289, L307, L1015 |
| 358 | fix | `tests/plans/test_review_requirements.py (module)` | D2 sibling-marker rule | **VALID** · S · T9 — No pytestmark; DB-backed tests take temp_db L163, L293 |
| 359 | fix | `tests/plans/test_review_requirements.py:test_reviewer_contracts_consume_bundle_ids` | — | **VALID** · S · T9 — L226 asserts exact wrapped YAML text |
| 360 | fix | `tests/plans/test_review_telemetry.py L167` | D2 sibling-marker rule | **VALID** · S · T9 — No pytestmark; temp_db L168, real projects/sessions/runs L203-267; same at L374+ |
| 361 | fix | `tests/plans/test_review_telemetry.py L35-42` | — | **VALID** · M · T9 — Cross-module imports from tests.storage.test_stage_review_findings with `# noqa: F401`; same leak in tests/agents/test_terminal_paths.py:41 and tests/review_learning/test_round_diff.py:29 |
| 362 | fix | `tests/review_telemetry_helpers.py:delivered_telemetry L10-64` | — | **VALID** · S · T11 — Single classification dict shared by reviewer_miss and repeated_check_keys; `**provenance` spread into three sites sharing nested lists by reference. No copy/deepcopy import |
| 363 | no-fix | `tests/servers/routes/test_providers.py L537` | D2 sibling-marker rule | **INVALID** — Module already has `pytestmark = pytest.mark.unit` L20 — per-test marker is a no-op |
| 364 | fix | `tests/workflows/test_observer_plan_mode.py L150-178` | — | **VALID** · M · T10 — Two parameter-independent blocks (persisted-anchor reuse L150-166, missing_request_anchor raises L168-178) re-executed for every parameter set |
| 365 | fix | `web/src/components/activity/FilesTab.tsx:saveFileContent` | — | **VALID** · S · T3 — L118-131 await fetch with no try/catch; only !response.ok handled |
| 366 | fix | `web/src/components/activity/IntegrationsTab.tsx` | — | **VALID** · M · T3 — L178-201 independent toggles; search row L208 and filter panel L218 can both render |
| 367 | fix | `web/src/components/activity/RulesTab.tsx` | — | **VALID** · M · T3 — L211-240 same independent toggles; finding's premise that IntegrationsTab already does this is WRONG |
| 368 | fix | `web/src/components/activity/SessionsTab.tsx:closeSearch` | — | **VALID** · S · T3 — L226-230 clears only searchInput; filter state set by 250ms debounce L172-175 |
| 369 | fix | `web/src/components/activity/__tests__/CronTab.test.tsx` | — | **VALID** · S · T3 — L220-231 only asserts getByTitle textContent; 'Wiki prune' primary header never asserted |
| 370 | fix | `web/src/components/activity/integrations/IntegrationsTabToolbar.tsx` | — | **VALID** · S · T3 — Exports only IntegrationsFilterPanel (L15); filename mismatched |
| 371 | fix | `web/src/components/activity/memory/MemoryTabData.ts:extractGraphLimits` | — | **VALID** · S · T3 — L185-189 Number(null)/Number("") = 0 = unlimited sentinel in sanitizeGraphLimit (KnowledgeGraphModel.ts:268) |
| 372 | no-fix | `web/.../KnowledgeGraph.falkordb.test.tsx` | — | **INVALID** — Fixture value doubles as memory_preview; L145 asserts it IS present — asserting absence breaks the test |
| 373 | fix | `web/src/components/activity/rules/__tests__/RulesTab.test.tsx` | — | **VALID** · S · T3 — L414 asserts readonly, L415 clicks Edit, L417 fireEvent.change with no readonly-cleared assertion |
| 374 | fix | `web/src/components/activity/skills/SkillContentView.tsx:selectPath` | — | **VALID** · M · T3 — L120 window.confirm; SkillsTab.tsx:80 uses useConfirmDialog; style guide L350 mandates ConfirmDialog |
| 375 | fix | `web/src/components/activity/skills/SkillsTabActions.ts:restoreSkill` | — | **VALID** · M · T3 — L144-150 returns parseSkillResponse which returns null on !ok; saveSkillFile L134 throws responseError |
| 376 | fix | `web/src/components/activity/skills/SkillsTabData.ts:loadSkillFiles` | — | **VALID** · S · T3 — L170-178, L185-193 discard server detail; responseError (SkillsTabActions.ts:34) is module-private |
| 377 | fix | `web/src/components/activity/skills/__tests__/SkillsHub.test.tsx` | — | **VALID** · S · T3 — L21-31 byte-identical to SkillsTab.test.tsx L23-33; web/src/test/helpers.tsx already exists |
| 378 | no-fix | `web/src/components/activity/terminal/TerminalDock.tsx` | test-types ratchet (#18781/#18783) | **INVALID** — No return-type lint rule in web/eslint.config.js; prevailing style omits component return types |
| 379 | fix | `web/src/components/activity/useActivityPanel.ts:openTerminal` | — | **VALID** · M · T3 — L187-190 setMobileView('chat') outside dirtyGuard.guardedRun unlike L161-227 siblings |
| 380 | no-fix | `web/src/components/chat/styles/rules-tab.css` | — | **INVALID** — Panel is direct child of full-height tab root; calc(100% + 0.35rem) would push it below the tab. top: 0.25rem is the #19159 anchor |
| 381 | fix | `web/src/components/shared/editableContent.ts:saveEdit` | — | **VALID** · S · T3 — L66-75 try/finally with no catch; rejected onSave escapes to `void saveEdit()` call sites |
| 382 | fix | `web/src/hooks/useMemory.ts:fetchKnowledgeGraph` | — | **VALID** · S · T3 — L387 hardcodes 500/2000 duplicating DEFAULT_GRAPH_LIMITS (KnowledgeGraphModel.ts:263) |

## Verification

Per leaf task, before close:

1. Focused tests for the touched modules (table above) — never the full suite.
   Prefix agent runs with `GOBBY_TEST_PROTECT=1`.
2. `uv run ruff format --check src/ tests/` and `uv run ruff check src/ tests/`.
3. `uv run mypy src/` for any task touching `src/`.
4. `uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
   for T9–T12 (the annotation fixes should *reduce* the baseline; regenerate it
   with `--write-baseline` only after the reduction is real).
5. T3 only: `npm test`, `tsc --noEmit`, eslint.
6. T12 only: `cargo test -p gobby-code -p gobby-wiki`, then **rebuild and
   reinstall** `~/.gobby/bin/gcode` — the `security.rs` hidden-secret-file fix is
   not live until the binary is reinstalled.
7. T8 only: `uv run gobby plans validate <file>` for each of the four plans, then
   `expire_plan_review_evidence` for `herdr-terminal-client`,
   `wiki-codewiki-restructure`, and `split-workflow-definition-storage`.

End-to-end confirmation of the three highest-value behavioural fixes:

- **Sandbox scope (146)** — write a `.mcp.json` with a non-gobby server whose
  args contain an absolute directory, spawn a sandboxed agent, and assert that
  directory is absent from the resolved read roots.
- **Pane-output redaction (160)** — force a deferred tmux health-check failure
  and assert `agent_runs.error` holds a bounded, redacted tail rather than the
  raw 4096-char pane dump.
- **Rate-limit detection (45)** — feed the coordinator an exception reading
  `Issue #429 not found` and assert the project does **not** enter
  `rate_limited`.

After every finding is fixed or documented `no-fix`, delete
`.gobby/plans/coderabbit.md` and record review lessons via
`gobby-review-learning.record_review_lesson` for the reusable classes:
the psycopg `%s` contract, the `asyncio_mode="auto"` marker class, and the
`no-fix-policy` for approved-plan prose edits.

