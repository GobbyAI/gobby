# Cargo build and test-hub load: evidence report

Task: #23902 (Research why concurrent cargo builds and test-hub pytest load
this machine, and fix the setup), under #22801 (Lane 4). Author: lane 4
developer gobby#16907. Started 2026-10-15 21:00 CDT. Source binding: worktree
`lane-4-rust-4` at `81f7f5e683` on 0.5.0 `3ae83e1a2c`.

Status: draft. Sections 1 and 3 hold the read-only findings and section 5
the test-hub window. Sections 2 and 4 fill in as the Release Manager grants
the cargo runs. Every number below names its source. Nothing in this task
changes shared config, spawn policy or the test hub.

Seat limits: the seat runs under SRT, which denies `ps`/`pgrep` process
listing and the Docker socket, and a Gobby shell-write hook blocks
`/usr/bin/time -l` (filed as #24588, shell-write classifier), so peak RSS is
not recorded. Build progress is read
from target fingerprint counts and load averages; container CPU and memory
come from the Orchestrator's host samples.

Daemon changes during the series. Compare health readings across these
boundaries with care: the hub window and R1 ran on the daemon as of
2026-10-15 22:00 CDT, and later runs ran on a daemon that includes #23359
(worker-thread GIL starvation), which targets health latency.

| Time (CDT) | Change | Activates |
| --- | --- | --- |
| 2026-10-15 23:36-23:38 | binary cutover C2, gdaemon 0.4.33 | #23880 |
| 2026-10-16 00:11 | Python restart R3 | #24540, #24243 (Python), #24511, #24426 |
| 2026-10-16 01:09 | Python restart R4 | #23359, #23884, #24511 |
| 2026-10-16 02:56 | binary cutover, gdaemon 0.4.41, gcode 1.9.33, ghook 0.9.22 | #23880, #24367, #23217, #24492, #24537 |
| 2026-10-16 03:44 | Python restart | #24581, #24561, #24559, #24512, #24535 |
| 2026-10-16 06:45-06:51 | binary cutover (section 1.3), runner 67250 | #24543, #24564, #24516, #24496, #24582, #23217, #24417, #24562, #24542, #24585 |
| 2026-10-16 07:56 | Python restart, runner 86273 | #24541, #24504, #24535 |
| 2026-10-16 08:18 | Python restart, runner 43791 | #24540 |
| 2026-10-16 08:30-08:34 | binary cutover, gdaemon 0.4.45, runner 28963 | #24385 |
| 2026-10-16 08:51 | Python restart, runner 17593 | #24583, #23830 |
| 2026-10-16 09:06-09:12 | binary cutover, gdaemon 0.4.46, runner 22972 | #24445 |
| 2026-10-16 09:56 | Python restart, runner 82758 | #24404, #24609, #24608 |
| 2026-10-16 10:07 | Python restart, runner 78855 | #24586, #24614 |
| 2026-10-16 10:30 | Python restart, runner 60265 | #24612 |
| 2026-10-16 10:56 | Python restart, runner 2633 | #24405, #23650 |
| 2026-10-16 11:10 | Python restart, runner 75648 | #24611 |
| 2026-10-16 11:31 | Python restart, runner 2470 | #24616 |
| 2026-10-16 11:45 | Python restart, runner 49771 | #24411 |
| 2026-10-16 12:08 | Python restart, runner 671 | #24540, #24620 |

#23359's Gate B (07:01-07:31 CDT, cargo-free) passed: 360 `/api/health`
probes, all 200, p99 1.020 s, max 1.780 s (LM7 via the Orchestrator). That
is the daemon's health latency with no cargo running and is the reference
for any later build's health readings.

Research targets created, all under the seat's sandbox target
`~/.gobby/cache/sandbox/cargo-target/lane-4-rust-4-13da843fd891a9af/research/`:
`cold-j18` (R1, partial, 1.6 GB, incomplete). No other target dir was
created; the hub window used no Cargo target.

## Summary so far

The host is an 18-core MacBook Pro (`getconf _NPROCESSORS_ONLN` = 18). The
load does not come from a slow machine. It comes from three multipliers that
stack:

1. **Every checkout compiles the whole dependency graph cold, up to three
   times.** Each checkout gets its own operator, agent and sandbox Cargo
   target (section 3.2). On 2026-10-15 there were 43 live worktrees, 45
   sandbox targets (261 GB) and 94 operator/agent targets (123 GB). A cold
   `cargo nextest run -p gobby-code` from a fresh sandbox target did not
   finish compiling dependencies inside 1200 s at `CARGO_BUILD_JOBS=3` under
   load near 30.
2. **Parallelism is capped per command, not per machine.** `CARGO_BUILD_JOBS`
   bounds rustc jobs inside one cargo invocation. Nothing bounds the number
   of invocations or nextest's test processes (default: one per CPU, so 18
   per run). In the ungated window 20:41-20:50 the five-minute load reached
   71/41/22 with health checks at 9.5 s (RM gobby#15991).
3. **A fixed job cap does not fit the host.** `CARGO_BUILD_JOBS=3` was sized
   for six concurrent heavy slots (6 x 3 = 18 cores); with one shared slot
   it leaves most cores idle. R1, a single cold build at 18 jobs started at
   load1 13-16, held load1 at 40-42 and saw `/api/health` reach 5.59 s, and
   the watcher stopped it (section 2.1). Load1 stayed high even when only
   one rustc was live, because one rustc's codegen threads can take every
   job token. Any cap has to start from the host's measured load at grant
   time, and the codegen threads count against it.

## 1. Baseline

### 1.1 Sandboxed seat (measured)

Seat target: `~/.gobby/cache/sandbox/cargo-target/lane-4-rust-4-13da843fd891a9af`,
`CARGO_BUILD_JOBS=3`, sandboxed (SRT). Wall times come from the RM grant
reports for #23575. CPU user+sys, peak RSS and peak load are pending the
section 2 runs, which record them.

| Run | Cache state | Wall | Load at start | Result |
| --- | --- | --- | --- | --- |
| `cargo nextest run -p gobby-code` (RED) | cold target | >1200 s, killed at bound | ~30 | 0 tests; still compiling deps |
| `cargo nextest run -p gobby-code --no-run` (warm-up, continuation) | partly warm | 5m41s | n/a | EXIT 0 |
| `cargo nextest run -p gobby-code --no-run` after a rebase | warm deps, changed workspace crates | 7m38s | 19.4/11.9/10.7 | EXIT 0 |
| `cargo nextest run -p gobby-code` (GREEN) | warm | 197.2 s | ~19 | 128/128 |
| `cargo clippy -p gobby-code` | warm build, cold check metadata | 18m29s | ~30 | EXIT 0 |
| `cargo nextest run -p gobby-code -E 'test(bounded_phase)'` | warm, no change | 5.65 s build + 0.47 s tests | n/a | 4/4 |

Other seats on 2026-10-15 (RM gobby#15991; exact START/END and load absent):
cold-seat `gobby-code` nextest packet B 98.1 s (warm after a prior build);
#23880 cargo build 6m55s; #24444 cargo build 7m38s including a
package-cache lock wait.

Pending: the release builds (coherent set and `gobby-client`) and the
operator-shell rows. A sandboxed seat cannot run in the operator shell, so
those rows come from the Orchestrator. The coherent-set cutover builds at
23:36 CDT and 02:56 CDT, and any `gobby-client` release build, recorded no
wall time, CPU, load, RSS or cache state (Orchestrator gobby#15977, 06:03
CDT); they are unmeasured. The 06:45 cutover was measured (section 1.3).

### 1.3 Operator shell (measured)

Source: Orchestrator gobby#15977, 2026-10-16 06:59 CDT. Bash `time`
keyword; `/usr/bin/time -l` was blocked by the shell-write scope gate, so
peak RSS is not recorded here either.

| Run | Cache state | Wall | CPU user + sys | load1/5/15 before | load1/5/15 after | Concurrent heavy / hub | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `uv run gobby cutover` (coherent-set release build of gcode, gdaemon, ghook, plus gclient, promotion and daemon restart) | warm main-checkout target; `gobby-core` 0.10.10 and dependents changed since the last build | 316.14 s | 571.38 s + 22.68 s (187% CPU) | 4.75 / 8.85 / 17.30 | 8.43 / 9.91 / 15.23 | 0 / 0 | exit 0 |

Daemon stop took 17.3 s and the health wait 11.3 s; the remaining ~287 s
is build and promotion. 187% CPU over 316 s means the release build used
under two cores on average on an 18-core host: the warm build is dominated
by serial work, with fat LTO (section 3.3) the leading candidate. A per-phase
split needs `--timings`, which the cutover does not pass.

### 1.2 Host and toolchain

- rustc and cargo 1.97.0, LLVM 22.1.6, cargo-nextest 0.9.137, `aarch64-apple-darwin`.
- No `~/.cargo/config.toml`; no sccache, mold or lld installed.
- Repository `.cargo/config.toml` sets only `[env] GOBBY_CLIENT_HOSTNAME`.
- Disk 7.3 TiB, 30% used, so disk space is not the constraint; disk writes are
  part of the cost of every cold target.

## 2. Native options

Measured runs go one at a time through the RM heavy slot (packet R1-R6, sent
2026-10-15 21:2x CDT), each with an Orchestrator TERM watcher that aborts on
load1 > 60 for 60 s or `/api/health` > 5 s. Each variant uses
`CARGO_PROFILE_*` environment overrides on the same source tree (`81f7f5e683`)
and a fresh cold target, so no shared config changes.

### 2.1 R1: cold build at 18 jobs (stopped by the watcher)

`CARGO_BUILD_JOBS=18 CARGO_TARGET_DIR=<sandbox target>/research/cold-j18
cargo build -p gobby-code --tests --timings`, sandbox `CARGO_HOME`.

Load1 from the seat (every 15 s); rustc process count from the
Orchestrator's host sampler (every 15 s, from 22:34; the seat sandbox denies
`ps`). The host is 18 cores.

| Time (CDT) | load1 | rustc | Note |
| --- | --- | --- | --- |
| before 22:30 | 12.8 | | RM reading at watcher ARM |
| 22:30:13 | 15.9 | | build starts 22:30:15 |
| 22:31:14 | 31.5 | | |
| 22:32:42 | 39.6 | | plateau begins |
| 22:34:08-22:34:57 | 40.6-41.2 | 10-16 | rustc peak 16 at 22:34:57 |
| 22:35:14-22:35:47 | 41.2-42.0 | 4-9 | |
| 22:36:03-22:36:19 | 41.0-41.2 | 1-3 | |
| 22:36:26 | | 1 | `/api/health` 5.59 s; watcher TERM 22:36:36 |
| 22:36:30 | 39.9 | 1 | |

Result: void, exit 144 after 6 min 21 s. 388 rlibs and 126 build-script
outputs were built; the `--timings` report is written only at the end, so
none exists. The partial target (1.6 GB) is kept and marked incomplete. The
host sampler saw no process named `ld`, so linking runs under another name
and is not counted.

How far it got, read from the partial target on 2026-10-16: the last
outputs, written 22:35-22:36, are `insta`, `syn`, `proptest`, `redis` and
`gix` (a 49 MB rlib built from at least ten codegen units). No `gobby-core`
or `gobby-code` rlib exists. R1 therefore stopped in the last third-party
crates, before any workspace crate compiled. `cargo tree -p gobby-code -e
normal,build,dev` lists 380 distinct packages, so 6 min 21 s at 18 jobs
under load1 40 covered nearly the whole third-party graph.

What R1 shows: load1 did not follow the rustc process count. Over the last
90 s R1 ran 1-5 rustc while load1 stayed at 40-42, and the health spike came
with one rustc live. The process count understates compile load: one rustc
runs parallel codegen threads that draw on cargo's jobserver tokens, up to
the job cap, and the macOS load average counts runnable threads. A few
large crates at the end of the graph (here `gix`, `redis` and `proptest`,
the last outputs written) can therefore hold up to 18 threads busy. Load1 was 13-16
before the build, so other work supplied at least that much. Without a
per-thread or per-process CPU sample, R1 cannot split the remaining load
between the build and the rest of the host; it measured the host as a
whole crossing the daemon's limit with one 18-job build running. Health
latency also has causes that are not load: project memory d5d32fff records
synchronous PostgreSQL work on the event loop, synchronous ghook-stamp reads
in the admin health route and localhost dial stalls alongside fleet
contention. One 5.59 s sample does not separate these.

R4 (warm rebuild) and R3 (line-tables comparison) depended on a completed R1
target and did not run.

| Option | What it changes | Spreads load or removes work | Run |
| --- | --- | --- | --- |
| Job cap (`CARGO_BUILD_JOBS` 18 vs 6 vs 3) | rustc processes per invocation | Spreads only: total CPU stays the same | R1 vs R2 |
| Dev debuginfo `line-tables-only` | DWARF emitted and linked per crate | Removes work and disk | R3 vs R1 |
| Warm incremental rebuild | Reuses a checkout's warm target | Removes work | R4 |
| Release `lto = "thin"` instead of fat `lto = true` | Final link optimisation | Removes serial work at the end of every release build | R5 vs R6 |
| `nice` / `taskpolicy -b` | Scheduling priority | Spreads only; protects the daemon's latency | paper |
| nextest `test-threads` cap | Test processes per run | Spreads only | paper |
| Linker | Default Apple linker (ld-prime) is already fast | Little to gain | paper |

## 3. Causes

### 3.1 Who sets `CARGO_BUILD_JOBS=3`, and why

No code sets it. `gcode grep -w CARGO_BUILD_JOBS src/ crates/` finds nothing.
It is policy text in `.gobby/workflows/rules/project-operating-policy.yaml`
("Every granted cargo run sets CARGO_BUILD_JOBS=3"), added by `be77687bb6`
(#24319, 2026-10-13) next to the six-slot heavy-run cap: six slots of three
jobs fill 18 cores. Seats type it into each command, and plans copy it.

The arithmetic holds only while six slots are live. With one shared slot
(the 20:50 fallback) it leaves 15 cores idle during every granted build.
It also misses two sources of parallelism:

- nextest runs one test process per logical CPU by default. Only three test
  groups are capped in `.config/nextest.toml` (`ghook-process` 2,
  `serial-db` 1, `gterm-pty` 1). Three concurrent nextest runs can hold 54
  test processes regardless of `CARGO_BUILD_JOBS`.
- The `gclient-test-gterm` setup script builds `gterm` inside a nextest run,
  outside the run's own build step.

Measured cost: R1 (18 jobs, load1 13-16 at start) held load1 at 40-42 and
was stopped when `/api/health` reached 5.59 s (section 2.1). The j6
comparison (R2) is pending.

### 3.2 Why each checkout builds cold, up to three times

`src/gobby/agents/cargo_target.py` gives every checkout three targets:

| Target | Path | Who builds there | Why separate |
| --- | --- | --- | --- |
| Operator | `~/.gobby/cache/cargo-target-v2/<project>/<checkout>` via the `target` symlink | interactive shells | #22532: a shared target let concurrent builds of different revisions reuse each other's workspace-crate artifacts |
| Agent | `.../<checkout>-agent` | unsandboxed spawned agents (shared Gobby `CARGO_HOME`) | #23198: fingerprints embed `$CARGO_HOME/registry/src`, so alternating homes rebuild every dependency |
| Sandbox | `~/.gobby/cache/sandbox/cargo-target/<checkout>` | sandboxed seats (sandbox `CARGO_HOME`) | same fingerprint reason; sandboxed runs may not write outside the sandbox cache (#24313 keys it by the built workspace) |

The isolation is correct. #22532 and #24313 record a real hazard: Cargo
checks workspace-member freshness by dep-info mtimes with paths relative to
the workspace root, so a target shared across checkouts can report a stale
binary from another checkout as fresh. Any sharing must keep workspace
crates per checkout.

The cost is the dependency graph, which is identical across checkouts.
Third-party crates do not change between worktrees, yet each new worktree
compiles all of them again. Survey of the 45 sandbox targets on 2026-10-15
(read-only `os.walk` sizes and `.fingerprint` counts): live targets range
from 2 GB to 39 GB, with 300-2,400 fingerprint entries in `debug` alone.

Measured cost: R1 compiled nearly all of the third-party graph for
`gobby-code`'s tests (380 packages) in 6 min 21 s at 18 jobs, holding load1
at 40-42, and wrote 1.6 GB before any workspace crate. Every new worktree
pays that again in each target it builds in. This seat's own warm `debug`
directory is 5.8 GB. At `CARGO_BUILD_JOBS=3` under load near 30, the same
cold dependency build did not finish inside 1200 s (section 1.1).

### 3.3 Release profile

`Cargo.toml` sets `[profile.release] lto = true` (fat LTO) and `strip = true`,
with `opt-level = 3` for gcode and gdaemon and `"z"` for ghook. Fat LTO
merges every crate into one module before code generation, so the end of
each release build is a long, memory-heavy, mostly single-threaded step.
Every cutover pays it for four binaries. The 06:45 cutover (section 1.3)
averaged 187% CPU over 316 s on a warm target, consistent with a long
serial tail. The LTO share itself is pending R5 vs R6.

## 4. Compiler caching and containers

### 4.1 sccache-style caching

Recommendation pending measurement, leaning **recommend for dependencies**.

- It removes work instead of spreading it. sccache keys each rustc
  invocation by a hash of its inputs, so a crate compiled once is reused by
  every checkout that compiles the same crate with the same flags.
- It fits the hazard in 3.2. sccache does not cache incremental
  compilations, and dev builds compile workspace members incrementally, so
  workspace crates stay per checkout while third-party crates are shared.
- Cache hits need identical source paths. Dependencies live under the shared
  `CARGO_HOME` registry (one for sandboxed seats, one for unsandboxed agents),
  so seats in the same home share paths. The operator home, agent home and
  sandbox home would fill three separate caches unless they are unified.
- Under SRT the sccache server needs a writable cache directory inside the
  sandbox cache and a local socket. Both need a policy entry; this is a
  follow-up, not a config change made here.
- Install: `sccache` (Homebrew or `cargo install sccache`). Needs Josh's go;
  nothing was installed.

### 4.2 Containers on macOS

Recommendation: **reject** for Rust builds.

- Docker Desktop runs a Linux VM. Builds inside it produce Linux binaries;
  gcode, gdaemon, ghook and gclient ship as macOS binaries, so every
  shipped build would still run natively.
- Bind-mounted source trees cross the VM file-sharing layer, which is slower
  than native APFS for Cargo's many small-file reads and writes.
- The VM's CPU cap only moves the contention; it does not remove compile
  work.

The test hub already runs in a container (`gobby-postgres-test-1`); section
5 covers it. Docker Desktop was not started, stopped or reconfigured.

## 5. Test hub

### 5.1 How a hub-backed run uses Postgres

The test hub is the `gobby-postgres-test-1` container on `127.0.0.1:60892`.
`tests/fixtures/postgres.py` gives every pytest process its own database and
every test a clean schema:

- Per process: `CREATE DATABASE gobby_test_<pid>_<nonce>`, `CREATE EXTENSION
  pg_search`, then the full migration set into a `gobby_test_*` worker schema.
  The database is dropped `WITH (FORCE)` at exit, and dead processes'
  databases are reaped by the next run.
- Per test: `postgres_db` resets the worker schema twice, on entry and on
  exit. Each reset deletes every application table's rows, restarts owned
  sequences and reinserts the canonical seed in one transaction.

Process isolation is therefore already in place. "More test databases" would
not separate runs any further; each run already has one.

### 5.2 Measurement window

One window, authorized by Josh for six concurrent runs, RM GO, Orchestrator
sampling the container and `/api/health` from the host. 2026-10-15
22:14:40-22:17:54 CDT. File: `tests/storage/test_hook_receipts.py` (31
tests). Each run: `DATABASE_URL=<test hub> GOBBY_TEST_PROTECT=1 uv run pytest
tests/storage/test_hook_receipts.py -q -p no:cacheprovider --durations=10`.

| Step | Runs | Stagger | Result | pytest time per run | Peak live | Peak connections | Postgres CPU | `/api/health` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline (60 s) | 0 | n/a | n/a | n/a | 0 | 1 | 0-3% | <= 35 ms |
| A | 1 | n/a | 31/31 | 7.92 s | 1 | 5 | 48% | <= 17 ms |
| B | 3 | 12 s | 93/93 | 7.54-8.73 s | ~2 | 5 | 41-57% | <= 68 ms |
| C | 6 | 3 s | 186/186 | 6.86-7.26 s | ~4 | 9 | 153%, then 128% | <= 16 ms |

Postgres CPU is `docker stats` (100% = one core). Container memory stayed
between 373 and 508 MiB. Host load1 stayed at 9-10 throughout; no lock
waits appeared in `pg_stat_activity`. Josh asked for staggered starts. A run
of this file takes 11-13 s end to end, so a 12 s stagger never overlaps six
runs; the Orchestrator approved a 3 s stagger for step C. Even then the
first runs finished before the last started, so the peak was about four
live runs.

### 5.3 What it shows

- One run of this file costs the test Postgres about half a core while it
  runs. Cost grows roughly linearly with live runs (about 1.5 cores at four).
- At four live runs the hub, the daemon and the host showed no strain. A
  light single-file run does not reproduce the 20:41 incident.
- The incident numbers point elsewhere. At 20:41-20:57 the Orchestrator
  measured 24 pytest and 22 rustc/ld processes live, load1 71, the test
  Postgres VM at 85% CPU and 31.8 GB RSS, and `/api/health` up to 9.5 s. Four
  live runs here used under 0.6 GB. The 31.8 GB figure is the Docker VM's
  footprint, which macOS reports for the whole VM; the window did not test
  whether larger files or package-scope runs drive it.
- Earlier samples on 2026-10-15 (RM gobby#15991) were package-sized runs: a
  six-file run with 240 s of silent stalls before 235 pass / 21 fail in
  101 s; a 31-file run that hit its 420 s timeout at 311/581; ten files 549
  pass in 349 s; seven files 397 pass in 108 s. Those stalls happened while
  rustc load was also high, so they cannot be attributed to the hub alone.

Not measured: package-scope or multi-file concurrency, and Docker VM memory
under load. Both need another operator-approved window.

## 6. Recommendation

Draft; items 1 and 2 firm up when R2 lands. Ranked by load removed per unit
of work. Each item is a follow-up for the Orchestrator to file; this task
changes no shared config.

1. **Admit heavy work by host load, and size the job cap from it.** The RM
   heavy slot counts cargo runs, but the daemon cares about total runnable
   threads. Grant a cargo run only while load1 is under a set threshold, and
   set `CARGO_BUILD_JOBS` from the idle cores at grant time instead of a
   fixed 3. Evidence: R1 (section 2.1), the 20:41 incident (load1 71 with
   24 pytest and 22 rustc/ld live). R2 gives the j6 data point for the
   formula.
2. **Run cargo and nextest at background priority.** `taskpolicy -b` (or
   `nice`) lets the scheduler favour the daemon over compile threads without
   capping throughput on an idle host. Unmeasured: needs one paired run
   (same build, with and without) under the same watcher.
3. **Share third-party crate builds across checkouts with sccache.** Every
   new worktree recompiles the same dependency graph cold (section 3.2);
   sccache removes that work while workspace crates stay per checkout.
   Needs Josh's go to install, plus an SRT policy entry for the cache dir
   and socket (section 4.1).
4. **Cap nextest test processes.** nextest defaults to one test process per
   CPU (18). Set `test-threads` in `.config/nextest.toml` so concurrent runs
   cannot multiply into dozens of test processes (section 3.1).
5. **Release LTO.** Pending R5/R6; thin LTO would shorten every cutover
   build if its binaries hold up.

Test hub: no change recommended now. Every pytest process already gets its
own database (section 5.1), and four live single-file runs left the hub,
the daemon and the host unstrained (section 5.2). Keep the cap at 3 live
hub runs. Measure package-scope runs in a separate approved window before
changing it.

Rejected: Rust builds in containers (section 4.2).

## 7. Coverage against the task's validation criteria

| Item | Status | Gap |
| --- | --- | --- |
| 1. Baseline table, sandboxed seat and operator shell | Partial | Operator shell: one warm coherent-set cutover row with wall, user+sys and load (section 1.3). Seat rows are wall-only. Peak RSS is unrecorded everywhere because `/usr/bin/time -l` is blocked by the shell-write scope gate. No cold release build, no standalone `gobby-client` release build and no operator-shell nextest row. |
| 2. Causes of `CARGO_BUILD_JOBS=3` and per-worktree targets, with cost | Done for cause; cost partial | R1 gives the cold dependency cost at 18 jobs; the j6 comparison is R2. |
| 3. At least three native options measured, with deltas | Open | R1 void; R2 (j6) queued behind cutover-critical runs; R3-R6 need a quiet window. |
| 4. sccache and containers assessed | Done on paper | sccache unmeasured (install needs Josh's go). |
| 5. Test-hub table at 1, 3 and 6 runs, yes/no on more databases | Done | Peak was about four live runs; package-scope runs unmeasured. |
| 6. Ranked recommendation naming follow-ups | Draft | Items 1-2 firm up with R2. |

_Last verified: 2026-10-16_
