Plan artifact: `.gobby/plans/content-ownership-rules.md`

# Content-based ownership rules for close gates and edit hooks

**Plan ID:** content-ownership-rules

## Overview
`kind: framing`

This plan specifies #22644 (Replace edit attribution with four content-based
ownership rules for close gates and edit hooks), under epic #22949 (Lane 7,
planning and research). Its implementation leaves sit under epic #23561. It
enforces Josh's four ownership rules (§Ownership Contract).

**Refresh (#25261, 2026-10-28).** Josh approved the V1 plan on 2026-10-05.
Sections 1.1 to 1.3 have landed (#23562, #23563 and #23564 are closed). Two
later changes overtook the open sections 1.4 to 1.8 (#23565 to #23569):
- The v4 close rewrite (epic #23561) replaced the close checklist with data
  invariants and step records in `src/gobby/tasks/close_invariants.py`. V1's
  gate 9 is now gate 3, `uncommitted_task_edits`. Gate 12, the transcript
  evidence it read, `_lifecycle_validation.py`, `_close_evaluation_support.py`,
  `_close_gate_helpers.py` and `_stage_review.py` are gone.
- Content-hash ownership (#24402, #24426, #24590, #25161) made per-task
  content records the ownership authority (§Content-Hash Contract). Josh's
  ruling of 2026-10-26 sets its premise: "There shouldn't be orphans unless
  the file is modified outside of gobby".

This refresh re-targets 1.4 to 1.8 against both models. It records the
rulings #25261 asks for in the Decision Record: the #23565 storage layout
(Decision 8), and keep or retire for #23566 (Decision 20), #23567
(Decision 21) and #23569 (Decision 2). Decision 22 re-targets #23568.

**What remains broken at `b42c2172d9`:**
- Gate 3 reads only the attribution owner's session variables
  (`_close_context.capture_attribution`). Uncommitted work that a previous
  session recorded for the task never blocks the close. #24402's foreign-owner
  exclusion also subtracts a live predecessor's dirt for the same task (1.7).
- Rule 2 refuses a claimant's commit of a live predecessor's dirt for the
  claimed task, although rule 3 already treats that predecessor as the
  claimant's past self. Once gate 3 requires that commit, the two would
  deadlock (1.8).
- A commit without a pathspec records the whole shared index, so it can
  record another live session's staged path that rule 2 never checked
  (Decision 23, 1.8).
- The recorder records every path a successful write call names, whatever
  happened to its bytes. It records nothing for a failed call that changed
  bytes (1.5).
- Mixed shell commands publish too few write paths. `cp a b && git restore --
  c` publishes `[b]` and `rm e && echo x > d` publishes `[d]`, so rule 3 never
  checks `c` or `e` before the command discards or deletes another session's
  dirt (1.5).
- Nothing enforces rule 4 (1.6).
- The ledger still writes three variables that v4 left without readers, and
  five accessors serve no live caller (1.4).

**When the leaves close:**
- Gate 3 checks the attribution owner's live task pairs plus every pair any
  session recorded for the task whose current content the task still owns,
  or that was recorded without a content record, each against its own
  checkout. #24402's exclusion of later foreign dirt
  still holds for paths the task no longer owns (1.7).
- A claimant may commit dirt whose live holder works a task the claimant now
  claims (rule 2), as rule 3 already lets it edit that dirt (1.8).
- While another live session holds dirt in a checkout, a commit there must
  name its paths (`git commit -- <path>...`), so it never records that
  session's staged paths (1.8).
- A write-kind call records a path only when the call changed its bytes,
  whether the call succeeded or failed (1.5).
- A shell command publishes the union of its write-kind segments' targets, so
  rule 3 and the recorder judge every path the command writes (1.5).
- A write into another Gobby-managed project is refused (rule 4), except
  through a registered worktree of that project bound to a task the session
  claims (1.6).
- The ledger keeps only state that something reads (1.4).

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
| 1 | `close_task` gate 3 (`uncommitted_task_edits`) | none (close invariant) | 1.7 |
| 2 | `before_tool` | `block-cross-session-foreign-staged-commit` (priority 25) | 1.3, 1.8 |
| 3 | `before_tool` | `block-cross-session-foreign-dirty-edit` (priority 26) | 1.3, 1.5 |
| 4 | `before_tool` | `block-cross-project-write` (priority 24, new) | 1.6 |

## Content-Hash Contract
`kind: framing`

This section answers #22644 criterion 2. Two content mechanisms work
together. The first is delivered; 1.5 builds the second.

- **Ownership records (delivered by #24402 to #25161).** Every recorded edit
  stores, under the task it is attributed to, the path's fingerprint in
  `task_edited_checkout_hashes` and the edit time in
  `task_edited_checkout_times`. Both are keyed
  `{task_id: {checkout_root: {rel: value}}}`. The fingerprint is
  `workflows/checkout_fingerprint.py::file_fingerprint`: git's blob hash of
  the bytes (a symlink's target text), None for an absent path, and `OSError`
  for a directory. The records survive close (`remove_claimed_task` leaves
  them). `dirty_edit_grants.checkout_path_owners` reads every session's
  records for a checkout. A record counts while the path's current
  fingerprint equals it, and the newest counting record names the owning
  task. A path written outside Gobby after its last record has no owner.
  Bash calls in a registered linked worktree record the paths whose
  fingerprints changed across the call (`worktree_shell_attribution`), and a
  Bash call that rewrites a path a claimed task owns re-records it
  (`SessionVariableManager.snapshot_claimed_shell_edits` and
  `adopt_claimed_shell_edits`).
- **Mutation test for named write paths (1.5, Decision 5).** The bullets
  below specify it.

- **Which tools.** Every tool call that normalization classifies as
  `canonical_tool_kind == "write"`, over the write paths that
  `resolve_edit_pair` maps to a pair (Decision 6): Write, Edit, MultiEdit,
  NotebookEdit, apply_patch, and shell segments that name their write targets
  (redirection, `cp`/`mv` destinations, `rm` operands, `git checkout --` and
  `git restore` with a worktree effect, inline Python writes). The paths are
  `canonical_write_file_paths` when that key is present, else
  `canonical_file_paths` (Decision 11). After 1.5, a compound shell command's
  `canonical_write_file_paths` holds every write-kind segment's targets.
  Execute-kind calls and segments, including `git add` and `git restore
  --staged` without `--worktree`/`-W` (Decision 7), record nothing and are not
  checked by rule 3.
- **Path resolution (delivered in 1.3).** The before-tool snapshot, the
  recorder, rule 3 and rule 4 resolve a write path the same way. A relative
  path joins the effective tool cwd, `task_claim_state.effective_tool_cwd(
  event.cwd, event.data)`: the tool input's `workdir`, else its `cwd`, joined
  to `event.cwd` when relative, else `event.cwd`. Symlinks are then resolved,
  and the checkout root is the innermost `.gobby/project.json` root above the
  resolved target, whether the input was relative or absolute. So
  `../other-worktree/p.py` and a relative symlink that leaves the cwd's
  checkout reach the checkout that holds the real file. With no `event.cwd`
  and no absolute `workdir` or `cwd` in the tool input, a relative path has no
  pair at any of the four sites; absolute paths resolve unchanged.
- **Where the pre-call identity is taken.** At `before_tool`,
  `ToolEventHandlerMixin.handle_before_tool` stores the `file_fingerprint` of
  each resolved write path. Entries live in the daemon-wide in-memory store
  `pre_write_digests.PRE_WRITE_DIGESTS`, keyed by `(session_id, request_id,
  realpath)`. `request_id` is the event's correlation id, which
  `hooks/events.py::correlate_hook_lifecycle` fills from `tool_use_id`,
  `toolCallId`, `tool_call_id` and the other provider keys, so overlapping
  calls on one path keep separate baselines. When an adapter supplies no id,
  `request_id` is "", and a later `before_tool` on the same session and path
  overwrites the entry. Separately, rule 3 runs one pathspec-limited `git
  status --porcelain=v1 --untracked-files=all -- <path>` per write path the
  session does not already own, to refuse a path dirty before first touch.
- **Where the post-call comparison runs.** At `after_tool`, for every
  write-kind call whether it succeeded or failed, the recorder takes each
  path's entry and fingerprints the path again. A different fingerprint,
  including a created or deleted file, means mutation, so the pair is
  recorded. An equal fingerprint records nothing, even for a path the session
  already owns or a merge-set path. A pair recorded while clean, because the
  same call committed it, keeps its task records; reconciliation drops it
  from the session's dirty set.
- **No entry: bounded fallback.** A path has no entry after a daemon restart
  during the call, a `before_tool` event that never reached the daemon, an
  id-less overlapping call whose entry the other call took, or an entry older
  than one hour. For those paths and for directories, which have no
  fingerprint, the post-call pathspec status decides: dirty records the pair,
  clean records nothing. This status is no proof of mutation. In these
  branches only, a no-op on an already-dirty path is recorded (the F2 shape),
  and a write committed by the same call is missed (the F3 shape). The policy
  chooses to record when in doubt, so rule 1 never misses dirt the session
  may have made; a false record costs a commit of the session's own paths,
  which rule 2 allows. A pair whose path cannot be fingerprinted when it is
  recorded (an unreadable file, or a directory) gets no content record, and
  the task's older record of it in that session is dropped; gate 3 still
  finds it through the task's live interval (Decision 22).
- **Fingerprint read failure.** Each path is observed on its own. A path
  whose before-call fingerprint raises `OSError` stores no entry. A path
  whose after-call fingerprint raises `OSError` (for example, the call made
  it unreadable) takes the no-entry fallback. Either way the other paths of
  the call are still fingerprinted and compared. Failures that are not
  observation failures, such as the ledger write, still reach
  `handle_after_tool`'s catch, which logs them.
- **Entry lifetime.** `after_tool` removes the entries it reads. A session's
  remaining entries are dropped at its `session_end`, and any entry older
  than one hour is dropped whenever a new one is stored, so entries whose
  `after_tool` never fires do not accumulate.
- **Untracked paths.** A file the call creates changes from absent to
  present, so it is recorded (creation is mutation). A pre-existing untracked
  file the session does not own is dirty at first touch, so rule 3 refuses
  the write.
- **Ignored paths.** Git reports nothing for them, so rule 3 never refuses
  them. A changed ignored path is recorded, and gate 3 never blocks on it
  because git reports it clean. Rule 1 covers git-tracked files only.
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
  gate 3 checks the pair.
- **Git status unavailable.** At `before_tool`, rule 3 refuses as
  unverified. At `after_tool` the fingerprint comparison needs no git. In the
  no-entry fallback, a failed status records the pair, and ledger
  reconciliation releases it once git reports it clean.
- **Node-origin events.** The hub's disk is not a node's checkout, so a
  node-origin call stores no snapshot and every pair takes the no-entry
  status, run on the node through `checkout_reads.run_git`. A read the node
  cannot answer records the pair. The pair gets no content record, and
  `checkout_path_owners` answers nothing for a node-origin event, so rules 2
  and 3 find it through the live interval. Gate 3 on the hub never judges
  it: while the recording session's dirty ledger holds it, gate 3 fails
  unavailable (Decision 4).
- **Accepted race.** Another process can change the path between the two
  fingerprints, and the recorder then records that change. Overlapping calls
  by one session on one path each compare against their own baseline, so the
  later call also records a change the earlier one made; both records belong
  to the same session. Two simultaneous clean first touches by different
  sessions both record.

## Decision Record
`kind: framing`

Decisions 1, 3, 7, 9 and 11 to 19 shipped with 1.1 to 1.3, except where a
refreshed decision says otherwise. Decisions 2, 5, 6, 8, 10, 15 and 16 were
rewritten, and 20 to 23 added, by the #25261 refresh (2026-10-28). The
refreshed rulings take effect with the Adversary's stamp and Josh's approval
through the Assistant (#25261 criterion 3).

1. **R1, rule 2 scope (Orchestrator gobby#14972, 2026-10-05 13:03 CT). This
   interprets Josh's verbatim rule 2; Josh approved it on 2026-10-05 at
   15:54 CT (Telegram, relayed by the Orchestrator).** Rule 2
   blocks a commit only when a committed path is dirty and is held in another
   live session's dirty ledger. Paths no live session mutated commit
   freely. A merge commit is exempt only for the paths the merge brings in,
   `git diff --name-only --no-renames HEAD MERGE_HEAD`; `--no-renames` lists
   both sides of a rename. Every other staged path in a merge commit still
   follows rule 2. #23363 land 7 passes: its six paths are exactly the
   files that differ between HEAD `b3a65779db` and MERGE_HEAD `7094fc272b`.
2. **#23569 ruling: claim transfer is retired, and #23569 is kept for rule 2's
   claimed-task exclusion (refresh, 2026-10-28).** V1 moved a predecessor's
   live pairs into the claimant's ledger under a pair lock. Content ownership
   made the move unnecessary: `checkout_path_owners` names the task as a
   path's owner from any session's records, so ownership follows the task
   without moving rows. Rule 3 already exempts a holder whose task the
   session now claims (`commit_guard.foreign_dirty_edit_conflict`, "past
   self"). Retired with the transfer, none of them ever built:
   `SessionVariablePairMutation`, `transfer_task_pairs`,
   `_transfer_task_dirty_pairs`, `untransferred_task_pairs`,
   `pair_transfer_errors`, `releases_task_ownership`, `task_tag_holders` and
   `end_task_tags`. One gap remains, so #23569 is kept with a new scope.
   Rule 2 (`foreign_staged_commit_conflict`) still refuses a claimant's
   commit of dirt a live holder keeps for the claimed task. Once 1.7 makes
   gate 3 require that commit, the claimant could neither commit nor close
   while the predecessor lives, for example a seat that escalated the task
   and waits. 1.8 gives rule 2 rule 3's past-self exclusion, and widens the
   holder's task for both rules from its claims to its live interval, so a
   pair with no content record that an escalated holder keeps still names the
   task (Decision 15). Rejected: retiring #23569 outright, which leaves that
   deadlock.
3. **R3, retirements (Orchestrator).** Retire `_claim_scope_conflicts`,
   `inspect_task_path_ownership` and `release_task_paths`. Every rule 2 and
   rule 3 refusal names the owning session ref and task ref.
4. **Node-origin events take the node's status (refreshed; Orchestrator
   ORCH-REV-CO-3, 2026-10-28).** #23277 (Hook envelope carries edited-file
   content) is partly delivered. `hooks/checkout_reads.py` reads a node's
   checkout over `checkout.read` (#24522), node-origin git runs on the node
   (#24523), `task_claim_state._resolve_node_edit_pair` takes a node event's
   pair from node facts, and `commit_guard._live_path_owners` counts only
   holders on the event's machine (#24529). The hub's disk is not the node's
   checkout, so fingerprinting it before and after a node call would compare
   two reads of the wrong file and silently drop the node's edit. Rule: for a
   node-origin event (`checkout_reads.node_origin()`), `handle_before_tool`
   stores no snapshot, so the recorder decides every pair by the no-entry
   status rule, and that status runs through `checkout_reads.run_git` on the
   node (1.5). A read the node cannot answer raises `CheckoutReadUnavailable`
   and records the pair, as a failed status does. A node-origin pair gets no
   content record (1.7). Content ownership is machine-local:
   `checkout_path_owners` reads only the records of sessions on this machine
   and answers nothing for a node-origin event, so the hub's bytes never name
   a node pair's owner, and rules 2 and 3 find a node holder's task through
   its live interval (Decision 15). Close gates run outside any hook
   delivery, and #23277 covers hook sites only, so gate 3's git runs on the
   hub and cannot judge a pair that a session on another machine recorded
   (Adversary CO-A5-F2, 2026-10-28). Such a remote pair never reaches the
   hub's digests, its status or the linked-commit fallback. While the
   recording session's dirty ledger holds the pair, gate 3 fails "Git could
   not inspect all task-attributed files."; once that session's own ledger
   reconcile releases it, the pair adds nothing. That reconcile is a node
   read: `daemon_git.status` runs on the node under `node_origin()`
   (hook-envelope-file-facts.md, row 2). Ceiling (Orchestrator ruling,
   2026-10-28): nothing reconciles an ended session's ledger, so a pair an
   ended node session still holds keeps the task's gate 3 unavailable until
   restraint-deferral #25284 (Node-backed close read releases an ended node
   session's held content-ownership pairs, after #24521) lands. Every #23277
   leaf that edits `commit_guard.py`, `task_claim_state.py` or `_tool.py` is
   closed (#24522, #24523, #24529). The open leaves #24521 (node
   `checkout.read` handler, crates only) and #24532 (live proof, an e2e test)
   share no file with this plan, so no section depends on them. Rejected: a
   dependency on #24521, because the status rule already records when the
   node cannot answer; failing closed on every remote pair, because
   `task_edited_file_checkouts` keeps a pair until close, so no node-touched
   task could ever close; and a node-read close route in this plan, which
   #25284 owns.
5. **Mutation detection fingerprints the named path before and after the
   call (Orchestrator, 2026-10-05: replacement accepted; refreshed).** At
   `before_tool` the handler stores each write path's fingerprint in memory;
   at `after_tool` the recorder fingerprints it again, and only a different
   fingerprint records. This follows Josh's definition literally. The first
   draft's rule (rule 3's clean first touch proves the HEAD blob, then a
   post-call pathspec status decides) was replaced after the Adversary showed
   three misses: a write in a call that then fails, a no-op write to an
   already-dirty path (owned under another task, or in the merge set) and a
   write committed by the same call. Entries are keyed by session, the
   event's normalized `request_id` (from `correlate_hook_lifecycle`; "" when
   the adapter supplies none) and path, so no adapter reads its own call id.
   A call with no entry, after a daemon restart for example, falls back to
   the post-call status rule, so no durable store is needed. That fallback
   and directories keep the old rule's F2 and F3 shapes, bounded as the
   Content-Hash Contract states. The refresh replaces V1's sha256 with
   `checkout_fingerprint.file_fingerprint`, which the ownership records
   already store, so a recorded hash and the mutation test agree. Rejected in
   the refresh: keeping the snapshot in session variables, as
   `claimed_shell_snapshots` does, which adds a row write to every write-kind
   `before_tool`; the status fallback already covers a restart.
6. **Write-kind calls only (accepted; refreshed).** The recorder and rule 3
   apply only to `canonical_tool_kind == "write"`, over the write paths that
   `resolve_edit_pair` maps to a pair. Rule 3 already dropped its
   `canonical_repo_mutation` gate (1.3). The recorder still requires it
   (`_tool.py` `is_canonical_edit`), and 1.5 drops it there. Normalization has
   no claim state, so `canonical_repo_mutation` is false for a sanctioned
   worktree of another project (Decision 9). It is also false for a separate
   clone of this project (same `.gobby/project.json` id, different git
   repository), which rules 1 to 3 cover too, because `resolve_edit_pair`
   keys on the project id.
7. **`git restore --staged` is execute-kind (accepted).** Without
   `--worktree`/`-W` it changes only the index. It is classified
   `"execute"`, like `git add`
   (`hooks/_normalization_git.py::classify_git_mutation`). This is the #22642
   command.
8. **#23565 ruling: the storage layout (refresh, 2026-10-28).** The ledger
   keeps these variables, and only these, for edits:
   - Ownership authority: `task_edited_checkout_hashes` and
     `task_edited_checkout_times`, `{task_id: {checkout_root: {rel: value}}}`
     in every session's variables. They survive close and drive
     `checkout_path_owners` (§Content-Hash Contract).
   - Live session dirt: `session_dirty_file_checkouts`
     `{checkout_root: [rel]}`, with `session_dirty_files` as its flat union,
     which `has_dirty_files` reads. Reconciliation and rule 3's stale-holder
     release drop pairs from both.
   - Live task interval: `task_edited_file_checkouts`
     `{task_id: {checkout_root: [rel]}}`, where a later task's edit of a pair
     takes it (`transfer_live_checkout_paths`), and `task_live_edit_starts`.
     `remove_claimed_task` drops a finished task's entries.
   - Session edit lists: `session_edited_files` and `task_edited_files`
     `{task_id: [rel]}`. They list what the session edited and carry no
     ownership. They serve the readers that ask whether a task has edits:
     `target_task_has_edits`, `has_target_task_dirty_files`,
     `outstanding_monolith_paths`, agent run completion and step records.
   - Retired by 1.4: `task_edited_file_checkouts_history`,
     `task_edited_file_checkouts_history_started_at` and
     `task_edited_file_times`. Their readers were gate 12, the claim window
     and five accessors that have served no live caller since v4.
   A path stored without a checkout reads as nothing (AGENTS.md rule 10), so
   1.4 deletes the two readers that compare `task_edited_files` with checkout
   pairs. Rejected: V1's tagged `session_dirty_files: {root: {rel: task |
   null}}` with an append-only nested `task_edited_files`, which would rebuild
   per-task ownership under a second key beside the content records and drop
   the hashes Josh's 2026-10-26 ruling relies on. Rejected: folding the flat
   lists into the checkout maps, which rewrites fixtures in about 40 test
   files while no reader of the flat lists misbehaves;
   `release_session_dirty_files` keeps `session_dirty_files` equal to the
   union of the checkout lists.
9. **Rule 4 compares project ids (accepted).** The project id of the path's
   `.gobby/project.json` root must equal the session's project id. The single
   exception is a registered worktree of the other project whose `task_id` is
   one of the session's claimed tasks. The exception does not waive rules 1
   to 3 (Orchestrator, 2026-10-05: enhancer E4 accepted). The predicate
   `task_claim_state.sanctioned_worktree_root` serves both rule 4 and
   `resolve_edit_pair`, so the worktree's pairs are checked, recorded and
   gated like any own-project pair.
10. **W1, accessor meaning (refreshed).** `task_edited_file_set`,
    `task_edited_file_set_for_checkout`, `task_live_checkout_files` and
    `target_task_has_edits` keep their meanings and callers.
    `task_edited_checkout_paths` returns the task's live checkout pairs
    (`task_edited_file_checkouts`) once 1.4 drops the history union. Its two
    callers, `_claim_release._uncommitted_refusal` and
    `_close_context.capture_attribution`, thereby stop judging pairs that a
    later task took.
11. **W2, write paths.** Rules 3 and 4 and the recorder read
    `canonical_write_file_paths` when the key is present, else
    `canonical_file_paths`. A dirty `cp` source is therefore not refused.
    For a shell command, `canonical_write_file_paths` is the union of the
    write targets of its write-kind segments: a segment's explicit write paths
    (the `cp` destination), else its paths. Execute-kind segments (`git add`,
    index-only `git restore`) contribute none. 1.5 delivers the union.
12. **W3, merges and rule 3 (Orchestrator, 2026-10-05: accepted). This
    interprets Josh's verbatim rule 3; Josh approved it with Decision 1 on
    2026-10-05 at 15:54 CT (Telegram, relayed by the Orchestrator).** While
    `MERGE_HEAD` exists in a checkout, writes to paths in
    that checkout's merge set (`git diff --name-only --no-renames HEAD
    MERGE_HEAD`, Decision 1) are exempt from rule 3, so the Merge Manager can
    resolve conflicts with edit tools. The recorder then records them for the
    writing session, which lets its merge commit pass rule 2 for its own
    edits. The exemption ends when `MERGE_HEAD` is gone and covers no path
    outside the merge set. Rejected alternative: conflict resolution only
    through shell commands that name no path.
13. **Paths outside every Gobby checkout are ignored.** `resolve_edit_pair`
    has no cwd fallback: with no `.gobby/project.json` root there is no pair.
    Such a path is in no Gobby-managed repo, so rules 1 to 3 cannot apply to
    it, and git status at a non-repository cwd could only fail. The root is
    found from the resolved target, never from the cwd (§Content-Hash
    Contract, Path resolution).
14. **W4, stale holders.** At rule 3's clean first touch, the pair is released
    from every other live holder's dirty ledger. Otherwise a holder whose
    ledger was not reconciled after its commit would duplicate ownership.
15. **W5, owners (refreshed).** Owners are all live sessions (status in
    `LIVE_SESSION_STATUS_ORDER`) whose `session_dirty_file_checkouts` holds the
    path under that checkout root, not only sessions with a claimed open task.
    The lookup has no project filter, because a sanctioned worktree's holders
    belong to another project (Decision 9) and ledger keys are already
    realpath roots. `ForeignPathOwner.owner_task_id` is the holder's task
    whose live interval (`task_live_edit_starts`) covers the path, over every
    task the holder has an interval for, claimed or released. Escalation
    releases the claim and keeps the interval (`release_claimed_task`).
    Without an interval, it is the holder's task whose
    `task_edited_file_checkouts` holds the pair. `ledger_task_id` is the
    content-hash owner (`checkout_path_owners`): the task whose newest content
    record, in any session on this machine, matches the path's current bytes.
    A pair with no content record (an unreadable file, a directory or a
    node-origin write) has none, and neither does any holder during a
    node-origin event (Decision 4). Rules 2 and 3 exempt a holder when either field names a task
    the session claims (1.8; Orchestrator ORCH-REV-CO-1 and CO-2,
    2026-10-28). The task ref is the claim's ref, or `#<seq_num>` read from
    `tasks` for a released task.
16. **Edit history retired with gate 12 (refresh, 2026-10-28).** V1 kept an
    append-only per-task history to exclude other tasks' paths from gate 12's
    evidence. v4 deleted gate 12 and the transcript evidence, so the history
    variables have no purpose, and 1.4 retires them. The content records
    (Decision 8) are the only per-task record that outlives a pair's live
    interval, and they serve ownership.
17. **The rule 2 user-directed exception needs no mechanism.** An ended owner
    is not live, a live owner acts on the user's direction, and the user's own
    terminal commits bypass agent hooks.
18. **Refusal events.** Rules 2, 3 and 4 block at `before_tool` through rule
    templates whose `block` effect carries the computed reason. Rule 1 stays a
    close invariant (gate 3).
19. **Rejected: a claim-time scope check.** `_claim_scope_conflicts` refused a
    claim whose declared or attributed paths another session owned. Rule 3
    already refuses the first write to such a path with the owner named, so
    the check duplicates it and blocks claims that would never touch the path.
20. **#23566 ruling: kept and re-targeted (refresh, 2026-10-28).** Four
    defects remain. A no-op write records its path. A failed call that changed
    bytes records nothing. Mixed shell commands under-report their write
    paths. The recorder's `canonical_repo_mutation` condition keeps a write
    into a sanctioned worktree of another project unrecorded. A no-op write
    also moves a pair's live interval and newest content record to the
    writing task, so task A's uncommitted edit escapes A's gate 3 once task B
    rewrote the same bytes. Delivered elsewhere and dropped from 1.5: the
    index-only `git restore` classification (Decision 7) and rule 3's
    write-kind gate. 1.5 keeps Decision 5's mechanism and drops V1's
    `_normalization_canonical.py` split, because write paths now merge in
    `hooks/_normalization_metadata.py::_merge_shell_segment_metadata` (507
    lines). Rejected: retiring #23566 because worktree Bash calls already
    record by fingerprint. Structured edits and main-checkout Bash calls still
    record by name.
21. **#23567 ruling: kept (refresh, 2026-10-28).** Nothing enforces rule 4.
    `hooks/_path_scope.py::_is_project_managed_path` is false for another
    project's checkout, so `canonical_repo_mutation` is false there, and no
    rule template judges the write. 1.6 keeps V1's design. The rule 3 wiring
    precondition it removed is already gone, and the size splits of
    `workflows/hooks.py` (909 lines) and `workflows/engine/core.py` (953)
    remain.
22. **#23568: gate 3 reads the task's content-owned pairs in every session
    (refresh, 2026-10-28).** Gate 3's attributed set is the attribution
    owner's live task pairs plus every pair any session recorded for the task
    in `task_edited_checkout_hashes` whose current content the task owns
    (`checkout_path_owners`). #24402's exclusion of paths a later live session
    dirtied still applies, except to paths whose content the closing task
    owns. A recorded pair under a root whose directory is gone is dropped from
    the cross-session set, because nothing there can be committed (precedent:
    the Orchestrator's ruling on CR7 LOW-2, 2026-10-05). The owner's own pairs
    keep `tasks/attributed_roots.py::is_ignorable_attributed_root`, which
    stays fail-closed for a registered root. A pair recorded without a
    content record, because its path could not be fingerprinted (an
    unreadable file, or a directory), is found from the task's live interval
    in every session (`task_edited_file_checkouts`). It counts unless its
    current content is another task's, so a failed observation never hides
    the task's work (Adversary CO-A4-F2, 2026-10-28). The write that could
    not fingerprint the path also drops the task's older hash of it in that
    session, so the pair stays without a content record after the file is
    readable again. A directory pair is
    inspected as a git pathspec: gate 3 fails while any path under it is
    dirty, including dirt another session made there after the task's write.
    That over-block is accepted. The owner's own directory pairs already
    behave so, rule 3 refuses the directory write when foreign dirt lies under
    it at first touch, and the block clears once that dirt is committed.
    All of this covers pairs that sessions on this machine recorded. A pair a
    session on another machine recorded, the attribution owner's included,
    follows Decision 4 instead.
    Rejected: V1's untransferred-pair inventory, retired with the transfer
    (Decision 2).
23. **Whole-index commits in a shared checkout (Orchestrator found work,
    2026-10-28).** A Plan Writer's `git commit -m` in the main checkout
    (2068aaf9a0) recorded another live Plan Writer's staged
    `.gobby/plans/external-mcp-transport-multiplexer.md`, and rule 2 did not
    refuse it. A commit without a pathspec records the whole shared index.
    Rule 2 reads `git diff --cached` when the hook runs and the shell commits
    later, so a path staged in between is never checked, and a path its
    stager never recorded has no holder (Decision 1). Which of the two
    happened is not established, because the other session's ledger is not
    readable from another seat. Rule: rule 2 refuses a commit that records
    the whole index (no pathspec, or `--include` with pathspecs; `--all`
    without one is the first) while another live session on the event's
    machine holds a pair in that checkout that git still reports dirty. The
    refusal names the holders and prescribes `git commit -m <msg> --
    <path>...`. A pathspec commit builds a temporary index from only the
    listed paths and leaves other sessions' staged paths staged, so no later
    staging can reach it. Concluding an in-progress merge, cherry-pick,
    revert or rebase is exempt, because git refuses a partial commit there,
    and rule 2's per-path check still applies to it. This changes the form
    of a commit rule 2 accepts, not its path scope: a path no live session
    mutated still commits freely with a pathspec, so Decision 1 holds.
    Residual: a session with no dirty pair in the checkout that stages a
    path it never recorded can still be swept by a whole-index commit while
    no other session holds dirt there; nothing attributes that path to it.
    Like every refreshed ruling, this takes effect with Josh's approval
    through the Assistant (#25261 criterion 3). Rejected: refusing only when
    the hook-time staged set holds a foreign path, which misses a path
    staged after the check; and rewriting the command into a pathspec
    commit, which hides the change from the caller.

## Retired Mechanisms
`kind: framing`

This section answers #22644 criteria 1 and 4.

**Delivered.** 1.1 (#23562) retired `release_task_paths`,
`inspect_task_path_ownership`, the claim-time scope check and
`_task_attribution_sessions`. 1.2 (#23563) retired
`capture_baseline_dirty_files` and `baseline_dirty_files`. 1.3 (#23564)
moved rules 2 and 3 to live session ownership, and no
`release_task_edited_files` remains. At `b42c2172d9`, none of those names
appears in `src/`, `docs/guides` or `docs/reference-audit`, except the stale
comment at `src/gobby/workflows/hooks.py:422`, which 1.6 fixes.

**Retired by the open sections:**
- **`task_edited_file_checkouts_history`,
  `task_edited_file_checkouts_history_started_at` and
  `task_edited_file_times`** (1.4), with the accessors
  `task_edited_checkout_history_paths`, `other_task_edited_checkout_paths`,
  `task_checkout_paths_in_window`, `task_edited_file_set_with_history` and
  `task_edited_file_times`.
  - Replacement: none. Gate 12 and the claim window that read them are gone,
    and the content records carry ownership (Decision 8).
  - Change `tests/workflows/test_task_claim_state.py`,
    `tests/workflows/test_session_variable_manager.py`,
    `tests/workflows/test_commit_guard.py` and
    `tests/agents/test_terminal_timeout_checkpoint.py`.
- **The unscoped comparisons of `task_edited_files` with checkout pairs**
  (1.4): `_claim_release._uncommitted_refusal`'s unscoped branch and
  `capture_attribution`'s `raw <= recorded_paths` completeness test.
  - Replacement: none. Every record carries its checkout root.
  - Replace `test_unscoped_ledger_is_not_replaced_by_commit_fallback` in
    `tests/mcp_proxy/tools/tasks/test_close_attribution.py`.
- **Recording by name** (1.5): the recorder's success-only call and its
  `canonical_repo_mutation` condition.
  - Replacement: the fingerprint comparison (§Content-Hash Contract).
  - Change `tests/hooks/test_tool_handlers.py` and
    `tests/hooks/test_normalization_canonical_mutation_scope.py`.

**Withdrawn from the plan, never built:** V1's tagged
`session_dirty_files`, its append-only nested `task_edited_files`,
`edit_ledger.py` with `EditLedgerMixin`, `end_task_tags`, `task_tag_holders`,
`SessionVariablePairMutation`, `transfer_task_pairs`,
`untransferred_task_pairs`, `releases_task_ownership`, the
`_close_gate_helpers.py` split, the `_normalization_git_paths.py` split, and
V1's edits to `state_manager.py` and `storage/hub/postgres_pool.py`
(Decisions 2, 8, 16 and 20).

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and targets files, never the full suite. No leaf restarts the daemon.
- **Rule 4 ships disabled (Orchestrator ORCH-REV-CO-4, 2026-10-28).** 1.6
  ships the `block-cross-project-write` template with `enabled: false`. The
  Orchestrator's next daemon restart syncs it as a disabled row (AGENTS.md
  rule 8; first sync takes the template's `enabled`, and later drift
  refreshes keep the row's toggle). Rule 4 goes live only through V2 step 6,
  after Josh approves enabling it through the Orchestrator.
- **Order.** 1.4 precedes 1.7, because both edit
  `_close_context.py::capture_attribution`,
  `session_edit_ledger.py::SessionEditLedger.record_edited_files` and
  `task_claim_state.py`. 1.8 precedes 1.7, so gate 3 never
  requires a commit that rule 2 refuses, and both edit `dirty_edit_grants.py`.
  1.5 precedes 1.6, whose end-to-end acceptance needs the recorder to record a
  sanctioned worktree write. 1.8 follows 1.4, because both edit
  `tests/workflows/test_commit_guard.py`. 1.4 follows 1.3
  (`task_claim_state.py`), 1.5 follows 1.3 (`_tool.py`), and 1.8 follows 1.3
  (`commit_guard.py`).
- **Cross-plan order (Orchestrator ORCH-REV-CO-5, 2026-10-28).**
  `.gobby/plans/s2-11-hub-hooks-rules.md` (#24151) ports to Rust the Python
  modules this plan changes, and `.gobby/plans/tasks-family.md` 3.7 ports
  gate 3's `capture_attribution`. A port that reads a module before this
  plan's leaf lands would carry the old rules into Rust, so each porting
  section follows the leaves that change its sources:
  - S2.11 2.7 (`_normalization_metadata.py`): 1.5;
  - S2.11 2.9 and 2.11 (`task_claim_state.py`, `dirty_edit_grants.py`,
    `session_edit_ledger.py`): 1.4, 1.7 and 1.8;
  - S2.11 2.10 (`commit_guard.py`, `worktree_shell_attribution.py`): 1.5
    and 1.8;
  - S2.11 5.7 and 5.8 (`_session_end.py`, `_tool.py`): 1.5;
  - S2.11 1.1, 2.17 to 2.19 and 7.13 (the rule-pass corpus,
    `workflows/hooks.py`, `engine/core.py`): 1.6;
  - tasks-family 3.7 (`_close_context.py::capture_attribution`): 1.4 and
    1.7.

  No task exists yet for those sections (S2.11 has no M1), so no heading
  here can name them. The plan that expands second carries these edges as
  `(depends: #N)` on its porting headings, or the Orchestrator sets them at
  that expansion. This plan never waits on the ports.
- **Large files.** Each targeted production file of 850 or more lines is
  split by its one section: `workflows/commit_guard.py` (863 lines, 1.8,
  which moves `_operation_paths`' 57 lines out before adding the
  whole-index refusal, so the file stays under 1,000 lines),
  `workflows/hooks.py` (909, 1.6) and `workflows/engine/core.py` (953, 1.6).
  `workflows/state_manager.py` (879), `hooks/_normalization_canonical.py`
  (875) and `storage/hub/postgres_pool.py` are no longer targeted. Every
  other target is under 850 lines: `_tool.py` 762, `session_activation.py`
  671, `task_claim_state.py` 670, `worktree_checkpoint.py` 594,
  `worktree_shell_attribution.py` 532, `_normalization_metadata.py` 507,
  `_claim_release.py` 352, `session_edit_ledger.py` 313, `_session_end.py`
  266, `dirty_edit_grants.py` 233 and `_close_context.py` 131.
- **Sweep evidence (#25261 criterion 1).** Facts were verified read-only on
  `0.5.0` at `b42c2172d9`.
  - Every Target and `Consumers unchanged` path of 1.4 to 1.8 exists at that
    commit, except the new files these sections create:
    `src/gobby/hooks/pre_write_digests.py`,
    `src/gobby/workflows/ownership_eval_context.py`,
    `src/gobby/workflows/engine/eval_context_defaults.py`,
    `src/gobby/workflows/git_operation_paths.py`,
    `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-project-write.yaml`,
    `tests/hooks/test_pre_write_digests.py`,
    `tests/workflows/test_ownership_eval_context.py` and
    `tests/workflows/test_git_operation_paths.py`. Every exact
    `path::symbol` Target of 1.4 to 1.8 resolves in `gcode outline` of its
    file. 1.1 to 1.3 keep their delivered text; their `operation: delete`
    targets are gone because those leaves deleted them, and the two
    `git_utils.py` symbols 1.2 deleted are now one `git_utils.py::*` Target.
  - No Target, acceptance item or `Consumers unchanged` entry of 1.4 to 1.8
    names `_close_evaluation_support.py`, `_lifecycle_validation.py`,
    `_close_gate_helpers.py`, `_stage_review.py`, `_lifecycle_paths.py`,
    `edit_ledger.py`, gate 12 or transcript evidence.
  - `gcode grep -l` over `tests/` found the four test files that name the
    variables and accessors 1.4 retires, and about 40 that use the surviving
    flat `session_dirty_files` and `task_edited_files` (Decision 8).
  - `uv run python` with
    `gobby.hooks.normalization.normalize_tool_fields` reproduced the
    write-path union defect: `cp a.txt b.txt && git restore -- c.txt`
    publishes write paths `['b.txt']`, `git add x.txt && git restore --
    y.txt` publishes none, and `rm e.txt && echo x > d.txt` and `mv a.txt
    b.txt && echo x > d.txt` publish `['d.txt']`.
- **No new persisted state.** The open sections add no session variable. The
  pre-write fingerprints (Decision 5) live only in the daemon's memory, in
  `PRE_WRITE_DIGESTS`, for at most one hour.

## P1: Content-based ownership
`kind: framing`

**Goal**: ownership follows bytes. A session owns exactly the paths it
changed, no session overwrites another's dirt, commits carry only their
author's dirt, their claimed task's dirt or a merge's paths, writes stay
inside the session's project, and close checks every uncommitted change the
closing task still owns.

### 1.1 Retire release_task_paths, inspect_task_path_ownership and the claim scope check [category: code]
`kind: deliverable`

**Status:** delivered by #23562 (closed). The text is kept as
delivered, plus a Consumers unchanged entry for a test added since;
its gate 9 is v4's gate 3, `uncommitted_task_edits`.

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_paths.py::*` — operation: delete — scope-reason: retires release_task_paths, inspect_task_path_ownership and their private helpers with the module
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle.py::create_lifecycle_registry`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py::*` — scope-reason: deletes four scope-check helpers, their call in register_claim_task, their commit_guard imports and the link-based _task_attribution_sessions
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
  `_task_attribution_sessions` (lines 71-84) has no other caller. Its
  discovery through best-effort `session_tasks` links is replaced by the tag
  query `task_tag_holders` (1.4, Decision 2).
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
  the call and its error branch in `claim_task`, the three `commit_guard`
  imports, and `_task_attribution_sessions`.
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
- `tests/mcp_proxy/tools/tasks/test_close_authority.py` — no-edit-reason: builds the registry to call close_task, which keeps its name
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

**Status:** delivered by #23563 (closed). The text is kept as
delivered, except that the two `git_utils.py` symbol Targets it deleted
are now one `git_utils.py::*` Target, plus a Consumers unchanged entry
for a test added since; its gate 9 is v4's gate 3,
`uncommitted_task_edits`.

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_actions.py::*` — operation: delete — scope-reason: the module only registers capture_baseline_dirty_files
- `src/gobby/mcp_proxy/tools/sessions/_factory.py::create_session_messages_registry`
- `src/gobby/hooks/session_activation.py::_missing_baseline_keys`
- `src/gobby/hooks/session_activation.py::_baseline_updates`
- `src/gobby/workflows/git_utils.py::*` — scope-reason: deletes GIT_STATUS_UNAVAILABLE_MARKER and get_dirty_files_async
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
- `tests/mcp_proxy/tools/sessions/test_registration_terminal_binding.py` — no-edit-reason: builds the registry to call a registration tool only

**Focused verification (planned):** run the listed test files with the
isolation prefix.

**Acceptance:**

- 1.2.1 - The sessions registry built by `create_session_messages_registry` has no `capture_baseline_dirty_files` tool. test: `tests/mcp_proxy/tools/test_internal_action_tools.py::test_sessions_registry_omits_capture_baseline_dirty_files`.
- 1.2.2 - Session activation on a fresh session writes no `baseline_dirty_files` variable and does not report it missing. test: `tests/hooks/test_session_activation_reconciliation.py::test_activation_seeds_no_baseline_dirty_files`.
- 1.2.3 - `rg -n "baseline_dirty_files|capture_baseline_dirty_files|GIT_STATUS_UNAVAILABLE_MARKER" src/ docs/guides docs/reference-audit -g '!src/gobby/workflows/hooks.py'` prints nothing. `hooks.py` is excluded because its line 408 comment is fixed in 1.6, which runs after 1.2; V2 step 2 sweeps the whole tree. test: `tests/hooks/test_session_activation_reconciliation.py::test_baseline_dirty_tracking_invariant_still_seeds_edit_ledgers`.

### 1.3 Rules 2 and 3 judge live session ownership [category: code] (depends: 1.1)
`kind: deliverable`

**Status:** delivered by #23564 (closed). The text is kept as
delivered; its gate 9 is v4's gate 3, `uncommitted_task_edits`.

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

Split `commit_guard.py` (956 lines): move exactly these commit-parsing
symbols (`_GIT_GLOBAL_OPTIONS_WITH_VALUE`, `GitCommitInvocation`,
`parse_git_commit_invocations`, the shell helpers `_SHELL_KEYWORDS` through
`_join_chdir`, and `resolve_commit_inspect_cwd`, all within lines 45-322)
into the new
`src/gobby/workflows/git_commit_parsing.py`, and move their tests into the new
`tests/workflows/test_git_commit_parsing.py`. The symbol list governs:
`ForeignPathOwner`, `CheckoutPathOwnership` and
`DirtyEditOwnershipInspectionError` (lines 70-92) sit inside that span and
stay in `commit_guard.py`.

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

### 1.4 Retire the ledger state v4 left without readers [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/session_edit_ledger.py::SessionEditLedger.record_edited_files`
- `src/gobby/workflows/task_claim_state.py::*` — scope-reason: deletes five accessors, narrows task_edited_checkout_paths to live pairs, drops the ledger-name parameter of _task_edited_file_checkouts, the edit-time key of remove_claimed_task and the stale gate list of release_claimed_task's docstring
- `src/gobby/mcp_proxy/tools/tasks/_claim_release.py::_uncommitted_refusal`
- `src/gobby/mcp_proxy/tools/tasks/_close_context.py::capture_attribution`
- `src/gobby/agents/worktree_checkpoint.py::_authorized_task_paths`
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: deletes the task_edited_file_times default only
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: record tests read task_edited_checkout_times instead of the retired variables
- `tests/workflows/test_task_claim_state.py::*` — scope-reason: deletes the edit-time accessor tests and adds the live-pair and removal tests
- `tests/workflows/test_commit_guard.py::*` — scope-reason: drops the inert history key from three fixtures
- `tests/agents/test_terminal_timeout_checkpoint.py::*` — scope-reason: drops the task_edited_file_times assertions
- `tests/mcp_proxy/tools/tasks/test_close_attribution.py::*` — scope-reason: replaces the unscoped-ledger completeness test
- `tests/mcp_proxy/tools/tasks/test_coordinator_claim_release.py::*` — scope-reason: adds the moved-pair release test
- `tests/workflows/test_session_defaults.py::*` — scope-reason: adds the retired-default test

**Research context:**
- `session_edit_ledger.py::SessionEditLedger.record_edited_files(session_id,
  repo_relative_paths, *, checkout_root=None, edited_at=None,
  started_at=None, attribute_to_task=True)` (lines 55-240) writes, for an
  attributed edit, `task_edited_files`, `task_edited_file_times` (lines
  164-175), `task_edited_checkout_times` (lines 176-194, the newest stamp
  wins), `task_edited_checkout_hashes` (`record_task_content_hashes`),
  `task_edited_file_checkouts`, `task_edited_file_checkouts_history` and
  `task_edited_file_checkouts_history_started_at` (lines 198-214). Both
  callers pass `checkout_root`: `_tool.py` line 667 and
  `worktree_shell_attribution.py` line 408.
- `task_claim_state.py`: `task_edited_checkout_paths` (lines 506-520) unions
  the live map with the history map. `task_edited_checkout_history_paths`,
  `task_checkout_paths_in_window`, `other_task_edited_checkout_paths` and
  `task_edited_file_set_with_history` have no caller outside the module, and
  `task_edited_file_times` (lines 593-605) is called only by
  `task_checkout_paths_in_window`. `_task_edited_file_checkouts(variables,
  ledger_name="task_edited_file_checkouts")` (line 173) takes the ledger
  name only for the history readers. `remove_claimed_task` (lines 276-293)
  pops the task from `task_edited_files`, `task_edited_file_checkouts`,
  `task_edited_file_times` and `task_live_edit_starts`.
  `release_claimed_task`'s docstring (line 254) names close "gates 7, 9, 10
  and 12", which v4 renumbered or deleted.
- `transfer_live_checkout_paths` (lines 472-503) moves a pair's live interval
  to the editing task but leaves `task_edited_files` untouched, so a task's
  flat list can name a path whose live pair a later task took.
- `_claim_release.py::_uncommitted_refusal` (lines 49-88) checks the holder's
  `task_edited_checkout_paths` per root, then adds `task_edited_file_set`
  paths missing from those pairs at the holder's workspace (lines 58-65).
  Once history is gone, that branch would refuse a release over a path a
  later task took.
- `_close_context.py::capture_attribution` (lines 64-131) sets `complete = raw
  <= recorded_paths` (lines 80-83), with `raw` the flat task list and
  `recorded_paths` from `task_edited_checkout_paths`. Without history, a path
  a later task took would make `complete` false, and gate 3 would fail with
  "Git could not inspect all task-attributed files." `complete` also turns
  false on `DirtyEditOwnershipInspectionError` (line 126), which stays.
  `tests/mcp_proxy/tools/tasks/test_close_attribution.py::test_unscoped_ledger_is_not_replaced_by_commit_fallback`
  pins the unscoped case as incomplete.
- `worktree_checkpoint.py::_authorized_task_paths` reads
  `task_edited_file_times` only as one of three ledgers whose absence of the
  task marks a pre-#21897 cleared child (lines 520-527).
- The defaults YAML declares `task_edited_file_times` (lines 37-39).
- `gcode grep -l` over `tests/` finds the retired names only in
  `test_task_claim_state.py`, `test_session_variable_manager.py`,
  `test_commit_guard.py` (history keys in three fixtures, lines 1283, 1336
  and 1371) and `test_terminal_timeout_checkpoint.py` (lines 249 and 319).

**Implementation:**
- `record_edited_files` stops writing `task_edited_file_times`,
  `task_edited_file_checkouts_history` and
  `task_edited_file_checkouts_history_started_at`. Its signature and every
  other key are unchanged; `edited_at` still feeds
  `task_edited_checkout_times`.
- In `task_claim_state.py`, delete `task_edited_checkout_history_paths`,
  `other_task_edited_checkout_paths`, `task_checkout_paths_in_window`,
  `task_edited_file_set_with_history` and `task_edited_file_times`.
  `task_edited_checkout_paths` returns the pairs of
  `task_edited_file_checkouts` only. `_task_edited_file_checkouts` drops its
  `ledger_name` parameter. `remove_claimed_task` stops handling
  `task_edited_file_times`. `release_claimed_task`'s docstring says it keeps
  what close gate 3 and claim release read.
- `_uncommitted_refusal` checks the `task_edited_checkout_paths` pairs only;
  delete the unscoped branch and its `checkout_unresolved` refusal.
- `capture_attribution` deletes `recorded_paths` and the completeness test.
  `complete` starts true, and only an ownership inspection failure clears it.
  `raw` still gates the linked-commit fallback.
- `_authorized_task_paths` checks `task_edited_files` and
  `task_edited_file_checkouts` for the cleared-child case.
- The defaults YAML drops `task_edited_file_times`.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` — no-edit-reason: passes attribution.clean_checkouts and attribution.complete unchanged
- `src/gobby/tasks/close_invariants.py` — no-edit-reason: receives attribution_complete as a boolean whose cause narrows without a signature change
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py` — no-edit-reason: merges remove_claimed_task's result, which only loses a retired key
- `src/gobby/storage/tasks/_live_session_recovery.py` — no-edit-reason: merges remove_claimed_task's result, which only loses a retired key
- `src/gobby/mcp_proxy/tools/spawn_agent/_execution.py` — no-edit-reason: merges remove_claimed_task's result, which only loses a retired key
- `src/gobby/workflows/observers.py` — no-edit-reason: merges remove_claimed_task's result, which only loses a retired key
- `src/gobby/workflows/monolith_guard.py` — no-edit-reason: reads the surviving task_edited_files lists
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py` — no-edit-reason: patches capture_attribution with a fixed CloseAttributionSnapshot
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py` — no-edit-reason: patches capture_attribution with a fixed CloseAttributionSnapshot
- `tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py` — no-edit-reason: patches _authorized_task_paths with a fixed path set

**Focused verification (planned):** run the seven listed test files plus
`tests/tasks/test_close_invariants.py` with the isolation prefix.

**Acceptance:**

- 1.4.1 - `record_edited_files` writes `task_edited_checkout_times`, `task_edited_checkout_hashes`, `task_edited_file_checkouts` and `task_edited_files` for the attributed task and writes none of `task_edited_file_times`, `task_edited_file_checkouts_history` and `task_edited_file_checkouts_history_started_at`; a replayed record with an older `edited_at` never lowers a pair's `task_edited_checkout_times` stamp. test: `tests/workflows/test_session_variable_manager.py::test_record_edited_files_writes_no_retired_ledgers`.
- 1.4.2 - After task B's edit of a pair takes its live interval from task A in the same session, `task_edited_checkout_paths(variables, A)` omits the pair and `task_edited_checkout_paths(variables, B)` holds it. test: `tests/workflows/test_task_claim_state.py::test_task_edited_checkout_paths_returns_live_pairs_only`.
- 1.4.3 - `release_task_claim` releases the holder's claim on task A when the only dirty path A edited was later taken by the holder's task B, and still refuses with `task_has_uncommitted_changes` while a live pair of A is dirty. test: `tests/mcp_proxy/tools/tasks/test_coordinator_claim_release.py::test_claim_release_ignores_pairs_a_later_task_took`.
- 1.4.4 - `capture_attribution`, given a `task_edited_files` path with no checkout pair and a linked commit, reports `complete`, skips the linked-commit fallback and returns no checkouts. test: `tests/mcp_proxy/tools/tasks/test_close_attribution.py::test_attribution_complete_ignores_unscoped_task_paths`, which replaces `test_unscoped_ledger_is_not_replaced_by_commit_fallback`.
- 1.4.5 - `task_claim_state` defines none of `task_edited_checkout_history_paths`, `other_task_edited_checkout_paths`, `task_checkout_paths_in_window`, `task_edited_file_set_with_history` and `task_edited_file_times`, and `remove_claimed_task` returns no `task_edited_file_times` key. test: `tests/workflows/test_task_claim_state.py::test_v4_orphaned_ledger_accessors_are_gone`.
- 1.4.6 - The bundled session variable defaults declare no `task_edited_file_times`. test: `tests/workflows/test_session_defaults.py::test_defaults_declare_no_retired_edit_ledgers`.

### 1.5 The recorder records only content-changing writes [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: the before-tool snapshot, the outcome-independent recorder call, is_canonical_edit without canonical_repo_mutation, and the recorder's fingerprint comparison under its new name _record_file_mutation
- `src/gobby/hooks/pre_write_digests.py`
- `src/gobby/hooks/event_handlers/_session_end.py::SessionEndMixin.handle_session_end`
- `src/gobby/workflows/worktree_shell_attribution.py::structured_edit_paths`
- `src/gobby/hooks/_normalization_metadata.py::_merge_shell_segment_metadata`
- `tests/hooks/test_pre_write_digests.py`
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: adds the content-check, failed-call, node-origin and #22642 regression tests and renames two recorder calls
- `tests/hooks/test_normalization.py::*` — scope-reason: adds the mixed-segment write-path union test
- `tests/hooks/test_normalization_canonical_mutation_scope.py::*` — scope-reason: test_read_only_probe_loop_is_not_an_in_project_mutation restates the recorder gate, which drops canonical_repo_mutation

**Granularity:** one section with more than six acceptance items, by design.
The recorder is one lifecycle owner: the before-tool snapshot, the store and
its session-end cleanup, and the after-tool comparison verify only together,
and the paths they fingerprint are the ones the normalization change
publishes. The #22642 regression needs both halves.

**Research context:**
- `_tool.py::ToolEventHandlerMixin.handle_after_tool` (lines 360-467) sets
  `is_canonical_edit` to `canonical_tool_kind == "write"` and
  `canonical_repo_mutation` (lines 411-414) and calls
  `_record_successful_file_mutation` only when `not is_failure and is_edit`
  (line 422). The worktree shell recording (`_record_worktree_shell_writes`,
  line 420) and claimed-shell adoption (`_adopt_shell_writes`, line 434)
  already run for failed calls. Lines 405-408 normalize a Bash call that
  arrives without canonical keys.
- `_record_successful_file_mutation` (lines 579-676) takes its paths from
  `worktree_shell_attribution.structured_edit_paths`, resolves pairs with
  `resolve_edit_pair` at `effective_tool_cwd(event.cwd, event.data)`, drops
  overlapped worktree paths and replayed landed paths
  (`_paths_landed_before_edit`), and calls `record_edited_files` per root.
  Nothing compares bytes, so every named path records.
  `tests/hooks/test_tool_handlers.py` calls it by name at lines 1186 and
  1499.
- `structured_edit_paths` (lines 121-130) returns
  `canonical_edit_file_paths`, else `canonical_file_paths`, for a canonical
  edit, else the tool input's file path. Edit paths come only from
  repo-mutation segments, so a Bash write into another project's sanctioned
  worktree publishes none. `begin_structured_edit_calls` (lines 451-502)
  also calls it, for structured tools only.
- Rule 3 reads `commit_guard._write_paths` (lines 419-426):
  `canonical_write_file_paths`, else `canonical_file_paths`.
- `_normalization_metadata.py::_merge_shell_segment_metadata` (lines 252-507)
  adds only each segment's explicit `item.write_paths` to the write paths
  (lines 438-440). `git restore`, `rm` and `mv` segments supply paths without
  explicit write paths. `_normalization_canonical.py` line 520 calls it.
- `ToolEventHandlerMixin.handle_before_tool` (lines 106-173) reads
  `event.data` and `_platform_session_id` and returns `allow` at its end.
- `SessionEndMixin.handle_session_end` (`_session_end.py` lines 28-266)
  resolves the platform session id.
- `hooks/events.py::correlate_hook_lifecycle` (line 169) sets
  `event.request_id`; the Claude Code, Codex, Droid, Grok, AGY and web chat
  adapters call it.
- `workflows/checkout_fingerprint.py::file_fingerprint` and
  `path_fingerprints` (31 lines) give git blob hashes, None for an absent
  path, and `OSError` for a directory.
- `workflows/task_dirty_state.py::task_dirty_paths(paths, cwd)` is the sync
  pathspec status: None when git fails, untracked dirty, ignored absent. It
  runs `utils/git.py::run_git_command`, which spawns git on the hub even for
  a node-origin event.
- `hooks/checkout_reads.py`: `node_origin()` is true inside a node
  delivery; `run_git(args, *, cwd, timeout)` runs git locally outside one
  and on the node inside one, raising `CheckoutReadUnavailable` (an
  `OSError`) when the node cannot answer. `_tool.py` already imports
  `node_origin`. `utils/daemon_git.py::parse_porcelain_v1_z` parses
  `status --porcelain=v1 -z` output.
- The #22642 case: `git restore --staged tests/e2e/test_terminal_client_stack.py`
  in a checkout where that path was staged before the claim; the path flipped
  from staged to unstaged and was recorded as an edit. The classification is
  fixed (Decision 7); no recorder test pins the regression.

**Implementation:**
- `_merge_shell_segment_metadata` keeps adding every segment's explicit
  `write_paths`, and adds each write-kind segment's resolvable paths,
  loop-bound ones included, when that segment has no explicit write paths.
  Execute-kind segments without explicit write paths add none. Unresolved
  paths stay excluded, and the function's other outputs are unchanged.
- `structured_edit_paths` returns, for a canonical edit,
  `canonical_write_file_paths` when present, else `canonical_edit_file_paths`,
  else `canonical_file_paths`; the tool-input fallback is unchanged.
- New `hooks/pre_write_digests.py`, class `PreWriteDigests`. It holds
  `dict[tuple[str, str, str], tuple[str | None, float]]`, keyed
  `(session_id, request_id or "", realpath)` with value `(fingerprint,
  time.monotonic())`, behind a `threading.Lock`. Its API:
  - `snapshot(session_id, request_id, paths)` first drops every entry older
    than 3600 seconds, then stores `file_fingerprint` for each path. A path
    whose fingerprint raises `OSError`, directories included, is logged at
    debug and gets no entry; the other paths are still stored.
  - `take(session_id, request_id, path) -> tuple[str | None] | None` pops the
    entry and returns a one-tuple holding the stored fingerprint, or None
    when there is no entry or the entry is older than 3600 seconds.
  - `clear_session(session_id)` drops all of that session's entries.
  The module holds one daemon-wide instance, `PRE_WRITE_DIGESTS`, which
  `_tool.py` and `_session_end.py` import. The module also defines
  `status_dirty_paths(root, rels) -> set[str] | None`: one
  `checkout_reads.run_git(["--literal-pathspecs", "--no-optional-locks",
  "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *rels],
  cwd=root, timeout=10)`, parsed with `parse_porcelain_v1_z`; both paths of a
  rename count. A non-zero exit, unparsable output or a raised `OSError`
  (`CheckoutReadUnavailable` included) returns None.
- `handle_before_tool`: when the session id is known and the call is
  write-kind (an `EDIT_TOOLS` name or `canonical_tool_kind == "write"`, after
  normalizing a Bash call that lacks canonical keys), resolve the
  `structured_edit_paths` paths with `resolve_edit_pair` at
  `effective_tool_cwd(event.cwd, event.data)` and call `snapshot` with
  `event.request_id` before the final `allow`. Snapshot errors are logged and
  never block. A node-origin event (`node_origin()`) stores no snapshot
  (Decision 4).
- `is_canonical_edit` becomes `canonical_tool_kind == "write"`.
  `handle_after_tool` calls the recorder for every write-kind call, failed
  ones included. The recorder is renamed `_record_file_mutation`.
- For each resolved pair (Decision 5), the recorder records the pair when
  `take` returns an entry whose fingerprint differs from the current one, and
  records nothing on equal fingerprints, including for a path the session
  already owns. Pairs with no entry, or whose current fingerprint raises
  `OSError`, are batched per root into one `status_dirty_paths(root, rels)`:
  dirty records, clean records nothing, None records. For a node-origin
  event the recorder fingerprints nothing on the hub: every pair takes this
  status, which runs on the node. The overlap and replay
  filters then apply as today. `_mark_session_had_edits_if_claimed` runs only
  when a pair was recorded.
- `handle_session_end` calls `PRE_WRITE_DIGESTS.clear_session(session_id)`
  once the session id is resolved.

Consumers unchanged:
- `src/gobby/hooks/_normalization_canonical.py` — no-edit-reason: calls _merge_shell_segment_metadata with an unchanged signature
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py` — no-edit-reason: drives handle_session_end, whose added in-memory cleanup touches no stored state
- `tests/workflows/test_session_end_cleanup.py` — no-edit-reason: drives handle_session_end, whose added in-memory cleanup touches no stored state
- `tests/workflows/test_step_snapshot_semantics.py` — no-edit-reason: drives handle_session_end, whose added in-memory cleanup touches no stored state

**Focused verification (planned):** run `tests/hooks/test_tool_handlers.py`,
`tests/hooks/test_pre_write_digests.py`, `tests/hooks/test_normalization.py`,
`tests/hooks/test_normalization_canonical_mutation_scope.py` and
`tests/workflows/test_commit_guard.py` with the isolation prefix.

**Acceptance:**

- 1.5.1 - #22642 regression: in a temporary git checkout where a tracked file is staged, a Bash `git restore --staged <path>` call records no pair in `session_dirty_files`, `task_edited_files` or `session_edited_files`. test: `tests/hooks/test_tool_handlers.py::test_git_restore_staged_records_no_attribution`.
- 1.5.2 - A Write or Edit that leaves a clean path's bytes unchanged records nothing, and one that changes them records the pair. test: `tests/hooks/test_tool_handlers.py::test_recorder_records_only_content_changes`.
- 1.5.3 - A Write creating a new untracked file records it. A path the session owns under task A, rewritten with the same bytes while task B is active, records nothing for B: A keeps the live interval and the newest content record. test: `tests/hooks/test_tool_handlers.py::test_recorder_handles_untracked_and_owned_paths`.
- 1.5.4 - Mixed shell segments publish `canonical_write_file_paths` as the union of write-kind targets: `cp a b && git restore -- c` gives `[b, c]`, `mv a b && echo x > d` gives `[a, b, d]`, `rm e && echo x > d` gives `[e, d]`, and `git add x && git restore -- y` and `git restore --staged x && echo z > y` give `[y]` only. test: `tests/hooks/test_normalization.py::test_mixed_segments_publish_union_of_write_kind_targets`.
- 1.5.5 - In a temporary git checkout, a Bash `git add <foreign path> && git restore -- <own path>` call, where `<own path>` is in the session's ledger and the restore changes it, records `<own path>` and nothing for the `git add` path. test: `tests/hooks/test_tool_handlers.py::test_recorder_ignores_execute_segment_paths_in_mixed_command`.
- 1.5.6 - A failed Bash call `echo changed > <path> && false` records the pair, and a failed call that leaves the bytes unchanged records nothing. test: `tests/hooks/test_tool_handlers.py::test_failed_write_call_records_only_changed_bytes`.
- 1.5.7 - With MERGE_HEAD present, a same-bytes Write to a dirty merge-set path held by another session records nothing for the writer. test: `tests/hooks/test_tool_handlers.py::test_merge_set_noop_write_records_nothing`.
- 1.5.8 - A Bash call that writes a clean tracked path and commits it (`echo x > <path> && git commit -qam m`) records the pair for the task, and reconciliation then drops it from the session's dirty set. test: `tests/hooks/test_tool_handlers.py::test_write_committed_in_same_call_keeps_task_record`.
- 1.5.9 - With no stored entry, the status rule decides (dirty records, clean records nothing), and a directory path uses the same rule. test: `tests/hooks/test_tool_handlers.py::test_recorder_falls_back_to_status_without_entry`.
- 1.5.10 - Interleaved `before(A)`, `before(B)`, `after(A)`, `after(B)` on one path with distinct request ids keep separate baselines. With no request id, the second `before` overwrites the first, and the second `take` finds no entry. test: `tests/hooks/test_pre_write_digests.py::test_interleaved_calls_keep_baselines_by_request_id`.
- 1.5.11 - `handle_session_end` drops the session's entries and keeps other sessions' entries, `snapshot` drops entries older than one hour, and `take` returns None for an entry older than one hour when no `snapshot` ran since. test: `tests/hooks/test_pre_write_digests.py::test_session_end_and_age_prune_drop_stale_entries`.
- 1.5.12 - A Bash write to the relative path `p.py` with `event.cwd` in the main checkout and `workdir` in a second checkout of the project fingerprints and records the second checkout's pair and records nothing for the main checkout's `p.py`. test: `tests/hooks/test_tool_handlers.py::test_recorder_resolves_relative_paths_at_tool_workdir`.
- 1.5.13 - A Bash call that changes two paths and makes the first unreadable (`chmod 000`) records both pairs, the first through the status fallback, and an unreadable path at `before_tool` stores no entry while the call's other paths keep theirs. test: `tests/hooks/test_tool_handlers.py::test_unreadable_path_takes_status_fallback_without_skipping_others`.
- 1.5.14 - The read-only probe loop `for i in 1 2 3; do curl -s -o /dev/null ...; done` reaches the recorder as a write-kind call and records nothing, because `/dev/null` resolves to no pair. test: `tests/hooks/test_normalization_canonical_mutation_scope.py::test_read_only_probe_loop_is_not_an_in_project_mutation`.
- 1.5.15 - Inside `checkout_reads.delivery_scope` for a node machine, with the hub holding a different file at the same root and path, a write-kind call stores no `PRE_WRITE_DIGESTS` entry and never fingerprints the hub file. The recorder runs the pathspec status through `checkout_reads.run_git` on the node (a patched node reply): a dirty reply records the pair, a clean reply records nothing, and `CheckoutReadUnavailable` records the pair. test: `tests/hooks/test_tool_handlers.py::test_node_origin_write_skips_snapshot_and_takes_node_status`.

### 1.6 Rule 4 refuses writes into another Gobby project [category: code] (depends: 1.3, 1.5)
`kind: deliverable`

Targets:
- `src/gobby/workflows/hooks.py::*` — scope-reason: moves the ownership eval-context wiring out of WorkflowHookHandler._evaluate_rules and fixes the stale comment at line 422
- `src/gobby/workflows/ownership_eval_context.py`
- `src/gobby/workflows/engine/core.py::*` — scope-reason: moves the eval_context.setdefault block out of RuleEngine.evaluate, whose signature is unchanged
- `src/gobby/workflows/engine/eval_context_defaults.py`
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-project-write.yaml`
- `tests/workflows/test_ownership_eval_context.py`
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: adds the rule 4 template test

Split `src/gobby/workflows/hooks.py` (909 lines): move the ownership
eval-context wiring in `WorkflowHookHandler._evaluate_rules` (lines 550-621:
the eval-context seed, the rule 2 and 3 calls, the review-scope keys and the
landing-merge key) into the new `src/gobby/workflows/ownership_eval_context.py`
as `build_ownership_eval_context`, which also computes rule 4.

Split `src/gobby/workflows/engine/core.py` (953 lines): move the
`eval_context.setdefault` block of `RuleEngine.evaluate` (lines 318-331)
into the new `src/gobby/workflows/engine/eval_context_defaults.py` as
`apply_eval_context_defaults(eval_context, *, blocking_deadline)`, adding
`cross_project_write_conflict` with default "".

**Research context:**
- Nothing enforces rule 4. `_path_scope.py::_is_project_managed_path` is
  false for a path in another repository, so `canonical_repo_mutation` is
  false for such a write; rule 4 gates on `canonical_tool_kind == "write"`
  and its paths, never on `canonical_repo_mutation`.
- `_evaluate_rules` (lines 273-801) calls `foreign_staged_commit_conflict`
  for every `before_tool` with a session, project id and project path, and
  `foreign_dirty_edit_conflict` when `canonical_tool_kind == "write"` and
  canonical paths exist (lines 555-589). Both imports are local to that
  block.
- An undefined name in a rule `when:` raises inside
  `TemplatingMixin._evaluate_condition`, and a `block` effect then fails
  closed, so the new key needs a default for every `RuleEngine.evaluate`
  caller, not only the hooks path.
- `utils/project_context.py::find_project_root(cwd)` resolves and walks up to
  `.gobby/project.json`; `get_project_context(root)` returns `id`, `name` and
  `project_path`.
- `task_claim_state.sanctioned_worktree_root(db, variables, root,
  root_project_id)` (delivered in 1.3) is the registered-worktree predicate,
  and `resolve_edit_pair` returns pairs under a sanctioned root. After 1.5 the
  recorder records them, and gate 3 checks them per root.
- `storage/worktrees.py::LocalWorktreeManager.list_worktrees(project_id=...,
  task_id=...)` returns `Worktree` rows; compare realpaths, because macOS
  `/tmp` resolves to `/private/tmp`.
- Memory a524ed54: cross-repo work uses `gobby-worktrees.create_worktree`
  with the target `project_path`, a writable `worktree_path` and the task id,
  and commits there.
- `hooks.py:422` says "Mirrors the baseline_dirty_files pattern below"; that
  pattern is gone since 1.2.
- Rule template shape (from `block-cross-session-foreign-staged-commit.yaml`):
  `tags`, then `rules.<name>` with `description`, `event: before_tool`,
  `enabled`, `priority`, `when: <key>` and `effects: [{type: block,
  reason: "{{ <key> }}"}]`.

**Implementation:**
- `build_ownership_eval_context(db, event, event_data, variables, *,
  session_id, project_path) -> dict[str, Any]` returns the keys the moved
  block sets today plus `cross_project_write_conflict`. `_evaluate_rules`
  calls it.
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
  `event: before_tool`, `enabled: false`, `priority: 24`,
  `when: cross_project_write_conflict`, block reason
  `"{{ cross_project_write_conflict }}"`. It ships disabled; V2 step 6
  records the toggle that enables it after Josh approves.
- Fix the `hooks.py:422` comment to describe the variable-defaults lazy init
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
- 1.6.2 - End to end through a registered worktree of another project bound to a claimed task, reached through a symlinked temp path: rule 4 allows writes there, yet rule 3 refuses a write to a dirty path the session does not own; a clean path's write passes, the recorder records the pair under the worktree's real root, and `capture_attribution` attributes that pair to the task under that root, so gate 3 inspects it. test: `tests/workflows/test_ownership_eval_context.py::test_sanctioned_worktree_write_obeys_rules_1_to_3`.
- 1.6.3 - `RuleEngine.evaluate` with an eval_context lacking `cross_project_write_conflict` evaluates the rule as not matching instead of failing closed. test: `tests/workflows/test_ownership_eval_context.py::test_rule4_default_key_keeps_unwired_callers_open`.
- 1.6.4 - The bundled `block-cross-project-write` template loads with event `before_tool`, `enabled: false`, priority 24 and a block effect carrying the key. test: `tests/workflows/test_task_enforcement_rules.py::test_block_cross_project_write_template_shape`.
- 1.6.5 - Rule 4 judges the file the tool names: with `event.cwd` in the session's checkout, a write to `p.py` with `workdir` in another project's checkout, a write to `../<other project>/p.py`, and a write through a relative symlink in the session's checkout that points into the other project are each refused, while the same relative write with `workdir` in the session's own task worktree is allowed. test: `tests/workflows/test_ownership_eval_context.py::test_rule4_resolves_workdir_relative_and_symlinked_paths`.

### 1.7 Rule 1: gate 3 checks the task's uncommitted work in every session [category: code] (depends: 1.4, 1.8)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_close_context.py::capture_attribution`
- `src/gobby/workflows/dirty_edit_grants.py::*` — scope-reason: adds task_recorded_pairs beside claimed_task_ledger_paths and makes checkout_path_owners machine-local
- `src/gobby/workflows/session_edit_ledger.py::SessionEditLedger.record_edited_files`
- `src/gobby/workflows/task_claim_state.py::*` — scope-reason: record_task_content_hashes gains the keyword-only unhashed paths it removes
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::*` — scope-reason: adds the cross-session gate 3 tests
- `tests/workflows/test_dirty_edit_grants.py::*` — scope-reason: adds the task_recorded_pairs tests, node-origin pairs and roots included

**Granularity:** one section with more than six acceptance items, by design.
`task_recorded_pairs` and gate 3's use of it verify only together: each
discovery rule exists for one gate 3 outcome.

**Research context:**
- `_lifecycle_close.py::_evaluate_close` (lines 57-218) sets the attribution
  owner to `get_claimed_session_id(task) or session_id`, calls
  `_close_context.capture_attribution`, and passes
  `attributed_checkouts=attribution.clean_checkouts` and
  `attribution_complete=attribution.complete` to
  `tasks/close_invariants.py::evaluate_close_invariants`. Gate 3 runs
  `task_dirty_paths_async` per checkout (lines 98-119): a dirty path fails it
  with "Commit task-attributed changes before closing." and
  `dirty_checkouts`, and an unavailable checkout or an incomplete
  attribution fails it with "Git could not inspect all task-attributed
  files."
- `capture_attribution` (lines 64-131) reads only the owner's variables:
  `task_live_checkout_files(variables, task.id)` gives the owner's live pairs
  by root. Per root it skips `is_ignorable_attributed_root` roots, keeps
  `committable_task_paths_async`, and subtracts, from the paths outside the
  owner's dirty set, those another live session holds dirty
  (`foreign_owned_dirty_paths_async(..., release_clean=False)`, the #24402
  exclusion, lines 106-129). So a previous session's uncommitted work for
  the task is never checked, and a live predecessor's dirt for the task is
  subtracted.
- `_lifecycle_close_finalization.py::_cleanup_closed_claim` (lines 176-207)
  merges `remove_claimed_task` into the owner only. Content records survive in
  every session.
- `dirty_edit_grants.py`: `claimed_task_ledger_paths(db, *, task_ids,
  checkout_root)` (lines 73-94) reads every session row whose
  `task_edited_checkout_hashes` holds one of the tasks (jsonb `?|`), for one
  root. `checkout_path_owners(db, *, checkout_root, paths)` (lines 97-137)
  maps each path to the task whose newest record matches its current
  fingerprint, across every session.
- A pair whose path cannot be fingerprinted when recorded gets no content
  record. `session_edit_ledger.py::SessionEditLedger.record_edited_files`
  takes its digests from `checkout_fingerprint.path_fingerprints`, which
  skips a path whose fingerprint raises `OSError` (an unreadable file or a
  directory), and `task_claim_state.record_task_content_hashes` returns
  early on empty digests. The pair still enters `session_dirty_file_checkouts`
  and `task_edited_file_checkouts`, and an earlier record of the same path
  stays as a stale hash, which matches again if the file returns to those
  bytes. `state_manager.py` (879 lines) also calls
  `record_task_content_hashes`, with the digests of adopted shell edits. `checkout_path_owners` compares each record with
  `dirty_edit_grants._current_digest`, which returns "unreadable" on
  `OSError`, so it never maps such a pair to a task. Only three functions
  write `task_edited_file_checkouts`: `record_edited_files` adds,
  `transfer_live_checkout_paths` moves a pair within a session, and
  `remove_claimed_task` drops a closed task's entry.
- `ledger_reconcile.reconcile_edit_ledgers` releases only its own session's
  `session_dirty_file_checkouts` entries that `task_dirty_paths_async`
  reports clean, and leaves `task_edited_file_checkouts` alone.
  `task_dirty_paths_async` calls `daemon_git.status`, which runs on the node
  under `checkout_reads.node_origin()` (`utils/daemon_git.py`, lines
  430-483), so a node session's own reconcile releases its pairs by the
  node's git. Nothing reconciles an ended session's ledger.
- `checkout_path_owners` reads `session_variables` with no machine filter
  (line 114) and compares each record with this machine's disk
  (`_current_digest`, line 131). Its other callers are
  `claimed_task_carriers` (rule 3), `claimed_task_owned_paths`
  (`state_manager.py`'s adoption of shell edits) and
  `commit_guard._live_path_owners`. `capture_attribution` takes the close
  session's `machine_id` only for `is_ignorable_attributed_root`, and runs
  the linked-commit fallback when the owner's `task_edited_file_set` and
  live pairs are both empty and linked commits exist.
- `tasks/attributed_roots.py::is_ignorable_attributed_root` treats a git
  checkout as inspectable and a missing registered root as fail-closed.

**Implementation:**
- Add `dirty_edit_grants.TaskRecordedPairs`, a `NamedTuple` of four maps
  from normalized checkout root to paths, and
  `dirty_edit_grants.task_recorded_pairs(db, *, task_id) ->
  TaskRecordedPairs`: one query for the session rows whose
  `task_edited_checkout_hashes` or `task_edited_file_checkouts` holds the
  task (jsonb `?`), joined to `sessions` for each row's `machine_id`. A row
  whose session is on this machine (`get_machine_id()`) feeds:
  - `content`: every pair its `task_edited_checkout_hashes` entry for the
    task records;
  - `fallback`: every pair of its `task_edited_file_checkouts` entry for the
    task that its hash entry for the task lacks, or whose `_current_digest`
    is "unreadable".

  A row whose session is on another machine feeds:
  - `remote`: every pair either entry records for the task;
  - `remote_held`: the `remote` pairs that the same row's
    `session_dirty_file_checkouts` still holds under that root, matched by
    `overlapping_task_edit_paths` (`task_edit_path_covers` either way), so a
    held directory entry holds a recorded child and a held child holds a
    recorded directory.

  A root whose directory is gone is dropped from `content` and `fallback`.
  `remote` and `remote_held` keep every root, because the hub cannot see
  another machine's directories (Decision 4).
- `record_task_content_hashes` takes a keyword-only `unhashed: Iterable[str]
  = ()` and removes those paths from the task's hashes under the root; it
  returns early only when both `digests` and `unhashed` are empty.
  `record_edited_files` passes the recorded paths that `path_fingerprints`
  skipped, so a pair recorded without a fingerprint keeps no stale hash and
  stays a fallback pair after its file is readable again. The
  `state_manager.py` call is unchanged. For a node-origin event
  (`checkout_reads.node_origin()`), `record_edited_files` takes no
  fingerprints and passes every recorded path as `unhashed`, because the
  hub's file at that root and path is not the node's (Decision 4).
- `checkout_path_owners` returns an empty map for a node-origin event
  (`checkout_reads.node_origin()`), and otherwise reads only the records of
  sessions on this machine (join `sessions`, `machine_id =
  get_machine_id()`), because it compares them with this machine's disk.
  This one guard covers every caller: `_live_path_owners` gives no
  `ledger_task_id` during a node-origin event, `claimed_task_carriers` finds
  no carrier there, `claimed_task_owned_paths` adopts no shell edit there,
  and a node session's record never names the owner of the hub's bytes
  (Decision 4).
- In `capture_attribution`, after reading the owner's live pairs:
  1. Read `task_recorded_pairs(db, task_id=task.id)`, which has already
     dropped this machine's roots whose directory is gone (Decision 22), and
     the owner session's `machine_id`. When the owner's session is on
     another machine, its live pairs leave the hub path before any read;
     `task_recorded_pairs` already lists them under `remote` from the
     owner's row (Decision 4).
  2. When `remote_held` is not empty, log its roots and paths and clear
     `complete`, so gate 3 fails "Git could not inspect all task-attributed
     files." No `remote` pair reaches `checkout_path_owners`, the attributed
     paths or the hub's status.
  3. For every root among the owner's remaining live pairs, `content` and
     `fallback`, call `checkout_path_owners` once over that root's paths.
     The task-owned set is the content pairs it maps to the closing task
     plus the fallback pairs it maps to no other task.
  4. Add each root's task-owned paths to the attributed paths of that root.
  5. Run the linked-commit fallback only when no session on any machine
     recorded a pair for the task: the owner's `task_edited_file_set` and
     live pairs are empty and all four maps are empty. So a remote owner or
     predecessor whose pairs have settled never sends the hub's copy of the
     linked commits' paths to gate 3.
  6. Pass `foreign_owned_dirty_paths_async` only the committed paths outside
     the task-owned set, so a path whose content the closing task owns is
     never subtracted.
  Any exception from `task_recorded_pairs` or `checkout_path_owners` is
  logged and clears `complete`, so gate 3 fails closed instead of passing
  over records it could not read.
- Gate 3's code and texts are unchanged.

Consumers unchanged:
- `src/gobby/tasks/close_invariants.py` — no-edit-reason: receives the enlarged attributed checkouts through its existing parameter
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` — no-edit-reason: calls capture_attribution with an unchanged signature
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_finalization.py` — no-edit-reason: commit_close re-evaluates through _evaluate_close, which calls capture_attribution unchanged
- `tests/mcp_proxy/tools/tasks/test_close_candidate.py` — no-edit-reason: patches capture_attribution with a fixed CloseAttributionSnapshot
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py` — no-edit-reason: patches capture_attribution with a fixed CloseAttributionSnapshot
- `src/gobby/workflows/state_manager.py` — no-edit-reason: calls record_task_content_hashes without the new keyword, and claimed_task_owned_paths, whose empty answer for a node-origin event already means no shell edit to adopt

**Focused verification (planned):** run
`tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py`,
`tests/mcp_proxy/tools/tasks/test_close_attribution.py`,
`tests/workflows/test_dirty_edit_grants.py` and
`tests/tasks/test_close_invariants.py` with the isolation prefix.

**Acceptance:**

- 1.7.1 - An ended predecessor session recorded `p` for task T and left it dirty, and the owner has no record of `p`; gate 3 for T fails with "Commit task-attributed changes before closing." and lists `p` under its checkout, and passes once `p` is committed. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_blocks_on_ended_predecessor_dirt_for_the_task`.
- 1.7.2 - A live predecessor that released T still holds T's dirty `p`; gate 3 fails on `p` instead of subtracting it as foreign. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_keeps_live_predecessor_dirt_for_the_task`.
- 1.7.3 - #24402 holds: after T committed `p`, a live session dirtied `p` for task U with a recorded edit, and another live session dirtied `q` with no task record; gate 3 for T passes. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_ignores_later_foreign_dirt_on_committed_paths`.
- 1.7.4 - A predecessor's record of `p` for T whose current content task U's later recorded edit owns adds nothing to T's attributed paths. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_skips_recorded_pairs_another_task_now_owns`.
- 1.7.5 - A recorded pair under a deleted checkout root is dropped and never makes gate 3 unavailable, while a failing `task_recorded_pairs` read fails gate 3 with "Git could not inspect all task-attributed files." test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_drops_deleted_roots_and_fails_closed_on_unread_records`.
- 1.7.6 - `task_recorded_pairs` returns as content pairs every root and path that any session's `task_edited_checkout_hashes` recorded for the task, and nothing recorded only for other tasks. It returns as fallback pairs a live pair of the task whose session holds no hash for it, a live directory pair, and a live pair whose stale hash names a file that is now unreadable, and it omits a live pair whose recorded hash belongs to a readable file. test: `tests/workflows/test_dirty_edit_grants.py::test_task_recorded_pairs_reads_every_session`.
- 1.7.7 - An escalated predecessor recorded, for T, a file `p` through the status fallback while `p` was unreadable and a directory `d` that now holds an untracked file, neither with a content record. After B claims T, gate 3 fails instead of passing over either pair. Once `p` is readable again but uncommitted, gate 3 still fails. B commits both through the guarded path, a Bash `git commit` event that `foreign_staged_commit_conflict` passes while the predecessor lives (1.8), never a raw git commit; gate 3 then passes. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_checks_predecessor_pairs_recorded_without_content`.
- 1.7.8 - Stale-hash recovery: T's predecessor session records readable `p` (hash H1), and a later recorded write changes `p` and leaves it unreadable, which removes that session's hash of `p` for T while its other paths keep theirs. After B claims T and `p` is made readable again without a byte change, gate 3 fails on the uncommitted `p`, then passes once B commits `p` through the guarded path (a Bash `git commit` event that `foreign_staged_commit_conflict` passes while the predecessor lives). test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_keeps_pairs_whose_stale_hash_was_dropped`.
- 1.7.9 - Inside `checkout_reads.delivery_scope` for a node machine, `record_edited_files` records T's pair `p` with no content hash although the hub holds a readable file at that root and path, and drops that session's older hash of `p` for T. `task_recorded_pairs` lists `p` under `remote` and `remote_held`, never under `content` or `fallback`, and drops `p` from `remote_held` once that session's dirty ledger releases it. A held directory entry `d` holds a recorded `d/f`, and a held `d/f` holds a recorded `d`. It keeps a root a node-machine session recorded although the hub has no such directory, and drops a hub-machine session's root whose directory is gone. test: `tests/workflows/test_dirty_edit_grants.py::test_task_recorded_pairs_keeps_node_origin_pairs_and_roots`.
- 1.7.10 - Node-recorded pairs at close: node session N, recording inside `checkout_reads.delivery_scope` for its machine, holds T's dirty `p` under root R, and the hub also has a git checkout at R. Case (a): N is T's attribution owner. Case (b): N has ended, and live hub session B owns T. In each case, with the hub's `p` clean, and again with the hub's `p` matching hub session H's content record for task U, gate 3 for T fails with "Git could not inspect all task-attributed files.", never counting `p` clean or dropping it. In (b) it stays so, which is the ceiling #25284 owns. In (a), once N's own ledger reconcile releases `p`, run inside N's delivery with the node's status reporting `p` clean, gate 3 passes, and a linked commit whose paths are dirty in the hub's checkout does not trigger the linked-commit fallback. test: `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py::test_gate3_fails_closed_on_held_node_pairs_and_ignores_hub_bytes`.
- 1.7.11 - `checkout_path_owners` ignores a node-machine session's content record even though it matches the hub's bytes at that root and path. Inside a node-machine `delivery_scope` it answers an empty map even though a hub session's record matches, so `claimed_task_carriers` finds no carrier there. test: `tests/workflows/test_dirty_edit_grants.py::test_checkout_path_owners_is_machine_local`.

### 1.8 Rule 2 lets a claimant commit its claimed task's dirt and refuses whole-index commits in a shared checkout [category: code] (depends: 1.3, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/workflows/commit_guard.py::*` — scope-reason: rules 2 and 3 skip a holder when its interval task or its content-owner task is one the session claims, testing each field on its own, _live_path_owners takes each holder's task from its live interval whether or not it is claimed, rule 2 refuses a whole-index commit while another live session holds dirt in the checkout, and _operation_paths moves out
- `src/gobby/workflows/git_operation_paths.py`
- `src/gobby/workflows/dirty_edit_grants.py::*` — scope-reason: renames _claimed_task_ids to the public claimed_task_ids and updates its two callers
- `tests/workflows/test_commit_guard.py::*` — scope-reason: adds the rule 2 claimed-task and whole-index tests
- `tests/workflows/test_git_operation_paths.py`

Split `src/gobby/workflows/commit_guard.py` (863 lines): move `_operation_paths`
(lines 429-485, with its nested `git` and `git_ok` helpers) unchanged into the
new `src/gobby/workflows/git_operation_paths.py` as `operation_paths`, and
import it in both rules.

**Research context:**
- Rule 2, `commit_guard.foreign_staged_commit_conflict` (lines 121-275),
  collects candidates per checkout, drops the session's own dirty paths
  (`_current_owned_paths`, lines 614-621) and the in-progress operation's
  paths (`_operation_paths`, merge, cherry-pick, revert or rebase), scopes the
  live holders (`scope_foreign_owners`), dirty-checks them while releasing
  clean stale holders (`_dirty_owned_paths_releasing_clean`), and adds every
  dirty path's holders to the conflicts (line 261). No holder is exempt for
  its task.
- Rule 3, `foreign_dirty_edit_conflict` (lines 278-398), reads `own_tasks`
  from `dirty_edit_grants.claimed_task_carriers` and drops holders whose
  `owner_task_id or ledger_task_id` is in `own_tasks` (lines 365-370), "a
  holder working a task this session now claims is this session's past
  self". Python's `or` tests only the first nonempty field, so a holder's
  interval task masks a content owner the session claims (Adversary
  CO-A5-F1).
- `commit_guard._live_path_owners` (lines 498-583) builds each holder's
  `ForeignPathOwner`. `owner_task_id` comes from `_live_interval_task`
  (lines 586-611), which scans only the holder's `claimed_tasks`, else from
  the first claimed task whose `task_edited_file_checkouts` holds the path;
  `task_ref` is `str(claimed_tasks[task_id])`. `ledger_task_id` is
  `dirty_edit_grants.checkout_path_owners` (lines 97-137), the content-hash
  owner. Escalation releases T from the holder's `claimed_tasks`
  (`_escalation_coordinator.clear_prior_claim_session_variables`, then
  `task_claim_state.release_claimed_task`) and keeps
  `task_live_edit_starts` and `task_edited_file_checkouts`. So for a pair
  with no content record (an unreadable file, a directory, a node-origin
  write) an escalated holder yields neither field, and rules 2 and 3 refuse
  T's claimant. `remove_claimed_task` (T closed) drops T's interval.
- Other readers of the two fields: `commit_guard_conflicts.py` renders
  "task <ref>" when `owner_task_id` is set and "no claimed task" otherwise;
  `agents/worktree_checkpoint.py::checkpoint_agent_worktree` treats an owner
  as foreign when its `task_ref` differs from the checkpointed task's
  `#<seq_num>`, unless `owner_task_id` is None and the holder is a recovery
  session. A claim's ref is `#<seq_num>`, else the task id
  (`workflows/observers.py`).
- `dirty_edit_grants._claimed_task_ids(db, session_id)` (lines 65-70) reads
  the tasks whose `claimed_by_session_id` is the session; only
  `claimed_task_carriers` and `claimed_task_owned_paths` call it.
- No test references `_operation_paths`; rule 3's merge tests exercise it
  (`tests/workflows/test_commit_guard.py::test_rule3_exempts_merge_set_paths_during_merge`).
- Rule 2 builds its candidates per `git_commit_parsing.GitCommitInvocation`.
  A path-scoped invocation (`pathspecs` set) lists its paths with `git
  ls-files`; one without pathspecs, or with `include_staged`, adds the
  `git diff --cached` set read when the hook runs; `all_tracked` adds `git
  diff HEAD`. With no live holder in the checkout (`_live_path_owners`
  empty) it passes at once. The shell runs the commit after the hook
  answers, so a path staged in between is never a candidate. Commit
  2068aaf9a0 (2026-10-28) recorded a path another live session had staged
  (Decision 23).

**Implementation:**
- Rename `_claimed_task_ids` to `claimed_task_ids` and update its two
  callers.
- In `_live_path_owners`, `_live_interval_task` scans every task in the
  holder's `task_live_edit_starts` instead of its `claimed_tasks` (the
  `claimed_tasks` parameter goes; ranking is unchanged, and a task with no
  recorded selection still ranks oldest). The fallback scans every task in
  the holder's `task_edited_file_checkouts`. `task_ref` is the claim's ref
  when the holder claims the task, else `#<seq_num>` (the task id when it has
  none) from one `SELECT id, seq_num FROM tasks WHERE id = ANY(%s)` per call
  over the unclaimed tasks named. Rules 2 and 3 both read these owners, so
  rule 3's past-self exclusion covers a released holder too, and the
  conflict texts name the released task's ref.
- In `foreign_staged_commit_conflict`, read `claimed_task_ids(db,
  session_id)` once per call (in a thread) and add a dirty path's holder to
  the conflicts only when neither its `owner_task_id` nor its
  `ledger_task_id` is among them, each field tested on its own. Candidate
  collection, the stale-holder release and the rule 2 text are unchanged.
- In `foreign_dirty_edit_conflict`, the past-self exclusion drops a holder
  when its `owner_task_id` is in `own_tasks` or its `ledger_task_id` is in
  `own_tasks`, replacing the `or` fallback (Decision 15).
- In `foreign_staged_commit_conflict`, an invocation that records the whole
  index (`not invocation.is_path_scoped or invocation.include_staged`),
  while no merge, cherry-pick, revert or rebase is in progress (the states
  `operation_paths` reads), is refused when
  `_dirty_owned_paths_releasing_clean` over every holder that
  `_live_path_owners` names for the checkout returns any path. Holders the
  claimed-task exclusion skips count too, because a pathspec commit is
  always available to the claimant. The refusal is
  `format_conflict_reason` over those holders followed by "This commit
  records the whole index, which can include their staged files. Commit
  only your own paths: `git commit -m <msg> -- <path>...`." Path-scoped
  invocations and an operation's conclusion keep the per-path check
  unchanged (Decision 23).
- Move `_operation_paths` to `git_operation_paths.operation_paths` unchanged.

**Focused verification (planned):** run `tests/workflows/test_commit_guard.py`,
`tests/workflows/test_git_operation_paths.py`,
`tests/workflows/test_dirty_edit_grants.py`,
`tests/hooks/test_node_origin_edits.py` and
`tests/agents/test_terminal_timeout_checkpoint.py` with the isolation
prefix; the last two pin `_live_path_owners`' machine scope and the
checkpoint's foreign-owner outcomes.

**Acceptance:**

- 1.8.1 - Session C claims task T, and live session P, which escalated T and so no longer claims it, holds dirty `p` with T's live interval and T's content record; rule 2 lets C commit `p`, while a dirty path P holds for task U, which C does not claim, is refused with the rule 2 text naming U's `#<seq_num>`. test: `tests/workflows/test_commit_guard.py::test_rule2_allows_commit_of_claimed_task_dirt_held_by_live_predecessor`.
- 1.8.2 - `operation_paths` imports from `git_operation_paths` and returns the merge set while `MERGE_HEAD` exists and an empty set when no operation is in progress. test: `tests/workflows/test_git_operation_paths.py::test_operation_paths_lists_merge_set_only_during_merge`.
- 1.8.3 - Fallback pairs held by a live escalated predecessor: in a temporary git checkout, live session P recorded for T, through the status fallback and so with no content record, a file `p` while it was unreadable and a directory `d` holding an untracked file, then escalated T. After C claims T and `p` is readable again, `_live_path_owners` names T, with T's `#<seq_num>` ref, as P's `owner_task_id` for both pairs while `ledger_task_id` is None. Rule 3 lets C edit `p`. C's Bash `git add p d && git commit -m m` event gets "" from `foreign_staged_commit_conflict` before the commit runs, and the commit then lands. A pair P holds for task U still refuses C's commit, naming U. test: `tests/workflows/test_commit_guard.py::test_claimant_commits_fallback_pairs_held_by_escalated_predecessor`.
- 1.8.4 - Interval and content owners differ: live session P holds dirty `p` under T's live interval, and ended session Q's content record for U is the newest one matching `p`'s current bytes, so `_live_path_owners` gives P `owner_task_id` T and `ledger_task_id` U. Session C, which claims U, may edit `p` under rule 3 and gets "" from `foreign_staged_commit_conflict` for `git add p && git commit -m m`. Session D, which claims neither T nor U, is refused by both rules, and both texts name T's `#<seq_num>`. test: `tests/workflows/test_commit_guard.py::test_claimed_task_exclusion_tests_interval_and_content_owners_separately`.
- 1.8.5 - Shared-index sweep, plan files included: in a temporary git checkout, live session P, whose `_agent_type` is `plan-writer`, recorded and staged `.gobby/plans/other.md`. Session C's Bash `git add p && git commit -m m` gets the whole-index refusal naming P before the commit runs, and so do `git commit -a -m m` and `git commit -i -m m -- p`. C's `git commit -m m -- p` gets "", and the commit records only `p` while `.gobby/plans/other.md` stays staged. test: `tests/workflows/test_commit_guard.py::test_rule2_refuses_whole_index_commit_while_another_session_holds_dirt`.
- 1.8.6 - With no other live holder in the checkout, and again once P's only pair is committed so the stale-holder release clears it, C's `git commit -m m` and `git commit -a -m m` get "". During a merge (`MERGE_HEAD` set), C's `git commit --no-edit` that concludes it passes the whole-index check, while a staged path outside the merge set that P holds dirty still refuses it with the rule 2 text. test: `tests/workflows/test_commit_guard.py::test_rule2_allows_whole_index_commit_alone_and_merge_conclusion`.

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
- 2026-10-05: Adversary finding CO-A3-F10 accepted. `session_tasks` links
  are best-effort: `claim_task` and `create_task(claim=true)` catch a link
  failure and still set `claimed_tasks`. Link-based predecessor discovery
  could therefore miss a tag holder at transfer, retry, gate 9 and close
  cleanup. The task tag is now the recovery authority. The new
  `EditLedgerMixin.task_tag_holders` is one jsonb query over
  `session_variables`. It replaces `_task_attribution_sessions`, which 1.1
  deletes, and `_transfer_task_dirty_pairs` drops its `prior_claimer`
  parameter (Decision 2, Decision 8, 1.1, 1.4, 1.8). An inventory failure
  fails gate 9 closed as `task_clean_proof_unavailable` (1.7). New
  regressions: 1.4.11 (unlinked holders found), 1.7.6 (fail-closed). The
  1.7.5, 1.8.6 and 1.4.10 predecessors are now unlinked. The sweep also
  updated the Retired Mechanisms claim-scope bullet and the Overview.
- 2026-10-05: V1 consensus. The Adversary (gobby#15470) confirmed
  consensus on `a75b9beee4` with CO-A3-F1 to F10 resolved and no remaining
  findings, after verifying the committed diff, the source call sites and a
  clean base validation. Decisions 1 (R1, landing-merge exception) and 12
  (W3, merge-set exemption from rule 3) remain flagged for Josh at
  approval. M1 is derived next by the Adversary through the
  handoff-manifest route.
- 2026-10-05: M1 withdrawn. CR7 bounced `9ec173bf81` (sha256 `c0146fd8`)
  with B1, which changes acceptance 1.2.3, so the M1 derived from
  `1b12887613` is stale. The whole `## M1 Task Manifest` block is removed
  unedited and its bytes stay in Git history (stale-M1 route, memory
  f5577ae0). The Adversary re-derives M1 after the repair.
- 2026-10-05: CR7 bounce repairs, with Josh's approval of Decisions 1 and
  12 (Telegram, 15:54 CT, relayed by the Orchestrator) recorded in the
  Decision Record.
  - B1: acceptance 1.2.3's sweep excludes `src/gobby/workflows/hooks.py`,
    whose line 408 comment 1.6 fixes after 1.2 closes. V2 step 2 keeps the
    full sweep.
  - LOW-1: the 1.3 `commit_guard.py` split names its symbol list as
    governing; `ForeignPathOwner`, `CheckoutPathOwnership` and
    `DirtyEditOwnershipInspectionError` stay.
  - LOW-2 (Orchestrator ruling: drop): gate 9 drops an owner's live pair
    under a deleted checkout root, as reconciliation and
    `untransferred_task_pairs` do, and 1.7.4 pins it. Naming the root in
    the unavailable text was the alternative, rejected because nothing can
    commit such a pair.
  - LOW-3: 1.5.4's test is renamed
    `test_git_restore_index_only_pathspecs_are_execute_kind`.
- 2026-10-05: Renewed V1 consensus. The Adversary (gobby#15470) confirmed
  consensus on `f1a15538e8` (sha256 `a7c8f55a`) with no remaining
  findings, after verifying the exact M1 withdrawal, the complete repair
  diff and a clean base validation. Josh's approval carries to the repaired
  commit (16:00 CT, relayed by the Orchestrator). The Adversary re-derives
  M1 through the handoff-manifest route; routing stays eight backend code
  leaves with TDD.
- 2026-10-28: Refresh under #25261 by the Plan Writer (gobby#18327) against
  `0.5.0` at `b42c2172d9`; no `src/` or `tests/` file changed through
  `beb0966eff`. 1.1 to 1.3 are delivered (#23562, #23563 and #23564) and
  keep their text. The v4 close rewrite renamed gate 9 to gate 3 and deleted
  gate 12 with its transcript evidence, and content-hash ownership (#24402 to
  #25161) made the per-task content records the ownership authority, so 1.4
  to 1.8 were re-targeted under their IDs. 1.4 retires the ledger state v4
  left without readers (Decision 8, the #23565 layout). 1.5 keeps the
  fingerprint recorder and adds the write-path union fix (Decision 20,
  #23566 kept). 1.6 keeps rule 4 (Decision 21, #23567 kept). 1.7 makes gate
  3 check the task's content-owned pairs from every session (Decision 22,
  #23568). 1.8 retires the claim transfer and gives rule 2 rule 3's
  claimed-task exclusion (Decision 2, #23569 kept with a new scope).
  Decisions 5, 6, 10, 15 and 16 were refreshed. The stale
  `## M1 Task Manifest` was withdrawn unedited, so the Adversary re-derives
  it (memory f5577ae0).
- 2026-10-28: V1 consensus on the #25261 refresh. The Plan Enhancer
  (gobby#18345) proposed CO-E4-01 (V2 step 5 runs Decision 2's deadlock case
  through `escalate_task` and `de_escalate_task`) and CO-E4-02 (V2 step 1 is
  one exact pytest command). The Orchestrator accepted both through Triage
  (gobby#17480), and they landed in `5f4c40c6b9`. The Plan Adversary
  (gobby#18336) raised CO-A4-F1 (nit: `PreWriteDigests.take` ignored the
  one-hour expiry), fixed in `0987242205`. It also raised CO-A4-F2
  (blocking: a pair recorded without a content fingerprint, an unreadable
  file or a directory, escaped 1.7's discovery after a handoff, including a
  stale hash once the file was readable again). `0987242205` and
  `ac23717515` fixed it with fallback-pair discovery from the task's live
  interval, the directory ruling in Decision 22, and dropping the task's
  stale hash when a recorded write cannot fingerprint a path. The Adversary
  confirmed consensus on `ac237175152ccefc84f61420c83f72e9c50369f9` with no
  remaining findings and a clean project-aware validation. Routing stays
  eight backend code leaves with TDD and task type feature. Josh's approval
  through the Assistant precedes work on #23565 to #23569 (#25261
  criterion 3).
- 2026-10-28: Orchestrator review of the stamp `23f2051eeb`, relayed by
  Triage (gobby#17480), returned it with five findings. The stale
  `## M1 Task Manifest` was withdrawn unedited (memory f5577ae0).
  - ORCH-REV-CO-1 and CO-2 (blocking): `_live_path_owners` took a holder's
    task only from its claims, and `ledger_task_id` is the content-hash
    owner, so a pair with no content record that an escalated predecessor
    holds named no task, and rules 2 and 3 refused its claimant while gate
    3 required the commit. 1.8 now takes the holder's task from its live
    interval, claimed or not, with a `#<seq_num>` ref for a released task,
    and adds 1.8.3. Decisions 2 and 15 and 1.8.1 are corrected. 1.7.7 and
    1.7.8 commit through the guarded path, and V2 step 5 repeats the live
    case without a content record.
  - ORCH-REV-CO-3 (blocking): #23277 is partly delivered. Decision 4 is
    rewritten: node-origin events store no snapshot and take the no-entry
    status on the node through `checkout_reads.run_git` (1.5, 1.5.15, and
    the Content-Hash Contract), node-origin pairs get no content record,
    and gate 3 keeps roots that a node session recorded (1.7, 1.7.9). No
    open #23277 leaf edits `commit_guard.py`, `task_claim_state.py` or
    `_tool.py`, so no section gained a `(depends: #N)`. The Orchestrator
    accepted that.
  - ORCH-REV-CO-4: rule 4 ships `enabled: false`, and V2 step 6 records the
    toggle that runs only after Josh approves through the Orchestrator.
  - ORCH-REV-CO-5: a Constraints bullet orders the S2.11 and tasks-family
    3.7 port sections after the leaves of this plan that change their
    sources.

  The Orchestrator withdrew its "No Python" ruling for this plan, which is
  a rules refresh and not a port. During this round, two found-work items
  reached Triage. #25280 (the `SessionEditLedger` fingerprint race) was
  filed. The other: another session's pre-commit stash and restore in the
  shared checkout reverted an edit made during its run. WIP checkpoints
  `2d900c772b` and `c11943c` restored attribution. Facts were verified
  read-only at `c11943c`.
- 2026-10-28: Adversary review of `4d94bebe1d`. Plan Adversary gobby#18336
  ended before reviewing it. The Orchestrator relaunched the seat, and Plan
  Adversary gobby#18407 raised two findings.
  - CO-A5-F1 (blocking): rules 2 and 3 tested `owner_task_id or
    ledger_task_id`, so a holder's interval task masked a content owner the
    session claims. 1.8 now tests each field on its own in both rules and
    adds 1.8.4 (`fb9ff9a3f5`). The Adversary confirmed the fix.
  - CO-A5-F2 (blocking): 1.7 discarded which machine recorded a pair, so
    the hub's digests and status could drop or pass a pair a node session
    recorded. Content ownership is now machine-local
    (`checkout_path_owners`), and `task_recorded_pairs` keeps remote pairs
    apart. A remote pair that its recording session's dirty ledger still
    holds, matched by `task_edit_path_covers` overlap, fails gate 3 as
    unavailable. A remote owner or predecessor never triggers the
    linked-commit fallback. Decisions 4, 15 and 22, the Content-Hash
    Contract, 1.7.9 and the new 1.7.10 and 1.7.11 carry it. The Orchestrator
    ruled on the remaining ceiling through Triage (option A): a pair an
    ended node session still holds keeps gate 3 unavailable until
    restraint-deferral #25284 lands, after #24521.
  - Orchestrator found work: the WIP commit `2068aaf9a0` also recorded
    another live Plan Writer's staged
    `.gobby/plans/external-mcp-transport-multiplexer.md`. Decision 23 and
    1.8 (1.8.5, 1.8.6 and V2 step 7) make rule 2 refuse a whole-index
    commit while another live session holds dirt in the checkout. Until
    then, seats commit with `git commit -- <path>`.

  WIP checkpoints: `fb9ff9a3f5`, `2068aaf9a0`, `bd2663f7f9` and `6dad31d`.
  One read during another session's pre-commit stash window (#25282)
  showed the file at its committed bytes; every edit was intact afterwards.
  Facts were verified read-only at `6dad31d`.

## V2: Verification
`kind: verification`

After every leaf has landed:

1. Run the focused suites of 1.4 to 1.8 together:
   `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_session_variable_manager.py tests/workflows/test_task_claim_state.py tests/workflows/test_commit_guard.py tests/agents/test_terminal_timeout_checkpoint.py tests/mcp_proxy/tools/tasks/test_close_attribution.py tests/mcp_proxy/tools/tasks/test_coordinator_claim_release.py tests/workflows/test_session_defaults.py tests/tasks/test_close_invariants.py tests/hooks/test_tool_handlers.py tests/hooks/test_pre_write_digests.py tests/hooks/test_normalization.py tests/hooks/test_normalization_canonical_mutation_scope.py tests/workflows/test_ownership_eval_context.py tests/workflows/test_task_enforcement_rules.py tests/workflows/test_hooks.py tests/workflows/engine/test_unavailable_tool_input_guards.py tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py tests/workflows/test_dirty_edit_grants.py tests/workflows/test_git_operation_paths.py -v`
2. `rg -n "release_task_paths|inspect_task_path_ownership|release_task_edited_files|baseline_dirty_files|task_edited_file_times|task_edited_file_checkouts_history|task_edited_checkout_history_paths|other_task_edited_checkout_paths|task_checkout_paths_in_window|task_edited_file_set_with_history" src/ docs/guides docs/reference-audit`
   prints nothing.
3. After the Orchestrator's daemon restart syncs the templates, the installed
   rows `block-cross-session-foreign-dirty-edit` and
   `block-cross-session-foreign-staged-commit` are enabled with priorities 26
   and 25, and the installed row `block-cross-project-write` exists disabled
   with priority 24.
4. In a scratch checkout of this project, a session's `git restore --staged`
   on a pre-staged path records nothing, and a later `close_task` passes gate
   3 (#22642 shape, live).
5. Decision 2's deadlock case, live: in a scratch checkout, live session A
   claims task T and writes `p`. A calls `escalate_task` on T, which clears
   the claim (`storage/tasks/_transitions.py::escalate_task`) while A stays
   live and keeps `p` dirty under T. The task's creator calls
   `de_escalate_task` on T, and session B claims T. B's `close_task` fails
   gate 3 on `p`, B commits `p` past rule 2, and `close_task` then passes
   gate 3. Repeat with A's write recorded through the status fallback, so
   `p` carries no content record: B still edits `p` past rule 3 and commits
   it past rule 2 (ORCH-REV-CO-1).
6. Rule 4 toggle, only after Josh approves enabling it through the
   Orchestrator: the Orchestrator calls
   `gobby-workflows:toggle_rule(name="block-cross-project-write",
   enabled=true)`, confirms the installed row reads enabled, and confirms
   that a Write into another registered project's checkout is refused with
   the rule 4 text. Without that approval the row stays disabled and this
   step is not run.
7. Whole-index commits, live (Decision 23): in a scratch checkout, live
   session A writes and stages `q`. Session B's `git add p && git commit -m
   m` is refused with the whole-index text naming A, and B's `git commit -m
   m -- p` lands a commit recording only `p` while `q` stays staged.
