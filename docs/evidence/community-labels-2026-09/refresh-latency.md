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

These unpaired samples establish the `internal_edges` improvement, but do not
establish the +250 ms community-refresh budget. Task #22862 measures that delta
with interleaved refresh and bypass variants on the same live path.

## 2026-10-06 paired live-path measurement (#22862)

The Orchestrator authorized two optimized local binaries built from the same lane
source, `f361117692a6542f581682ab95ca9e018dfed958`. A runs the existing refresh;
B adds a temporary `if std::hint::black_box(true) { return; }` at the start of
`index/indexer/lifecycle.rs::refresh_communities`. The bypass was restored after
building B, and neither binary was installed or promoted. Both builds used
`cargo build --release -p gobby-code --bin gcode`.

The shell stayed in the lane worktree. Both binaries ran the same command:

```text
<local-binary> index --project /Users/josh/Projects/gobby --files=crates/gcode/src/lib.rs --quiet
```

One excluded warm-up per variant used JSON output to check refresh status. A
reported 2,557 communities, `skipped_unchanged=true`, and zero changed/new/retired
communities; B reported no communities. Neither warm-up was degraded. Warm-up
wall times were 11,527.926 ms and 2,181.142 ms. The five measured pairs then ran
A, B, A, B in order, with no instrumentation environment variable. Every sample
exited zero. The helper checked the corpus HEAD and target-file hash before and
after each sample; both stayed unchanged.

| Pair | A start (UTC) | A wall (ms) | A load 1m, start/end | B start (UTC) | B wall (ms) | B load 1m, start/end |
| --- | --- | ---: | --- | --- | ---: | --- |
| 1 | 17:04:31.518 | 2,470.521 | 16.2783 / 16.2783 | 17:04:34.061 | 791.118 | 16.2783 / 16.2783 |
| 2 | 17:04:34.972 | 2,019.487 | 16.2783 / 14.9746 | 17:04:37.088 | 669.080 | 14.9746 / 14.9746 |
| 3 | 17:04:37.954 | 1,826.560 | 14.9746 / 14.9746 | 17:04:39.935 | 907.692 | 14.9746 / 13.8555 |
| 4 | 17:04:40.991 | 2,849.724 | 13.8555 / 13.8555 | 17:04:44.060 | 972.922 | 13.8555 / 13.8555 |
| 5 | 17:04:45.152 | 1,732.071 | 13.8555 / 15.0679 | 17:04:47.043 | 789.993 | 15.0679 / 15.0679 |

A p50 is **2,019.487 ms**; B p50 is **791.118 ms**. Their difference is
**+1,228.369 ms**, over the +250 ms budget. The median within-pair difference is
+1,350.407 ms. This result requires the input-digest contingency below.

Provenance:

- Corpus HEAD: `12ab0abfcff3653403178b7a42c58775f78a6220`.
- Target SHA-256: `34a9494955e7aba646eb70e7ec032b2f50c47deb4cee213efeb711bd906c08b1`.
- A binary SHA-256: `b4e1b2fe6db3a0e468a4c074616bce1f6d094bfff7c9a917933fa2b8096bda88`.
- B binary SHA-256: `afd7f18d5a537399f941f86f6697f42e484a839af6f5007364ace14cdaf19de8`.
- Session artifacts: `.gobby/tmp/22862-measure-ab.py` and
  `.gobby/tmp/22862-ab-results.json` in the lane worktree. The helper measures
  subprocess wall time, records `os.getloadavg()[0]`, excludes warm-ups, and
  computes both medians from the unrounded samples.

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
