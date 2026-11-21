# Rust Workspace (`crates/`)

The former `gobby-cli` repo now lives here — Gobby is a monorepo. The Rust code is
a Cargo workspace (`Cargo.toml`, `Cargo.lock`, and `rust-toolchain.toml` at the
repo root). Use `cargo` for all Rust operations, respect `rust-toolchain.toml`,
and load the `rust` skill before editing Rust. Rust-specific conventions are
defined in this file. Shared architecture facts (crate-to-binary mappings and
the rebuild/reinstall new-inode requirement) live in `AGENTS.md`.

```bash
# Build / check a specific crate (use -p; avoid whole-workspace builds unless needed)
cargo build -p gobby-code                 # gcode CLI
cargo build --release -p <crate>          # one release artifact; install it via a new inode (AGENTS.md)
# Required: start the isolated test hub and set its schema-test URL.
GOBBY_SCHEMA_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  uv run --no-sync gobby cutover --wait   # build/install the binary set, pin, restart, smoke
                                          # refuses uncommitted schema inputs (--allow-dirty overrides)
                                          # proves `schema plan` plus rollback-only `schema preflight`
                                          # before promoting; nothing is promoted or stopped on refusal
                                          # under a window hold, `--hold-token TOKEN` presents its token
                                          # (`gobby install --hold-token TOKEN` takes the same option)

# Lint & format — match repo config; never relax lints to pass
cargo clippy -p <package>                 # e.g. gobby-code, gobby-core, gobby-daemon, gobby-hooks
cargo fmt -p <package> -- --check         # drop --check to auto-format

# Tests — run through nextest, the repo's configured runner
# (.config/nextest.toml routes DB-backed tests into a serial group; bare
# `cargo test` bypasses that and flakes). Scope with -p or a filter —
# workspace-wide runs are slow and mostly irrelevant to your change.
cargo nextest run -p gobby-code
cargo nextest run -p gobby-core -E 'test(grant)'
cargo test --doc -p <package>             # nextest does not run doctests

# PostgreSQL-backed gcore schema tests need the postgres feature and the test hub:
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-core --features postgres

# gcode DB tests compile when DATABASE_URL is set at build time (GOBBY_POSTGRES_TEST_*
# are ignored). The fixture applies the schema and seeds this machine's row itself, so
# it works only in the `gobby_gcode_test` namespace on the hub DATABASE_URL names, never
# gobby_test's pytest-managed schema. The fixture selects `gobby_gcode_test_v<compiled schema>_<root hash prefix>`
# on this hub and creates it with pg_search when absent:
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  cargo nextest run --profile ci -p gobby-code --test-threads 1
# Run every module, including DB tests without serial_db in their names.
# One nextest process at a time protects their shared fixture database;
# serial_test guards alone do not serialize separate nextest processes.
```

Set `GOBBY_SCHEMA_TEST_DATABASE_URL` for every cutover. The candidate catalog
preflight needs an isolated schema-test database; `DATABASE_URL` alone does not
supply it. The example uses the local test hub, which must be running. A missing
setting refuses activation before the daemon stops or any binary is promoted.

`gobby-terminal` builds a vendored Zig library (libghostty-vt) whenever the
`vt-engine` feature is on, which the `gterm` binary requires. That build needs
Zig 0.16: `ZIG` when set, else the PATH `zig` when it is 0.16, else Homebrew's
keg-only `zig@0.16` under `/opt/homebrew` or `/usr/local`
(`crates/gterminal/build_zig.rs`). It compiles against whatever macOS SDK
Command Line Tools ships, including 27; no `DEVELOPER_DIR` or Xcode selection is
involved.

```bash
cargo build --release -p gobby-terminal --features vt-engine --bin gterm
```

Builds land in deterministic checkout-specific directories under
`~/.gobby/cache/cargo-target-v2/<project_id>/`. Gobby links each registered
checkout's `target` there for your shell's builds. Unsandboxed spawned agents build
with Gobby's shared Cargo home (`~/.gobby/cache/cargo-home`), and Cargo
fingerprints embed the home's registry paths, so their `CARGO_TARGET_DIR` is a
sibling `<checkout>-agent` directory; sharing one target across two homes would
rebuild every dependency on each alternation. Installers and `gobby cutover` read
release artifacts from `CARGO_TARGET_DIR` when it is set. Each Cargo home is shared
across checkouts, so registry and Git inputs are reused without allowing different
checkout revisions to exchange compiled artifacts. A pre-existing
real `target/` directory or foreign symlink is left alone. On macOS, large concurrent
target trees can still require a higher vnode ceiling; see
`docs/guides/system-requirements.md`, Troubleshooting.

Each checkout therefore owns the binaries below its `target/` link. Tests and probes
that execute a binary by path should still build it first so their source-to-binary
provenance is explicit.

Inline `#[cfg(test)]` modules count toward the owning production file's
1,000-line ceiling. Keep large unit-test modules out of production Rust files.
Place the tests at `<module>/tests.rs` and declare them from `<module>.rs` with:

```rust
#[cfg(test)]
#[path = "<module>/tests.rs"]
mod tests;
```

## Daemon family crates

Stage 2 of `ROADMAP.md` absorbs the Python daemon one route family at a time,
each as its own crate statically linked into `gdaemon` (decision 16):

- Directory `crates/g<family>`, package `gobby-<family>` (`crates/gtasks` →
  `gobby-tasks`), `publish = false`, version inherited from the workspace.
- Add the crate to the root `Cargo.toml` `members` list when its Stage 2 epic
  starts; no placeholder crates.
- Own the family's row types and repositories, built on the `gcore` async pool
  and transaction seam. Reach another family only through its public API;
  Cargo rejects the cycle otherwise, and that is intended.

### The family contract

A family crate depends on `gobby-core` with its `route-family` feature, never on
`gdaemon`. It exports one static that implements
`gobby_core::route_family::RouteFamily` over the front door's state. Only `name`,
`prefixes` and `router` are required; every other method has a default, so a family
overrides only what it uses:

```rust
use axum::Router;
use gobby_core::bootstrap::RouteBackend;
use gobby_core::route_family::RouteFamily;

pub struct TasksFamily;
pub static TASKS: TasksFamily = TasksFamily;

impl<S: Clone + Send + Sync + 'static> RouteFamily<S> for TasksFamily {
    fn name(&self) -> &'static str {
        "tasks" // the family's front_door.routes key
    }
    fn prefixes(&self) -> &'static [&'static str] {
        &["/api/tasks"]
    }
    fn router(&self) -> Router<S> {
        Router::new() // .route(..).with_state(own) for the native handlers
    }
    fn default_backend(&self) -> RouteBackend {
        RouteBackend::Proxy // until the checkpoint's activation commit
    }
}
```

- Registration. A family registers with one line in `FAMILIES` in
  `crates/gdaemon/src/front_door/registry.rs`, one family per line in alphabetical
  order, plus its dependency line in gdaemon's `Cargo.toml`, and families never edit
  `serve.rs`. Runner wiring in `serve.rs`, such as a supervisor, recovery or port
  slot, changes only through a Found Work line that names the slot.
- One backend per family. The family's name is its `front_door.routes` key, and there
  is one backend per family: `Proxy`, `Native` or `Compare`, from the routes entry or
  else `default_backend`. Compare applies to the whole family, so routes that move on
  different cuts form a cut group: register one family per cut group, each with a
  distinct name.
- Routing. `router` serves the native handlers under `prefixes`, matched on whole
  path segments; a request under a prefix that no handler matches is proxied, and
  `claims(&Parts)` returning false proxies a request unchanged. `route_claims`
  declares `RouteClaim` rows (method, `PathMatch`, `Gate`, `Credential`, `PeerRule`,
  `Kind`) that the front door's claim runner answers through `serve_claim` before
  the routing table, and `claim_enabled` turns them off for a state. Rows apply only
  while the family's backend is Native. Within a phase the most specific path wins
  (exact, then segment, then raw prefix, then any); equal specificity goes to the
  lower `start_rank`, then to registry order. Paths match raw, never percent-decoded.
  `Credential::AfterAuthenticate` runs after authentication, but identity may still
  be absent: a handler that acts on a caller reads `FamilyContext::caller` and
  refuses `None`.
- Compare. The caller always receives the proxied response, and every outcome goes
  to the compare log that `gdaemon compare-report` summarizes. GET and HEAD run both
  legs and compare them. Any other method proxies once and calls `check_proxied`
  with the request and the proxied response; it returns a `ProxiedCheck`:
  `Skipped`, `Verified`, or `Mismatch(reason)`, where the reason names a JSON pointer
  or a cause and never a request or response value.
- Context. The hooks reach the process's `FamilyContext` (`activate` through
  `Activation::ctx`; `deactivate` takes only its `StopMode`): the frame publisher
  for WebSocket broadcasts, the backend and lease status watches, the pool budget
  and serve pool, the home directory, the configuration epoch (`config_epoch`,
  `subscribe_config`) and its one publisher (`config_publisher`), the degraded set,
  `caller` for the identity the front door authenticated, an optional startup
  recorder, and `Extensions`, the typed handles a later chunk inserts before
  families start, read with `extension::<T>()`. A handle the daemon may lack (no
  database, no lease, no runtime) is an `Option`, and `FamilyContext::unconfigured()`
  holds none. A family takes these handles from the context and builds none of its
  own.
- Lifecycle. Hooks run only for Native and Compare entries; a Proxy family runs
  none. They run in `start_rank` order, lower first, with registry order inside a
  rank. `start` runs once per process, before admission. `activate` runs once per
  lease and may register jobs through its `Activation`. It runs before the supervised
  lifetime has verified the lease (the entry heartbeat and the job admission come
  after every `activate`), so only the jobs it stages are held back. Any other effect
  of an `activate` hook that needs the lease's authority must carry its own fence:
  stop on the context's lease state, or guard the effect in its own SQL. Heartbeats
  verify that lease state only from the admission to the lifetime's last heartbeat
  before its release. Before the admission, and through the teardown that follows a
  terminal restart (which keeps the lease), it can read Active with nothing verifying
  it. On every other exit the lifetime publishes Lost where its heartbeats end,
  before it waits to release, so the lease state does not read Active through that
  wait. `deactivate` runs at lease
  release (`StopMode::Stop`, a stop) or a drain (`StopMode::Drain`), in reverse
  start order, and the whole pass shares one stop deadline. `before_spawn`
  (`SpawnKind::FirstStart` or `Respawn`), `after_healthy` and `recovery_complete`
  are the hook points of the worker supervisor and the recovery driver.
- Hook errors. `HookError::Ordinary` marks the family degraded and the pass
  continues with the other families. `HookError::Fatal` stops the pass and says
  whether a relaunch could clear the cause. The unwind deactivates in reverse the
  failing family first, since its hook may hold part of what it acquired, then the
  families already started, all under one deadline that `start_families` and
  `activate_families` count from the moment the unwind begins (their
  `unwind_budget`), so a slow hook cannot spend the cleanup budget. `deactivate`
  must therefore be safe on a family whose `start` or `activate` stopped part way.
- Jobs. `Activation::register_job(name, interval, run)` stages a periodic job for
  the lease. Only a Native family may register one; Compare is refused with
  `JobRefused::Compare`. Staging applies the job sink's own rule
  (`JobSink::validate_job`; the daemon's sink refuses a zero interval, an empty
  name, a name already held and any registration after it started), so the
  family sees the refusal when it stages. Staged jobs reach the sink only when the activation pass succeeds, so a fatal failure leaves nothing
  registered and a retry registers the same names cleanly. An ordinary failure
  discards only that family's staged jobs. If the sink still refuses a job at
  commit, that is an ordinary failure of its family, and the jobs that pass
  registered for the family are removed (`JobSink::remove_job`). The lease's jobs
  are admitted by the supervised lifetime: `BackendSupervisor::run_ending` calls
  `LeaseScoped::admit` under the lease lock, right after its entry heartbeat
  verified the lease, so its four early returns (a sticky refusal at entry, no PID
  claim, standby, an error from `lock_lease` at entry) start no job, and neither
  does an `admit` that fails. The lease's jobs end while it is still held
  (`LeaseScoped::end`), on every exit of a lifetime that reached `supervise`: an
  orderly end drains them before the lease is released, and a lost lease, which
  cannot be held through a drain, cancels them at once. The job host is fenced by
  the lease watch, so once the lease is no longer held the running job is cancelled
  and no job starts again. Lease loss wins over shutdown: the fence is watched
  through the drain grace, and a loss that is ready with the stop or arrives during
  the drain aborts and joins the running job at once, and a job whose result is
  ready in the same poll as the loss is logged as cancelled, not reported. The
  lease keeps its heartbeat through the worker drain and the pre-release end, and
  no further. A supervised heartbeat waits for the lease lock for at most
  `HEARTBEAT_LOCK_LIMIT` (6 s; the 120 s `lease_lock_limit` is the budget of a
  lifetime's entry and release only) and is polled beside everything else the
  supervisor waits on (a shutdown, the worker's exit, the handshake, a backoff, the
  worker drain, the pre-release end), so a lock wait holds none of them. Every
  heartbeat error, a lock wait that reaches its bound included, publishes Lost,
  which aborts the running job. While the supervisor waits in one of those selects,
  a lease that can no longer be verified is published Lost within 12 s of its last
  verified heartbeat (interval 1 s, lock bound 6 s, operation bound 5 s). A worker
  spawn (at most 10 s) is awaited outside them: the heartbeat due after it starts
  at once, so across a spawn the bound is 21 s (spawn 10 s, lock bound 6 s,
  operation bound 5 s) plus the intent marker's snapshot read that precedes the
  spawn, which is awaited outside them too and has no bound. So a leased-family job
  can run 21 s past the last verified heartbeat before it is aborted, and longer by
  however long that one file read takes: nothing bounds it. The supervisor never
  drops a heartbeat in flight: the lifetime's `Pulse` owns it across select arms,
  and `run_ending` awaits it before it takes the PID claim or releases. It never
  leaves one unpolled either. A heartbeat queued on the lease lock is handed the
  lock unpolled and would then hold it, with its own bounds stopped, until it is
  polled. So every await of the supervised lifetime is a `Pulse` method or a select
  with a pulse arm, or is passed to `Pulse::through` (the reap after a worker's exit
  or a failed handshake, the marker read after an exit or a drain, and the marker
  read after each heartbeat in the wait loop), or is a bare await directly after a
  pulse call returned the heartbeat's result, when none is in flight (the marker
  snapshot, the spawn, the two drains after a lost lease). The first two kinds hold
  by construction, since the await itself polls the heartbeat. The four bare awaits
  hold only by where they stand, and nothing in the code checks that. A new await in
  `supervise` must be one of the three; a wait that can follow a select another arm
  won is never bare, however short, and when in doubt a wait goes through
  `through`. A heartbeat that fails beside a wait passed to `through` publishes
  Lost, and the wait still returns its own result. Aborting a job cancels only its task: work it
  already dispatched to a blocking thread or already sent as SQL (the node hook
  spool prune's `spawn_blocking` is an example of such work) is neither cancelled
  nor joined, so a leased-family job's work must be safe to finish after a lost
  lease or be fenced by its own SQL.
- Lease lifetime. `ActiveDaemonLease::begin_lifetime` is the run loop's only
  acquisition and returns a `LeaseLifetime`. While a lifetime is open, `try_acquire`
  and `recover_stale_owner` refuse, and the admin promote and recover routes answer
  409 `this daemon's lease lifetime has not ended` unless the lease is active (an
  active lease keeps its answers: promote 200 without promoting, recover its own 409
  `lease owner is already active`). A lifetime stays open until the run loop passes
  its guard to the next `begin_lifetime`, which ends it and acquires under one hold
  of the lease lock (a guard issued by another lease is refused before acquiring and
  leaves this lease unchanged); a guard dropped any other way (stop, refusal, error) never
  reopens acquisition in this process. Inside one lifetime the watch leaves Active
  at most once and never returns, so the job host needs no generation on its watch.
  `fence()` is None unless the watch reads Active, and `heartbeat()` fails, keeping
  its session, on a lease published Lost. A lifetime is over, though still open,
  once its `run_ending` has returned anything but RestartTerminals. From then on the
  watch does not read Active: `LeaseLoss::publish` is the one loss path, and it only
  replaces Active. The lease watch fences the job host and nothing else: the terminal
  host (with its own epoch authority) and token-event retention (elected by its own
  advisory lock) start in `ActiveServices::start`, before the entry heartbeat, and
  run until `ActiveServices::stop`. `run_ending` publishes Lost itself where its
  heartbeats end, after the pre-release end and before it waits for the lease lock
  to release, on every exit but RestartTerminals. Through that wait status reads
  standby and promote and recover answer the lifetime 409; the release then
  publishes Standby.
- Pool. A family queries through `FamilyContext::serve_pool`, the shared serve pool.
  Each native pool takes `POOL_SHARE` connections from the budget that
  `gobby_core::database_concurrency::pool_budget` resolves from the stored
  concurrency keys, and a budget below four shares marks `database_pool` degraded
  and is one of the start's warnings.
- Logging and metrics. `FamilyContext::logger(name)` returns a `FamilyLogger`
  whose namespace is `gobby.<family>` and whose counters and histograms carry a
  `family` label in `gobby_core::telemetry`. A family logs through `tracing` under a
  `gobby` target, which is the only namespace the routed log files record, and never
  prints to stderr. The configured log directory and its parents must be writable
  only by the daemon's user: the writer follows symbolic links and opens
  non-regular files on every open and reopen, as the Python writer does. Shared
  log directories are not supported.
- Errors. A family maps its failures onto the `DomainError` classes and renders
  them through the `gobby_core::domain_error` adapters
  (`docs/contracts/domain-errors.md`). It defines no error framework or status
  mapping of its own.
- Dormancy. A family ships with `default_backend` returning `Proxy`, and no runtime
  switch turns it on. Its checkpoint's activation commit changes the compiled
  default to `Native`; the change reaches a daemon only through `gobby cutover`, and
  rollback restores an earlier binary generation (`docs/guides/configuration.md`,
  Family activation and rollback). `front_door.routes` is a harness and
  isolated-test override, not an operator switch.
- Freeze. The seam's production files are frozen: the `### Frozen files` list in
  the `seam-contract` kept block of `docs/port/record.md` (section Frozen seam
  contract) names them, and `gport check-slice` refuses a change to one unless a
  Found Work line under the changing chunk names its path.

The crate → binary map and the rebuild-and-reinstall requirement (including the
new-inode install step macOS needs) live in `AGENTS.md` under Architecture Facts.

Keep managed binary versions, lockfile version entries and managed pins out of
source branches. When landing into `0.5.0`, `land_commit` generates a `0.0.1` patch
bump against the locked integration tip for every affected managed dependency
closure, with matching `Cargo.lock` and managed pins. This includes `gcode`,
`gdaemon`, `ghook`, `gclient`, `gmcp`, and `gterm`, including unpublished managed
binaries. Child landings into an epic branch do not bump. Publishing remains
separate, and gcode maintenance floors derive from its managed pin.
Unpublished binaries go through the checkout build on every `gobby install`; a
changed source without a bump is refused promotion, so the installed binary
stays stale.
The shared workspace promotion guard records version/source identity
in `.<binary>-release-identity.json` and refuses same-version changed-source
promotion. Cutover checks it before daemon stop; separate component installs
use the same check. Same-source re-promotion is permitted.
A version that a rollback restore burned is refused by every later promotion, so a
crate that must ship again needs a version bump before `gobby cutover`.
