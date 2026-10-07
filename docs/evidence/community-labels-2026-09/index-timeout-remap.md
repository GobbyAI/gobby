# Community remap timeout investigation (#23638)

The complete timeout traces localize ten of eleven failures to the
`communities.assign_ids` interval; one ends at `communities.replace_lock`.
For example, daemon.log lines 25042–25086 for PID 17886 show assignment starting
at 13,485 ms, with no persistence marker before the 30-second kill. That interval
also includes prior-row cloning, row construction, and changed-row counting, so
the trace alone does not uniquely identify the stalled instruction.

The original `remap.rs:119–145` scans every current/prior community pair and
constructs the prior member set for each pair, including disjoint pairs.
Source excerpt hash:
`9dbc3beb279728b45fdc2c07cd88e52e84b238ea45a379a68c4556ce10468e77`.
Singleton-heavy partitions therefore require quadratic comparisons and set
allocations. Changed-row counting adds a second current/prior scan.

## Isolated reproduction

`singleton_remap_stays_within_index_budget` constructs 6,000 current and prior
singleton communities. Half retain their paths; half are disjoint. Fixture
construction precedes the timer. Every resulting ID and the allocation watermark
are checked before the timing assertion.

Before implementation, this exact command ran the named test and failed its
timing assertion after **176.45587575 seconds**:

```sh
cargo nextest run -p gobby-code --run-ignored ignored-only -E 'test(singleton_remap_stays_within_index_budget)'
```

RED nextest run: `791bf22b-8c8b-4f5c-b5f7-4afb4e1b2f0d`.
During that run, a bounded three-second macOS sample of test PID 24161 captured
`assign_ids → match_candidates → overlap`, including set construction and
membership checks. It is saved locally at
`.gobby/tmp/23638-singleton-red-sample.txt`. Only two stack samples were returned;
they establish execution in that path, not a statistical percentage of CPU time.
A separate process snapshot at 2:56 elapsed reported 42.52 seconds of CPU.

After the minimal member-index implementation, the identical command passed:
nextest run `c17dcb3d-e2d5-493b-a304-7e566ba55e75`, test runtime **1.065 seconds**.
This is an unoptimized debug fixture comparison, not an optimized live-daemon
benchmark. The final regression runs normally rather than being ignored; its
five-second ceiling leaves headroom over the measured GREEN result while keeping
assignment well within the daemon's 30-second total index budget.

## Fix and remaining acceptance

Build the prior member-to-community index once, deduplicating members within each
prior community. Current members accumulate overlaps only with intersecting prior
communities. Retain original candidate order, Jaccard comparison, tie breaking,
ID allocation, watermark, and label carry behavior. A separate fixture checks
repeated members, shared members, disjoint communities, and empty members.
Count changed stored rows through a prior-ID map instead of repeated searches.
The gcode release version and Python maintenance pin advance together to 1.9.13.

The isolated reproduction confirms pathological remap work. A live stalled
instruction capture remains unavailable. The Monitor's 14:41–14:56 watch saw no
index process reach 20 seconds; a later child was already a zombie at 22 seconds,
with no timeout or phase trace. That observation does not establish a pipe or
reaping defect.

Task acceptance remains open until the reviewed fix is landed and the Orchestrator
activates the coherent binary set, followed by at least 30 minutes of daemon logs
with lane-6 worktree edits and zero index timeouts for that worktree. This document
does not claim that live acceptance or #22862's final refresh-budget measurement.
