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

Shapes 1 and 2: `claimed_task_carriers` no longer requires a `session_tasks` link, and
`_live_path_owners` attributes a handed-off session's live ledger to its task.

The #22604 shape (live map cleared, history retained, claimant is the same session)
keeps its history-only paths as carriers. History never creates a holder.

Ownership is content-based. Bounces d063d81a and e9fff519 refuted a commit-clock design: a
same-second edit was refused, and a backdated committer date kept stale ownership. The
replacement, from the ORCH ruling, records content hashes.

- Each Gobby edit records the git blob hash of each path it left, per task, checkout and
  path, in `task_edited_checkout_hashes`. An absent path records null.
- A task owns a path while the path's current hash matches a record of that task. Among
  matching records the newest edit owns the path (`task_edited_checkout_times`), and ledger
  key order never decides the owner. No commit time enters the test.
- Any change outside Gobby breaks the match. The path then needs `approve-dirty-paths`,
  including a path committed after its last Gobby edit and then changed outside Gobby.
- The commit guard's own-session exemption counts a path only when its current hash matches a
  record in the caller's ledger.
- Before each claimant Bash call, `snapshot_claimed_shell_edits` keeps the claimed tasks'
  ledgered paths that match a recorded hash now. After the call, `adopt_claimed_shell_edits`
  records the new hash of each kept path the call changed, for its owning task. A path that
  was not owned before the call, such as one written or deleted outside Gobby earlier, is
  never adopted. No mtime or clock enters the test, so `cp -p` rewrites are owned.

Residual gaps, named and not fixed:

- Script-body writes (for example `python3 gen.py`) have no snapshot. A new path a script
  creates stays unowned until a Gobby edit records it. The seat checkpoint-commits its own
  script-written paths, as #25171 did.
- An outside write made during the claimant's own Bash call is adopted. The hook cannot
  separate the two writes.
- A Bash call that writes into a second checkout reached by `cd` adopts only the checkout of
  its starting directory. The other checkout's paths stay unowned and need approval.
- A directory-only ledger entry is not owned.
- Ledgers recorded before this change carry no hashes. In-flight dirt needs one
  `approve-dirty-paths` after deploy; no backward compatibility is kept (rule 10).
