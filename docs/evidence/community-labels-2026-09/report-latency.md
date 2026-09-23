# `gcode graph report` latency: `analyze` → `centrality()` swap (plan 1.2, Q1.7)

Evidence for `.gobby/plans/gcode-import-communities.md` acceptance items 1.2.2 and 1.2.3:
the median wall time of `gcode graph report` over five runs before and after
`crates/gcode/src/graph/report/summary.rs` stopped calling
`gobby_core::graph_analytics::analyze` and started calling the new `centrality()` entry
point, plus a payload comparison across the swap.

## Method

- Machine: the Gobby development Mac (Darwin 27.0.0, Apple Silicon), live daemon and
  FalkorDB running, other agent sessions active throughout. Numbers are indicative of a
  working machine, not a quiet-room benchmark.
- Command, run from `/Users/josh/Projects/gobby` against project
  `d45545c5-ded5-4335-b115-0245752edacf`:

  ```bash
  gcode graph report --format json --project /Users/josh/Projects/gobby --top-n 10
  ```

- Both binaries are `cargo build -p gobby-code --release` artifacts from the lane worktree
  `lane/22581-gcode-import-communities`, built from the same tree except this leaf's diff,
  and copied aside so both survive the second build. `gcode --version` reports `1.8.0` for
  both. The installed `~/.gobby/bin/gcode` was deliberately not used: promotion signs the
  binary, so its bytes differ from the cargo artifact for reasons unrelated to this change.
- Wall time is `time.perf_counter()` around the subprocess. Harness scripts live in the
  session scratchpad and are not committed.

## Sequential runs (five per binary, one warm-up each)

| Binary | Runs (s) | Median (s) |
| --- | --- | ---: |
| before, `analyze` | 17.364, 15.000, 17.121, 21.664, 14.865 | 17.121 |
| after, `centrality()` | 19.320, 19.793, 25.629, 21.847, 16.489 | 19.793 |

Taken alone these two blocks would read as a 16% regression. They are not comparable:
they ran roughly twenty minutes apart, the second block overlapped a release build and
other sessions' indexing, and the graph itself grew between them (341,899 nodes and
1,711,821 edges at the first capture, 343,014 and 1,722,077 at the second). The spread
inside each block, 14.9 s to 21.7 s and 16.5 s to 25.6 s, is wider than the gap between
their medians.

## Interleaved A/B (the result to read)

Five pairs, alternating which binary goes first in each pair, one warm-up per binary
before the first pair, so background load and index growth hit both arms equally.

| Binary | Runs (s) | Median (s) | Fastest (s) |
| --- | --- | ---: | ---: |
| before, `analyze` | 19.196, 16.566, 16.014, 22.613, 16.561 | 16.566 | 16.014 |
| after, `centrality()` | 21.053, 15.371, 16.593, 15.219, 16.133 | **16.133** | 15.219 |

The post-swap median is 0.43 s below the pre-swap median and the fastest post-swap run is
0.8 s below the fastest pre-swap run. Acceptance 1.2.3 asks for no regression, and there
is none. The remaining difference is inside the run-to-run noise of a loaded machine, so
the honest claim is "no measurable change", not "a speed-up".

## Payload comparison (acceptance 1.2.2)

From the same interleaved run, comparing the first report each binary produced:

| Report block | Identical across the swap |
| --- | --- |
| `hotspots` | yes |
| `bridge_summary` | yes |
| `bridge_edges` | yes |
| `degradation_details` | yes |
| `summary` | no |

`summary` differs only in `node_count` and `edge_count`. Those are live totals that other
sessions' indexing changes between any two runs, seconds apart or not; they are not a
function of this diff. Every block this leaf can reach, hotspots and bridges included, is
byte-identical.

## Why the medians barely move

The live command path does not execute the swapped code. `load_report_snapshot`
(`crates/gcode/src/graph/report/loading.rs`) fills `ReportGraphSnapshot.hotspots` from
FalkorDB degree queries and leaves `nodes` and `code_edges` empty, and the production
entry point `generate_report_with_options` hands that snapshot straight to
`generate_report_from_snapshot_with_options`, which only calls `summarize_hotspots` when
`hotspots` is `None`. So the two `analyze` calls this leaf removed ran on the in-memory
snapshot path, which today serves `empty_report` and the report unit tests.

That is worth recording rather than hiding: the plan's research context describes the two
discarded Leiden partitions per report as a cost on `gcode graph report`, and on the
current code they are not. The saving is real but it lands on the in-memory path, where
the report tests now pin the output, and on any future caller that builds a report from
nodes and edges instead of from FalkorDB rows. The measured medians confirm the swap costs
nothing on the live path.
