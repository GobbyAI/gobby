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
3. Stage only task paths and commit with a task reference. Preserve foreign work.
   Use `git commit --only -m '[<project_name>-#<task_number>] fix: describe the change' -- <task paths>`.
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
| `integration_ancestry` | Every linked or prospective commit resolves and is an ancestor of the authoritative integration branch. A no-work close may instead waive unlanded candidates (see below). |

The response exposes `gates`, `can_close`, `closed`, and resolved commit identities.
Task, caller, machine, and repository authorization are checked before the gates.
The final close re-evaluates current state before writing. A concurrent change
can require another preview or retry.

Close reasons are `done`, `moot`, `superseded`, and `wont_do`.
The no-work reasons `moot`, `superseded`, and `wont_do` permit a commitless close.
Explain the disposition in `changes_summary`.
They do not waive child closure or attributed-file cleanliness. For linked commits,
a no-work close waives only unlanded candidates, which it treats as superseded or
waived. A linked commit already on the integration branch still fails
`integration_ancestry`; close such work with `done`. A `superseded` close with
unlanded linked commits must name the superseding task or commit in `superseded_by`,
and the closing record stores it as a task lifecycle event. `unlink_commit` refuses
commits already on the integration branch.

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

Peer evidence a close depends on is recorded by the peer, not the closer. A
review LAND is the reviewer's approved `record_review_step` record for the exact
commit. `land_commit` lands that commit only for the task's Dev Manager or the
root Orchestrator, never for the reviewer, and records a daemon-attested landing
receipt on the task. Nothing else records a close receipt.

A live release window holds every landing: `land_commit` refuses with
`release_window_active` and does not escalate, so retry after the window ends.
Only the Release Manager calls gobby-agents `acquire_release_window`,
`renew_release_window(token, generation)` and
`end_release_window(token, generation)`. The lease lasts 10 minutes from
acquire or the last renew, and each renew returns the next generation. An
unreadable `<git common dir>/gobby/release-window.json` refuses landings with
`release_window_unreadable` until an operator deletes that file. The same codes
refuse new work: `admit_team` for a team with no record, and a spawn that would
create a fresh worktree or clone. An admitted team keeps working: its admission
re-runs, replacement seats join its tree, an interrupted admission recovers its
tree, and spawns that reuse a tree proceed.

The Release Manager validates and activates the window's pinned candidate with
`gobby release batch --token <token> --generation <n> [--base-sha <sha>]`, run
in the background from its session. It refuses before touching the window
unless the caller owns it, the main checkout HEAD is the pinned candidate, and
the baseline (the last succeeded batch, else `--base-sha`) is an ancestor of
it. It then renews the lease while it runs the checks the diff selects,
activates by cutover or restart, and smokes the daemon. Each batch writes a
record under `<git common dir>/gobby/release-batches/` and then ends the
window if it still owns it. A success becomes the next baseline; a failure
keeps the old one and files one regression task per candidate.

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
