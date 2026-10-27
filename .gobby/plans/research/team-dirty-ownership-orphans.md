# #25161 criterion 1: which carrier condition failed for the #22604 file

Source: Triage relay (message `941a2335-3941-4938-9885-265615935a85`), relaying the
Assistant (gobby#15976) reads that Josh approved. Read-only. No further reads were made
for this file.

## Read 1: session_tasks rows for #22604

Task `6cff9fba-c13f-4ece-8b2b-6c57154fe7bc`, from `get_task_sessions`, oldest first, UTC.

| Time (UTC, 2026-10-25 unless noted) | Session | Row |
| --- | --- | --- |
| 10-25 00:44:29 | 1d3d81d7-5ebb-451a-a15d-47d44647dddb | claimed |
| 10-25 02:13:29 | bda0d13f-bb4c-4bfc-a953-0f95717161a1 | worked_on |
| 10-25 16:52:30 | 6d59ff00-a70a-4fc6-b020-c0a52209a688 | reviewing |
| 10-25 17:02:13 | 6d59ff00 | claimed |
| 10-25 17:02:49 | 6d59ff00 | worked_on |
| 10-25 22:16:40 | b966e398 (Triage) | de_escalated |
| 10-26 02:33:36 | 4beff261-2970-43bd-9e9b-a6359ea54474 | claimed |
| 10-26 20:42:49 | bda0d13f | claimed (row 75161, reused from 10-25) |
| 10-26 22:59:21 | 49fa73fa-d560-4c0e-b03a-9ce08f3ed088 | reviewing |
| 10-26 23:06:46 | 139d0f0a-25f1-4d63-97f2-248d337b8dff | closed |

bda0d13f's `claimed` row keeps id 75161 but takes the 10-26 timestamp. Its `worked_on`
row (75165) is from 10-25. The relay reads this as an upsert that rewrote the timestamp
on reclaim.

## Read 2: task_edited_file_checkouts for the #22604 file

File: `docs/evidence/community-labels-2026-09/study-rating-instructions-2026-10-26.md`.
Source: `get_variable` on each #22604-linked session.

- bda0d13f: recorded the file under #22604 (`6cff9fba`) in `task_edited_file_checkouts_history` only,
  as the last of 40 paths in worktree `task-22604-gate-generated-names-through-a-jev-choic`.
  Its live `task_edited_file_checkouts` is `{}`.
- 4beff261: live and history both list 20 paths. The file is not among them.
- 6d59ff00: live lists 12 paths. The file is not among them.
- 1d3d81d7, 49fa73fa (reviewer), 139d0f0a (closer): live `{}`.

## Failed carrier condition

Condition (a) (the caller's own session ledger) and condition (d) (`claimed_task_carriers`)
both read the live map. bda0d13f's live map was cleared at a commit, reclaim or continuity
boundary while the file stayed uncommitted. History kept the entry. The guard therefore
saw an unowned uncommitted file and refused the claimant's own edit.

Relay cause: an upsert on reclaim rewrote the row timestamp, consistent with this clearing.

## Relation to the candidate fix

The candidate in #25161 covers three shapes. `claimed_task_carriers` no longer requires
a `session_tasks` link, and `_live_path_owners` attributes a handed-off session's live
ledger to its task. Both also read `task_edited_file_checkouts_history` for the claimed
task. The #22604 shape (live map cleared, history retained, claimant is the same session)
is fixed: the claimant's history-only paths are carriers, and history attributes a
foreign session's live dirt to the claimed task. History never creates a holder.
