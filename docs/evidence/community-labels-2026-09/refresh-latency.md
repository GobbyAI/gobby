# Community refresh latency

Measured 2026-09-22 with the PostgreSQL-backed `gcode index` implementation in the
isolated `gobby_gcode_test` database. The pre-leaf executable came from merge commit
`b614aaaa14` in a temporary managed worktree; the post-leaf executable came from the
task worktree. Both used the same indexed project row and target file. The initial full
index and the post-leaf warm refresh were excluded from the samples.

The installed binary was not used: its worker grant could not reauthenticate, and this
leaf is explicitly prohibited from installing, cutting over, or restarting the daemon.
The harness calls the same `index_files` path as `gcode index`, with projections and
embeddings disabled in both versions.

## Results

| Corpus | File | Before (ms) | Before p50 | After (ms) | After p50 | Delta | +250 ms budget |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Gobby checkout | `crates/gcode/src/lib.rs` | 456, 487, 477, 488, 501 | 487 ms | 82,361, 89,170, 92,320, 80,019, 93,531 | 89,170 ms | +88,683 ms | Over |
| Game Goblins C1 | `src/game_goblins/replenishment/forecasting.py` | 365, 383, 379, 370, 391 | 379 ms | 331, 498, 497, 477, 449 | 477 ms | +98 ms | Pass |

Every post-leaf timed run reported `skipped_unchanged=true`, `changed=0`,
`new_ids=0`, and `retired_ids=0`. The Gobby result therefore isolates the cost noted in
the plan: the current skip avoids the database replacement only after the full import
graph and Leiden partition have been rebuilt. The smaller C1 graph remains within the
budget.

## 2026-09-24 correction: the Gobby cost was `internal_edges`, not Leiden

A `sample` of a live one-file `gcode index` on the Gobby checkout put about 100% of the
main thread in `internal_edges` (partition.rs). It scanned every folded import edge once
per community, and it runs for every group in the low-cohesion pass and again for every
final community: O(communities x edges) over 2,353 communities. Leiden, the import load
and the database were near zero. Task #22858 finds each member's edges with a `BTreeMap`
range over the `left <= right` keys, which gives the same count.

The harness corpus is gone (the `gobby_gcode_test` database was recreated on 2026-09-23),
so the fix was measured on the live path, with release builds and the same command:
`gcode index --files=crates/gcode/src/lib.rs --quiet` on the main checkout, file
unchanged, 2,353 communities, `skipped_unchanged=true`.

| Binary | Wall | User |
| --- | ---: | ---: |
| Installed before #22858 | 17.37 s | 6.60 s |
| #22858, run 1 | 2.47 s | 0.33 s |
| #22858, run 2 | 0.99 s | 0.46 s |

The input-digest contingency below is not needed: the rebuild is now cheap, and the
existing output-signature skip avoids the write.

## Over-budget contingency

The required digest contingency is a digest of the deduplicated `(source, module)` row
set, stored beside `partition_signature` in a follow-up migration and compared before
building the graph. An equal digest can return the unchanged report without running
Leiden; a changed digest continues through the existing build/remap/transaction path.
Per the authoritative 3.2 scope, that migration is named here and is not added by this
leaf.

## Commands

Both versions were run with the required Cargo prefix and corpus-specific environment
variables:

```text
CARGO_BUILD_JOBS=4 nice -n 15 -- cargo nextest run -p gobby-code -E 'test(incremental_latency_experiment)' --status-level fail --final-status-level fail --no-capture
CARGO_BUILD_JOBS=4 nice -n 15 -- cargo nextest run -p gobby-code -E 'test(incremental_latency_experiment_reports_after)' --status-level fail --final-status-level fail --no-capture
```

The baseline and post-leaf harnesses each printed all five elapsed times and completed
with one passing test.
