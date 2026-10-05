Plan artifact: `.gobby/plans/content-ownership-rules.md`

# Content-based ownership rules for close gates and edit hooks

**Plan ID:** content-ownership-rules

## Overview
`kind: framing`

This plan specifies #22644 (Replace edit attribution with four content-based
ownership rules for close gates and edit hooks), under epic #22949 (Lane 7,
planning and research). It replaces the edit-attribution layer with Josh's
four ownership rules (§Ownership Contract) and retires every mechanism the
task names (§Retired Mechanisms).

**The problem.** Attribution today means "a successful tool call named this
path", never "this call changed the file's bytes". Two observed failures follow
from that:
- **#22642 (plan-authoring task).** gobby#14054 was blocked at close by gate 9
  (uncommitted_task_edits) and then gate 10 (test-types audit) on
  `tests/e2e/test_terminal_client_stack.py`, a file it never changed. The path
  was already staged in the shared main checkout before the claim. A git index
  command whose tool input named the path flipped it from staged to unstaged,
  and the recorder (`src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._record_successful_file_mutation`)
  logged that as an edit. `release_task_paths` then refused to release it.
- **#23363 (Evidence stall fix), land 7.** The Merge Manager's landing-merge
  commit in the main checkout was blocked by
  `block-cross-session-foreign-staged-commit`, because the six merged paths
  were attributed to the live owner (15011/#23363). The owner's
  `release_task_paths` refused, since the uncommitted content was the merge.
  Neither side could proceed. Land SHAs: base `b3a65779db`, MERGE_HEAD
  `7094fc272b`. The workaround (merge --abort, release while clean, re-stage)
  landed as `a6d5aeb17b`.

**When the leaves close:**
- A session owns a path only when one of its write-kind tool calls changed
  that path's bytes. A call that names a path without changing it records
  nothing (the #22642 regression).
- A session may not mutate a path that is dirty and not its own (rule 3). The
  refusal names each live owner's session ref and task ref.
- A commit may not capture another live session's dirty paths (rule 2). A
  landing merge passes for every path the merge brings in.
- A write into another Gobby-managed project is refused (rule 4), except
  through a registered worktree of that project bound to a task the session
  claims.
- Gate 9 (rule 1) checks only the task's live pairs, the attribution
  owner's plus any untransferred predecessor pairs, per checkout, and names
  no foreign owner. Close ends the task's live tags in every linked session.
- One ledger carries ownership: `session_dirty_files` holds the live pairs,
  each tagged with the one task it is live for, and `task_edited_files` is
  the append-only per-task history. The edit-time, checkout-mirror,
  history and baseline variables are gone, along with `release_task_paths`,
  `inspect_task_path_ownership`, `capture_baseline_dirty_files` and the
  claim-time scope check.

## Ownership Contract
`kind: framing`

Josh's four rules, verbatim from #22644 (user decision, 2026-09-20):

1. If you create or mutate a file in a git-tracked folder, you must commit it before closing.
2. You must not commit files you did not create or mutate during your session (exception: the user directs it, e.g. #22640; landing merges are merge commits and already fit).
3. You may not mutate files that are dirty.
4. You may not create or mutate files in another gobby-managed repo.

Definitions agreed in the same exchange, verbatim. The task's validation
criteria call these "the three definitions"; the exchange recorded four
bullets, and all four are binding here:

- 'Mutate' means bytes changed: hash the named path before and after the mutating call (or compare against the pre-touch blob at close). A call that names a path without changing its content records nothing.
- 'Dirty' in rule 3 means dirty before this session first touched it: at first touch, a pathspec-limited git status for that one path plus absence from the session's own ledger means refuse. No session-start baseline is needed.
- Rule 4 keys on the project, never the checkout path: a task worktree under ~/.gobby/worktrees and the main checkout are the same repo; forbidden is writing into a checkout of a different project except through a worktree of that project owned by the task (memory a524ed54 documents that sanctioned path).
- Accepted blind spot: shell commands that edit files without naming them in tool input (sed -i, formatters via Bash). Do not add git status diffs around Bash calls to close it.

**Where each rule is enforced:**

| Rule | Hook event | Enforcing rule template | Section |
| --- | --- | --- | --- |
| 1 | `close_task` gate 9 (`uncommitted_task_edits`) | none (close checklist) | 1.7 |
| 2 | `before_tool` | `block-cross-session-foreign-staged-commit` (priority 25) | 1.3 |
| 3 | `before_tool` | `block-cross-session-foreign-dirty-edit` (priority 26) | 1.3 |
| 4 | `before_tool` | `block-cross-project-write` (priority 24, new) | 1.6 |

## Content-Hash Contract
`kind: framing`

This section answers #22644 criterion 2. It implements the definition's
"hash the named path before and after the mutating call" option with a
sha256 of the path's bytes, held in memory only for the duration of the call
(Decision 5).

- **Which tools.** Every tool call that normalization classifies as
  `canonical_tool_kind == "write"`, over the write paths `resolve_edit_pair`
  maps to a pair (Decision 6): Write,
  Edit, MultiEdit, NotebookEdit, apply_patch, and shell segments that name
  their write targets (redirection, `cp`/`mv` destinations, `git checkout --`
  and `git restore` with a worktree effect, inline Python writes). The paths
  are `canonical_write_file_paths` when that key is present, else
  `canonical_file_paths` (Decision 11). In a compound shell command only the
  write-kind segments' targets count. Execute-kind calls and segments, including
  `git add` and `git restore --staged` without `--worktree`/`-W`
  (Decision 7), record nothing and are not checked by rule 3.
- **Path resolution.** The before-tool snapshot, the recorder, rule 3 and
  rule 4 resolve a write path the same way. A relative path joins the
  effective tool cwd, `task_claim_state.effective_tool_cwd(event.cwd,
  event.data)`: the tool input's `workdir`, else its `cwd`, joined to
  `event.cwd` when relative, else `event.cwd`. Symlinks are then resolved,
  and the checkout root is the innermost `.gobby/project.json` root above the
  resolved target, whether the input was relative or absolute. So
  `../other-worktree/p.py` and a relative symlink that leaves the cwd's
  checkout reach the checkout that holds the real file. With no `event.cwd`
  and no absolute `workdir` or `cwd` in the tool input, a relative path has
  no pair at any of the four sites; absolute paths resolve unchanged. This narrows today's rule 3, which
  read such a path against `project_path` although the recorder could never
  record it. Adapters copy `event.cwd` from the provider payload.
- **Where the pre-call identity is taken.** At `before_tool`,
  `ToolEventHandlerMixin.handle_before_tool` stores the sha256 of each write
  path's bytes, or None when the path is absent. Entries live in the
  daemon-wide in-memory store `pre_write_digests.PRE_WRITE_DIGESTS`, keyed by
  `(session_id, request_id, realpath)`. `request_id` is the event's
  correlation id, which `hooks/events.py::correlate_hook_lifecycle` fills
  from `tool_use_id`, `toolCallId`, `tool_call_id` and the other provider
  keys, so overlapping calls on one path keep separate baselines. When an
  adapter supplies no id, `request_id` is "", and a later `before_tool` on
  the same session and path overwrites the entry. Separately, rule 3 runs one pathspec-limited `git status
  --porcelain=v1 --untracked-files=all -- <path>` per write path the session
  does not already own, to refuse a path dirty before first touch.
- **Where the post-call comparison runs.** At `after_tool`, for every
  write-kind call whether it succeeded or failed, the recorder takes each
  path's entry and re-hashes the path. Different bytes, including a created
  or deleted file, mean mutation, so the pair is recorded. Equal bytes record
  nothing, even for a path the session already owns or a merge-set path. A
  pair recorded while clean, because the same call committed it, stays in
  `task_edited_files`; reconciliation releases its live pair.
- **No entry: bounded fallback.** A path has no entry after a daemon restart
  during the call, a `before_tool` event that never reached the daemon, an
  id-less overlapping call whose entry the other call took, or an entry older
  than one hour. For those paths and for directories, which have no byte
  hash, the post-call pathspec status decides: dirty records the pair, clean
  records nothing. This status is no proof of mutation. In these branches
  only, a no-op on an already-dirty path is recorded (the F2 shape), and a
  write committed by the same call is missed (the F3 shape). The policy
  chooses to record when in doubt, so rule 1 never misses dirt the session
  may have made; a false record costs a commit of the session's own paths,
  which rule 2 allows.
- **Hash read failure.** Each path is observed on its own. A path whose
  before-call hash raises `OSError` stores no entry. A path whose after-call
  hash raises `OSError` (for example, the call made it unreadable) takes the
  no-entry fallback. Either way the other paths of the call are still
  hashed and compared. Failures that are not observation failures, such as
  the ledger write, still reach `handle_after_tool`'s outer catch, which logs
  them.
- **Entry lifetime.** `after_tool` removes the entries it reads. A session's
  remaining entries are dropped at its `session_end`, and any entry older
  than one hour is dropped whenever a new one is stored, so entries whose
  `after_tool` never fires do not accumulate.
- **Untracked paths.** A file the call creates changes from absent to
  present, so it is recorded (creation is mutation). A pre-existing untracked
  file the session does not own is dirty at first touch, so rule 3 refuses
  the write.
- **Ignored paths.** Git reports nothing for them, so rule 3 never refuses
  them. A changed ignored path is recorded, as the recorder does today, and
  gate 9 never blocks on it because git reports it clean. Rule 1 covers
  git-tracked files only.
- **Paths outside any checkout.** `resolve_edit_pair` returns no pair when no
  `.gobby/project.json` root contains the path (Decision 13), so the path is
  neither checked by rule 3 nor recorded. Rule 4 still applies to every
  write path that resolves.
- **Paths in another project's checkout.** Rule 4 refuses an unsanctioned
  write before it runs, and `resolve_edit_pair` returns no pair for it. Rule
  4's exception, a registered worktree of the other project bound to one of
  the session's claimed tasks (Decision 9), does not waive rules 1 to 3:
  `resolve_edit_pair` returns that worktree's pair, so rule 3 checks the first
  touch, the recorder records a change under the worktree's real root, and
  gate 9 checks the pair.
- **Git status unavailable.** At `before_tool`, rule 3 refuses as unverified
  (today's text, kept). At `after_tool` the hash comparison needs no git. In
  the no-entry fallback, a failed status records the pair, and ledger
  reconciliation releases it once git reports it clean.
- **Accepted race.** Another process can change the path between the two
  hashes, and the recorder then records that change. Overlapping calls by one
  session on one path each compare against their own baseline, so the later
  call also records a change the earlier one made; both records belong to
  the same session. Two simultaneous clean first touches by different
  sessions both record.

## Decision Record
`kind: framing`

1. **R1, rule 2 scope (Orchestrator gobby#14972, 2026-10-05 13:03 CT). This
   interprets Josh's verbatim rule 2; Josh rules on it at approval.** Rule 2
   blocks a commit only when a committed path is dirty and is held in another
   live session's `session_dirty_files`. Paths no live session mutated commit
   freely. A merge commit is exempt only for the paths the merge brings in,
   `git diff --name-only --no-renames HEAD MERGE_HEAD`; `--no-renames` lists
   both sides of a rename. Every other staged path in a merge commit still
   follows rule 2. #23363 land 7 passes: its six paths are exactly the
   files that differ between HEAD `b3a65779db` and MERGE_HEAD `7094fc272b`.
2. **R2, transfer at claim (Orchestrator).** At claim, the task's live pairs
   (pairs tagged with the task, Decision 8) move from ended or
   no-longer-claiming predecessor sessions into the claimant's ledger, still
   tagged with the task. A predecessor's pair tagged with another task never
   moves, even when the predecessor's history names the claimed task.
   Ownership never duplicates across two live sessions.
   Each move is one hub transaction under a new `SessionVariablePairMutation`
   lock that takes both rows' session-variable advisory keys in session-id
   order. Eligibility is rechecked inside it, a failure is reported in the
   claim result, and a later `already_claimed` claim retries it (Orchestrator,
   2026-10-05: enhancer E3 accepted, option (a); a two-step move was rejected
   because a failure between the steps leaves two live holders). Until the
   retry succeeds, gate 9 refuses the close while an untransferred pair is
   dirty (1.7), so the claim's success never waives rule 1.
3. **R3, retirements (Orchestrator).** Retire `_claim_scope_conflicts`,
   `inspect_task_path_ownership` and `release_task_paths`. Every rule 2 and
   rule 3 refusal names the owning session ref and task ref.
4. **Single-machine scope (accepted).** Josh parked #23277 (Hook envelope
   carries edited-file content) until the Rust multi-machine port. This plan
   has no foreign-origin sections and no prerequisite on #23277. Node-side
   ownership belongs to the Rust port.
5. **Mutation detection hashes the named path before and after the call
   (Orchestrator, 2026-10-05: replacement accepted).** At `before_tool` the
   handler stores the sha256 of each write path's bytes in memory; at
   `after_tool` the recorder re-hashes it, and only different bytes record.
   This follows Josh's definition literally. The first draft's rule (rule 3's
   clean first touch proves the HEAD blob, then a post-call pathspec status
   decides) was replaced after the Adversary showed three misses: a write in
   a call that then fails, a no-op write to an already-dirty path (owned
   under another task, or in the merge set) and a write committed by the same
   call. The objections that first rejected stored hashes no longer apply.
   Entries are keyed by session, the event's normalized `request_id` (from
   `correlate_hook_lifecycle`; "" when the adapter supplies none) and path,
   so no adapter reads its own call id. A call with no entry, after a daemon
   restart for example, falls back to the post-call status rule, so no
   durable store is needed. That fallback and directories keep the old rule's
   F2 and F3 shapes, bounded as the Content-Hash Contract states.
6. **Write-kind calls only (accepted).** The recorder and rule 3 apply only to
   `canonical_tool_kind == "write"`, over the write paths that
   `resolve_edit_pair` maps to a pair. Today's rule 3 checks
   `canonical_repo_mutation` alone, so it also inspects `git add` paths; that
   ends. `canonical_repo_mutation` is no longer their gate. Normalization has
   no claim state, so it is false for a sanctioned worktree of another project
   (Decision 9). It is also false for a separate clone of this project (same
   `.gobby/project.json` id, different git repository), which rules 1 to 3
   now cover too, because `resolve_edit_pair` keys on the project id.
7. **`git restore --staged` is execute-kind (accepted).** Without
   `--worktree`/`-W` it changes only the index. It is reclassified from
   `"write"` to `"execute"`, like `git add`. This is the #22642 command.
8. **One ledger, live set and history kept apart (accepted; live tag by
   Orchestrator ruling on CO-A3-F9, 2026-10-05).**
   `session_dirty_files: {checkout_root: {rel: task_id | null}}` is live
   ownership. Each live pair carries the one task it is live for, or null.
   `task_edited_files: {task_id: {checkout_root: [rel]}}` is the append-only
   per-task history, for audit and gate 12; it never makes a pair live. A
   task's live pairs are the session's pairs tagged with that task. A
   mutation recorded while task B is active tags the pair B, replacing any
   earlier tag, so the pair is live for B only. The tag ends when
   reconciliation drops the pair, when the pair transfers to a claimant
   (Decision 2), when the session mutates it for another task, or when the
   task closes. At close, `_cleanup_closed_claim` sets the task's tags to
   null in the attribution owner's ledger and in every other session linked
   to the task, so a clean untransferred predecessor pair never stays live
   for a closed task. A nulled pair stays in its session's live set, and
   history is untouched. A claim released without closing (live-session
   recovery, a spawn handoff, a claim observer) keeps the tag, so the next
   claimant can take the pair.
   Rejected: a release marker on history rows, which reconciliation,
   re-mutation and close would each have to flip; the tag ends with the pair
   that reconciliation already drops. Every other attribution variable is
   deleted. Legacy branches are deleted outright under AGENTS.md rule 10 (no
   backward compatibility): a stored value of the old shape reads as empty.
9. **Rule 4 compares project ids (accepted).** The project id of the path's
   `.gobby/project.json` root must equal the session's project id. The single
   exception is a registered worktree of the other project whose `task_id` is
   one of the session's claimed tasks. The exception does not waive rules 1
   to 3 (Orchestrator, 2026-10-05: enhancer E4 accepted). The predicate
   `task_claim_state.sanctioned_worktree_root` serves both rule 4 and
   `resolve_edit_pair`, so the worktree's pairs are checked, recorded and
   gated like any own-project pair.
10. **W1, accessor meaning.** `task_edited_file_set`,
    `task_edited_file_set_for_checkout` and `target_task_has_edits` return the
    task's live pairs (the session pairs tagged with the task), which is their
    effective meaning today: `reconcile_edit_ledgers` drops clean pairs from
    the per-task live ledger, and `remove_claimed_task` drops a closed task's
    rows. `task_edited_checkout_paths` returns every recorded pair (gate 12
    evidence). Their callers therefore stay unchanged.
11. **W2, write paths.** Rules 3 and 4 and the recorder read
    `canonical_write_file_paths` when the key is present, else
    `canonical_file_paths`. A dirty `cp` source is therefore not refused.
    For a shell command, `canonical_write_file_paths` is the union of the
    write targets of its write-kind segments: a segment's explicit write paths
    (the `cp` destination), else its paths. Execute-kind segments (`git add`,
    index-only `git restore`) contribute none (1.5).
12. **W3, merges and rule 3 (Orchestrator, 2026-10-05: accepted). This
    interprets Josh's verbatim rule 3; Josh rules on it at approval, alongside
    Decision 1.** While `MERGE_HEAD` exists in a checkout, writes to paths in
    that checkout's merge set (`git diff --name-only --no-renames HEAD
    MERGE_HEAD`, Decision 1) are exempt from rule 3, so the Merge Manager can
    resolve conflicts with edit tools. The recorder then records them for the
    writing session, which lets its merge commit pass rule 2 for its own
    edits. The exemption ends when `MERGE_HEAD` is gone and covers no path
    outside the merge set. Rejected alternative: conflict resolution only
    through shell commands that name no path.
13. **Paths outside every Gobby checkout are ignored.** `resolve_edit_pair`
    drops today's cwd fallback (`find_project_root(search_start) or cwd_path`):
    with no `.gobby/project.json` root there is no pair. Such a path is in no
    Gobby-managed repo, so rules 1 to 3 cannot apply to it, and git status at
    a non-repository cwd could only fail. The root is found from the resolved
    target, never from the cwd (§Content-Hash Contract, Path resolution).
14. **W4, stale holders.** At rule 3's clean first touch, the pair is released
    from every other live holder's `session_dirty_files`. Otherwise a holder
    whose ledger was not reconciled after its commit would duplicate ownership,
    and its gate 9 would block on another session's edit.
15. **W5, owners.** Owners are all live sessions (status in
    `LIVE_SESSION_STATUS_ORDER`) whose `session_dirty_files` holds the path
    under that checkout root, not only sessions with a claimed open task. The
    lookup has no project filter, because a sanctioned worktree's holders
    belong to another project (Decision 9) and ledger keys are already
    realpath roots.
    The task ref is the pair's live tag when the holder still claims that
    task, else "no claimed task".
16. **W6, task ledger lifetime.** `task_edited_files` is append-only for the
    session's life: `remove_claimed_task` no longer drops it, because a closed
    task's pairs must keep excluding those paths from a later task's gate 12
    evidence (the job the history ledger did). The existing exemption stays:
    a task that closed before the evidence window excludes only its live
    pairs. `_cleanup_closed_claim` clears
    `had_edits` when no task still in `claimed_tasks` has live pairs.
    History serves audit and gate 12 only and never revives a live
    association (Orchestrator ruling on CO-A3-F9). Every other reader,
    `outstanding_monolith_paths`, transfer, the close guard and rules 2 and 3
    among them, reads live tags.
17. **The rule 2 user-directed exception needs no mechanism.** An ended owner
    is not live, a live owner acts on the user's direction, and the user's own
    terminal commits bypass agent hooks.
18. **Refusal events.** Rules 2, 3 and 4 block at `before_tool` through rule
    templates whose `block` effect carries the computed reason. Rule 1 stays a
    close-checklist gate.
19. **Rejected: a claim-time scope check.** `_claim_scope_conflicts` refused a
    claim whose declared or attributed paths another session owned. Rule 3
    already refuses the first write to such a path with the owner named, so
    the check duplicates it and blocks claims that would never touch the path.

## Retired Mechanisms
`kind: framing`

This section answers #22644 criteria 1 and 4. Each bullet names the retired
mechanism, its replacement, and the tests deleted or changed.

- **`release_task_paths`** (`_lifecycle_paths.py`, whole module, with
  `inspect_task_path_ownership`, `_claimed_session_worktree_path` and
  `_lifecycle_checkout_root`).
  - Replacement: no release tool. Reconciliation releases clean pairs, rule
    3's clean first touch releases stale holders (Decision 14), and claim
    transfers a predecessor's live pairs (Decision 2).
  - Delete `tests/mcp_proxy/tools/test_release_task_paths.py` (1.1).
  - Delete `test_lifecycle_checkout_root_loads_the_session_once` and
    `test_inspect_task_path_ownership_returns_checkout_unresolved` in
    `tests/mcp_proxy/tools/tasks/test_checkout_unresolved_envelopes.py` (1.1).
  - Change the tool-name lists in
    `tests/mcp_proxy/tools/test_read_only_classification.py` and
    `tests/workflows/test_task_enforcement_rules.py` (1.1).
  - Change `tests/workflows/test_commit_guard.py` (release advice in the rule 2
    text, 1.3) and `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py`
    (release advice in the gate 9 text, 1.7).
- **`task_edited_file_times`** and the replay check
  `ToolEventHandlerMixin._paths_landed_before_edit`.
  - Replacement: the content check. A replayed envelope whose content already
    landed finds the path clean, so nothing is recorded (§Content-Hash
    Contract).
  - Change `tests/workflows/test_task_claim_state.py`,
    `tests/workflows/test_session_variable_manager.py`,
    `tests/hooks/test_tool_handlers.py` (`edited_at`) and
    `tests/agents/test_terminal_timeout_checkpoint.py` (1.4).
- **`task_edited_file_checkouts`**, its `_history` and `_history_started_at`
  variants, and `session_dirty_file_checkouts`.
  - Replacement: the task-tagged nested `session_dirty_files` replaces the
    per-task live map `task_edited_file_checkouts` and
    `session_dirty_file_checkouts`, and the nested append-only
    `task_edited_files` replaces the history variants (Decision 8).
  - Change `tests/workflows/test_session_variable_manager.py`,
    `tests/workflows/test_task_claim_state.py`,
    `tests/workflows/test_code_review_scope.py`,
    `tests/workflows/test_hooks.py`, `tests/workflows/test_workflow_hooks.py`,
    `tests/storage/tasks/test_claim_transfer_state.py`,
    `tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py`,
    `tests/mcp_proxy/tools/tasks/test_escalation_coordinator.py`,
    `tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py` and
    `tests/agents/test_terminal_timeout_checkpoint.py` (1.4); change
    `tests/workflows/test_commit_guard.py` (1.3) and
    `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py`
    (1.7).
- **`capture_baseline_dirty_files`** and the `baseline_dirty_files` variable.
  - Replacement: rule 3's first-touch pathspec status. No session-start
    baseline exists.
  - Delete `tests/mcp_proxy/tools/sessions/test_capture_baseline.py` and the
    `TestSessionCaptureBaselineDirtyFiles` class in
    `tests/mcp_proxy/tools/test_internal_action_tools.py` (1.2).
  - Change `tests/hooks/test_session_activation_reconciliation.py` and
    `tests/mcp_proxy/services/test_direct_tool_session_activation.py` (1.2).
  - Drop the inert `baseline_dirty_files` fixture key or label from
    `tests/hooks/test_stop_handoff_pending.py`,
    `tests/hooks/test_hook_manager.py`,
    `tests/workflows/test_database_deadline_retry.py`,
    `tests/workflows/test_hook_evaluation_serialization.py`,
    `tests/workflows/test_hook_evaluation_timeout.py`,
    `tests/workflows/test_proxy_hooks.py`,
    `tests/workflows/test_skill_loaded_call_tool_path.py`,
    `tests/workflows/test_turn_interrupt_stop_gates.py` and
    `tests/workflows/test_hooks.py` (1.2), and from
    `tests/workflows/test_commit_guard.py` (1.3) and
    `tests/workflows/test_session_variable_manager.py` (1.4).
- **Foreign-owner inspection in gate 9**
  (`_lifecycle_validation.py::validate_uncommitted_task_edits` calling
  `foreign_owned_dirty_paths`, and
  `_lifecycle_close_finalization.py::_linked_commit_clean_proof_paths`).
  - Replacement: gate 9 checks only the task's live pairs, the attribution
    owner's plus any untransferred predecessor pairs, per checkout root
    (1.7). Dirt live for another task or for no task never blocks the
    close, because it is not the task's to commit, and rule 3 already kept
    the owner from writing another session's dirt.
  - Change `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py`,
    `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py`
    and `tests/mcp_proxy/tools/tasks/test_close_attribution.py` (1.7).
- **Claim-time scope check** (`_claim_scope_conflicts` and its helpers).
  - Replacement: rule 3 (Decision 19).
  - Delete `test_claim_task_blocks_foreign_owner_of_declared_or_attributed_path`
    and `test_claim_task_with_empty_scope_does_not_guess_conflicts` in
    `tests/mcp_proxy/tools/test_claim_task.py`, and its scope-check patches
    (1.1).
- **`SessionVariableManager.release_task_edited_files`**, the removal of a
  closed task's rows in `remove_claimed_task`, and the legacy gate 12 proofs
  (`_legacy_task_checkout_proof`, `_legacy_closed_other_tasks`,
  `_without_closed_task_paths`, `_same_git_checkout`).
  - Replacement: the append-only task ledger (Decision 16). Close ends the
    task's live tags in every linked session through `end_task_tags`
    (Decision 8).
  - Change `tests/workflows/test_session_variable_manager.py`,
    `tests/workflows/test_task_claim_state.py` and
    `tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py`, and
    replace `test_closed_task_cleanup_removes_only_its_edit_entry` in
    `tests/mcp_proxy/tools/tasks/test_close_task_flow.py` (1.4).

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and targets files, never the full suite. No leaf restarts the daemon. The
  rule templates (1.3, 1.6) go live only after the Orchestrator's next daemon
  restart syncs them (AGENTS.md rule 8).
- **Order.** Retirements (1.1, 1.2) remove consumers first. Rules 2 and 3
  (1.3) land before the ledger reshape (1.4) and use the existing accessors,
  whose signatures survive 1.4. That keeps `commit_guard.py` in one section.
- **Large files.** Each production file of 850 or more lines is targeted by
  exactly one section, which splits it: `commit_guard.py` (956 lines, 1.3),
  `state_manager.py` (934, 1.4), `_normalization_canonical.py` (971, 1.5),
  `workflows/hooks.py` (852, 1.6), `workflows/engine/core.py` (948, 1.6) and
  `_lifecycle_close.py` (910, 1.7). `found_work_gate.py` (877) and
  `tasks/transcript_evidence.py` (991) are never targeted; their calls keep
  working because the accessors and owner helpers keep their signatures.
- **Sweep evidence.** Consumer sweeps used `rg` over `src/gobby`, `tests/` and
  `docs/` because `gcode grep` truncates multi-file sweeps. Run from `0.5.0` at
  `a78a5e9982`:
  - `rg -l "release_task_paths|inspect_task_path_ownership|capture_baseline_dirty_files|baseline_dirty_files|task_edited_file_times|task_edited_file_checkouts" docs/ src/gobby/install/`
    found the six live docs this plan targets; `docs/reviews`, `docs/design`
    and `docs/plans/completed` are historical and stay unchanged.
  - `rg -c` over `tests/` for the retired names found the 30 test files
    listed in §Retired Mechanisms.
  - `rg -n task_edited_files src/gobby` found the raw readers this plan
    targets; `tasks/transcript_evidence.py` receives sets as parameters and
    never reads the variable.
- **No new persisted state beyond the two ledgers.** The ownership state
  kept in session variables is `session_dirty_files` and `task_edited_files`
  only; there is no session baseline. The pre-write sha256 digests
  (Decision 5) live only in the daemon's memory, in `PRE_WRITE_DIGESTS`, for
  at most one hour, and are no session variable.

## P1: Content-based ownership
`kind: framing`

**Goal**: ownership follows bytes. A session owns exactly the paths it
changed, no session overwrites another's dirt, commits carry only their
author's dirt or a merge's paths, writes stay inside the session's project,
and close checks only the uncommitted work live for the task it closes.

### 1.1 Retire release_task_paths, inspect_task_path_ownership and the claim scope check [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_paths.py::*` — operation: delete — scope-reason: retires release_task_paths, inspect_task_path_ownership and their private helpers with the module
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle.py::create_lifecycle_registry`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: deletes four scope-check helpers, their call in register_claim_task and their commit_guard imports
- `src/gobby/workflows/enforcement/blocking.py::TASK_MUTATION_TOOLS_BY_SERVER`
- `src/gobby/install/shared/skills/gobby/references/tasks/implementation.md`
- `docs/reference-audit/tasks.json::*` — scope-reason: drops the two retired tool rows
- `tests/mcp_proxy/tools/test_release_task_paths.py::*` — operation: delete — scope-reason: every test covers the retired tool
- `tests/mcp_proxy/tools/tasks/test_checkout_unresolved_envelopes.py::*` — scope-reason: deletes the two tests of the retired module
- `tests/mcp_proxy/tools/test_read_only_classification.py::*` — scope-reason: drops inspect_task_path_ownership from the read-only list and adds the registry assertion
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: drops the retired names from both tool lists
- `tests/mcp_proxy/tools/test_claim_task.py::*` — scope-reason: deletes the claim-scope conflict tests and their patches

**Research context:**
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_paths.py` (394 lines) defines
  `release_task_paths`, `inspect_task_path_ownership`,
  `register_release_task_paths`, `_claimed_session_worktree_path` and
  `_lifecycle_checkout_root`. The last two are used only by
  `_lifecycle_claim._claim_scope_conflicts` and the module itself, so they
  die with it.
- `_lifecycle.py::create_lifecycle_registry` imports and calls
  `register_release_task_paths`.
- `_lifecycle_claim.py`: `_claim_scope_conflicts` (lines 87-151) is called
  from `register_claim_task.claim_task` (line 283). `_declared_affected_paths`,
  `_canonical_claim_scope_path`, `_claim_scope_conflict_reason` and
  `_DECLARED_AFFECTED_FILE_SOURCES` serve only that check. The module imports
  `DirtyEditOwnershipInspectionError`, `ForeignPathOwner` and
  `foreign_owned_dirty_paths` from `commit_guard` for it.
  `_task_attribution_sessions` (lines 71-84) stays; 1.8 uses it.
- `enforcement/blocking.py::TASK_MUTATION_TOOLS_BY_SERVER` lists
  `release_task_paths` under `gobby-tasks`.
- Docs naming the tools: `references/tasks/implementation.md` (skill
  reference) and `docs/reference-audit/tasks.json` (rows near lines 468 and
  484).
- Tests: `test_read_only_classification.py:38` (`TASK_READ_ONLY_TOOLS` holds
  `inspect_task_path_ownership`), `test_task_enforcement_rules.py:61,89`
  (`INTERACTIVE_TASK_MUTATIONS`, `READ_ONLY_TASK_TOOLS`),
  `test_claim_task.py` (`test_claim_task_blocks_foreign_owner_of_declared_or_attributed_path`
  at line 180, `test_claim_task_with_empty_scope_does_not_guess_conflicts` at
  line 265, and the patches at lines 210-214).

**Implementation:**
- Delete `_lifecycle_paths.py`, and drop its import and registration from
  `create_lifecycle_registry`.
- In `_lifecycle_claim.py`, delete the four scope-check helpers, the constant,
  the call and its error branch in `claim_task`, and the three `commit_guard`
  imports.
- Remove `release_task_paths` from `TASK_MUTATION_TOOLS_BY_SERVER`.
- Remove both tools from `implementation.md` and `tasks.json`. In
  `implementation.md`, replace the release advice with: "Dirty paths you did
  not change are not yours. Ask their owner to commit with
  `gobby-agents.send_message`."
- Message texts in `commit_guard` and gate 9 that name `release_task_paths`
  change with their owners (1.3 and 1.7).

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_factory.py` — no-edit-reason: calls create_lifecycle_registry with an unchanged signature
- `tests/storage/tasks/test_closed_candidate_repair.py` — no-edit-reason: builds the registry and calls no retired tool
- `.gobby/plans/research/security-boundaries-22103/probe_control_plane.py` — no-edit-reason: prints only the sorted server keys of TASK_MUTATION_TOOLS_BY_SERVER, and removing release_task_paths leaves every server key in place

**Focused verification (planned):** run the five test files above plus
`tests/mcp_proxy/tools/tasks/test_lifecycle_registry.py` if present, with the
isolation prefix.

**Acceptance:**

- 1.1.1 - The `gobby-tasks` registry built by `create_lifecycle_registry` exposes neither `release_task_paths` nor `inspect_task_path_ownership`. test: `tests/mcp_proxy/tools/test_read_only_classification.py::test_task_registry_omits_retired_path_tools`.
- 1.1.2 - `claim_task` succeeds for a task whose declared affected files are dirty and held by another live session, and returns no scope-conflict error. test: `tests/mcp_proxy/tools/test_claim_task.py::test_claim_task_ignores_foreign_dirty_declared_paths`.
- 1.1.3 - `TASK_MUTATION_TOOLS_BY_SERVER` no longer names `release_task_paths`, and the enforcement tool lists hold neither retired name. test: `tests/workflows/test_task_enforcement_rules.py::test_retired_path_tools_absent_from_enforcement_lists`.

### 1.2 Retire capture_baseline_dirty_files and baseline_dirty_files [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_actions.py::*` — operation: delete — scope-reason: the module only registers capture_baseline_dirty_files
- `src/gobby/mcp_proxy/tools/sessions/_factory.py::create_session_messages_registry`
- `src/gobby/hooks/session_activation.py::_missing_baseline_keys`
- `src/gobby/hooks/session_activation.py::_baseline_updates`
- `src/gobby/workflows/git_utils.py::GIT_STATUS_UNAVAILABLE_MARKER`
- `src/gobby/workflows/git_utils.py::get_dirty_files_async`
- `docs/guides/variables.md`
- `docs/guides/sessions.md`
- `docs/reference-audit/sessions.json::*` — scope-reason: drops the retired tool row
- `src/gobby/install/shared/skills/gobby/references/sessions/terminals.md`
- `tests/mcp_proxy/tools/sessions/test_capture_baseline.py::*` — operation: delete — scope-reason: every test covers the retired tool
- `tests/mcp_proxy/tools/test_internal_action_tools.py::*` — scope-reason: deletes TestSessionCaptureBaselineDirtyFiles
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: drops baseline_dirty_files from the activation expectations
- `tests/mcp_proxy/services/test_direct_tool_session_activation.py::*` — scope-reason: drops the baseline key from the seeded variables
- `tests/hooks/test_stop_handoff_pending.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/hooks/test_hook_manager.py::*` — scope-reason: renames the inert capture_baseline_dirty_files label
- `tests/workflows/test_database_deadline_retry.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/workflows/test_hook_evaluation_serialization.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/workflows/test_hook_evaluation_timeout.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/workflows/test_proxy_hooks.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/workflows/test_skill_loaded_call_tool_path.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/workflows/test_turn_interrupt_stop_gates.py::*` — scope-reason: drops the inert baseline fixture key
- `tests/workflows/test_hooks.py::*` — scope-reason: drops the inert baseline fixture key

**Granularity:** one section. It has three acceptance items and one outcome:
the baseline is gone. The production files are one deleted module, one
registry line, two activation helpers and two `git_utils` symbols; the test
edits are fixture-key removals.

**Research context:**
- `mcp_proxy/tools/sessions/_actions.py` (85 lines) only registers
  `capture_baseline_dirty_files` (`register_action_tools`). `_factory.py::create_session_messages_registry`
  imports and calls it (line 103).
- `git_utils.py::GIT_STATUS_UNAVAILABLE_MARKER` and `get_dirty_files_async`
  serve only the baseline: the tool and `session_activation._baseline_updates`
  (line 411 writes `[GIT_STATUS_UNAVAILABLE_MARKER]`).
- `session_activation.py`: `_missing_baseline_keys` (lines 269-270) and
  `_baseline_updates` (lines 405-411) handle `baseline_dirty_files`. Hooks
  never sample it, and no close gate reads it. Keep both function names and
  the `baseline_dirty_tracking` invariant in `SESSION_ACTIVATION_INVARIANTS`;
  it still seeds `session_edited_files`, `active_task_id` and
  `task_edited_files`.
- Docs: `docs/guides/variables.md:118` (baseline variable row),
  `docs/guides/sessions.md:242` (tool row), `docs/reference-audit/sessions.json`,
  and `references/sessions/terminals.md:32`.
- `tests/hooks/test_hook_manager.py:1310` uses the tool name only as a label
  string in a `run_coro_blocking` timeout test; use `"example_label"`.

**Implementation:**
- Delete `_actions.py`; drop its import and call from
  `create_session_messages_registry`.
- Drop the `baseline_dirty_files` lines from `_missing_baseline_keys` and
  `_baseline_updates`, and the now-unused imports.
- Delete `GIT_STATUS_UNAVAILABLE_MARKER` and `get_dirty_files_async`.
- Remove the docs rows and the `terminals.md` advice line.
- Remove the fixture key from each listed test, and delete the baseline test
  module and class.

Consumers unchanged:
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: calls create_session_messages_registry with an unchanged signature
- `src/gobby/mcp_proxy/tools/sessions/__init__.py` — no-edit-reason: re-exports the factory unchanged
- `tests/mcp_proxy/tools/sessions/test_get_session_messages_paging.py` — no-edit-reason: exercises message tools only
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py` — no-edit-reason: asserts no retired tool name
- `tests/mcp_proxy/tools/test_tool_verbosity.py` — no-edit-reason: builds the registry and asserts no retired tool name
- `tests/sessions/bench_transcript_search_prefilter.py` — no-edit-reason: builds the registry for transcript search only
- `tests/sessions/test_handoff.py` — no-edit-reason: exercises handoff tools only
- `tests/sessions/test_handoff_found_work.py` — no-edit-reason: exercises handoff tools only
- `tests/sessions/test_machine_scoped_consumers.py` — no-edit-reason: exercises machine-scoped session tools only

**Focused verification (planned):** run the listed test files with the
isolation prefix.

**Acceptance:**

- 1.2.1 - The sessions registry built by `create_session_messages_registry` has no `capture_baseline_dirty_files` tool. test: `tests/mcp_proxy/tools/test_internal_action_tools.py::test_sessions_registry_omits_capture_baseline_dirty_files`.
- 1.2.2 - Session activation on a fresh session writes no `baseline_dirty_files` variable and does not report it missing. test: `tests/hooks/test_session_activation_reconciliation.py::test_activation_seeds_no_baseline_dirty_files`.
- 1.2.3 - `rg -n "baseline_dirty_files|capture_baseline_dirty_files|GIT_STATUS_UNAVAILABLE_MARKER" src/ docs/guides docs/reference-audit` prints nothing. test: `tests/hooks/test_session_activation_reconciliation.py::test_baseline_dirty_tracking_invariant_still_seeds_edit_ledgers`.

### 1.3 Rules 2 and 3 judge live session ownership [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/commit_guard.py::*` — scope-reason: rewrites the owner lookup, both guards and their formatters, and moves the commit-parsing half out
- `src/gobby/workflows/git_commit_parsing.py`
- `src/gobby/workflows/code_review_scope.py::*` — scope-reason: changes only the module import of the moved commit-parsing names
- `src/gobby/workflows/observer_commits.py::*` — scope-reason: changes only the module import of parse_git_commit_invocations
- `src/gobby/sessions/transcripts/tool_activity.py::*` — scope-reason: changes only the local import inside is_commit_producing
- `src/gobby/workflows/task_claim_state.py::*` — scope-reason: adds effective_tool_cwd, resolve_edit_pair and sanctioned_worktree_root beside the ledger accessors
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: the recorder resolves pairs through resolve_edit_pair, and _resolve_repo_edit_paths is deleted
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-session-foreign-dirty-edit.yaml::*` — scope-reason: description text only
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-session-foreign-staged-commit.yaml::*` — scope-reason: description text only
- `tests/workflows/test_commit_guard.py::*` — scope-reason: rewrites the ownership tests and moves the parsing tests out
- `tests/workflows/test_git_commit_parsing.py`
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: _resolve_repo_edit_paths tests move to resolve_edit_pair and lose the cwd fallback case
- `tests/workflows/test_task_claim_state.py::*` — scope-reason: adds resolve_edit_pair tests

**Granularity:** one section with more than six acceptance items, by design.
Rules 2 and 3 share the owner lookup, the formatter, the stale-holder release
and the pair resolver, and neither verifies without them. The parsing split
is mechanical and forced by the size lint.

Split `commit_guard.py` (956 lines): move the commit-parsing half, lines 45-322
(`_GIT_GLOBAL_OPTIONS_WITH_VALUE`, `GitCommitInvocation`,
`parse_git_commit_invocations`, the shell helpers `_SHELL_KEYWORDS` through
`_join_chdir`, and `resolve_commit_inspect_cwd`), into the new
`src/gobby/workflows/git_commit_parsing.py`, and move their tests into the new
`tests/workflows/test_git_commit_parsing.py`.

**Research context:**
- Today's rule 3 is `commit_guard.foreign_dirty_edit_conflict` (lines
  489-544), wired in `workflows/hooks.py::WorkflowHookHandler._evaluate_rules`
  (lines ~517-555) only when `canonical_repo_mutation` is true, and blocked by
  `block-cross-session-foreign-dirty-edit` (`before_tool`, priority 26, `when:
  foreign_dirty_edit_conflict`). It resolves paths only under `project_path`
  (`_canonical_mutation_paths`, lines 547-571).
- Today's rule 2 is `foreign_staged_commit_conflict` (lines 369-486), blocked
  by `block-cross-session-foreign-staged-commit` (`before_tool`, priority 25).
  Candidates are `git ls-files --cached --others --exclude-standard --
  <pathspecs>` for a path-scoped commit, else `git diff --cached --name-only
  --diff-filter=ACDMRTUXB`. They are intersected with owners and dirty-checked
  by `_dirty_owned_paths_releasing_clean`, which releases clean stale entries
  through `_release_clean_ledger_entries` (lines 651-682). That helper also
  calls `release_task_edited_files`.
- Owners today (`_active_path_owners`, lines 574-633): sessions holding a
  claimed open task in the project, with status in `TERMINAL_OWNER_STATUSES`
  (equal to `storage/sessions/_constants.py::LIVE_SESSION_STATUS_ORDER`), each
  read with `SessionVariableManager.get_variables`.
- Kept names and signatures, reimplemented over live ownership:
  `ForeignPathOwner` (its `owner_task_id` becomes `str | None`),
  `CheckoutPathOwnership`, `DirtyEditOwnershipInspectionError`,
  `active_owned_dirty_paths`, `foreign_owned_dirty_paths`,
  `foreign_owned_dirty_paths_async` and `inspect_checkout_path_ownership_async`.
  Their other callers stay unchanged: `sync/integrity.py::_active_bundled_content_owner_sessions`,
  `found_work_gate.py::_foreign_owned_dirty_paths` and
  `agents/worktree_checkpoint.py` (line 389).
- Refusal texts today: `_format_conflict_reason`, `_format_dirty_edit_reason`
  and `_format_unverified_dirty_edit_reason`; `_format_session_ref` renders
  `project#seq`.
- Commit-parsing importers: `code_review_scope.py:26` (`GitCommitInvocation`,
  `resolve_commit_inspect_cwd`), `observer_commits.py:9`
  (`parse_git_commit_invocations`), `sessions/transcripts/tool_activity.py:320`
  (inside `is_commit_producing`), and `tests/workflows/test_commit_guard.py`
  (around lines 33 and 1214-1605).
- `_tool.py::ToolEventHandlerMixin._resolve_repo_edit_paths(file_path, cwd, *,
  project_id)` returns `(repo_root, rel)`. It uses
  `utils/project_context.py::find_project_root` (walks up to
  `.gobby/project.json`, resolving symlinks) and `get_project_context`, falls
  back to the cwd when no root is found, and returns None for a path in
  another project. `.gobby/project.json` is tracked, and worktree creation
  ensures it (`worktrees/creation.py`), so every checkout of a project has the
  same id. The recorder reads the session's variables through
  `SessionVariableManager(db)` with `db = getattr(self._session_manager, "db",
  None)`. The recorder passes `event.cwd`, so a relative path is read against
  the session cwd even when the tool ran in another `workdir`. The root search
  starts at that cwd, and it moves to the target's own root only for an
  absolute input, so `../other-worktree/p.py` and a relative symlink leaving
  the cwd's checkout yield None.
- `commit_guard.py::_normalized_command_cwd(event, project_path)` (lines
  325-343) is the tool cwd today: the tool input's `workdir`, else `cwd`,
  joined to `event.cwd` when relative, else `event.cwd`, else
  `project_path`. Rule 2 uses it for the commit's cwd (line 399), and rule 3's
  `_canonical_mutation_paths` (lines 547-571, called only at line 508) uses it
  and keeps only paths under `project_path`. No test calls either helper.
- `hooks/_path_scope.py::current_tool_cwd` resolves a relative `workdir`
  against the daemon's own process cwd (`_resolve_base_dir` calls
  `Path.resolve`), so it is not the rule. Its callers (`_path_scope`,
  `condition_helpers_paths`, `code_navigation_recovery`) classify path scope,
  resolve no pairs, and stay unchanged.
- `_path_scope.py::_is_project_managed_path` is false for a path in another
  repository, so `canonical_repo_mutation` is false for a write into a
  sanctioned worktree of another project.
- `storage/worktrees.py::LocalWorktreeManager.list_worktrees(project_id=...,
  task_id=...)` returns `Worktree` rows with `worktree_path`, `project_id` (the
  target project) and `task_id`. `get_by_path` matches the stored string
  exactly, while `find_project_root` returns a resolved path, and macOS `/tmp`
  resolves to `/private/tmp`, so compare realpaths.
  `mcp_proxy/tools/worktrees/_create.py` lets `create_worktree` take a target
  `project_path` with a `task_id`.
- `workflows/task_dirty_state.py`: `task_dirty_paths(paths, cwd)` (sync, runs
  `git status --porcelain=v1 --untracked-files=all -- <paths>`) and
  `task_dirty_paths_async`. Both return None when git fails; untracked files
  are dirty and ignored files are absent.
- Write paths: `_normalization_canonical.py::_build_canonical_tool_metadata`
  sets `canonical_write_file_paths` only when a segment reports separate write
  targets (for example the destination of `cp`).
- `ledger_reconcile.py::session_dirty_file_set_for_checkout(variables, root)`
  returns the session's own dirty paths in that checkout. It keeps its
  signature through 1.4.

**Implementation:**
- Move the parsing half to `git_commit_parsing.py` unchanged, and point every
  importer at it. No re-export stays in `commit_guard`.
- Add `task_claim_state.sanctioned_worktree_root(db, variables, root,
  root_project_id) -> bool` (Decision 9): true when some key of
  `claimed_tasks` has a `LocalWorktreeManager(db).list_worktrees(
  project_id=root_project_id, task_id=task)` row whose `worktree_path`
  realpath equals `root`. Rule 4 (1.6) calls it too.
- Add `task_claim_state.effective_tool_cwd(event_cwd, event_data) -> str |
  None` (§Content-Hash Contract, Path resolution): the body of
  `_normalized_command_cwd` without the `project_path` fallback. A relative
  or empty `event_cwd` counts as absent, and a relative `workdir` or `cwd`
  with no base gives None. `_normalized_command_cwd` becomes
  `effective_tool_cwd(event.cwd, event.data) or project_path` and serves
  rule 2's commit cwd only.
- Add `task_claim_state.resolve_edit_pair(file_path, cwd, *, project_id, db,
  variables) -> tuple[str, str] | None`, where `cwd` is the effective tool
  cwd. It expands `~`; joins a relative path to `cwd` (None when `cwd` is
  None); resolves the result with `Path.resolve(strict=False)`; takes the
  root as `find_project_root(resolved.parent)`, the innermost root above the
  real target, for relative and absolute inputs alike; and returns
  `(realpath of root, normalized rel)`. There is no cwd fallback (Decision
  13). A root whose project id differs from `project_id` yields a pair only
  when `db` is not None and `sanctioned_worktree_root(db, variables, root,
  root_project_id)` holds. Delete `_resolve_repo_edit_paths`; the recorder
  calls `resolve_edit_pair` with `effective_tool_cwd(event.cwd, event.data)`,
  its `db` and the session's variables.
- Add `_write_paths(event_data)`: `canonical_write_file_paths` when the key is
  present, else `canonical_file_paths` or `canonical_file_path` (Decision 11).
- Add `_live_path_owners(db, *, checkout_root, paths, exclude_session_id) ->
  dict[str, tuple[ForeignPathOwner, ...]]` (W5): one query for sessions with
  status in `LIVE_SESSION_STATUS_ORDER` in any project, excluding the caller.
  For each, read its
  variables, intersect `session_dirty_file_set_for_checkout(variables, root)`
  with `paths`, and pick the task ref from the claimed task whose
  `task_edited_file_set_for_checkout(variables, task, root)` holds the path,
  else None. After 1.4 that accessor reads the pair's live tag, so at most
  one task matches. Replace `_active_path_owners` and
  `_active_foreign_path_owners`.
- Rule 3, `foreign_dirty_edit_conflict(db, event, *, session_id, project_id,
  project_path) -> str`:
  1. Return "" unless `canonical_tool_kind == "write"` (Decision 6).
  2. Resolve each write path with `resolve_edit_pair`, passing
     `effective_tool_cwd(event.cwd, event.data)`, `db` and the session's
     variables; drop paths with no pair and group the rest by root. Delete
     `_canonical_mutation_paths`.
  3. Drop pairs in the session's own `session_dirty_file_set_for_checkout`
     (no git call).
  4. If `<root>/.git` resolves to a git dir containing `MERGE_HEAD` (use
     `git rev-parse -q --verify MERGE_HEAD`), drop pairs in the merge set
     `git diff --name-only --no-renames HEAD MERGE_HEAD` (Decision 12).
  5. Run `task_dirty_paths_async(rels, root)` once per root. None means
     return the unverified text. Dirty pairs are refused: return the rule 3
     text naming each owner from `_live_path_owners`, or "no live session owns
     it".
  6. Clean pairs pass. Release each from every other live holder with
     `SessionVariableManager.release_session_dirty_files(holder, [rel],
     checkout_root=root)` (Decision 14).
- Rule 2, `foreign_staged_commit_conflict`: keep the candidate collection.
  Drop candidates in the session's own ledger, then, when MERGE_HEAD exists in
  the commit's checkout root, the merge set (Decision 1). Intersect the rest
  with `_live_path_owners`, dirty-check them, release clean stale holders, and
  refuse the dirty ones with the rule 2 text. `_release_clean_ledger_entries`
  no longer calls `release_task_edited_files`; it touches only
  `session_dirty_files`.
- Refusal texts, rendered by `_format_conflict_reason` and
  `_format_dirty_edit_reason` (one line per path; `<ref>` from
  `_format_session_ref` and `_format_ref`):
  - Rule 3: "Edit blocked: dirty path(s) were changed outside this session
    (rule 3: you may not mutate files that are dirty):" then "- <path> —
    session <ref>, task <ref>", "- <path> — session <ref>, no claimed task" or
    "- <path> — no live session owns it", then "Ask each owner with
    `gobby-agents.send_message` to commit its work; ask the user about unowned
    paths. Then retry."
  - Rule 3 unverified: keep `_format_unverified_dirty_edit_reason` unchanged.
  - Rule 2: "Commit blocked: staged path(s) hold another live session's
    uncommitted work (rule 2: do not commit files you did not create or
    mutate):" then the same per-path lines, then "Commit only your own paths
    with `git commit --only -- <paths>`, or ask each owner with
    `gobby-agents.send_message` to commit first."
- Keep both rule YAML names, keys, events, priorities and `when:` keys. Set
  the descriptions to "Rule 3: block writes to dirty paths this session does
  not own" and "Rule 2: block commits that capture another live session's
  dirty paths".

**Focused verification (planned):** run `tests/workflows/test_commit_guard.py`,
`tests/workflows/test_git_commit_parsing.py`, `tests/hooks/test_tool_handlers.py`,
`tests/workflows/test_task_claim_state.py`,
`tests/workflows/test_code_review_scope.py`, `tests/sync/test_integrity.py`
and `tests/workflows/engine/test_unavailable_tool_input_guards.py` with the
isolation prefix.

**Acceptance:**

- 1.3.1 - Rule 3 refuses a Write to a path that is dirty and absent from the session's ledger, naming the holding live session's ref and its claimed task ref; with no live holder the line says "no live session owns it". test: `tests/workflows/test_commit_guard.py::test_rule3_refuses_dirty_first_touch_naming_owner`.
- 1.3.2 - Rule 3 allows a write to a path in the session's own ledger without running git, and allows a clean path while releasing it from another live holder's `session_dirty_files`. test: `tests/workflows/test_commit_guard.py::test_rule3_allows_owned_and_clean_paths_and_releases_stale_holder`.
- 1.3.3 - Rule 3 ignores execute-kind calls (`git add`, `git restore --staged`), uses the `cp` destination and not its dirty source, and refuses as unverified when git status fails. test: `tests/workflows/test_commit_guard.py::test_rule3_checks_write_paths_of_write_kind_calls_only`.
- 1.3.4 - With MERGE_HEAD present, rule 3 allows writes to merge-set paths, including both names of a rename, and still refuses a dirty path outside the merge set; once MERGE_HEAD is gone, a dirty unowned former merge-set path is refused again. test: `tests/workflows/test_commit_guard.py::test_rule3_exempts_merge_set_paths_during_merge`.
- 1.3.5 - #23363 land 7 regression: a merge commit whose merge-set paths are dirty and held by another live session passes rule 2, while a non-merge path staged in the same commit and held by another live session is refused with the rule 2 text. test: `tests/workflows/test_commit_guard.py::test_rule2_landing_merge_passes_for_merge_set_paths`.
- 1.3.6 - Rule 2 allows the session's own dirty paths and unowned dirty paths, and refuses another live session's dirty path with no `release_task_paths` advice in the text. test: `tests/workflows/test_commit_guard.py::test_rule2_blocks_only_other_live_sessions_dirty_paths`.
- 1.3.7 - `resolve_edit_pair` returns `(root, rel)` for relative and absolute paths in any checkout of the session's project and, through `sanctioned_worktree_root`, in another project's registered worktree bound to a claimed task (reached through a symlinked temp path); it returns None for any other path of another project and for a path under no `.gobby/project.json` root. test: `tests/workflows/test_task_claim_state.py::test_resolve_edit_pair_keys_on_project_root`.
- 1.3.8 - `parse_git_commit_invocations` and `resolve_commit_inspect_cwd` import from `git_commit_parsing` and keep their behavior. test: `tests/workflows/test_git_commit_parsing.py::test_parse_git_commit_invocations_handles_chdir_and_nested_shells`.
- 1.3.9 - With the cwd in the main checkout, `resolve_edit_pair` maps `../<worktree>/p.py` and a relative symlink in the main checkout that points into the worktree to the worktree's pair, and maps a relative path to None when the cwd is None. test: `tests/workflows/test_task_claim_state.py::test_resolve_edit_pair_finds_root_from_resolved_target`.
- 1.3.10 - `effective_tool_cwd` returns an absolute `workdir`, joins a relative `workdir` or `cwd` to `event.cwd`, returns `event.cwd` without either, and returns None for a relative `workdir` without `event.cwd`. Rule 3 judges a relative write path at that cwd: with `event.cwd` in the main checkout and `workdir` in a second checkout of the project, a write to `p.py` is refused when the second checkout's `p.py` is dirty and unowned, and allowed when only the main checkout's `p.py` is. test: `tests/workflows/test_commit_guard.py::test_rule3_resolves_relative_paths_at_tool_workdir`.
### 1.4 One ownership ledger [category: code] (depends: 1.2, 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/state_manager.py::*` — scope-reason: the ledger mutators move out and change shape
- `src/gobby/workflows/edit_ledger.py`
- `src/gobby/workflows/task_claim_state.py::*` — scope-reason: every ledger accessor reads the nested shape
- `src/gobby/workflows/ledger_reconcile.py::*` — scope-reason: reconciliation and the dirty-set accessors read the nested shape
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: deletes two retired variables and updates the task_edited_files description
- `src/gobby/workflows/monolith_guard.py::outstanding_monolith_paths`
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: drops edited_at and deletes _paths_landed_before_edit
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::derive_close_transcript_evidence`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::_legacy_task_checkout_proof`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::_legacy_closed_other_tasks`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::_without_closed_task_paths`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::_same_git_checkout`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::_cleanup_closed_claim`
- `src/gobby/agents/worktree_checkpoint.py::_authorized_task_paths`
- `src/gobby/agents/task_recovery.py::TaskRecoveryHandler._clear_claim_session_variables`
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: ledger mutator tests use the nested shape
- `tests/workflows/test_task_claim_state.py::*` — scope-reason: accessor tests use the nested shape and lose the times and history cases
- `tests/workflows/test_monolith_guard.py::*` — scope-reason: adds the per-root and live-pair projection tests
- `tests/workflows/test_code_review_scope.py::*` — scope-reason: fixtures use the nested session_dirty_files
- `tests/workflows/test_hooks.py::*` — scope-reason: fixtures use the nested session_dirty_files
- `tests/workflows/test_workflow_hooks.py::*` — scope-reason: fixtures use the nested session_dirty_files
- `tests/storage/tasks/test_claim_transfer_state.py::*` — scope-reason: fixtures use the nested session_dirty_files
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: drops edited_at and the replay tests
- `tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py::*` — scope-reason: gate 12 fixtures use the nested task ledger and lose the legacy cases
- `tests/mcp_proxy/tools/tasks/test_escalation_coordinator.py::*` — scope-reason: fixtures use the nested task ledger
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: replaces the closed-task cleanup test, which pins the deleted ledger removal
- `tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py::*` — scope-reason: fixtures use the nested task ledger and lose the legacy branch
- `tests/agents/test_terminal_timeout_checkpoint.py::*` — scope-reason: fixtures use the nested task ledger

**Granularity:** one section with more than six production files and more
than six acceptance items, by design.
It has one lifecycle owner, the ledger shape, and every listed file reads the
retired shape directly; any subset leaves readers of a shape that no longer
exists.

Split `state_manager.py` (934 lines): move the ledger mutators
(`record_edited_file`, `record_edited_files`, `release_session_dirty_files`
and the module helper `_session_dirty_file_checkouts`) into the new
`src/gobby/workflows/edit_ledger.py` as `EditLedgerMixin`, which
`SessionVariableManager` inherits, and add `end_task_tags` there; delete
`release_task_edited_files`.

**Research context:**
- Today's variables: `session_dirty_files` (flat list) plus
  `session_dirty_file_checkouts`; `task_edited_files {task: [rel]}` plus
  `task_edited_file_checkouts`, `task_edited_file_times`,
  `task_edited_file_checkouts_history` and
  `task_edited_file_checkouts_history_started_at`.
- `state_manager.py`: `record_edited_files(session_id, repo_relative_paths, *,
  checkout_root=None, edited_at=None)` (lines 603-719) records into the
  session ledger, `session_edited_files` and the active task
  (`task_claim_state.active_task_id_for_edit`). `release_session_dirty_files`
  (lines 721-775) drops clean paths. `release_task_edited_files` (lines
  777-889) served `release_task_paths` and the rule 2 stale release; both
  callers are gone after 1.1 and 1.3. Mutations go through
  `SessionVariableManager._mutate_variables`.
- `task_claim_state.py` accessors: `task_edited_file_set`,
  `task_edited_file_set_for_checkout`, `task_edited_checkout_paths`,
  `task_edited_checkout_history_paths`, `other_task_edited_checkout_paths`,
  `task_edited_file_times`, `target_task_has_edits`, and `remove_claimed_task`
  (lines 118-134), which drops the task's ledger rows today.
- `ledger_reconcile.py`: `session_dirty_file_set`,
  `session_dirty_file_set_for_checkout`, `_task_ledger_paths`,
  `reconcile_edit_ledgers` (lines 116-178), and the constants
  `SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE` and `_TASK_LEDGER_VARIABLES`.
- Callers that keep working through the W1 accessors (Decision 10):
  `workflows/hooks.py` (`has_dirty_files`, `has_target_task_dirty_files`),
  `agents/run_completion.py::_agent_run_task_dirty_scope`,
  `storage/tasks/_live_session_recovery.py::recover_expired_live_session_claims`,
  `mcp_proxy/tools/tasks/_stage_review.py::submit_for_review`,
  `workflows/code_review_scope.py::inspect_commit_review_scope`,
  `sessions/handoff_summary.py::_load_session_file_paths` (reads the flat
  `session_edited_files`, unchanged), `tasks/transcript_evidence.py` (takes
  sets as parameters) and the rule `require-commit-before-status`.
- `monolith_guard.py::outstanding_monolith_paths` reads raw
  `task_edited_files` values as flat lists and projects each against the
  project root. `safe_evaluator.py::_outstanding_monolith_paths` (line 794)
  exposes it to the three rules of
  `monolith-enforcement/require-same-session-decomposition.yaml`, which block
  commits, task transitions and turn end while it is non-empty. Today the
  task ledger loses clean pairs and closed tasks, so it reports only live
  work; once the ledger is append-only (Decision 16) it would also report
  released history. Only gate 12 reads recorded pairs
  (`task_edited_checkout_paths`, `other_task_edited_checkout_paths`).
- `_tool.py::_record_successful_file_mutation` passes `edited_at`, and
  `_paths_landed_before_edit` (lines 388-417) skips a replayed envelope whose
  content already landed, using edit times.
- Gate 12 (`_close_evaluation_support.py::derive_close_transcript_evidence`,
  lines 203-377) unions `task_edited_checkout_paths` with
  `task_edited_checkout_history_paths`. When both are empty and no history
  stamp exists it calls `_legacy_task_checkout_proof`, which alone calls
  `_same_git_checkout`, and it filters by `_legacy_closed_other_tasks` and
  `_without_closed_task_paths`. `other_task_edited_checkout_paths` and
  `_closed_before_window_task_ids` stay.
- `task_claim_state.py::other_task_edited_checkout_paths(variables, task_id,
  historical_exempt_task_ids)` returns every other task's live pairs and
  skips history pairs only for exempt tasks; gate 12 passes
  `_closed_before_window_task_ids`.
  `tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py::test_close_excludes_other_task_edit_in_same_checkout`
  is parametrized over `closed_before_window` cases.
- `_lifecycle_close_finalization.py::_cleanup_closed_claim` calls
  `remove_claimed_task`, then clears `had_edits` when `commit_shas and not
  updates["task_edited_files"]` (line 528). It reads and merges the
  variables of `evaluation.edit_session_id`, the attribution owner
  (`get_claimed_session_id(task) or resolved_session_id`,
  `_lifecycle_close.py` lines 264 and 438), which differs from the closer
  `evaluation.resolved_session_id` when one session closes another's task.
  `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_closed_task_cleanup_removes_only_its_edit_entry`
  pins the removal of the closed task's `task_edited_files` row.
- `_lifecycle_claim.py::_task_attribution_sessions(ctx, task_id,
  claimed_by_session_id)` returns the given session plus every session
  linked to the task. `claim_task` (line 358) and `create_task(claim=true)`
  (`_crud.py`) link the session as `"claimed"` before setting
  `claimed_tasks`, and `observers.detect_task_claim` adds a claim only after
  one of them succeeded, so every session that can tag a pair with the task
  is linked to it. The link is best-effort (a failure is logged), the same
  dependency the 1.8 transfer has.
- `worktree_checkpoint.py::_authorized_task_paths` keeps a pre-#21897 legacy
  branch reading `legacy_variables.get("task_edited_files")` (line 514).
- `task_recovery.py::TaskRecoveryHandler._clear_claim_session_variables`'
  docstring names `task_edited_file_checkouts` (line 457).

**Implementation:**
- `session_dirty_files` becomes `{checkout_root: {rel: task_id | null}}`, the
  live set with each pair's one live task, and `task_edited_files` becomes
  the append-only history `{task_id: {checkout_root: [rel]}}`. A value of
  any other shape, including the flat list and a `{root: [rel]}` map, reads
  as empty (Decision 8).
- `EditLedgerMixin.record_edited_files(session_id, rels, *, checkout_root:
  str) -> bool`: `checkout_root` is required and `edited_at` is gone. It sets
  each pair in `session_dirty_files` to the active task (null without one),
  replacing any earlier tag, appends the pair to the active task's
  `task_edited_files` row, and adds it to `session_edited_files`.
  `release_session_dirty_files(session_id, rels, *, checkout_root: str)`
  drops pairs, tags included, from `session_dirty_files` only.
  `end_task_tags(session_id, task_id) -> int` sets every `session_dirty_files`
  pair tagged with the task to null and returns the count. It runs under
  `_mutate_variables` like the other mutators, so a concurrent record on the
  same row is never overwritten, and it never touches `task_edited_files`.
- In `task_claim_state.py`, add `task_live_checkout_paths(variables, task_id)
  -> frozenset[tuple[str, str]]`: the `session_dirty_files` pairs tagged with
  the task. History never contributes (Decision 16).
  `task_edited_file_set`, `task_edited_file_set_for_checkout` and
  `target_task_has_edits` derive from it; `task_edited_checkout_paths` returns
  every recorded pair. `other_task_edited_checkout_paths` keeps
  `historical_exempt_task_ids`: for each other task it returns the task's
  live pairs, plus its recorded pairs when the task is not exempt.
  `derive_close_transcript_evidence` keeps passing
  `_closed_before_window_task_ids`. A task closed before the window therefore
  stops suppressing a later edit to the same pair once its live tag ends,
  while live or overlapping other-task pairs still suppress it. A later
  mutation of that pair for another task retags it, so the closed task's
  history never reads as live.
  Delete `task_edited_checkout_history_paths`,
  `task_edited_file_times` and `_task_edited_file_checkouts`.
  `remove_claimed_task` no longer touches `task_edited_files` (Decision 16).
- In `ledger_reconcile.py`, both dirty-set accessors read the nested shape;
  `reconcile_edit_ledgers` releases pairs git reports clean and every pair
  under a root that no longer exists. Delete `_task_ledger_paths`,
  `SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE` and `_TASK_LEDGER_VARIABLES`.
- In the defaults YAML, delete `task_edited_file_checkouts` and
  `task_edited_file_times`, and describe `task_edited_files` as the
  append-only history `{task_id: {checkout_root: [relative paths]}}`.
- `outstanding_monolith_paths` keeps its signature and returns nothing
  without a project root. It projects the session's live task pairs, the
  `session_dirty_files` pairs with a non-null tag, each against its own root
  (Decision 16). A recorded
  pair that is no longer live is not projected, so another session's later
  growth of that file never blocks this session. An over-budget file the
  session owns stays live, because `require-monolith-resolution-before-commit`
  refuses the commit that would clean it.
- `_tool.py`: stop passing `edited_at`; delete `_paths_landed_before_edit`
  and its call. Until 1.5 lands, a replayed landed path is recorded, found
  clean by reconciliation and released, so gate 9 never sees it.
- Gate 12: `task_checkout_paths = task_edited_checkout_paths(variables,
  task_id)`; delete the history union, the `history_started_at` branch and the
  four legacy helpers.
- `_cleanup_closed_claim`: after merging `remove_claimed_task` into
  `evaluation.edit_session_id` (the attribution owner, which may differ from
  the closer `resolved_session_id`), call `end_task_tags(session_id, task_id)`
  for every session `_task_attribution_sessions(ctx, task_id,
  evaluation.edit_session_id)` returns. That covers the owner, a predecessor
  whose clean pair was never transferred (gate 9 lets that close pass, 1.7)
  and a pair under a deleted root, which `untransferred_task_pairs` skips.
  The nulled pairs stay in each session's `session_dirty_files`, so session
  ownership holds until reconciliation finds them clean, and every
  `task_edited_files` row stays. The closer's own ledger changes only when
  the closer is one of those sessions. Then clear the owner's `had_edits`
  when no task left in its `claimed_tasks` has live pairs. Other claim
  releases leave tags unchanged (Decision 8).
- Delete the legacy branch of `_authorized_task_paths`; fix the
  `task_recovery` docstring.

Consumers unchanged:
- `src/gobby/agents/run_completion.py` — no-edit-reason: reads task_edited_file_set, whose live-pair meaning is unchanged (Decision 10)
- `src/gobby/storage/tasks/_live_session_recovery.py` — no-edit-reason: reads the live-pair accessors only
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py` — no-edit-reason: reads task_edited_file_set only
- `src/gobby/sessions/handoff_summary.py` — no-edit-reason: reads the unchanged flat session_edited_files
- `src/gobby/tasks/transcript_evidence.py` — no-edit-reason: receives path sets as parameters and never reads the variables
- `src/gobby/workflows/found_work_gate.py` — no-edit-reason: calls foreign_owned_dirty_paths, whose signature 1.3 keeps
- `src/gobby/sync/integrity.py` — no-edit-reason: calls active_owned_dirty_paths, whose signature 1.3 keeps
- `src/gobby/workflows/safe_evaluator.py` — no-edit-reason: calls outstanding_monolith_paths, whose signature is unchanged
- `tests/config/test_config_runtime_config_resolution.py` — no-edit-reason: drives derive_close_transcript_evidence without ledger history fixtures
- `tests/tasks/test_close_transcript_sync.py` — no-edit-reason: drives derive_close_transcript_evidence without ledger history fixtures
- `tests/tasks/test_transcript_exclusions.py` — no-edit-reason: drives derive_close_transcript_evidence without ledger history fixtures
- `tests/agents/test_task_recovery.py` — no-edit-reason: the recovery change is a docstring only

**Focused verification (planned):** run every listed test file plus
`tests/workflows/test_commit_guard.py` and
`tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` with
the isolation prefix.

**Acceptance:**

- 1.4.1 - `record_edited_files` sets `session_dirty_files[root][rel]` to the active task (null without one, replacing an earlier tag), appends the pair to `task_edited_files[active][root]` and `session_edited_files`, and requires `checkout_root`. test: `tests/workflows/test_session_variable_manager.py::test_record_edited_files_writes_nested_ledgers`.
- 1.4.2 - `task_edited_file_set` and `target_task_has_edits` report only pairs tagged with the task, while `task_edited_checkout_paths` reports every recorded pair after reconciliation releases one. F9 sequence: session S records p under task A, commits it and reconciliation releases it, then records p under task B; A then has no live pair and B has p, although A's history still names p. test: `tests/workflows/test_task_claim_state.py::test_live_and_recorded_task_pairs_differ_after_release`.
- 1.4.3 - `remove_claimed_task` returns no `task_edited_files` or `session_dirty_files` update, so a claim released without closing keeps the task's history and its live tags. test: `tests/workflows/test_task_claim_state.py::test_remove_claimed_task_keeps_task_ledger`.
- 1.4.4 - `reconcile_edit_ledgers` releases clean pairs and every pair under a checkout root that no longer exists, and never edits `task_edited_files`. test: `tests/workflows/test_session_variable_manager.py::test_reconcile_releases_clean_and_missing_root_pairs`.
- 1.4.5 - Gate 12 credits the task's own recorded pairs without the history variables. Another task's live pairs always exclude its paths from this task's evidence, and its recorded but not live pairs exclude them unless that task is in `_closed_before_window_task_ids`. The parametrized cases are: closed before the window and released (credited), closed before the window and still live (excluded), closed before the window with its released pair re-mutated by this task, which retags it (credited), and an overlapping task (excluded). The legacy cases are deleted. test: `tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py::test_close_excludes_other_task_edit_in_same_checkout`.
- 1.4.6 - `outstanding_monolith_paths` projects a worktree pair against its own root, so an over-budget file in a task worktree is reported. test: `tests/workflows/test_monolith_guard.py::test_outstanding_monolith_paths_projects_each_pair_against_its_root`.
- 1.4.7 - Old-shape values (`session_dirty_files` as a list or as `{root: [rel]}`, `task_edited_files` with list values) read as empty, and no retired variable name remains in `src/`. test: `tests/workflows/test_task_claim_state.py::test_old_shape_ledger_values_read_as_empty`.
- 1.4.8 - `outstanding_monolith_paths` reports only live pairs: a pair whose task closed and whose live pair was released is not reported after the file grows past the ceiling while another session owns it, while an over-budget pair live for one of the session's tasks is reported. test: `tests/workflows/test_monolith_guard.py::test_outstanding_monolith_paths_ignores_released_history`.
- 1.4.9 - `end_task_tags(session_id, task_id)` sets only the pairs tagged with the task to null, keeps them in `session_dirty_files`, leaves pairs tagged with another task and every `task_edited_files` row unchanged, and returns the count; afterwards `task_live_checkout_paths` for the task is empty. test: `tests/workflows/test_session_variable_manager.py::test_end_task_tags_nulls_only_that_tasks_pairs`.
- 1.4.10 - Closer C closes a task whose attribution owner O is another session, while predecessor P, linked to the task and no longer claiming it, holds a clean untransferred pair tagged with it. `_cleanup_closed_claim` merges `remove_claimed_task` into O only, calls `end_task_tags` for O and P and never for C, and clears O's `had_edits` only when no task left in O's `claimed_tasks` has live pairs. test: `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_closed_task_cleanup_ends_task_tags_in_linked_sessions`, which replaces `test_closed_task_cleanup_removes_only_its_edit_entry`.

### 1.5 The recorder records only content-changing writes [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: the before-tool hash snapshot, the failed-call path and the recorder's content check
- `src/gobby/hooks/pre_write_digests.py`
- `src/gobby/hooks/event_handlers/_session_end.py::*` — scope-reason: handle_session_end clears the ending session's pre-write digests
- `src/gobby/hooks/_normalization_canonical.py::_classify_shell_segment_without_redirection`
- `src/gobby/hooks/_normalization_canonical.py::_merge_shell_segment_metadata`
- `src/gobby/hooks/_normalization_git_paths.py`
- `tests/hooks/test_pre_write_digests.py`
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: adds the content-check and #22642 regression tests
- `tests/hooks/test_normalization.py::*` — scope-reason: restore --staged becomes execute-kind
- `tests/hooks/test_normalization_canonical_mutation_scope.py::*` — scope-reason: test_read_only_probe_loop_is_not_an_in_project_mutation restates the recorder gate, which drops canonical_repo_mutation; it must assert that the probe loop records nothing through the hash comparison

**Granularity:** one section with more than six acceptance items, by design. The recorder is one lifecycle owner: the
before-tool snapshot, the store and its session-end cleanup, and the
after-tool comparison verify only together, and the write paths they hash are
the ones the normalization change publishes. The #22642 regression needs both
halves. The normalization split is forced by the size lint.

Split `_normalization_canonical.py` (971 lines): move the `git add`, `git
checkout` and `git restore` branches of
`_classify_shell_segment_without_redirection` (lines 644-682) into the new
`src/gobby/hooks/_normalization_git_paths.py` as
`classify_git_path_segment(parts, git_subcommand_index, cwd) ->
_ShellSegmentMetadata | None`, called from the original function.

**Research context:**
- `_tool.py::ToolEventHandlerMixin._record_successful_file_mutation` (sync,
  lines 291-386) records every path a successful edit-tool call names. It runs
  for an `EDIT_TOOLS` name or when `is_canonical_edit` holds
  (`canonical_tool_kind == "write"` and `canonical_repo_mutation` true, line
  271). After
  1.3 it resolves pairs with `task_claim_state.resolve_edit_pair`; after 1.4 it
  calls `record_edited_files(session_id, rels, checkout_root=root)`.
- `_tool.py::ToolEventHandlerMixin.handle_after_tool` (lines 223-289) calls
  the recorder only when `not is_failure and is_edit`, so a write-kind call
  that changes bytes and then fails records nothing today. One `try/except`
  around the recorder logs and allows, so an error on one path today skips
  the call's remaining paths. The recorder loop (from line 319) resolves each
  path against `event.cwd`, never the tool's `workdir`.
- `_tool.py::ToolEventHandlerMixin.handle_before_tool` (lines 54-109) reads
  `event.data` (which carries the canonical keys) and
  `_platform_session_id`, and returns `allow` at its end.
- `_session_end.py::SessionEndMixin.handle_session_end` resolves the
  platform session id from `_platform_session_id` or the external id.
- `hooks/events.py::correlate_hook_lifecycle` sets `event.request_id` from
  the first of `request_id`, `requestId`, `interaction_id`, `item_id`,
  `tool_call_id`, `toolCallId`, `tool_use_id` and `toolUseId` (and their
  case variants) in `event.data`. The Claude Code, Grok, Droid and Codex
  lifecycle adapters call it; Codex's `call_id` is not in the list, so a
  Codex tool event may carry no id.
- `task_dirty_state.py::task_dirty_paths(paths, cwd)` is the sync pathspec
  status (None when git fails; untracked dirty; ignored absent).
- `ledger_reconcile.py::session_dirty_file_set_for_checkout(variables, root)`
  gives the session's own pairs.
- The git branches today (`_normalization_canonical.py:644-682`): `git add`
  returns `_ShellSegmentMetadata("execute", paths=..., repo_mutation=True)`.
  `git checkout` and `git restore` return `"write"` with `repo_mutation=True`;
  `restore` without `--` takes its pathspecs from
  `_normalization_operands.py::_git_restore_positional_args_after`.
  `_ShellSegmentMetadata` lives in `_normalization_segments.py`.
- `_normalization_canonical.py::_merge_shell_segment_metadata` publishes as
  `write_paths` only the explicit `item.write_paths` of each segment, while
  `paths` (the write-kind `canonical_file_paths`) holds every repo-mutation
  segment's paths. `git restore`, `rm` and `mv` supply `paths` without
  `write_paths`; `cp` supplies both. So `cp a b && git restore -- c` publishes
  write paths `[b]` and drops `c`, and `git add x && git restore -- y` has no
  write-path key, so the W2 fallback names the index-only `x` too.
- `tests/hooks/test_normalization.py::TestExternalNavigationScope.test_git_restore_pathspecs_without_separator_are_write_paths`
  pins `git restore --staged src/gobby/x.py` and `git restore --staged --
  notes.md` as write-kind; both become execute-kind. `git restore -s HEAD~1
  --worktree a.md b.md` stays write-kind.
- The #22642 case: `git restore --staged tests/e2e/test_terminal_client_stack.py`
  in a checkout where that path was staged before the claim; the path flipped
  from staged to unstaged and was recorded as an edit.

**Implementation:**
- `classify_git_path_segment` holds the moved branches unchanged, except
  `git restore` with `--staged`/`-S` and without `--worktree`/`-W` returns
  `"execute"` with its paths and `repo_mutation=True`, like `git add`
  (Decision 7).
- `_merge_shell_segment_metadata` builds `write_paths` as the union of the
  write targets of write-kind segments: a segment's explicit `write_paths`
  when it has them (keeping W2's `cp` destination-only behavior), else its
  resolved mutation paths, including loop-bound ones. Execute-kind segments,
  `git add` and index-only `git restore` among them, contribute none. The
  function's other outputs are unchanged.
- New `hooks/pre_write_digests.py`, class `PreWriteDigests`. It holds
  `dict[tuple[str, str, str], tuple[str | None, float]]` (key `(session_id,
  request_id or "", realpath)`, value `(sha256 hex or None,
  time.monotonic())`) behind a `threading.Lock`. Its API:
  - `file_digest(path) -> str | None | Literal["dir"]` streams sha256 over the
    bytes, returning None when the path is absent and `"dir"` for a
    directory.
  - `snapshot(session_id, request_id, paths)` stores a digest for each path
    except directories, and first drops every entry older than 3600 seconds.
    A path whose `file_digest` raises `OSError` is logged and gets no entry;
    the other paths are still stored.
  - `take(session_id, request_id, path) -> tuple[str | None] | None` pops the
    entry and returns a one-tuple holding the stored digest, or None when
    there is no entry.
  - `clear_session(session_id)` drops all of that session's entries.
- Both hooks pass `event.request_id`, which `correlate_hook_lifecycle` has
  already filled.
- The module holds one daemon-wide instance, `PRE_WRITE_DIGESTS =
  PreWriteDigests()`, which `_tool.py` and `_session_end.py` import. The
  daemon runs one hook handler, so no constructor wiring is needed.
- `handle_before_tool`: when the session id is known and the call is
  write-kind (`canonical_tool_kind == "write"` or an `EDIT_TOOLS` name),
  resolve the W2 write paths at `task_claim_state.effective_tool_cwd(event.cwd,
  event.data)` with symlinks resolved (§Content-Hash Contract, Path
  resolution) and call `snapshot` just before the final `allow`. Snapshot
  errors are logged and never block.
- `handle_after_tool` runs the recorder for every write-kind call, including
  failed ones; the outcome no longer gates it.
- The recorder records only when `canonical_tool_kind == "write"` (or an
  `EDIT_TOOLS` name), over the W2 write paths (Decision 11) that
  `resolve_edit_pair` maps to a pair (Decision 6) at
  `effective_tool_cwd(event.cwd, event.data)`. `is_canonical_edit` in
  `_tool.py` drops its `canonical_repo_mutation` condition, so a write into a
  sanctioned worktree of another project reaches the recorder. For each
  resolved pair (Decision 5):
  - With an entry from `take`, record the pair when `file_digest` now differs
    from the stored digest. Equal digests record nothing, including for a path
    the session already owns, so a no-op under another active task never
    retags the pair or enters that task's history. The comparison does not consult git, so a path
    committed by the same call is still recorded.
  - With no entry, or a directory, batch the pair per root into one
    `task_dirty_paths(rels, root)`: dirty records the pair, clean records
    nothing, and None records it (§Content-Hash Contract).
  - Each pair's `file_digest` call catches `OSError`, logs it, and sends that
    pair to the status batch; the loop continues with the next pair
    (§Content-Hash Contract, Hash read failure).
- `SessionEndMixin.handle_session_end` calls
  `PRE_WRITE_DIGESTS.clear_session(session_id)` once the session id is
  resolved.
- `_mark_session_had_edits_if_claimed` runs only when at least one pair was
  recorded. `_notify_code_index` keeps its current trigger.


**Focused verification (planned):** run `tests/hooks/test_tool_handlers.py`,
`tests/hooks/test_pre_write_digests.py` and `tests/hooks/test_normalization.py`
with the isolation prefix.

**Acceptance:**

- 1.5.1 - #22642 regression: in a temporary git checkout where a tracked file is staged, a Bash `git restore --staged <path>` call records no pair in `session_dirty_files`, `task_edited_files` or `session_edited_files`. test: `tests/hooks/test_tool_handlers.py::test_git_restore_staged_records_no_attribution`.
- 1.5.2 - A Write or Edit that leaves a clean path's bytes unchanged records nothing, and one that changes them records the pair. test: `tests/hooks/test_tool_handlers.py::test_recorder_records_only_content_changes`.
- 1.5.3 - A Write creating a new untracked file records it. A path the session owns under task A, rewritten with the same bytes while task B is active, records nothing in B's ledger, while a real change records it for B. test: `tests/hooks/test_tool_handlers.py::test_recorder_handles_untracked_and_owned_paths`.
- 1.5.4 - `git restore --staged` and `git restore -S` without `--worktree`/`-W` normalize to execute-kind; `git restore --staged --worktree` and `git restore -W` stay write-kind. test: `tests/hooks/test_normalization.py::TestExternalNavigationScope.test_git_restore_pathspecs_without_separator_are_write_paths`.
- 1.5.5 - Mixed shell segments publish `canonical_write_file_paths` as the union of write-kind targets: `cp a b && git restore -- c` gives `[b, c]`, `mv a b && echo x > d` and `rm e && echo x > d` include `d` with the `mv` or `rm` paths, and `git add x && git restore -- y` and `git restore --staged x && echo z > y` give `[y]` only. test: `tests/hooks/test_normalization.py::test_mixed_segments_publish_union_of_write_kind_targets`.
- 1.5.6 - In a temporary git checkout, a Bash `git add <foreign path> && git restore -- <own path>` call, where `<own path>` is already in the session's ledger and the restore changes it, records `<own path>` and nothing for the `git add` path. test: `tests/hooks/test_tool_handlers.py::test_recorder_ignores_execute_segment_paths_in_mixed_command`.
- 1.5.7 - A failed Bash call `echo changed > <path> && false` records the pair, and a failed call that leaves the bytes unchanged records nothing. test: `tests/hooks/test_tool_handlers.py::test_failed_write_call_records_only_changed_bytes`.
- 1.5.8 - With MERGE_HEAD present, a same-bytes Write to a dirty merge-set path held by another session records nothing for the writer. test: `tests/hooks/test_tool_handlers.py::test_merge_set_noop_write_records_nothing`.
- 1.5.9 - A Bash call that writes a clean tracked path and commits it (`echo x > <path> && git commit -qam m`) records the pair in `task_edited_files`, and `reconcile_edit_ledgers` then releases its clean live pair. test: `tests/hooks/test_tool_handlers.py::test_write_committed_in_same_call_keeps_task_record`.
- 1.5.10 - With no stored entry, the status rule decides (dirty records, clean records nothing), and a directory path uses the same rule. test: `tests/hooks/test_tool_handlers.py::test_recorder_falls_back_to_status_without_entry`.
- 1.5.11 - Interleaved `before(A)`, `before(B)`, `after(A)`, `after(B)` on one path with distinct request ids keep separate baselines. With no request id, the second `before` overwrites the first, and the second `after` takes the status fallback. test: `tests/hooks/test_pre_write_digests.py::test_interleaved_calls_keep_baselines_by_request_id`.
- 1.5.12 - `handle_session_end` drops the session's entries and keeps other sessions' entries, and `snapshot` drops entries older than one hour. test: `tests/hooks/test_pre_write_digests.py::test_session_end_and_age_prune_drop_stale_entries`.
- 1.5.13 - A Bash write to the relative path `p.py` with `event.cwd` in the main checkout and `workdir` in a second checkout of the project hashes and records the second checkout's pair and records nothing for the main checkout's `p.py`. test: `tests/hooks/test_tool_handlers.py::test_recorder_resolves_relative_paths_at_tool_workdir`.
- 1.5.14 - A Bash call that changes two paths and makes the first unreadable (`chmod 000`) records both pairs, the first through the status fallback, and an unreadable path at `before_tool` stores no entry while the call's other paths keep theirs. test: `tests/hooks/test_tool_handlers.py::test_unreadable_path_takes_status_fallback_without_skipping_others`.

### 1.6 Rule 4 refuses writes into another Gobby project [category: code] (depends: 1.3, 1.5, 1.7)
`kind: deliverable`

Targets:
- `src/gobby/workflows/hooks.py::WorkflowHookHandler._evaluate_rules`
- `src/gobby/workflows/ownership_eval_context.py`
- `src/gobby/workflows/engine/core.py::*` — scope-reason: moves the eval_context default block out of RuleEngine.evaluate
- `src/gobby/workflows/engine/eval_context_defaults.py`
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-project-write.yaml`
- `tests/workflows/test_ownership_eval_context.py`
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: adds the rule 4 template test

Split `src/gobby/workflows/hooks.py` (852 lines): move the ownership eval-context wiring
in `WorkflowHookHandler._evaluate_rules` (lines 517-582: the rule 2 and 3
calls, the review-scope keys and the landing-merge key) into the new
`src/gobby/workflows/ownership_eval_context.py` as
`build_ownership_eval_context`, which also computes rule 4.

Split `src/gobby/workflows/engine/core.py` (948 lines): move the `eval_context.setdefault`
block of `RuleEngine.evaluate` (lines 318-332) into the new
`src/gobby/workflows/engine/eval_context_defaults.py` as
`apply_eval_context_defaults(eval_context, *, blocking_deadline)`, adding
`cross_project_write_conflict` with default "".

**Research context:**
- Nothing enforces rule 4 today. `_path_scope.py::_is_project_managed_path`
  returns `project_root is None`, so `canonical_repo_mutation` is False for a
  write into another repo; rule 4 therefore gates on `canonical_tool_kind ==
  "write"` and its paths, never on `canonical_repo_mutation`.
- An undefined name in a rule `when:` raises inside
  `TemplatingMixin._evaluate_condition`, and a `block` effect then fails
  closed, so the new key needs a default for every `RuleEngine.evaluate`
  caller, not only the hooks path.
- `utils/project_context.py::find_project_root(cwd)` resolves and walks up to
  `.gobby/project.json`; `get_project_context(root)` returns `id`, `name` and
  `project_path`.
- `storage/worktrees.py::LocalWorktreeManager.list_worktrees(project_id=...,
  task_id=...)` returns `Worktree` rows with `worktree_path`, `project_id` (the
  target project) and `task_id`. `get_by_path` matches the stored string
  exactly, while `find_project_root` returns a resolved path, and macOS `/tmp`
  resolves to `/private/tmp`, so compare realpaths.
- Memory a524ed54: cross-repo work uses `gobby-worktrees.create_worktree`
  with the target `project_path`, a writable `worktree_path` and the task id,
  and commits there.
- The session's claimed tasks are the keys of the `claimed_tasks` variable.
  After 1.3, `task_claim_state.sanctioned_worktree_root(db, variables, root,
  root_project_id)` is the registered-worktree predicate, and
  `resolve_edit_pair` already returns pairs under a sanctioned root. After
  1.5 the recorder records them, and after 1.7 gate 9 checks them per root.
- `_evaluate_rules` calls `foreign_dirty_edit_conflict` only when
  `canonical_repo_mutation` is true and canonical paths exist (`hooks.py`
  line 545), so today rule 3 never sees a sanctioned worktree write.
- `hooks.py:408` says "Mirrors the baseline_dirty_files pattern below"; that
  pattern is gone after 1.2.
- Rule template shape (from `block-cross-session-foreign-staged-commit.yaml`):
  `tags`, then `rules.<name>` with `description`, `event: before_tool`,
  `enabled: true`, `priority`, `when: <key>` and `effects: [{type: block,
  reason: "{{ <key> }}"}]`.

**Implementation:**
- `build_ownership_eval_context(db, event, event_data, variables, *,
  session_id, project_path) -> dict[str, Any]` returns the keys the moved block
  set today plus `cross_project_write_conflict`. `_evaluate_rules` calls it.
  It calls `foreign_dirty_edit_conflict` for every write-kind `before_tool`
  event, without today's `canonical_repo_mutation` precondition; rule 3
  resolves its own pairs (Decision 6).
- `cross_project_write_conflict(db, event_data, variables, *, project_id,
  cwd) -> str`, for `before_tool` with `canonical_tool_kind == "write"`:
  1. Resolve each W2 write path (Decision 11) as §Content-Hash Contract, Path
     resolution says: `build_ownership_eval_context` passes `cwd =
     effective_tool_cwd(event.cwd, event_data)`, a relative path joins it,
     and the result is resolved with `Path.resolve(strict=False)`. A relative
     path with `cwd` None is skipped.
  2. `root = find_project_root(path.parent)` on the resolved path; no root
     means allowed (not Gobby-managed).
  3. `other = get_project_context(root)["id"]`; equal to the session's
     `project_id` means allowed.
  4. Allowed when `task_claim_state.sanctioned_worktree_root(db, variables,
     root, other)` holds (Decision 9). Rules 1 to 3 still apply to that write.
  5. Otherwise return: "Write blocked: <path> is in project <name> (<id>), not
     this session's project <name> (rule 4: do not create or mutate files in
     another Gobby-managed repo). Work there from a session in that project,
     or through a worktree of that project registered to a task you have
     claimed."
- New `block-cross-project-write.yaml`: rule `block-cross-project-write`,
  description "Rule 4: block writes into another Gobby-managed project",
  `event: before_tool`, `priority: 24`, `when: cross_project_write_conflict`,
  block reason `"{{ cross_project_write_conflict }}"`.
- Fix the `hooks.py:408` comment to describe the variable-defaults lazy init
  on its own.

Consumers unchanged:
- `tests/mcp_proxy/services/test_tool_proxy_validation.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/servers/test_mcp_routes.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_block_tools_after_handoff_compact.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_call_tool_provider_shapes.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_claim_reconciliation.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_code_review_freshness.py` — no-edit-reason: calls is_foreign_landing_merge directly, which is unchanged
- `tests/workflows/test_evaluation_runtime.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_plan_mode_delivery.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_plan_mode_rules.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys
- `tests/workflows/test_tool_context_rehydration.py` — no-edit-reason: drives _evaluate_rules, whose moved wiring returns the same keys

**Focused verification (planned):** run
`tests/workflows/test_ownership_eval_context.py`,
`tests/workflows/test_task_enforcement_rules.py`, `tests/workflows/test_hooks.py`
and `tests/workflows/engine/test_unavailable_tool_input_guards.py` with the
isolation prefix.

**Acceptance:**

- 1.6.1 - A Write whose path lies in a checkout of a different registered project returns the rule 4 text naming both projects, while writes into the session's own main checkout, its task worktree and a path under no `.gobby/project.json` root are allowed. test: `tests/workflows/test_ownership_eval_context.py::test_rule4_refuses_other_project_and_allows_own_and_unmanaged`.
- 1.6.2 - End to end through a registered worktree of another project bound to a claimed task, reached through a symlinked temp path: rule 4 allows writes there, yet rule 3 refuses a write to a dirty path the session does not own; a clean path's write passes, the recorder records the pair under the worktree's real root, and gate 9 blocks the close until that pair is committed. test: `tests/workflows/test_ownership_eval_context.py::test_sanctioned_worktree_write_obeys_rules_1_to_3`.
- 1.6.3 - `RuleEngine.evaluate` with an eval_context lacking `cross_project_write_conflict` evaluates the rule as not matching instead of failing closed. test: `tests/workflows/test_ownership_eval_context.py::test_rule4_default_key_keeps_unwired_callers_open`.
- 1.6.4 - The bundled `block-cross-project-write` template loads with event `before_tool`, priority 24 and a block effect carrying the key. test: `tests/workflows/test_task_enforcement_rules.py::test_block_cross_project_write_template_shape`.
- 1.6.5 - Rule 4 judges the file the tool names: with `event.cwd` in the session's checkout, a write to `p.py` with `workdir` in another project's checkout, a write to `../<other project>/p.py`, and a write through a relative symlink in the session's checkout that points into the other project are each refused, while the same relative write with `workdir` in the session's own task worktree is allowed. test: `tests/workflows/test_ownership_eval_context.py::test_rule4_resolves_workdir_relative_and_symlinked_paths`.

### 1.7 Rule 1: gate 9 checks the task's live pairs per checkout [category: code] (depends: 1.4, 1.8)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py::evaluate_task_clean_proof`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py::apply_task_cleanliness_gate`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py::validate_uncommitted_task_edits`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::CloseAttributionSnapshot`
- `src/gobby/mcp_proxy/tools/tasks/_close_evaluation_support.py::CloseEvaluationFingerprint.capture`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::capture_attribution`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::commit_close`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py::_linked_commit_clean_proof_paths`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_evaluate_close`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_acceptance_root_diagnostic`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_is_deliberate_close`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::_apply_escalated_close_gate`
- `src/gobby/mcp_proxy/tools/tasks/_close_gate_helpers.py`
- `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py::*` — scope-reason: the gate 9 message changes
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::*` — scope-reason: gate 9 reads live and untransferred pairs per checkout
- `tests/mcp_proxy/tools/tasks/test_close_attribution.py::*` — scope-reason: attribution carries live pairs
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py::*` — scope-reason: snapshot fixtures use live_pairs
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: snapshot fixtures use live_pairs

**Granularity:** one section. Gate 9 is one lifecycle owner: the snapshot
fields, the proof, the gate and the commit-close recheck change together, and
the helper move is forced by the size lint. It follows 1.8 because it reads
`untransferred_task_pairs`.

Split `_lifecycle_close.py` (910 lines): move `_acceptance_root_diagnostic`,
`_is_deliberate_close` and `_apply_escalated_close_gate` (lines 96-152, used
only inside the file) into the new
`src/gobby/mcp_proxy/tools/tasks/_close_gate_helpers.py`.

**Research context:**
- `_lifecycle_close.py::_evaluate_close` calls `apply_task_cleanliness_gate(ctx,
  evaluation, edited_paths=attribution.clean_proof_paths, owner_session_id,
  project_id, repo_path)` (lines 529-536).
- `_lifecycle_validation.py::evaluate_task_clean_proof(ctx, *, edited_paths,
  repo_path)` returns `TaskCleanProof(status, dirty_paths, reason)` from one
  `task_dirty_paths_async(paths, repo_path)`; it is also called by
  `_lifecycle_close_finalization.commit_close` (line 357, with
  `fresh_attribution.clean_proof_paths`).
- `apply_task_cleanliness_gate` passes dirty paths to
  `validate_uncommitted_task_edits`, which calls `foreign_owned_dirty_paths`
  (line 191) to annotate owners and fails with "Task-attributed files still
  have uncommitted changes: ... Commit them, or ask the owner to commit or
  release_task_paths, and retry." (line 220).
  `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py::test_uncommitted_task_edits_names_dirty_paths`
  pins that text.
- `_close_evaluation_support.py::CloseAttributionSnapshot` carries
  `clean_proof_paths`, which feeds `CloseEvaluationFingerprint.capture`.
- `_lifecycle_close_finalization.py::capture_attribution` derives live paths
  from the flat `task_edited_file_set` against `repo_path` only. When that set
  is empty it falls back to linked-commit paths, filtered by
  `_linked_commit_clean_proof_paths`, the second foreign-owner lookup.
- After 1.4, `task_claim_state.task_live_checkout_paths(variables, task_id)`
  returns the task's live `(root, rel)` pairs.
- After 1.8, a claim whose pair transfer failed still succeeds, and the
  predecessor keeps the task's live pairs until a `claim_task` retry moves
  them. `_lifecycle_claim.untransferred_task_pairs(ctx, *, task_id,
  claimant_session_id)` returns those pairs by predecessor ref, read-only.
  `capture_attribution` (line 135) reads only the claimant's variables, so
  without them gate 9 would pass over the predecessor's dirt.

**Implementation:**
- `CloseAttributionSnapshot.clean_proof_paths` becomes `live_pairs:
  frozenset[tuple[str, str]]`, from
  `task_live_checkout_paths(owner_variables, task_id)`, and the snapshot
  gains `untransferred_pairs: frozenset[tuple[str, str, str]]` (predecessor
  ref, root, rel), from `untransferred_task_pairs(ctx, task_id=task_id,
  claimant_session_id=owner_session_id)`. The fingerprint captures both.
- `capture_attribution` drops the linked-commit fallback for gate 9 and deletes
  `_linked_commit_clean_proof_paths`. Committed paths are clean, so the
  fallback never proved anything gate 9 needs.
- `evaluate_task_clean_proof(ctx, *, live_pairs)` groups the pairs by root and
  runs `task_dirty_paths_async(rels, root)` per root. Any None means
  `unavailable`. Dirty results are reported as `<root>/<rel>`.
- `apply_task_cleanliness_gate(ctx, evaluation, *, live_pairs,
  untransferred_pairs)` and `validate_uncommitted_task_edits(dirty_paths,
  untransferred_dirty)` drop `owner_session_id`, `project_id`, `repo_path`
  and the owner lookup. One `evaluate_task_clean_proof` runs over the live
  pairs and the untransferred pairs together. The live pairs are the
  attribution owner's (`evaluation.edit_session_id`), which is not the
  closer when one session closes another's claimed task, so neither text
  calls them the closer's. The failure text for a dirty live pair is: "Files
  created or mutated for the task are uncommitted: <root>/<rel>, ... Commit
  them and retry." For a dirty untransferred pair it is: "Files a previous
  session created or mutated for the task were not transferred to the task's
  claimant: <root>/<rel> (session <ref>), ... Call claim_task again from the
  claimant to transfer them, commit them, and retry." Both texts appear when
  both apply. Clean untransferred pairs pass, and the close then ends their
  tag (1.4, `_cleanup_closed_claim`).
- `commit_close` and `_evaluate_close` pass both fields.
- Move the three helpers to `_close_gate_helpers.py` and import them back.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_tool.py` — no-edit-reason: calls _evaluate_close with an unchanged signature
- `tests/tasks/test_close_checklist.py` — no-edit-reason: drives _evaluate_close without snapshot fixtures
- `tests/config/test_config_runtime_config_resolution.py` — no-edit-reason: drives the fingerprint without snapshot fixtures

**Focused verification (planned):** run the five listed test files plus
`tests/tasks/test_close_checklist.py` with the isolation prefix.

**Acceptance:**

- 1.7.1 - Gate 9 fails with "Files created or mutated for the task are uncommitted: <root>/<rel> ... Commit them and retry." for a live pair, with no owner lookup and no `release_task_paths` advice. test: `tests/mcp_proxy/tools/tasks/test_mcp_close_checklist.py::test_uncommitted_task_edits_names_dirty_paths`.
- 1.7.2 - Gate 9 checks pairs in a task worktree and in the main checkout against their own roots, and passes when the task's live pairs are clean even though another session's dirty path shares a relative name. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate9_checks_live_pairs_per_checkout_root`.
- 1.7.3 - The #22642 shape closes: a session whose only touch was `git restore --staged` on a pre-staged path has no live pairs, so gate 9 passes. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate9_passes_when_index_only_touch_recorded_nothing`.
- 1.7.4 - Git status failure for any root reports `task_clean_proof_unavailable`, and `CloseEvaluationFingerprint.capture` changes when the live pairs change. test: `tests/mcp_proxy/tools/tasks/test_close_attribution.py::test_live_pairs_feed_fingerprint_and_unavailable_proof`.
- 1.7.5 - After a claim whose pair transfer failed, gate 9 refuses the close with the untransferred-pair text naming the ended predecessor's dirty pair; a `claim_task` retry moves the pair, the next close fails with the live-pair text, and the close passes once the pair is committed. A live predecessor that still claims the task, a clean untransferred pair, and a pair under a deleted checkout root never block. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate9_refuses_close_until_failed_transfer_is_retried`.

### 1.8 Claim transfers a predecessor's live pairs [category: code] (depends: 1.1, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: adds the transfer helper and its calls after a successful claim and on the already-claimed return
- `src/gobby/workflows/edit_ledger.py`
- `src/gobby/workflows/task_claim_state.py::*` — scope-reason: adds the releases_task_ownership predicate shared by the transfer and the close check
- `src/gobby/storage/hub/protocol.py::*` — scope-reason: adds the SessionVariablePairMutation lock target and its __all__ entry
- `src/gobby/storage/hub/postgres_pool.py::*` — scope-reason: imports the new lock target and adds its advisory_lock_keys branch
- `tests/mcp_proxy/tools/test_claim_task.py::*` — scope-reason: adds the transfer tests
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: adds the transfer transaction tests
- `tests/storage/test_manager_surface_parity.py::*` — scope-reason: adds the pair lock key test

**Research context:**
- `_lifecycle_claim.py::register_claim_task.claim_task` links the session
  (`link_task(..., "claimed")`, line 358) and merges `add_claimed_task`
  (task_claim_state, sets `claimed_tasks[task] = ref` and `active_task_id`)
  into the claimant's variables (line 383).
- `_task_attribution_sessions(ctx, task_id, claimed_by_session_id)` (lines
  71-84) returns the prior claimer plus every session linked to the task
  through `session_task_manager.get_task_sessions`.
- A session is live when its status is in
  `storage/sessions/_constants.py::LIVE_SESSION_STATUS_ORDER`.
- After 1.4, `task_claim_state.task_live_checkout_paths(variables, task_id)`
  gives a session's live pairs for the task, and
  `SessionVariableManager.release_session_dirty_files(session_id, rels, *,
  checkout_root)` drops pairs from `session_dirty_files`. Both ledgers live in
  `edit_ledger.py` (`EditLedgerMixin`).
- `SessionVariableManager._mutate_variables` serializes one row through
  `db.transaction_immediate(SessionVariableMutation(session_id=...))`, an
  advisory transaction lock, and reads the row with a plain `SELECT`.
  `storage/hub/postgres_pool.py::_acquire_lock` raises
  `LockAcquisitionOrderError` unless each nested lock has a strictly greater
  `PRIORITY`, and `SessionVariableMutation.PRIORITY` is 950, so one
  transaction cannot take two `SessionVariableMutation` locks. A
  `SELECT ... FOR UPDATE` does not exclude `_mutate_variables`, whose plain
  read does not block. `advisory_lock_keys` maps `SessionVariableMutation` to
  its fallback key `"<module>.<qualname>:<repr>"`. The payload codec is
  `state_manager._decode_variables_payload` and `_encode_variables_payload`.
  No Rust crate writes `session_variables`.
- `_lifecycle_claim.py::register_claim_task.claim_task` returns
  `already_claimed: True` early (line 249) when
  `tasks/state_semantics.py::get_claimed_session_id(task)` is the caller, so a
  retry never reaches the code after the merge.
- `tests/workflows/test_session_variable_manager.py` runs on the real test
  hub (`db` fixture over `temp_db`) and already holds threaded concurrency
  tests (`test_claim_set_variable_values_serializes_concurrent_claims`).
  `tests/storage/test_manager_surface_parity.py` tests `advisory_lock_keys`.
- The close path reads the owner's variables with
  `ctx.session_var_manager.get_variables`
  (`_lifecycle_close_finalization.py::capture_attribution`), and task tools
  read a session row with `ctx.session_manager.get(session_id)`
  (`_close_evaluation_support.py` line 251).

**Implementation:**
- Add `SessionVariablePairMutation(first_session_id, second_session_id)` to
  `storage/hub/protocol.py` (frozen dataclass, `PRIORITY` 950, listed in
  `__all__`). Its `advisory_lock_keys` branch returns
  `advisory_lock_keys(SessionVariableMutation(session_id=sid))` for each id
  in sorted order. The keys are the exact single-row keys, so every
  `_mutate_variables` call on either row waits for the pair, and two pair
  transactions always lock in the same order.
- Add `task_claim_state.releases_task_ownership(variables, task_id, *, live:
  bool) -> bool`: true when the session is not live or its `claimed_tasks`
  no longer holds the task. It is the one predecessor eligibility rule for
  the transfer and for `untransferred_task_pairs`.
- Add `EditLedgerMixin.transfer_task_pairs(task_id, *, from_session_id,
  to_session_id) -> int`. One `self.db.transaction_immediate(
  SessionVariablePairMutation(...))` transaction does all of the following;
  the codec is imported inside the method, because `state_manager` imports
  `edit_ledger`:
  1. Read both variable rows, the predecessor's `sessions.status`, and the
     task row.
  2. Recheck eligibility under the lock: `get_claimed_session_id` of the task
     row is `to_session_id`, the claimant's `claimed_tasks` holds the task,
     and `releases_task_ownership(predecessor variables, task_id, live=...)`
     holds. If any check fails, return 0 with nothing written.
  3. Take the predecessor's pairs tagged with the task
     (`task_live_checkout_paths`); pairs tagged with any other task stay.
     Set each in the claimant's `session_dirty_files` with the task's tag,
     append it to the claimant's `task_edited_files[task_id]`, and remove it,
     tag included, from the predecessor's `session_dirty_files`. Leave the claimant's
     `session_edited_files` (it did not edit them) and the predecessor's
     `task_edited_files` (gate 12, Decision 16) unchanged.
  4. Write both changed rows and return the number of pairs moved.
  Any exception rolls back both rows.
- Add `_transfer_task_dirty_pairs(ctx, *, task_id, claimant_session_id,
  prior_claimer) -> list[str]`. It calls `transfer_task_pairs` for each
  session from `_task_attribution_sessions` other than the claimant, and
  returns one `"<session ref>: <error>"` entry per failed transfer. A
  completed transfer leaves the predecessor no live pairs, so a repeat is a
  no-op.
- Add `untransferred_task_pairs(ctx, *, task_id, claimant_session_id) ->
  dict[str, frozenset[tuple[str, str]]]`, read-only, for gate 9 (1.7). For
  each session from `_task_attribution_sessions` other than the claimant, it
  reads the status with `ctx.session_manager.get` (live when the status is in
  `LIVE_SESSION_STATUS_ORDER`) and the variables with
  `ctx.session_var_manager.get_variables`. When `releases_task_ownership`
  holds, it keeps the session's `task_live_checkout_paths(variables,
  task_id)` whose root directory still exists, keyed by the session's ref. A
  live predecessor that still claims the task contributes nothing, and a
  pair under a deleted checkout can never be committed or reconciled by its
  ended holder, so it is dropped.
- `claim_task` calls `_transfer_task_dirty_pairs` after the claimant's
  variables merge. Before the `already_claimed` early return, it calls
  `_transfer_task_dirty_pairs` again, which retries any
  transfer that failed earlier. A non-empty list goes into the result as
  `pair_transfer_errors`. The claim itself still succeeds. While errors
  remain, the `already_claimed` message says to call `claim_task` again to
  retry the transfer, instead of "do not call claim_task again". Until the
  retry succeeds, gate 9 refuses the close while any untransferred pair is
  dirty (1.7), so a failed transfer never lets the task close over the
  predecessor's uncommitted work.
- A live predecessor that still claims the task keeps its pairs (Decision 2).
  An ended predecessor is never named as an owner, because owners are live
  sessions only (Decision 15).

**Focused verification (planned):** run `tests/mcp_proxy/tools/test_claim_task.py`,
`tests/workflows/test_session_variable_manager.py` and
`tests/storage/test_manager_surface_parity.py` with the isolation prefix.

**Acceptance:**

- 1.8.1 - Claiming a task whose ended predecessor holds live pairs for it moves those pairs into the claimant's `session_dirty_files` and `task_edited_files[task]` and removes them from the predecessor's `session_dirty_files`, keeping the predecessor's task ledger. test: `tests/mcp_proxy/tools/test_claim_task.py::test_claim_transfers_ended_predecessor_live_pairs`.
- 1.8.2 - A live predecessor that still claims the task keeps its pairs, and a live predecessor that released the claim hands them over, so no pair is held by two live sessions. test: `tests/mcp_proxy/tools/test_claim_task.py::test_claim_transfer_never_duplicates_live_ownership`.
- 1.8.3 - `transfer_task_pairs` moves the pairs in one transaction and leaves the claimant's `session_edited_files` unchanged. A failure injected after the claimant's row is written and before the predecessor's row is written rolls both rows back, leaving exactly one live holder, and a second call completes the move. test: `tests/workflows/test_session_variable_manager.py::test_transfer_task_pairs_rolls_back_on_failure`.
- 1.8.4 - While one thread holds a `SessionVariablePairMutation` transaction open, another thread's `_mutate_variables` on either row waits until it commits and then sees the transferred ledger. test: `tests/workflows/test_session_variable_manager.py::test_single_row_mutation_waits_on_pair_lock`.
- 1.8.5 - `advisory_lock_keys(SessionVariablePairMutation(b, a))` equals the `SessionVariableMutation` keys of `a` then `b`, and taking a `SessionVariableMutation` lock after the pair lock raises `LockAcquisitionOrderError`. test: `tests/storage/test_manager_surface_parity.py::test_session_variable_pair_lock_keys_match_single_row_keys_in_sorted_order`.
- 1.8.6 - When a claim's transfer fails, the result carries `pair_transfer_errors` and its message invites a retry; a later `claim_task` by the same session takes the `already_claimed` return, completes the transfer, and reports no `pair_transfer_errors`. test: `tests/mcp_proxy/tools/test_claim_task.py::test_already_claimed_retry_completes_failed_pair_transfer`.
- 1.8.7 - F9 regression: session S mutates p under task A and q under A, releases A without closing (both stay tagged A), then mutates p for task B (p retags to B); in a variant, S commits p under A, reconciliation releases it, and S mutates p again for B. When another session claims A, only q transfers, p stays with S tagged B, `untransferred_task_pairs` for A never names p, and A's close is not blocked by p while S's close of B still is. test: `tests/mcp_proxy/tools/test_claim_task.py::test_claim_transfers_only_pairs_live_for_the_claimed_task`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by Plan Writer 3 (gobby#15468) under the
  Orchestrator's rulings R1-R3 (2026-10-05 13:03 CT) and its accepted writer
  decisions. Facts were verified read-only on `0.5.0` at `a78a5e9982`. The
  Orchestrator accepted W3 (Decision 12) the same day, bounded to the merge
  set while `MERGE_HEAD` exists, and flagged it for Josh alongside Decision 1.
  The draft is narrative only, with no M1.
- 2026-10-05: Applied the four plan-enhancer-taskless-old suggestions, all
  accepted by the Orchestrator. E1: gate 12 keeps the closed-before-window
  exemption (1.4). E2: shell write paths are the union of write-kind segment
  targets (1.5). E3: claim transfer is one pair-locked transaction with an
  in-lock recheck and an idempotent retry (option (a), 1.8). E4: rules 1 to 3
  apply under a sanctioned worktree of another project (1.3, 1.5, 1.6, with
  1.6 now depending on 1.5 and 1.7). The sweep also corrected the Content-Hash
  Contract's W2 reference from Decision 12 to Decision 11.
- 2026-10-05: Adversary findings CO-A3-F1 to F4 accepted. F4 adds the
  security-boundaries probe to 1.1's unchanged consumers. F1 to F3 replace
  Decision 5 (Orchestrator ruling, 2026-10-05). The recorder now compares a
  sha256 of each write path taken at `before_tool` with one taken at
  `after_tool`, for failed calls too. Entries are keyed by session,
  normalized `request_id` and path, are cleared at session end and expire
  after one hour, and the status rule remains a bounded fallback (1.5).
- 2026-10-05: Adversary findings CO-A3-F5 to F8 accepted, with the
  Constraints wording fix. F5: `outstanding_monolith_paths` projects live
  pairs only (1.4, Decision 16). F6: one effective tool cwd and
  target-based root discovery for the snapshot, the recorder and rules 3
  and 4 (Content-Hash Contract, 1.3, 1.5, 1.6). F7: a hash read error sends
  that path to the status fallback without skipping the others (1.5). F8:
  gate 9 refuses a close while a failed claim transfer leaves a dirty
  untransferred pair, so 1.7 now depends on 1.8 (1.7, 1.8, Decision 2). The
  Constraints bullet separates persisted session variables from the
  in-memory digest store.
- 2026-10-05: Adversary finding CO-A3-F9 accepted under the Orchestrator's
  ruling (live association and history stay distinct; the task-tag shape and
  the non-close-release reading confirmed). `session_dirty_files` becomes
  `{root: {rel: task_id | null}}`; a mutation for B retags the pair to B; the
  tag ends at reconciliation, retag, transfer or task close; history never
  revives a live pair (Decisions 2, 8, 10, 15, 16; 1.4, 1.5, 1.8). New
  regressions 1.4.2's F9 sequence, a 1.4.5 retag case and 1.8.7. Prose:
  1.8 names `_transfer_task_dirty_pairs`, and Path resolution covers an
  absolute tool-input `cwd`.
- 2026-10-05: CO-A3-F9 close branches, from the Adversary's recheck of
  `b4798c53c2`. `_cleanup_closed_claim` ends the closed task's tags through
  the new row-locked `EditLedgerMixin.end_task_tags`. It runs for the
  attribution owner `evaluation.edit_session_id`, which may differ from the
  closer, and for every other session `_task_attribution_sessions` links to
  the task, so a clean untransferred predecessor pair (which F8 lets close)
  does not stay live after the close. Nulled pairs keep session ownership,
  and history is untouched (Decision 8, 1.4). 1.4.3 now pins that a
  non-close release keeps tags. New regressions: 1.4.9 for the mutator and
  1.4.10 for the cleanup with a different closer, owner and predecessor.
  The sweep for the same closer-versus-owner class reworded the Overview,
  the gate 9 retirement bullet, the 1.7 title, 1.7.2 and both gate 9 texts,
  which no longer call the owner's pairs the closer's. 1.4 also replaces
  `test_closed_task_cleanup_removes_only_its_edit_entry`, which pinned the
  deleted removal of a closed task's rows.

## V2: Verification
`kind: verification`

After every leaf has landed:

1. Run the focused suites of 1.1 to 1.8 together against the test hub.
2. `rg -n "release_task_paths|inspect_task_path_ownership|capture_baseline_dirty_files|baseline_dirty_files|task_edited_file_times|task_edited_file_checkouts|session_dirty_file_checkouts|release_task_edited_files" src/ docs/guides docs/reference-audit`
   prints nothing.
3. After the Orchestrator's daemon restart syncs the templates, the installed
   rows `block-cross-session-foreign-dirty-edit`,
   `block-cross-session-foreign-staged-commit` and `block-cross-project-write`
   are enabled with priorities 26, 25 and 24.
4. In a scratch checkout of this project, a session's `git restore --staged`
   on a pre-staged path records nothing, and a later `close_task` passes gate
   9 (#22642 shape, live).
