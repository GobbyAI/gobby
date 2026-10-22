# Close verified work

Load before any task close, including no-work dispositions. Discover `close_task`
on `gobby-tasks` and post-close `review_task_memories` on `gobby-memory`.
Tool schemas own parameters.

Finish in this order:

1. Finish the task, resolve owned findings, and check its acceptance criteria.
2. Run focused validation after the final edit and follow every command to its
   definitive exit. Record exact commands and results for the reviewer. For
   changed Python tests, run the repository's test-types and test-quality audits
   over all affected tests; keep the suppression ratchet clean.
3. Commit only task-owned paths with a task reference. Preserve foreign work.
4. Call `close_task(task_id=..., commit_sha=..., changes_summary=..., preview=true)`.
   Include validation results in the summary so completion evidence is reviewable.
5. Repair every failed gate, then call the same close with `preview=false`.
   A successful close returns `closed=true` and atomically links an explicit
   candidate commit. Preview leaves task state and commit links unchanged.
6. After closing, call `review_task_memories` with the task and summary. Record
   only valuable durable knowledge.

## Four close gates

Both leaves and epics use these invariants, in order:

| Gate | Required state |
| --- | --- |
| `children_closed` | Every child is closed. |
| `linked_commits` | At least one linked or prospective commit, or an explicit no-work reason. |
| `uncommitted_task_edits` | Every task-attributed path is clean in its recorded checkout; attribution must be complete. |
| `integration_ancestry` | Every linked or prospective commit resolves and is an ancestor of the authoritative integration branch. |

The response exposes `gates`, `can_close`, `closed`, and resolved commit identities.
Task, caller, machine, and repository authorization are checked before the gates.
The final close re-evaluates current state before writing. A concurrent change
can require another preview or retry.

Close reasons are `done`, `moot`, `superseded`, and `wont_do`.
The no-work reasons `moot`, `superseded`, and `wont_do` permit a commitless close.
Explain the disposition in `changes_summary`;
they do not waive child closure, attributed-file cleanliness, or ancestry checks
for commits already linked to the task.

Developers and independent reviewers remain responsible for acceptance criteria,
test quality, and validation results. The close tool does not score transcripts,
validate TDD proof, or spawn a reviewer. Follow the task's review, landing, and
activation workflow before requesting its final close.

Use `link_commit` to attach work while keeping the task open. Include every commit
that belongs to the task, and integrate those commits before closing. Closing the
last child may close eligible organizational ancestors; an explicit epic close
uses the same five gates. Never report an epic complete while children remain.

When the project's `task_close_policy` sets `enforce_step_records`, a non-epic
close also needs the approved step records that policy selects for the task's
category: implementation and closing always, plus test and review where
configured. Implementation, review and closing records cite the latest linked
commit; a test record may cite any linked commit. With no linked commit, records
carry the no-work disposition. `force` never bypasses them.

Each step has one writer: `record_test_step`, `record_implementation_step`,
`record_review_step` and `record_closing_step`. A writer accepts a session only
when its agent definition's `task_step_writes` grants that step, and a review
approval only from a session that never claimed, implemented or edited the task.

Peer evidence a close depends on is recorded by the peer, not the closer:
`record_close_receipt(task_id, kind, commit_sha, facts)` stores a daemon-attested
`independent_review_approval` (LAND of an exact commit) or `activation` receipt
on another session's task, authorized against the locked task row.
A session that has ever claimed the task can neither approve nor land it.
An `activation` receipt comes only from the task's creator or delegator. When
both have ended, or the live creator is the task's claimant and no delegator is
live, a live session in the task's project takes over that authority with
`transfer_task_authority(task_id, reason)`. It becomes the delegator, the
delegation receiver is kept, and the reason is appended to the delegation reason.
It refuses a closed task, the claimant, and any caller
while a creator that is not the claimant, or a delegator, is still live. A paused
holder counts as ended once every agent run linked to it ended or its recorded
process exited on this machine; a lost terminal or wake binding alone does not.
Re-running it is a no-op for the session already holding authority.

When a CLOSED/VALID task's `closed_commit_sha` differs from the explicit
`commit_sha` its original VALID close review accepted,
`repair_closed_candidate(task_id, review_id, expected_closed_commit_sha, reason)`
corrects only that marker to the review's full candidate. It previews by
default; pass `preview=false` to apply. It refuses unless the caller is a
registered, machine-identified session in the task's project, the task is
unclaimed, unescalated and outside integration, the marker still equals
`expected_closed_commit_sha`, and the linked commits still match the review.
Closure, review evidence and links are kept, and the correction is recorded as
a lifecycle event.

Guide: [Close](../../../../../../../../docs/guides/tasks.md#close).

_Last verified: 2026-10-22_
