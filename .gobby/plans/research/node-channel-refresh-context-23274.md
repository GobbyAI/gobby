# #23274 node-channel refresh 2: working context (Plan Writer gobby#16361)

Base: `0.5.0` at `093e210fb7` (2026-10-11). Plan: `.gobby/plans/gdaemon-node-channel.md`
(consensus 2026-10-10 at `2ee7957aef`, M1 at `8bd25b3c8a`). Coordinator gobby#15977.
Adversary gobby#16364. No enhancer message received yet. Task #23274 claimed by gobby#16361.

## Why refresh
#23457 (Atomic gdaemon lifecycle, closed 2026-10-11) landed a DIFFERENT shape than the
plan's settlement (a)/(b): no `AppState`/`ModeServices`. Its task description records a
re-settlement (gobby#16181 via Lane 2 gobby#15997): gcore owns ONE `RunMode` parser
matching Python; `HubDatabaseBootstrap` carries derived mode + hub endpoint fields;
gdaemon `state.rs` owns PID/lease/backend lifecycle; node state has no
`ActiveDaemonLease`; `FrontDoorState` holds optional runtime projection; #23274
"consumes this parser/state and creates no second container".

## Coordinator rulings this run
- #23269's commit 91a483db0d never landed on 0.5.0 (refusal absent). Orchestrator filed
  #24075 (lands it after #23458, lane 2). Plan treats #24075 as a prerequisite EDGE the
  Orchestrator adds, NOT plan scope, no manifest edge (contract lines 386-389).
- Plan against landed code (RuntimeState, minimal RunMode).

## Landed facts (verified at b81397088d / 093e210fb7)
- gcore `bootstrap.rs` 963 total, first `#[cfg(test)]` line 417. crates/AGENTS.md says inline
  tests count toward the 1,000 ceiling -> 1.1 puts new parsing in NEW
  `crates/gcore/src/bootstrap/run_mode.rs` (keep bootstrap.rs net growth tiny).
  `HubDatabaseBootstrap` (l.42-53) fields: `mode: RunMode`, `hub_daemon_url: Option<String>` (raw),
  database_url, api_key, daemon_url, bind_host, daemon_port, websocket_port, front_door.
  `RunMode {Standalone, Hub, Node}` l.57-62 (derive Clone Copy Debug PartialEq Eq).
  Manual Debug l.65-76 omits mode/hub_daemon_url. Mode match inline in
  `parse_hub_database_bootstrap` (l.207-248): remote->Node ALWAYS (even hub:true); local/None:
  hub must be YAML Bool else "bootstrap hub must be a boolean"; other datastore_mode ->
  "bootstrap datastore_mode must be local or remote". hub_daemon_url raw optional string.
  `parse_files_home_view` l.299-360 (local rejection text lacks Python suffix, l.328);
  `parse_hub_origin` l.377-399 (no lowercase). `read_files_home_view` unchanged role.
- PARITY GAPS vs Python (`src/gobby/config/bootstrap.py` unchanged): `(remote, hub:true)`
  must RAISE "hub: true requires datastore_mode: local"; `hub: "yes"` is true via
  `_parse_yaml_bool`; remote requires hub_daemon_url; local rejects it ("...; this process is
  the owner"); own-origin rejection (`_own_origins` l.464); host lowercased; no hub_cert field.
- `crates/gcore/tests/bootstrap_mode.rs::native_mode_matches_python_bootstrap` asserts
  `("datastore_mode: remote\nhub: true", Node)` and `hub: string` errors -> MUST change (Target).
- `lease::LeaseMode` = `pub use gobby_core::bootstrap::RunMode as LeaseMode`; `ActiveDaemonLease::new`
  bails for Node (lease/mod.rs:73).
- gdaemon `serve.rs` 425 total / first cfg(test) 384. `run` (l.53-130): generates secret
  (no env read now), `load_enabled_bootstrap`, resolver+`AuthState` (all modes), backend_ports,
  unimplemented_families warnings, `tls::load`+warnings, runtime, block_on:
  `RuntimeState::prepare(&bootstrap, home, &secret)` -> `Option<(Arc<RuntimeState>, BackendSupervisor)>`
  (None = PID claim held elsewhere -> Ok), bind both ports, spawn `owner_run`, `serve_with_runtime(
  listeners, routes, tls, auth, Some(state), select{shutdown_signal, lifecycle})`.
  `load_enabled_bootstrap` l.152-172 default literal sets mode Standalone, hub_daemon_url None.
  Public `serve(listeners, routes, tls, auth, shutdown)` l.233 -> `serve_with_runtime(.., None, ..)`
  l.252-291 (HeartbeatHost, per listener FrontDoorState::new + `FrontDoorState{runtime, ..state}`
  literal l.268, RouteTable, FrontDoor::new, accept loops, select). `accept` l.296 private.
- `state.rs` 129 lines: header says node relay "consumes gcore's RunMode and this state; must never
  create a second mode parser or a database lease for a node". `RuntimeState {mode, lease: Option,
  lease_state: Option, backend: watch::Receiver<BackendStatus>}`. `prepare` claims PID via
  `claim_from_environment(home/gobby.pid)` FIRST, lease only for non-node (requires database_url
  "native runtime ownership requires bootstrap database_url"), then builds `BackendSupervisor::with_lease`
  in ALL modes (node too -> would spawn `python -m gobby.runner`; BUG for node). `run` loops lease,
  `ActiveServices::start` (state_services.rs: pool max 8 "gobby-gdaemon-active", terminals, retention
  when database_url), `supervisor.run`. `standby()`.
- `RuntimeState {` literal only in tests/lease.rs:58 (+ `state.runtime = Some(..)` l.76). `prepare`
  sole caller serve.rs:92. `claim_pid_file` returns None when flock held/other pid alive -> second
  serve on same home exits 0.
- front_door/mod.rs 181 lines: `FrontDoorState {#[cfg(unix)] runtime: Option<Arc<RuntimeState>>,
  target, backend_state, client, auth}`; `FrontDoor {state, table: Arc<RouteTable>}`,
  `FrontDoor::new(state, table: RouteTable)`. `handle`: observe_peer -> (runtime) admin_api route
  (auth + key required) / backend Refused 503 / standby 503 -> challenge::answer -> authenticate ->
  ws::splice / table.dispatch. So standby gate precedes native routes.
- health.rs: `unavailable(target: SocketAddr, state)`, `bad_gateway(target: SocketAddr, error)`,
  `json_response` now `pub(crate)` (reuse for channel/machines errors).
- Tests calling `serve(`: common/mod.rs 32,77; heartbeat.rs 323; http_contracts.rs 273;
  front_door.rs 424 (`start_authenticated_front_door` l.420). `FrontDoor::new`: serve.rs 275,
  front_door.rs 79,107,245,417, lease.rs 78. `postgres_resolver_and_node_mode_fail_closed` l.236
  still exists. `resolver_error_is_503_and_missing_secret_refuses_start` and
  `parent_fd_eof_stops_serve` NO LONGER EXIST under those names (do not cite).
- 1.6 anchors at tip: conftest `spawn_daemon_instance` l.1174, `_seed_e2e_runtime_state` l.566;
  `_project_admitted` servers.py:599; `issue_for_operator` handshake.py:115; ApiKeyManager mint l.71,
  revoke l.100; _health.py mode l.388; `_handle_ping` core.py:154; handshake.rs proof l.220;
  challenge.rs l.91; auth_login.py `_publish` l.275; `list_keys` api_keys.py:174;
  `_mount_ws_endpoint` _app_ui.py:46; `break_glass_admits` auth_service.py:410.

## Refresh design (decided; advisor-reviewed)
- Keep 6 leaves/numbering, D2 mapping, Decisions 2-14,17 semantics, 1.3-1.5 behavior & acceptance
  wording (two adversary rounds). Delete M1 (lines 1558-end). One dated 2026-10-11 changelog entry.
- Replace "Lifecycle Ownership Settlement (#23457)" with framing "Landed #23457 baseline": state the
  re-settlement, settlement (a)(b) superseded, (c)(d) survive; node contract.
- Decision 1: #23523 closed 2026-10-11 (gate satisfied, past tense); #24075 Orchestrator edge.
- Replace Decision 16: no AppState/ModeServices; RuntimeState is the mode-aware owner; parent D2's
  AppState prose not implemented (D2.1-D2.7 don't name it). Flag to coordinator with Decisions 2/3.
- Decision 15: #23269 refusal not on 0.5.0 at b81397088d; #24075 lands it; node never spawns Python.
- 1.1: Targets bootstrap.rs::* (add `hub_cert: Option<String>`, Debug prints mode/hub_daemon_url/
  hub_cert, call `run_mode::parse_mode_fields`, parse_hub_origin lowercases, local-rejection text =
  Python's, register `mod run_mode`), NEW bootstrap/run_mode.rs (fn returning (RunMode,
  Option<String> url, Option<String> cert) with Python rules; reuses existing `RunMode`, no new enum),
  bootstrap/front_door.rs::* (`yaml_bool` pub(super)), serve.rs::* (literal gains hub_cert: None),
  tests/bootstrap_mode.rs::* (fix remote+hub to expect error, "yes" to Hub, or fold into vectors),
  tests/fixtures/bootstrap_run_modes.json, crates/gcore/tests/bootstrap_run_modes.rs,
  tests/config/test_bootstrap_run_modes.py.
- 1.2 "Node startup: a node takes only the PID claim and answers the challenge itself":
  state.rs: `pub enum Prepared { Backend(Arc<RuntimeState>, BackendSupervisor), Node(PidFileClaim) }`;
  `prepare` -> `Result<Option<Prepared>>`, returns `Prepared::Node(claim)` right after the claim (no
  lease, no BackendConfig/supervisor); header comment updated. `NodeServices { bootstrap_path,
  hub_daemon_url }` + `NodeServices::from_bootstrap(&bootstrap, path)` (api_key required, message
  names `gobby auth login`). serve.rs run: match prepared: Node -> NodeServices, bind, `serve_doors`
  with `FrontDoor::node`; Backend -> resolver+AuthState built HERE (moved), owner_run,
  serve_with_runtime. Extract `pub async fn serve_doors(doors: Vec<(TcpListener, FrontDoor)>, tls,
  shutdown)` from serve_with_runtime (HeartbeatHost + accept loops). front_door/mod.rs private `enum
  Door {Backend{state, table}, Node(Arc<NodeServices>)}`, `FrontDoor::node`; node arm: observe_peer,
  challenge::answer(&node.bootstrap_path), else `health::unavailable(&node.hub_daemon_url, Down)`.
  health.rs `impl Display` targets. Tests: NEW tests/run_modes.rs (1.2.1 prepare returns Node w/o
  database_url and backend modes require it; NodeServices refuses w/o api_key), 1.2.2 spawned node
  serve with database_url "not a PostgreSQL DSN", terminal_ws native, GOBBY_BACKEND_COMMAND writing a
  marker -> answers interactive challenge, other request 503 target=hub_daemon_url, marker absent,
  second serve on same home exits 0 (same PID claim). 1.2.3 resolverless rename in front_door.rs.
- 1.3 relay: NodeServices gains relay + websocket_port; from_bootstrap hub_cert rule; serve.rs node arm
  passes ws_listener; drop all "AppState::from_bootstrap"/"no GOBBY_FRONT_DOOR_SECRET" wording.
- 1.4 channel: `NativeRoutes` (nodes/mod.rs) as `FrontDoorState.native` field defaulted in
  `FrontDoorState::new`; `serve_with_runtime` gains `native` param, public `serve` passes default ->
  NO edits to tests/front_door.rs or run_modes.rs. Hub `HubNodes` built in run's Backend arm when
  mode==Hub (database_url already required by prepare). Node client spawned in Node arm, aborted
  after serve_doors. Standby gate precedes native routes (standby hub answers 503 standby). Tests use
  `serve_doors` for hub doors with native routes.
- 1.5 machines: `NativeRoutes::Standalone(Option<Arc<Pool>>)` built in run's Backend arm for
  standalone; drop the four `db: None` test-literal Targets and state.rs Target.
- 1.6: drop GOBBY_FRONT_DOOR_SECRET/PARENT_FD clauses; update line anchors above.
- Constraints: re-measure; sweep base 093e210fb7; add sweeps `RuntimeState {`, `RuntimeState::prepare`,
  `serve_with_runtime`, `FrontDoorState {`, `.runtime =`, `claim_from_environment`.

## Finish
`uv run gobby plans validate .gobby/plans/gdaemon-node-channel.md -p /Users/josh/Projects/gobby`
(+ gobby-plans:validate_plan; #23932 timeouts known). Commit `[gobby-#23274] docs: ...` on 0.5.0 in main
checkout with Co-Authored-By. Send gobby#16364 path+SHA+sweep evidence; send gobby#15977 SHA + flags
(Decisions 2/3; parent-D2 AppState deviation; Prepared enum shape).
