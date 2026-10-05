# Working context for #22644 (Replace edit attribution with four content-based ownership rules)

Working notes for Plan Writer 3 (gobby#15468). The plan file `.gobby/plans/content-ownership-rules.md`
has not been written yet. All facts below were verified on 0.5.0 on 2026-10-05.

## Rulings (Orchestrator gobby#14972, 13:03 CT; record them in the Decision Record)

- **R1, rule 2 scope.** Rule 2 blocks a commit only when a committed path is dirty and sits in another
  live session's ownership ledger. Paths that no live session mutated commit freely. Merge commits are
  exempt only for the paths the merge brings in (`git diff --name-only HEAD MERGE_HEAD`); every other
  staged path in a merge commit still follows rule 2. Record this as a Decision that names Josh's verbatim
  rule 2 as the rule it interprets, so Josh rules on it at approval. #23363 land 7 (Evidence stall fix)
  passes under this rule.
- **R2, transfer at claim.** At claim, the task's dirty paths move from ended or unclaimed predecessor
  sessions linked to the task into the claimant's ledger. Ownership never duplicates across two live
  sessions.
- **R3, retirements.** Retire `_claim_scope_conflicts`, `inspect_task_path_ownership` and
  `release_task_paths`. Every rule 2 and rule 3 refusal names the owning session ref and task ref.
- **Writer decisions the Orchestrator accepted:**
  - Single-machine scope. Josh parked #23277 (Hook envelope carries edited-file content) until the Rust
    multi-machine port. That plan's single-machine leaves became Lane 6 #23537 (Monolith guard ignores
    MultiEdit edits) and #23538 (ghook inbox owner-only). This plan has no foreign-origin sections and
    no prerequisite on #23277; node-side ownership belongs to the Rust port.
  - Mutation detection (the "pre-touch blob" option in Josh's definition). Rule 3's first-touch check
    proves the path clean, so its pre-touch content is the HEAD blob. After the call, a pathspec
    `git status` on that path decides whether it was mutated: dirty means mutated, which records it;
    clean records nothing. Stored pre/post sha256 was rejected because call ids are adapter-specific
    (`tool_use_id`, `call_id` and `toolCallId` are each read only inside their own adapter), and a pre
    hash would need a store that survives restarts.
  - Write-kind calls only. Both the recorder and rule 3 apply to `canonical_tool_kind == "write"` with
    `canonical_repo_mutation`.
    - Index-only git commands are neither checked nor recorded: `git add` is already `"execute"`.
      Reclassify `git restore --staged` without `--worktree`/`-W` as `"execute"`; today it is `"write"`
      in `_normalization_canonical.py::_classify_shell_segment_without_redirection`, around lines
      657-682.
    - The #22642 regression is `git restore --staged tests/e2e/test_terminal_client_stack.py` (the
      path flipped from staged to unstaged). It must record nothing.
    - A write-kind call on a clean path that leaves its bytes unchanged must also record nothing.
  - One ledger, with legacy branches deleted under AGENTS.md rule 10 (no backward compatibility), and
    the rule 4 project-id check (below).

## New ledger shape (session variables)

- `session_dirty_files: {checkout_root: [relative paths]}` is the live ownership used by rules 2 and 3.
  It replaces the flat `session_dirty_files` list and `session_dirty_file_checkouts`.
  - `reconcile_edit_ledgers` releases paths git reports clean.
  - A checkout root that no longer exists on disk holds no dirt, so its paths are released.
- `task_edited_files: {task_id: {checkout_root: [relative paths]}}` is append-only for the session's
  life; it is no longer cleared at close. Gates 9 and 12 and `target_task_has_edits` read it.
- A task's live pairs are `task_edited_files[task] ∩ session_dirty_files`. `has_target_task_dirty_files`
  and gate 9 read those.
- `session_edited_files` (flat list) is unchanged and is only displayed by
  `handoff_summary._load_session_file_paths`.
- Retired variables: `task_edited_file_times`, `task_edited_file_checkouts`, `..._history`,
  `..._history_started_at`, `session_dirty_file_checkouts`, `baseline_dirty_files`.
- Checkout identity stays because `transcript_tool_arguments.match_task_file` matches realpath and
  relpath against `(root, rel)` pairs, and because rules 1 and 3 must tell the same relative path in two
  checkouts apart.

## Rule mechanics

- **Rule 3**, `before_tool`, replacing `commit_guard.foreign_dirty_edit_conflict` and
  `block-cross-session-foreign-dirty-edit.yaml`. For each write path whose pair is not in its own
  `session_dirty_files`, run a pathspec status in that path's checkout:
  - dirty: refuse, naming each live session holding the pair (session ref and task ref), or saying that
    no live session owns it;
  - clean: allow, and drop the pair from any other live session's `session_dirty_files` (a stale
    holder, which is the reason ownership never duplicates);
  - git unavailable: refuse as unverified.
- **Post-tool recorder** (`_tool.py::_record_successful_file_mutation`):
  - a pair it already owns appends to the active task's ledger without running git;
  - otherwise a pathspec status runs: dirty records the pair in `session_dirty_files`, the task ledger
    and `session_edited_files`; clean records nothing;
  - ignored paths show clean, so they are not recorded;
  - paths outside a checkout, or in another project, are dropped as today in `_resolve_repo_edit_paths`;
  - delete `_paths_landed_before_edit`, the replay/times check.
- **Rule 2** (`foreign_staged_commit_conflict`, `block-cross-session-foreign-staged-commit.yaml`): the
  candidates (pathspec ls-files, or the staged names) that are held by another live session and are not
  in its own ledger, minus the merge-brought-in paths when MERGE_HEAD exists, then a dirty check. The
  refusal names the owner session ref and task ref.
  - Existing helper: `code_review_freshness.is_foreign_landing_merge` (registered worktree branch owned
    by another session) feeds `foreign_landing_merge` for the code-review gate only.
- **Rule 4**, new before-tool rule: the project id from `find_project_root` and `.gobby/project.json`
  differs from the session's project, so the write is refused.
  - Exception: the path's checkout root is a registered worktree whose `task_id` is one of the
    session's claimed tasks. The `Worktree` model (`storage/worktrees.py`) has `project_id` (the target
    project) and `task_id`; `create_worktree(project_path=..., task_id=...)` stores both.
  - Today nothing enforces rule 4; only attribution is dropped.
- **Rule 1** (gate 9):
  - `_lifecycle_close.py::_evaluate_close` calls `apply_task_cleanliness_gate(edited_paths =
    attribution.clean_proof_paths)`;
  - `_lifecycle_close_finalization.py::capture_attribution` uses the flat `task_edited_file_set` against
    `repo_path` only;
  - new behavior: check the owner's live task pairs per checkout root;
  - drop the `validate_uncommitted_task_edits` call to `foreign_owned_dirty_paths` (it is the
    foreign-owner annotation), and drop the message's "release_task_paths" advice;
  - `_linked_commit_clean_proof_paths` must be checked for its foreign-owner use.

## Consumers to target (rg sweep of src/gobby)

- `workflows/state_manager.py`: `record_edited_files`, `release_session_dirty_files`,
  `release_task_edited_files` (retire), `_session_dirty_file_checkouts`.
- `workflows/task_claim_state.py`: all of its ledger accessors. `remove_claimed_task` stops clearing
  the task ledger. `release_claimed_task` is unchanged.
- `workflows/ledger_reconcile.py`: `reconcile_edit_ledgers`, `_task_ledger_paths`,
  `session_dirty_file_set(_for_checkout)`.
- `workflows/hooks.py::_evaluate_rules` (lines ~480-645): the eval_context wiring for the guards,
  `has_dirty_files`, `has_target_task_dirty_files`, and a baseline comment at line 408.
- `workflows/engine/core.py` (lines 320-322): eval_context defaults.
- `workflows/commit_guard.py`, retired or rewritten:
  - `_active_path_owners`, `_active_foreign_path_owners`, `_release_clean_ledger_entries`,
    `_dirty_owned_paths_releasing_clean`, `_owned_dirty_paths`, `active_owned_dirty_paths`;
  - `foreign_owned_dirty_paths(_async)`, `inspect_checkout_path_ownership_async`;
  - the formatters.
- `workflows/code_review_scope.py::inspect_commit_review_scope`.
- `workflows/monolith_guard.py::outstanding_monolith_paths` reads the raw `task_edited_files` as a flat
  list.
- `workflows/found_work_gate.py` (4 refs).
- `mcp_proxy/tools/tasks/`:
  - `_lifecycle_paths.py`: delete the whole module;
  - `_lifecycle.py::create_lifecycle_registry`: registration;
  - `_lifecycle_claim.py::_claim_scope_conflicts`: retire, and add the R2 transfer, reusing
    `_task_attribution_sessions`;
  - `_lifecycle_validation.py::validate_uncommitted_task_edits`;
  - `_lifecycle_close_finalization.py`: `capture_attribution`, `_cleanup_closed_claim`,
    `_linked_commit_clean_proof_paths`;
  - `_close_evaluation_support.py`: `derive_close_transcript_evidence`. Delete
    `_legacy_task_checkout_proof`, `_legacy_closed_other_tasks`, `_without_closed_task_paths` and the
    history-stamp logic;
  - `_stage_review.py::submit_for_review`.
- `mcp_proxy/tools/sessions/_actions.py::capture_baseline_dirty_files_tool`: retire.
- `hooks/session_activation.py`: `SESSION_ACTIVATION_INVARIANTS`, `_baseline_updates`,
  `_missing_baseline_keys`.
- `agents/worktree_checkpoint.py::_authorized_task_paths`: delete the legacy pre-#21897 branch.
- `agents/task_recovery.py::TaskRecoveryHandler._clear_claim_session_variables`: docstring.
- `agents/run_completion.py::_agent_run_task_dirty_scope`.
- `storage/tasks/_live_session_recovery.py::recover_expired_live_session_claims`.
- `sessions/handoff_summary.py::_load_session_file_paths`.
- `tasks/transcript_evidence.py`: it passes pairs through; check `_DerivationState.task_checkout_paths`.
- `workflows/enforcement/blocking.py::TASK_MUTATION_TOOLS_BY_SERVER`: lists `release_task_paths`.
- Templates and docs:
  - `install/shared/workflows/variables/gobby-default-variables.yaml`;
  - rule YAMLs `block-cross-session-foreign-dirty-edit`, `block-cross-session-foreign-staged-commit`
    and `require-commit-before-status`;
  - skills docs `references/tasks/implementation.md` and `references/sessions/terminals.md`.

## Tests referencing retired mechanisms (count of hits)

- Workflows:
  - tests/workflows/test_commit_guard.py (35)
  - tests/workflows/test_session_variable_manager.py (18)
  - tests/workflows/test_task_claim_state.py (13)
  - tests/workflows/test_code_review_scope.py (2)
  - tests/workflows/test_task_enforcement_rules.py (2)
  - tests/workflows/test_database_deadline_retry.py (2)
  - tests/workflows/test_hook_evaluation_serialization.py (2)
  - tests/workflows/test_skill_loaded_call_tool_path.py (2)
  - tests/workflows/engine/test_unavailable_tool_input_guards.py (2)
  - tests/workflows/test_hook_evaluation_timeout.py (1)
  - tests/workflows/test_hooks.py (1)
  - tests/workflows/test_proxy_hooks.py (1)
  - tests/workflows/test_turn_interrupt_stop_gates.py (1)
- MCP proxy, task tools:
  - tests/mcp_proxy/tools/test_release_task_paths.py (29, delete)
  - tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py (12)
  - tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py (3)
  - tests/mcp_proxy/tools/tasks/test_checkout_unresolved_envelopes.py (2)
  - tests/mcp_proxy/tools/tasks/test_escalation_coordinator.py (2)
  - tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py (2)
  - tests/mcp_proxy/tools/tasks/test_close_attribution.py (1)
- MCP proxy, other tools and services:
  - tests/mcp_proxy/tools/sessions/test_capture_baseline.py (11, delete)
  - tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py (6)
  - tests/mcp_proxy/tools/test_internal_action_tools.py (5)
  - tests/mcp_proxy/tools/test_claim_task.py (3)
  - tests/mcp_proxy/tools/test_read_only_classification.py (1)
  - tests/mcp_proxy/services/test_direct_tool_session_activation.py (1)
- Hooks:
  - tests/hooks/test_session_activation_reconciliation.py (17)
  - tests/hooks/test_stop_handoff_pending.py (3)
  - tests/hooks/test_hook_manager.py (1)
  - tests/hooks/test_tool_handlers.py (1)
- Agents: tests/agents/test_terminal_timeout_checkpoint.py (4)

## Section sketch (one section is one leaf)

The first five sections are ordered: retirements first remove the consumers, then the ledger reshape,
which reimplements the accessors over the new storage.

1. Retire `release_task_paths`, `inspect_task_path_ownership` and gate 9's foreign-owner annotation,
   including the message text in `commit_guard` and gate 9.
2. Retire `capture_baseline_dirty_files` and `baseline_dirty_files`.
3. Single ownership ledger (state_manager, task_claim_state, ledger_reconcile, defaults yaml, hooks.py
   dirty views). Needs a Granularity note.
4. Recorder records only content-changing mutations, plus the `git restore --staged` reclassification
   and the #22642 regressions.
5. Rule 3 first-touch refusal, including the stale-holder release and the owner-naming message.
6. Rule 2 commit guard with the R1 merge-path exemption.
7. Rule 4 cross-project write refusal (new rule YAML and condition).
8. Rule 1 gate 9 per-root live pairs.
9. R2 claim transfer, retiring `_claim_scope_conflicts`.
10. Gate 12 reads the single ledger, deleting the legacy proofs.
11. Recovery, checkpoint and remaining readers (worktree_checkpoint, run_completion,
    live_session_recovery, stage_review, monolith_guard, found_work_gate, code_review_scope).

Each acceptance item names `path::test_symbol`. Criteria 1 to 4 of #22644 are the skeleton: the rules
and definitions verbatim, each retirement with its replacement, the hash contract, rules 3 and 4 with
their event and error text, and the tests per retired mechanism.

## Design v2 (context 2, 2026-10-05; supersedes the sketch above where they differ)

### Verified facts found in context 2
- `production-size-growth` lint is PER SECTION (`semantic_lint.py::_lint_production_size_growth`): every
  section that targets a file of 850+ lines needs its own paragraph containing "split" or "move", naming
  the file and a new same-extension bare-path Target that does not exist yet. So touch each large file
  in exactly one section. Large files: commit_guard 956, _normalization_canonical 971, state_manager
  934, core.py 948, _lifecycle_close 910, hooks.py 852 (found_work_gate 877 and transcript_evidence
  991 are avoided: they stay Consumers unchanged).
- Undefined name in a rule `when:` raises inside `TemplatingMixin._evaluate_condition`, and a block
  effect then fails CLOSED. So rule 4's new eval-context key needs a default in `engine/core.py`.
- `canonical_repo_mutation` is False for a write into another repo:
  `_path_scope.py::_is_project_managed_path` returns `project_root is None`. Rule 4 must gate on
  `canonical_tool_kind == "write"` plus paths, NOT on `canonical_repo_mutation`. Rule 3 and the
  recorder keep the `canonical_repo_mutation` gate.
- `canonical_repo_mutation` is also True for `git add` (execute kind with repo_mutation), and today's
  rule 3 (`commit_guard.foreign_dirty_edit_conflict`) checks only `canonical_repo_mutation`, so it also
  inspects git add paths. The new rule 3 also requires `canonical_tool_kind == "write"`, inside
  commit_guard, so hooks.py is unchanged for rule 3.
- Write paths: `_build_canonical_tool_metadata` sets `canonical_write_file_paths` only when a caller
  passes write_paths (for example `cp src dst`). Decision W2: rule 3, rule 4 and the recorder use
  `canonical_write_file_paths` when the key is present, else `canonical_file_paths` (so a dirty `cp`
  source is not refused).
- `task_dirty_state.py`: `task_dirty_paths(paths, cwd)` (sync, `git status --porcelain=v1
  --untracked-files=all -- paths`) and `task_dirty_paths_async` (`daemon_git.status`). Both return None
  when git is unavailable. Untracked files show as dirty; ignored files show nothing.
- Recorder `_tool.py::_record_successful_file_mutation` is sync. `_paths_landed_before_edit` is the
  replay check: it skips a replayed outage-queued envelope whose content already landed. The content
  check subsumes it, because a landed path is clean, so nothing is recorded.
- `_tool.py::_resolve_repo_edit_paths(file_path, cwd, *, project_id)` returns
  `(repo_root, rel)`. It uses `find_project_root` (`.gobby/project.json`) and `get_project_context`,
  and returns None for another project's path. Decision: in 1.3, move its body into
  `task_claim_state.py::resolve_edit_pair` (new). The recorder and rule 3 share it, so rule 3 covers
  every pair the recorder records, including absolute paths into another checkout of the same project.
  Today rule 3 only resolves paths under `project_path` (`commit_guard._canonical_mutation_paths`).
- Owner rows today (`commit_guard._active_path_owners`): sessions with claimed open tasks in the
  project whose status is in `TERMINAL_OWNER_STATUSES` (= `LIVE_SESSION_STATUS_ORDER`), each read with
  `SessionVariableManager.get_variables`.
  - New: every live session in the project except self. The paths come from
    `session_dirty_file_set_for_checkout(variables, root)`. The task ref comes from the claimed task
    whose `task_edited_file_set_for_checkout` holds the path, else "no claimed task".
  - `ForeignPathOwner.owner_task_id` becomes `str | None`.
- `ForeignPathOwner`, `CheckoutPathOwnership`, `DirtyEditOwnershipInspectionError`,
  `active_owned_dirty_paths`, `foreign_owned_dirty_paths(_async)` and
  `inspect_checkout_path_ownership_async` keep their names and signatures and are reimplemented over
  live ownership.
  - Consumers that stay unchanged: `sync/integrity.py::_active_bundled_content_owner_sessions` (uses
    `active_owned_dirty_paths`, a NEW consumer missed in context 1),
    `found_work_gate.py::_foreign_owned_dirty_paths`, and `worktree_checkpoint.py` (call at line 389).
  - `_lifecycle_close_finalization._linked_commit_clean_proof_paths` and
    `_lifecycle_validation.validate_uncommitted_task_edits` lose their owner lookup in 1.7.
- Rule 2 today (`foreign_staged_commit_conflict`) is already R1-shaped. Candidates are `ls-files
  --cached --others --exclude-standard -- pathspecs` for a path-scoped commit, else `diff --cached
  --name-only --diff-filter=ACDMRTUXB`, intersected with owners, then dirty-checked by
  `_dirty_owned_paths_releasing_clean`.
  - Changes: the owners become live sessions; a path in the session's own `session_dirty_files` is
    allowed; when `MERGE_HEAD` exists in that checkout root, subtract `git diff --name-only HEAD
    MERGE_HEAD`; and the message drops the `release_task_paths` advice.
- Current refusal texts are in `commit_guard._format_conflict_reason`, `_format_dirty_edit_reason` and
  `_format_unverified_dirty_edit_reason`, with `_format_session_ref` giving `project#seq`.
- Gate 9: `_lifecycle_close.py:529-536` calls `apply_task_cleanliness_gate(ctx, evaluation,
  edited_paths=attribution.clean_proof_paths, owner_session_id, project_id, repo_path)`.
  - `evaluate_task_clean_proof(ctx, *, edited_paths, repo_path)` returns `TaskCleanProof(status,
    dirty_paths, reason)`. It is also called from `_lifecycle_close_finalization.commit_close` (line
    357, with `fresh_attribution.clean_proof_paths`).
  - `CloseAttributionSnapshot` (`_close_evaluation_support.py:56`) has `clean_proof_paths`, which feeds
    `CloseEvaluationFingerprint.capture`.
  - The gate 9 message is "Task-attributed files still have uncommitted changes: ... Commit them, or ask
    the owner to commit or release_task_paths, and retry." `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py:362`
    pins it.
- `capture_attribution` derives its live paths from `task_edited_file_set`. When that set is empty it
  falls back to linked-commit paths, and filters them with `_linked_commit_clean_proof_paths`, which is
  the foreign-owner use.
- `_cleanup_closed_claim` calls `remove_claimed_task`, then runs `clear_had_edits` if
  `commit_shas and not updates["task_edited_files"]`.
- `_lifecycle_paths.py` also DEFINES `_claimed_session_worktree_path` and `_lifecycle_checkout_root`.
  - They are used only by `_lifecycle_claim._claim_scope_conflicts` and by `_lifecycle_paths` itself,
    so they die with the module.
  - Tests that die with it:
    - `tests/mcp_proxy/tools/tasks/test_checkout_unresolved_envelopes.py::test_lifecycle_checkout_root_loads_the_session_once`;
    - `::test_inspect_task_path_ownership_returns_checkout_unresolved`.
  - Lists that name the tools:
    - `tests/mcp_proxy/tools/test_read_only_classification.py:38`;
    - `tests/workflows/test_task_enforcement_rules.py:61,89`.
  - Claim tests:
    - `tests/mcp_proxy/tools/test_claim_task.py` `test_claim_task_blocks_foreign_owner_of_declared_or_attributed_path` (line 180);
    - `test_claim_task_with_empty_scope_does_not_guess_conflicts` (line 265);
    - the patches at lines 210-214.
  - `_declared_affected_paths`, `_canonical_claim_scope_path` and `_claim_scope_conflict_reason` are
    used only by the scope check. `_task_attribution_sessions` stays for the R2 transfer.
- `mcp_proxy/tools/sessions/_actions.py` only registers `capture_baseline_dirty_files`.
  - Delete the whole file, and drop the `register_action_tools` import and call in
    `sessions/_factory.py::create_session_messages_registry` (line 103).
  - `git_utils.py::GIT_STATUS_UNAVAILABLE_MARKER` and `get_dirty_files_async` are used only by the
    baseline, so delete them.
  - `session_activation.py`: in `_missing_baseline_keys` and `_baseline_updates`, drop the
    `baseline_dirty_files` lines. Keep the names and keep the `baseline_dirty_tracking` invariant (it
    still seeds `session_edited_files`, `active_task_id` and `task_edited_files`).
  - The stale comment at `hooks.py:408` is fixed in 1.6, the only hooks.py section.
- Docs to update: `docs/guides/variables.md:118` (baseline), `docs/guides/sessions.md:242` (tool row),
  `docs/reference-audit/tasks.json` (rows near 468 and 484), `docs/reference-audit/sessions.json`, and
  skill refs `tasks/implementation.md` and `sessions/terminals.md`. Leave the historical docs alone
  (`docs/reviews`, `docs/design`, `docs/plans/completed`).
- Commit-parsing importers: `code_review_scope.py:26` (`GitCommitInvocation`,
  `resolve_commit_inspect_cwd`), `observer_commits.py:9`, `sessions/transcripts/tool_activity.py:320`,
  and `tests/workflows/test_commit_guard.py` (around lines 33 and 1214-1605).
- Rule 4 lookup: `storage/worktrees.py::LocalWorktreeManager.get_by_path(worktree_path)` returns a
  `Worktree` with `.task_id` and `.project_id`. The project context comes from
  `utils/project_context.py::find_project_root` and `get_project_context(root)` (which returns `id` and
  `project_path`).
- Existing tests: `tests/hooks/test_normalization.py` covers git restore classification; `test_commit_guard.py`
  and `test_task_enforcement_rules.py` cover the guard rules.
- Literal sweeps used `rg` because `gcode grep` truncated multi-file sweeps; state this in Constraints.

### Final section order and file ownership (each large file in exactly one section)
1. **1.1 R3 retirements:**
   - delete the whole `_lifecycle_paths.py` (`::*` with operation: delete);
   - `_lifecycle.py::create_lifecycle_registry`;
   - `_lifecycle_claim.py` (scope-check symbols plus `register_claim_task`);
   - `blocking.py::TASK_MUTATION_TOOLS_BY_SERVER`;
   - `implementation.md` and `tasks.json`;
   - tests: delete `test_release_task_paths.py` and update the lists and claim tests.
   - Message texts naming `release_task_paths` stay with their owners (1.3 and 1.7).
2. **1.2 Baseline retirement:** `_actions.py` delete, `_factory.py`, `session_activation.py`,
   `git_utils.py`, the defaults yaml if it lists the baseline, the docs, and tests (delete
   `test_capture_baseline.py`; update `test_session_activation_reconciliation.py`,
   `test_internal_action_tools.py` and `test_direct_tool_session_activation.py`).
3. **1.3 Rules 2 and 3 judge live session ownership** (depends: 1.1).
   - commit_guard SPLIT: move the commit-parsing half (lines 45-322: `GitCommitInvocation`,
     `parse_git_commit_invocations`, the shell helpers, `resolve_commit_inspect_cwd`) to
     `src/gobby/workflows/git_commit_parsing.py` (new). Update the importers.
   - Add `task_claim_state.resolve_edit_pair`, used by `_tool.py` (recorder resolution) and by rule 3.
   - Rule 3:
     - write-kind only, using W2 paths;
     - a pair in its own `session_dirty_file_set_for_checkout` is allowed with no git call;
     - otherwise run a pathspec status per root: dirty means refuse and name the owners or "no live
       session owns it"; clean means allow and release the pair from every other live holder (W4);
       None means refuse as unverified.
   - Rule 2: the R1 merge exemption and live owners.
   - Keep the YAML names and keys (`block-cross-session-foreign-dirty-edit`,
     `block-cross-session-foreign-staged-commit`); update only the descriptions.
   - Delete `_release_clean_ledger_entries`' call to `release_task_edited_files` (stale release touches
     only `session_dirty_files`), which lets 1.4 delete the method without touching commit_guard.
   - Granularity: rules 2 and 3 share the owner lookup and formatter in one near-ceiling module.
4. **1.4 One ownership ledger** (depends: 1.2, 1.3).
   - state_manager SPLIT: move the ledger mutators to `src/gobby/workflows/edit_ledger.py` (new).
   - Also: `task_claim_state.py`, `ledger_reconcile.py`, the defaults yaml,
     `monolith_guard.outstanding_monolith_paths` (project each recorded pair against its own root),
     `_tool.py` (drop `edited_at`; delete `_paths_landed_before_edit`), `_close_evaluation_support.py`
     (gate 12: `task_edited_checkout_paths` returns all pairs; delete the history accessor,
     `_legacy_task_checkout_proof`, `_legacy_closed_other_tasks`, `_without_closed_task_paths`,
     `_same_git_checkout` if unused, and the `history_started_at` reads), `_cleanup_closed_claim`,
     `worktree_checkpoint._authorized_task_paths` (delete the legacy branch), and the
     `task_recovery` docstring.
   - Granularity: one lifecycle owner. Every file reads the retired shape directly.
5. **1.5 Recorder records only content-changing writes** (depends: 1.4).
   - `_tool.py` content check.
   - `_normalization_canonical` SPLIT: move the `git add`, `git checkout` and `git restore`
     classification branches (lines about 644-690) to `src/gobby/hooks/_normalization_git_paths.py`
     (new), and make `restore --staged` without `--worktree`/`-W` execute-kind.
   - #22642 regression tests.
6. **1.6 Rule 4** (depends: 1.3).
   - hooks.py SPLIT: move the ownership eval-context wiring (lines about 517-580) to
     `src/gobby/workflows/ownership_eval_context.py` (new); rule 4's check also lives there.
   - core.py SPLIT: move the eval_context `setdefault` block (lines 319-332) to
     `src/gobby/workflows/engine/eval_context_defaults.py` (new), adding a
     `cross_project_write_conflict` default of "".
   - New YAML `block-cross-project-write.yaml`.
   - Fix the stale comment at `hooks.py:408`.
7. **1.7 Rule 1, gate 9 per checkout** (depends: 1.4).
   - `CloseAttributionSnapshot.clean_proof_paths` is replaced by `live_pairs`.
   - Changed functions: `evaluate_task_clean_proof(ctx, *, live_pairs)`,
     `apply_task_cleanliness_gate`, `validate_uncommitted_task_edits` (drop the owner lookup and the
     release advice), `capture_attribution`, `commit_close`, and the fingerprint.
   - Delete `_linked_commit_clean_proof_paths`.
   - `_lifecycle_close` SPLIT: move `_acceptance_root_diagnostic`, `_is_deliberate_close` and
     `_apply_escalated_close_gate` (lines 96-152, used only inside the file) to
     `src/gobby/mcp_proxy/tools/tasks/_close_gate_helpers.py` (new).
   - Message: "Files this session created or mutated for the task are uncommitted: <root>/<rel>, ...
     Commit them and retry."
8. **1.8 R2 claim transfer** (depends: 1.1, 1.4).
   - After the claim succeeds, for each `_task_attribution_sessions` session other than the claimant
     that is not live, or that no longer claims the task, move its live pairs for the task (task
     ledger ∩ `session_dirty_files`) into the claimant's `session_dirty_files` and
     `task_edited_files[task]`, and drop them from the predecessor's `session_dirty_files`. The
     predecessor's task ledger stays for gate 12 credit.

### Writer decisions taken in context 2 (record W1-W6; send W3 to the Orchestrator for ruling)
- **W1, accessor semantics.**
  - `task_edited_file_set`, `task_edited_file_set_for_checkout` and `target_task_has_edits` return the
    task's LIVE pairs (task ledger ∩ `session_dirty_files`), today's effective meaning.
  - `task_edited_checkout_paths` returns ALL recorded pairs (gate 12).
  - So `hooks.py`, `run_completion`, `_live_session_recovery`, `_stage_review`, `code_review_scope`,
    `handoff_summary`, `transcript_evidence` and `require-commit-before-status` stay unchanged.
- **W2, write paths:** use `canonical_write_file_paths` when present, else `canonical_file_paths`.
- **W3, merges and rule 3.** While `MERGE_HEAD` exists in a checkout, writes to paths in `git diff
  --name-only HEAD MERGE_HEAD` (plus unmerged paths) are exempt from rule 3, so the MM can resolve
  conflicts. NOT yet ruled; ask the Orchestrator.
- **W4, stale holders.** At rule 3's clean first touch, release the pair from every other live
  holder's `session_dirty_files`. Otherwise an unreconciled committed holder would duplicate ownership,
  and its gate 9 would block on another session's edit. Accepted race: two simultaneous clean first
  touches both record.
- **W5, owners.** Owners are all live sessions in the project. The task ref is the claimed task whose
  live pairs hold the path, or "no claimed task".
- **W6, task ledger lifetime.** The task ledger is append-only for the session's life:
  `remove_claimed_task` no longer drops it, because closed tasks' pairs exclude them from later tasks'
  gate 12 evidence, which the history ledger did. `_cleanup_closed_claim` then clears had_edits when no
  task still in `claimed_tasks` has live pairs.
- **User-directed exception (rule 2).** It needs no mechanism: an ended owner is not live, a live
  owner is directed by the user, and the user's terminal commits bypass agent hooks.

### Refusal texts (draft)
- **Rule 3:**
  - "Edit blocked: dirty path(s) were changed outside this session (rule 3: you may not mutate files
    that are dirty):"
  - then one line per path, "- <path> — session <ref>, task <ref>" or "- <path> — no live session owns
    it";
  - then "Ask each owner with `gobby-agents.send_message` to commit its work; ask the user about
    unowned paths. Then retry."
  - Unverified: keep the existing `_format_unverified_dirty_edit_reason` text.
- **Rule 2:**
  - "Commit blocked: staged path(s) hold another live session's uncommitted work (rule 2: do not commit
    files you did not create or mutate):"
  - then one line per path;
  - then "Commit only your own paths with `git commit --only -- <paths>`, or ask each owner with
    `gobby-agents.send_message` to commit first."
- **Rule 4:**
  - "Write blocked: <path> is in project <name> (<id>), not this session's project <name> (rule 4: do
    not create or mutate files in another Gobby-managed repo)."
  - then "Work there from a session in that project, or through a worktree of that project registered
    to a task you have claimed."
- Hook event for rules 2, 3 and 4: `before_tool`. Priorities: rule 2 is 25 and rule 3 is 26; rule 4
  gets 24.

### Status
- The MM released the main hold (#23536 landed at a8b9514bea), so staging and committing my own plan
  file is allowed, path-only, after an empty-index check.
- The Orchestrator's backlog-zero rule (13:29) exempts Lane 7 planning.
