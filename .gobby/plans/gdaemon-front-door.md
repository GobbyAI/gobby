Plan artifact: `.gobby/plans/gdaemon-front-door.md`

# Gobby 1.0 Stage 1: gdaemon front door

**Plan ID:** gdaemon-front-door

## Overview
`kind: framing`

Stage 1 of the Gobby 1.0 roadmap (epic #21543) puts the Rust `gdaemon` binary in
front of the Python backend and gives it the four things every later stage builds on:
a proxying front door with a routing table, the three run modes (`standalone`, `hub`,
`node`), user API keys bound to machines with node registration, the singleton lease
and backend lifecycle in Rust, and an HTTP contract corpus that gates every Stage 2
native takeover. When Stage 1 closes, story A runs unchanged behind gdaemon, a second
machine can enroll against a hub over pinned TLS with its own key, and every proxied
route family has a recorded parity fixture.

Decisions were elicited across three planning sessions (ROADMAP.md decisions 13, 16,
17; this plan's Decision Record was confirmed on 2026-09-10). This document is
decision-complete; every deliverable below carries its settled design.

## Constraints
`kind: framing`

- **No backward compatibility.** 0.5.0 has not shipped. Each cutover is a single
  commit; there are no dual-accept windows. Intermediate states between leaves are
  work breakdown, not compatibility.
- **Running daemon.** Only three leaves touch a running daemon: 1.3 and 5.2 each need
  one coordinated restart with reinstalled `~/.gobby/bin/` binaries (install by new
  inode); 4.3 is a flag day for every client on the machine. Leaf 4.2 pre-writes the
  local `api_key` into bootstrap so the 4.3 restart finds it. Stale worktrees and
  binaries get 401 until rebased or reinstalled. Each of those restarts runs through
  `gobby restart` or `gobby cutover` (without `--allow-dirty`), which prove the start
  half first with `restart_start_refusal` (#22403) and refuse with the running daemon
  untouched.
- **TLS default.** `front_door.tls.mode` is `off` when `bind_host` is loopback and
  refused otherwise; explicit `self-signed` is permitted on loopback for tests and
  opt-in. Story A never sees TLS. Self-signed TLS is tested end to end (user
  requirement): Rust proxy tests, the e2e fixture's `tls=self-signed` variant,
  `gobby auth login` pinning, and the hub-node pair test all run over it.
- **Ports.** `:60887` HTTP and `:60888` WS stay public on gdaemon. Python binds
  `daemon_port + 100` and `websocket_port + 100` on `127.0.0.1` while the front door
  is enabled (defaults `60987`/`60988`). `:60890` is released; `:60891` managed
  PostgreSQL; `:60892` test hub.
- **Key format is locked** for the hosted version: `gobby_` + 43 base62 + 6-char
  CRC32, SHA-256 at rest. Hashing can be peppered later in place; issuance can gain a
  device-code flow later; neither changes the format.
- **Node model (decision 17).** The node is thin: no datastore, no key table, no
  lease. Crates always dial loopback `daemon_url`; a node's gdaemon relays to
  `hub_daemon_url` over one warm pinned-TLS connection. S2.11 closes as relay. The hub
  reaches a node only over the node-opened channel; there is no daemon-endpoint
  column.
- **Expansion mapping.** The root is #21543. Phases P1, P4, P5 correspond to
  the existing placeholder epics #21551, #21555, #21554. At expansion
  apply the coordinator either re-parents the generated leaves under those epics or
  closes the placeholders as `duplicate` of the generated phase sub-epics. Run modes
  (#21553) and the HTTP contract corpus (#21552) expand from their slice plans.
  #21575 (S4.2) is delivered by the run-modes slice's 2.2 and closed as
  `duplicate` at apply. ROADMAP.md S1.4 row, #21555 text (`sk-` prefix, migration 421,
  runtime-handshake bootstrap, endpoint columns) and #21578's mode wording are
  reconciled at materialization to match this plan.
- **Migration numbering.** At the 2026-09-26 refresh the latest is
  `453_plan_review_source_path.sql`, so this plan's migration is `454`. If another
  migration takes 454 before 4.2 lands, the leaf takes the next free number and names
  it in its close summary. Never a baseline edit.
- **Consumer sweeps.** Exact-symbol Targets were resolved with `gcode outline` and
  `gcode grep -w <symbol> src tests crates -l` on branch `0.5.0` at commit
  `569960eab3` (2026-09-10) and re-swept for 1.1, 1.3, 4.3, and 5.2 at
  `09b0f41781` (2026-09-26), with the 1.4 and 4.5 splits and the new 5.2 runner
  Targets swept at `0cc1ad1ea8` (2026-09-27), and every consumer the code index reports for an exact
  Target is in some deliverable's Targets (validation reports zero consumer-coverage
  warnings). Conventions used: a symbol the plan changes is an exact Target; a file
  that only consumes a changed symbol is a `::*` entry whose scope-reason names the
  consumed symbol and the edit (an import or patch path that moves, an argument
  that changes) or says `verification only` when the file needs no edit and is
  re-run; a module the plan rewrites end to end (`src/gobby/config/bootstrap.py`,
  `src/gobby/utils/local_token.py`, `crates/gcore/src/local_token.rs`) is one `::*`
  entry. The index lists 52 importers of `BootstrapConfig`; all construct it with
  keyword arguments or read existing fields, and every field this plan adds has a
  default (`front_door.enabled: true`, `hub: false`, `tls.mode: off`, `api_key`,
  `api_key_id`, `hub_cert` absent; the hub origin reuses the existing `hub_daemon_url`), so none of them changes. Targets the
  draft listed but the plan only consumes were removed (`DaemonEndpoint`,
  `read_daemon_endpoint_at`, `BootstrapConfig.to_config_dict`,
  `WebSocketServer.start`, `select_test_gdaemon`, `LocalMachineManager.upsert_seen`,
  `BootstrapConfig` in 2.3).
- **Rust conventions.** Load the `rust` skill before editing crates; `crates/CLAUDE.md`
  governs. Every crate change is live only after rebuild and reinstall by new inode.

## P1: Front door proxy (S1.1, #21551)
`kind: framing`

**Goal**: `gdaemon serve` owns the public ports and proxies every request to Python
with a routing table, typed unavailability, and byte-level WS splicing; `gobby start`
launches both as siblings.

### 1.1 Front-door bootstrap keys and the backend port helper [category: config]
`kind: deliverable`

Targets:
- `crates/gcore/src/bootstrap.rs::HubDatabaseBootstrap`
- `crates/gcore/src/bootstrap.rs::parse_hub_database_bootstrap`
- `src/gobby/config/bootstrap.py::*` — scope-reason: the dataclass, `bootstrap_from_mapping`, and the new `backend_ports` helper change together; new fields default so the 42 constructor sites in the consumer sweep need no edit
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `docs/guides/configuration.md`
- `tests/config/test_bootstrap.py::*` — scope-reason: bootstrap parser tests gain the new block's cases

Add a `front_door` block to the bootstrap file, parsed identically by
`crates/gcore/src/bootstrap.rs` and `src/gobby/config/bootstrap.py`. On the Rust
side `HubDatabaseBootstrap` gains `bind_host`, `daemon_port`, `websocket_port`
(parsed the way `read_daemon_endpoint_at` already parses host and port; that
function and `DaemonEndpoint` are unchanged) and `front_door`, because
`gdaemon serve` needs all three ports from one read:

```yaml
front_door:
  enabled: true            # false = S1.1 decision 1 rollback: Python binds the public ports
  routes:                  # family name -> proxy | native | compare; default proxy
    health: native
    terminal_ws: proxy
```

Rules: `enabled` defaults to `true`; unknown route values are a parse error in both
languages; unknown family names are accepted (families are introduced by Stage 2
leaves, and the parser must not gate them). Add one helper per language that returns
the backend port pair: `backend_ports(daemon_port, websocket_port) -> (http, ws)`
computing `daemon_port + 100` and `websocket_port + 100`, exported from
`gobby.config.bootstrap` and `gobby_core::bootstrap`. The block stays bootstrap-only:
`to_config_dict` and `DaemonConfig` are unchanged, and the runner and `gobby status`
read `bootstrap_config.front_door` directly, the way `init_servers` already reads
`bind_host` and `websocket_port` from the bootstrap object. Document the block and
the offset convention in the `### Bootstrap` section of `docs/guides/configuration.md`,
the page `HUB_BACKEND_MIGRATION_DOCS` already links.
`crates/gcore/assets/config/runtime_config_contract.json` is a derived carrier of
`src/gobby/config/` and must be regenerated in this leaf.

**Acceptance:**

- 1.1.1 - Both parsers accept the `front_door` block, default `enabled` to true, and reject an unknown route value. symbol: `parse_hub_database_bootstrap`. file: `src/gobby/config/bootstrap.py`.
- 1.1.2 - `backend_ports` returns the `+100` pair in both languages. test: `tests/config/test_bootstrap.py::test_backend_ports_offset`.
- 1.1.3 - The runtime config contract carrier is regenerated and validation passes. file: `crates/gcore/assets/config/runtime_config_contract.json`.
- 1.1.4 - The block and the offset convention are documented. behavior: "front_door block" in `docs/guides/configuration.md`.

### 1.2 `gdaemon serve`: HTTP proxy, WS splice, routing table, typed 503 [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/main.rs::Command`
- `crates/gdaemon/src/main.rs::main`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/front_door/mod.rs`
- `crates/gdaemon/src/front_door/proxy.rs`
- `crates/gdaemon/src/front_door/ws.rs`
- `crates/gdaemon/src/front_door/health.rs`
- `crates/gdaemon/src/front_door/routes.rs`
- `crates/gdaemon/tests/front_door.rs`
- `crates/gdaemon/tests/ws_golden_proxy.rs`

Add `gdaemon serve` (a new `Command::Serve` arm) that reads bootstrap, binds
`bind_host:daemon_port` and `bind_host:websocket_port` with tokio + hyper (gdaemon is
synchronous today; `serve` introduces the tokio runtime for this subcommand only), and
proxies to `127.0.0.1:<backend http>` and `127.0.0.1:<backend ws>`.

- **HTTP proxy** (`front_door/proxy.rs`): forward method, path, query, headers, and
  body untouched; stream responses; no buffering of bodies; `Connection`/hop-by-hop
  headers handled per RFC 9110.
- **WS splice** (`front_door/ws.rs`): forward the upgrade request; after both legs
  return 101, `hyper::upgrade::on` on each leg and `tokio::io::copy_bidirectional`.
  permessage-deflate, ping/pong, close codes, and backpressure pass through
  byte-for-byte. Applies to `:60888`, `/ws` on `:60887`, and the dev HMR socket.
- **Routing table** (`front_door/routes.rs`): the `front_door.routes` map from 1.1,
  read at start. `proxy` forwards; `native` dispatches to a gdaemon handler (only
  `health` exists in Stage 1); `compare` runs both and logs a diff, returning the
  proxied response (used by Stage 2). A flip is an edit plus a gdaemon restart; no
  SIGHUP reload in Stage 1.
- **Typed unavailability** (`front_door/health.rs`): while the backend refuses
  connections, proxied routes and native `/api/health` return 503 with
  `Retry-After: 1` and body
  `{"status": "unavailable", "backend": {"state": "down" | "starting", "target": "127.0.0.1:<port>"}}`;
  502 only when the backend answers malformed. Once Python listens, its own health
  body passes through untouched. `starting` is reported when the backend process is
  known to have been spawned and has not yet accepted a connection (the supervisor in
  5.2 supplies that fact; until then the state is `down`).
- **Rollback**: `front_door.enabled: false` makes `serve` exit with a typed message;
  the topology fallback lives in 1.3.

Validation is in-crate: `tests/front_door.rs` runs the proxy against an in-process
hyper stub backend (path and header passthrough, streaming body, typed 503 on a
refused backend, WS splice including deflate frames and close codes, using the
workspace's `tokio-tungstenite` as the client). `tests/ws_golden_proxy.rs` streams
every fixture in `tests/fixtures/terminal_ws_golden/` through the proxy to a stub
that echoes, asserting byte equality on both legs.

**Acceptance:**

- 1.2.1 - `gdaemon serve` proxies HTTP with headers and streaming bodies intact. test: `crates/gdaemon/tests/front_door.rs::http_passthrough_preserves_headers_and_body`.
- 1.2.2 - WS upgrade is spliced byte-for-byte including deflate and close codes. test: `crates/gdaemon/tests/front_door.rs::ws_splice_preserves_deflate_and_close`.
- 1.2.3 - A refused backend yields the typed 503 body with `Retry-After`. test: `crates/gdaemon/tests/front_door.rs::refused_backend_returns_typed_503`.
- 1.2.4 - The routing table honors `proxy`, `native`, and `compare` for the `health` family. symbol: `crates/gdaemon/src/front_door/routes.rs`. file: `crates/gdaemon/src/front_door/routes.rs`.
- 1.2.5 - The golden WS corpus replays through the proxy unchanged. test: `crates/gdaemon/tests/ws_golden_proxy.rs::corpus_replays_byte_equal_through_proxy`.

### 1.3 Sibling start topology, backend ports, e2e front-door mode [category: code] (depends: 1.2, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/daemon.py::start`
- `src/gobby/cli/daemon.py::_launch_direct_runner`
- `src/gobby/cli/daemon.py::_is_daemon_healthy`
- `src/gobby/cli/daemon.py::restart`
- `src/gobby/cli/daemon_start.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: registers `start` from `.daemon`; the import moves to `.daemon_start`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/runner.py::main`
- `src/gobby/runner.py::_healthy_daemon_running`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner_init/__init__.py::*` — scope-reason: re-exports `init_servers`; verification only
- `src/gobby/runner_lifecycle.py::*` — scope-reason: consumer of `_healthy_daemon_running`; the bind-race check passes the backend port it failed to bind
- `src/gobby/cli/_install_daemon.py::*` — scope-reason: imports of `start` and `_is_daemon_healthy` move to `gobby.cli.daemon_start`
- `src/gobby/cli/cutover.py::*` — scope-reason: consumer of `restart`, which stays in `daemon.py` in this leaf; verification only
- `src/gobby/cli/hub_backup/cli.py::*` — scope-reason: consumer of `_services_start` and `_services_stop`, which stay in `daemon.py`; verification only
- `src/gobby/cli/pack.py::*` — scope-reason: consumer of `_services_start` and `_services_stop`, which stay in `daemon.py`; verification only
- `src/gobby/cli/hub_backup/rehearsal.py::*` — scope-reason: `_SHARED_PORTS` module constant gains the backend port pair
- `src/gobby/cli/daemon_health.py::health`
- `tests/e2e/conftest.py::DaemonInstance`
- `tests/e2e/conftest.py::daemon_instance`
- `tests/e2e/conftest.py::prepare_daemon_env`
- `tests/e2e/test_daemon_lifecycle.py::*` — scope-reason: runs through the front door
- `tests/e2e/test_daemon_auth.py::*` — scope-reason: runs through the front door
- `tests/e2e/test_autonomous_mode.py::*` — scope-reason: consumer of `daemon_instance`; runs behind the front door, verification only
- `tests/e2e/test_crash_recovery.py::*` — scope-reason: consumer of `prepare_daemon_env`; runs behind the front door, verification only
- `tests/e2e/test_daemon_tmux_isolation.py::*` — scope-reason: consumer of `prepare_daemon_env`; verification only
- `tests/e2e/test_e2e_smoke.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_external_terminal_attach.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_full_workflow.py::*` — scope-reason: consumer of `daemon_instance` and `prepare_daemon_env`; verification only
- `tests/e2e/test_grok_session_deferral.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_inter_agent_messages.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_mcp_proxy_e2e.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_parallel_clones.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_plan_coverage_responsiveness.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_restart_responsiveness.py::*` — scope-reason: consumer of `DaemonInstance` and `daemon_instance`; verification only
- `tests/e2e/test_review_learning_e2e.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_runtime_boundary.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_sequential_review_loop.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_session_tracking.py::*` — scope-reason: consumer of `daemon_instance` and `prepare_daemon_env`; verification only
- `tests/e2e/test_single_active_daemon.py::*` — scope-reason: consumer of `DaemonInstance` and `prepare_daemon_env`; verification only
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_task_close_checklist_e2e.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_terminal_client_stack.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_usage_reporting.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_worktree_merge_live.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/e2e/test_worktrees_e2e.py::*` — scope-reason: consumer of `daemon_instance`; verification only
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: consumer of `DaemonInstance` and `prepare_daemon_env`; verification only
- `tests/mcp_proxy/test_annotate_mcp.py::*` — scope-reason: consumer of `DaemonInstance`, `daemon_instance`, and `daemon_token`; verification only
- `tests/test_runner_lifecycle.py::*` — scope-reason: port binding assertions move to the backend pair
- `tests/test_runner_shutdown.py::*` — scope-reason: consumer of `runner.main`; the pre-start health probe it patches now targets the public port
- `tests/providers/test_version_gate.py::*` — scope-reason: patches `run_gobby`; verification only
- `tests/config/test_restart_config_consumers.py::*` — scope-reason: consumer of `init_servers`; verification only
- `tests/runner_init/test_config_runtime_startup.py::*` — scope-reason: consumer of `init_servers`; verification only
- `tests/cli/test_cli_falkor.py::*` — scope-reason: imports `start` locally; the import moves to `gobby.cli.daemon_start`
- `tests/cli/test_daemon_coverage.py::*` — scope-reason: imports `_services_start`, `stop`, and `status`, which stay; patches that drive `start` retarget to `gobby.cli.daemon_start`; `health` assertions keep the public port
- `tests/cli/test_daemon_falkordb.py::*` — scope-reason: imports `_services_start`, which stays in `daemon.py`; verification only
- `tests/cli/test_cli_daemon.py::*` — scope-reason: imports `_is_daemon_healthy` and patches start-path helpers at `gobby.cli.daemon`; both follow the move to `gobby.cli.daemon_start`
- `tests/cli/test_cli.py::*` — scope-reason: patches start-path helpers at `gobby.cli.daemon`; patches that drive `start` retarget to `gobby.cli.daemon_start`
- `tests/cli/test_daemon_handoffs.py::*` — scope-reason: same start-path patch retarget; `_do_stop` stays in `daemon.py` in this leaf
- `tests/cli/test_daemon_remote_mode.py::*` — scope-reason: same start-path patch retarget
- `tests/cli/test_daemon_set_coherence.py::*` — scope-reason: same start-path patch retarget
- `tests/storage/test_schema_divergence.py::*` — scope-reason: same start-path patch retarget
- `tests/servers/routes/test_admin.py::*` — scope-reason: patches `gobby.cli.daemon` helpers the restart helpers use; verification only
- `tests/test_runner_pid_file.py::*` — scope-reason: imports `start`, which moves to `gobby.cli.daemon_start`; `_healthy_daemon_running` keeps its signature
- `tests/test_runner_env_scrub.py::*` — scope-reason: consumer of `_healthy_daemon_running`; verification only

`gobby start` keeps the pid claim and lease in the Python runner (S1.1 decision 1:
lifecycle moves in 5.2). When `front_door.enabled` is true it spawns `gdaemon serve`
first (from `~/.gobby/bin/gdaemon`, after the admissions `start` already runs: `worktree_daemon_refusal` and
`binary_set_apply_refusal`, which refuses a mixed or mismatched installed set; #22403
replaced the old `_schema_restart_refusal` with `restart_start_refusal`, which
`restart` and `cutover` keep calling unchanged), then the runner with the claim
fd, and waits for native health on the public port. When false it behaves exactly as
today. Move `start` and `_launch_direct_runner` into the new
`src/gobby/cli/daemon_start.py`; `src/gobby/cli/daemon.py` is at 991 lines and this
leaf must split it below the ceiling (the `daemon_start.py` bare-path Target is the
new file).

The runner (`run_gobby`, `init_servers`) binds the backend port pair on `127.0.0.1`
when the front door is enabled and the public ports on `bind_host` otherwise;
`init_servers` builds the `WebSocketConfig` and the HTTP server from the pair, so
`WebSocketServer` and `HTTPServer` are unchanged. `_healthy_daemon_running(port,
host)` keeps its signature: `main`'s pre-start probe passes the public port (gdaemon
answers) and `run_daemon`'s bind-race check passes the port it failed to bind.
`_SHARED_PORTS` gains the pair. `gobby status`/`health` keep reading the public
port. Importers of `start` and `_is_daemon_healthy` follow the move to `daemon_start.py`:
`src/gobby/cli/__init__.py` (command registration), `restart` in `daemon.py` (its
`ctx.invoke(start)`), `_install_daemon.py`, `tests/cli/test_cli_daemon.py`,
`tests/cli/test_cli_falkor.py`, and `tests/test_runner_pid_file.py`. Tests that patch
start-path helpers at `gobby.cli.daemon` (`test_cli.py`, `test_daemon_coverage.py`,
`test_daemon_handoffs.py`, `test_daemon_remote_mode.py`, `test_daemon_set_coherence.py`,
`test_schema_divergence.py`) retarget the patches that drive `start`. `cutover.py`,
`hub_backup/cli.py`, and `pack.py` import only `restart`, `_services_start`, or
`_services_stop`, which stay, so they are verification only.

This leaf depends on 1.4: its restart installs the coherent `gcode`/`gdaemon`/`ghook`
set, and without 1.4's suppression every fail-open hook during a planned shutdown
behind the front door would report a failed post instead of being suppressed.

**Granularity:** one leaf. The start topology and the runner's backend binding are one
lifecycle change: with `front_door.enabled` defaulting to true, a runner that binds the
backend pair without a `gobby start` that spawns gdaemon leaves the public ports
unserved, and the reverse leaves gdaemon proxying to a port nothing binds. The e2e
front-door mode is this behavior's verification, and the `daemon.py` split is a pure
move that the `start` edit forces (991 lines). The ghook suppression is independently
closeable and is 1.4. The Target count is consumer sweep: of the hand-maintained
production files, only `daemon.py`, `daemon_start.py`, `runner.py`,
`runner_init/servers.py`, `runner_lifecycle.py`, `rehearsal.py`, and `daemon_health.py`
change behavior; the rest follow the import move or are verification only.

e2e: `daemon_instance` gains a front-door mode (default on) that launches the pinned
`select_test_gdaemon()` in front of the real runner with an isolated `GOBBY_HOME`,
free public and backend ports, and waits on the public health route;
`test_daemon_lifecycle.py` and `test_daemon_auth.py` run through it.

**Acceptance:**

- 1.3.1 - `gobby start` spawns gdaemon then the runner and waits for public health; `front_door.enabled: false` restores today's topology. file: `src/gobby/cli/daemon_start.py`.
- 1.3.2 - The runner binds the backend pair on loopback behind the front door. test: `tests/test_runner_lifecycle.py::test_backend_ports_behind_front_door`.
- 1.3.3 - The e2e fixture runs the real runner behind the pinned gdaemon and the lifecycle and auth suites pass through it. test: `tests/e2e/test_daemon_lifecycle.py::test_daemon_starts_behind_front_door`.
- 1.3.4 - `src/gobby/cli/daemon.py` is below 1,000 lines after the split. file: `src/gobby/cli/daemon.py`.

### 1.4 ghook treats the typed 503 as unreachable [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/ghook/src/transport.rs::post_and_cleanup`
- `crates/ghook/src/dispatch.rs::*` — scope-reason: consumer of `post_and_cleanup` and `suppress_after_failed_post`; verification only
- `crates/ghook/src/action.rs::*` — scope-reason: consumer of `DeliveryFailureKind`; a typed 503 now renders the existing `Connect` message ("Daemon unreachable"), verification only
- `crates/ghook/src/planned_shutdown.rs::*` — scope-reason: `should_suppress_failed_post` already suppresses on `Connect`; verification only
- `crates/ghook/src/diagnostics.rs::*` — scope-reason: consumer of `DeliveryFailureKind`; verification only
- `crates/ghook/tests/contract.rs::*` — scope-reason: add the typed-503 suppression contract cases

S1.1 decision 4, against current code (re-read 2026-09-27; `daemon_is_reachable` no
longer exists): planned-shutdown suppression is
`planned_shutdown::should_suppress_failed_post`, which requires a fail-open hook, a
`DeliveryFailureKind` of `Connect` or `Timeout`, and a fresh shutdown marker.
`transport::post_and_cleanup` classifies every non-2xx response as `Http`, so the typed
503 would never suppress. The one change: `post_and_cleanup` classifies a 503 whose
JSON body carries `"status": "unavailable"` (the 1.2 typed body, any `backend.state`) as
`Connect`, the same body-sniffing pattern `DeliveryReport::is_retry_backpressure`
already uses for `{"status": "retry"}`. Suppression, the failure message, and
diagnostics then follow unchanged; any other 503 or HTTP error stays `Http`. gclient
needs no change (non-2xx already means unreachable). Split from 1.3 on 2026-09-27: the change is crate-only, has its own
contract test, and is inert until 1.3 puts gdaemon on the public ports, so it lands and
installs first. The body shape is 1.2's; 5.2 adds `refused` to `backend.state` without
changing `status`, so this check needs no later edit.

**Acceptance:**

- 1.4.1 - A typed 503 classifies as `Connect` and suppresses fail-open hooks under a fresh shutdown marker; an untyped 503 stays `Http`. test: `crates/ghook/tests/contract.rs::typed_503_counts_as_unreachable`.

## Run modes and HTTP contract corpus (S1.2, S1.5): slice plans
`kind: framing`

Former P2 (run modes, #21553) is planned in `.gobby/plans/gdaemon-run-modes.md` and
former P3 (HTTP contract corpus, #21552) in `.gobby/plans/gdaemon-http-contract-corpus.md`;
each is a single-phase slice rooted at its epic. The former 2.2 `AppState` containers
move to their first consumer (4.4 or 5.1), and P4 slice planning restores its waits
(former P2, 2.3, 3.1) against the slice leaves.

Open ordering gap for P4 and 5.1 slice planning (from the #23098 audit). 4.4's pair
test asserts that the node runs no maintenance loop (run-modes 2.2), which needs a
running Python node runner. `run_gobby` takes `ActiveDaemonLease` for every bootstrap,
so that runner stands by until 5.1 exempts `node` from the lease. A runnable node
therefore needs the lease exemption before 4.4's pair test. P4 slice planning orders
the exemption first or drops that assertion from 4.4.
- A 4.4 edge to 5.1 would form a cycle, because P5 depends on P4.
- Lane 7's recommendation: drop the maintenance clause and the `2.3` dependency from
  4.4. Launch the pair-test node as a bare `gdaemon serve` with the node bootstrap
  (`watch_parent_fd` needs no Python parent). Add a 5.2 acceptance item stating that a
  `node` gdaemon never spawns or supervises a Python backend.
- Hazard to settle before 5.2: `gobby start` on a node bootstrap parks Python in
  standby, and a manual lease promote or recover there could make the node an active
  daemon on the hub database. Either document the command as unsupported until 5.2,
  or refuse promotion when `run_mode()` is `node`.

## P4: API keys and node registration (S1.4, #21555) (depends: P1)
`kind: framing`

**Goal**: user API keys bound to machines replace the shared daemon token; the hub
front door validates every request; a fresh machine enrolls over pinned self-signed
TLS; a node registers over one channel.

### 4.1 Front-door TLS with self-signed generation and pinned client [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/bootstrap.rs::HubDatabaseBootstrap`
- `crates/gcore/src/bootstrap.rs::parse_hub_database_bootstrap`
- `crates/gcore/src/tls.rs`
- `crates/gcore/Cargo.toml`
- `src/gobby/config/bootstrap.py::*` — scope-reason: the dataclass, `bootstrap_from_mapping`, and the new pinned-client helpers change together; `tls.mode` defaults to `off` so constructor sites need no edit
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/front_door/tls.rs`
- `crates/gdaemon/tests/front_door.rs`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `tests/e2e/conftest.py::daemon_instance`
- `tests/e2e/conftest.py::DaemonInstance`
- `docs/guides/configuration.md`

Bootstrap gains:

```yaml
front_door:
  tls:
    mode: off | self-signed | files    # default: off on loopback bind_host, refused otherwise
    cert: ~/.gobby/tls/front_door.crt  # files mode
    key: ~/.gobby/tls/front_door.key
```

Both parsers enforce the default and the refusal. On `self-signed`, gdaemon generates
an ECDSA P-256 key pair and a ten-year self-signed certificate on first `serve` when
`~/.gobby/tls/` is empty (`rcgen`, new dependency in `crates/gdaemon/Cargo.toml`),
files 0600, SANs `localhost`, the hostname, and every non-loopback address bound at
generation; it prints `front door certificate sha256:<fingerprint>` at every start.
`files` mode loads an operator-supplied PEM pair (covers `tailscale cert` and hosted
certificates). The acceptor is `tokio-rustls` on both public listeners; the WS splice
from 1.2 runs inside the TLS stream unchanged.

`crates/gcore/src/tls.rs` adds the pinned client helper used by every Rust caller
that dials a hub: `pinned_client(pem_path) -> reqwest::blocking::Client` (and the
async form) with a root store holding exactly that certificate and no system roots,
plus `fingerprint(pem) -> String`. Python gets the same in
`gobby.config.bootstrap` as a `verify` path for httpx and an `ssl.SSLContext` for
`websockets`. Clients refuse `http://` to a non-loopback host unless `--insecure`
(4.2 wires the flag).

e2e: `daemon_instance` gains `tls="self-signed"` on loopback (explicitly permitted
for tests); `DaemonInstance.http_url`/`ws_url` return `https`/`wss` and expose
`cert_path` for pinning. `front_door.rs` gains TLS variants of the passthrough,
typed-503, and WS-splice tests.

**Acceptance:**

- 4.1.1 - Both parsers default `tls.mode` to `off` on loopback and refuse `off` on a non-loopback bind. symbol: `parse_hub_database_bootstrap`. file: `src/gobby/config/bootstrap.py`.
- 4.1.2 - First `serve` in `self-signed` mode generates key and certificate 0600 and prints the fingerprint. test: `crates/gdaemon/tests/front_door.rs::self_signed_generated_on_first_serve`.
- 4.1.3 - HTTP passthrough, typed 503, and WS splice pass over TLS with the pinned client. test: `crates/gdaemon/tests/front_door.rs::ws_splice_over_self_signed_tls`.
- 4.1.4 - The pinned client rejects a different certificate and system roots are not consulted. test: `crates/gcore/tests/tls.rs::pinned_client_rejects_unpinned_cert`.
- 4.1.5 - The e2e fixture serves `https` with `tls="self-signed"` and the lifecycle suite passes over it. test: `tests/e2e/test_daemon_lifecycle.py::test_daemon_serves_over_self_signed_tls`.

### 4.2 `api_keys`, key format, issuance routes, and local-key adoption [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/454_add_api_keys.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated derived schema carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: derived schema carrier regenerated
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: derived schema carrier regenerated
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: derived schema carrier regenerated
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated derived schema carrier
- `src/gobby/storage/api_keys.py`
- `src/gobby/utils/api_key_format.py`
- `crates/gcore/src/api_key_format.rs`
- `src/gobby/servers/routes/auth.py::create_auth_router`
- `src/gobby/servers/routes/auth.py::_LoginRateLimiter`
- `src/gobby/servers/routes/api_keys.py`
- `src/gobby/servers/_app_routes.py::*` — scope-reason: mount the api_keys router
- `src/gobby/cli/install.py::_provision_local_api_token`
- `src/gobby/runner_init/helpers.py::ensure_machine_identity`
- `src/gobby/runner_init/storage.py::*` — scope-reason: consumer of `ensure_machine_identity`; the call site is unchanged, verification only
- `src/gobby/config/bootstrap.py::*` — scope-reason: the dataclass and `bootstrap_from_mapping` gain the four key fields; all default to absent so constructor sites need no edit
- `src/gobby/config/bootstrap_io.py::update_bootstrap_yaml`
- `crates/gcore/src/bootstrap.rs::HubDatabaseBootstrap`
- `crates/gcore/src/bootstrap.rs::parse_hub_database_bootstrap`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `tests/storage/test_api_keys.py`
- `tests/utils/test_api_key_format.py`
- `tests/servers/routes/test_api_keys.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: consumer of `_provision_local_api_token`; asserts the minted key lands in bootstrap
- `tests/cli/test_install_coverage.py::*` — scope-reason: consumer of `_provision_local_api_token`; same
- `tests/cli/test_install_prompts.py::*` — scope-reason: consumer of `_provision_local_api_token`; verification only
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::*` — scope-reason: patches `ensure_machine_identity`; verification only
- `tests/storage/test_machines.py::*` — scope-reason: consumer of `ensure_machine_identity`; asserts startup adoption mints the local key

Migration `454_add_api_keys.sql`:

```sql
CREATE TABLE api_keys (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id uuid NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    key_hash text NOT NULL UNIQUE,
    key_hint text NOT NULL,
    label text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX idx_api_keys_machine ON api_keys (machine_id);
ALTER TABLE machines ADD COLUMN last_heartbeat_at timestamptz;
ALTER TABLE machines ADD COLUMN node_version text;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE api_keys TO gobby_daemon_runtime;
```

Regenerate every derived carrier listed in Targets. Many keys per machine; no
uniqueness beyond the hash.

Key format, one helper per language (`src/gobby/utils/api_key_format.py`,
`crates/gcore/src/api_key_format.rs`): `generate() -> str` returns `gobby_` + 43
base62 chars encoding 32 random bytes + 6 base62 chars of zero-padded CRC32 over the
43-char body; `parse(key) -> Body | None` checks prefix, length, alphabet, and
checksum without touching a database; `hash(key) -> str` is SHA-256 hex of the full
key (Python reuses `storage.auth.hash_token`); `hint(key)` is the last 4 chars of the
body. Cross-language vectors live in both test files.

`src/gobby/storage/api_keys.py::ApiKeyManager` (hub-transaction style with `%s`
placeholders): `mint(user_id, machine_id, label) -> (plaintext, ApiKey)`,
`list_for_user(user_id)`, `revoke(key_id, user_id)`, `resolve_hash(key_hash) ->
ApiKey | None` (joins `machines`, `revoked_at IS NULL`), `touch(key_id)` throttled
to once a minute.

Routes (`src/gobby/servers/routes/api_keys.py`, mounted in `_app_routes.py`):
- `POST /api/auth/keys/bootstrap` (public path, behind the existing
  `_LoginRateLimiter` keyed by client id): body `{email, password, machine_id,
  hostname, os, label}`; verifies via `AuthService.verify_password`; upserts the
  machine under that user through `LocalMachineManager.upsert_seen` (which raises
  `MachineOwnershipConflictError` for a machine owned by someone else, mapped to
  403); mints; returns `{key, key_id, hint, user_id, machine_id}` once with
  `Cache-Control: no-store`.
- `POST /api/auth/keys` (authenticated): mint for the caller's own machine
  (`{label}`), used by rotate.
- `GET /api/auth/keys` (authenticated): list the caller's keys only; the response
  carries `id`, `hint`, `label`, `machine_id`, `created_at`, `last_used_at`, and
  `revoked_at`, never `key_hash` or plaintext.
- `DELETE /api/auth/keys/{id}` (authenticated): revoke, owner only; a key owned by
  another user answers 404 exactly like an absent id.
Until 4.3, "authenticated" means today's `AuthService`; 4.3 swaps the principal
source without changing the routes.

Bootstrap gains `api_key`, `api_key_id`, and `hub_cert` (both parsers,
`update_bootstrap_yaml` writer, config contract carrier regenerated). The hub origin
is the existing `hub_daemon_url` field: `_parse_mode_owner_fields` requires it with
`datastore_mode: remote` and rejects it on a local bootstrap, and Rust reads it
through `FilesHomeView`, so no new URL key is added:

```yaml
hub_daemon_url: https://hub.tailnet:60887   # existing field; remote bootstraps only
hub_cert: ~/.gobby/tls/hub.pem
api_key: gobby_...
api_key_id: 4f1c...
```

Hub machine: `_provision_local_api_token` in `gobby install` also mints the local
machine's key directly through `ApiKeyManager` under `require_sole_user()` and writes
it to bootstrap (the token file is still provisioned until 4.3). Startup adoption:
`ensure_machine_identity` mints the local key when bootstrap has none, so an existing
install gets its key on first start after this leaf.

**Granularity:** one leaf. The migration, `ApiKeyManager`, the format helpers, and the
routes are one issuance path: none is observable without the others, and a format-only
leaf would be a library with no caller. Local-key adoption rides because it is the
issuance path's second caller (the hub machine's own key, a few lines in two existing
functions), and 4.3's flag day requires every existing install to hold a key before it
lands; a separate leaf would test the same `mint` call through the same manager. The
client CLI (`gobby auth login`, `gobby auth key`) has its own test surface and its own
downstream consumer (4.4's pair test), so it is 4.5. The Target count is the regenerated
schema and config carriers plus consumer sweep; the hand-edited production files are
the migration, `api_keys.py`, both format helpers, the two route modules,
`_app_routes.py`, `install.py`, `runner_init/helpers.py`, and the two bootstrap parsers
and writer.

**Acceptance:**

- 4.2.1 - Migration 454 creates `api_keys` and the two `machines` columns, and every derived carrier is regenerated. file: `crates/gcore/assets/schema/migrations/454_add_api_keys.sql`.
- 4.2.2 - `generate`/`parse`/`hash` agree across Python and Rust on shared vectors, and `parse` rejects a bad checksum. test: `tests/utils/test_api_key_format.py::test_cross_language_vectors`.
- 4.2.3 - Bootstrap route verifies the password, binds the machine, and returns the plaintext once; a foreign-owned machine gets 403. test: `tests/servers/routes/test_api_keys.py::test_bootstrap_mints_bound_key`.
- 4.2.4 - Install and startup adoption mint the local machine's key into bootstrap. symbol: `ensure_machine_identity`.
- 4.2.5 - List and revoke are owner-scoped and redacted: one user cannot list or revoke another user's keys, a foreign revoke answers 404 like an absent id, and list responses carry neither `key_hash` nor plaintext. test: `tests/servers/routes/test_api_keys.py::test_key_management_is_owner_scoped_and_redacted`.

### 4.5 `gobby auth login` and `gobby auth key` [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/auth.py::auth`
- `src/gobby/cli/auth_login.py`
- `tests/cli/test_auth_login.py`
- `tests/cli/test_cli_auth.py::*` — scope-reason: consumer of the `auth` group; gains the `login` and `key` subcommand cases
- `tests/e2e/test_auth_login.py`
- `docs/guides/cli-commands.md`

Split from 4.2 on 2026-09-27 (Granularity there). The routes, the bootstrap fields
(`api_key`, `api_key_id`, `hub_cert`), and `update_bootstrap_yaml` are 4.2's; this
leaf consumes them unchanged.

CLI (`src/gobby/cli/auth_login.py`, registered under the existing `auth` group;
`gobby auth token` is removed in 4.3, so this leaf leaves it in place):
- `gobby auth login [--hub URL] [--email] [--fingerprint sha256:...] [--label]
  [--insecure]`: connects with verification disabled, reads the leaf certificate,
  prints its fingerprint, and asks for confirmation unless `--fingerprint` matches (a
  mismatch refuses); writes the PEM to `hub_cert`; posts the bootstrap request over
  the now-pinned connection with this machine's id, hostname, and os; writes
  `api_key`, `api_key_id`, and `hub_cert` to bootstrap. `--insecure` permits `http://`
  to a non-loopback host.
- Login never changes the machine's owner topology. It refuses, before any network
  call and with bootstrap untouched, on a `datastore_mode: local` bootstrap, naming
  `docs/guides/shared-stack.md` (Client setup) as the way to create a remote
  bootstrap. `--hub` defaults to the bootstrap's `hub_daemon_url`; a `--hub` whose
  normalized origin differs from it refuses the same way. Decided 2026-09-27 (PD
  preference, confirmed by source): switching the mode cannot produce a loadable
  bootstrap, because a local bootstrap's `database_url` must be loopback and
  `_validate_managed_database_url` (`src/gobby/config/bootstrap.py`) rejects loopback
  for `datastore_mode: remote`, and `_parse_mode_owner_fields` requires dropping
  `files_home`, which strands the local owner's files; login has no hub DSN to write.
  Rejected: switch the mode in place (unloadable bootstrap and stranded owner data);
  rewrite `hub_daemon_url` on mismatch (silent re-homing of an enrolled node).
- `gobby auth key --show | --rotate`, `gobby auth key list`, `gobby auth key revoke
  ID`. Rotate: mint via `POST /api/auth/keys`, write bootstrap atomically, verify with
  `GET /api/auth/status`, then revoke the old id; on verify failure revoke the new key
  and keep the old.

**Acceptance:**

- 4.5.1 - `gobby auth login` pins by fingerprint, refuses a mismatch, and writes bootstrap. test: `tests/e2e/test_auth_login.py::test_login_pins_self_signed_hub`.
- 4.5.2 - Rotate mints, verifies, then revokes the old key, and rolls back on verify failure. test: `tests/cli/test_auth_login.py::test_rotate_verifies_before_revoke`.
- 4.5.3 - Login refuses a `datastore_mode: local` bootstrap and a `--hub` that differs from `hub_daemon_url`, before any network call and with bootstrap byte-identical. test: `tests/cli/test_auth_login.py::test_login_refuses_local_bootstrap_and_hub_mismatch`.

### 4.3 Hub-side key validation, front-door identity, and shared-token cutover [category: code] (depends: 4.2, 4.5)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/auth.rs`
- `crates/gdaemon/src/front_door/challenge.rs`
- `crates/gdaemon/src/front_door/proxy.rs`
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/tests/front_door.rs`
- `crates/gcore/src/local_token.rs::*` — scope-reason: the module's three readers switch from the token file to the bootstrap `api_key`; their names and signatures are unchanged, so `grant/acquisition.rs`, `ai/effective_config.rs`, and `ai/daemon/transport.rs` need no edit
- `crates/gterminal/src/host/mod.rs::read_local_token`
- `src/gobby/servers/auth_service.py::AuthService.authenticate`
- `src/gobby/servers/auth_service.py::AuthService._accepted_bearer`
- `src/gobby/servers/auth_service.py::AuthService.verify_bearer`
- `src/gobby/servers/auth_service.py::AuthService.verify_ws_token`
- `src/gobby/servers/auth_service.py::AuthService.refresh`
- `src/gobby/servers/auth_service.py::AuthService._token_hash_snapshot`
- `src/gobby/servers/auth_service.py::AuthService.local_token`
- `src/gobby/servers/auth_service.py::AuthService.bind_runtime`
- `src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`
- `src/gobby/servers/websocket/auth.py::AuthMixin._authenticate`
- `src/gobby/servers/websocket/server.py::*` — scope-reason: inherits `AuthMixin._authenticate`; the call site is unchanged, verification only
- `src/gobby/servers/grant_auth.py::bearer_matches_grant`
- `src/gobby/servers/routes/runtime_handshake.py::create_runtime_handshake_router`
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: re-exports `create_runtime_handshake_router`; verification only
- `src/gobby/servers/routes/configuration_effective.py::*` — scope-reason: drop the `verify_bearer` consumer
- `src/gobby/runtime_grants/handshake.py::challenge_proof`
- `src/gobby/runtime_grants/handshake.py::_recompute_capability_signature`
- `src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`
- `src/gobby/runtime_grants/__init__.py::*` — scope-reason: re-exports `challenge_proof`; verification only
- `src/gobby/runtime_grants/launch.py::*` — scope-reason: `materialize_managed_launch` takes `signing_secret` and passes it to the three issuers
- `src/gobby/utils/local_token.py::*` — scope-reason: module rewritten: the file reader becomes the bootstrap key reader, `local_token_path` is deleted, issuers and verifier sign with `signing_secret`, `daemon_auth_headers` drops the identity env headers
- `src/gobby/agents/constants.py::*` — scope-reason: the spawn env builder's `operator_token` parameter becomes `signing_secret`
- `src/gobby/agents/spawn.py::*` — scope-reason: passes `current_lease().grant_signing_secret` instead of `read_local_api_token()` to the env builder and `materialize_managed_launch`
- `src/gobby/agents/tmux/spawner.py::*` — scope-reason: same for the tmux env builder call
- `src/gobby/agents/code_index.py::*` — scope-reason: the preflight passes the lease signing secret to `materialize_managed_launch`
- `src/gobby/ai/_managed_tool_chat_lease.py::*` — scope-reason: passes the lease signing secret to `materialize_managed_launch`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: `code_index_api_token` becomes the lease signing secret, and the selection block moves out (split below)
- `src/gobby/mcp_proxy/tools/spawn_agent/_selection.py`
- `src/gobby/hooks/inbox.py::*` — scope-reason: consumer of `read_local_api_token`; the missing-credential warning names `gobby auth login` instead of the token file, and the drain loop and retention passes move out (split below)
- `src/gobby/hooks/inbox_maintenance.py`
- `src/gobby/runner_maintenance/messaging.py::*` — scope-reason: import of `drain_hook_inbox_loop` moves to `gobby.hooks.inbox_maintenance`
- `tests/hooks/test_inbox.py::*` — scope-reason: imports of `_compute_sleep_seconds` and `prune_orphaned_inbox_temp_files` and the `_JITTER_RANDOM` patch move to `gobby.hooks.inbox_maintenance`; `read_local_api_token` patches keep their path
- `tests/hooks/test_inbox_temp_reaper.py::*` — scope-reason: imports and the `get_hook_inbox_dir` and `prune_orphaned_inbox_temp_files` patches move to `gobby.hooks.inbox_maintenance`
- `tests/hooks/test_envelope_marker_retention.py::*` — scope-reason: the `prune_hook_inbox` import and the `get_hook_inbox_dir` and `prune_processed_envelope_markers` patches move to `gobby.hooks.inbox_maintenance`
- `src/gobby/storage/auth.py::*` — scope-reason: remove the local-token hash accessors
- `src/gobby/config/registry.py::*` — scope-reason: drop the `auth.api_token_hash` registration
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `src/gobby/agents/sandbox_policy.py::sensitive_write_roots`
- `src/gobby/agents/external_write_grants.py::*` — scope-reason: consumer of `sensitive_write_roots`; the returned set loses the token file, call site unchanged
- `src/gobby/agents/sandbox.py::*` — scope-reason: consumer of `sensitive_write_roots`; call site unchanged
- `src/gobby/agents/sandbox_resolvers.py::*` — scope-reason: consumer of `sensitive_write_roots`; call site unchanged
- `src/gobby/cli/auth.py::token`
- `src/gobby/cli/install.py::_provision_local_api_token`
- `src/gobby/runner_init/servers.py::_handshake_factory`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/cli/daemon_start.py`
- `src/gobby/utils/daemon_client.py::DaemonClient.__init__`
- `src/gobby/hooks/factory.py::*` — scope-reason: constructs `DaemonClient` without the machine-id header argument
- `src/gobby/hooks/health_monitor.py::*` — scope-reason: same
- `tests/e2e/conftest.py::prepare_daemon_env`
- `tests/e2e/conftest.py::daemon_instance`
- `tests/e2e/conftest.py::daemon_token`
- `tests/servers/conftest.py::*` — scope-reason: the auth fixtures build front-door identity headers instead of a token
- `tests/servers/test_auth_service.py::*` — scope-reason: bearer tests become front-door identity tests
- `tests/servers/test_auth_middleware.py::*` — scope-reason: same
- `tests/servers/test_http_middleware.py::*` — scope-reason: consumer of `AuthService.authenticate`; same
- `tests/servers/websocket/test_servers_websocket_auth.py::*` — scope-reason: same
- `tests/servers/routes/test_runtime_handshake.py::*` — scope-reason: challenge and handshake against forwarded identity
- `tests/servers/routes/test_runtime_config.py::*` — scope-reason: consumers of `AuthService.local_token`, `issue_for_operator`, and `verify_agent_api_token`; use the signing secret
- `tests/servers/test_mcp_programmatic_boundary.py::*` — scope-reason: consumer of `AuthService.local_token`; uses the signing secret
- `tests/ask/http_mcp_support.py::*` — scope-reason: builds `AuthService` with a token file and issues agent tokens; use the signing secret, no token file
- `tests/mcp_proxy/test_workspaces_registry.py::*` — scope-reason: builds `AuthService` with a token file and patches `authenticate`; same
- `tests/servers/routes/test_ask.py::*` — scope-reason: its authenticated harness builds `AuthService` with a token file and issues agent and tool tokens; same
- `tests/servers/test_grant_auth.py::*` — scope-reason: `bearer_matches_grant` cases compare against the forwarded machine header
- `tests/servers/routes/test_configuration_routes.py::*` — scope-reason: drop verify_bearer usage
- `tests/storage/test_storage_auth.py::*` — scope-reason: drop token-file tests
- `tests/storage/test_managed_credentials.py::*` — scope-reason: consumer of `issue_for_operator`; forwarded machine identity
- `tests/runtime_grants/test_maintenance_principal.py::*` — scope-reason: consumers of `_recompute_capability_signature` and `verify_agent_api_token`; signing secret
- `tests/runner_init/test_grant_issuance.py::*` — scope-reason: consumer of `verify_agent_api_token`; signing secret
- `tests/agents/test_agent_constants.py::*` — scope-reason: consumers of `local_token_path` and `verify_agent_api_token`; signing secret, no token file
- `tests/agents/test_spawn_executor.py::*` — scope-reason: consumer of `local_token_path`; no token file
- `tests/agents/test_external_write_grants.py::*` — scope-reason: consumer of `sensitive_write_roots`; the asserted list loses the token file
- `tests/ai/test_managed_tool_chat_lease.py::*` — scope-reason: patches `read_local_api_token`; patches the lease secret instead
- `tests/utils/test_local_token.py::*` — scope-reason: consumer of `verify_agent_api_token`; signing secret, no token file
- `tests/cli/test_daemon_protected_runs.py::*` — scope-reason: fixture reads the key from bootstrap
- `tests/contracts/http/manifest.json`
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: managed-token signing secret changes
- `tests/runtime_grants/test_golden_vectors.py::*` — scope-reason: same
- `docs/contracts/secrets.md`
- `docs/contracts/identity-model.md`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/admin-operations.md`
- `docs/guides/shared-stack.md`

Single cutover; the token file, its hash, and the alias are gone after this commit.
`docs/guides/shared-stack.md` (Client setup) stops copying `~/.gobby/local_cli_token`
from the hub and runs `gobby auth login` (4.5) after creating the remote bootstrap;
4.3.6's literal sweep covers it.

**Granularity:** one leaf and one commit. The token file, its hash, and the alias leave
together, and any consumer left on the old credential would 401 between commits. The
two size splits below ride in the same commit because the credential edits land in
those files and each split is a pure move.

**Split `src/gobby/hooks/inbox.py`** (983 lines) before its credential edit: move the
periodic drain loop and its retention passes (`drain_hook_inbox_loop`,
`_compute_sleep_seconds`, `_JITTER_RANDOM`, `prune_hook_inbox`,
`_prune_hook_inbox_blocking`, `prune_orphaned_inbox_temp_files`,
`_is_orphaned_temp_name`, `ORPHANED_TEMP_RETENTION_SECONDS`,
`ORPHANED_TEMP_PRUNE_MAX_ENTRIES`) into the new `src/gobby/hooks/inbox_maintenance.py`.
The new module imports `get_hook_inbox_dir` and `drain_hook_inbox_once` from
`inbox.py`, and nothing in `inbox.py` imports it back, so there is no cycle.
`runner_maintenance/messaging.py` imports the loop from the new module, and the three
hook test files import from it and patch `get_hook_inbox_dir`,
`prune_processed_envelope_markers`, `prune_orphaned_inbox_temp_files`, and
`_JITTER_RANDOM` there. `inbox.py` ends near 845 lines.

**Split `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`** (964 lines, almost
all of it `spawn_agent_impl`) before its credential edit: move the selection block,
from the `isolation` default through the managed-runtime `validate_selection` check
that ends just before `get_machine_id`, into the new
`src/gobby/mcp_proxy/tools/spawn_agent/_selection.py` as `resolve_spawn_selection`. It
returns a frozen `SpawnSelection` carrying every value the rest of `spawn_agent_impl`
reads (isolation, provider, model, API base and token, requested reasoning effort,
the resolved reasoning) or the error response `spawn_agent_impl` returns today. No
test patches a name the moved block uses: tests patch `get_project_context`,
`get_isolation_handler`, `get_machine_id`, `execute_spawn`, `prepare_terminal_spawn`,
`finalize_executed_spawn`, `provider_mcp_config_error`, and
`repair_isolation_environment`, which all stay. No patch path moves, and the spawn
tests under `tests/mcp_proxy/tools/spawn_agent/` re-run as verification. The file
ends near 855 lines.

**gdaemon validation** (`front_door/auth.rs`, hub and standalone only): the bearer
is dispatched on its kind, and the dispatch is the one seam a later token kind adds
to: a `gobby_` prefix is an API key; a `v1.` prefix is a managed capability token and
passes through; any other bearer is 401 `{"code":"missing_auth"}` without a database
hit (reserved for the OAuth access tokens D1's `gobby-mcp` adds in Stage 2). For an
API key, `parse` it (bad checksum: the same 401 without a database hit), SHA-256 it,
and run one indexed
`SELECT` on `api_keys` joined to `machines` where `revoked_at IS NULL` through gcore's
`postgres` feature; no cache until measured. On success the proxy strips any
client-supplied `X-Gobby-User-Id`, `X-Gobby-Machine-Id`, `X-Gobby-Key-Id`, and
`X-Gobby-Front-Door` headers, then sets all four: the three ids from the row and
`X-Gobby-Front-Door: <per-boot secret>`. On failure: 401 with the existing body
shape. Requests without a bearer (cookies, managed capability tokens `v1.<payload>.<sig>`,
public paths) pass through with the client headers stripped and no identity set.
`last_used_at` is touched at most once a minute per key. The per-boot secret is 32
random bytes generated by `gobby start` (leaf 1.3's `daemon_start.py`) and passed in
env `GOBBY_FRONT_DOOR_SECRET` to both processes; 5.2 moves generation to gdaemon.

**Python trusts the front door.** `AuthService._accepted_bearer` returns the operator
principal when `X-Gobby-Front-Door` equals the env secret (constant-time), reading
user, machine, and key ids from the headers into `AuthDecision`; `verify_bearer`,
`refresh`, `_token_hash_snapshot`, `local_token`, and the `X-Gobby-Local-Token` alias
are removed; managed capability tokens and `gobby_session` cookies are verified as
today. `AuthMixin._authenticate` (WS `:60988`) accepts the same header.
`bearer_matches_grant` compares an interactive principal's `machine_id` to the
front-door machine header instead of the daemon's local machine id, so a node user's
grant is valid at the hub. `HandshakeService.issue_for_operator` and the handshake
route bind `machine_id` to the forwarded machine and reject a body naming another.
With `front_door.enabled: false` (rollback) Python accepts only cookies and managed
tokens; the shared token does not come back.

**Managed capability tokens** sign with `deployment_runtime.grant_signing_secret`
instead of the operator token. `_issue_managed_api_token`, the three public issuers,
and `verify_agent_api_token` rename their first parameter from `operator_token` to
`signing_secret`; `_recompute_capability_signature` and `_handshake_factory` take
the secret from the lease view (`ActiveDaemonLease.grant_signing_secret` today;
5.1's read-only view later). Every caller that read `read_local_api_token()` to
sign now passes `current_lease().grant_signing_secret`: the spawn env builder in
`agents/constants.py`, `runtime_grants/launch.py::materialize_managed_launch` and
its callers in `agents/spawn.py`, `agents/tmux/spawner.py`, `agents/code_index.py`,
`ai/_managed_tool_chat_lease.py`, and the `code_index_api_token` argument in
`mcp_proxy/tools/spawn_agent/_implementation.py`. Golden vectors in
`crates/gcore/src/grant/tests.rs` and `tests/runtime_grants/test_golden_vectors.py`
are regenerated.

**Challenge route goes native** (`front_door/challenge.rs`, all modes):
`POST /api/runtime/handshake/challenge` with `kind: interactive` is answered by the
local gdaemon as `HMAC-SHA256(api_key, nonce)` from its own bootstrap and is never
forwarded; `kind: managed` is forwarded to the hub, where Python's `challenge_proof`
recomputes the capability signature with the signing secret. `challenge_and_handshake`
in gcore is unchanged: it keeps `reject_remote_endpoint` (crates dial loopback only),
and the bearer `interactive_bearer` hands it is now the bootstrap key, so the proof
stays HMAC(bearer, nonce).

**Readers.** `read_local_cli_token*` in gcore keep their names and read `api_key` from
bootstrap (env capability preference unchanged); `read_local_api_token` and
`daemon_auth_headers` read bootstrap; `local_token_path` is removed with its
consumers (`cli/auth.py`, `cli/install.py`, `auth_service.py`, `storage/auth.py`, and
the three tests). gterm's `read_local_token` reads `api_key` from the same
`GOBBY_HOME` bootstrap; `sensitive_write_roots` drops the token-file entry
(bootstrap.yaml is already listed; its three call sites and the asserting test are
otherwise unchanged). `DaemonClient` sends the bearer only and its constructor loses
the machine-id header argument (`hooks/factory.py` and `hooks/health_monitor.py`
construct it); `daemon_auth_headers` drops the `_AGENT_IDENTITY_ENV_HEADERS` loop and
keeps its signature, so its seven call sites are unchanged. `X-Gobby-Machine-Id` is
no longer sent by any client (managed tokens carry `machine_id` in their claims).
`hooks/inbox.py` keeps reading the operator credential for replay and its
missing-credential warning names `gobby auth login`. `gobby auth token` is removed;
`gobby install` stops provisioning the file; `config_store` key `auth.api_token_hash`
is dropped from the registry. e2e `prepare_daemon_env` provisions a key row and
bootstrap instead of the token file, and `daemon_token` reads `api_key` from that
bootstrap.

**Docs.** `secrets.md` (Daemon API Token table becomes the API key table: bootstrap
field, SHA-256 hash in `api_keys.key_hash`, bearer header, rotation via `gobby auth
key --rotate`), `identity-model.md` (the key row is the one new seam: `user_id` and
`machine_id` on `api_keys`, nothing else gains a user column), `gterm-protocols.md`
(frames socket credential), `http-endpoints.md` (credential precedence: bearer key,
cookie), `admin-operations.md` (rotation procedure).

**Corpus.** Re-record the auth cases in `tests/contracts/http/` with the manifest
`schema_version` bumped to 2.

**Acceptance:**

- 4.3.1 - gdaemon resolves a valid key to user and machine, forwards the identity headers with the front-door secret, and rejects revoked, malformed, or unknown-kind bearers with 401 (the last two without a database hit). test: `crates/gdaemon/tests/front_door.rs::valid_key_forwards_identity_headers`.
- 4.3.2 - Python accepts the operator principal only with the matching front-door secret on HTTP and WS, and no longer accepts the token or the alias. test: `tests/servers/test_auth_service.py::test_front_door_identity_requires_secret`.
- 4.3.3 - Managed capability tokens sign and verify with `grant_signing_secret` and the golden vectors are regenerated. test: `tests/runtime_grants/test_golden_vectors.py::test_managed_token_signed_with_grant_secret`.
- 4.3.4 - The interactive challenge is answered locally by gdaemon and never forwarded; the managed challenge reaches Python. test: `crates/gdaemon/tests/front_door.rs::interactive_challenge_answered_locally`.
- 4.3.5 - A node user's interactive grant validates at the hub. symbol: `bearer_matches_grant`.
- 4.3.6 - No reference to `local_cli_token`, `X-Gobby-Local-Token`, or `auth.api_token_hash` remains under `src/`, `crates/`, or `docs/` (literal sweep recorded in the leaf). behavior: "API key" in `docs/contracts/secrets.md`.
- 4.3.7 - The e2e suites pass with a provisioned key and the auth corpus cases are re-recorded at `schema_version` 2. file: `tests/contracts/http/manifest.json`.
- 4.3.8 - `src/gobby/hooks/inbox.py` is below 1,000 lines after the loop and retention move, and the moved loop still drains and prunes on its own cadence. file: `src/gobby/hooks/inbox_maintenance.py`.
- 4.3.9 - `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` is below 1,000 lines after the selection move. file: `src/gobby/mcp_proxy/tools/spawn_agent/_selection.py`.

### 4.4 Node channel, relay backend, and `/api/machines` [category: code] (depends: 4.1, 4.3, 4.5)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/state.rs`
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/front_door/proxy.rs`
- `crates/gdaemon/src/nodes/mod.rs`
- `crates/gdaemon/src/nodes/channel.rs`
- `crates/gdaemon/src/nodes/registry.rs`
- `crates/gdaemon/src/nodes/machines_api.rs`
- `crates/gdaemon/tests/nodes.rs`
- `tests/e2e/test_hub_node_pair.py`
- `tests/e2e/conftest.py::daemon_instance`
- `docs/guides/configuration.md`

**Relay backend.** In `node` mode the front door's backend target is `hub_daemon_url` over
the pinned client (`hub_cert`), one pooled hyper + rustls connection reused across
requests, instead of loopback Python. Requests are forwarded byte-for-byte, bearer
included; the node validates nothing and holds no table. A hub that refuses
connections yields the 1.2 typed 503 with `"target": "<hub_daemon_url>"`. Both node
listeners relay: the HTTP listener to `hub_daemon_url`, and the WS listener (`:60888` and
`/ws` on `:60887`) to `wss://<hub_daemon_url host:port>/ws`, because the hub's `/ws` on its
HTTP port serves the same `WebSocketServer` as its `:60888` listener
(`src/gobby/servers/_app_ui.py`), so no second hub port is configured. The 1.2 WS
splice runs unchanged inside the pinned TLS stream.

**Channel.** `NodeServices` gains a channel task that opens `GET /api/nodes/channel`
(WS upgrade) on the hub with `Authorization: Bearer <api_key>`, sends
`{"type":"hello","node_version":"<gdaemon version>","platform":"<os>/<arch>"}`,
expects `{"type":"ack","machine_id":...}`, and then keeps WS ping/pong every 30 s,
reconnecting with backoff (1 s doubling to 60 s) after any close. `HubServices` gains
`nodes/registry.rs`: an in-memory map `machine_id -> ChannelHandle` (one per machine).
Each `ChannelHandle` carries an opaque connection id assigned at registration; a new
connection atomically swaps the entry for its `machine_id` and closes the old
handle, and a closing channel removes the entry only when the stored connection id
is still its own, so a delayed cleanup of the old channel never erases its
successor. On each heartbeat the hub re-reads the
key row and closes the channel with code 4401 when `revoked_at` is set, and writes
`machines.last_heartbeat_at` and `node_version` at most once a minute. Only `hello`,
`ack`, and ping/pong exist in Stage 1; S2.7 and S2.8 add commands.

**API.** `nodes/machines_api.rs` serves `GET /api/machines` and
`GET /api/machines/{id}` natively (hub and standalone), key-authenticated through
4.3, returning the `machines` row fields plus `"connected": bool` from the registry.

**Pair test.** `tests/e2e/test_hub_node_pair.py` starts a hub `daemon_instance`
with `tls="self-signed"` and a second gdaemon in `node` mode (bootstrap
`datastore_mode: remote`, `hub: false`, `hub_daemon_url`, `hub_cert`, `api_key` from a
`gobby auth login` run against the hub), asserts the node appears `connected` in
`/api/machines`, that a request through the node's loopback front door reaches the
hub and returns the hub's identity, that one WS upgrade through the node's loopback
front door reaches the hub's `WebSocketServer` and echoes a frame, that the node
runs no maintenance loop (run-modes 2.2), and that revoking the key closes the channel within
30 s and makes the next relayed request fail with 401.

**Acceptance:**

- 4.4.1 - A node relays to `hub_daemon_url` over the pinned connection and reports the typed 503 when the hub is down. test: `crates/gdaemon/tests/nodes.rs::node_relays_over_pinned_tls`.
- 4.4.2 - The channel registers the machine, heartbeats, and is replaced by a newer connection; when the old channel's cleanup runs after the new ack, the new channel stays registered and `connected` in `/api/machines`. test: `crates/gdaemon/tests/nodes.rs::channel_registers_and_replaces`.
- 4.4.3 - Revocation closes the channel within one heartbeat interval. test: `crates/gdaemon/tests/nodes.rs::revoked_key_closes_channel_on_heartbeat`.
- 4.4.4 - `/api/machines` lists rows with connection state. file: `crates/gdaemon/src/nodes/machines_api.rs`.
- 4.4.5 - The hub-node pair test passes over self-signed TLS end to end, including one WS upgrade relayed through the node. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.
- 4.4.6 - A node relays a WS upgrade to `wss://<hub_daemon_url host:port>/ws` over the pinned connection and the golden frames and close codes pass byte-equal. test: `crates/gdaemon/tests/nodes.rs::node_relays_ws_over_pinned_tls`.

## P5: Singleton lease and backend lifecycle in Rust (S1.3, #21554) (depends: P4)
`kind: framing`

**Goal**: gdaemon holds the database-scoped lease, owns the pid claim, spawns and
supervises Python only when active, and serves a typed standby; the Python lease
modules retire.

### 5.1 Lease in Rust, standby surface, Python reads its runtime row [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/lease/mod.rs`
- `crates/gdaemon/src/lease/standby.rs`
- `crates/gdaemon/src/lease/admin_api.rs`
- `crates/gdaemon/src/state.rs`
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/front_door/health.rs`
- `crates/gdaemon/tests/lease.rs`
- `src/gobby/daemon_lease.py::ActiveDaemonLease`
- `src/gobby/daemon_lease.py::ActiveDaemonLease.try_acquire`
- `src/gobby/daemon_lease.py::current_lease`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/servers/auth_service.py::AuthService.authenticate`
- `src/gobby/servers/auth_service.py::AuthService._effectful_allowed`
- `src/gobby/cli/daemon_health.py::health`
- `tests/test_daemon_lease.py::*` — scope-reason: the lease becomes a read-only view
- `tests/test_runner_lease_lifecycle.py::*` — scope-reason: same
- `tests/servers/test_auth_service.py::*` — scope-reason: lease_not_held path removed
- `tests/ask/native_probe_harness.py::*` — scope-reason: the contained probe constructs `ActiveDaemonLease` and calls `try_acquire`; it binds the read-only view instead
- `tests/ask/test_native_probe_cleanup.py::*` — scope-reason: consumer of `current_lease`; asserts the probe binds and clears the view
- `tests/ask/test_native_probe_harness.py::*` — scope-reason: same

`crates/gdaemon/src/lease/` ports the FW.1 contract (#21548): a `hub` or
`standalone` gdaemon takes the advisory lock keyed on `current_database()` on a
dedicated connection, generates a fresh `grant_signing_secret`, bumps
`deployment_runtime.fencing_epoch` and `epoch_updated_at` for the deployment token
(gcore `grant::handshake::deployment_token`), heartbeats, and treats a lost
connection as lease loss (stop the backend, re-enter standby). A `node` never
leases. `StandaloneServices` and `HubServices` hold the lease handle.

**Standby** (`lease/standby.rs`): waits for the lock with the same stale-owner
recovery semantics as `ActiveDaemonLease`; serves `/api/health` as the 1.2 typed 503
with `"state": "standby"`, `/api/admin/lease/status|promote|recover` natively
(`lease/admin_api.rs`, key-authenticated through 4.3, same payloads as
`daemon_lease_control.py`), and the same typed 503 on every other route. `gobby
status` prints `standby` from the body.

**Python side.** `ActiveDaemonLease` becomes a read-only view: at startup it selects
`fencing_epoch` and `grant_signing_secret` by the deployment token and holds them;
`try_acquire`, heartbeat, and abort paths are removed. `current_lease()` returns the
view; `servers/grant_auth.py`, `agents/code_index.py`, and
`runner_init/servers.py` consume it unchanged. Because Python runs only while gdaemon
is active, `_effectful_allowed` is always true and the `lease_not_held` decision is
removed from `authenticate` (its exception handler and `lease_fence.py` are deleted in
5.3).

**Contained probe and tests seed the row.** After this leaf only gdaemon writes
`deployment_runtime`, but the contained Ask probe
(`tests/ask/native_probe_harness.py::_acquire_contained_runtime_authority`) runs an
in-process runner with no gdaemon. The harness keeps its private pid claim and, in
place of `ActiveDaemonLease.try_acquire`, seeds the row itself with the upsert gdaemon
performs (the SQL `try_acquire` runs today: insert epoch 1 with a fresh
`secrets.token_urlsafe(32)` secret, or bump `fencing_epoch`, replace the secret, and
set `epoch_updated_at`), then binds the read-only view; cleanup clears the view and
releases the claim. 5.1.4's test seeds the same way. Precedent: `tests/code_index/conftest.py`
already seeds `deployment_runtime` by direct SQL. Nothing native requires the lease's
advisory lock to be held to verify a grant (the only advisory locks under
`crates/gcore/src` and `crates/gdaemon/src` today are the schema-apply and sweep
locks), so a seeded row without the lock is sufficient. Rejected: spawning the pinned
test gdaemon to take a real lease, because after 5.2 `gdaemon serve` supervises its own
backend and would conflict with the probe's in-process runner, and it would add a Rust
binary dependency to a Python probe.

**Acceptance:**

- 5.1.1 - gdaemon acquires the lease, rotates the secret, and bumps the epoch; a second gdaemon enters standby. test: `crates/gdaemon/tests/lease.rs::second_daemon_enters_standby`.
- 5.1.2 - Standby serves the typed 503 `standby` body and the native lease admin routes. test: `crates/gdaemon/tests/lease.rs::standby_serves_typed_503_and_admin_routes`.
- 5.1.3 - Lease loss stops the backend and re-enters standby. test: `crates/gdaemon/tests/lease.rs::lost_connection_reenters_standby`.
- 5.1.4 - Python reads epoch and secret from its row and never writes them. test: `tests/test_daemon_lease.py::test_view_reads_deployment_runtime_row`.
- 5.1.5 - `gobby status` prints `standby` for a standby daemon. symbol: `health`.
- 5.1.6 - The contained Ask probe seeds its `deployment_runtime` row, binds the read-only view without `try_acquire`, and clears it on cleanup. test: `tests/ask/test_native_probe_cleanup.py::test_probe_seeds_row_and_clears_view`.

### 5.2 Lifecycle: pid claim port, backend supervision, `gobby start/stop/restart/status`, service templates [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/lifecycle/mod.rs`
- `crates/gdaemon/src/lifecycle/pid_file.rs`
- `crates/gdaemon/src/lifecycle/backend.rs`
- `crates/gdaemon/src/lifecycle/shutdown_intent.rs`
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/front_door/health.rs`
- `crates/gdaemon/tests/pid_file_golden.rs`
- `crates/gdaemon/tests/lifecycle.rs`
- `tests/fixtures/pid_file_records/daemon_claim.json`
- `tests/fixtures/pid_file_records/service_reservation.json`
- `src/gobby/cli/daemon_start.py`
- `src/gobby/cli/daemon.py::_do_stop`
- `src/gobby/cli/daemon.py::stop`
- `src/gobby/cli/daemon.py::restart`
- `src/gobby/cli/daemon.py::status`
- `src/gobby/cli/daemon.py::_get_running_daemon_pid`
- `src/gobby/cli/daemon_lifecycle.py`
- `src/gobby/cli/__init__.py::*` — scope-reason: registers `stop`, `restart`, and `status` from `.daemon`; the import moves to `.daemon_lifecycle`
- `src/gobby/cli/daemon_preflight.py::*` — scope-reason: consumer: `restart_start_refusal` runs before both restart forms and its call site moves with `restart`; verification only
- `src/gobby/cli/daemon_singleton.py::*` — scope-reason: consumer: `stop_singleton_gate` admits the stop before the pid-record fallback; verification only
- `src/gobby/cli/_daemon_protected_runs.py::*` — scope-reason: consumer: `clear_protected_runs` moves with `_do_stop`; verification only
- `src/gobby/cli/_daemon_handoffs.py::*` — scope-reason: consumer: `protect_pending_handoffs` moves with `_do_stop`; verification only
- `src/gobby/cli/cutover.py::*` — scope-reason: import of `restart` moves to `gobby.cli.daemon_lifecycle`, and the post-promotion restart uses `--full` with `expected_identity`
- `src/gobby/cli/hub_backup/cli.py::*` — scope-reason: consumer of `_services_start` and `_services_stop`, which stay in `daemon.py`; verification only
- `src/gobby/cli/pack.py::*` — scope-reason: consumer of `_services_start` and `_services_stop`, which stay in `daemon.py`; verification only
- `src/gobby/servers/routes/admin/_lifecycle.py::register_lifecycle_routes`
- `src/gobby/servers/routes/admin/_lifecycle.py::_wait_for_process_exit`
- `src/gobby/servers/routes/admin/_lifecycle.py::_append_restart_helper_log`
- `src/gobby/servers/routes/admin/_lifecycle.py::_force_stop_process`
- `src/gobby/servers/routes/admin/_lifecycle.py::_run_service_restart_helper`
- `src/gobby/servers/routes/admin/_lifecycle.py::_run_direct_restart_helper`
- `src/gobby/servers/routes/admin/_lifecycle.py::_should_restart_via_service_manager`
- `src/gobby/servers/routes/admin/_lifecycle.py::_spawn_restart_helper`
- `src/gobby/servers/routes/admin/__init__.py::*` — scope-reason: mounts `register_lifecycle_routes`; the call site is unchanged, verification only
- `tests/servers/routes/admin/test_protected_cron_runs.py::*` — scope-reason: consumer of `register_lifecycle_routes`; verification only
- `src/gobby/runner.py::main`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/runner.py::GobbyRunner.create`
- `src/gobby/runner_init/storage.py::*` — scope-reason: adds `StartRefusal` beside `bundled_content_refusal` and raises it from `init_storage_and_config` (the file is a `::*` consumer entry in 4.2, so one scope form)
- `src/gobby/runner_startup_code_index.py::*` — scope-reason: consumer of `GobbyRunner.create`; verification only
- `tests/mcp_proxy/test_semantic_search.py::*` — scope-reason: consumer of `GobbyRunner.create`; verification only
- `tests/memory/test_falkor_phase2_wiring.py::*` — scope-reason: consumer of `GobbyRunner.create`; verification only
- `tests/test_runner_lifecycle_restart_replay.py::*` — scope-reason: consumer of `GobbyRunner.create`; verification only
- `src/gobby/runner_init/__init__.py::*` — scope-reason: re-exports the new `StartRefusal` beside `bundled_content_refusal`
- `src/gobby/cli/daemon_health.py::health`
- `src/gobby/runner_pid_file.py::adopt_inherited_claim`
- `src/gobby/cli/installers/service_common.py::_resolve_install_context`
- `src/gobby/cli/installers/service_common.py::service_unit_has_launch_env`
- `src/gobby/cli/installers/service.py::*` — scope-reason: consumer of `_resolve_install_context` and `service_unit_has_launch_env`; renders the gdaemon launch path
- `src/gobby/cli/installers/service_linux.py::*` — scope-reason: same
- `src/gobby/install/shared/services/com.gobby.daemon.plist.j2`
- `src/gobby/install/shared/services/gobby-daemon.service.j2`
- `src/gobby/install/shared/services/gobby-launcher.cmd.j2`
- `tests/cli/test_cli_daemon.py::*` — scope-reason: start/stop/restart/status drive gdaemon
- `tests/cli/test_daemon_handoffs.py::*` — scope-reason: consumers of `_do_stop` and `stop`; patch paths move to `gobby.cli.daemon_lifecycle`
- `tests/cli/test_daemon_remote_mode.py::*` — scope-reason: consumers of `_do_stop` and `restart`; patch paths move
- `tests/cli/test_cli_falkor.py::*` — scope-reason: consumers of `stop`, `restart`, and `status`; patch paths move
- `tests/cli/test_daemon_coverage.py::*` — scope-reason: consumers of `stop` and `status`; patch paths move
- `tests/cli/installers/test_cli_installers_service.py::*` — scope-reason: rendered units launch gdaemon
- `tests/servers/routes/test_admin.py::*` — scope-reason: restart and shutdown cases assert the intent marker and no helper spawn
- `tests/test_runner_pid_file.py::*` — scope-reason: adoption tests removed, golden fixtures exported
- `tests/cli/test_daemon_set_coherence.py::*` — scope-reason: patches `gobby.cli.daemon._do_stop`; the patch path moves to `gobby.cli.daemon_lifecycle`
- `tests/storage/test_schema_divergence.py::*` — scope-reason: same `_do_stop` patch move
- `tests/cli/test_cutover.py::*` — scope-reason: patches `gobby.cli.cutover.restart`; asserts the post-promotion restart uses `--full`
- `tests/runner_init/test_runner_init_storage.py::*` — scope-reason: consumer of `init_storage_and_config`; the dirty-bundled-content case asserts `StartRefusal`
- `tests/runner_helpers.py::*` — scope-reason: consumer of `init_storage_and_config`; verification only
- `tests/test_runner_shutdown.py::*` — scope-reason: consumer of `runner.main`; gains the refusal exit status and supervisor-pipe EOF cases
- `crates/gdaemon/src/lease/mod.rs`
- `src/gobby/storage/schema_contract.py::*` — scope-reason: adds `SchemaVerifyRefusal` and raises it from `_run_gdaemon`'s missing-binary and nonzero-exit branches
- `src/gobby/storage/schema_divergence.py::*` — scope-reason: consumer of `SchemaContractError`; the subclass is still caught, verification only
- `tests/storage/test_schema_contract.py::*` — scope-reason: gains the refusal-versus-transient classification cases
- `docs/guides/admin-operations.md`
- `docs/guides/cli-commands.md`

**Granularity:** one leaf. The pid claim, supervision, CLI, and service templates move
ownership of the daemon lifecycle together; any subset leaves two owners of the pid
lock or a launcher that nothing supervises. The start-refusal status and the liveness
pipe are the supervisor's contract with the backend it spawns: without them the
supervisor either respawns a refusal forever or leaves an orphan running after its own
crash, so neither is independently closeable. Releasing the lease on refusal is the same
supervisor transition, and the verify-failure split decides which exits reach it.

**Cutover restarts with `--full`** (confirmed 2026-09-27 from source): `run_cutover`
in `src/gobby/cli/cutover.py` promotes the coherent `gcode`/`gdaemon`/`ghook` set
through `promote_workspace_binary_set`, verifies the new gdaemon
(`_verify_restart_target`), and only then restarts. A backend-only restart would leave
the old gdaemon process running from the replaced inode in front of the new binaries.

**Pid claim** (`lifecycle/pid_file.rs`): gdaemon claims `~/.gobby/gobby.pid.lock`
with the same flock, writes the same JSON role record, and honors
`GOBBY_SERVICE_LAUNCH`/`GOBBY_SERVICE_NONCE` reservations exactly as
`src/gobby/runner_pid_file.py` does (`claim_pid_file`, `reserve_service_start`,
`convert_or_acquire_service_claim`, `cancel_service_reservation` semantics). The
Python module stays as the library the offline CLI tools use for mutual exclusion
(`files_migrate.py`, `hub_backup`, `install_files_home.py`, `utils_config.py`,
`daemon_singleton.py`); only the runner's claim and `adopt_inherited_claim` go.
`tests/fixtures/pid_file_records/` holds records written by the Python module; the
Rust golden test parses each and the Python test parses Rust-written records.

**Supervision** (`lifecycle/backend.rs`): an active gdaemon spawns
`python -m gobby.runner` with the backend ports, `GOBBY_FRONT_DOOR_SECRET` (generated
here now; 1.3's `gobby start` generation is removed), and the service env; reports
`starting` to the 1.2 typed 503 until the backend accepts; stops it on lease loss or
shutdown with the existing drain semantics (SIGTERM, wait, SIGKILL after the
runner's settlement timeout). A standby never spawns. When the backend exits on its
own, the supervisor reads the shutdown-intent marker the runner already writes
(`src/gobby/shutdown_intent.py`: `get_shutdown_marker_path`, the
`read_active_shutdown_intent` record shape; ported to `lifecycle/shutdown_intent.rs`
with a golden test against a Python-written marker): intent `restart` respawns the
backend immediately (lease held, epoch unchanged, grants valid); intent `stop` exits
gdaemon after releasing the lease and the pid claim; no active marker is a crash and
respawns with backoff (1 s doubling to 30 s). Only a marker the current backend
wrote steers the supervisor: immediately before every spawn it records the raw bytes
of whatever active marker exists (they carry `sender_pid`, `timestamp`, and `source`,
so any new write differs) and leaves the file in place; on the backend's exit a marker
byte-equal to the recorded one counts as absent (a crash, with backoff). The runner
still clears the marker once it is listening (`runner_lifecycle.py`), as today.
Without this, a `stop` marker left by an orphan draining on liveness-pipe EOF (below)
would make a new gdaemon whose first backend dies before listening exit and release
the lease, and an already-acted-on `restart` marker would respawn a backend that keeps
dying before listening with no backoff. The file stays so ghook's planned-shutdown
suppression (1.4; `should_suppress_failed_post` requires a fresh marker) keeps covering
the typed 503 while a restarted backend is `starting`. Rejected: deleting the marker
before spawn (drops that suppression for the whole startup window of every planned
restart); honoring a marker only when its `timestamp` is at or after the spawn (a
wall-clock step misorders the comparison).

**Start refusals are not crashes** (decided 2026-09-27). Today every refusal and every
crash leaves `runner.main` with status 1: the worktree refusal calls `sys.exit(1)`, the
dirty-bundled-content refusal is a bare `RuntimeError` raised from
`GobbyRunner.create` and `init_storage_and_config` (`src/gobby/runner_init/storage.py`),
and the startup schema check (`verify_schema` in `run_gobby`) raises
`SchemaContractError`; all reach `main`'s generic handler. Each is deterministic until
an operator acts, so backoff respawn would only loop and flood the log. This leaf adds
`StartRefusal(RuntimeError)` beside `bundled_content_refusal` in
`runner_init/storage.py` (re-exported from `gobby.runner_init`); the two bundled-content
raise sites raise it, `run_gobby` wraps only its `verify_schema` call's
`SchemaVerifyRefusal` in it, and `main` exits with status 78 (`EX_CONFIG`) for it and
for the worktree refusal. Every other exit keeps status 1 and is a crash. `main` is
never the service-manager entry after this leaf, so status 78 meets no launchd or
systemd restart policy. On 78 the supervisor does not respawn; it tees the backend's
stderr to the log, keeps the last non-empty stderr line as the refusal text, and
reports the typed 503 with `"backend": {"state": "refused", "refusal": "<text>",
"target": ...}` (`status` stays `unavailable`, so 1.4's ghook check is unchanged).

**Only a completed verify refuses** (decided 2026-09-28). `_run_gdaemon`
(`src/gobby/storage/schema_contract.py`) raises `SchemaContractError` for four
different failures: no installed gdaemon, a launch `OSError`, the 300 s timeout, and a
nonzero gdaemon exit. The first and last are deterministic until an operator acts; a
timeout or launch failure can pass on the next attempt, and making it status 78 would
strand a healthy machine as `refused`. `_run_gdaemon` raises
`SchemaVerifyRefusal(SchemaContractError)` for the missing binary and the nonzero exit,
and plain `SchemaContractError` for the timeout and `OSError`; `run_gobby` wraps only
`SchemaVerifyRefusal`, so the other two stay status 1 and respawn with backoff. The
subclass keeps every existing `except SchemaContractError` (`daemon_preflight.py`,
`schema_divergence.py`) unchanged. Residual trade: a gdaemon run that cannot reach
PostgreSQL also exits nonzero and becomes a refusal. The supervisor's own lease
connection is alive at that instant, so this needs a failure that hits one connection
and not the other; the cost is a released lease (below) and one operator `gobby
restart`.

**A refused gdaemon releases the lease and keeps the pid claim** (decided
2026-09-28; supersedes the 2026-09-27 hold). The refusals split by what they depend on:
the worktree and dirty-bundled-content refusals are machine-local by construction,
and the schema refusal cannot be proven shared, because `gdaemon schema verify` checks
the live database against the identity compiled into *this machine's* installed
gdaemon (`verify_database_identity`), so a stale binary set on one machine refuses
while a current standby would serve. The policy therefore keys on status 78 alone. On
78 the supervisor releases the lease through the same path intent `stop` uses (the
advisory lock connection closes, heartbeats stop, and the next acquirer bumps the
epoch), keeps the pid claim, and stays up in `refused`, a state that never contends for
the lease again: `refused` is not `standby`, and only process exit leaves it, so there
is no acquire, spawn, refuse, release loop. The per-machine pid claim never blocks a
standby on another machine; keeping it stops a service-manager relaunch or a second
`gobby start` from racing a second gdaemon on this machine, and it is what `gobby stop`
and `gobby restart --full` use to find the process. When the database is truly
diverged, every current machine refuses once, releases, and stops contending; the hub
is down exactly as it would be under a hold, and the machine an operator repairs first
acquires on its restart without waiting for another refused holder to be restarted.
Rejected: holding the lease while refused (blocks a healthy standby indefinitely
behind a machine-local refusal, and gains nothing in the shared case); releasing the
pid claim and exiting gdaemon (hands the retry to launchd `KeepAlive`/systemd
`Restart=`, which relaunches into the same refusal); per-class policy (needs a
shared-versus-local signal the binary-identity check cannot give).

**Operator recovery.** `gobby status` and `gobby start` print `refused` and the
refusal text, which carries the class's remedy: run from the main checkout (worktree),
commit or discard bundled content (dirty content), or `gobby install` / `gobby
cutover` to refresh the installed set and schema (schema verify). After fixing the
cause on that machine, `gobby restart` there: the backend is not serving, so it takes
the `--full` form, stops gdaemon through the pid record, and starts a fresh gdaemon
that contends for the lease and enters `standby` if another machine acquired it
meanwhile. Each refused machine needs its own restart; a repair elsewhere does not
clear it. `gobby start` stops waiting when
public health reports `refused`, prints the refusal, and exits 1 with gdaemon left up;
`gobby status` prints `refused` and the text. Because the Python admin routes are down
while the backend is refused or down, `gobby restart` without `--full` reads public
health first and performs the `--full` form when the backend is not serving.

**Descriptors and orphan backends** (decided 2026-09-27). The backend is spawned
without the pid-lock descriptor. Rust's standard library opens every descriptor with
`O_CLOEXEC`, and `seal_inherited_descriptors` (`src/gobby/utils/spawn.py`) only marks
inherited descriptors non-inheritable and does not close them, so a lock descriptor
reaching the runner would hold the flock (it lives on the open file description) after
gdaemon exits, and the next `gdaemon serve` would fail closed against a backend no one
supervises. The same crash would also orphan the backend: 5.1 stops it on lease loss
only while gdaemon is alive, and after 5.3 deletes `lease_fence.py` an orphan keeps
effectful work running under a stale epoch. The supervisor therefore spawns the
backend with the read end of a liveness pipe as its one intentionally inherited
descriptor, named in `GOBBY_SUPERVISOR_FD`; `run_gobby` watches it and, on EOF (gdaemon
gone by any cause, including SIGKILL), requests the normal shutdown drain with intent
`stop`. On Windows the supervisor passes the read handle as an explicitly inheritable
handle whose value `GOBBY_SUPERVISOR_FD` carries, and the runner opens it with
`msvcrt.open_osfhandle`. Rejected: `PR_SET_PDEATHSIG` (Linux-only; macOS is the primary host);
`getppid` polling (adds a poll loop and misreads a reparented child during the poll
interval); letting the child hold the lock descriptor (the stranded-lock failure
above).

`gobby start` keeps its admissions (worktree guard, `binary_set_apply_refusal`) before
spawning `gdaemon serve`; a service-manager launch starts `gdaemon serve` directly and
relies on the runner's refusals above.

**Admin routes stay in Python.** `POST /api/admin/shutdown` and
`POST /api/admin/restart` in `src/gobby/servers/routes/admin/_lifecycle.py` keep
their admission logic (handoff-shutdown preparation and the 409 `handoff_pending`,
the protected-cron 409 `restart_protected` unless `force`, the `terminals` drain
flag, the shutdown-source marker, and the runner shutdown request with its intent)
and lose everything that relaunched a process: `_spawn_restart_helper`,
`_run_service_restart_helper`, `_run_direct_restart_helper`,
`_should_restart_via_service_manager`, `_force_stop_process`,
`_wait_for_process_exit`, and `_append_restart_helper_log` are deleted, and the
routes stop importing the CLI `stop`/`restart`/`status` functions. The web UI's
restart button (`web/src/lib/api.ts`) keeps its path and now gets a backend-only
restart. gdaemon serves neither route natively; a standby answers them with the
typed 503, and `gobby stop` falls back to the pid record.

**CLI** (move `_do_stop`, `stop`, `restart`, `status`, `_get_running_daemon_pid`
into the new `src/gobby/cli/daemon_lifecycle.py`; `daemon.py` must stay below the
ceiling and this split is the exemption; `src/gobby/cli/__init__.py`, `cutover.py`, and the
CLI test files that import or patch the moved names follow the import move;
`hub_backup/cli.py` and `pack.py` import only `_services_start` and `_services_stop`,
which stay): `gobby start` spawns
`gdaemon serve` and waits for public health; `gobby stop` calls
`POST /api/admin/shutdown`, waits for the pid claim to clear, and falls back to
SIGTERM on the pid record only after the admissions `_do_stop` runs today
(`stop_singleton_gate`, `clear_protected_runs`, `protect_pending_handoffs`) admit the
stop; `gobby restart` calls `POST /api/admin/restart` by
default (the runner exits with intent `restart` and the supervisor respawns it) and
`--full` does stop then start; the restart-protected cron guard, the handoff guard, and
`--wait`/`--force` run client-side before either form, and `restart_start_refusal`
(`src/gobby/cli/daemon_preflight.py`, #22403: worktree guard, installed set, schema
identity, read-only `gdaemon schema plan`) runs before either form and refuses with
the running daemon untouched; `gobby cutover` always restarts with `--full`, because a
backend-only restart would leave the old gdaemon in front of the new binaries, and
keeps passing `expected_identity`; `gobby status` probes the lock as today. The
launchd plist, systemd unit, and Windows launcher templates launch `gdaemon serve`
directly with the service env; `_resolve_install_context` renders the gdaemon path
and `service.py`/`service_linux.py` consume it. The runner's `main` no longer probes
for a healthy daemon or adopts a claim.

**Acceptance:**

- 5.2.1 - Rust and Python parse each other's pid records and reservations. test: `crates/gdaemon/tests/pid_file_golden.rs::python_records_parse`.
- 5.2.2 - `probe_daemon_lock` reports a gdaemon-held claim as a live daemon. test: `tests/test_runner_pid_file.py::test_probe_reports_gdaemon_claim`.
- 5.2.3 - The supervisor spawns the backend only when active, restarts it on crash, and reports `starting`. test: `crates/gdaemon/tests/lifecycle.rs::supervisor_restarts_crashed_backend`.
- 5.2.4 - Backend-only restart keeps the epoch and existing grants valid; `--full` bumps the epoch. test: `tests/cli/test_cli_daemon.py::test_restart_backend_only_keeps_grants`.
- 5.2.5 - Service templates launch gdaemon and `service_unit_has_launch_env` still detects the launch env. file: `src/gobby/install/shared/services/gobby-daemon.service.j2`.
- 5.2.6 - `src/gobby/cli/daemon.py` stays below 1,000 lines after the lifecycle split. file: `src/gobby/cli/daemon_lifecycle.py`.
- 5.2.7 - `POST /api/admin/restart` keeps its admission checks, writes intent `restart`, spawns no helper, and the deleted helper functions are gone. test: `tests/servers/routes/test_admin.py::test_restart_writes_intent_without_helper`.
- 5.2.8 - The supervisor parses a Python-written shutdown-intent marker and respawns on `restart`, exits on `stop`, and backs off on a crash; a `stop` marker present before the spawn followed by an early backend death backs off, and a consumed `restart` marker followed by an early death backs off with the marker file still present. test: `crates/gdaemon/tests/lifecycle.rs::shutdown_intent_marker_drives_respawn`.
- 5.2.9 - `gobby restart` (both forms) and `gobby cutover` refuse before any stop when `restart_start_refusal` fails, leaving the running daemon untouched, and cutover restarts with `--full`. test: `tests/cli/test_cli_daemon.py::test_restart_backend_only_runs_start_preflight`.
- 5.2.10 - The supervisor does not respawn after a start-refusal exit (status 78) and reports the typed 503 with `backend.state: refused` and the refusal text, and the spawned backend holds no pid-lock descriptor. test: `crates/gdaemon/tests/lifecycle.rs::start_refusal_is_not_respawned`.
- 5.2.11 - `gobby stop` reaches the pid-record SIGTERM fallback only after the singleton, protected-run, and handoff admissions pass. test: `tests/cli/test_daemon_handoffs.py::test_stop_fallback_runs_after_admissions`.
- 5.2.12 - `runner.main` exits 78 for the worktree, dirty-bundled-content, and startup schema-verify (`SchemaVerifyRefusal`) refusals and 1 for any other failure. test: `tests/test_runner_shutdown.py::test_start_refusals_exit_78`.
- 5.2.13 - `gobby start` stops waiting when public health reports `backend.state: refused`, prints the refusal text, and exits 1; `gobby restart` without `--full` takes the `--full` form while the backend is not serving. test: `tests/cli/test_cli_daemon.py::test_start_and_restart_handle_refused_backend`.
- 5.2.14 - The runner requests the shutdown drain with intent `stop` when the supervisor's liveness pipe reaches EOF. test: `tests/test_runner_shutdown.py::test_supervisor_pipe_eof_requests_shutdown`.
- 5.2.15 - After a start-refusal exit the refused gdaemon releases the lease and keeps its pid claim, a second gdaemon on the same database acquires the lease and starts its backend, and the refused gdaemon stays `refused` without contending when the lock frees again. One test covers every refusal class because the supervisor sees only status 78; 5.2.12 tests the classes. test: `crates/gdaemon/tests/lifecycle.rs::refused_daemon_releases_lease_to_standby`.
- 5.2.16 - `_run_gdaemon` raises `SchemaVerifyRefusal` for a missing gdaemon and a nonzero exit and plain `SchemaContractError` for a timeout and a launch `OSError`, and `runner.main` exits 1 for the latter two. test: `tests/storage/test_schema_contract.py::test_verify_refusal_versus_transient_failure`.

### 5.3 Retire the Python lease modules [category: refactor] (depends: 5.2)
`kind: deliverable`

Targets:
- `src/gobby/daemon_lease_control.py::*` — operation: delete — scope-reason: retire the entire file
- `src/gobby/servers/lease_fence.py::*` — operation: delete — scope-reason: retire the entire file
- `src/gobby/cli/daemon_lease.py::*` — operation: delete — scope-reason: retire the entire file
- `src/gobby/daemon_lease.py::ActiveDaemonLease`
- `src/gobby/daemon_lease.py::current_lease`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/runner.py::_on_lease_loss`
- `src/gobby/runner.py::_on_lease_invalidation`
- `src/gobby/runner_init/servers.py::_bind_runtime_grants`
- `src/gobby/servers/admit_barrier.py::*` — scope-reason: EffectFence consumer
- `src/gobby/servers/http.py::*` — scope-reason: EffectFence consumer
- `src/gobby/servers/exception_handlers.py::register_exception_handlers`
- `src/gobby/servers/app_factory.py::*` — scope-reason: consumer of `register_exception_handlers`; the call site is unchanged, verification only
- `src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`
- `src/gobby/cli/__init__.py::*` — scope-reason: unregister the lease command group
- `tests/servers/test_daemon_lease_control.py::*` — operation: delete — scope-reason: retire the entire file
- `tests/servers/test_lease_fence.py::*` — operation: delete — scope-reason: retire the entire file
- `tests/test_runner_lease_lifecycle.py::*` — scope-reason: standby and loss paths removed
- `tests/fixtures/test_postgres_safety.py::*` — scope-reason: lease reference updated
- `tests/ask/native_probe_harness.py::*` — scope-reason: imports `monitor_active_lease` and `drain_effect_fence` from the deleted modules; the contained runner drops both and follows the `_bind_runtime_grants` change
- `tests/e2e/conftest.py::daemon_instance`
- `docs/guides/admin-operations.md`

Delete `daemon_lease_control.py` (standby app and promotion loop), `lease_fence.py`
(`EffectFence`, `LeaseNotHeld`, `StaleEpochFence`) with the `admit_barrier.py` and
`http.py` consumers and the exception handlers, and `cli/daemon_lease.py` (`gobby
daemon lease ...`, superseded by the native routes; `gobby status` shows lease state).
`daemon_lease.py` keeps only the view from 5.1. `run_gobby` drops the lease
acquisition, standby serving, loss and invalidation callbacks; the runner is a plain
backend that exits when told. The e2e fixture drives gdaemon lifecycle for start,
stop, and restart. Update `admin-operations.md` for the native lease routes.

**Acceptance:**

- 5.3.1 - The three modules and their tests are deleted and nothing imports them. file: `src/gobby/daemon_lease_control.py`.
- 5.3.2 - The runner starts with no lease acquisition and exits cleanly on SIGTERM from gdaemon. test: `tests/test_runner_lease_lifecycle.py::test_runner_has_no_lease_paths`.
- 5.3.3 - e2e start, stop, and restart go through gdaemon and the lifecycle suite passes. symbol: `daemon_instance`.

## D1 `gobby-mcp` crate: native MCP transports with OAuth and DCR (depends: 4.4)
`kind: deferred`

Claude Code and the other CLIs launch `gobby mcp-server`, a Python stdio wrapper that
reads the credential itself and dials the daemon per request. A thin node has no
Python, and remote MCP clients need an HTTP transport with standard authorization.
The replacement is a workspace-private `gobby-mcp` crate (binary `gmcp`, per
decision 16) with this contract, decided on 2026-09-10:

- **Transports.** stdio for local CLIs (replaces `gobby mcp-server` in every CLI
  config the installer writes) and streamable HTTP for remote MCP clients, served
  through the front door as the `mcp` route family.
- **Restart survival.** The stdio process outlives gdaemon and backend restarts: it
  holds no session state of its own, dials the loopback front door per request with
  the bootstrap key, and returns the typed unavailable result while the front door
  or backend is down, the way `DaemonProxy._request` does today.
- **Authorization.** OAuth 2.1 per the MCP authorization specification: the HTTP
  transport is a resource server publishing protected-resource metadata; gdaemon
  hosts the authorization server endpoints (metadata, Dynamic Client Registration,
  authorize, token) backed by a client-registry table and the `users` and
  `api_keys` identity from 4.2; access tokens are a second bearer kind validated at
  the front door seam 4.3 reserves.
- **Ownership.** S2.10 (external-MCP multiplexer) and S2.12 (MCP front door flip)
  own the crate; the OAuth endpoints and client registry are planned with S2.12.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "The MCP crate, its HTTP transport, and OAuth/DCR issuance belong to the Stage 2 MCP takeover; Stage 1 delivers the API-key identity they build on and keeps the front-door bearer seam open for the second token kind."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - 4.4.5
```

## D2 gcode index writes from a node need hub HTTP routes (depends: 4.4)
`kind: deferred`

Decision 17 removed the datastore tunnel, so `gcode index` on a node cannot write
PostgreSQL directly; it needs hub HTTP routes (was S4.5). Owned by S2.7 and S4.1b.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Requires the Stage 2 native code-index family; out of Stage 1 scope."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - 4.4.5
```

## D3 Hook envelope carries edited-file content for disk-reading rules (depends: 4.3)
`kind: deferred`

Rules that read the edited file from disk cannot do so on a hub when the hook came
from a node; the envelope must carry the fact. Which rules do this is unverified.
Owned by S2.11 (hook ingress and the node-local envelope ledger).

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Depends on the S2.11 hook ingress design; the rule inventory is not yet verified."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - 4.3.7
```

## E1 End-to-end verification
`kind: verification`

Stage 1 closes when all of the following hold on the `0.5.0` branch with freshly
built and installed binaries:

1. Story A: `gobby install && gobby start` on a loopback bind brings up gdaemon and
   the backend with `tls.mode: off`; `gobby status` reports `standalone`, healthy;
   the web UI, Claude Code hooks, `gcode`, and `gclient` work unchanged; `gobby restart`
   respawns only the backend and existing agent grants stay valid.
2. Contract corpus: `GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/` and
   `cargo test -p gobby-daemon --test http_contracts` both pass on `schema_version` 2.
3. Front door: `cargo test -p gobby-daemon` passes including TLS, WS golden replay,
   lease, lifecycle, and node tests.
4. Story B pair: `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py`
   passes over self-signed TLS: enroll, relay, `/api/machines`, revoke.
5. Cutover hygiene: a literal sweep finds no `local_cli_token`, `X-Gobby-Local-Token`,
   `auth.api_token_hash`, `X-Gobby-Machine-Id` (client side), `daemon_lease_control`,
   or `lease_fence` reference under `src/`, `crates/`, `web/`, or `docs/`.
6. Repo gates: `uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/`,
   `cargo fmt --check && cargo clippy --workspace`, and the test-types audit pass;
   every hand-maintained production file stays under 1,000 lines.

## V1 Plan Changelog
`kind: framing`

- 2026-09-10: First draft after elicitation across sessions #12690's predecessors and
  #12690. Decision Record confirmed with the TLS-default amendment and the
  self-signed-TLS testing requirement. Materialized to
  `.gobby/plans/gdaemon-front-door.md`.
- 2026-09-10: Consumer sweep resolved before enhancement and adversarial review;
  draft validation passes with 5 phases and no consumer-coverage warnings. Two
  refinements from the sweep: the `front_door` and `hub` bootstrap keys stay
  bootstrap-only (`to_config_dict` and `DaemonConfig` unchanged; the runner reads
  the bootstrap object directly), and 5.2 keeps the existing Python
  `/api/admin/shutdown` and `/api/admin/restart` admission routes, deletes their
  process-relaunch helpers, and has the gdaemon supervisor act on the existing
  shutdown-intent marker instead of adding a native `/api/admin/backend/restart`
  route (Decision Record S1.3 restart semantics unchanged: backend-only by default,
  `--full` for the whole daemon).
- 2026-09-10: D1 rewritten as the `gobby-mcp` crate contract (stdio and streamable
  HTTP transports, restart survival, OAuth 2.1 with DCR hosted by gdaemon) owned by
  S2.10/S2.12; 4.3 dispatches bearers on kind (`gobby_` API key, `v1.` managed
  token, anything else 401 and reserved for OAuth access tokens) so the second
  token kind is a `front_door/auth.rs` change only.
- 2026-09-26: Current-code refresh under #22951 from Researcher drift evidence at
  `09b0f41781`; the Decision Record and P1 order 1.1, 1.2, 1.3 are unchanged. 4.3 names
  two pure-move splits for files past the 850-line heuristic (the `hooks/inbox.py`
  loop and retention passes to `hooks/inbox_maintenance.py`; the
  `spawn_agent/_implementation.py` selection block to `spawn_agent/_selection.py`) with
  4.3.8, 4.3.9, and a Granularity record. 1.3 replaces the deleted
  `_schema_restart_refusal` with the admissions `start` runs today, corrects the
  `daemon.py` size and importer list, and adds the missed consumers. 5.2 keeps
  #22403's refuse-before-stop guarantee for both restart forms and cutover (`--full`),
  keeps `_do_stop`'s admissions ahead of the SIGTERM fallback, treats a start-refusal
  exit as no-respawn, and adds 5.2.9 through 5.2.11 and the missed consumers. 4.2
  reuses the existing `hub_daemon_url` instead of adding `hub_url` (4.4 and Constraints
  follow), and its migration moves from the occupied 431 to 454. Bootstrap docs
  target `docs/guides/configuration.md` because `docs/guides/bootstrap.md` does not
  exist. The `BootstrapConfig` importer count is 52.
  Consumer sweep additions: the contained Ask probe harness and its two tests
  (5.1, 5.3), the three token-file `AuthService` test harnesses (4.3), and two
  `daemon_instance` consumers (1.3).
  Open for review (PD, 2026-09-26; resolve before M1): (a) 4.2 refuses `gobby auth
  login` on a `datastore_mode: local` bootstrap instead of switching the mode; the PD
  prefers the explicit refusal pending full review. (b) 5.2 folds in three inferred
  items: cutover restarts with `--full`, a start-refusal exit is not respawned, and the
  backend holds no pid-lock descriptor. (c) 1.3 and 4.2 carry no Granularity record.
  (d) After 5.1 the lease view reads a `deployment_runtime` row that only gdaemon
  writes, but `tests/ask/native_probe_harness.py` runs a contained runner without
  gdaemon; the plan does not yet say how the probe gets that row. All four resolved
  2026-09-27 (next entry).
- 2026-09-27: Open review points resolved under #22951 by Lane 7 (gobby#14682), from
  source at `0.5.0` `0cc1ad1ea8`; pending PD design review. (a) 4.5 keeps the refusal: a
  mode switch cannot yield a loadable bootstrap (`_validate_managed_database_url`
  rejects loopback DSNs in remote mode) and would strand `files_home`; login also stops
  writing `hub_daemon_url` and refuses a differing `--hub` (4.5.3).
  `docs/guides/shared-stack.md` joins 4.3 for the token-copy removal. (b) 5.2: cutover
  `--full` confirmed from `run_cutover`; start refusals become a typed `StartRefusal`
  with exit 78 (5.2.12), the typed 503 gains `backend.state: refused` with the refusal
  text (5.2.10 extended), `gobby start` stops waiting on it, and `gobby restart` falls
  back to `--full` while the backend is not serving (5.2.13); holding the lease while
  refused is recorded with its rejected alternative; the no-pid-lock-descriptor rule is
  confirmed with its reason. New finding beyond (a)-(d): nothing stopped an orphaned
  backend after an abrupt gdaemon death, so 5.2 adds a supervisor liveness pipe
  (5.2.14). (c) 1.3 splits
  the ghook typed-503 suppression into new 1.4, re-derived against current ghook
  because `daemon_is_reachable` no longer exists (suppression keys on
  `DeliveryFailureKind`, so `post_and_cleanup` classifies the typed 503 as `Connect`;
  1.3.3 moves to 1.4.1; 1.3.4 and 1.3.5
  become 1.3.3 and 1.3.4; 1.3 depends on 1.4), and 4.2 splits `gobby auth login`/`auth
  key` into new 4.5 (4.2.4 and 4.2.5 move to 4.5.1 and 4.5.2; 4.2.6 and 4.2.7 become
  4.2.4 and 4.2.5, so Round 1's "item 4.2.7" is now 4.2.5; 4.3 and 4.4 depend on 4.5).
  Both keep one-leaf Granularity records for the remainder, and 5.2's record now covers
  the refusal status and the pipe. (d) 5.1 seeds the row in the contained probe with
  the upsert gdaemon performs, following the `tests/code_index/conftest.py` precedent
  (5.1.6); spawning a pinned gdaemon is rejected. New 5.2 consumers:
  `GobbyRunner.create`, `init_storage_and_config`, and their tests.
- 2026-09-28: PD design review (gobby#14730) of candidate `6d8bf7441e` accepted 4.5 and
  the liveness pipe and bounced 5.2's start-refusal lease hold, which let a
  machine-local refusal block a healthy hub standby indefinitely. Revised under #22951
  by Lane 7 (gobby#14682) against `0.5.0` `4649f87487`: `gdaemon schema verify` checks
  the database against this machine's compiled identity, so a schema refusal cannot be
  proven shared and the policy keys on status 78 alone. A refused gdaemon releases the
  lease, keeps the per-machine pid claim, and stays in a non-contending `refused` state
  until process exit, with operator recovery written out (5.2.15). New finding: the
  plan made every `verify_schema` failure a refusal, so a timeout or launch failure
  would strand a healthy machine; `_run_gdaemon` now raises `SchemaVerifyRefusal` only
  for a missing binary or a nonzero exit, and the transient failures stay crashes
  (5.2.16). The 2026-09-27 hold decision is superseded. New 5.2 targets: `lease/mod.rs`,
  `schema_contract.py`, and `schema_divergence.py`, plus `test_schema_contract.py`.
- 2026-09-28: Writer #14578 read-only consensus input on `84029b7643` agreed the plan
  is ready and raised one medium finding: an orphan draining on liveness-pipe EOF writes
  a `stop` marker that is cleared only when the next runner listens, so a new gdaemon
  whose first backend dies early would read it and exit. A first fix cleared the marker
  before every spawn (`4a098defa9`); the Writer's re-check found that drops ghook's
  planned-restart suppression during backend startup (1.4). Final: the supervisor
  records the pre-spawn marker's bytes, leaves the file, and treats a byte-equal marker
  as absent, which also stops a consumed `restart` marker from respawning without
  backoff (5.2.8 extended with both cases); timestamp comparison and pre-spawn deletion
  are recorded as rejected.
- 2026-09-29: Under #23098 (PD option (a)), P2 and P3 moved to single-phase slice
  plans `gdaemon-run-modes.md` (root #21553) and `gdaemon-http-contract-corpus.md`
  (root #21552), each refreshed against the landed P1 code; this plan keeps a pointer
  section. 2.2 `AppState` moves to its first consumer (4.4 or 5.1). The P4, 4.3, and
  4.4 waits on P2, 3.1, and 2.3 are dropped here and restored by P4 slice planning
  against the slice leaves; P4 now depends on P1 directly, which keeps the P1-before-P4
  ordering that ran through P2.

**Round 1** `kind: enhancement`

- enhancer_run: a3018b92-1ecf-4981-885b-a3c43ad44dad
- enhancer_session: 276759ab-a76b-4a34-a67f-84a6a51ec938
- converged: false
- suggestions_presented: 5
- accepted:
  - E1 / better / 4.2 pins owner scoping and redaction for list and revoke (foreign revoke answers 404 like an absent id; list carries no hash or plaintext); item 4.2.7. Security boundary; rung 2 reuse of the planned router and test file.
  - E3 / better / 4.4 registry replacement is an atomic swap with a per-connection id and identity-checked removal so a delayed old-channel cleanup never erases the successor; 4.4.2 extended. Real reconnect race in the planned design; rung 6 minimum mechanism.
  - E4 / better / 4.4 defines the node WS relay target as `wss://<hub_url>/ws` (the hub's HTTP-port `/ws` serves the same `WebSocketServer` as `:60888`, so no second hub port is configured), extends the pair test with one relayed WS upgrade, and adds 4.4.6. The node's WS listener target was unspecified; rung 2 reuse of the 1.2 splice and 4.1 pinned client.
  - E5 / clarity / 3.2 adds a completeness precheck that every natively servable family has a manifest case; item 3.2.4. Closes the gap where a Stage 2 native flip without recorded cases would pass the parity gate.
- declined:
  - E2 / better / wrap bootstrap enrollment's `upsert_seen` and `mint` in one transaction with a fault-injection test: over-mechanism at rung 1. `upsert_seen` runs first and raises the ownership conflict before any write; a mint failure after it leaves only an idempotently re-upsertable machine row and no key, which the next `gobby auth login` completes.
- resolution_notes: The user read all five suggestions and confirmed the votes as recommended. Four folded into 3.2, 4.2, and 4.4 as listed. Targets inventories unchanged (every named test file was already a Target). Constraints and the Decision Record unchanged.
