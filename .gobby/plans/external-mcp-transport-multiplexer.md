Plan artifact: `.gobby/plans/external-mcp-transport-multiplexer.md`

# External-MCP transport multiplexer as a delegated backend

**Plan ID:** external-mcp-transport-multiplexer

## Overview
`kind: framing`

#21566 moves the transport and keeps the policy. The connection pool, process
supervision and framing for external MCP servers move into gdaemon. Discovery,
leases, schema validation, the rule engine, secrets, OAuth and result handling
stay in Python behind `ToolProxyService` and its `is_internal()` seam. The same
Rust pool then serves #24884's native gateway (rust-internal-mcp-servers 2.1.6).

The plan also closes hub-node row F5 (`.gobby/plans/research/hub-node-live-task-list-25236.md`).
A stdio server runs on the caller's machine, so a node session's local stdio or
file tool runs on that node, while URL connectors stay hub-hosted (F5.2, F5.3).
gmcp gains end-to-end cancellation, the one F5.1 gap the code shows (F5.4). A
live hub-node proof covers the rest of F5.1.

Josh's rulings (memory 83a9445f) apply: the hub-node proof covers macOS and
Linux, and no latency budget gates anything. There is no backward
compatibility. #23275's packaging (#24359) and server-side OAuth (#24360)
leaves keep their scope; this plan touches neither.

## Decision Record
`kind: framing`

1. **The multiplexer is a gdaemon module, `crates/gdaemon/src/mcp_client/`.**
   It exposes one Rust `Pool` API. Python reaches it through a loopback route
   that the Backend door answers in Standalone and Hub modes. Node mode reaches
   it only through node commands. #24884 calls the `Pool` directly.
   - Rejected: a new crate or `gobby-core`. Only gdaemon runs the pool, and a
     `gobby-core` change would force bumps of every dependent binary.
   - Rejected: a separate process. It would add a second supervisor and a
     second credential for no isolation the hub needs.
2. **Transports are stdio, streamable HTTP and legacy SSE. WebSocket is
   removed.** Stdio uses rmcp's `TokioChildProcess`, streamable HTTP uses
   rmcp's reqwest client, and SSE is a small client built on rmcp's
   `client-side-sse` parser, because rmcp 3.5.1 ships no legacy SSE client.
   The epic names exactly these three. WebSocket has no MCP specification
   transport, no bundled template and no installed server, so it is removed
   from models, the CLI, the web form and the tool description.
   - Rejected: dropping SSE too. The epic names it, and `oauth.py` authorizes
     SSE servers today.
3. **Python keeps all policy.** It keeps discovery, leases, schema validation,
   rules, the server registry and tool-inventory cache, `$secret` resolution,
   browser policy, metrics, untrusted-value cleaning, result offload and
   paging, and per-tool timeouts. The Rust pool receives a fully resolved
   server spec and runs only the protocol. Resolved header and env values are
   secrets: Python, gdaemon and the node never log them, return them in
   status, or echo them in errors, and `ServerSpec`'s `Debug` prints only
   header names and env keys. gmcp never sees a spec. Text an upstream server
   writes, in tool output or in its own JSON-RPC errors, passes through as
   untrusted content, cleaned for invisible characters as today. It is not a
   Gobby disclosure, and no redaction layer is added.
4. **OAuth and dynamic client registration stay in Python.** The interactive
   login (`gobby mcp auth`, `authorize_server`) keeps using the Python MCP SDK
   on the operator's machine. Storage, keepalive refresh, advisory locks and
   the callback server do not move. For each request Python puts the current
   access token in the spec's `Authorization` header. On HTTP 401 the pool
   returns a typed `unauthorized` error carrying `WWW-Authenticate`. Python
   then reloads a rotated token or refreshes under the existing lock, and
   retries once. Without a usable token it raises the existing
   `MCPAuthorizationRequired`.
   - Rejected: rmcp's `auth` feature. It would duplicate the encrypted store,
     the advisory locks, the refusal policy for registration and the refresh
     backoff that `oauth.py` and `oauth_keepalive.py` already own.
   - gmcp-stdio.md Decision 3 (CIMD only) governs Gobby's own authorization
     server (#24360), not this client.
5. **Placement uses no new schema column.** A stdio server runs on the machine
   of the session in `get_current_session_id()`. With no session, it runs on
   the local machine. URL servers always run on the hub's pool.
   `resolve_target_machine` resolves the session's machine and refuses with
   its existing codes (`node_offline`, `checkout_unresolved` and the others).
   The hub resolves secrets and OAuth tokens and sends the resolved spec in
   the command. The node holds it only in memory.
   - `McpBackend` keeps an in-memory holder set, server id to the machine ids
     it has sent `mcp.request` for that server. Removing, disabling or
     re-configuring a server, `disconnect_server` and `disconnect_all` send
     `mcp.disconnect` to every holder and disconnect the local pool.
     `get_health_report` merges local status with `mcp.status` from every
     holder.
   - An offline holder does not block the change. The row is still removed
     or updated, the result lists each failed holder with its routing code,
     the failure is logged with the machine id, and the holder stays in the
     set for the next invalidation. The node's idle close (Decision 6) is
     the bound on an orphaned connection, and the fingerprint guarantees the
     next request uses the fresh spec. A hub restart loses the holder set,
     with the same fallback.
   - Rejected: a per-server machine column. The caller's machine already
     answers where a local tool must run, and a column would pin one server
     to one machine for every session.
   - Rejected: running every subprocess on the hub. F5.3 forbids treating that
     as parity.
6. **Pool semantics.** A connection is keyed by server id. Its fingerprint is
   the SHA-256 of the canonical spec JSON, and a changed fingerprint (edited
   config or rotated token) replaces the connection. Requests on one
   connection run concurrently under JSON-RPC ids. Connect attempts keep
   today's policy: up to three retries at 1, 2 and 4 seconds. Three
   consecutive failed connects open a 30-second circuit with one half-open
   probe. An idle connection closes after 300 seconds. A connection found
   closed before a request is sent reconnects once. A transport failure after
   the request is written returns `transport_closed` and never replays,
   because a replay could repeat a mutation. Periodic health pings are
   dropped; the pool observes transport closure directly.
7. **Every stdio server runs under `gdaemon mcp-child`.** The pool spawns the
   wrapper as a process-group leader. The wrapper runs the server in its own
   group with inherited stdio and polls `getppid()` once a second. When
   gdaemon dies, the wrapper signals the group with SIGTERM, waits 5 seconds,
   then sends SIGKILL. Disconnect, idle close and `Pool::shutdown` signal the
   group the same way. This replaces the Python psutil supervisor for MCP, and
   `src/gobby/utils/child_supervisor.py` stays for its ACP user. Windows
   compiles with process-wrap's `JobObject`, and a Windows Rust CI job proves
   that compile. Its runtime proof belongs to the 1.0 Windows work, per Josh's
   ruling.
   - Rejected: supervising only browser hooks, as Python does. An npx launch is
     already a launcher plus a child, so one rule for every server is simpler
     and closes the same orphan path.
8. **Cancellation cancels the backend call, never a Python task.** Every pool
   request carries a `call_id`. Cancelling it sends `notifications/cancelled`
   upstream, and the pending request answers `cancelled`. gmcp sends its own
   call id in `x-gobby-call-id` and, when its client cancels, posts
   `/api/mcp/tools/cancel`. Python maps that id to the in-flight backend call
   and cancels it. `McpBackend` raises the typed `MCPCallCancelled`, and
   `classify_error` maps it to the new `ToolProxyErrorCode.CANCELLED`, so the
   original request finishes through the normal error path.
   - Either message can arrive first. Both registries, the Python one keyed
     by (caller session, client call id) and the pool's keyed by `call_id`,
     use one rule. A cancel for an unknown id leaves a tombstone that expires
     after 600 seconds, the longest a call may live. Each registry keeps at
     most 4,096 tombstones and evicts the oldest first. A call that finds its
     tombstone answers `CANCELLED` without contacting the upstream server.
   - The pool registers a `call_id` on entry, before connect and retry, and
     checks cancellation again immediately before the upstream write.
     Completion removes the entry. A late or duplicate cancel is a no-op.
   - `call_mcp_tool` checks the tombstone before dispatch, so a call
     cancelled before it starts never runs, internal or external. A call
     already running is cancelled only when it is external. An internal tool
     is never interrupted, because asyncio cancellation between its writes
     could leave partial state. gmcp discards its result.
   - Rejected: relying on HTTP disconnects. gmcp's blocking ureq call cannot
     close its socket, and a disconnect through the node relay reaches the hub
     unreliably.
9. **Timeouts.** `McpBackend` fixes an absolute deadline when a call starts.
   Routing, the `unauthorized` retry and connect all spend that one budget,
   and a call whose budget is gone answers `timeout`. Each transport hop gets
   a 5,000 ms grace so the inner result arrives before the outer wait ends:
   - The pool's deadline is the remaining budget sent as
     `McpRequest.timeout_ms`. At that deadline the pool cancels upstream and
     answers `timeout`, which Python maps to its existing timeout result.
   - On the local route the HTTP read timeout is the inner budget plus the
     grace.
   - A node-placed call's outer `NodeCommandClient.send` timeout is
     `min(remaining + 5,000, 600,000)` ms, the internal command route's
     ceiling, and its inner `timeout_ms` is that value minus 5,000. A
     requested 600,000 ms becomes 595,000 ms inner.
   - When the outer wait still ends in `node_timeout` or a channel error,
     `McpBackend` sends a best-effort `mcp.cancel` for the `call_id` and
     never replays the call. The inner deadline bounds the node-side call
     either way.
10. **Node commands take the next contract version after #25207.**
   remote-terminal-attach 1.1 (#25207, open and unblocked) takes version 5.
   `mcp.request`, `mcp.cancel`, `mcp.disconnect` and `mcp.status` take
   version 6.
   - Rejected: claiming version 5. #25207 is already expanded with version 5
     test names.

## Constraints
`kind: framing`

- Evidence was observed at HEAD `babdb0d558df4d9dbaa7b553bab18f238f5a3b1e` on
  2026-10-28.
- Literal sweeps:
  - `rg -l "mcp_proxy\.transports|mcp_proxy\.lazy|connection_cleanup|client_manager\.(connections|health|invocation)|child_supervisor" src tests`
    lists the transport importers.
  - `rg -n "get_client_session\(|ensure_connected\(|\.connections\b|get_lazy_connection_states|get_server_health|health_check_all" src`
    finds one production caller outside `mcp_proxy`:
    `servers/routes/mcp/endpoints/discovery.py:180`.
  - The `connections` fallbacks in `registry.py`, `server.py` and
    `services/_manager_compat.py` are dead once `is_connected` is always
    present.
  - The websocket sweep `rg -n "SUPPORTED_TRANSPORTS|\"websocket\"" src web/src`
    hits `transport_types.py`, `models.py`, `stdio_tools.py`,
    `cli/mcp_proxy.py` and `McpServerFields.tsx`.
  - `rg -n "mcp_proxy\.bundled" src tests` shows `transports/stdio.py` is the
    only production user of `bundled.py`.
  - `rg -l "get_client_session|ensure_connected|_connect_server|get_lazy_connection_states|health_check_all|get_server_health|_monitor_health|\.connections\b" tests/mcp_proxy tests/servers tests/hooks tests/telemetry tests/workflows`
    lists the tests of the removed manager API. Files that only assign those
    names on a `MagicMock` or define them on a fake that production no longer
    calls keep passing and stay out of Targets.
- Live registry: seven stdio servers and one OAuth HTTP server (fieldy). All
  eight bundled templates are stdio. No websocket or SSE server is installed.
- Shipping rule: every Rust leaf that changes `gdaemon` or `gmcp` bumps that
  crate by `0.0.1` in its own commit and updates `Cargo.lock` and
  `version_pins.py`. The leaves form one dependency chain for that reason. The
  Orchestrator announces and performs every cutover; no leaf copies binaries.
- No production file may reach 1,000 lines.
- Rust work loads the `rust` skill. Python runs through `uv run`. Tests use the
  isolated database and `GOBBY_TEST_PROTECT=1`.

## Cross-plan edges
`kind: framing`

| Leaf | External work | Edge | Reason |
| --- | --- | --- | --- |
| 3.1 | #25207 (remote-terminal-attach 1.1) | blocked by | It takes contract version 5 and moves the terminal types out of `node_commands.rs`. |
| 3.2 | #24376 (node-halves 2.7) | blocked by | It creates the node command dispatcher that 3.2 extends. |
| none | #24884 (rust-internal-mcp-servers 2.1) | relation | It already waits on #21566 and calls `Pool` directly. |
| none | #24359, #24360 (gmcp-stdio) | relation | Packaging and server-side OAuth keep their scope. |

**Granularity:** Ten deliverables, one per independently testable outcome. The
Rust pool (1.1), the process supervisor (1.2) and the URL transports (1.3) are
three separate lifecycle owners. OAuth helpers (2.1) are testable before
delegation (2.2). Cancellation (2.3) spans gmcp, Python and the pool as one
behavior. The contract (3.1), node handler (3.2) and hub placement (3.3) are
separate owners.

## P1: Rust multiplexer in gdaemon
`kind: framing`

### 1.1 Connection pool, stdio transport and loopback route [category: code]
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/lib.rs::*` — scope-reason: export the new mcp_client module
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/wire.rs`
- `crates/gdaemon/src/mcp_client/stdio.rs`
- `crates/gdaemon/src/mcp_client/internal_api.rs`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoorState`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoorState::new`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: the in-file test helper builds FrontDoorState and must name the new pool field
- `crates/gdaemon/src/serve.rs::*` — scope-reason: share one pool across listeners in serve_with_runtime and await Pool::shutdown when serving ends
- `crates/gdaemon/tests/common/channel.rs::*` — scope-reason: the FrontDoorState literal names the new pool field
- `crates/gdaemon/tests/support/mcp_fixture.rs`
- `crates/gdaemon/tests/mcp_client.rs`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gdaemon/src/nodes/internal_api.rs::handle` is the loopback
  precedent. It admits only a loopback peer carrying `x-gobby-backend-secret`,
  answers 404 otherwise, limits bodies to 2 MiB, and runs only under
  `NativeRoutes::Hub`. `FrontDoor::handle` routes it after the backend-refusal
  check, and `Door::Node` answers it with 404.
- Observed: rmcp 3.5.1 provides `client` and `transport-child-process`
  (process-wrap 10 with default wrappers). gdaemon has no rmcp dependency yet.
- Observed: `src/gobby/mcp_proxy/lazy.py::CircuitBreaker` (3 failures, 30 s,
  one half-open call), `RetryConfig` (3 retries, 1 s doubling, 16 s cap) and
  `client_manager/health.py::LAZY_IDLE_TIMEOUT_SECONDS` (300 s) define the
  policy that Decision 6 ports.
- Chosen approach:
  - `wire.rs` defines the serde types shared by the route and, later, node
    commands:
    - `ServerSpec { id, transport, command, args, env, runtime_hook, url,
      headers, connect_timeout_ms }`
    - `McpRequest { call_id, op, server, tool, arguments, uri, timeout_ms }`,
      where `op` is `list_tools`, `call_tool` or `read_resource`
    - `McpReply`, either `{ok: true, result, connected}` or
      `{ok: false, error: {code, message, ...}}`
    - Error codes are `invalid_request`, `connect_failed`, `circuit_open`
      (`recovery_in_ms`), `unauthorized` (`www_authenticate`), `timeout`,
      `cancelled`, `transport_closed`, `server_error` (`rpc_code`) and
      `result_too_large`.
  - `mod.rs` owns `Pool`, which implements Decision 6, the `call_id` registry
    with its tombstones (Decision 8) and `Pool::shutdown`. Requests use rmcp's
    `send_cancellable_request`, and a cancel or deadline calls the handle's
    `cancel`.
  - The public Rust surface, which #24884 consumes without HTTP, is
    `Pool::new`, `Pool::request(McpRequest) -> McpReply`,
    `Pool::cancel(call_id) -> bool`, `Pool::disconnect`, `Pool::status` and
    `Pool::shutdown`, plus the `wire` types. `Pool` stays one concrete type,
    and the route is a thin adapter over it.
  - `ServerSpec` implements `Debug` by hand (Decision 3). `Pool::status`
    reports each connection's id, transport, connected flag and in-flight
    count, never spec values.
  - Pool policy (Decision 6), stated here for the leaf:
    - A connection is keyed by server id. Its fingerprint is the SHA-256 of
      the canonical spec JSON, and a changed fingerprint replaces the
      connection.
    - Requests on one connection run concurrently under JSON-RPC ids.
    - A connect retries up to three times at 1, 2 and 4 seconds. Three
      consecutive failed connects open a 30-second circuit with one
      half-open probe.
    - An idle connection closes after 300 seconds. There are no health
      pings.
    - A connection found closed before a request is sent reconnects once. A
      transport failure after the request is written answers
      `transport_closed` and never replays.
  - Call registry (Decision 8), stated here for the leaf:
    - A request registers its `call_id` on entry, before connect and retry,
      and checks cancellation again immediately before the upstream write.
      Completion removes the entry.
    - A cancel for an unknown `call_id` leaves a tombstone that expires after
      600 seconds. At most 4,096 tombstones are kept, oldest evicted first. A
      request that finds its tombstone answers `cancelled` without
      contacting the server. A late or duplicate cancel is a no-op.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
  - `stdio.rs` launches the spec's command, args and env as given; 1.2 adds the
    launch environment.
  - `internal_api.rs` serves `POST /api/internal/mcp/request`,
    `POST /api/internal/mcp/cancel` (`{call_id}` returns `{cancelled}`),
    `POST /api/internal/mcp/disconnect` (`{id}` or `{all: true}`) and
    `GET /api/internal/mcp/status`. It reuses the loopback and secret
    admission. It answers in Standalone and Hub, limits bodies to 32 MiB, and
    is 404 on the Node door.
  - `FrontDoorState` gains `mcp_clients: Arc<mcp_client::Pool>`, built by
    `FrontDoorState::new`.
  - The fixture binary `gdaemon-mcp-fixture` (`[[bin]]` with
    `required-features = ["mcp-fixture"]`, never built in release) is an rmcp
    stdio server. Its tools are `echo`, `whoami` (pid, ppid and `GOBBY_HOME`),
    `getenv`, `sleep` (records receipt of `notifications/cancelled` to a
    marker file), `big` and `spawn_grandchild`.
- Planned verification:
  `cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_client`
  and `cargo fmt -p gobby-daemon -- --check`.

**Granularity:** One lifecycle owner, the pool. Its route, wire types and
fixture are the minimum needed to test it; the acceptance items cover its
transitions and its direct Rust surface.

**Acceptance:**

- 1.1.1 - `list_tools`, `call_tool` and `read_resource` reach a stdio fixture through `POST /api/internal/mcp/request`, and concurrent calls on one connection all complete. test: `crates/gdaemon/tests/mcp_client.rs::stdio_round_trip_and_concurrent_calls`.
- 1.1.2 - A request whose spec fingerprint changed replaces the pooled connection, and an idle connection closes after the idle limit (test clock). test: `crates/gdaemon/tests/mcp_client.rs::fingerprint_change_and_idle_close_replace_connection`.
- 1.1.3 - Three failed connects answer `circuit_open` with `recovery_in_ms` until the window passes, then one half-open attempt runs. test: `crates/gdaemon/tests/mcp_client.rs::circuit_opens_after_three_failed_connects`.
- 1.1.4 - A deadline sends `notifications/cancelled` upstream and answers `timeout`; `POST /api/internal/mcp/cancel` does the same and answers the pending request `cancelled`. test: `crates/gdaemon/tests/mcp_client.rs::deadline_and_cancel_notify_upstream`.
- 1.1.5 - A connection found closed before sending reconnects once; a transport failure after sending answers `transport_closed` and the fixture records one call. test: `crates/gdaemon/tests/mcp_client.rs::no_replay_after_send`.
- 1.1.6 - Non-loopback peers, a wrong or missing secret, and every request on the Node door get 404 before the body is read. test: `crates/gdaemon/tests/mcp_client.rs::route_admits_only_loopback_backend_secret`.
- 1.1.7 - `Pool::shutdown` leaves no fixture process alive. test: `crates/gdaemon/tests/mcp_client.rs::shutdown_reaps_every_server`.
- 1.1.8 - The contract documents the public `Pool` API, the route, wire types, error codes and pool policy. behavior: "Pool::request" in `docs/contracts/mcp-multiplexer.md`.
- 1.1.9 - An integration test drives `Pool::request` and `Pool::cancel` against the stdio fixture with no HTTP route, as #24884 will. test: `crates/gdaemon/tests/mcp_client.rs::pool_api_serves_without_route`.
- 1.1.10 - With deterministic barriers, a cancel that arrives before its request and a cancel during connect backoff each answer the request `cancelled` with zero fixture calls; a cancel after completion and a duplicate cancel change nothing; tombstones expire and stay within their bound. test: `crates/gdaemon/tests/mcp_client.rs::cancel_orderings_never_reach_upstream`.

### 1.2 Stdio launch environment and orphan-proof supervision [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/main.rs::*` — scope-reason: add the hidden mcp-child subcommand to Command and its dispatch
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/stdio.rs`
- `crates/gdaemon/src/mcp_client/launch_env.rs`
- `crates/gdaemon/src/mcp_client/supervise.rs`
- `crates/gdaemon/assets/browser-loopback.json`
- `crates/gdaemon/tests/mcp_client_stdio.rs`
- `docs/contracts/mcp-multiplexer.md`
- `.github/workflows/rust-ci.yml`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `.github/workflows/rust-ci.yml` cross-lints only `gobby-terminal`
  for `x86_64-pc-windows-msvc`. `gobby-daemon` pulls `openssl-sys` and `ring`
  for that target, so a cross lint from the Linux runner cannot build it.
  `release-gdaemon.yml` builds it natively on `windows-latest`, but only on a
  release tag.
- Observed: `src/gobby/mcp_proxy/transports/stdio.py::StdioTransportConnection._open_transport`
  does four things on the executing machine:
  - runs `bundled.py::resolve_runtime_stdio_args`;
  - expands `${VAR}` and `${VAR:-default}` with
    `utils/env.py::expand_env_variables`, where an empty value counts as unset
    and an unset variable without a default stays literal;
  - sets `npm_config_prefer_offline=true` when the command is `npx`;
  - wraps browser hooks in `utils/child_supervisor.py::supervised_argv`.

  It writes stderr to the configured errlog.
- Observed: `bundled.py` has two runtime hooks:
  - `chrome_executable_path` injects the Chrome path found by
    `resolve_chrome_devtools_executable_path`, from per-OS candidates and the
    ms-playwright cache globs;
  - `loopback_browser` adds that, and for `@playwright/mcp@` args also replaces
    `--config` with `src/gobby/install/shared/mcp/browser-loopback.json`, a
    path that does not exist on a node without the Python package.
- Observed: gdaemon has no parent-death precedent.
  `crates/gdaemon/src/lifecycle/backend.rs::BackendChild` is the
  process-group signalling precedent.
- Chosen approach:
  - `launch_env.rs` ports both hooks with every candidate list, the `${VAR}`
    rules and npx offline. It embeds the loopback config with `include_str!`
    and writes it to `<gobby_home>/mcp/browser-loopback.json` before launch.
  - `supervise.rs` implements `gdaemon mcp-child -- <argv>` (Decision 7), and
    `stdio.rs` runs every server through it, using `current_exe()`:
    - The pool spawns the wrapper as a process-group leader. The wrapper
      runs the server in its own process group with inherited stdio.
    - The wrapper polls `getppid()` once a second. When the parent changes,
      it sends SIGTERM to the server's group, waits 5 seconds, then sends
      SIGKILL.
    - Disconnect, idle close and `Pool::shutdown` signal the group the same
      way.
    - On Windows the server runs in process-wrap's `JobObject`. That path
      must compile; its runtime proof belongs to the 1.0 Windows work.
  - Stderr appends to `<gobby_home>/logs/mcp-servers.log`.
  - `rust-ci.yml` gains a `windows-latest` job, "Clippy (gobby-daemon,
    Windows)", running
    `cargo clippy -p gobby-daemon --target x86_64-pc-windows-msvc -- -D warnings`.
    It is compile-only; the runtime orphan proof stays deferred.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_client_stdio`
  and the new Windows job passing on the leaf's pushed commit.

**Acceptance:**

- 1.2.1 - `${VAR}`, `${VAR:-default}`, empty and unset variables expand exactly as `expand_env_variables` does, and `npx` launches get `npm_config_prefer_offline=true`. test: `crates/gdaemon/tests/mcp_client_stdio.rs::launch_env_matches_python_rules`.
- 1.2.2 - `chrome_executable_path` and `loopback_browser` produce the Python hook's argv for the same candidate tree, with `--config` pointing at the written loopback file. test: `crates/gdaemon/tests/mcp_client_stdio.rs::runtime_hooks_match_python_argv`.
- 1.2.3 - SIGKILL of the process that owns the pool leaves no server or grandchild alive within 10 seconds. test: `crates/gdaemon/tests/mcp_client_stdio.rs::owner_death_reaps_server_group`.
- 1.2.4 - Disconnect and idle close terminate the whole group, grandchild included. test: `crates/gdaemon/tests/mcp_client_stdio.rs::disconnect_reaps_grandchildren`.
- 1.2.5 - Server stderr lands in the MCP server log. test: `crates/gdaemon/tests/mcp_client_stdio.rs::stderr_reaches_server_log`.
- 1.2.6 - Rust CI type-checks gdaemon, including the `JobObject` launch path, for `x86_64-pc-windows-msvc` on a Windows runner. behavior: "Clippy (gobby-daemon, Windows)" in `.github/workflows/rust-ci.yml`.

### 1.3 Streamable HTTP and SSE transports [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/http.rs`
- `crates/gdaemon/src/mcp_client/sse.rs`
- `crates/gdaemon/tests/mcp_client_http.rs`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `src/gobby/mcp_proxy/transports/http.py::build_mcp_http_client`
  sends configured headers with a 30 s timeout and a 300 s read timeout.
  `transports/sse.py` reuses the HTTP connection with the SDK's `sse_client`.
- Observed: rmcp 3.5.1 has `transport-streamable-http-client-reqwest` and
  `client-side-sse`, but no legacy SSE client.
- Chosen approach:
  - `http.rs` uses rmcp's streamable HTTP client with the spec's headers and
    the same timeouts.
  - `sse.rs` implements the legacy protocol as an rmcp `Transport`: GET the
    stream, take the `endpoint` event, and POST messages there.
  - A 401 from either transport, at connect or on a request, answers
    `unauthorized` with the `WWW-Authenticate` value and drops the connection.
  - Tests serve an rmcp streamable-HTTP server and an in-process legacy SSE
    fixture from axum.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_client_http`.

**Acceptance:**

- 1.3.1 - Streamable HTTP lists and calls tools with the spec's headers. test: `crates/gdaemon/tests/mcp_client_http.rs::streamable_http_round_trip_sends_headers`.
- 1.3.2 - Legacy SSE lists and calls tools through the advertised endpoint. test: `crates/gdaemon/tests/mcp_client_http.rs::legacy_sse_round_trip`.
- 1.3.3 - A 401 answers `unauthorized` with the server's `WWW-Authenticate`, and the next request with a new bearer reconnects. test: `crates/gdaemon/tests/mcp_client_http.rs::unauthorized_is_typed_and_new_bearer_reconnects`.
- 1.3.4 - Delivery: the HTTP fixture records the sentinel bearer and the stdio fixture's `getenv` returns the sentinel env value, the one reply allowed to contain it. Non-disclosure: through non-echoing operations (`whoami`, a 401, a failed connect) the sentinels appear in no captured gdaemon log line, `Pool::status` or status reply, gdaemon-authored error, reply envelope field or `ServerSpec` `Debug` output. test: `crates/gdaemon/tests/mcp_client_http.rs::spec_secrets_stay_out_of_status_errors_and_logs`.

## P2: Python delegation
`kind: framing`

### 2.1 OAuth bearer handoff helpers [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/oauth.py::*` — scope-reason: add bearer_for and recover_unauthorized, and move build_mcp_http_client and gobby_client_info here for the CLI login flow
- `tests/mcp_proxy/test_oauth.py::*` — scope-reason: cover the bearer helpers and the moved login helpers

**Research context:**
- Observed: `oauth.py::_authorize_loaded_server` drives login with the Python
  MCP SDK `Client` over `sse_client` or `streamable_http_client`, using
  `transports/http.py::build_mcp_http_client` and
  `transports/base.py::gobby_client_info`. It needs no daemon transport.
  `automatic_oauth_authorization` always raises `MCPAuthorizationRequired`.
- Observed: `PersistentOAuthProvider` exposes `refresh_persisted_token`
  (token endpoint only), `_reload_rotated_token`, and refresh locking through
  `oauth_keepalive.py::oauth_server_lock`. Keepalive already refreshes
  600 seconds before expiry.
- Chosen approach:
  - `bearer_for(config, store)` returns the stored access token. It raises
    `MCPAuthorizationRequired(oauth_auth_command(config))` when none is
    stored.
  - `recover_unauthorized(config, store, sent_token)` returns a usable token.
    It first reloads storage and returns a rotated token that differs from
    `sent_token`. Otherwise it refreshes under the server lock. It raises
    `MCPAuthorizationRequired` when refresh fails or needs consent.
  - `build_mcp_http_client` and `gobby_client_info` move into `oauth.py`,
    because 2.2 deletes `transports/`.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_oauth.py -v`.

**Acceptance:**

- 2.1.1 - `bearer_for` returns the stored token and raises `MCPAuthorizationRequired` with the auth command when none exists. test: `tests/mcp_proxy/test_oauth.py::test_bearer_for_requires_stored_token`.
- 2.1.2 - `recover_unauthorized` returns a rotated token without calling the token endpoint, refreshes once under the server lock otherwise, and raises `MCPAuthorizationRequired` when refresh fails. test: `tests/mcp_proxy/test_oauth.py::test_recover_unauthorized_reload_then_refresh`.
- 2.1.3 - The CLI login still completes against a stub server with the moved helpers. test: `tests/mcp_proxy/test_oauth.py::test_authorize_server_uses_local_http_client`.

### 2.2 MCPClientManager delegates transport to gdaemon [category: code] (depends: 1.3, 2.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/client_manager/backend.py`
- `src/gobby/mcp_proxy/client_manager/wire.py`
- `src/gobby/mcp_proxy/manager.py::*` — scope-reason: replace transport state with the backend client and keep registry, inventory, browser policy and untrusted-value cleaning
- `src/gobby/mcp_proxy/client_manager/server_registry.py::*` — scope-reason: drop transport connections and lazy-connector bookkeeping from add, remove, update and refresh
- `src/gobby/mcp_proxy/client_manager/tool_inventory.py::*` — scope-reason: list tools through the backend client
- `src/gobby/mcp_proxy/client_manager/connections.py::*` — operation: delete — scope-reason: the gdaemon pool owns connections
- `src/gobby/mcp_proxy/client_manager/health.py::*` — operation: delete — scope-reason: the gdaemon pool owns idle close and health
- `src/gobby/mcp_proxy/client_manager/invocation.py::*` — operation: delete — scope-reason: the manager calls the backend directly
- `src/gobby/mcp_proxy/lazy.py::*` — operation: delete — scope-reason: the gdaemon pool owns retry and the circuit
- `src/gobby/mcp_proxy/connection_cleanup.py::*` — operation: delete — scope-reason: no Python transport remains to clean up
- `src/gobby/mcp_proxy/bundled.py::*` — operation: delete — scope-reason: 1.2 ported the runtime hooks to gdaemon
- `src/gobby/mcp_proxy/transports/__init__.py`
- `src/gobby/mcp_proxy/transports/base.py::*` — operation: delete — scope-reason: retire the Python transport layer
- `src/gobby/mcp_proxy/transports/factory.py::*` — operation: delete — scope-reason: retire the Python transport layer
- `src/gobby/mcp_proxy/transports/http.py::*` — operation: delete — scope-reason: retire the Python transport layer
- `src/gobby/mcp_proxy/transports/sse.py::*` — operation: delete — scope-reason: retire the Python transport layer
- `src/gobby/mcp_proxy/transports/stdio.py::*` — operation: delete — scope-reason: retire the Python transport layer
- `src/gobby/mcp_proxy/transports/websocket.py::*` — operation: delete — scope-reason: Decision 2 removes the websocket transport
- `src/gobby/install/shared/mcp/browser-loopback.json::*` — operation: delete — scope-reason: 1.2 embeds the loopback config in gdaemon
- `src/gobby/mcp_proxy/transport_types.py::*` — scope-reason: drop websocket from SUPPORTED_TRANSPORTS
- `src/gobby/mcp_proxy/models.py::*` — scope-reason: drop websocket validation and its URL schemes
- `src/gobby/mcp_proxy/stdio_tools.py::*` — scope-reason: drop websocket from the add_mcp_server transport description
- `src/gobby/cli/mcp_proxy.py::*` — scope-reason: move the add command out and register it beside the template commands
- `src/gobby/cli/mcp_proxy_add.py`
- `web/src/components/activity/mcp/McpServerFields.tsx::*` — scope-reason: drop the WebSocket transport option
- `src/gobby/runner_init/mcp_stack.py::*` — scope-reason: construct the manager without transport-tuning arguments
- `src/gobby/runner_init/services.py::*` — scope-reason: construct the manager without transport-tuning arguments
- `src/gobby/servers/routes/mcp/endpoints/discovery.py::_list_external_server_tools`
- `src/gobby/servers/routes/mcp/endpoints/registry.py::_mcp_manager_is_connected`
- `src/gobby/servers/routes/mcp/endpoints/server.py::*` — scope-reason: drop the CircuitBreakerOpen import and the dead connections fallback
- `src/gobby/mcp_proxy/services/_manager_compat.py::*` — scope-reason: drop the dead connections fallbacks
- `docs/guides/mcp-oauth.md`
- `tests/mcp_proxy/test_backend_client.py`
- `tests/cli/test_cli_mcp_proxy.py::*` — scope-reason: patch helpers through the module that now owns the add command
- `tests/mcp_proxy/transports/_support.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_client_mode.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_http_transport.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_mcp_proxy_transports_base.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_negotiation.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_sse_transport.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_stdio.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_stdio_transport.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_websocket_transport.py::*` — operation: delete — scope-reason: the transport tests retire with the transports
- `tests/mcp_proxy/transports/test_stdio_browser_shutdown.py::*` — operation: delete — scope-reason: 1.2 owns the orphan-reaping tests in Rust
- `tests/mcp_proxy/test_oauth.py::*` — scope-reason: drop the transport-connection OAuth tests that retire with the transports
- `tests/mcp_proxy/test_semantic_unicode.py::*` — scope-reason: fake the backend client instead of patching get_client_session and ensure_connected
- `tests/hooks/test_python_pipeline_classifier.py::*` — scope-reason: smoke the openapi server through manager calls instead of get_client_session
- `tests/mcp_proxy/test_lazy.py::*` — operation: delete — scope-reason: the lazy connector retires
- `tests/mcp_proxy/test_lazy_idle_health.py::*` — operation: delete — scope-reason: idle close moved to gdaemon
- `tests/mcp_proxy/test_connection_lock_convoy.py::*` — operation: delete — scope-reason: connection locks retire
- `tests/mcp_proxy/test_manager_disconnect_cancellation.py::*` — operation: delete — scope-reason: transport disconnects moved to gdaemon
- `tests/mcp_proxy/test_manager_stale_sessions.py::*` — operation: delete — scope-reason: stale-session reconnects moved to gdaemon
- `tests/mcp_proxy/test_bundled_runtime_hooks.py::*` — operation: delete — scope-reason: 1.2 owns the hook tests in Rust
- `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py::*` — scope-reason: drive the openapi server through the backend client
- `tests/mcp_proxy/test_manager_coverage.py::*` — scope-reason: assert backend delegation instead of transport internals
- `tests/mcp_proxy/test_mcp_manager.py::*` — scope-reason: assert backend delegation instead of transport internals
- `tests/mcp_proxy/test_discovery_deadlines.py::*` — scope-reason: fake the backend client instead of BaseTransportConnection
- `tests/mcp_proxy/test_oauth_setup.py::*` — scope-reason: patch the moved login helpers
- `tests/mcp_proxy/test_server_registry.py::*` — scope-reason: drop lazy-connector assertions
- `tests/mcp_proxy/test_server_mgmt.py::*` — scope-reason: drop lazy-connection-state assertions
- `tests/mcp_proxy/test_tool_inventory.py::*` — scope-reason: fake the backend client instead of client sessions
- `tests/mcp_proxy/test_unicode_gaps.py::*` — scope-reason: fake backend replies instead of client sessions
- `tests/mcp_proxy/test_server_coverage.py::*` — scope-reason: replace the server-health fake with the backend status
- `tests/mcp_proxy/test_browser_policy.py::*` — scope-reason: enforce browser policy at the manager boundary
- `tests/mcp_proxy/test_mcp_templates.py::*` — scope-reason: drop the bundled-module assertion
- `tests/mcp_proxy/test_sync_templates.py::*` — scope-reason: drop runtime-hook argv assertions now owned by 1.2
- `tests/servers/test_mcp_routes.py::*` — scope-reason: replace CircuitBreakerOpen and connection fakes with backend errors
- `tests/servers/routes/mcp_endpoints/test_discovery_routes.py::*` — scope-reason: discovery lists tools through the manager instead of ensure_connected
- `tests/servers/routes/mcp_endpoints/test_registry_routes.py::*` — scope-reason: fake is_connected instead of connections
- `tests/servers/routes/test_admin.py::*` — scope-reason: drop connections fakes
- `tests/servers/test_http_server.py::*` — scope-reason: drop connections fakes
- `tests/servers/test_http_endpoints.py::*` — scope-reason: drop connections fakes
- `tests/servers/routes/test_browser_mcp_lockdown.py::*` — scope-reason: probe for Chrome without the deleted bundled module
- `tests/telemetry/test_flow_instrumentation.py::*` — scope-reason: instrument the backend call instead of ensure_connected
- `tests/workflows/test_user_template_sync.py::*` — scope-reason: patch the backend client instead of the connect retry helper
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: call through the manager instead of get_client_session

**Research context:**
- Observed: `src/gobby/mcp_proxy/manager.py::MCPClientManager` (363 lines) is a
  facade over `client_manager/`.
  - `call_tool` runs `invocation.call_tool`, which handles browser policy,
    tracing, metrics and the dead-connection retry, then
    `clean_untrusted_value`.
  - Production callers outside `mcp_proxy` use `call_tool`, `list_tools`,
    `read_resource`, `get_tool_info`, `cache_discovered_tools`,
    `is_connected`, `disconnect_server`, `disconnect_all` and `connect_all`.
    The single exception is `discovery.py::_list_external_server_tools`,
    which calls `ensure_connected` and then `session.list_tools()`.
- Observed: `src/gobby/hub/node_client.py::NodeCommandClient._http_client`
  builds the loopback client from `load_bootstrap().daemon_port` and
  `front_door_secret()`. The backend client reuses that pattern.
- Chosen approach:
  - `wire.py` holds the Pydantic mirror of 1.1's wire types.
  - `backend.py` owns `McpBackend`:
    - It builds a `ServerSpec` from the secret-resolved config and adds the
      bearer from 2.1's `bearer_for` when `requires_oauth` is set.
    - It fixes the call's absolute deadline at entry (Decision 9). The
      `unauthorized` retry spends the same budget, each backend request
      carries the remaining budget as `timeout_ms`, and the HTTP read timeout
      is that budget plus 5 seconds.
    - It maps error codes onto the manager's existing exceptions and result
      shapes. On `unauthorized` it calls `recover_unauthorized` and retries
      once.
    - It records each reply's `connected` flag for `is_connected`.
    - On `asyncio.CancelledError` it sends `cancel` for its `call_id` as a
      shielded task, then re-raises.
  - The manager keeps its public method names except the removed
    connection-level ones: `connections`, `get_client`,
    `get_client_session`, `ensure_connected`, `_connect_server`,
    `get_lazy_connection_states`, `health_check_all`, `get_server_health`,
    `_monitor_health` and `_reconnect`.
  - The kept lifecycle methods map onto 1.1's local routes with no second
    lifecycle layer. 3.3 extends the same methods to node holders:
    - `remove_server`, `set_server_enabled` with `enabled=False` and a
      config-changing `update_server` post `disconnect {id}` at once, so the
      old process or connection does not wait for idle close.
    - `disconnect_server` posts `disconnect {id}`, and `disconnect_all`
      posts `disconnect {all: true}`.
    - `is_connected` reads the last reply's `connected` flag and turns false
      after a disconnect.
    - `connect_all` registers the configs and warms each
      `preconnect_servers` entry with one `list_tools`.
    - `get_health_report` reads `GET /api/internal/mcp/status`.
  - `McpBackend` logs and raised errors carry the server id, error code and
    backend message, never resolved header or env values (Decision 3).
  - `_list_external_server_tools` calls `mcp_manager.list_tools(server_name)`
    within its existing deadline.
  - `enforce_browser_call` runs in `MCPClientManager.call_tool` before
    dispatch.
- Split: `src/gobby/cli/mcp_proxy.py` is 877 lines and every websocket
  mention sits in `add_server` (lines 334-469). Move `add_server` into the new
  `src/gobby/cli/mcp_proxy_add.py` without websocket in its transport choices
  or URL check, and have `register_template_commands` import it beside the
  template and OAuth commands.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`
  over every test file in Targets, `uv run ruff check src/`,
  `uv run mypy src/` and the web typecheck.

**Granularity:** Seventeen production files are edited or created and fourteen
are deleted, but they form one replacement: the transport layer and every
consumer of its removed API. The acceptance items are the behaviors of that
one seam. Splitting the deletion from delegation would leave a commit
where external servers do not work.

**Acceptance:**

- 2.2.1 - Stdio and HTTP servers list and call tools through `McpBackend` against a stub backend route, with results cleaned of untrusted characters. test: `tests/mcp_proxy/test_backend_client.py::test_manager_delegates_list_call_and_read`.
- 2.2.2 - An `unauthorized` reply triggers `recover_unauthorized` and one retry with the new bearer; a second `unauthorized` raises `MCPAuthorizationRequired`. test: `tests/mcp_proxy/test_backend_client.py::test_unauthorized_recovers_once`.
- 2.2.3 - `timeout`, `circuit_open` and `transport_closed` map to the existing timeout result and `MCPError` messages, and an `unauthorized` retry receives only the budget the first attempt left. test: `tests/mcp_proxy/test_backend_client.py::test_error_codes_map_to_existing_shapes`.
- 2.2.4 - Cancelling the awaiting coroutine sends a backend `cancel` for its `call_id`. test: `tests/mcp_proxy/test_backend_client.py::test_cancelled_call_cancels_backend`.
- 2.2.5 - A websocket server config is rejected by `MCPServerConfig` and absent from the CLI choices. test: `tests/mcp_proxy/test_backend_client.py::test_websocket_transport_is_rejected`.
- 2.2.6 - On the local pool, `remove_server`, disabling, a config-changing `update_server`, `disconnect_server` and `disconnect_all` post the matching local backend disconnect at once and flip `is_connected`; `connect_all` warms only `preconnect_servers`; `get_health_report` returns the backend status. test: `tests/mcp_proxy/test_backend_client.py::test_lifecycle_methods_map_to_backend_routes`.
- 2.2.7 - Sentinel header and env values reach the stub backend in the spec and appear in no captured log record or raised error, including after a failed `unauthorized` recovery. test: `tests/mcp_proxy/test_backend_client.py::test_resolved_secrets_stay_out_of_logs_and_errors`.

### 2.3 Client cancellation from gmcp to the upstream server [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gmcp/Cargo.toml`
- `crates/gmcp/src/server.rs::WrapperServer::call_tool`
- `crates/gmcp/src/call_tool.rs::call`
- `crates/gmcp/src/daemon.rs::*` — scope-reason: carry an optional call id header in RequestOptions and post the cancel request
- `crates/gmcp/src/call_tool/tests.rs::*` — scope-reason: pass a never-cancelled token to the widened call
- `crates/gmcp/tests/call_tool.rs::*` — scope-reason: cover cancel propagation against the stub daemon
- `crates/gmcp/tests/stub_daemon/mod.rs::*` — scope-reason: record the call id header and serve the cancel route
- `src/gobby/servers/routes/mcp/endpoints/cancellation.py`
- `src/gobby/servers/routes/mcp/endpoints/execution.py::call_mcp_tool`
- `src/gobby/servers/routes/mcp/tools.py::create_mcp_router`
- `src/gobby/servers/auth_service.py::_AGENT_CAPABILITY_MATRIX`
- `src/gobby/mcp_proxy/client_manager/backend.py`
- `src/gobby/mcp_proxy/models.py::*` — scope-reason: add MCPCallCancelled and ToolProxyErrorCode.CANCELLED
- `src/gobby/mcp_proxy/services/argument_validation.py::classify_error`
- `tests/servers/test_mcp_tool_cancel.py`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gmcp pin

Consumers unchanged:
- `src/gobby/mcp_proxy/services/tool_proxy.py` — no-edit-reason: ToolProxyService._classify_error delegates to classify_error with the same arguments.
- `src/gobby/servers/routes/mcp/endpoints/__init__.py` — no-edit-reason: it re-exports call_mcp_tool, whose `(request, server)` signature stays.
- `tests/mcp_proxy/services/test_scope_resolution_matrix.py` — no-edit-reason: it calls call_mcp_tool without the call id header, which keeps today's path.
- `tests/mcp_proxy/transports/test_rest_session_resolution.py` — no-edit-reason: it calls call_mcp_tool without the call id header, which keeps today's path.
- `tests/servers/routes/mcp_endpoints/test_execution_offload.py` — no-edit-reason: it calls call_mcp_tool without the call id header, which keeps today's path.
- `tests/servers/routes/mcp_endpoints/test_resolved_session_ownership.py` — no-edit-reason: it calls call_mcp_tool without the call id header, which keeps today's path.
- `src/gobby/servers/_app_routes.py` — no-edit-reason: create_mcp_router keeps its signature and returns the router that now also carries the cancel route.
- `src/gobby/servers/routes/mcp/__init__.py` — no-edit-reason: it re-exports create_mcp_router unchanged.
- `tests/mcp_proxy/test_oauth_registry.py` — no-edit-reason: it mounts create_mcp_router, whose signature stays.
- `tests/servers/routes/mcp_endpoints/test_bridge.py` — no-edit-reason: it mounts create_mcp_router, whose signature stays.
- `tests/servers/routes/mcp_endpoints/test_http_scope_resolution_matrix.py` — no-edit-reason: it mounts create_mcp_router, whose signature stays.
- `tests/servers/routes/mcp_endpoints/test_template_routes.py` — no-edit-reason: it mounts create_mcp_router, whose signature stays.
- `tests/servers/routes/test_runtime_handshake.py` — no-edit-reason: it asserts only the handshake entries, which do not change.

**Research context:**
- Observed: `crates/gmcp/src/call_tool.rs::call` runs the daemon request in
  `spawn_blocking` with blocking ureq. `guarded_call` bounds the wait and
  reports `background_call_continues`, so nothing reaches the daemon or the
  upstream server when the client cancels. This is the F5.1 gap.
- Observed: `src/gobby/servers/routes/mcp/endpoints/execution.py` is 843
  lines. A new route module keeps it below the decomposition threshold.
- Chosen approach:
  - gmcp generates a UUID per call, sends it as `x-gobby-call-id`, and selects
    on the request context's cancellation token. On cancel it posts
    `POST /api/mcp/tools/cancel {call_id}` with the same session headers and
    returns.
  - `cancellation.py` holds a ContextVar for the client call id, set by
    `call_mcp_tool`, the owner-scoped registry with its tombstones
    (Decision 8), and the `cancel_mcp_tool` endpoint.
    - `cancel_mcp_tool` cancels a registered call and returns
      `{cancelled: true}`. For an unknown id it leaves a tombstone in the
      caller's own scope and returns `{cancelled: false}`. A tombstone
      expires after 600 seconds, the longest a call may live. At most 4,096
      are kept, oldest evicted first. A late or duplicate cancel is a no-op.
    - `call_mcp_tool` checks the tombstone before dispatch. A hit returns the
      ordinary failure payload with `error_code: "CANCELLED"` through
      `_process_tool_proxy_result`.
    - `McpBackend` registers each external call on entry, before
      `bearer_for`, routing and connect. Until the request is posted, a
      cancel only marks the entry cancelled and sends nothing, because no
      destination exists yet. Immediately before posting, `McpBackend`
      checks that mark and the tombstone. After posting, a cancel goes to
      the destination that received the request: here the local backend
      `cancel`, and from 3.3 also a node's `mcp.cancel`. Completion removes
      the entry. Internal tools are never registered.
    - A backend `cancelled` reply, or a hit at either check, raises
      `MCPCallCancelled`, a subclass of `MCPError` in `models.py`.
      `classify_error` maps it to `ToolProxyErrorCode.CANCELLED` before any
      string matching, so `_execute_tool`'s existing error path and workflow
      finalization run unchanged.
  - `_AGENT_CAPABILITY_MATRIX` gains `POST /api/mcp/tools/cancel` with
    identity binding, so only the calling session can cancel its calls.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gmcp/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the gmcp
    pin in `src/gobby/install/version_pins.py`. It copies no binary; the
    Orchestrator rebuilds and cuts over.
- Planned verification:
  `cargo nextest run -p gobby-mcp --test call_tool` and
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_mcp_tool_cancel.py -v`.

**Acceptance:**

- 2.3.1 - A client cancel makes gmcp post the cancel with the call id it sent. test: `crates/gmcp/tests/call_tool.rs::client_cancel_posts_daemon_cancel`.
- 2.3.2 - Through an app that mounts `create_mcp_router`, a real `ToolProxyService` and a stub loopback backend that holds the call until cancel, a cancel for an in-flight external call reaches the backend and the original HTTP response body has `success: false` and `error_code: "CANCELLED"`; a cancel from another session leaves that call running and returns `{cancelled: false}`. test: `tests/servers/test_mcp_tool_cancel.py::test_cancel_reaches_backend_for_owner_only`.
- 2.3.4 - With deterministic barriers, a cancel before the original POST, a cancel while `call_mcp_tool` resolves context, and a cancel while `McpBackend` awaits `bearer_for` each end the call `CANCELLED` with zero backend requests; a cancel before an internal call starts skips it, while an internal call already running completes; a late or duplicate cancel changes nothing. test: `tests/servers/test_mcp_tool_cancel.py::test_cancel_orderings_before_dispatch`.
- 2.3.3 - The contract documents the cancel route and header. behavior: "x-gobby-call-id" in `docs/contracts/mcp-multiplexer.md`.

## P3: Node execution
`kind: framing`

### 3.1 MCP commands in node-commands contract version 6 [category: code] (depends: 2.3, #25207)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: declare the mcp module, add one CommandFrame arm per MCP command and one ReplyResult arm per reply, and set CONTRACT_VERSION to 6
- `crates/gdaemon/src/node_commands/mcp.rs`
- `crates/gdaemon/src/node_commands/frame_dispatch.rs::command_frame`
- `crates/gdaemon/src/nodes/registry.rs::CommandReceiver::recv`
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: add the four mcp commands, their replies and malformed messages
- `crates/gdaemon/tests/node_command_parity.rs::*` — scope-reason: add the version 6 message list and round-trip test
- `crates/gdaemon/tests/node_internal_route.rs::*` — scope-reason: malformed mcp commands send nothing
- `src/gobby/hub/node_commands.py::*` — scope-reason: set CONTRACT_VERSION to 6 and add the mcp command and result models from client_manager wire types
- `tests/hub/test_node_command_parity.py::*` — scope-reason: add the version 6 message list and round-trip test
- `tests/hub/test_node_command_corpus.py::*` — scope-reason: round-trip the new corpus messages
- `docs/contracts/node-commands.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gdaemon/src/node_commands.rs` is 979 lines with
  `CONTRACT_VERSION` 4. `checkout_read.rs` is the precedent for a command in
  its own module. `frame_dispatch.rs::command_frame` maps names to variants,
  and `registry.rs::CommandReceiver::recv` matches every variant for its
  `request_id`. Commit 56de60d743 shows the carrier set.
- Split: the new command types live in the new
  `crates/gdaemon/src/node_commands/mcp.rs`. `node_commands.rs` gains only the
  module declaration and one enum arm per command and reply, so the move
  keeps it under 1,000 lines after #25207's own split.
- Chosen approach:
  - `mcp.request` carries 1.1's `McpRequest` as its payload.
  - `mcp.cancel` carries `{call_id}`.
  - `mcp.disconnect` carries `{id}` or `{all: true}`, as 1.1's route does.
  - `mcp.status` carries no payload.
  - Replies are `McpResult { mcp: McpReply }`, `{cancelled}`,
    `{mcp_disconnected}` (the count closed) and `{mcp_status}` (1.1's status
    entries). Each reply has a required key that no other `ReplyResult` arm
    uses, so the untagged enum stays unambiguous.
  - A reply over 8 MiB (`checkout_read.rs::REPLY_BUDGET`) becomes
    `result_too_large`.
  - Channel errors keep their existing codes and stages.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `cargo nextest run -p gobby-daemon -E 'test(node_command) | binary(node_internal_route)'`
  and the two Python parity tests with
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`.

**Acceptance:**

- 3.1.1 - `mcp.request`, `mcp.cancel`, `mcp.disconnect`, `mcp.status` and their replies round-trip byte-identically in Rust at version 6. test: `crates/gdaemon/tests/node_command_parity.rs::v6_mcp_commands_round_trip`.
- 3.1.2 - The same corpus messages round-trip through the Python models. test: `tests/hub/test_node_command_parity.py::test_v6_mcp_commands_round_trip`.
- 3.1.3 - A malformed `mcp.request` (unknown field, missing server) sends nothing to the node. test: `crates/gdaemon/tests/node_internal_route.rs::malformed_mcp_requests_send_nothing`.
- 3.1.4 - `node_commands.rs` stays under 1,000 lines with the MCP types in their own module. file: `crates/gdaemon/src/node_commands/mcp.rs`.
- 3.1.5 - The contract documents the four commands, the reply budget and the error mapping. behavior: "mcp.request" in `docs/contracts/node-commands.md`.

### 3.2 Node MCP command handler [category: code] (depends: 3.1, #24376)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/src/state.rs::NodeServices`
- `crates/gdaemon/src/state.rs::NodeServices::prepare`
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/tests/node_mcp.rs`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gdaemon/src/nodes/channel.rs::run_client` ignores command
  frames today. #24376 creates `nodes/agents/commands.rs::dispatch` and routes
  each command frame there.
- Observed: `NodeServices::prepare` is the only constructor of `NodeServices`.
- Chosen approach:
  - `NodeServices` gains `mcp_clients: Arc<mcp_client::Pool>`.
  - `dispatch` sends `mcp.request` to `Pool::request`, `mcp.cancel` to
    `Pool::cancel`, `mcp.disconnect` to `Pool::disconnect` and `mcp.status`
    to `Pool::status`. MCP commands bypass the per-run queue because they
    name no run.
  - The node's serve path calls `Pool::shutdown` at exit through the
    `serve.rs` hook from 1.1.
  - The node expands `${VAR}` from its own environment and runs hooks against
    its own filesystem.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `cargo nextest run -p gobby-daemon --features mcp-fixture --test node_mcp`.

**Acceptance:**

- 3.2.1 - An `mcp.request` frame runs the stdio fixture on the node, and `whoami` reports the node daemon's `GOBBY_HOME`. test: `crates/gdaemon/tests/node_mcp.rs::request_runs_server_on_node`.
- 3.2.2 - `mcp.cancel` for an in-flight call answers `{cancelled: true}` and the pending request answers `cancelled`. test: `crates/gdaemon/tests/node_mcp.rs::cancel_reaches_node_pool`.
- 3.2.3 - MCP commands do not wait behind a busy run's queue. test: `crates/gdaemon/tests/node_mcp.rs::mcp_commands_skip_run_queues`.
- 3.2.4 - Delivery: a node-run `getenv` returns the sentinel env value, the one reply allowed to contain it. Non-disclosure: through `whoami`, a failed connect and `mcp.status`, the sentinel appears in no captured node log line, command reply, error reply or reply envelope field. test: `crates/gdaemon/tests/node_mcp.rs::spec_secrets_stay_out_of_node_logs_and_replies`.
- 3.2.5 - `mcp.status` lists a node-run server, and `mcp.disconnect` for its id reaps its whole process group, grandchild included, and drops it from the next `mcp.status`. test: `crates/gdaemon/tests/node_mcp.rs::disconnect_and_status_reach_node_pool`.

### 3.3 Hub places stdio servers on the caller's machine [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/client_manager/backend.py`
- `src/gobby/mcp_proxy/manager.py::*` — scope-reason: inject the session manager and hub database used for placement, and fan lifecycle methods and the health report out to node holders
- `src/gobby/runner_init/mcp_stack.py::*` — scope-reason: pass the session manager and hub database to the manager
- `src/gobby/runner_init/services.py::*` — scope-reason: pass the session manager and hub database to the manager
- `tests/mcp_proxy/test_backend_placement.py`

Consumers unchanged:
- `src/gobby/hub/machine_routing.py` — no-edit-reason: resolve_target_machine already returns local or remote and raises the routing codes placement needs.
- `src/gobby/hub/node_client.py` — no-edit-reason: NodeCommandClient.send already carries any command name, payload and timeout.

**Research context:**
- Observed: `src/gobby/hub/machine_routing.py::resolve_target_machine` returns
  `RoutedMachine(local=...)`. For a remote target it requires a project
  checkout, a live channel and the same owner, and raises `NodeCommandError`
  otherwise. `src/gobby/utils/session_context.py::get_current_session_id`
  holds the caller session for every proxied request.
- Chosen approach:
  - For a stdio server, `McpBackend` reads `get_current_session_id()` and
    looks up the session.
  - It calls `resolve_target_machine(project_id=session.project_id,
    requested=None, origin_machine_id=session.machine_id)`.
  - A remote target sends `mcp.request` and `mcp.cancel` through
    `NodeCommandClient.send` and records the machine in the holder set
    (Decision 5). A local target, a URL server or a missing session uses the
    local route.
  - Budgets (Decision 9), stated here for the leaf: the call's absolute
    deadline was fixed at entry. The outer `NodeCommandClient.send` timeout
    is `min(remaining + 5,000, 600,000)` ms, and the inner
    `McpRequest.timeout_ms` is that value minus 5,000, so a requested
    600,000 ms becomes 595,000 ms inner.
  - On `node_timeout` or a channel error, `McpBackend` sends a best-effort
    `mcp.cancel` for the `call_id` and does not replay.
  - Remove, disable, config update, `disconnect_server` and
    `disconnect_all` send `mcp.disconnect` to every holder concurrently,
    each bounded by 5,000 ms so an offline node cannot stall the change or
    hub shutdown.
  - Offline holders (Decision 5), stated here for the leaf: a failed holder
    does not block the change. The row is still removed or updated, the
    method's result lists each failed holder with its routing code, the
    failure is logged with the machine id, and the holder stays in the set
    for the next invalidation. The node's 300-second idle close bounds the
    orphaned connection, and the fingerprint makes the next request use the
    fresh spec. A hub restart loses the in-memory holder set, with the same
    fallback.
    `get_health_report` adds each holder's `mcp.status` under its machine
    id, or its routing code when unreachable.
  - `NodeCommandError` maps to `MCPError` with its code.
  - Discovery and calls follow the same rule, so a node-only command is also
    discovered on its node.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_backend_placement.py -v`.

**Acceptance:**

- 3.3.1 - A stdio call from a node session goes to that node as `mcp.request`; a URL server call from the same session stays on the hub route. test: `tests/mcp_proxy/test_backend_placement.py::test_stdio_follows_caller_machine_url_stays_on_hub`.
- 3.3.2 - A call with no session or from a hub-local session uses the local route. test: `tests/mcp_proxy/test_backend_placement.py::test_local_and_sessionless_calls_stay_local`.
- 3.3.3 - `node_offline` and `checkout_unresolved` surface as `MCPError` with those codes and run nothing on the hub. test: `tests/mcp_proxy/test_backend_placement.py::test_routing_refusals_never_fall_back_to_hub`.
- 3.3.4 - With two node holders of one stdio server, remove, disable, config update, `disconnect_server` and `disconnect_all` each send `mcp.disconnect` to both holders and the local pool; with one holder offline the change still applies, the result names that holder and its code, and the holder stays tracked; `get_health_report` merges both nodes' `mcp.status`. test: `tests/mcp_proxy/test_backend_placement.py::test_lifecycle_fans_out_to_every_holder`.
- 3.3.5 - A node-placed call sends outer `min(remaining + 5000, 600000)` ms and inner 5,000 ms less, so a requested 600,000 ms becomes 595,000 inner; with delayed dispatch the caller receives the pool's typed `timeout`, not `node_timeout`; a `node_timeout` sends one `mcp.cancel` and no replay. test: `tests/mcp_proxy/test_backend_placement.py::test_node_budgets_nest_and_cancel_on_outer_loss`.

## P4: Live proof
`kind: framing`

### 4.1 Hub-node pair proof of F5.1 and F5.2 [category: test] (depends: 3.3)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_pair.py::*` — scope-reason: add the F5 MCP proof beside the existing relay tests
- `tests/e2e/fixtures/mcp_fixture_server.py`
- `tests/e2e/gmcp_client.py::*` — scope-reason: send a client cancel and read progress for the proof

**Research context:**
- Observed: `tests/e2e/test_hub_node_pair.py` starts an isolated hub and an
  enrolled node (`pair_hub`, `pair_node`).
  `test_gmcp_on_node_reaches_hub_through_relay` already proves gmcp on the
  node reaches the hub.
- Chosen approach:
  - The fixture is a Python MCP SDK server with `whoami`, `sleep`, `big` and
    `read_file`.
  - The hub registers it three times:
    - as a stdio server;
    - as a streamable-HTTP server the test hosts behind a static bearer from
      a `$secret`;
    - as a streamable-HTTP server with `requires_oauth`. Its host also serves
      a refresh-grant token endpoint, rejects a stale bearer with 401 and
      `WWW-Authenticate`, and records each bearer it receives. The test seeds
      `MCPOAuthStorage` in the isolated hub with that token endpoint, a
      client, a stale access token and a refresh token.
  - The OAuth case crosses the real seam: gdaemon's typed `unauthorized`,
    2.1's `recover_unauthorized` in `McpBackend`, and the pool's reconnect
    with the new bearer. It adds no auth mechanism.
  - The test runs on macOS and Linux and has no latency assertions.
- Planned verification:
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py -v -m e2e`;
  `pair_hub` and `pair_node` start their own isolated daemons and state.

**Acceptance:**

- 4.1.1 - From gmcp on the node, the stdio `whoami` reports the node's `GOBBY_HOME` and `read_file` reads a file that exists only under the node's checkout. test: `tests/e2e/test_hub_node_pair.py::test_node_stdio_tool_runs_on_node`.
- 4.1.2 - From gmcp on the node, the hub-hosted HTTP connector succeeds with its bearer and an internal `gobby-tasks` call succeeds. test: `tests/e2e/test_hub_node_pair.py::test_node_reaches_hub_connector_and_internal_tool`.
- 4.1.3 - From gmcp on the node, `get_tool_schema` returns the stdio tool's schema and an oversized `big` result pages through `gobby-results:get_tool_result`. test: `tests/e2e/test_hub_node_pair.py::test_node_discovers_schema_and_pages_oversized_result`.
- 4.1.4 - From gmcp on the node, a short tool timeout and a client cancel each end the call, and the fixture records `notifications/cancelled` for both the node stdio and hub HTTP servers. test: `tests/e2e/test_hub_node_pair.py::test_node_timeout_and_cancel_reach_upstream`.
- 4.1.5 - From gmcp on the node, a call to the OAuth HTTP connector succeeds after one refresh: the fixture records the stale bearer answered 401 with `WWW-Authenticate`, one refresh grant, then the new bearer, and storage holds the new token. test: `tests/e2e/test_hub_node_pair.py::test_node_oauth_connector_recovers_from_401`.

## V1 Plan Changelog
`kind: framing`

2026-10-28 — The Plan Writer drafted the narrative; enhancement and adversarial
review are pending. Per the Orchestrator's ruling the draft lives at this path
under #25259; the earlier unowned draft at
`.gobby/plans/external-mcp-multiplexer.md` stays untouched for Josh.

2026-10-28 — The Orchestrator confirmed Decision 2 (remove the WebSocket
transport). Triage dispositioned all five Plan Enhancer suggestions as apply:
the live OAuth 401 recovery proof (4.1.5), the manager lifecycle mapping
(2.2.6), secret non-leakage proofs (Decision 3, 1.3.4, 2.2.7, 3.2.4), the
Windows compile-only CI job (1.2.6) and the direct `Pool` surface for #24884
(1.1.8, 1.1.9).

## V2 Verification
`kind: verification`

- Base plan validation: `uv run gobby plans validate .gobby/plans/external-mcp-transport-multiplexer.md -p /Users/josh/Projects/gobby`.
- Expansion validation stays unrun until the Plan Adversary derives and applies
  `## M1 Task Manifest` from the consensus bytes.
- Each deliverable owns its focused checks. No full pytest or whole-workspace
  Rust suite is required.
