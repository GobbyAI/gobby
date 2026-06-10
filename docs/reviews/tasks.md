# Review: tasks

- **Scope:** `src/gobby/tasks/` — expansion (`expansion/` package: `_contract`, `_compile`,
  `_common`, `_reset`, `_apply`, `_qa`, `_validate`, `_plan_gate`; `expansion_service.py`,
  `expansion_qa_coverage.py`), validation (`validation.py`, `validation_history.py`,
  `validation_models.py`, `state_semantics.py`, `lifecycle_repair.py`), commits/tree/models
  (`commits.py`, `tree_builder.py`, `isolation.py`, `task_types.py`, `categories.py`), and the
  LLM `prompts/`. Cross-seam reads into `llm/`, the plan-coverage contract, the close-gate
  consumers (`mcp_proxy/tools/tasks/_lifecycle_close.py`, `workflows/observer_commits.py`),
  storage tasks, dispatch, and tests.
- **Reviewer:** Claude Fable 5 — 3-agent parallel fan-out, all Blockers synthesizer-verified.
- **Commit / branch:** `0.5.0` @ HEAD `dc298276f` (working tree clean at review time).
- **Summary:** 1 Blocker · 16 Important · 6 Nit — the apply path is genuinely atomic and the
  coverage/plan-hash gates can't be silently bypassed (strong), but the commit-before-close
  gate is satisfied by an `auto_link_commits` call that links zero commits, the LLM validation
  gate is opt-out by an unenforced free-text field, and there are two divergent validation
  implementations (the tested one is the weaker, orphaned one). Recurring theme: gates keyed on
  session-global flags or optional data, not task-scoped truth.

## Findings

### [BLOCKER] `auto_link_commits` flips the session-global `task_has_commits` flag even when zero commits are linked — commit-before-close gate bypass
- **Where:** `workflows/observer_commits.py:96` (verified: `detect_commit_link` accepts `auto_link_commits` in its tool set), `:109-113` (sets `task_has_commits = True` whenever the result has no `error` key — regardless of `total_linked`); the MCP `auto_link_commits` always returns a non-error dict `{linked_tasks, total_linked, skipped}` even when `total_linked == 0` (`mcp_proxy/tools/task_sync.py:252-256`). Consumers: `require-commit-before-status.yaml:36`, `completion-readiness.yaml:12`, `require-memory-review-before-status.yaml:12`, `strip-skip-validation-with-commit.yaml:11` all key off `task_has_commits`.
- **Failure mode:** An agent (or any auto-link trigger) calls `auto_link_commits` once; it links zero commits but returns success → the per-session `task_has_commits` boolean goes true → the "must have a commit before closing" gate is satisfied for a task that has no commit. The flag is session-scoped, not task-scoped, so it doesn't even verify the linked commit belongs to the task being closed — any commit anywhere in the session unlocks the gate for every task touched in that session.
- **Why it matters:** The commit-before-close gate is the contract that a task can't be marked done without committing its diff; it's defeated by a no-op call.
- **Minimal fix:** In `detect_commit_link`, require `result.total_linked > 0` for `auto_link_commits`, and gate on the *specific* closing task's id vs the linked task ids rather than a session-global boolean. At minimum, drop `auto_link_commits` from the trusted set.
- **Confidence:** high (verified).

### [IMPORTANT] LLM validation gate is silently skipped whenever `task.validation_criteria` is empty
- **Where:** `mcp_proxy/tools/tasks/_lifecycle_close.py:270` (`if is_leaf and ctx.task_validator and task.validation_criteria:`); criteria is required only at `_crud.py:115` for `category=="code"`; expansion sets it from an optional manifest field (`expansion/_apply.py:201`, may be None); storage `create_task` doesn't enforce it.
- **Failure mode:** A leaf task reaching close with falsy `validation_criteria` bypasses the entire LLM validation block. Expansion-created code tasks (manifest omits `validation`), dispatch/build-created tasks, non-`code` categories, and storage-direct creations can all close with zero LLM validation — the commit/edit checks still run, so broken code can be committed and the task closed without the validator ever evaluating the diff. The TaskValidator-side analogue of the upstream close-gate poisoning.
- **Minimal fix:** Treat missing criteria on a leaf implementation task as a hard gate failure (validate against the description, as `validate_task` already does for the no-criteria case at `validation.py:659-676`), or enforce criteria at the storage `create_task` boundary.

### [IMPORTANT] Two divergent validation implementations — the tested one is the weaker, orphaned path
- **Where:** `mcp_proxy/tools/task_validation.py:65-234` (`create_validation_registry`/`validate_task`) is exported and exercised by three test modules but **not merged into any live registry** (`registries.py` wires only task + task-ops; verified). The live gate is `_lifecycle_validation.py`.
- **Failure mode:** If ever wired, the orphaned `validate_task` is materially weaker: on `valid` it calls `close_task` directly (`:194`), bypassing the commit-requirement check, the `session_had_edits` check, the failure-pattern override, and doc-only handling — trusting the raw LLM verdict; its `pending` branch silently no-ops. Tests assert this path "works," masking both its absence and its weakness. A latent gate-bypass waiting for a re-wire.
- **Minimal fix:** Remove `create_validation_registry`/`validate_task` and its tests, or route it through the live gate helpers before any wiring.

### [IMPORTANT] Validation history is never recorded on the live close path
- **Where:** `validation_history.py:63` (`record_iteration`) — only caller is the orphaned tool (`task_validation.py:174`); the live gate (`validate_leaf_task_with_llm`) and `close_task` never construct a `ValidationHistoryManager` or record an iteration.
- **Failure mode:** `task_validation_history` is effectively never populated in production, so every history-derived feature (`get_iteration_history`, `has_recurring_issues`, recurring-issue escalation) operates on an empty table; the `validation_fail_count` retry/escalation flow is never reached on the live path (which blocks-but-doesn't-escalate on repeated `invalid`). Advertised features silently no-op.
- **Minimal fix:** Record an iteration from `validate_leaf_task_with_llm` and wire `ValidationHistoryManager` into the live path; or delete the subsystem if retired.

### [IMPORTANT] Smart-context validation feeds unrelated commits to the judge when a task has no linked commits
- **Where:** `validation.py:468-483` (`get_validation_context_smart` Strategy 2 pulls the last `DEFAULT_COMMIT_WINDOW` commits unconditionally), `:538-576` (diff falls back to the last commit); reached from `_lifecycle_validation.py:343-350` when no task-linked diff and no `changes_summary`.
- **Failure mode:** For a leaf task with criteria but no linked commits and unknown `session_had_edits`, the validator's "changes" context is built from the repo's last 10 commits regardless of task/session — the judge evaluates the wrong diff and can return `valid` for off-task changes. Stale/wrong-file evidence accepted by the gate (bounded by the commit-requirement check, but the no-commit/no-edit window is reachable).
- **Minimal fix:** Label fallback context as unattributed and refuse to auto-`valid` on it, or restrict the window to the claim-session window (already computed in `_lifecycle_close.py`).

### [IMPORTANT] `repair-lifecycle --force --task` reseeds/clears the stage manifest of a closed/escalated task with no terminal guard
- **Where:** `lifecycle_repair.py:218,297` (`skipped` flips False under `force and task_scoped`), `_apply_candidate:329-338` (force path `DELETE FROM task_stage_states` then `initialize_manifest`); the reseed candidates never check `is_task_closed`/`is_escalated`/`claimed` (only the *remove* path does, via `_is_metadata_only`).
- **Failure mode:** `--force --task <closed-or-escalated-task>` deletes the manifest history and re-initializes a fresh `ready` manifest. The task doesn't re-open (close reads `closed_at`), but stage history is destroyed and `current_stage`/`projected_task_state` now report an active stage on a closed task — an inconsistent projection that can confuse dispatch/observers. Last-writer-wins on stage state behind an operator flag (same unguarded-state family as pipelines/build/sessions).
- **Minimal fix:** Refuse reseed when `is_task_closed`/`is_escalated` even under `--force`; reuse `_is_metadata_only`'s terminal checks for the reseed path.

### [IMPORTANT] Path traversal: attacker-influenced task text makes expansion read an arbitrary file into the LLM prompt
- **Where:** `expansion/_compile.py:443-475` (`_build_file_context`) — paths from `extract_mentioned_files(task_payload)` over title/description/validation_criteria; normalized only with `lstrip("./")` (`:455`) then `repo_path / normalized` (`:458`), read up to 3500 chars (`_common.py:349`) with **no containment check**.
- **Failure mode:** A task description containing `` `src/../../../../etc/passwd` `` matches the path regex; `lstrip("./")` leaves embedded `..`; the join resolves outside the repo and up to 3500 chars are injected into the expansion prompt — arbitrary file read into LLM context. (Doesn't reach a structured control field, so not a dispatch hijack.)
- **Minimal fix:** `resolved = (repo_path / normalized).resolve(); if not resolved.is_relative_to(repo_path.resolve()): continue`; reject candidates containing `..` segments.

### [IMPORTANT] `validate_compiled_spec` omits `task_type`/`implementation_domain` — malformed LLM output crashes inside the apply transaction instead of being rejected at the gate
- **Where:** `expansion/_validate.py:67-155` (validates category/uniqueness/phase-refs/cycles, never `task_type` or `implementation_domain`); these flow through `_normalize_native_compiled_spec` (`_compile.py:290-404`) into `_create_task_in_transaction` (`storage/tasks/_creation.py:108-110`), where `validate_task_type`/`validate_implementation_domain` *raise* `ValueError`.
- **Failure mode:** A garbage `task_type: "garbage"` passes `validate_compiled_spec`, is persisted as `compiled_spec`, then fails at apply time with an uncaught `ValueError` inside `transaction_immediate` (atomic rollback — no partial tree, but a late crash/500 rather than a clean `{valid: False}` rejection; the saved spec retries the same crash).
- **Minimal fix:** Validate `task_type` (against `VALID_TASK_TYPES`) and `implementation_domain` in `validate_compiled_spec`.

### [IMPORTANT] Expansion `apply_run` idempotency guard is a TOCTOU — duplicate task trees on concurrent triggers
- **Where:** `expansion/_apply.py:77-286` — `find_apply_blocking_expansion_output(task.id)` (`:100-105`) runs *before* `transaction_immediate` (`:113`); the MCP entry (`mcp_proxy/tools/tasks/_expansion.py:203,224`) acquires no dispatch mutex, and the transaction locks `TaskSeqAllocation`, not the parent's expansion state.
- **Failure mode:** Two triggers reaching apply for the same parent in the window (MCP retries, or MCP/agent racing a dispatcher-spawned pipeline) both pass the pre-transaction check, both run, each creating a full duplicate subtask tree. No unique constraint backs "one applied expansion per parent."
- **Minimal fix:** Move the blocking check inside the transaction and take a parent-scoped lock (extend the lock target or acquire the task dispatch mutex on the MCP path).

### [IMPORTANT] Expansion QA module (`_qa.py`) is dead — `check_routing`/`check_manifest_coverage`/`check_shape` enforce nothing
- **Where:** `expansion/_qa.py` (whole module) — `run_expansion_qa` and its sub-checks have **no callers in `src/gobby`** (only the test file imports them). `check_routing` is the only spec-layer validator of `assigned_agent` against known agents; `check_manifest_coverage` the only one checking every manifest section maps to a leaf — neither runs in `compile_run`/`apply_run`.
- **Failure mode:** The module reads as a defensive QA gate but protects nothing; real coverage enforcement lives elsewhere and real routing safety lives at the dispatch boundary. False assurance, with a 1:1 test suite that passes while enforcing nothing. (Wiring it in would also close the `task_type` gap above.)
- **Minimal fix:** Wire `run_expansion_qa` into `compile_run` after `validate_compiled_spec` (fail on `not valid`), or delete the module + tests.

### [IMPORTANT] Session-end auto-link is a silent no-op — `project_id`/`project_name` not threaded, so every `#N` ref fails to resolve
- **Where:** `hooks/event_handlers/_session_end.py:57-61` (`auto_link_commits(..., since=session.created_at, cwd=cwd)` with no `project_id`/`project_name` despite `session.project_id` being available); `extract_task_ids_from_message` only returns `#N` form (`commits.py:582`); `auto_link_commits` resolves `#N` only when `project_id` is truthy (`commits.py:748-757`), else `get_task("#42")` raises "requires project_id for seq_num lookup".
- **Failure mode:** The primary automatic commit→task linkage path is dead for the normal `[project-#N]` convention — tasks don't get commits attached at session end (feeding `get_task_diff`, `update_observed_files`, conflict detection, review evidence). `project_name` also falls back to the daemon's current project, not the session's.
- **Minimal fix:** Pass `project_id=session.project_id` (and the session's `project_name`).

### [IMPORTANT] `is_doc_only_diff` misclassifies a rename of a doc file into a code file as doc-only, skipping LLM validation
- **Where:** `commits.py:169` (`file_pattern = r"^diff --git a/(.+?) b/"` checks the `suffix` of the **old (a/)** path only); consumer `_lifecycle_validation.py:381-382` skips LLM validation when `is_doc_only_diff` is true.
- **Failure mode:** A rename `notes.md` → `code.py` (adding code) has header `diff --git a/notes.md b/code.py`; the function inspects only `notes.md` (`.md` → doc) and judges the whole change doc-only → LLM validation skipped. (Quoted paths `"a/x.py"` don't match the regex at all — fails safe but breaks doc-only detection for paths with spaces.)
- **Minimal fix:** Parse both old and new paths (handle `rename to`/quoting); classify doc-only only if every added/renamed-to path is a doc extension.

### [IMPORTANT] Hyphenated/spaced project names can never auto-link — `(\w+)` in TASK_ID_PATTERNS rejects them
- **Where:** `commits.py:527-534` (all three `TASK_ID_PATTERNS` use `(\w+)` for the project segment); the canonical ref is `[<project_name>-#<n>]` and project names allow hyphens/spaces.
- **Failure mode:** For a project named `gobby-pro`, `[gobby-pro-#7]` matches none of the patterns — a whole class of repos gets zero commit linkage with no error. Compounds the session-end no-op.
- **Minimal fix:** Broaden the project capture to allow hyphens/spaces, or match against a sanitized project slug.

### [IMPORTANT] `tree_builder` `DependencyCycleError` is uncaught and recursion is unbounded
- **Where:** `tree_builder.py:275-282` (`_wire_dependencies` wraps `add_dependency` in `except ValueError`, but `add_dependency` raises `DependencyCycleError`, which is NOT a `ValueError` subclass — `storage/task_dependencies.py:43,61-64`); `:208-210,280-281` (recursive `_create_node`/`process_node` with no depth cap).
- **Failure mode:** A cyclic `depends_on` edge in LLM/imported tree JSON raises `DependencyCycleError` out of `build`, aborting the whole tree after tasks were already created (bypassing the per-edge error collection); a depth-~1000 tree hits `RecursionError` mid-construction.
- **Minimal fix:** Catch `(ValueError, DependencyCycleError)` and continue; add an explicit depth/node-count cap.

### [IMPORTANT] `get_task_diff` produces an unbounded combined diff; `current_stage` trusts a serialized projection over live stages
- **Where:** `commits.py:117-137` (loops `git show` per linked commit, joins with no size cap — the MCP `get_task_diff` tool returns `result.diff` directly, uncapped; downstream validation caps at 30000 but the raw result doesn't); `state_semantics.py:39-43` (`current_stage` returns `state["current_stage"]` if present, never consulting the live `stages` array — a stale serialized projection shadows authoritative stage rows).
- **Minimal fix:** Single `git diff <base>..<head>` with a size cap/truncation marker; prefer live `stages` when both are present (or document the precedence).

### [NIT] Expansion/repair atomicity and dependency edge cases
- **Where:** `expansion/_reset.py:62-104` (`reset_expansion_output` deletes in a non-transactional loop — a mid-loop failure partially deletes the tree; bottom-up and re-runnable, so recoverable); `expansion/_apply.py:326-331` (`_add_dependency` catches only `ValueError`, not `DependencyCycleError` — currently unreachable since cycles are pre-validated, but the swallow intent mismatches the caught type); `tree_builder.py:200-205,240-248` (duplicate task titles collapse `_title_to_id`, silently mis-wiring title-based `depends_on` to the last-created task — warning only).

### [NIT] Validation-history schema and ordering
- **Where:** `storage/postgres_baseline_schema.sql:581-592` (`task_validation_history` has a surrogate PK only, no uniqueness on `(task_id, iteration)`) + `task_validation.py:164-165` (`iteration = current_fail_count + 1`, incremented only on `invalid`) → a `valid` and a later attempt can share an iteration number, making `get_latest_iteration`'s `ORDER BY iteration DESC LIMIT 1` (`validation_history.py:138-143`) tie-break arbitrarily. Latent (table unpopulated in production). Fix: order by `created_at DESC, id DESC`.

## Systemic patterns

1. **Gates keyed on session-global flags or optional data, not task-scoped truth.** `task_has_commits` is a per-session boolean any of three tools can set (the Blocker), never checked against the closing task. The LLM validation gate hinges on a free-text `validation_criteria` enforced at one boundary for one category. Both are "absence/optional treated as permission," echoing the upstream "absence-of-signal = success" findings.
2. **Two divergent validation implementations** — the orphaned `task_validation.py` (raw verdict, direct close, no commit/failure-pattern checks, *with* tests and history recording) vs the live `_lifecycle_validation.py` (failure-pattern override, commit checks, *no* history recording). The weaker one is the tested one. Unify them.
3. **Two expansion compile paths, two trust models** — the contract path derives every structured field from a parser-validated manifest; the native path takes fields from raw LLM JSON, defending `assigned_agent`/`category` but leaving `task_type`/`implementation_domain`/`validation` to fail late or pass through. Defense is concentrated at the dispatch boundary, not at apply.
4. **Regex diff/ref parsing repeatedly ignores git quoting and rename semantics** — `is_doc_only_diff`, `extract_task_ids_from_message` (hyphen/space project names), `_count_unique_diff_paths` all hand-roll parsing and miss quoted paths and old-vs-new rename distinction (same family the sessions/build reviews flagged).
5. **Optional project scoping that silently degrades** — `auto_link_commits`/`resolve_task_reference`/`get_task` treat `project_id` as optional and no-op when absent; the session-end caller drops it, so the failure is invisible.
6. **Terminal-state guards are inconsistent** — `_is_metadata_only` guards the manifest-removal path against closed/escalated/claimed tasks, but the manifest-reseed paths under `--force` have no equivalent guard.

## Verified non-bugs (cleared — don't re-chase)

- **Expansion apply is genuinely atomic** — all task creation, manifest inserts, dependency edges, affected-files, artifact writes, and `allow_automation` updates run inside one `transaction_immediate` with all sub-managers sharing the adapter; any mid-apply raise rolls back the whole tree (no orphan/partial tree).
- **The LLM does not control enforcement-gating fields** — `allow_automation` is inherited from parent build state, never read from the spec; `assigned_agent` from the LLM is only accepted if it's in the enabled registry, else falls back to heuristics + a default, and dispatch re-validates via `_agent_dispatchable`; cycle detection runs in both `compile_run` and `apply_run`.
- **No data loss of completed/in-progress subtasks on reset** — `_validate_reset_targets` refuses to delete claimed/committed/closed/isolated/progressed-stage tasks.
- **Coverage and plan-hash gates can't be silently bypassed** — `_validate_contract_manifest` raises on any deliverable section lacking a manifest entry; `compile_run` raises on `deliverable_count == 0`; `run_expansion_qa_coverage` requires `actual_plan_hash == plan_hash == expected_hash`, so a plan tampered after hash recording can't claim coverage.
- **Commit linking is project-scoped on the `task_id` path** — `_resolve_task_filter` resolves `#N` under the task's project and gates accepted refs to `{task_id, task.id, #seq, seq}`; a wrong `project_name` causes under-linking, not cross-project linking. `normalize_commit_sha` verifies the object is a real commit (`git cat-file -t`) before linking; word-boundary anchors prevent substring/embedded-number false matches.
- **cwd discipline is mostly correct in commits.py** (unlike build/sessions) — `get_task_diff` (MCP/CLI pass `cwd=repo_path`), `auto_link_commits`, and `update_observed_files` all use the right repo; the only loose end is `get_task_diff`'s unused `Path.cwd()` default.
- **`pending`/LLM-error fails closed on the live path** (`validation_status != "valid"` → can't close); the failure-pattern override correctly makes failure evidence win over a `valid` verdict; `skip_validation` is **enforced** (requires override justification + current-session evidence), not silently stripped (CLAUDE.md is stale here).
- **The small models validate against fixed enums and raise on unknown values** (`isolation`/`task_types`/`categories`); `_resolve_branch_for_task` and `_dedupe_commits` have cycle protection / correct quoting.
- **`%s` placeholders are correct** per repo convention.
