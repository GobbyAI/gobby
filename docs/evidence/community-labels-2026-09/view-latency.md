# `gcode graph view --view communities` latency (plan 4.3, Q1.7)

Evidence for `.gobby/plans/gcode-import-communities.md` acceptance item 4.3.15: the list
view on the Gobby checkout, read from stored `code_communities` rows, measured over five
runs. A second measurement covers the full command before and after #22790 (Make the gcode
freshness pre-gate cheap when nothing changed).

## Result

Read from stored rows, the view completes in **under one second**: 0.346 s median over five
runs, slowest run 0.430 s. Until #22790, the full command took 2.2 to 2.5 s because of the
project-scoped freshness pre-gate that every `graph view` runs first. With #22790 the full
command also completes in **under one second**: 0.906 s median over five runs, against
2.390 s and 2.500 s for the binary without it.

The installed `~/.gobby/bin/gcode` keeps the old pre-gate until a cutover promotes a build
that contains #22790.

## Method

- Machine: the Gobby development Mac (macOS 27.0, arm64), with the live daemon running and
  other agent sessions editing the checkout throughout. The numbers reflect a working
  machine, not a quiet-room benchmark.
- Binary: the installed `~/.gobby/bin/gcode`, `gcode 1.9.0`, contract v11, sha256
  `d25d18a1f997859f120d48813265c0c66006c316a44a220ba8eb8d6ed10b8f80`. It was promoted at
  the 2026-09-22 cutover from 0.5.0 at `1e21ebb72d` or later, which contains this leaf's
  implementation commit `fad92ab3d2`.
- Project: `/Users/josh/Projects/gobby` (`d45545c5-ded5-4335-b115-0245752edacf`), with
  7,692 indexed files and 156,147 symbols at measurement time.
- Commands, run from the main checkout. Each arm gets one warm-up run, then five timed
  runs:

  ```bash
  gcode graph view --view communities --format json --project /Users/josh/Projects/gobby
  gcode graph view --view communities --format json --project /Users/josh/Projects/gobby --allow-stale
  ```

- Wall time is `time.perf_counter()` around the subprocess, from an inline `python -c`
  harness in the measuring session. The harness is not committed.

## Runs

| Arm | Runs (s) | Median (s) |
| --- | --- | ---: |
| from stored rows (`--allow-stale`) | 0.208, 0.378, 0.430, 0.340, 0.346 | **0.346** |
| full command, first block | 1.657, 1.703, 1.658, 2.004, 2.206 | 1.703 |
| full command, second block | 2.678, 2.486, 2.222, 2.005, 2.186 | 2.222 |
| baseline: `gcode --version` | 0.015, 0.014, 0.014, 0.021, 0.023 | 0.015 |
| baseline: `gcode status` (same freshness gate) | 2.884, 2.518, 2.244, 1.711, 1.926 | 2.244 |

The stored-rows arm covers what this leaf built: it connects, calls
`communities::read_for_context`, builds the list payload, and prints it. No Leiden run or
partition rebuild happens on the read path.

## Full command before and after #22790

#22790 lets the pre-gate check an indexed regular file from its metadata alone when both
its mtime and its ctime predate the last index. Such a file is not opened, read or
canonicalized. Every other file still goes through the indexer's full classification.

- Both binaries are `cargo build -p gobby-code --release` artifacts from the lane worktree
  `lane/22581-gcode-import-communities`. They were built from the same tree, differing
  only by #22790's diff, and copied aside so both survive the second build.
  `gcode --version` reports `1.9.0` for both. `gcode schema-identity --json` output for
  each is identical to the installed `~/.gobby/bin/gcode`'s, so both arms read the same
  live schema. The installed binary could not serve as either arm because it does not
  contain #22790.
- Same machine, project and full command as above, without `--allow-stale`.
- Order: a before block, the after block, then a second before block, to rule out a
  load spike during the first. Each block gets one warm-up run, then five timed runs.
- Wall time is `time.perf_counter()` around the subprocess. The harness script lives in the
  session scratchpad and is not committed.

| Binary | Runs (s) | Median (s) |
| --- | --- | ---: |
| before #22790, first block | 2.390, 2.342, 2.249, 2.852, 3.150 | 2.390 |
| after #22790 | 1.039, 0.921, 0.803, 0.906, 0.846 | **0.906** |
| before #22790, second block | 2.233, 2.151, 2.500, 2.592, 2.623 | 2.500 |

The slowest after run, 1.039 s, beats the fastest before run, 2.151 s, so the gap is well
outside run-to-run noise.

## Where the full-command time went before #22790

`dispatch.rs` calls `ensure_project_fresh` before every `graph view`. A `sample` profile of
one full run put every main-thread sample inside that call's lock-free pre-gate,
`freshness_probe::project_changed_since`. The pre-gate reported no change on that run, so
none of the time went to refreshing the index; the check alone cost it. Inside the
pre-gate, most samples were in `walker::discover_files_with_options` classifying each
file through `push_classified_file`:

- `security::is_binary` opens and reads every file,
- `security::validate_path` and `push_classified_file` each canonicalize every path,
- `indexer::util::relative_path` canonicalizes every path again in the probe loop.

That is three `realpath` calls, one open and read, and one `stat` per file across about
7,700 files. It explains the roughly 1.3 s of system time a full run shows. `gcode status`
goes through the same gate and costs the same, so this cost belongs to every
project-scoped read and not to the communities view. #22790, under the gcode lane epic
#22777, fixes it; the section above has the measurement.
