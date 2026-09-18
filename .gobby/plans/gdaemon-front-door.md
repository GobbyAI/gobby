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
  binaries get 401 until rebased or reinstalled.
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
  `hub_url` over one warm pinned-TLS connection. S2.11 closes as relay. The hub
  reaches a node only over the node-opened channel; there is no daemon-endpoint
  column.
- **Expansion mapping.** The root is #21543. Phases P1, P2, P4, P5, P3 correspond to
  the existing placeholder epics #21551, #21553, #21555, #21554, #21552. At expansion
  apply the coordinator either re-parents the generated leaves under those epics or
  closes the placeholders as `duplicate` of the generated phase sub-epics; the
  placeholders have no children today. #21575 (S4.2) is delivered by 2.3 and closed as
  `duplicate` at apply. ROADMAP.md S1.4 row, #21555 text (`sk-` prefix, migration 421,
  runtime-handshake bootstrap, endpoint columns) and #21578's mode wording are
  reconciled at materialization to match this plan.
- **Migration numbering.** Latest is `430_add_run_evidence_and_reports.sql`; this
  plan's migration is `431`. Never a baseline edit.
- **Consumer sweeps.** Exact-symbol Targets were resolved with `gcode outline` and
  `gcode grep -w <symbol> src tests crates -l` on branch `0.5.0` at commit
  `569960eab3` (2026-09-10), and every consumer the code index reports for an exact
  Target is in some deliverable's Targets (validation reports zero consumer-coverage
  warnings). Conventions used: a symbol the plan changes is an exact Target; a file
  that only consumes a changed symbol is a `::*` entry whose scope-reason names the
  consumed symbol and the edit (an import or patch path that moves, an argument
  that changes) or says `verification only` when the file needs no edit and is
  re-run; a module the plan rewrites end to end (`src/gobby/config/bootstrap.py`,
  `src/gobby/utils/local_token.py`, `crates/gcore/src/local_token.rs`) is one `::*`
  entry. The index lists 42 importers of `BootstrapConfig`; all construct it with
  keyword arguments or read existing fields, and every field this plan adds has a
  default (`front_door.enabled: true`, `hub: false`, `tls.mode: off`, `api_key`,
  `api_key_id`, `hub_url`, `hub_cert` absent), so none of them changes. Targets the
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
- `docs/guides/bootstrap.md`
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
the offset convention in `docs/guides/bootstrap.md`.
`crates/gcore/assets/config/runtime_config_contract.json` is a derived carrier of
`src/gobby/config/` and must be regenerated in this leaf.

**Acceptance:**

- 1.1.1 - Both parsers accept the `front_door` block, default `enabled` to true, and reject an unknown route value. symbol: `parse_hub_database_bootstrap`. file: `src/gobby/config/bootstrap.py`.
- 1.1.2 - `backend_ports` returns the `+100` pair in both languages. test: `tests/config/test_bootstrap.py::test_backend_ports_offset`.
- 1.1.3 - The runtime config contract carrier is regenerated and validation passes. file: `crates/gcore/assets/config/runtime_config_contract.json`.
- 1.1.4 - The block and the offset convention are documented. behavior: "front_door block" in `docs/guides/bootstrap.md`.

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

### 1.3 Sibling start topology, backend ports, ghook typed-503, e2e front-door mode [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/daemon.py::start`
- `src/gobby/cli/daemon.py::_launch_direct_runner`
- `src/gobby/cli/daemon.py::_is_daemon_healthy`
- `src/gobby/cli/daemon_start.py`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/runner.py::main`
- `src/gobby/runner.py::_healthy_daemon_running`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner_init/__init__.py::*` — scope-reason: re-exports `init_servers`; verification only
- `src/gobby/runner_lifecycle.py::*` — scope-reason: consumer of `_healthy_daemon_running`; the bind-race check passes the backend port it failed to bind
- `src/gobby/cli/_install_daemon.py::*` — scope-reason: imports of `start` and `_is_daemon_healthy` move to `gobby.cli.daemon_start`
- `src/gobby/cli/cutover.py::*` — scope-reason: import of `start` moves to `gobby.cli.daemon_start`
- `src/gobby/cli/hub_backup/cli.py::*` — scope-reason: import of `start` moves to `gobby.cli.daemon_start`
- `src/gobby/cli/pack.py::*` — scope-reason: import of `start` moves to `gobby.cli.daemon_start`
- `src/gobby/cli/hub_backup/rehearsal.py::*` — scope-reason: `_SHARED_PORTS` module constant gains the backend port pair
- `src/gobby/cli/daemon_health.py::health`
- `crates/ghook/src/planned_shutdown.rs::daemon_is_reachable`
- `crates/ghook/src/planned_shutdown.rs::should_suppress_failed_post`
- `crates/ghook/tests/contract.rs::*` — scope-reason: add the typed-503 suppression contract cases
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
- `tests/test_runner_lifecycle.py::*` — scope-reason: port binding assertions move to the backend pair
- `tests/test_runner_shutdown.py::*` — scope-reason: consumer of `runner.main`; the pre-start health probe it patches now targets the public port
- `tests/providers/test_version_gate.py::*` — scope-reason: patches `run_gobby`; verification only
- `tests/config/test_restart_config_consumers.py::*` — scope-reason: consumer of `init_servers`; verification only
- `tests/runner_init/test_config_runtime_startup.py::*` — scope-reason: consumer of `init_servers`; verification only
- `tests/cli/test_cli_falkor.py::*` — scope-reason: patch paths for `start` move to `gobby.cli.daemon_start`
- `tests/cli/test_daemon_coverage.py::*` — scope-reason: patch paths for `start` move to `gobby.cli.daemon_start`; `health` assertions keep the public port
- `tests/cli/test_daemon_falkordb.py::*` — scope-reason: patch paths for `start` move to `gobby.cli.daemon_start`

`gobby start` keeps the pid claim and lease in the Python runner (S1.1 decision 1:
lifecycle moves in 5.2). When `front_door.enabled` is true it spawns `gdaemon serve`
first (from `~/.gobby/bin/gdaemon`, refusing when the installed schema identity does
not match as `_schema_restart_refusal` already checks), then the runner with the claim
fd, and waits for native health on the public port. When false it behaves exactly as
today. Move `start` and `_launch_direct_runner` into the new
`src/gobby/cli/daemon_start.py`; `src/gobby/cli/daemon.py` is at 997 lines and this
leaf must split it below the ceiling (the `daemon_start.py` bare-path Target is the
new file).

The runner (`run_gobby`, `init_servers`) binds the backend port pair on `127.0.0.1`
when the front door is enabled and the public ports on `bind_host` otherwise;
`init_servers` builds the `WebSocketConfig` and the HTTP server from the pair, so
`WebSocketServer` and `HTTPServer` are unchanged. `_healthy_daemon_running(port,
host)` keeps its signature: `main`'s pre-start probe passes the public port (gdaemon
answers) and `run_daemon`'s bind-race check passes the port it failed to bind.
`_SHARED_PORTS` gains the pair. `gobby status`/`health` keep reading the public
port. Importers of `start` and `_is_daemon_healthy` (`_install_daemon.py`,
`cutover.py`, `src/gobby/cli/hub_backup/cli.py`, `pack.py`, and the three CLI test
files) follow the move to `daemon_start.py`.

ghook change (S1.1 decision 4): `daemon_is_reachable` treats the typed 503
`unavailable` body as unreachable so planned-shutdown suppression triggers on it;
any other HTTP response still counts as reachable. gclient needs no change (non-2xx
already means unreachable).

e2e: `daemon_instance` gains a front-door mode (default on) that launches the pinned
`select_test_gdaemon()` in front of the real runner with an isolated `GOBBY_HOME`,
free public and backend ports, and waits on the public health route;
`test_daemon_lifecycle.py` and `test_daemon_auth.py` run through it.

**Acceptance:**

- 1.3.1 - `gobby start` spawns gdaemon then the runner and waits for public health; `front_door.enabled: false` restores today's topology. file: `src/gobby/cli/daemon_start.py`.
- 1.3.2 - The runner binds the backend pair on loopback behind the front door. test: `tests/test_runner_lifecycle.py::test_backend_ports_behind_front_door`.
- 1.3.3 - ghook suppresses fail-open hooks on the typed 503. test: `crates/ghook/tests/contract.rs::typed_503_counts_as_unreachable`.
- 1.3.4 - The e2e fixture runs the real runner behind the pinned gdaemon and the lifecycle and auth suites pass through it. test: `tests/e2e/test_daemon_lifecycle.py::test_daemon_starts_behind_front_door`.
- 1.3.5 - `src/gobby/cli/daemon.py` is below 1,000 lines after the split. file: `src/gobby/cli/daemon.py`.

## P2: Run modes (S1.2, #21553; S4.2 #21575 pulled forward) (depends: P1)
`kind: framing`

**Goal**: bootstrap names the mode, gdaemon carries mode-specific services in a typed
container, and a node refuses to run hub maintenance.

### 2.1 `hub` flag in both parsers and writers, mode name on health [category: config]
`kind: deliverable`

Targets:
- `crates/gcore/src/bootstrap.rs::HubDatabaseBootstrap`
- `crates/gcore/src/bootstrap.rs::DatastoreMode`
- `crates/gcore/src/bootstrap.rs::parse_hub_database_bootstrap`
- `src/gobby/config/bootstrap.py::*` — scope-reason: the dataclass, `_parse_datastore_mode`, `bootstrap_from_mapping`, and the new `run_mode()` accessor change together; `hub` defaults to false so constructor sites need no edit
- `src/gobby/config/bootstrap_io.py::update_bootstrap_yaml`
- `src/gobby/config/postgres_bootstrap.py::*` — scope-reason: consumer of `update_bootstrap_yaml`; the writer's signature is unchanged, verification only
- `src/gobby/ui_exposure.py::*` — scope-reason: consumer of `update_bootstrap_yaml`; verification only
- `src/gobby/cli/install_setup.py::ensure_daemon_config`
- `src/gobby/cli/install_files_home.py::*` — scope-reason: consumer of `ensure_daemon_config`; verification only
- `src/gobby/install/shared/config/bootstrap.yaml.j2`
- `crates/gdaemon/src/front_door/health.rs`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `docs/guides/bootstrap.md`
- `tests/config/test_bootstrap.py::*` — scope-reason: bootstrap parser tests gain the new block's cases
- `tests/config/test_files_home.py::*` — scope-reason: consumer of `update_bootstrap_yaml` and `ensure_daemon_config`; written files gain the `hub` line
- `tests/cli/test_install_setup.py::*` — scope-reason: consumer of `ensure_daemon_config`; written files gain the `hub` line
- `tests/integration/sandbox/test_public_ghook_install.py::*` — scope-reason: consumer of `ensure_daemon_config`; verification only

Keep `datastore_mode: local | remote` and add `hub: bool` (default `false`).
`(local, false)` is `standalone`, `(local, true)` is `hub`, `(remote, false)` is
`node`; `(remote, true)` is rejected by both parsers with the same message. Add a
`run_mode()` accessor in both languages returning the mode name. Writers
(`update_bootstrap_yaml`, installer setup, the bundled template) emit the one extra
line. Native `/api/health` (proxied or native) adds `"mode": "standalone" | "hub" |
"node"`; when proxied, gdaemon injects the field into Python's JSON body. Reconcile
#21578's `runtime_mode: node` wording to `datastore_mode: remote` plus `hub: false`
at materialization. Regenerate the runtime config contract carrier.

**Acceptance:**

- 2.1.1 - Both parsers derive the mode from the pair and reject `(remote, true)`. symbol: `_parse_datastore_mode`. symbol: `DatastoreMode`.
- 2.1.2 - Writers emit `hub: false` by default. test: `tests/config/test_bootstrap.py::test_writers_emit_hub_flag`.
- 2.1.3 - `/api/health` reports the mode name. file: `crates/gdaemon/src/front_door/health.rs`.
- 2.1.4 - The config contract carrier is regenerated. file: `crates/gcore/assets/config/runtime_config_contract.json`.

### 2.2 `AppState` mode containers in gdaemon [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs`
- `crates/gdaemon/src/state.rs`
- `crates/gdaemon/src/front_door/routes.rs`
- `crates/gdaemon/tests/front_door.rs`

Add `crates/gdaemon/src/state.rs`:

```rust
pub struct AppState { pub common: Arc<CommonServices>, pub mode: ModeServices }
pub enum ModeServices { Standalone(StandaloneServices), Hub(HubServices), Node(NodeServices) }
```

`CommonServices` holds the front door (proxy client, routing table, backend target),
bootstrap view, and health state. Stage 1 variant structs are empty except for the
handles later leaves add (4.4 adds the node channel to `NodeServices` and the
registry to `HubServices`; 5.1 adds the lease to `StandaloneServices` and
`HubServices`). Families needing a mode-only service take that variant's struct and
are mounted only inside its `match` arm; no `Option` fields for mode-dependent
services. A node starts the front door and native health like the other modes and
reports `mode: node`; every other node refusal arrives with the leaf that owns it.

**Acceptance:**

- 2.2.1 - `AppState` is constructed per mode with no `Option` mode fields. file: `crates/gdaemon/src/state.rs`.
- 2.2.2 - A `node` bootstrap serves native health with `mode: node`. test: `crates/gdaemon/tests/front_door.rs::node_mode_serves_health_only`.

### 2.3 Python guards hub-only loops behind the mode flag (S4.2) [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/runner.py::GobbyRunner._initialize_runtime_services`
- `src/gobby/runner_init/services.py::*` — scope-reason: every maintenance loop registration reads the mode
- `tests/test_runner_lifecycle.py::*` — scope-reason: node-mode startup assertions

Every hub-only loop (cron scheduler, memory dream, code-index maintenance, session
reconcilers, backup rehearsal, agent launchers) is registered only when
`run_mode()` is `standalone` or `hub`. In `node` mode the runner refuses to start
them and logs one line per skipped loop at INFO. Nothing else changes: this exists so
the first hub-node pair test in 4.4 has a node that runs no maintenance. #21575 is
closed as `duplicate` of the leaf created from this section.

**Acceptance:**

- 2.3.1 - A `node` bootstrap starts no hub-only loop and logs each skip. test: `tests/test_runner_lifecycle.py::test_node_mode_skips_hub_loops`.
- 2.3.2 - `standalone` and `hub` start every loop as today. symbol: `GobbyRunner._initialize_runtime_services`.

## P3: HTTP contract corpus (S1.5, #21552)
`kind: framing`

**Goal**: a recorded request/response corpus for the proxied surface that pytest and
Rust both replay; the parity gate for every Stage 2 takeover. Independent of P1; can
start immediately.

### 3.1 Fixture format, Python recorder and replay, first corpus [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/http/manifest.json`
- `tests/contracts/http/README.md`
- `tests/contracts/http_corpus.py`
- `tests/contracts/test_http_corpus.py`
- `tests/contracts/conftest.py`

Fixture format (`schema_version` 1), one case per file under `tests/contracts/http/`:

```json
{
  "schema_version": 1,
  "name": "health_ok",
  "family": "health",
  "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
  "response": {"status": 200, "headers": {"content-type": "application/json"}, "body": {"status": "ok", "version": "@mask@"}},
  "mask": ["/response/body/version", "/response/body/uptime_seconds"]
}
```

`manifest.json` lists cases in order and carries the corpus-level `schema_version`
and, per family, `"parity": "proxy" | "native"` (Stage 2 flips a family to `native`
when gdaemon serves it). Headers are an allowlist on both sides: `content-type`,
`retry-after`, `x-gobby-user-id`, `x-gobby-machine-id`, `x-gobby-key-id`. `mask`
names volatile fields by JSON pointer; both harnesses replace them with `"@mask@"`
before comparing, and the recorder applies masks at write time so recordings are
deterministic.

`tests/contracts/http_corpus.py` holds the loader, mask, and compare helpers. The
recorder is a pytest option `--record-http-contracts` (in `tests/contracts/conftest.py`)
that drives each manifest case against the existing e2e `daemon_instance` fixture (the
fixture is consumed, not modified; it records whatever topology the fixture runs,
which is direct Python until 1.3 lands and the front door afterwards, and the
recorded Python behavior is the same either way) and rewrites the file.
`test_http_corpus.py` replays every case against the same fixture and asserts
equality, parametrized by case name.

First corpus: `GET /api/health` (200) and the typed 503 body from 1.2; `/api/config/*`
schema and values; reduced `GET /api/tasks`; `POST /api/runtime/handshake/challenge`
and `POST /api/runtime/handshake`; the 401 body shapes (`missing_auth`,
`missing_grant`, `forged_identity`, and the generic Authentication-required message);
the HTTP-200 internal-error envelope from `src/gobby/servers/exception_handlers.py`
(`{"status":"error","message":"Internal error occurred but request acknowledged","error_logged":true}`),
reproduced not fixed. The `X-Gobby-Local-Token` alias is not recorded: leaf 4.3
deletes it and re-records the auth cases with a `schema_version` bump.

**Acceptance:**

- 3.1.1 - The loader rejects a case whose `schema_version` differs from the manifest's. test: `tests/contracts/test_http_corpus.py::test_schema_version_mismatch_rejected`.
- 3.1.2 - `--record-http-contracts` rewrites every manifest case deterministically (two runs produce identical files). test: `tests/contracts/test_http_corpus.py::test_recorder_is_deterministic`.
- 3.1.3 - Every first-corpus case replays equal against the front-door fixture. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.
- 3.1.4 - The manifest carries per-family parity flags and the README documents the format, masks, and re-record procedure. file: `tests/contracts/http/README.md`.

### 3.2 Rust replay harness against gdaemon [category: test] (depends: 3.1, 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/tests/http_contracts.rs`
- `crates/gdaemon/tests/support/contracts.rs`

`crates/gdaemon/tests/http_contracts.rs` reads `tests/contracts/http/manifest.json`
from the workspace root, starts `gdaemon serve` on free ports with a case-driven stub
backend that answers each recorded request with the recorded response for `proxy`
families and with no backend for `native` families, sends each request through the
front door, masks, and asserts equality. A `native` family whose replay differs fails
the crate's test suite: that is the parity gate Stage 2 flips. The support module
holds the Rust loader, mask, and compare helpers (serde_json + JSON pointer), kept
behaviorally identical to `tests/contracts/http_corpus.py`.

A completeness precheck runs before replay: every family gdaemon can serve natively
(the static handler table in `front_door/routes.rs`; `health` alone in Stage 1) must
appear in the manifest with at least one case, and the test fails naming the missing
family. Bootstrap route names outside that table are not checked. This keeps the
parity gate closed when a Stage 2 leaf adds a native handler without recording its
family.

**Acceptance:**

- 3.2.1 - Every `proxy` family case replays byte-equal through gdaemon. test: `crates/gdaemon/tests/http_contracts.rs::proxy_families_replay_equal`.
- 3.2.2 - The `health` family replays equal in `native` mode. test: `crates/gdaemon/tests/http_contracts.rs::native_health_replays_equal`.
- 3.2.3 - Masking in Rust matches Python on a shared vector. test: `crates/gdaemon/tests/http_contracts.rs::mask_matches_python_vector`.
- 3.2.4 - Every natively servable family has at least one manifest case, and a missing family fails the suite by name. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.

## P4: API keys and node registration (S1.4, #21555) (depends: P2)
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
- `docs/guides/bootstrap.md`

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

### 4.2 `api_keys`, key format, issuance, `gobby auth login` and `gobby auth key` [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/431_add_api_keys.sql`
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
- `src/gobby/cli/auth.py::auth`
- `src/gobby/cli/auth.py::token`
- `src/gobby/cli/auth_login.py`
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
- `tests/cli/test_auth_login.py`
- `tests/cli/test_cli_auth.py::*` — scope-reason: consumer of the `auth` group and `token`; gains the `login` and `key` subcommand cases
- `tests/cli/test_cli_install.py::*` — scope-reason: consumer of `_provision_local_api_token`; asserts the minted key lands in bootstrap
- `tests/cli/test_install_coverage.py::*` — scope-reason: consumer of `_provision_local_api_token`; same
- `tests/cli/test_install_prompts.py::*` — scope-reason: consumer of `_provision_local_api_token`; verification only
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py::*` — scope-reason: patches `ensure_machine_identity`; verification only
- `tests/storage/test_machines.py::*` — scope-reason: consumer of `ensure_machine_identity`; asserts startup adoption mints the local key
- `tests/e2e/test_auth_login.py`
- `docs/guides/cli-commands.md`

Migration `431_add_api_keys.sql`:

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

Bootstrap gains `api_key`, `api_key_id`, `hub_url`, and `hub_cert` (both parsers,
`update_bootstrap_yaml` writer, config contract carrier regenerated):

```yaml
hub_url: https://hub.tailnet:60887
hub_cert: ~/.gobby/tls/hub.pem
api_key: gobby_...
api_key_id: 4f1c...
```

CLI (`src/gobby/cli/auth_login.py`, registered under the existing `auth` group;
`gobby auth token` is removed in 4.3, so this leaf leaves it in place):
- `gobby auth login --hub URL [--email] [--fingerprint sha256:...] [--label]
  [--insecure]`: connects with verification disabled, reads the leaf certificate,
  prints its fingerprint, and asks for confirmation unless `--fingerprint` matches (a
  mismatch refuses); writes the PEM to `hub_cert`; posts the bootstrap request over
  the now-pinned connection with this machine's id, hostname, and os; writes
  `api_key`, `api_key_id`, `hub_url` to bootstrap. `--insecure` permits `http://` to a
  non-loopback host.
- `gobby auth key --show | --rotate`, `gobby auth key list`, `gobby auth key revoke
  ID`. Rotate: mint via `POST /api/auth/keys`, write bootstrap atomically, verify with
  `GET /api/auth/status`, then revoke the old id; on verify failure revoke the new key
  and keep the old.

Hub machine: `_provision_local_api_token` in `gobby install` also mints the local
machine's key directly through `ApiKeyManager` under `require_sole_user()` and writes
it to bootstrap (the token file is still provisioned until 4.3). Startup adoption:
`ensure_machine_identity` mints the local key when bootstrap has none, so an existing
install gets its key on first start after this leaf.

**Acceptance:**

- 4.2.1 - Migration 431 creates `api_keys` and the two `machines` columns, and every derived carrier is regenerated. file: `crates/gcore/assets/schema/migrations/431_add_api_keys.sql`.
- 4.2.2 - `generate`/`parse`/`hash` agree across Python and Rust on shared vectors, and `parse` rejects a bad checksum. test: `tests/utils/test_api_key_format.py::test_cross_language_vectors`.
- 4.2.3 - Bootstrap route verifies the password, binds the machine, and returns the plaintext once; a foreign-owned machine gets 403. test: `tests/servers/routes/test_api_keys.py::test_bootstrap_mints_bound_key`.
- 4.2.4 - `gobby auth login` pins by fingerprint, refuses a mismatch, and writes bootstrap. test: `tests/e2e/test_auth_login.py::test_login_pins_self_signed_hub`.
- 4.2.5 - Rotate mints, verifies, then revokes the old key, and rolls back on verify failure. test: `tests/cli/test_auth_login.py::test_rotate_verifies_before_revoke`.
- 4.2.6 - Install and startup adoption mint the local machine's key into bootstrap. symbol: `ensure_machine_identity`.
- 4.2.7 - List and revoke are owner-scoped and redacted: one user cannot list or revoke another user's keys, a foreign revoke answers 404 like an absent id, and list responses carry neither `key_hash` nor plaintext. test: `tests/servers/routes/test_api_keys.py::test_key_management_is_owner_scoped_and_redacted`.

### 4.3 Hub-side key validation, front-door identity, and shared-token cutover [category: code] (depends: 4.2, 3.1)
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
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: `code_index_api_token` becomes the lease signing secret
- `src/gobby/hooks/inbox.py::*` — scope-reason: consumer of `read_local_api_token`; the missing-credential warning names `gobby auth login` instead of the token file
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

Single cutover; the token file, its hash, and the alias are gone after this commit.

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

### 4.4 Node channel, relay backend, and `/api/machines` [category: code] (depends: 4.1, 4.3, 2.3)
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
- `docs/guides/bootstrap.md`

**Relay backend.** In `node` mode the front door's backend target is `hub_url` over
the pinned client (`hub_cert`), one pooled hyper + rustls connection reused across
requests, instead of loopback Python. Requests are forwarded byte-for-byte, bearer
included; the node validates nothing and holds no table. A hub that refuses
connections yields the 1.2 typed 503 with `"target": "<hub_url>"`. Both node
listeners relay: the HTTP listener to `hub_url`, and the WS listener (`:60888` and
`/ws` on `:60887`) to `wss://<hub_url host:port>/ws`, because the hub's `/ws` on its
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
`datastore_mode: remote`, `hub: false`, `hub_url`, `hub_cert`, `api_key` from a
`gobby auth login` run against the hub), asserts the node appears `connected` in
`/api/machines`, that a request through the node's loopback front door reaches the
hub and returns the hub's identity, that one WS upgrade through the node's loopback
front door reaches the hub's `WebSocketServer` and echoes a frame, that the node
runs no maintenance loop (2.3), and that revoking the key closes the channel within
30 s and makes the next relayed request fail with 401.

**Acceptance:**

- 4.4.1 - A node relays to `hub_url` over the pinned connection and reports the typed 503 when the hub is down. test: `crates/gdaemon/tests/nodes.rs::node_relays_over_pinned_tls`.
- 4.4.2 - The channel registers the machine, heartbeats, and is replaced by a newer connection; when the old channel's cleanup runs after the new ack, the new channel stays registered and `connected` in `/api/machines`. test: `crates/gdaemon/tests/nodes.rs::channel_registers_and_replaces`.
- 4.4.3 - Revocation closes the channel within one heartbeat interval. test: `crates/gdaemon/tests/nodes.rs::revoked_key_closes_channel_on_heartbeat`.
- 4.4.4 - `/api/machines` lists rows with connection state. file: `crates/gdaemon/src/nodes/machines_api.rs`.
- 4.4.5 - The hub-node pair test passes over self-signed TLS end to end, including one WS upgrade relayed through the node. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.
- 4.4.6 - A node relays a WS upgrade to `wss://<hub_url>/ws` over the pinned connection and the golden frames and close codes pass byte-equal. test: `crates/gdaemon/tests/nodes.rs::node_relays_ws_over_pinned_tls`.

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

**Acceptance:**

- 5.1.1 - gdaemon acquires the lease, rotates the secret, and bumps the epoch; a second gdaemon enters standby. test: `crates/gdaemon/tests/lease.rs::second_daemon_enters_standby`.
- 5.1.2 - Standby serves the typed 503 `standby` body and the native lease admin routes. test: `crates/gdaemon/tests/lease.rs::standby_serves_typed_503_and_admin_routes`.
- 5.1.3 - Lease loss stops the backend and re-enters standby. test: `crates/gdaemon/tests/lease.rs::lost_connection_reenters_standby`.
- 5.1.4 - Python reads epoch and secret from its row and never writes them. test: `tests/test_daemon_lease.py::test_view_reads_deployment_runtime_row`.
- 5.1.5 - `gobby status` prints `standby` for a standby daemon. symbol: `health`.

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
- `src/gobby/cli/cutover.py::*` — scope-reason: import of `restart` moves to `gobby.cli.daemon_lifecycle`
- `src/gobby/cli/hub_backup/cli.py::*` — scope-reason: imports of `stop` and `restart` move to `gobby.cli.daemon_lifecycle`
- `src/gobby/cli/pack.py::*` — scope-reason: import of `restart` moves to `gobby.cli.daemon_lifecycle`
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
- `docs/guides/admin-operations.md`
- `docs/guides/cli-commands.md`

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
respawns with backoff (1 s doubling to 30 s).

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
ceiling and this split is the exemption; `cutover.py`,
`src/gobby/cli/hub_backup/cli.py`, `pack.py`, and the four CLI test files follow the
import move): `gobby start` spawns
`gdaemon serve` and waits for public health; `gobby stop` calls
`POST /api/admin/shutdown`, waits for the pid claim to clear, and falls back to
SIGTERM on the pid record; `gobby restart` calls `POST /api/admin/restart` by
default (the runner exits with intent `restart` and the supervisor respawns it) and
`--full` does stop then start; the restart-protected cron guard and
`--wait`/`--force` apply to both forms; `gobby status` probes the lock as today. The
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
- 5.2.8 - The supervisor parses a Python-written shutdown-intent marker and respawns on `restart`, exits on `stop`, and backs off on a crash. test: `crates/gdaemon/tests/lifecycle.rs::shutdown_intent_marker_drives_respawn`.

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
