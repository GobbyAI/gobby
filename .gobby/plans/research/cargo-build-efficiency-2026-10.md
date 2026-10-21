# Cargo build and test-hub load: evidence report

Task: #23902 (Research why concurrent cargo builds and test-hub pytest load
this machine, and fix the setup), under #22801 (Lane 4). Author: lane 4
developer gobby#16907. Started 2026-10-15 21:00 CDT. Source binding: worktree
`lane-4-rust-4` at `81f7f5e683` on 0.5.0 `3ae83e1a2c`.

Status: incomplete research, recovered on 2026-10-20 by gobby#17265. Historical
observations remain attributed to their original sessions; they have not been
rerun or reconstructed from raw logs. Sections 1 and 3 hold the findings and section 5
the test-hub window. Sections 2 and 4 fill in as the Release Manager grants
the cargo runs. Every number below names its source. Nothing in this task
changes shared config, spawn policy or the test hub.

Historical seat limits: the seat ran under SRT, which denied `ps`/`pgrep` process
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

The host is an 18-core MacBook Pro (`getconf _NPROCESSORS_ONLN` = 18).
Historical observations suggest three possible multipliers; controlled
measurements are still needed to separate their costs:

1. **Every checkout compiles the whole dependency graph cold, up to three
   times.** Each checkout gets its own operator, agent and sandbox Cargo
   target (section 3.2). On 2026-10-15 there were 43 live worktrees, 45
   sandbox targets (261 GB) and 94 operator/agent targets (123 GB). A cold
   `cargo nextest run -p gobby-code` from a fresh sandbox target did not
   finish compiling dependencies inside 1200 s at `CARGO_BUILD_JOBS=3` under
   load near 30.
2. **Parallelism is capped per command, not per machine.** `CARGO_BUILD_JOBS`
   bounds rustc jobs inside one cargo invocation. Nothing bounds the number
   of invocations or nextest's test processes (default: one per CPU, so up to
   18 per run before test-group limits). In the ungated window 20:41-20:50 the five-minute load reached
   71/41/22 with health checks at 9.5 s (RM gobby#15991).
3. **A fixed job cap needs a controlled comparison.** `CARGO_BUILD_JOBS=3` was sized
   for six concurrent heavy slots (6 x 3 = 18 cores). This arithmetic does
   not measure idle cores or account for background work. R1 started at
   load1 13-16, held load1 at 40-42 and saw `/api/health` reach 5.59 s, and
   the watcher stopped it (section 2.1). Load1 stayed high even when only
   one rustc was live, but those samples do not establish which process
   supplied that load. Codegen parallelism and background work both need
   consideration when comparing job caps.

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
includes build and promotion. The whole command averaged 187% CPU, including
stop, health wait and promotion. This does not isolate the build's CPU use
or prove a serial LTO bottleneck. A build-only observation with `--timings`
and a paired fat/thin LTO comparison are still needed.

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
| Job cap (`CARGO_BUILD_JOBS` 18 vs 6 vs 3) | Cargo jobserver parallelism | Primarily spreads work; CPU and memory deltas require measurement | R1 vs R2 |
| Dev debuginfo `line-tables-only` | DWARF emitted and linked per crate | Removes work and disk | R3 vs R1 |
| Warm incremental rebuild | Reuses a checkout's warm target | Removes work | R4 |
| Release `lto = "thin"` instead of fat `lto = true` | Cross-crate optimisation | Can reduce link work; local benefit unmeasured | R5 vs R6 |
| `nice` / `taskpolicy -b` | Scheduling priority | Spreads only; protects the daemon's latency | paper |
| nextest `test-threads` cap | Test processes per run | Spreads only | paper |
| Linker | External final linking | Actual linker invocation and local benefit unmeasured; no replacement installed | paper |

## 3. Causes

### 3.1 Who sets `CARGO_BUILD_JOBS=3`, and why

The original source audit found no code setting it:
`gcode grep -w CARGO_BUILD_JOBS src/ crates/` found nothing.
The same scoped search completed with no hits on 2026-10-20 at recovery HEAD.
It is policy text in `.gobby/workflows/rules/project-operating-policy.yaml`
("Every granted cargo run sets CARGO_BUILD_JOBS=3"), added by `be77687bb6`
(#24319, 2026-10-13) next to the six-slot heavy-run cap: six slots of three
jobs fill 18 cores. Seats type it into each command, and plans copy it.

The arithmetic describes a full six-slot fleet. With one slot it permits
three Cargo jobs; it does not establish that the other 15 cores are idle.
It also misses two sources of parallelism:

- nextest defaults to one test process per logical CPU, subject to configured
  test-group limits. The original three groups were `ghook-process` 2,
  `serial-db` 1 and `gterm-pty` 1; the recovery checkout also has a
  `gdaemon-lease` group capped at 1. The 18-per-run default is an upper bound,
  not a measured count or a claim that group-capped ghook tests run 18 at once.
  Cargo's build-job cap does not set nextest's test-process budget.
- The `gclient-test-gterm` setup script builds `gterm` inside a nextest run,
  outside the run's own build step.

Current group limits were read directly from `.config/nextest.toml:37-41`:
`gcode evidence` returned `invalid_selector` because that config path is not
visible in the caller's index. The fallback `rg -n` returned
`38:ghook-process = { max-threads = 2 }`, `39:serial-db = { max-threads = 1 }`,
`40:gterm-pty = { max-threads = 1 }`, `41:gdaemon-lease = { max-threads = 1 }`.

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

Recovery audit of `src/gobby/agents/cargo_target.py:91-110` confirms the
home split remains documented in code: `94| Sandboxed runs build with their
own ``CARGO_HOME``, and fingerprints embed` and `99| return
_sandbox_target_root() / _checkout_component(checkout)` (wrapped here), excerpt
hash `4ee4f9fca27938f98d1120c3c92f5aca8ec71d469e4c84f1c304d1cbe5e629c3`
at recovery HEAD `0f9054f7ed7286b7568e4ebdbdf7f76ec1ec346a`.

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
with `opt-level = 3` for gcode and gdaemon and `"z"` for ghook. Current-source
evidence at recovery HEAD `0f9054f7ed7286b7568e4ebdbdf7f76ec1ec346a`:
`Cargo.toml:16-30`, `17| lto = true`, excerpt hash
`861a78a31522588a754edf6fd71d8236b79fd87fba5f8823b5d1b68408bfa5f0`
(the evidence range includes lines 16-34).

[Cargo's profile reference](https://doc.rust-lang.org/cargo/reference/profiles.html#lto)
describes cross-crate optimization and additional link-time cost. It does
not establish this machine's LTO CPU, memory, or wall-time share. Those remain
pending R5/R6. The same reference documents line-table debuginfo, macOS's
unpacked split-debuginfo default, incremental reuse for workspace/path
dependencies, and the codegen-units throughput tradeoff. These are candidate
variables, not measured local improvements.

## 4. Compiler caching and containers

### 4.1 sccache-style caching

Recommendation: **recommend a bounded dependency-cache pilot**, conditional
on Josh approving an install; expected local saving is unmeasured.

- It removes work instead of spreading it. sccache keys each rustc
  invocation by a hash of its inputs, so a crate compiled once is reused by
  every checkout that compiles the same crate with the same flags.
- It fits the hazard in 3.2. sccache does not cache incremental
  compilations, and dev builds compile workspace members incrementally, so
  workspace crates stay per checkout while third-party crates are shared.
- Cache reuse depends on compiler inputs and path handling. Dependencies live under the shared
  `CARGO_HOME` registry (one for sandboxed seats, one for unsandboxed agents),
  so seats in the same home share paths. Different homes may reduce cross-class
  hits; three separate caches are not an established requirement. Test actual
  hit/miss reasons before proposing home unification.
- Under SRT the sccache server needs a writable cache directory inside the
  sandbox cache and a local socket. Both need a policy entry; this is a
  follow-up, not a config change made here.
- Install: `sccache` (Homebrew or `cargo install sccache`). Needs Josh's go;
  nothing was installed.

[sccache's Rust support contract](https://github.com/mozilla/sccache/blob/main/docs/Rust.md)
requires incremental compilation disabled for cacheable invocations and
excludes crates invoking the system linker, including binaries and proc macros.
It also warns about proc macros reading files. Thus it cannot eliminate the
release binary link and is not automatically a safe cross-checkout cache.
The pilot must prove hits for dependencies, current-checkout workspace output,
SRT server/cache access, and correct misses after changed inputs.

### 4.2 Containers on macOS

Recommendation: **reject** for Rust builds.

- Docker Desktop runs a Linux VM. Builds inside it produce Linux binaries;
  gcode, gdaemon, ghook and gclient ship as macOS binaries, so every
  shipped build would still run natively.
- Bind-mounted source trees cross the VM file-sharing layer, which is slower
  than native APFS in some workloads. No local bind-mount comparison was run;
  Docker's VMM documentation describes file-I/O improvements, so a universal
  slowdown claim would be unjustified.
- The VM's CPU cap only moves the contention; it does not remove compile
  work.

The test hub already runs in a container (`gobby-postgres-test-1`); section
5 covers it. Docker Desktop was not started, stopped or reconfigured.
[Docker documents the Linux VM boundary and VMM options](https://docs.docker.com/desktop/features/vmm/).
The recommendation rests on the required macOS artifacts and additional VM
complexity; it does not claim a measured container performance delta.

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

Reconfirmed at recovery HEAD `0f9054f7ed7286b7568e4ebdbdf7f76ec1ec346a`:
`tests/fixtures/postgres.py:273-323`, `276| database_name =
f"gobby_test_{os.getpid()}_{uuid.uuid4().hex[:12]}"` (wrapped here),
`280| _reap_dead_process_databases(connection, url)`, excerpt hash
`8c0cb1927164cd5020d35500ca6bc70b357fed9b9d9a8345a05b87389428e043`.
The same excerpt shows forced drop on teardown and dead-PID reaping limited
to normalized loopback server identities. A multi-server design would have
to run that cleanup against each selected server; it does not currently
discover other servers.

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

Draft hypotheses; ranking must be revisited after paired measurements. The
historical order below does not establish a measured reduction in load or CPU.
Each item is a follow-up for the Orchestrator to file; this task changes no
shared config, concurrency thresholds or admission policy.

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

Proposed follow-up task titles and costs (no task has been filed here):

| Rank for further evaluation | Proposed implementation task | Expected effect | Cost and decision boundary |
| --- | --- | --- | --- |
| 1 | Pilot sccache dependency reuse under SRT with per-checkout workspace provenance | Removes repeated cacheable dependency compilation on hits; local CPU/wall/RSS reduction unmeasured | Medium: Josh-approved installation, sandbox permissions, cache correctness and hit-rate proof |
| 2 | Compare and adopt thin release LTO if runtime/size checks hold | Potentially removes release optimization work; local saving unmeasured | Low config cost, medium validation cost; depends on R5/R6 |
| 3 | Reduce development/test debuginfo while retaining backtraces | Potentially reduces debug generation, linking and disk writes; magnitude unmeasured | Low config cost; debugger/backtrace tradeoff needs paired proof |
| 4 | Set a measured per-run nextest process budget | Spreads concurrent tests over time; no intrinsic reduction in test work | Low config cost; compare duration and load, retain serial groups |
| 5 | Apply measured background priority to Cargo validation commands | Scheduling preference; no intrinsic reduction in compilation | Low spawn-env/wrapper cost; only adopt if responsiveness improves |
| 6 | Reconcile fleet job budgets with measured Cargo parallelism | Spreads work; jobserver tokens are not a complete host resource model | Medium policy/spawn cost; Orchestrator/Josh own any threshold or admission change |

These are candidate titles, not authority to install or change policy. Shared
configuration changes remain outside this research deliverable. No extra test
databases are proposed from the existing four-live-run evidence.

## 7. Coverage against the task's validation criteria

| Item | Status | Gap |
| --- | --- | --- |
| 1. Baseline table, sandboxed seat and operator shell | Partial | Operator shell: one warm coherent-set cutover row with wall, user+sys and load (section 1.3). Seat rows are wall-only. Peak RSS is unrecorded everywhere because `/usr/bin/time -l` is blocked by the shell-write scope gate. No cold release build, no standalone `gobby-client` release build and no operator-shell nextest row. |
| 2. Causes of `CARGO_BUILD_JOBS=3` and per-worktree targets, with cost | Done for cause; cost partial | R1 gives the cold dependency cost at 18 jobs; the j6 comparison is R2. |
| 3. At least three native options measured, with deltas | Open | R1 void; R2 (j6) queued behind cutover-critical runs; R3-R6 need a quiet window. |
| 4. sccache and containers assessed | Done on paper | sccache unmeasured (install needs Josh's go). |
| 5. Test-hub table at 1, 3 and 6 runs, yes/no on more databases | Partial | Six starts peaked at about four live runs. True simultaneous 3/6-run measurements and per-run end-to-end wall times remain missing; package-scope runs unmeasured. |
| 6. Ranked recommendation naming follow-ups | Draft | Items 1-2 firm up with R2. |

## 8. Recovery and remaining measurement protocol (2026-10-20)

Recovery ledger: `/Users/josh/.gobby/backups/worktree-cleanup/20261020T212057Z/migration-23902/result.json`.
It records original report commit `7ca90e970954e1ca7e5972025b601d2036d1e321`
applied into checkpoint `0f9054f7ed7286b7568e4ebdbdf7f76ec1ec346a`.
No dirty paths were recovered. The checkpoint is WIP, without validation,
review, or completion credit. Historical raw logs/review receipt IDs have not
been supplied in the recovery ledger. Original narrative measurements are
preserved with that provenance limitation.

On 2026-10-20 Orchestrator gobby#17164 deferred the three-command cold packet
until completion-fleet validations drain (message
`25e69896-bdec-4a25-beef-6c9cbdec7b6c` is the request; the reply arrived as
pending P2P context). Host-shell counterparts belong to the Orchestrator.
This report remains incomplete until measurements meet the criteria.

### Recorder and commands

`cargo-build-efficiency/measure.py` records a single observation, with an
1800-second limit plus up to 10 seconds to terminate its process group. It
records exact SHA, checkout, command, Cargo home, overrides, toolchain,
wall/user/sys time, `wait4` child high-water RSS, load1/load5 samples every
0.25 seconds, exit status, timeout, and interruption. RSS is the child-process
high-water value, not aggregate simultaneous compiler memory. Host aggregate
RSS, if sampled separately, must have its own label and sampling interval.
`/usr/bin/time -l /usr/bin/true` was still blocked by the shell-write scope
hook during recovery; this script uses OS accounting directly.

Each cold target must be absent; each warm target must have a passing cold
observation with matching source/context/variant. The script never cleans a
target or overwrites an observation. "Cold" means a cold Cargo target, with
registry downloads and the OS filesystem cache left warm/uncontrolled. It
does not mean a rebooted machine. No source/profile/shared-config edit occurs.
Context is supplied by the launcher and must be corroborated by its actual
sandbox or host run; the flag alone does not prove a sandbox boundary.

Initial requested packet, **one command at a time**, from this worktree:

```bash
uv run python .gobby/plans/research/cargo-build-efficiency/measure.py --context sandbox --root .gobby/research-runs/23902-a --workload coherent --variant baseline --cache cold --seconds 1800
uv run python .gobby/plans/research/cargo-build-efficiency/measure.py --context sandbox --root .gobby/research-runs/23902-a --workload gclient --variant baseline --cache cold --seconds 1800
uv run python .gobby/plans/research/cargo-build-efficiency/measure.py --context sandbox --root .gobby/research-runs/23902-a --workload nextest --variant baseline --cache cold --seconds 1800
```

These commands are prepared, **not executed**. Add `--dry-run` to inspect a
command without creating output or launching Cargo. Host counterpart uses
the same source tree, operator Cargo home and `--context operator`, plus
a distinct research-only `--root`; it must not use the main cutover target.
Only Orchestrator runs the host counterpart. No promotion or daemon restart.

Recorder verification (synthetic shims, no Rust compilation):

```bash
uv run python .gobby/plans/research/cargo-build-efficiency/smoke_measure.py
uv run ruff check --no-force-exclude .gobby/plans/research/cargo-build-efficiency/measure.py .gobby/plans/research/cargo-build-efficiency/smoke_measure.py
uv run ruff format --check --no-force-exclude .gobby/plans/research/cargo-build-efficiency/measure.py .gobby/plans/research/cargo-build-efficiency/smoke_measure.py
```

The lifecycle smoke check passed cold/warm recording, metric presence,
overwrite/missing-warm guards, timeout and TERM interruption. It uses temporary
fake Cargo/rustc executables and discards its synthetic output. This validates
the recorder, and supplies **no baseline or option-performance evidence**.

### Full matrix, retained until measured

For every valid cell below, run both `--cache cold` and then `--cache warm`
using the same root and variant. Run baseline first. Baseline must cover
both sandbox and operator contexts; option deltas must identify their context
and compare the same workload/cache state. Each invocation needs its window;
the table does not authorize an automatic whole-matrix run.

| Variant flag | Coherent release | Standalone gclient release | ghook nextest | Purpose |
| --- | --- | --- | --- | --- |
| `baseline` | pending | pending | pending | Cargo jobs 3; repository-default nextest concurrency |
| `jobs1` | pending | pending | pending | Measure job-cap cost without exceeding current jobs-3 ceiling |
| `nice10` | pending | pending | pending | Same jobs/flags, background CPU priority; spreading, not avoided compilation |
| `thin-lto` | pending | pending | n/a | Fat versus thin, including warm final-link/no-op distinction |
| `line-tables` | n/a | n/a | pending | Full versus line-table dev/test debug info |
| `test-threads1` | n/a | n/a | pending | Test-process cap isolated from Cargo build-job cap |

A no-change warm run measures freshness/no-op reuse, not an incremental
edit rebuild or a new final link. Preserve that distinction in final tables.
R5/R6 need a completed release comparison; a warm no-op cannot establish an
LTO saving. Jobs 6/18 remain ungranted; the historical stopped R1 is not a
replacement for a paired comparison. Incremental/codegen-units/split-debug
and linker remain paper options unless their own paired packet is measured.
Shared target directories remain rejected for workspace artifacts because
of the recorded cross-checkout hazard; a dependency-cache pilot needs install
approval and separate correctness evidence.

For each pair report absolute values and `100 * (variant / baseline - 1)`
for wall, CPU user+sys and RSS, plus load1/load5 maxima and contemporaneous
fleet activity. At least three native options need successful comparable
observations; a timeout is censored evidence, not a passing result or speedup.
If background load differs materially, retain both observations and repeat
in a matched window rather than ascribe the difference to the option.

### Hub window still required

Run `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_hook_receipts.py -q
-p no:cacheprovider --durations=10` in groups of 1, 3, and 6 simultaneous
processes, with the six-process exception explicitly coordinated. Record
each subprocess's end-to-end wall time and test duration separately. The
Orchestrator samples `docker stats` for `gobby-postgres-test-1` and queries
`pg_stat_activity` on the isolated test server for connection counts, distinct
active test databases, and lock waits; record host load1/load5 throughout.
Keep timestamps and maximum overlapping live pytest processes to prove the
requested concurrency was reached. No Docker configuration changes.

Current provisional answer on more databases: **no additional databases
recommended**, because existing fixtures already create one per pytest
process. This says nothing definitive about same-server CPU/I/O capacity at
six live runs. If a controlled window finds server resource saturation,
more databases on that server do not add resources; evaluate separately capped
servers with per-run DSN selection and reaping on each selected server.
Do not claim that design is necessary until measurements justify it.

_Last verified: 2026-10-20 (recovery audit; historical measurements dated above)_
