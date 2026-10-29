# Close verified work

Load before any task close, including no-work dispositions. The installed
definition's rule selectors, close reasons and task scope determine who may
close. A refused caller reports to its authorized coordinator. Discover
`close_task` on `gobby-tasks` and post-close
`review_task_memories` on `gobby-memory`. Tool schemas own parameters.

Authorized closers finish in this order:

1. Finish the task, resolve owned findings, and check its acceptance criteria.
2. Run focused validation after the final edit and follow every command to its
   definitive exit. Record exact commands and results for the reviewer. For
   changed Python tests, run the repository's test-types and test-quality audits
   over all affected tests; keep the suppression ratchet clean.
3. Stage only task paths and commit with a task reference. Preserve foreign work.
   Use `git commit --only -m '[<project_name>-#<task_number>] fix: describe the change' -- <task paths>`.
4. Call `close_task(task_id=..., commit_sha=..., changes_summary=..., preview=true)`.
   Include validation results in the summary so completion evidence is reviewable.
5. Repair every failed gate, then call the same close with `preview=false`.
   A successful close returns `closed=true` and atomically links an explicit
   candidate commit. Preview leaves task state and commit links unchanged.
6. After closing, call `review_task_memories` with the task and summary. Record
   only valuable durable knowledge.

## Who closes

- Selected closure rules must permit the call.
- `task_close_reasons` and `task_close_scope` constrain the enabled definition's
  permitted reasons and tasks; `required_review_capabilities` can require an
  approved review from a definition with the declared permissions.
- No-work dispositions additionally require `close_without_work`, or the
  operator-terminal exemption.
- A definition with `respect_task_protections` follows the project's
  `task_protection_policy` for foreign claims, labels and protected subtrees.
- `preview=true` is open to every seat and changes nothing.

## Five close gates

Leaves and epics use these gates, in order. `step_records` applies to non-epic closes.

| Gate | Required state |
| --- | --- |
| `children_closed` | Every child is closed. |
| `linked_commits` | At least one linked or prospective commit. A no-work close with no commit skips this gate. |
| `uncommitted_task_edits` | Every task-attributed path is clean in its recorded checkout; attribution must be complete. |
| `integration_ancestry` | Every linked or prospective commit resolves and is an ancestor of the authoritative integration branch. A no-work close with no linked commit skips this gate; with a linked commit, unlink each candidate first (see below). |
| `step_records` | When `task_close_policy` sets `enforce_step_records`, the approved records for the task's category. A no-work close skips this gate. |

The response exposes `gates`, `can_close`, `closed`, and resolved commit identities.
Task, caller, machine, and repository authorization are checked before the gates.
The final close re-evaluates current state before writing. A concurrent change
can require another preview or retry.

Close reasons are `done`, `moot`, `superseded`, and `wont_do`. Delivered work closes
with `done`. A missing no-work capability returns `no_work_close_forbidden`.
Definition reason and scope restrictions return `task_close_reason_forbidden`
and `task_close_scope_forbidden`; protected tasks return `task_protected`.
A no-work close is commitless and explains the disposition in `changes_summary`.
A no-work close with any linked commit fails `integration_ancestry`, landed or not.
Unlink each unlanded candidate first with
`unlink_commit(task_id, commit_sha, reason)`. The reason and the caller are recorded
as a task lifecycle event. `unlink_commit` refuses a commit already on the integration
branch, so landed work closes with `done`. A `superseded` close must name the
superseding task or commit in `superseded_by`, or it is refused with
`superseded_by_required`; the closing record stores it.
No-work closes do not waive child closure or attributed-file cleanliness. A no-work
close skips the `step_records` gate, so it needs no step records.

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
commit; a test record may cite any linked commit. A no-work close skips this gate.
`force` never bypasses a gate that applies.

Each step has one writer: `record_test_step`, `record_implementation_step`,
`record_review_step` and `record_closing_step`. A writer accepts a session only
when its agent definition's `task_step_writes` grants that step, and a review
approval only from a session that never claimed, implemented or edited the task.

Peer evidence a close depends on is recorded by the peer, not the closer. A
review LAND is the reviewer's approved `record_review_step` record for the exact
commit. `land_commit` requires an enabled definition granting `land_tasks` and
either a run bound to the task or a locally owned root session, and records a
daemon-attested landing receipt on the task. Nothing else records a close receipt.

Follow the project's assigned restart and activation owner. Announce a plain
restart or cutover globally, proceed, and report DAEMON BACK when healthy;
no worker/validator quiet-window acknowledgement is required. Honor configured
quiet hours and separately coordinate operations that can kill agent panes.

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

_Last verified: 2026-10-23_
