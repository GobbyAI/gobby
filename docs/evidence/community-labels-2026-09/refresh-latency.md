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
At the time of that measurement, the authoritative 3.2 scope only named the
contingency. Task #22862 subsequently implemented it; the post-digest measurement
below verifies the budget on the activated implementation.

## 2026-10-07 isolated post-digest measurement

The community refresh adds **120.495 ms p50**, within the **+250 ms** budget.
A p50 is **297.858 ms**; B p50 is **177.363 ms**. The median within-pair
difference is **125.116 ms**. Six pairs alternate AB/BA, with three of each
order. All twelve timed subprocesses exited zero. This satisfies the amended
measurement criterion; no further production code change is needed.

The Orchestrator admitted this full lane-root corpus at 04:22 CT. Both timed
variants come from source `49bc1076ce13dca9f5439855e8b717abd8d402e4`, archived
inside the lane's primary worktree. A is the unchanged release build. B differs
only by this temporary early return in `index/indexer/lifecycle.rs`:

```diff
pub(crate) fn refresh_communities(conn: &mut Client, ctx: &Context, outcome: &mut IndexOutcome) {
+    if std::hint::black_box(true) {
+        return;
+    }
     match communities::refresh_project_communities(conn, ctx) {
```

Neither binary was installed. The control was never committed, and the archived
source was restored byte-for-byte after building B. Both builds used the same
scratch Cargo target, `CARGO_BUILD_JOBS=4`, `nice -n 15`, and
`cargo build --release -p gobby-code --bin gcode`. A built successfully in
22m28s; B built successfully in 6m13s.

The corpus is `/Users/josh/.gobby/worktrees/gobby/lane-5-rust` at commit
`a57101f88600202cc8cc2b8429759c77adb2755d`. A private fixture daemon and the
isolated PostgreSQL hub on port **60892** served both variants. The fixture
registered the lane as an overlay and seeded an import-free parent sentinel in
private state to satisfy the parent-index prerequisite. It then indexed the
entire lane with `--full`: **8,159 scanned files, 8,158 indexed files,
171,796 symbols, 52,176 imports, and 2,545 communities**. The sentinel was
tombstoned; it contributes no import edges. The main checkout was neither read
for indexing nor indexed, and port 60891 was never used.

Cold seeding used the installed gcode from activated source `78bf815513`, with
the installed gdaemon from that same cutover. A cold seed with the pinned older
gcode had failed with `start indexed file transaction: connection closed`.
Seeding is excluded from timing and took 615.406 seconds. Both subsequent A
warm-ups reported `skipped_unchanged=true`, `changed=0`, `new_ids=0`, and
`retired_ids=0`, with all 2,545 communities retained. B's warm-up reported no
community refresh. Warm-ups of 347.152 ms, 290.987 ms, and 759.810 ms are excluded.

Every timed command was:

```text
<A-or-B-binary> index --project /Users/josh/.gobby/worktrees/gobby/lane-5-rust --files=crates/gcode/src/lib.rs --quiet
```

| Pair | Order | A start (UTC) | A wall (ms) | A load 1m, start/end | B start (UTC) | B wall (ms) | B load 1m, start/end |
| --- | --- | --- | ---: | --- | --- | ---: | --- |
| 1 | AB | 09:31:13.062504 | 290.773 | 5.104492 / 5.104492 | 09:31:13.413015 | 164.537 | 5.104492 / 5.104492 |
| 2 | BA | 09:31:13.863159 | 283.760 | 5.104492 / 5.104492 | 09:31:13.639516 | 163.972 | 5.104492 / 5.104492 |
| 3 | AB | 09:31:14.203741 | 311.578 | 5.104492 / 5.104492 | 09:31:14.573989 | 173.779 | 5.104492 / 5.104492 |
| 4 | BA | 09:31:15.095547 | 288.500 | 5.104492 / 5.104492 | 09:31:14.834482 | 196.034 | 5.104492 / 5.104492 |
| 5 | AB | 09:31:15.440483 | 304.943 | 5.104492 / 5.104492 | 09:31:15.810711 | 180.947 | 5.104492 / 5.104492 |
| 6 | BA | 09:31:16.348471 | 383.682 | 5.104492 / 5.104492 | 09:31:16.054764 | 198.957 | 5.104492 / 5.104492 |

Provenance:

- Target SHA-256: `34a9494955e7aba646eb70e7ec032b2f50c47deb4cee213efeb711bd906c08b1`.
- A binary SHA-256: `9373ed58915900ca26f4b8ba01afdde8a0d432784dc1b3c88cb95669558e13cf`.
- B binary SHA-256: `86bff32c935c4fcecfd0f5f9da1d2b3d0bb7f9b43c1a2863966f90e5a7f3314f`.
- Seed gcode SHA-256: `196f80d0c505e6d64f88043ddab9ca29923621894972cbcc924ba66697e94d23`.
- Isolated daemon gdaemon SHA-256: `d8f52703a9216a6c45281585fc7d6ec078fc92c9680a84974a12570f79dac9af`.
- Scratch artifacts under `.gobby/scratchpads/gobby-15405/`:
  `22862-ab-isolated.json`, `22862-seed-result.txt`, `22862-with-refresh-49bc`,
  and `22862-without-refresh-49bc`. The helper checks corpus HEAD and target
  hash before and after every sample and computes medians from unrounded values.

Focused validation passed: **1 test in 666.27 seconds**.

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
GOBBY_TEST_PROTECT=1 \
PYTHONPATH=/Users/josh/.gobby/worktrees/gobby/lane-5-rust \
GOBBY_HOME=/Users/josh/.gobby/worktrees/gobby/lane-5-rust/.gobby/scratchpads/gobby-15405/22862-test-home \
TMPDIR=/Users/josh/.gobby/worktrees/gobby/lane-5-rust/.gobby/scratchpads/gobby-15405/22862-pytest-temp \
GOBBY_ALLOW_WORKTREE_DAEMON=1 uv run pytest .gobby/tmp/test_22862_measure.py -s -q
```

The scratch harness also passed Ruff, a scoped test-type audit (zero errors),
test-quality audit (zero issues), and the suppression ratchet over all tracked
Python roots plus both scratch helpers (4,564 files, 190 baseline suppressions,
zero new or stale entries). The source archive was excluded from that audit
because it duplicates the historical baseline. The Rust slot was released at
04:32 CT, before the 04:40 hard stop.

## Commands

Both versions were run with the required Cargo prefix and corpus-specific environment
variables:

```text
CARGO_BUILD_JOBS=4 nice -n 15 -- cargo nextest run -p gobby-code -E 'test(incremental_latency_experiment)' --status-level fail --final-status-level fail --no-capture
CARGO_BUILD_JOBS=4 nice -n 15 -- cargo nextest run -p gobby-code -E 'test(incremental_latency_experiment_reports_after)' --status-level fail --final-status-level fail --no-capture
```

The baseline and post-leaf harnesses each printed all five elapsed times and completed
with one passing test.
