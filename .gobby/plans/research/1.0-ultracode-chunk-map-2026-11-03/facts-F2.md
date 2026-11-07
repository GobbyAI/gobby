# F2 facts: runtime switches and today's process topology

Fact-finder F2 for the map-v2 restructure (task #25629). This is a read-only pass over
`/Users/josh/Projects/gobby`, branch 0.5.0, HEAD `17343de2b1`, on 2026-11-03. Cites are
repo-relative path:line. Lines marked "observed" come from the live machine, not from code.
This report decides nothing.

## Bottom line

- Today `gobby start` runs `gdaemon serve` as the front door. gdaemon supervises one Python
  child, `python -m gobby.runner`, which binds loopback ports at the public ports plus 100.
  Every request that no hand-wired native surface takes is proxied to that child.
- The per-family switch already exists. It is `front_door.routes.<family>` in
  `~/.gobby/bootstrap.yaml`, with the values proxy, native and compare. A missing entry takes
  the family's compiled `default_backend()`, which is Proxy for every production family.
- The live bootstrap has no `front_door` block (observed). So the routes map is empty, and
  every family in the route table is proxied today.
- gdaemon reads the bootstrap once, at process start. There is no reload. A flip needs a full
  restart and no rebuild. A plain `gobby restart` restarts only the Python child and keeps the
  old route table.
- No runner-mode switch and no MCP gateway exist. Both are new work. Both can live in the same
  bootstrap file with no new store.
- gdaemon launches Python in exactly one mode: the argv in `GOBBY_BACKEND_COMMAND`. The Python
  launcher writes that argv, and the installed launchd plist freezes it at install time. A
  previous gdaemon generation stays a valid rollback only while that argv still starts today's
  full runner and Python still honors today's child contract.

## (a) Today's topology

### What `gobby start` launches

- `start` (src/gobby/cli/daemon_start.py:338-458) uses the OS service manager when a service is
  installed (daemon_start.py:414-438, `service_start` at :425). Otherwise it falls back to
  `_launch_direct_runner` (daemon_start.py:452).
- The direct path builds `backend_command = [sys.executable, "-m", "gobby.runner"]`
  (daemon_start.py:238). On every non-Windows host it then launches `[gdaemon, "serve"]`
  instead (daemon_start.py:242-247). It passes the Python argv as JSON in
  `GOBBY_BACKEND_COMMAND` (daemon_start.py:259).
- On Windows the direct path launches Python itself, and `gdaemon serve` refuses to run
  (crates/gdaemon/src/serve.rs:358-363).
- The launchd template runs `{{ gdaemon_executable }} serve` (src/gobby/install/shared/services/com.gobby.daemon.plist.j2:8-12)
  and sets `GOBBY_BACKEND_COMMAND` (plist.j2:23-24). The installer fills both from
  `_native_launch_context` (src/gobby/cli/installers/service_common.py:97-104). The systemd and
  Windows templates set the same variable (gobby-daemon.service.j2:12, gobby-launcher.cmd.j2:5).
- Observed: `~/Library/LaunchAgents/com.gobby.daemon.plist` (dated Oct 11) runs
  `/Users/josh/.gobby/bin/gdaemon serve` with `GOBBY_BACKEND_COMMAND` set to
  `["/Users/josh/Projects/gobby/.venv/bin/python3", "-m", "gobby.runner"]`. The process table
  shows gdaemon (parent launchd) with one `.venv/bin/python3` child, and gterm as a separate
  launchd child. The Python backend therefore runs the main checkout's source.

### What `gdaemon serve` does at start

1. It loads the bootstrap once (serve.rs:131). It refuses `front_door.enabled: false`
   (serve.rs:381-402, message at :395-399).
2. It composes the route families: the static `FAMILIES` plus `RuntimeFamily` when a pool exists
   (serve.rs:165-171). It warns about, and proxies, any routes entry naming a family it does not
   implement (serve.rs:173-178). It latches the terminal route (serve.rs:179).
3. `RuntimeState::prepare` claims the PID file, builds the lease and builds the
   `BackendSupervisor` from `BackendConfig::from_environment` (crates/gdaemon/src/state.rs:138-181,
   :158).
4. It binds `daemon_port` and `websocket_port` (serve.rs:288-303). Each listener's backend target
   is `127.0.0.1:<daemon_port + 100>` (serve.rs:407-423; crates/gcore/src/bootstrap.rs:40, 86-97).
5. `RuntimeState::run` loops: lease, `ActiveServices::start`, `supervisor.run`, stop
   (state.rs:183-252). `ActiveServices` always runs token-event retention and starts the terminal
   host only when the terminal route is native (crates/gdaemon/src/state_services.rs:27-93).
6. `serve_with_runtime` builds one `RouteTable` per listener (serve.rs:506-540, :532). Outside
   node mode it passes an empty `HeartbeatHost` (serve.rs:535).

### What gdaemon serves natively today

- Hand-wired surfaces in `FrontDoor::handle`, before the route table
  (crates/gdaemon/src/front_door/mod.rs:112-248): `/_gobby/*` WS bind and publish, `POST
  /api/internal/terminals/write`, `/api/admin/lease/*`, the typed 503 for a refused or standby
  backend, `/api/internal/nodes/*`, the handshake-challenge rewrite, API-key authentication,
  `/api/nodes/channel` on a hub, `/api/machines*`, and WS upgrades (native WS or splice). None of
  these sit behind a switch.
- Route-table families: `FAMILIES = [&HealthFamily, &TerminalFamily]`
  (crates/gdaemon/src/front_door/routes.rs:40), plus `RuntimeFamily` (crates/gruntime/src/lib.rs:123-157).
  - `health` claims `/api/health`. Its native handler still forwards to Python and adds
    `x-gobby-served-by: gdaemon` (crates/gdaemon/src/front_door/health.rs:81-103).
  - `terminal_ws` is a stub with no prefixes (routes.rs:43-55). It exists so the terminal latch
    has a key.
  - `runtime` claims `/api/runtime` and `/api/config` (gruntime lib.rs:124-129) and implements
    three rows (gruntime lib.rs:50-69).
- No production family overrides `default_backend`. The only override is a test family
  (routes.rs:619-636). With the live routes map empty, all three families resolve to Proxy.

### How a request reaches Python

- A request enters `FrontDoor::handle` (mod.rs:112-248). Authentication strips identity headers
  and stamps the resolved operator identity and the backend secret
  (crates/gdaemon/src/front_door/auth.rs:125-163).
- WS upgrades go to native WS or splice. Native WS parses only `subscribe` and `unsubscribe` and
  passes every other inbound frame to Python unchanged
  (crates/gdaemon/src/websocket/subscriptions.rs:1-8).
- Everything else calls `RouteTable::dispatch` (routes.rs:112-128). An unclaimed path, a Proxy
  family, or a request the family's `claims` declines goes to `proxy::forward`
  (crates/gdaemon/src/front_door/proxy.rs:43-99). That forwards to the loopback backend address.
- The supervised Python child binds `127.0.0.1` at the backend ports
  (src/gobby/runner_backend.py:71-77).

## (b) How each family's backend is chosen

### Source and precedence

- `RouteTable::new` takes `routes.get(family.name())` and falls back to
  `family.default_backend()` (routes.rs:89-109). The default is Proxy
  (crates/gcore/src/route_family.rs:29-31).
- The values come only from the bootstrap block `front_door.routes`. gcore parses it at
  crates/gcore/src/bootstrap/front_door.rs:161-201. Python parses it identically at
  src/gobby/config/bootstrap.py:328-355 (front_door.rs:1-2 states the parity rule).
- Allowed values are proxy, native and compare (front_door.rs:22-38, error at :188;
  bootstrap.py:45-46). Any non-empty string is accepted as a family key.
- There is no environment variable for routes and no database source. `GOBBY_TERMINAL_ROUTE` flows
  the other way, from gdaemon to Python (state.rs:160-169).
- Per request, `claims(&Parts)` can still send a request to the proxy whatever the backend
  (routes.rs:119-126; route_family.rs:33-35). Row-level coverage is therefore code, and the flip is
  per family.

### When a change takes effect

- The table is read once per process (routes.rs:1-9; serve.rs:131 and :532). gdaemon has no
  reload, signal handler or file watcher for it (a grep of crates/gdaemon/src for sighup, reload
  and file-watch terms finds nothing relevant).
- A change therefore needs a full restart and no rebuild.
- `gobby restart` without `--full` takes the backend-only path on a healthy daemon
  (src/gobby/cli/daemon_lifecycle.py:511). It posts `/api/admin/restart` (:574) and reports that
  "native supervisor retains its listeners and lease" (:600). gdaemon then respawns Python with the
  same config (crates/gdaemon/src/lifecycle/backend.rs:257-260). The old route table stays. A
  routes edit followed by a plain restart is silently ignored for HTTP families.
- `gobby restart --full` (daemon_lifecycle.py:452) and `gobby cutover` replace the process.
  Cutover runs `_do_stop` and then `start` (src/gobby/cli/cutover.py:401-413).
- An entry naming a family this binary lacks is warned about and proxied
  (`unimplemented_families`, routes.rs:152-163; serve.rs:173-178). A previous gdaemon generation
  given a newer routes entry therefore proxies that family. This is the existing per-family
  rollback property.

### The terminal latch, the one two-sided switch

- gdaemon latches `front_door.routes.terminal_ws` at start (routes.rs:59-75). A later different
  value is `RestartRequired` (crates/gterminals/src/family.rs:30-43, 70-78). Native maps to
  `SupervisorMode::Owner`, and proxy or compare to `Observer` (family.rs:17-21, 63-68).
- Native is refused while the stub claims no prefixes (family.rs:52-57; routes.rs:59-68).
- gdaemon hands the latched value to Python as `GOBBY_TERMINAL_ROUTE` (state.rs:160-169). Python
  refuses to start when its own bootstrap disagrees (runner_backend.py:80-91). It skips its own
  terminal host manager when the route is native (src/gobby/runner_init/terminal_wiring.py:64-72).
- The refusal exits 78 (src/gobby/runner.py:631, :644). gdaemon latches `Refused`
  (backend.rs:228-236). The refusal is sticky until a full restart (state.rs:237-247). So a
  terminal edit followed by a plain `gobby restart` leaves the daemon refusing.

### Compare mode

- The caller always gets Python's response (routes.rs:186-254). GET and HEAD run both legs and
  diff the JSON. Other methods proxy once and run `check_proxied`.
- Outcomes go to `~/.gobby/front-door-compare.jsonl` (crates/gdaemon/src/front_door/compare.rs:17).
  `gdaemon compare-report` exits 1 on a diff, a native error, a mismatch or an empty window
  (crates/gdaemon/src/main.rs:30-39, 107-116).
- The waiver manifest is compiled into the binary (crates/gdaemon/src/front_door/waivers.rs:12), so
  waivers change only with a new generation.

### Who writes the map

- Nothing in src/gobby writes the `front_door` key. A grep for `"front_door"` finds only the parser
  call at bootstrap.py:262. Flips today are hand edits.
- Python's bootstrap writer is a locked read-modify-write (src/gobby/config/bootstrap_io.py:52-70,
  90-110). `validated_bootstrap_payload` returns the merged mapping (bootstrap_io.py:113-129), so
  other keys survive a Python rewrite.

## (c) Settings gdaemon reads that survive restart and promotion

### bootstrap.yaml is the only one

- gdaemon reads `~/.gobby/bootstrap.yaml` at every start (serve.rs:131; bootstrap.rs:198-213).
- Promotion does not touch it. `src/gobby/install/bin_set_coherence.py` has no reference to the
  bootstrap. It writes binaries, signatures, promotion metadata and the identity stamp
  (bin_set_coherence.py:88-153, 181-201, 273-274).
- The file is per Gobby home. Isolated test daemons with their own home therefore get their own
  switches.

### Parser strictness by level (this decides where a new key can live)

- Top level: both parsers read named keys and ignore the rest. gcore does so at
  bootstrap.rs:215-261. Python does so at bootstrap.py:202-266, and refuses only three retired
  keys (`database_url_ref`, `postgres_install_mode`, `ui_expose`, bootstrap.py:207-218).
- Inside `front_door` and `front_door.tls`: unknown keys are refused by both
  (front_door.rs:12, 14, 168, 261-283; bootstrap.py:47, 50, 333-337).
- The strict parse also runs in Rust clients. `read_api_key_at` reads the API key through
  `read_hub_database_bootstrap_file` (crates/gcore/src/local_token.rs:8-19), which runs
  `parse_front_door` (bootstrap.rs:236). A new key inside `front_door` would therefore break the
  previous gdaemon, the live Python parser, and the API-key read in older Rust clients.
- A new route value beyond proxy, native and compare would be refused by older parsers in the
  same way (front_door.rs:183-190; bootstrap.py:348-351).

### Database and other candidates checked

- gdaemon's SQL touches `token_events`, `token_event_retention_totals`, `machines`, `api_keys`,
  `sessions` and `deployment_runtime`. None of these holds settings. `deployment_runtime` holds
  the lease fencing epoch and the grant signing secret (crates/gdaemon/src/lease/mod.rs:161-168).
- gruntime reads Python's published `config_active_state` only to serve `GET
  /api/config/effective` (crates/gruntime/src/config.rs:1, 54-74). Python publishes it.
- gdaemon and gruntime never read `config_store`. gcode does
  (crates/gcode/src/config/runtime_contract.rs:43).
- `project_policy` is a session variable set by a project rule
  (.gobby/workflows/rules/project-operating-policy.yaml:19-23). It is not daemon-readable.
- No config-epoch symbol exists in Python or Rust (a grep for config_epoch finds nothing). R2
  plans one.
- A database-backed switch store would be new. It would need a migration in gcore's single
  migration chain, which meets the restart and cutover schema preflight (F1's topic). It would
  still be read once at start unless a reload seam were also built.

### Validation before the stop is not proven

- A bad routes value makes `gdaemon serve` exit at `load_enabled_bootstrap` (front_door.rs:183-190;
  serve.rs:381-402). gdaemon has no config-check verb (main.rs:25-40).
- `restart_start_refusal` suppresses `BootstrapConfigError` while it reads the node mode
  (src/gobby/cli/daemon_preflight.py:59-62).
  It re-reads the bootstrap only as a fallback for the hub URL (daemon_preflight.py:98-102, 147-163).
- Whether the hub or operational-config reads catch a bad routes value before the stop was not
  proven. If they do not, a typo plus a full restart leaves no daemon.

## (d) MCP routing today

- gdaemon has no MCP handling. A grep for `/mcp` in crates/gdaemon/src finds nothing.
- Python serves two MCP surfaces. Streamable `/mcp` is at src/gobby/servers/app_factory.py:71-73.
  REST `/api/mcp/*` is at src/gobby/servers/routes/mcp/tools.py:52-62, including
  `POST /api/mcp/tools/call` (:57) and `POST /api/mcp/{server_name}/tools/{tool_name}` (:58).
- No family claims either prefix, so `lookup` finds nothing and `proxy::forward` runs
  (routes.rs:112-115). Every MCP call reaches Python's tool proxy and its policy pass.
- `gobby_core::mcp::ToolRegistry` exists (crates/gcore/src/mcp.rs:181-284). Its module doc says the
  hub gateway owns composition, authentication, policy and backend selection (mcp.rs:1-4). Only
  gcore's tests construct it (crates/gcore/tests/mcp_contract.rs:27-31, 92, 418).
- `ToolRegistry::new` refuses a duplicate server name (mcp.rs:191-195) and a duplicate tool name
  within a server (mcp.rs:199-204). One public server name therefore maps to exactly one
  `ToolFamily`.
- The contract keys backend state per public server name in bootstrap `front_door.routes`. Absent
  entries default to proxy (docs/contracts/internal-mcp.md:75-89). The gateway owns the single
  policy pass, and the Python compatibility endpoint must not add a second one
  (internal-mcp.md:86-88).
- Per-tool routing is not in the contract and not in the registry. The granularity is one server.
  Serving part of one server natively needs either a new per-tool override in the gateway or a
  move of those tools to the owning family's server.
- The route table cannot do per-server routing by itself. `claims` sees only `Parts`
  (route_family.rs:33-35). gmcp sends wait tools to `/api/mcp/tools/call` with the server and tool
  in the JSON body (crates/gmcp/src/call_tool.rs:110-119). Schema lookup is a POST body too
  (crates/gmcp/src/tools.rs:56). Streamable `/mcp` is JSON-RPC. The gateway must read the body.
- gmcp is Gobby's stdio MCP wrapper (crates/gmcp/README.md:1-9). CLI MCP configs launch it
  (src/gobby/cli/installers/mcp_config_shared.py:19). It serves a fixed meta-tool list
  (crates/gmcp/src/server.rs:53-132) and turns each call into a daemon REST call: non-wait tools go
  to `/api/mcp/{server}/tools/{tool}` (call_tool.rs:120-129), and discovery, search, recommend,
  server management and session variables use other `/api/...` paths (tools.rs:22-180). It
  reports readiness at `/api/mcp/bridge/ready` (server.rs:203-232). It executes nothing itself, so
  it needs no change when a server flips.
- MCP traffic over WS, such as chat `tool_call` events, rides application frames. Those always go
  to Python (subscriptions.rs:1-8). No route switch covers them.

## (e) How many ways gdaemon can launch Python

### gdaemon side: one mode

- `BackendConfig::from_environment` reads `GOBBY_BACKEND_COMMAND` as a JSON argv. Without it, the
  default is `python -m gobby.runner` (backend.rs:68-97). It is read once per gdaemon process, in
  `prepare` (state.rs:158). A backend-only restart respawns the same argv (backend.rs:257-260).
- gdaemon is argv-agnostic. It checks only that the executable is non-empty and that no argument
  has a NUL byte (backend.rs:83-86).
- The child contract is fixed in code:
  - `GOBBY_HOME` and `GOBBY_SUPERVISOR_FD`, a liveness pipe that carries a fresh write token
    (backend.rs:336-339, 366-377);
  - `GOBBY_FRONT_DOOR_SECRET_FD`, a separate secret pipe (backend.rs:346);
  - `GOBBY_TERMINAL_ROUTE` (state.rs:160-169);
  - exit 78 means a sticky refusal (backend.rs:228-236);
  - shutdown-intent markers pick stop, restart or terminal drain (backend.rs:239-262;
    crates/gdaemon/src/lifecycle/shutdown_intent.rs:11-17).
- `BackendConfig.environment` (backend.rs:58) is the existing channel for passing a mode value to
  the child. Today it carries only the terminal route.

### Launcher side: the argv is chosen outside gdaemon

- The direct path computes the argv at each start (daemon_start.py:238, 259).
- The service path uses the argv frozen into the unit at install time (service_common.py:97-104;
  plist.j2:23-24). The plist is rewritten only by install, or when it lacks the launch variables
  (src/gobby/cli/installers/service.py:106-119, 122-163; called at :205 and :411). Start, restart and
  cutover do not re-render it.
- The two paths can therefore diverge if the launcher's argv changes during the port.
- gdaemon tests also set the variable (crates/gdaemon/tests/lifecycle.rs:667,
  crates/gdaemon/tests/run_modes.rs:113).

### Python side: the mode comes from the environment

- `main` consumes the supervisor and secret descriptors (runner.py:596-649, :620).
- With a supervisor descriptor, `run_gobby` uses `SupervisedPidOwnership` (runner.py:417). Without
  one, on POSIX, it claims the PID file itself as a contained runner (runner.py:427).
- `backend_bind` picks loopback backend ports when supervised or when the front door is enabled.
  Only an unsupervised runner with the front door disabled binds the public ports
  (runner_backend.py:71-77).

### `front_door.enabled: false` is not a working rollback

- On POSIX, `gobby start` always launches gdaemon (daemon_start.py:242-247). The service template
  always runs `gdaemon serve` (plist.j2:8-12).
- `gdaemon serve` exits with `front_door_disabled` when the flag is false (serve.rs:395-399).
- So today the flag stops the daemon unless someone runs `python -m gobby.runner` by hand.

### What this means after R0-R6 add a worker entry point

- The binary does not choose the mode, so a previous gdaemon generation spawns whatever the
  launcher or the frozen plist hands it.
- That generation stays a valid rollback only while three things hold:
  1. `GOBBY_BACKEND_COMMAND`, in both the direct path and the installed plist, still names
     `-m gobby.runner`;
  2. `python -m gobby.runner` started without any new marker still boots today's full runner;
  3. Python on 0.9.0 still honors the child contract above.
- The Python source is the live checkout's, through the venv path (observed). Restoring a binary
  never restores Python.
- If 0.9.0 wrote a worker argv into `GOBBY_BACKEND_COMMAND`, an older gdaemon would spawn the
  worker without the new handshake. The new gdaemon should select worker mode itself. It can set
  a value in the child environment (the `GOBBY_TERMINAL_ROUTE` pattern) or derive the worker argv
  from the runner argv's interpreter.

## Implications: the minimal switch mechanism

| Switch | Exists today | Store and key | Default | New work | Rollback while Python exists |
|---|---|---|---|---|---|
| Family backend | Yes | `front_door.routes.<family>` in bootstrap.yaml | The compiled `default_backend()`, Proxy | Keep every new family's default at Proxy. Optionally add a writer verb on `update_bootstrap_yaml`, since nothing writes `front_door` today. | Set the entry to proxy and do a full restart. Or restore the previous generation, which warns about and proxies a family it lacks. |
| MCP gateway | No. The contract only. | Same map: one key for the gateway family, plus per-server keys named by public server name (internal-mcp.md:77-78) | Proxy | The gateway as a route family that claims `/api/mcp` and `/mcp`, default Proxy. Body-aware per-server dispatch inside its router. Ownership of the policy pass. A per-tool override only if a chunk must split one server. | Gateway key to proxy plus a full restart puts every call back on Python's current path and policy pass. A per-server key to proxy returns one server. A previous generation ignores both kinds of key. |
| Runner mode | No | A new top-level key, for example `runner: python` or `rust`, added to both parsers in one commit | python | Parse it in gcore and Python. Latch it at gdaemon start. Hand it to Python through the child environment. Python obeys the latch. | `runner: python` plus a full restart. Or the previous generation, which ignores the key and spawns today's full runner. |
| Family loop ownership (cron, lifecycle jobs) | Only for terminals | Derived from the family entry or runner mode that gdaemon latched | Python owns the loops | Generalize the terminal latch: gdaemon hands Python its latched set, and Python skips the loops gdaemon owns. | Follows the switch it derives from. |

Rules these facts imply for the map:

1. Use bootstrap.yaml as the only switch store. Both sides parse it identically, it survives
   restart and promotion, and it already holds the family switch. A database store adds a
   migration and a preflight interaction and still gives no runtime flip.
2. All flips are restart-bound. Checkpoint cutover chunks and every rollback recipe must use a
   full restart. Cutover already does (cutover.py:401-413). A plain `gobby restart` ignores HTTP
   flips and makes a terminal mismatch a sticky refusal. A runner switch built on the terminal
   pattern would inherit that refusal. Python should follow gdaemon's
   latch rather than refuse with exit 78, or the restart CLI should force a full restart when a
   latched switch changed.
3. Put every new key at the top level. Never add a key inside `front_door`. Never add a fourth
   route value. Either change breaks older parsers, including older Rust clients' API-key reads.
4. Validate the edited bootstrap with both parsers before the stop. The restart preflight does not
   provably catch a bad value (daemon_preflight.py:59-62), and gdaemon exits on one.
5. Dormancy needs every landed effect behind a switch that defaults to today's behavior. Hand-wired
   surfaces in `FrontDoor::handle` (mod.rs:112-248) and services in `ActiveServices`
   (state_services.rs:27-93) run unconditionally, so port code must register through the route
   table or a switch-gated job.
6. `RouteTable::new` builds every compiled family's router even while proxied (routes.rs:96-106).
   `serve.rs` constructs `RuntimeFamily` unconditionally when a pool exists (serve.rs:165-171).
   Family constructors must therefore be inert.
7. A `default_backend` change is code and goes live at the next promotion of the 0.9.0 checkout,
   whoever runs it. Keep defaults at Proxy until the family's own checkpoint, or until deletion.
   This defers the planned native-default flip (seams.md gap 10).
8. Keep `GOBBY_BACKEND_COMMAND` on `-m gobby.runner` in the launcher and in the frozen plist until
   Python is deleted. Keep today's child contract working in 0.9.0 Python while any older
   generation is a rollback target.
9. Compare is the live-observation step per family. The caller always gets Python's answer, and
   `gdaemon compare-report` scores the window. A checkpoint can go from proxy to compare to native.
10. Startup logs only unimplemented families (serve.rs:173-178). Nothing reports the latched
    backends or runner mode, and only health adds `x-gobby-served-by`. One startup log line per
    non-proxy latch is cheap evidence for the live window.
11. WS application frames, including MCP-over-WS tool calls, have no switch. They stay in Python
    until a WS handler seam exists.
12. After a family's Python code is deleted, proxy no longer works as its rollback. From then on,
    rollback needs the previous Rust generation (F3's mechanism).

## Not verified

- Whether the restart or cutover preflight refuses a malformed `front_door` value before stopping.
- How long a full restart takes. The drain budget alone is 17 s (backend.rs:93); start and
  readiness were not measured.
- How the schema preflight treats an older generation against a database migrated by a newer one.
  That is F1 and F3's topic, and it bounds how long the previous generation stays usable.
