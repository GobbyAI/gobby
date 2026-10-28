Plan artifact: `.gobby/plans/external-mcp-transport-multiplexer.md`

# External-MCP multiplexer and client policy in Rust

**Plan ID:** external-mcp-transport-multiplexer

## Overview
`kind: framing`

#21566 moves external MCP into Rust. gdaemon gains the connection pool,
process supervision and framing for external MCP servers, plus the client-side
policy that sits on that path: `$secret` resolution, OAuth (refresh,
keepalive, interactive login and dynamic client registration), placement of
stdio servers on the caller's machine, loopback-browser policy and
untrusted-value cleaning. The rest of the MCP proxy policy has named Rust
owners: #24884 (discovery, leases, schema validation, result offload, the
error contract, the cancel route, the external tool-inventory cache),
#24151 (the rules hook), #21570 (registry management and deletion of the
transitional Python forwarder) and #25283 (`gobby-results` paging).

Josh's ruling of 2026-10-28 08:08 CDT governs: "If it's not on the hub-node
path it needs to be rust. When we finish 1.0 there will be no python." Python
keeps only a thin transitional forwarder. Until #24884 lands,
`ToolProxyService` is still the front door. Its `MCPClientManager` forwards
each external call to gdaemon with the stored server config and the caller's
identity. #21570 deletes that forwarder before 1.0.

The plan also closes hub-node row F5 (`.gobby/plans/research/hub-node-live-task-list-25236.md`).
A stdio server runs on the caller's machine, so a node session's local stdio or
file tool runs on that node, while URL connectors stay hub-hosted (F5.2, F5.3).
gmcp gains end-to-end cancellation, the one F5.1 gap the code shows (F5.4). A
live hub-node proof covers the rest of F5.1. The plan also carries the
`gclient mcp-proxy` operator verbs that `.gobby/plans/client-operator-verbs.md`
assigns to #21566.

Josh's rulings (memory 83a9445f) apply: the hub-node proof covers macOS and
Linux, and no latency budget gates anything. There is no backward
compatibility. #23275's packaging (#24359) and server-side OAuth (#24360)
leaves keep their scope; this plan touches neither.

## Decision Record
`kind: framing`

1. **External MCP lives in a gdaemon module, `crates/gdaemon/src/mcp_client/`.**
   It has two Rust types:
   - `Pool` runs the protocol for a fully resolved `ServerSpec`. Nodes use it
     directly.
   - `Backend` is the hub-side front over the `Pool`. It owns secret
     resolution, OAuth, browser policy, result cleaning, placement and the
     holder set.

   Python reaches `Backend` through a loopback route that the Backend door
   answers in Standalone and Hub modes. Node mode reaches only the `Pool`, and
   only through node commands. #24884's gateway calls `Backend` directly.
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
   - Rejected: dropping SSE too. The epic names it, and OAuth authorizes SSE
     servers today.
3. **Rust owns external-MCP execution policy, and every other proxy policy
   has a named Rust owner.**
   - This plan, in Rust:
     - `$secret:` resolution (1.4, 1.5);
     - OAuth bearer, 401 recovery, refresh and keepalive (1.6);
     - interactive login and dynamic client registration (1.7);
     - loopback-browser policy and untrusted-value cleaning (1.5);
     - placement and the holder set (3.3).
   - #24884, the native gateway (rust-internal-mcp-servers 2.1):
     - discovery and leases (2.1.4);
     - argument injection and schema validation (2.1.2, 2.1.3);
     - result offload and the structured error contract (2.1.5);
     - dispatch through `Backend` (2.1.6);
     - the native cancel route (2.1.9);
     - the external tool-inventory cache, passive `GET /api/mcp/tools` and
       external `schema_hash` (2.1.9).
   - #24151, the rules hook. #25256's plan `.gobby/plans/s2-11-hub-hooks-rules.md`
     covers it in 7.14 (the DR6 pass endpoint and Python proxy forward) and
     7.7 (the MCP seam).
   - #21570:
     - server registry CRUD, templates, import, sync, `recommend_tools` and
       MCP metrics;
     - deletion of this plan's transitional Python forwarder before 1.0.
   - #25283: the `gobby-results` paging tools (`get_tool_result`,
     `search_tool_result`).

   Until #24884 lands, Python's `MCPClientManager` is a thin forwarder (2.1).
   It sends the server config exactly as the hub row stores it, with
   `$secret:` references unresolved, plus the caller's session, machine and
   project. No resolved secret value crosses the loopback route.

   Resolved header and env values are secrets. gdaemon and the node never log
   them, return them in status, or echo them in errors. `ServerSpec`'s
   `Debug` prints only header names and env keys. gmcp never sees a spec.

   Text an upstream server writes, in tool output or in its own JSON-RPC
   errors, passes through as untrusted content, cleaned for invisible
   characters. It is not a Gobby disclosure, and no redaction layer is added.
4. **OAuth runs in Rust on rmcp's `auth` feature.** The `oauth.py` modules
   it would have duplicated are deleted here.
   - Storage: `mcp_client::oauth::OAuthStore` implements rmcp's
     `CredentialStore` over the scoped secret store from 1.4. It keeps
     today's secret name, `mcp_oauth_<sha256(project_id\nid\nurl)>`, and
     today's `OAuthState` JSON shape, so stored tokens survive the cutover
     with no re-login.
   - Locking: `acquire_refresh_guard` holds `pg_advisory_lock` on the same
     key Python computes (`oauth_keepalive.py::oauth_lock_key`).
   - Python rules ported unchanged:
     - a rotated-token reload before any refresh;
     - shared backoff, where `Retry-After` wins over a jittered exponential
       delay capped at 900 seconds;
     - consent detection (`invalid_grant`, or "revoked" in the description),
       which clears the tokens;
     - keepalive 600 seconds before expiry on a 60-second tick;
     - refusal to re-register an existing client;
     - credential-free failure summaries.
   - Login is the subcommand `gdaemon mcp-auth <name>`. It runs on the
     hub or standalone machine with direct database access, as
     `gobby mcp-proxy auth` does today. `gobby mcp-proxy auth`, and
     `gclient mcp-proxy auth` after it, exec the subcommand.
   - Rejected: keeping OAuth in Python behind bearer handoff helpers. Josh's
     ruling removes Python by 1.0.
   - Rejected: a login route with a long-poll. A subcommand needs no route
     admission and keeps no pending-login state.
   - gmcp-stdio.md Decision 3 (CIMD only) governs Gobby's own authorization
     server (#24360), not this client.
5. **Placement uses no new schema column.** A stdio server runs on the
   caller's machine. With no caller, it runs on the local machine. URL
   servers always run on the hub's pool.
   - Caller identity comes from the front door, which already resolves it:
     the Python forwarder now and #24884's `ToolContext` later. `Backend`
     receives it as `Caller { session_id, machine_id, project_id }`.
   - `mcp_client::placement` ports `resolve_target_machine` for a request
     with no explicit target. A remote machine needs a `machines` row, a
     `project_checkouts` row for the project and a live channel in the node
     `Registry`. Otherwise the request is refused with `machine_not_found`,
     `checkout_unresolved` or `node_offline`. The local machine is
     `gobby_core::machine::read_local_machine_id()`.
   - The hub resolves secrets and OAuth tokens and sends the resolved spec in
     the node command. The node holds it only in memory.
   - `Backend` keeps an in-memory holder set: server id to the machine ids it
     has sent `mcp.request` for that server.
   - A disconnect for one server or for all reaches the local pool and sends
     `mcp.disconnect` to every holder. Python triggers it on server removal,
     disablement, a config change, `disconnect_server` and `disconnect_all`.
     Status merges the local pool with `mcp.status` from every holder.
   - An offline holder does not block the change:
     - the row is still removed or updated;
     - the result lists each failed holder with its routing code;
     - the failure is logged with the machine id;
     - the holder stays in the set for the next invalidation.

     The node's idle close (Decision 6) bounds an orphaned connection, and
     the fingerprint guarantees the next request uses the fresh spec. A hub
     restart loses the holder set, with the same fallback.
   - Rejected: a per-server machine column. The caller's machine already
     answers where a local tool must run, and a column would pin one server
     to one machine for every session.
   - Rejected: running every subprocess on the hub. F5.3 forbids treating that
     as parity.
6. **Pool semantics.**
   - A connection is keyed by server id. Its fingerprint is the SHA-256 of
     the canonical resolved-spec JSON, and a changed fingerprint (an edited
     config or a rotated token) replaces the connection.
   - Requests on one connection run concurrently under JSON-RPC ids.
   - Connect attempts keep today's policy: up to three retries at 1, 2 and 4
     seconds. Three consecutive failed connects open a 30-second circuit with
     one half-open probe.
   - An idle connection closes after 300 seconds.
   - A connection found closed before a request is sent reconnects once. A
     transport failure after the request is written returns
     `transport_closed` and never replays, because a replay could repeat a
     mutation.
   - Periodic health pings are dropped; the pool observes transport closure
     directly.
7. **Every stdio server runs under `gdaemon mcp-child`.** The pool spawns the
   wrapper as a process-group leader. The wrapper runs the server in its own
   group with inherited stdio and polls `getppid()` once a second. When
   gdaemon dies, the wrapper signals the group with SIGTERM, waits 5 seconds,
   then sends SIGKILL. Disconnect, idle close and `Pool::shutdown` signal the
   group the same way.
   - This replaces the Python psutil supervisor for MCP.
     `src/gobby/utils/child_supervisor.py` stays for its ACP user.
   - Windows compiles with process-wrap's `JobObject`, and a Windows Rust CI
     job proves that compile. Its runtime proof belongs to the 1.0 Windows
     work, per Josh's ruling.
   - Rejected: supervising only browser hooks, as Python does. An npx launch is
     already a launcher plus a child, so one rule for every server is simpler
     and closes the same orphan path.
8. **Cancellation cancels the backend call, never a Python task.** Every
   request carries a `call_id`. Cancelling it sends `notifications/cancelled`
   upstream, and the pending request answers `cancelled`.
   - gmcp sends its own call id in `x-gobby-call-id`. When its client
     cancels, gmcp posts `/api/mcp/tools/cancel`.
   - Until #24884 2.1.9 serves that route natively, Python maps the id to the
     in-flight forwarder call and posts the backend `cancel`.
   - `Backend` routes the cancel to wherever the request went: the local
     `Pool`, or a node through `mcp.cancel`.
   - `McpBackend` raises the typed `MCPCallCancelled`, and `classify_error`
     maps it to the new `ToolProxyErrorCode.CANCELLED`, so the original
     request finishes through the normal error path.
   - Either message can arrive first. Three registries share one rule:
     Python's, keyed by (caller session, client call id); `Backend`'s; and
     the `Pool`'s, both keyed by `call_id`.
     - A cancel for an unknown id leaves a tombstone that expires after 600
       seconds, the longest a call may live.
     - Each registry keeps at most 4,096 tombstones and evicts the oldest
       first.
     - A call that finds its tombstone answers `CANCELLED` without
       contacting the upstream server.
   - `Backend` and `Pool` register a `call_id` on entry, before secret
     resolution, OAuth, placement, connect and retry. They check cancellation
     again immediately before the upstream write or node send. Completion
     removes the entry. A late or duplicate cancel is a no-op.
   - `call_mcp_tool` checks the tombstone before dispatch, so a call
     cancelled before it starts never runs, internal or external. A call
     already running is cancelled only when it is external. An internal tool
     is never interrupted, because asyncio cancellation between its writes
     could leave partial state. gmcp discards its result.
   - Rejected: relying on HTTP disconnects. gmcp's blocking ureq call cannot
     close its socket, and a disconnect through the node relay reaches the hub
     unreliably.
9. **Timeouts.** Each hop's inner deadline ends 5,000 ms before its outer
   wait, so the inner typed result always arrives first.
   - The Python forwarder fixes an absolute deadline at entry. It sends the
     remaining budget as `timeout_ms`, and its HTTP read timeout is that
     budget plus 5,000 ms.
   - `Backend` fixes its own absolute deadline from `timeout_ms` at entry.
     Secret resolution, OAuth recovery, placement and connect all spend that
     one budget. A call whose budget is gone answers `timeout`.
   - A local call gives the `Pool` the remaining budget. At that deadline
     the pool cancels upstream and answers `timeout`, which Python maps to
     its existing timeout result.
   - A node-placed call waits `outer = min(remaining, 600,000)` ms for the
     node reply, the internal command route's ceiling, and sends the inner
     `timeout_ms = outer - 5,000`. A requested 600,000 ms becomes 595,000 ms
     inner.
   - When the outer wait still ends in `node_timeout` or a channel error,
     `Backend` sends a best-effort `mcp.cancel` for the `call_id` and never
     replays the call. The inner deadline bounds the node-side call either
     way.
10. **Node commands take the next contract version after #25207.**
   remote-terminal-attach 1.1 (#25207, open and unblocked) takes version 5.
   `mcp.request`, `mcp.cancel`, `mcp.disconnect` and `mcp.status` take
   version 6.
   - Rejected: claiming version 5. #25207 is already expanded with version 5
     test names.
11. **The project-scoped secret store has one Rust implementation.** 1.4 adds
   `SecretStore::secret_in` and `SecretStore::set_in` to `gobby-runtime`. A
   request with a project reads the project row first and falls back to the
   global row, as `storage/secrets.py::_lookup_secret_row` does. #24466
   (runtime-family 5.9) consumes `set_in` per its amended 5.9.10, so
   `gobby-runtime` gains no second writer.
12. **The `gclient mcp-proxy` carrier is a thin client.** Each verb calls the
   same `/api/mcp` route its Python verb calls today, whichever backend
   serves it. `auth` and `oauth-shape` exec `gdaemon mcp-auth`. The
   `mcp-proxy` row of client-operator-verbs stays on #21566 and needs no
   amendment there.
   - The verbs go through gclient's one hand-written parser, which gains
     their long options and a list of repeatable options. Python's
     single-letter aliases are dropped (AGENTS.md rule 10).
   - Rejected: a separate `clap` parser for the subtree. It would be a second
     parser and a new dependency.

## Constraints
`kind: framing`

- Evidence was observed at HEAD `babdb0d558df4d9dbaa7b553bab18f238f5a3b1e` on
  2026-10-28 and refreshed at `90a14ac3190325d025cf8bf07e345c5c2fc6917e` for
  the Rust-policy revision.
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
  - `rg -l "mcp_proxy\.oauth|from gobby\.mcp_proxy\.oauth" src tests` lists
    `cli/mcp_oauth.py`, `oauth.py`, `oauth_keepalive.py`,
    `transports/http.py`, `runner_init/mcp_stack.py`,
    `runner_init/services.py`, `tests/mcp_proxy/test_oauth.py`,
    `test_oauth_callback.py`, `test_oauth_keepalive.py` and
    `test_oauth_registry.py`.
  - `rg -l "client_manager\.secrets|resolve_secrets_in_config|_resolve_secrets_in_config" src tests`
    lists `client_manager/connections.py`, `client_manager/secrets.py`,
    `client_manager/server_registry.py`, `manager.py`,
    `tests/mcp_proxy/test_manager_coverage.py` and
    `tests/mcp_proxy/test_mcp_manager.py`.
  - `rg -n "filter_browser_tools|enforce_browser_call" src` shows
    `client_manager/invocation.py` is the only caller of
    `enforce_browser_call`. `client_manager/tool_inventory.py` calls
    `filter_browser_tools`, including on the cached inventory path #24884
    takes over.
- Live registry: seven stdio servers and one OAuth HTTP server (fieldy). All
  eight bundled templates are stdio. No websocket or SSE server is installed.
- Shipping rule: every Rust leaf that changes `gdaemon`, `gmcp` or `gclient`
  bumps that crate by `0.0.1` in its own commit and updates `Cargo.lock` and
  `version_pins.py`. A `gobby-runtime` change ships through the `gdaemon`
  bump, because gdaemon is its only dependent. The leaves form one dependency
  chain for that reason. The Orchestrator announces and performs every
  cutover; no leaf copies binaries.
- No production file may reach 1,000 lines. `crates/gdaemon/src/serve.rs`
  is 818 lines; leaves that touch it add only calls into `mcp_client`.
- Rust work loads the `rust` skill. Python runs through `uv run`. Tests use the
  isolated database and `GOBBY_TEST_PROTECT=1`.

## Cross-plan edges
`kind: framing`

| Leaf | External work | Edge | Reason |
| --- | --- | --- | --- |
| 3.1 | #25207 (remote-terminal-attach 1.1) | blocked by | It takes contract version 5 and moves the terminal types out of `node_commands.rs`. |
| 3.2 | #24376 (node-halves 2.7) | blocked by | It creates the node command dispatcher that 3.2 extends. |
| none | #21570 (MCP front door flip) | blocks | #21570 already waits on #21566. Before 1.0 it deletes the transitional Python forwarder (2.1, and the Python half of 2.2). It also owns registry CRUD, templates, import, sync, `recommend_tools` and MCP metrics. |
| none | #24884 (rust-internal-mcp-servers 2.1) | relation | It already waits on #21566 and calls `Backend` directly. It owns discovery, leases, validation, offload and the error contract (2.1.2-2.1.6), plus the native cancel route and the external inventory cache (2.1.9). |
| none | #24151 (S2.11 hub half; plan by #25256) | relation | The rules hook for proxied calls is s2-11 7.14; its MCP seam 7.7 already depends on #21566. |
| none | #25283 (native `gobby-results` paging) | relation | It ports `get_tool_result` and `search_tool_result`, which 4.1.3 exercises through Python today. |
| 1.4 | #24466 (runtime-family 5.9) | relation | Its 5.9.10 consumes 1.4's `SecretStore::set_in`; neither adds a second writer. |
| 3.3 | #21564 (agents family, `ownership.rs`) | relation | 3.3's `placement.rs` is the precedent its general machine-routing port can share; no ordering. |
| 5.1, 5.2 | #24954 (client-operator-verbs S3.1) | relation | They carry the `mcp-proxy` CLI row assigned to #21566. No leaf waits on #21571, whose 1.7 gate waits on #21566. |
| 5.2 | #24467 (runtime-family 5.10, native `POST /api/config/secrets`) | blocked by | Its 5.10.6 adds the optional `project_id` that 5.2's secret prompt sends and passes it to 1.4's `set_in`. 5.2 does not edit `save_secret`. |
| none | #24359, #24360 (gmcp-stdio) | relation | Packaging and server-side OAuth keep their scope. |

**Granularity:** Fifteen deliverables, one per independently testable outcome.
- The Rust pool (1.1), the process supervisor (1.2) and the URL transports
  (1.3) are three lifecycle owners.
- The scoped secret store (1.4) is testable against Python's ciphertext
  before any MCP code uses it.
- The hub `Backend` front (1.5) is its own lifecycle owner. The OAuth runtime
  (1.6) and the login subcommand (1.7) are two separate state machines.
- The Python forwarder (2.1) and cancellation (2.2) are separate behaviors.
- The node contract (3.1), node handler (3.2) and hub placement (3.3) are
  separate owners.
- 4.1 is the live proof.
- The CLI carrier splits into read verbs (5.1) and write and local verbs
  (5.2).

## P1: Rust external-MCP backend in gdaemon
`kind: framing`

### 1.1 Connection pool and stdio transport [category: code]
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/lib.rs::*` — scope-reason: export the new mcp_client module
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/wire.rs`
- `crates/gdaemon/src/mcp_client/stdio.rs`
- `crates/gdaemon/tests/support/mcp_fixture.rs`
- `crates/gdaemon/tests/mcp_client.rs`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: rmcp 3.5.1 provides `client` and `transport-child-process`
  (process-wrap 10 with default wrappers). gdaemon has no rmcp dependency yet.
- Observed: `src/gobby/mcp_proxy/lazy.py::CircuitBreaker` (3 failures, 30 s,
  one half-open call), `RetryConfig` (3 retries, 1 s doubling, 16 s cap) and
  `client_manager/health.py::LAZY_IDLE_TIMEOUT_SECONDS` (300 s) define the
  policy that Decision 6 ports.
- Observed: memory de6f17ed. MCP runtime state is keyed by server UUID, and
  consumers pass resolved ids. The pool keeps that key.
- Chosen approach:
  - `wire.rs` defines the serde types that the pool and, later, node
    commands share:
    - `ServerSpec { id, transport, command, args, env, runtime_hook, url,
      headers, connect_timeout_ms }`, always fully resolved;
    - `McpRequest { call_id, op, server, tool, arguments, uri, timeout_ms }`,
      where `op` is `list_tools`, `call_tool` or `read_resource`;
    - `McpReply`, either `{ok: true, result, connected}` or
      `{ok: false, error: {code, message, ...}}`.
    - Error codes are `invalid_request`, `connect_failed`, `circuit_open`
      (`recovery_in_ms`), `unauthorized` (`www_authenticate`), `timeout`,
      `cancelled`, `transport_closed`, `server_error` (`rpc_code`) and
      `result_too_large`.
  - `mod.rs` owns `Pool`. It implements Decision 6, the `call_id` registry
    with its tombstones (Decision 8) and `Pool::shutdown`. Requests use
    rmcp's `send_cancellable_request`, and a cancel or deadline calls the
    handle's `cancel`.
  - The public Rust surface is `Pool::new`, `Pool::request(McpRequest) ->
    McpReply`, `Pool::cancel(call_id) -> bool`, `Pool::disconnect`,
    `Pool::status` and `Pool::shutdown`, plus the `wire` types. `Pool` stays
    one concrete type. 1.5 builds `Backend` on it, and 3.2 serves it to
    nodes.
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
  - `stdio.rs` launches the spec's command, args and env as given; 1.2 adds the
    launch environment.
  - The fixture binary `gdaemon-mcp-fixture` (`[[bin]]` with
    `required-features = ["mcp-fixture"]`, never built in release) is an rmcp
    stdio server. Its tools are `echo`, `whoami` (pid, ppid and `GOBBY_HOME`),
    `getenv`, `sleep` (records receipt of `notifications/cancelled` to a
    marker file), `big`, `spawn_grandchild`, `invisible` (returns text and
    keys carrying invisible Unicode) and the loopback-browser names
    `browser_navigate` and `browser_run_code`.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_client`
  and `cargo fmt -p gobby-daemon -- --check`.

**Granularity:** One lifecycle owner, the pool. Its wire types and fixture are
the minimum needed to test it; the acceptance items cover its transitions and
its Rust surface.

**Acceptance:**

- 1.1.1 - `Pool::request` runs `list_tools`, `call_tool` and `read_resource` against the stdio fixture, and concurrent calls on one connection all complete. test: `crates/gdaemon/tests/mcp_client.rs::stdio_round_trip_and_concurrent_calls`.
- 1.1.2 - A request whose spec fingerprint changed replaces the pooled connection, and an idle connection closes after the idle limit (test clock). test: `crates/gdaemon/tests/mcp_client.rs::fingerprint_change_and_idle_close_replace_connection`.
- 1.1.3 - Three failed connects answer `circuit_open` with `recovery_in_ms` until the window passes, then one half-open attempt runs. test: `crates/gdaemon/tests/mcp_client.rs::circuit_opens_after_three_failed_connects`.
- 1.1.4 - A deadline sends `notifications/cancelled` upstream and answers `timeout`; `Pool::cancel` does the same and answers the pending request `cancelled`. test: `crates/gdaemon/tests/mcp_client.rs::deadline_and_cancel_notify_upstream`.
- 1.1.5 - A connection found closed before sending reconnects once; a transport failure after sending answers `transport_closed` and the fixture records one call. test: `crates/gdaemon/tests/mcp_client.rs::no_replay_after_send`.
- 1.1.6 - `Pool::shutdown` leaves no fixture process alive. test: `crates/gdaemon/tests/mcp_client.rs::shutdown_reaps_every_server`.
- 1.1.7 - The contract documents the `Pool` API, wire types, error codes and pool policy. behavior: "Pool::request" in `docs/contracts/mcp-multiplexer.md`.
- 1.1.8 - With deterministic barriers, a cancel that arrives before its request and a cancel during connect backoff each answer the request `cancelled` with zero fixture calls; a cancel after completion and a duplicate cancel change nothing; tombstones expire and stay within their bound. test: `crates/gdaemon/tests/mcp_client.rs::cancel_orderings_never_reach_upstream`.

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
  - sets `npm_config_prefer_offline=true` when the command is `npx`, unless
    the server env sets it (memory ff600a62);
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

- 1.2.1 - `${VAR}`, `${VAR:-default}`, empty and unset variables expand exactly as `expand_env_variables` does, and `npx` launches get `npm_config_prefer_offline=true` unless the server env sets it. test: `crates/gdaemon/tests/mcp_client_stdio.rs::launch_env_matches_python_rules`.
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
    1.6 recovers from it.
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
- 1.3.4 - Delivery: the HTTP fixture records the sentinel bearer and the stdio fixture's `getenv` returns the sentinel env value, the one reply allowed to contain it. Non-disclosure: through non-echoing operations (`whoami`, a 401, a failed connect) the sentinels appear in no captured gdaemon log line, `Pool::status` entry, gdaemon-authored error, reply envelope field or `ServerSpec` `Debug` output. test: `crates/gdaemon/tests/mcp_client_http.rs::spec_secrets_stay_out_of_status_errors_and_logs`.

### 1.4 Project-scoped secret store read and write [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gruntime/src/secrets.rs::*` — scope-reason: add secret_in and set_in on SecretStore, route secret through secret_in, and add the name, category and scope rules they share
- `crates/gruntime/tests/secrets.rs::*` — scope-reason: cover scoped reads, the global fallback and Python-compatible writes
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gruntime/src/secrets.rs::SecretStore::secret` reads
  through `SecretStore::encrypted_value`, which hard-codes
  `project_id = '00000000-0000-0000-0000-000000000002'`, the global scope.
  No Rust writer exists. `SecretStore::seal` and `SecretStore::open_sealed`
  serve sealed tokens, not named secrets.
- Observed: `src/gobby/storage/secrets.py::SecretStore._lookup_secret_row`
  reads the project row first and falls back to the global row. A `None`
  project reads only the global row.
  `src/gobby/storage/secrets.py::SecretStore.set` does the following:
  - validates `category` against `VALID_CATEGORIES`
    (`general`, `llm`, `mcp_server`, `memory`, `integration`);
  - normalizes and validates the name against `SECRET_NAME_GRAMMAR`
    (`[A-Za-z_][A-Za-z0-9_]*`, `storage/secret_names.py`);
  - encrypts with `Fernet(DEK)`;
  - upserts with
    `INSERT INTO secrets ... ON CONFLICT (name, project_id) DO UPDATE` and a
    random UUIDv4 id;
  - writes a `None` project to the global scope.
- Observed: `src/gobby/mcp_proxy/client_manager/secrets.py::resolve_secrets_in_config`
  resolves through `store.get(name, project_id=config.project_id)`, and
  `src/gobby/mcp_proxy/oauth.py::MCPOAuthStorage` reads and writes its state
  with the same project scope.
- Observed: #24466 (runtime-family 5.9) planned a global-only `secrets::set`.
  Its amended 5.9.10 consumes `set_in` instead (Decision 11).
- Observed: gdaemon is the only crate that depends on `gobby-runtime`, so
  this change ships through a gdaemon bump.
- Chosen approach:
  - `SecretStore::secret_in(name, project_id: Option<Uuid>)` applies
    `_lookup_secret_row`'s order and decrypts as `secret` does today.
    `SecretStore::secret(name)` becomes `secret_in(name, None)`.
  - `SecretStore::set_in(name, value, category, description, project_id:
    Option<Uuid>)` ports `SecretStore.set`. It uses the same Fernet DEK
    through the `fernet` crate. Fernet tokens carry a timestamp and a random
    IV, so tests compare decrypted plaintexts, never ciphertext.
  - Errors carry only the normalized name, never a value.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-runtime --test secrets`
  against the isolated database.

**Acceptance:**

- 1.4.1 - `secret_in` returns the project row when one exists, falls back to the global row otherwise, and reads only the global row for a `None` project. test: `crates/gruntime/tests/secrets.rs::secret_in_reads_project_then_global`.
- 1.4.2 - A value written by `set_in` decrypts through Python's `SecretStore.get` with the same scope, and a value Python wrote decrypts through `secret_in`; a second `set_in` upserts in place. test: `crates/gruntime/tests/secrets.rs::set_in_round_trips_with_python`.
- 1.4.3 - `set_in` rejects an unknown category and a name outside `SECRET_NAME_GRAMMAR`, and its errors carry no value. test: `crates/gruntime/tests/secrets.rs::set_in_validates_name_and_category`.
- 1.4.4 - The contract documents the scope order and the single writer. behavior: "SecretStore::set_in" in `docs/contracts/mcp-multiplexer.md`.

### 1.5 Hub backend front, secret resolution and loopback route [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/wire.rs`
- `crates/gdaemon/src/mcp_client/backend.rs`
- `crates/gdaemon/src/mcp_client/resolve.rs`
- `crates/gdaemon/src/mcp_client/browser_policy.rs`
- `crates/gdaemon/src/mcp_client/untrusted.rs`
- `crates/gdaemon/src/mcp_client/internal_api.rs`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoorState`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoorState::new`
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: the in-file test helper builds FrontDoorState and must name the new backend field
- `crates/gdaemon/src/serve.rs::*` — scope-reason: share one Backend across listeners in serve_with_runtime and await Backend::shutdown when serving ends
- `crates/gdaemon/tests/common/channel.rs::*` — scope-reason: the FrontDoorState literal names the new backend field
- `crates/gdaemon/tests/mcp_backend.rs`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gdaemon/src/nodes/internal_api.rs::handle` is the loopback
  precedent. It admits only a loopback peer carrying `x-gobby-backend-secret`,
  answers 404 otherwise, limits bodies to 2 MiB, and runs only under
  `NativeRoutes::Hub`. `FrontDoor::handle` routes it after the backend-refusal
  check, and `Door::Node` answers it with 404.
- Observed: `crates/gdaemon/src/nodes/mod.rs::NativeRoutes` is
  `Standalone(Option<Arc<Pool>>)` or `Hub(Arc<HubNodes>)`, and `HubNodes`
  holds the database pool and the node `Registry`. `Backend` takes a clone.
- Observed: `src/gobby/mcp_proxy/client_manager/secrets.py::resolve_secrets_in_config`
  replaces every `$secret:NAME` match of `SECRET_REF_PATTERN` in headers, env
  values and args with the project-scoped value. When any name is missing,
  or no database is available, it raises
  `MCPError("Server '<name>' needs configuration: missing secret(s) <names>", missing_secrets=names)`.
  It never strips a reference and proceeds. Memory de6f17ed keeps references
  in the stored config and resolves them only for connections.
- Observed: `src/gobby/mcp_proxy/browser_policy.py` is the loopback-browser
  policy. It applies only to servers whose `runtime_hook` is
  `loopback_browser`.
  - `enforce_browser_call` refuses the unsafe tools `browser_run_code`,
    `browser_file_upload`, `upload_file` and `take_memory_snapshot`.
  - It refuses the host-path arguments `requestFilePath`,
    `responseFilePath`, `filePath`, `path`, `filename` and `outputDirPath`.
  - For `browser_navigate`, `navigate_page` and `new_page`, it refuses any
    URL that is not HTTP(S) on `127.0.0.1`, `::1` or `localhost` without
    userinfo. `about:blank` and `navigate_page` back, forward and reload are
    allowed.
  - `filter_browser_tools` drops the unsafe tools and strips the host-path
    arguments from the schemas.
- Observed: `src/gobby/prompts/rendering.py::clean_untrusted_value` strips
  four classes of characters from text leaves. TAB, LF and CR are kept.
  - tags: U+E0000-U+E007F;
  - zero-width: U+200B-U+200D, U+2060 and U+FEFF;
  - bidi: U+202A-U+202E and U+2066-U+2069;
  - controls: C0, DEL and C1.

  It rejects a dictionary key that carries any of them, and logs counts
  only. `src/gobby/mcp_proxy/manager.py::MCPClientManager.call_tool` applies
  it to every external result.
- Chosen approach:
  - `wire.rs` adds the hub request types:
    - `StoredServer { id, name, project_id, transport, command, args, env,
      headers, url, runtime_hook, requires_oauth, connect_timeout_ms }`, the
      config as the hub row stores it, references unresolved;
    - `Caller { session_id, machine_id, project_id }`;
    - `BackendRequest { call_id, op, server: StoredServer, caller:
      Option<Caller>, tool, arguments, uri, timeout_ms }`.
    - New error codes: `missing_secrets` (`names`), `policy_refused`
      (`message`) and `untrusted_key`.
  - `backend.rs` owns `Backend`. `Backend::request` does the following in
    order:
    - registers the `call_id` with the same tombstone rules as the pool
      (Decision 8);
    - fixes the absolute deadline (Decision 9);
    - applies the browser policy to `call_tool`;
    - resolves secrets;
    - builds the `ServerSpec` and calls `Pool::request` with the remaining
      budget;
    - cleans the result. A `list_tools` result also passes through the
      browser tool filter.

    The rest of the surface is `Backend::cancel`, `Backend::disconnect`,
    `Backend::status` and `Backend::shutdown`. #24884 calls these without
    HTTP. 1.6 adds OAuth and 3.3 adds placement inside the same methods.
  - `resolve.rs` ports `resolve_secrets_in_config` over
    `SecretStore::secret_in(name, project_id)` and fails closed with
    `missing_secrets`, naming secret names only. With no database pool it
    fails closed the same way.
  - `browser_policy.rs` ports `enforce_browser_call` (answering
    `policy_refused` with Python's message) and `filter_browser_tools`, both
    `pub` so #24884 reuses the filter for the cached inventory.
  - `untrusted.rs` ports `clean_untrusted_value` with the four classes and
    logs counts only. A result key carrying an invisible character answers
    `untrusted_key`.
  - `internal_api.rs` serves the loopback route:
    - `POST /api/internal/mcp/request` with a `BackendRequest`;
    - `POST /api/internal/mcp/cancel` with `{call_id}`, returning
      `{cancelled}`;
    - `POST /api/internal/mcp/disconnect` with `{id}` or `{all: true}`,
      returning `{disconnected, failed_holders: []}`;
    - `GET /api/internal/mcp/status`.

    It reuses the loopback and secret admission, answers in Standalone and
    Hub, limits bodies to 32 MiB, and answers 404 on the Node door before
    reading the body.
  - `FrontDoorState` gains `mcp_clients: Arc<mcp_client::Backend>`, built by
    `FrontDoorState::new`. `serve.rs` awaits `Backend::shutdown`, which shuts
    down the pool.
  - Non-disclosure (Decision 3): resolved values reach only the spec. Errors,
    logs and status name servers, codes and secret names, never values.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_backend`
  and `cargo fmt -p gobby-daemon -- --check`.

**Granularity:** One lifecycle owner, the hub `Backend`. Secret resolution,
browser policy and cleaning are steps of its one request path and are not
reachable apart from it. The route is the thin adapter Python needs.

**Acceptance:**

- 1.5.1 - Through `POST /api/internal/mcp/request`, `list_tools`, `call_tool` and `read_resource` reach the stdio fixture with a `StoredServer` whose env and args hold `$secret:` references, and the fixture's `getenv` returns the project-scoped value. test: `crates/gdaemon/tests/mcp_backend.rs::route_resolves_project_secrets_and_round_trips`.
- 1.5.2 - A missing secret answers `missing_secrets` naming only the missing names, with zero fixture launches; a project-less server resolves from the global scope. test: `crates/gdaemon/tests/mcp_backend.rs::missing_secrets_fail_closed_by_name`.
- 1.5.3 - On a `loopback_browser` server, `browser_run_code`, a host-path argument and a non-loopback URL answer `policy_refused` with Python's messages and never reach the fixture; `about:blank` and a loopback URL pass; `list_tools` omits the unsafe tools and the host-path arguments. test: `crates/gdaemon/tests/mcp_backend.rs::browser_policy_matches_python`.
- 1.5.4 - The fixture's `invisible` tool returns text with each character class stripped and TAB, LF and CR kept, and a result key carrying an invisible character answers `untrusted_key`. test: `crates/gdaemon/tests/mcp_backend.rs::results_are_cleaned_of_invisible_unicode`.
- 1.5.5 - Non-loopback peers, a wrong or missing secret, and every request on the Node door get 404 before the body is read. test: `crates/gdaemon/tests/mcp_backend.rs::route_admits_only_loopback_backend_secret`.
- 1.5.6 - Delivery: the resolved sentinel reaches the fixture env. Non-disclosure: through `whoami`, a missing secret and a failed connect, neither the sentinel nor any resolved header value appears in a captured gdaemon log line, a route reply, `Backend::status` or an error. test: `crates/gdaemon/tests/mcp_backend.rs::resolved_secrets_stay_out_of_replies_and_logs`.
- 1.5.7 - An integration test drives `Backend::request` and `Backend::cancel` against the stdio fixture with no HTTP route, as #24884 will. test: `crates/gdaemon/tests/mcp_backend.rs::backend_api_serves_without_route`.
- 1.5.8 - The contract documents the `Backend` API, the route, `StoredServer`, `Caller` and the new error codes. behavior: "Backend::request" in `docs/contracts/mcp-multiplexer.md`.

### 1.6 OAuth bearer, 401 recovery, refresh and keepalive in Rust [category: code] (depends: 1.5)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/wire.rs`
- `crates/gdaemon/src/mcp_client/backend.rs`
- `crates/gdaemon/src/mcp_client/oauth.rs`
- `crates/gdaemon/src/mcp_client/oauth_keepalive.rs`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: start the OAuth keepalive task beside the Backend and stop it when serving ends
- `crates/gdaemon/tests/support/oauth_fixture.rs`
- `crates/gdaemon/tests/mcp_client_oauth.rs`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `src/gobby/mcp_proxy/oauth.py::MCPOAuthStorage` stores one JSON
  `OAuthState` as a project-scoped secret. Its fields are `tokens`,
  `client`, `expires_at`, `metadata`, `resource`, `issuer`,
  `retry_not_before` and `retry_attempt`. The secret name is
  `mcp_oauth_<sha256(project_id\nid\nurl)>`.
- Observed: `src/gobby/mcp_proxy/oauth.py::PersistentOAuthProvider` behaves
  as follows:
  - `async_auth_flow` fails fast while backoff is active and the token is
    invalid ("OAuth token endpoint is in backoff").
  - It retries a 401 once after `_reload_rotated_token`, which applies a
    stored access token that differs from the one sent and has not expired.
  - It refreshes under `_enter_refresh_lock`.
  - `_record_token_endpoint_failure` handles a token-endpoint failure. When
    `consent_required` holds, it clears the tokens and raises
    `MCPAuthorizationRequired(oauth_auth_command(config))`. Otherwise it
    calls `schedule_backoff` and saves.
  - `refresh_persisted_token` calls only the token endpoint.
- Observed: `src/gobby/mcp_proxy/oauth_keepalive.py` holds the keepalive
  rules:
  - `oauth_lock_key` is the first 8 bytes of SHA-256(name), signed
    big-endian, and `oauth_server_lock` holds `pg_advisory_lock` on it.
  - `schedule_backoff` honours `Retry-After` or `x-retry-after`. Otherwise
    it waits `min(900, 5 * 2^(attempt-1))` seconds times a jitter of
    0.5-1.0.
  - `consent_required` is `invalid_grant`, or "revoked" in the description.
  - `refresh_due_oauth_servers` runs every 60 seconds over enabled rows with
    `requires_oauth`, a URL and an `http` or `sse` transport.
  - `refresh_server_if_due` refreshes 600 seconds before expiry when a
    client and a refresh token exist, rechecking under the lock.
  - Failures log the exception class only.
- Observed: `src/gobby/mcp_proxy/oauth.py::oauth_auth_command` yields
  `gobby mcp-proxy auth <name>`, plus `--global` for the global scope.
- Observed: rmcp 3.5.1's `auth` feature (adds `oauth2` and `async-trait`)
  provides the following:
  - `CredentialStore`, with `load`, `save`, `clear`,
    `on_refresh_token_rejected` and `acquire_refresh_guard`;
  - `CredentialRefreshGuard::new(guard)`;
  - `AuthorizationManager::set_credential_store`, `configure_client`,
    `set_metadata`, `refresh_token` and `get_access_token`;
  - `WWWAuthenticateParams::parse`.
- Chosen approach:
  - `oauth.rs` owns `OAuthStore`, rmcp's `CredentialStore` over
    `SecretStore::secret_in` and `set_in` (category `general`).
    - It reads and writes the `OAuthState` JSON shape. `save` merges token
      fields into the stored state and keeps `client`, `metadata`,
      `resource` and the backoff fields.
    - `acquire_refresh_guard` holds the advisory lock on `oauth_lock_key`
      over a dedicated connection and releases it on drop.
    - `oauth.rs` also ports `schedule_backoff`, `consent_required`,
      `oauth_auth_command` and a credential-free failure summary that keeps
      only `error`, `error_description` and `message`.
  - In `Backend::request`, a `requires_oauth` server gets
    `Authorization: Bearer <access token>` from `OAuthStore`. With no stored
    token it answers `authorization_required` (`auth_command`). On an
    `unauthorized` reply:
    - `Backend` first reloads a rotated token. Otherwise it refreshes once
      under the guard, unless backoff is active, which answers
      `oauth_unavailable` (`retry_in_ms`).
    - It retries the request once with the new bearer within the same
      deadline.
    - A consent failure clears the tokens and answers
      `authorization_required`.
    - A second `unauthorized` answers `authorization_required`.
  - `oauth_keepalive.rs` runs the 60-second tick in Standalone with a
    database and in Hub mode. It reads the eligible `mcp_servers` rows, and
    `serve.rs` stops it at shutdown.
  - Python's keepalive keeps running until 2.1 deletes it. Both refresh
    under the same advisory lock and reload before refreshing, so they never
    double-spend a refresh token.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1, enables rmcp's `auth` feature, and
    updates `Cargo.lock` and the gdaemon pin in
    `src/gobby/install/version_pins.py`. It copies no binary; the Orchestrator
    rebuilds and cuts over.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_client_oauth`.

**Granularity:** One lifecycle owner, the stored OAuth credential: its
reload, refresh, backoff, consent and keepalive transitions. Login is the
separate state machine in 1.7.

**Acceptance:**

- 1.6.1 - A `requires_oauth` HTTP server receives the stored bearer, and a server with no stored token answers `authorization_required` with `gobby mcp-proxy auth <name>` (plus `--global` for the global scope). test: `crates/gdaemon/tests/mcp_client_oauth.rs::bearer_attached_or_authorization_required`.
- 1.6.2 - A 401 with a rotated stored token retries once with it and never calls the token endpoint; without one, one refresh grant runs under the advisory lock, the new token is persisted in `OAuthState` shape, and the retry succeeds. test: `crates/gdaemon/tests/mcp_client_oauth.rs::unauthorized_reloads_then_refreshes_once`.
- 1.6.3 - A token-endpoint `invalid_grant` clears the tokens and answers `authorization_required`; a 429 with `Retry-After` records the shared backoff, and a call during backoff answers `oauth_unavailable` without contacting the endpoint. test: `crates/gdaemon/tests/mcp_client_oauth.rs::consent_and_backoff_match_python`.
- 1.6.4 - State Python's `MCPOAuthStorage` wrote is read by `OAuthStore`, and state `OAuthStore` wrote is read by Python, keeping `client`, `metadata` and `resource`. test: `crates/gdaemon/tests/mcp_client_oauth.rs::oauth_state_round_trips_with_python`.
- 1.6.5 - A keepalive tick refreshes a token due within 600 seconds and skips one in backoff or without a refresh token; two concurrent refreshers spend the refresh token once. test: `crates/gdaemon/tests/mcp_client_oauth.rs::keepalive_refreshes_due_tokens_once`.
- 1.6.6 - No access token, refresh token or client secret appears in a captured log line, reply or error across 1.6.2 and 1.6.3. test: `crates/gdaemon/tests/mcp_client_oauth.rs::oauth_credentials_stay_out_of_logs_and_replies`.

### 1.7 OAuth login and dynamic client registration in gdaemon [category: code] (depends: 1.6)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/main.rs::*` — scope-reason: add the mcp-auth subcommand with its server id, timeout and shape arguments
- `crates/gdaemon/src/mcp_client/login.rs`
- `crates/gdaemon/src/mcp_client/oauth.rs`
- `crates/gdaemon/tests/mcp_client_login.rs`
- `src/gobby/cli/mcp_oauth.py::auth_server`
- `src/gobby/cli/mcp_oauth.py::oauth_shape`
- `src/gobby/mcp_proxy/oauth.py::*` — scope-reason: remove authorize_server and _authorize_loaded_server, now served by gdaemon mcp-auth
- `src/gobby/mcp_proxy/oauth_callback.py::*` — operation: delete — scope-reason: gdaemon mcp-auth owns the loopback callback
- `tests/mcp_proxy/test_oauth_callback.py::*` — operation: delete — scope-reason: the callback tests move to Rust
- `tests/mcp_proxy/test_oauth.py::*` — scope-reason: drop the interactive login tests that move to Rust
- `tests/cli/test_mcp_oauth.py::*` — scope-reason: assert the auth and oauth-shape commands exec gdaemon mcp-auth
- `docs/guides/mcp-oauth.md`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `src/gobby/cli/mcp_oauth.py::auth_server` requires a running
  daemon and a local database. It then:
  - resolves the row by name and scope;
  - resolves its headers;
  - calls `authorize_server` with `webbrowser.open`;
  - maps `TimeoutError` to "OAuth authorization timed out; run auth again
    to retry" and other failures to their class name;
  - sends `PUT /api/mcp/servers/{name}` with `requires_oauth`.
- Observed: `src/gobby/cli/mcp_oauth.py::oauth_shape` prints
  `oauth_keepalive.py::oauth_state_shape`. That maps each of `tokens`,
  `client`, `expires_at`, `metadata`, `resource` and `issuer` to its
  Python type name or `null`, never a value.
- Observed: `src/gobby/mcp_proxy/oauth.py::_authorize_loaded_server` runs
  under the server lock. Its rules:
  - It reuses a stored client's redirect port and refuses a stored redirect
    that is not `http://127.0.0.1:<port>/callback` without query, fragment or
    userinfo.
  - It initializes the MCP session and lists tools, so the server's
    challenge drives discovery, and fails when no token results.
  - `PersistentOAuthProvider.async_auth_flow` refuses to re-register an
    existing client.
  - It picks the token-endpoint auth method `none`, then
    `client_secret_basic`, then `client_secret_post` from
    `token_endpoint_auth_methods_supported` (default `client_secret_basic`),
    and refuses when none is supported.
  - Registration that answers 429 or 5xx schedules backoff.
- Observed: `src/gobby/mcp_proxy/oauth_callback.py::OAuthCallback` accepts
  one loopback `/callback` request. `_oauth_error_message` maps
  `access_denied` and the other standard error codes to fixed messages.
- Observed: `crates/gdaemon/src/main.rs::resolve_database_url` reads
  `DATABASE_URL` or `bootstrap.yaml` for subcommands.
- Observed: rmcp 3.5.1's `AuthorizationManager` provides `new(base_url)`,
  `resolve_metadata_from_challenge`, `register_client`,
  `configure_client`, `get_authorization_url` (PKCE) and
  `exchange_code_for_token`.
- Chosen approach:
  - `gdaemon mcp-auth <name> [--project-id <uuid>] [--timeout <seconds>] [--shape]`
    runs on the hub or standalone machine with the database from
    `resolve_database_url`.
    - It loads the row by name in exactly the given scope, as
      `LocalMCPManager.get_server(name, project_id=project_id or GLOBAL_PROJECT_ID)`
      does. An omitted `--project-id` means the global scope.
    - An unknown name exits 1 with "No MCP server '<name>' in <scope> scope".
    - Callers pass a name and a scope, so neither the Python CLI nor gclient
      resolves a server id first.
  - `login.rs` does the following:
    - resolves the row's headers through 1.5's resolver;
    - takes the 1.6 guard;
    - probes the server and discovers metadata from its challenge;
    - registers a client only when none is stored, with the auth-method
      order above;
    - binds `127.0.0.1:<stored or ephemeral port>/callback`;
    - prints the authorization URL to stderr and opens it with `open` on
      macOS or `xdg-open` on Linux;
    - verifies the callback's state and exchanges the code;
    - saves through `OAuthStore`.

    It ports the callback error messages, the redirect checks, the
    re-registration refusal and the registration backoff. Exit status 0
    means credentials are stored. A timeout prints Python's timeout message
    and exits nonzero. Every failure message is credential-free.
  - `--shape` prints `oauth_state_shape`'s JSON with Python's type names
    (`OAuthToken`, `OAuthClientInformationFull`, `float`, `OAuthMetadata`,
    `ProtectedResourceMetadata`, `str`) and touches nothing.
  - `auth_server` keeps its scope and daemon checks. It execs
    `<gobby home>/bin/gdaemon mcp-auth <name> [--project-id <id>] --timeout <t>`
    with inherited stdio, then sends the existing `PUT` after exit 0.
    `oauth_shape` execs the same with `--shape` and needs no running
    daemon, as today.
  - Windows compiles `start` for browser opening; runtime proof stays with
    the 1.0 Windows work.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_client_login`
  and
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_mcp_oauth.py tests/mcp_proxy/test_oauth.py -v`.

**Acceptance:**

- 1.7.1 - Against a stub authorization server that challenges the MCP URL, `gdaemon mcp-auth` discovers metadata, registers a client with the first supported auth method, completes PKCE through a scripted callback, and stores credentials that 1.6's `OAuthStore` reads. test: `crates/gdaemon/tests/mcp_client_login.rs::login_registers_authorizes_and_stores`.
- 1.7.2 - With a stored client it reuses the redirect port and never registers again; a stored non-loopback redirect is refused before any request. test: `crates/gdaemon/tests/mcp_client_login.rs::stored_client_reuses_port_and_never_reregisters`.
- 1.7.3 - A callback with `access_denied`, a state mismatch and a timeout each exit nonzero with Python's message and store nothing; a 429 registration records backoff. test: `crates/gdaemon/tests/mcp_client_login.rs::callback_errors_and_timeout_store_nothing`.
- 1.7.4 - `--shape` prints the field-to-type map with Python's type names and no values, and an unknown name or wrong scope exits 1 naming the scope. test: `crates/gdaemon/tests/mcp_client_login.rs::shape_prints_types_never_values`.
- 1.7.5 - `gobby mcp-proxy auth` execs `gdaemon mcp-auth` with the server name, scope and timeout and sends the `requires_oauth` update only after exit 0; `gobby mcp-proxy oauth-shape` prints the subcommand's JSON. test: `tests/cli/test_mcp_oauth.py::test_auth_and_shape_exec_gdaemon_mcp_auth`.

## P2: Transitional Python forwarder
`kind: framing`

### 2.1 MCPClientManager forwards external calls to gdaemon [category: code] (depends: 1.7)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/client_manager/backend.py`
- `src/gobby/mcp_proxy/client_manager/wire.py`
- `src/gobby/mcp_proxy/manager.py::*` — scope-reason: replace transport state, secret resolution and result cleaning with the backend client, and keep registry, inventory and the cached-inventory browser filter
- `src/gobby/mcp_proxy/client_manager/server_registry.py::*` — scope-reason: drop transport connections, secret resolution and lazy-connector bookkeeping from add, remove, update and refresh
- `src/gobby/mcp_proxy/client_manager/tool_inventory.py::*` — scope-reason: list tools through the backend client
- `src/gobby/mcp_proxy/client_manager/connections.py::*` — operation: delete — scope-reason: the gdaemon pool owns connections
- `src/gobby/mcp_proxy/client_manager/health.py::*` — operation: delete — scope-reason: the gdaemon pool owns idle close and health
- `src/gobby/mcp_proxy/client_manager/invocation.py::*` — operation: delete — scope-reason: the manager calls the backend directly
- `src/gobby/mcp_proxy/client_manager/secrets.py::*` — operation: delete — scope-reason: gdaemon resolves secret references (1.5)
- `src/gobby/mcp_proxy/browser_policy.py::*` — scope-reason: remove enforce_browser_call, which the gdaemon Backend enforces (1.5) and whose only caller, invocation.py, is deleted; keep filter_browser_tools for the cached inventory
- `src/gobby/mcp_proxy/oauth.py::*` — operation: delete — scope-reason: gdaemon owns OAuth storage, refresh and login (1.6, 1.7)
- `src/gobby/mcp_proxy/oauth_keepalive.py::*` — operation: delete — scope-reason: the gdaemon keepalive replaces it (1.6)
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
- `src/gobby/runner_init/mcp_stack.py::*` — scope-reason: construct the manager with the session manager and without transport tuning, automatic OAuth or keepalive
- `src/gobby/runner_init/services.py::*` — scope-reason: construct the manager with the session manager and without transport tuning, automatic OAuth or keepalive
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
- `tests/mcp_proxy/test_oauth.py::*` — operation: delete — scope-reason: 1.6 and 1.7 own the OAuth tests in Rust
- `tests/mcp_proxy/test_oauth_keepalive.py::*` — operation: delete — scope-reason: 1.6 owns the keepalive tests in Rust
- `tests/mcp_proxy/test_oauth_registry.py::*` — scope-reason: seed OAuth state without the deleted MCPOAuthStorage
- `tests/mcp_proxy/test_semantic_unicode.py::*` — scope-reason: fake the backend client instead of patching get_client_session and ensure_connected
- `tests/hooks/test_python_pipeline_classifier.py::*` — scope-reason: smoke the openapi server through manager calls instead of get_client_session
- `tests/mcp_proxy/test_lazy.py::*` — operation: delete — scope-reason: the lazy connector retires
- `tests/mcp_proxy/test_lazy_idle_health.py::*` — operation: delete — scope-reason: idle close moved to gdaemon
- `tests/mcp_proxy/test_connection_lock_convoy.py::*` — operation: delete — scope-reason: connection locks retire
- `tests/mcp_proxy/test_manager_disconnect_cancellation.py::*` — operation: delete — scope-reason: transport disconnects moved to gdaemon
- `tests/mcp_proxy/test_manager_stale_sessions.py::*` — operation: delete — scope-reason: stale-session reconnects moved to gdaemon
- `tests/mcp_proxy/test_bundled_runtime_hooks.py::*` — operation: delete — scope-reason: 1.2 owns the hook tests in Rust
- `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py::*` — scope-reason: drive the openapi server through the backend client
- `tests/mcp_proxy/test_manager_coverage.py::*` — scope-reason: assert backend delegation instead of transport internals and drop the moved secret-resolution tests
- `tests/mcp_proxy/test_mcp_manager.py::*` — scope-reason: assert backend delegation and stored secret references instead of transport internals
- `tests/mcp_proxy/test_discovery_deadlines.py::*` — scope-reason: fake the backend client instead of BaseTransportConnection
- `tests/mcp_proxy/test_oauth_setup.py::*` — scope-reason: drop the login helpers that moved to gdaemon
- `tests/mcp_proxy/test_server_registry.py::*` — scope-reason: drop lazy-connector assertions
- `tests/mcp_proxy/test_server_mgmt.py::*` — scope-reason: drop lazy-connection-state assertions
- `tests/mcp_proxy/test_tool_inventory.py::*` — scope-reason: fake the backend client instead of client sessions
- `tests/mcp_proxy/test_unicode_gaps.py::*` — scope-reason: fake backend replies instead of client sessions
- `tests/mcp_proxy/test_server_coverage.py::*` — scope-reason: replace the server-health fake with the backend status
- `tests/mcp_proxy/test_browser_policy.py::*` — scope-reason: keep the cached-inventory filter tests and drop the call-policy tests that moved to Rust
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
- Observed: `src/gobby/utils/session_context.py::get_current_session_id`
  holds the caller session for every proxied request. The session row gives
  its `machine_id` and `project_id`.
- Observed: `runner_init/mcp_stack.py` and `runner_init/services.py` pass
  `automatic_oauth_authorization` into the manager and call
  `schedule_oauth_keepalive`. `services.py` also calls
  `cancel_oauth_keepalive`.
- Chosen approach. This is the transitional forwarder of Decision 3, and
  #21570 deletes it before 1.0.
  - `wire.py` holds the Pydantic mirror of 1.5's `BackendRequest`,
    `StoredServer`, `Caller` and `McpReply`.
  - `backend.py` owns `McpBackend`:
    - It builds `StoredServer` from the manager's stored config, with
      `$secret:` references unresolved. It builds `Caller` from
      `get_current_session_id()` and the session row, or sends none.
    - It fixes the call's absolute deadline at entry (Decision 9). It sends
      the remaining budget as `timeout_ms`, and the HTTP read timeout is that
      budget plus 5,000 ms.
    - It maps reply codes onto the manager's existing exceptions and result
      shapes:
      - `timeout` becomes the existing timeout result;
      - `circuit_open`, `connect_failed`, `transport_closed`,
        `server_error`, `oauth_unavailable` and `untrusted_key` become
        `MCPError` messages;
      - `missing_secrets` becomes `MCPError` with Python's
        "needs configuration" message and `missing_secrets`;
      - `authorization_required` becomes
        `MCPAuthorizationRequired(auth_command)`;
      - `policy_refused` becomes `ValueError(message)`, as
        `enforce_browser_call` raised;
      - the routing codes `machine_not_found`, `checkout_unresolved`,
        `node_offline`, `node_timeout` and `channel_closed` that 3.3 adds
        become `MCPError` carrying that code.
    - It records each reply's `connected` flag for `is_connected`.
    - On `asyncio.CancelledError` it sends `cancel` for its `call_id` as a
      shielded task, then re-raises.
  - The manager keeps its public method names except the removed
    connection-level ones: `connections`, `get_client`,
    `get_client_session`, `ensure_connected`, `_connect_server`,
    `get_lazy_connection_states`, `health_check_all`, `get_server_health`,
    `_monitor_health` and `_reconnect`. It no longer resolves secrets or
    cleans call results, because the Backend does both.
  - The kept lifecycle methods map onto 1.5's local routes with no second
    lifecycle layer. 3.3 extends the same routes to node holders.
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
    backend message, never header or env values (Decision 3).
  - `_list_external_server_tools` calls `mcp_manager.list_tools(server_name)`
    within its existing deadline.
  - `filter_browser_tools` stays in Python for the cached inventory until
    #24884 takes the cache. Applying it again to a live list the Backend
    already filtered changes nothing.
  - The runner stops passing `automatic_oauth_authorization` and stops
    scheduling the Python keepalive. It passes the session manager the
    forwarder needs.
- Split: `src/gobby/cli/mcp_proxy.py` is 877 lines and every websocket
  mention sits in `add_server` (lines 334-469). Move `add_server` into the new
  `src/gobby/cli/mcp_proxy_add.py` without websocket in its transport choices
  or URL check, and have `register_template_commands` import it beside the
  template and OAuth commands.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest`
  over every test file in Targets, `uv run ruff check src/`,
  `uv run mypy src/` and the web typecheck.

**Granularity:** About twenty production files are edited or created and
nineteen are deleted, but they form one replacement: the Python transport,
secret and OAuth layers and every consumer of their removed API. The
acceptance items are the behaviors of that one seam. Splitting the deletion
from the forwarding would leave a commit where external servers do not work.

**Acceptance:**

- 2.1.1 - Stdio and HTTP servers list and call tools through `McpBackend` against a stub backend route, which receives the stored config with `$secret:` references unresolved and the caller's session, machine and project. test: `tests/mcp_proxy/test_backend_client.py::test_manager_forwards_stored_config_and_caller`.
- 2.1.2 - `authorization_required` raises `MCPAuthorizationRequired` with the reply's auth command; `missing_secrets` raises `MCPError` with Python's "needs configuration" message and the names; `policy_refused` raises `ValueError` with its message. test: `tests/mcp_proxy/test_backend_client.py::test_policy_and_auth_codes_map_to_existing_errors`.
- 2.1.3 - `timeout`, `circuit_open` and `transport_closed` map to the existing timeout result and `MCPError` messages, `node_offline` and `checkout_unresolved` map to `MCPError` carrying that code, and the HTTP read timeout is the remaining budget plus 5,000 ms. test: `tests/mcp_proxy/test_backend_client.py::test_error_codes_map_to_existing_shapes`.
- 2.1.4 - Cancelling the awaiting coroutine sends a backend `cancel` for its `call_id`. test: `tests/mcp_proxy/test_backend_client.py::test_cancelled_call_cancels_backend`.
- 2.1.5 - A websocket server config is rejected by `MCPServerConfig` and absent from the CLI choices. test: `tests/mcp_proxy/test_backend_client.py::test_websocket_transport_is_rejected`.
- 2.1.6 - `remove_server`, disabling, a config-changing `update_server`, `disconnect_server` and `disconnect_all` post the matching backend disconnect at once and flip `is_connected`; `connect_all` warms only `preconnect_servers`; `get_health_report` returns the backend status. test: `tests/mcp_proxy/test_backend_client.py::test_lifecycle_methods_map_to_backend_routes`.
- 2.1.7 - The runner starts no Python OAuth keepalive, and no module under `src/gobby/mcp_proxy` imports `oauth`, `oauth_keepalive` or `client_manager.secrets`. test: `tests/mcp_proxy/test_backend_client.py::test_python_oauth_and_secret_resolution_are_gone`.

### 2.2 Client cancellation from gmcp to the upstream server [category: code] (depends: 2.1)
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
    returns. The route stays the same when #24884 2.1.9 serves it natively.
  - `cancellation.py` is transitional Python that #21570 deletes. It holds a
    ContextVar for the client call id, set by `call_mcp_tool`, plus the
    owner-scoped registry with its tombstones (Decision 8) and the
    `cancel_mcp_tool` endpoint.
    - `cancel_mcp_tool` cancels a registered call and returns
      `{cancelled: true}`. For an unknown id it leaves a tombstone in the
      caller's own scope and returns `{cancelled: false}`. A tombstone
      expires after 600 seconds, the longest a call may live. At most 4,096
      are kept, oldest evicted first. A late or duplicate cancel is a no-op.
    - `call_mcp_tool` checks the tombstone before dispatch. A hit returns the
      ordinary failure payload with `error_code: "CANCELLED"` through
      `_process_tool_proxy_result`.
    - `McpBackend` registers each external call on entry, before it builds
      the request. Until the request is posted, a cancel only marks the entry
      cancelled. Immediately before posting, `McpBackend` checks that mark
      and the tombstone. After posting, a cancel goes to the local backend
      `cancel`. The Backend forwards it to wherever the call went, including
      a node from 3.3. Completion removes the entry. Internal tools are
      never registered.
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

- 2.2.1 - A client cancel makes gmcp post the cancel with the call id it sent. test: `crates/gmcp/tests/call_tool.rs::client_cancel_posts_daemon_cancel`.
- 2.2.2 - Through an app that mounts `create_mcp_router`, a real `ToolProxyService` and a stub loopback backend that holds the call until cancel, a cancel for an in-flight external call reaches the backend and the original HTTP response body has `success: false` and `error_code: "CANCELLED"`; a cancel from another session leaves that call running and returns `{cancelled: false}`. test: `tests/servers/test_mcp_tool_cancel.py::test_cancel_reaches_backend_for_owner_only`.
- 2.2.3 - The contract documents the cancel route and header. behavior: "x-gobby-call-id" in `docs/contracts/mcp-multiplexer.md`.
- 2.2.4 - With deterministic barriers, a cancel before the original POST, a cancel while `call_mcp_tool` resolves context, and a cancel while `McpBackend` builds the request each end the call `CANCELLED` with zero backend requests; a cancel before an internal call starts skips it, while an internal call already running completes; a late or duplicate cancel changes nothing. test: `tests/servers/test_mcp_tool_cancel.py::test_cancel_orderings_before_dispatch`.

## P3: Node execution
`kind: framing`

### 3.1 MCP commands in node-commands contract version 6 [category: code] (depends: 2.2, #25207)
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
- `src/gobby/hub/node_commands.py::*` — scope-reason: set CONTRACT_VERSION to 6 and add the mcp command and result models
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
- Observed: `src/gobby/hub/node_commands.py` mirrors every command for the
  parity corpus, even though 3.3 now sends MCP commands only from Rust.
- Split: the new command types live in the new
  `crates/gdaemon/src/node_commands/mcp.rs`. `node_commands.rs` gains only the
  module declaration and one enum arm per command and reply, so the move
  keeps it under 1,000 lines after #25207's own split.
- Chosen approach:
  - `mcp.request` carries 1.1's `McpRequest`, whose `ServerSpec` is already
    resolved by the hub.
  - `mcp.cancel` carries `{call_id}`.
  - `mcp.disconnect` carries `{id}` or `{all: true}`.
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
  - `NodeServices` gains `mcp_clients: Arc<mcp_client::Pool>`. A node has no
    `Backend`: it resolves no secrets and holds no OAuth state.
  - `dispatch` sends `mcp.request` to `Pool::request`, `mcp.cancel` to
    `Pool::cancel`, `mcp.disconnect` to `Pool::disconnect` and `mcp.status`
    to `Pool::status`. MCP commands bypass the per-run queue because they
    name no run.
  - The node's serve path calls `Pool::shutdown` at exit through the
    `serve.rs` hook from 1.5.
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

### 3.3 Backend places stdio servers on the caller's machine [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/mcp_client/mod.rs`
- `crates/gdaemon/src/mcp_client/backend.rs`
- `crates/gdaemon/src/mcp_client/placement.rs`
- `crates/gdaemon/src/mcp_client/internal_api.rs`
- `crates/gdaemon/tests/mcp_backend_placement.rs`
- `docs/contracts/mcp-multiplexer.md`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

Consumers unchanged:
- `src/gobby/hub/machine_routing.py` — no-edit-reason: its Python callers are spawn routing, which #21564 owns; placement ports only the no-explicit-target case.

**Research context:**
- Observed: `src/gobby/hub/machine_routing.py::resolve_target_machine`
  defaults the target to the origin machine and returns local for the local
  machine. A remote target needs the following, checked in order:
  - a `machines` row, else `machine_not_found`;
  - a `project_checkouts` row for the project, else `checkout_unresolved`;
  - a live channel, else `node_offline`;
  - the same owner as the origin, else `machine_not_owned`.

  With no explicit target, origin and target are the same machine, so the
  owner check always passes.
- Observed: `crates/gdaemon/src/nodes/registry.rs::Registry` provides
  `command_sender`, `is_connected` and `connected_ids`.
  `CommandSender::request(id, frame)` returns the reply or `None` when the
  channel closes. `crates/gdaemon/src/nodes/internal_api.rs::handle` shows
  the frame build with `CONTRACT_VERSION` and the `node_offline`,
  `channel_closed` and `node_timeout` failures.
  `crates/gdaemon/src/nodes/machines_api.rs` reads `machines` from the hub
  pool. `gobby_core::machine::read_local_machine_id` names the local
  machine.
- Observed: `crates/gdaemon/tests/common/channel.rs` provides
  `channel_database`, `ChannelHub` and `node` for an in-process hub-node
  pair.
- Chosen approach:
  - `placement.rs` holds `place(caller, server, &NativeRoutes)`, which
    returns `Local` or `Remote(machine_id)`:
    - A URL server, a missing caller, or the local machine is `Local`.
    - Otherwise it checks the `machines` row, the `project_checkouts` row
      and `Registry::is_connected`, in that order, and refuses with
      `machine_not_found`, `checkout_unresolved` or `node_offline`.
    - Standalone mode has no registry, so a foreign machine is
      `node_offline`.
    - A refusal runs nothing on the hub.
  - `Backend::request` places after secret and OAuth resolution. A remote
    call builds `mcp.request` with the resolved spec, sends it through
    `CommandSender::request`, and records the machine in the holder set
    (Decision 5).
  - Budgets (Decision 9), stated here for the leaf: the call's absolute
    deadline was fixed at entry. The outer wait for the node reply is
    `min(remaining, 600,000)` ms, and the inner `McpRequest.timeout_ms` is
    that value minus 5,000. A requested 600,000 ms becomes 595,000 ms inner.
  - On `node_timeout` or a channel error, `Backend` sends a best-effort
    `mcp.cancel` for the `call_id` and does not replay.
  - `Backend::cancel` sends `mcp.cancel` to the machine that received the
    call, or cancels the local pool.
  - `Backend::disconnect` for one server or all sends `mcp.disconnect` to
    every holder concurrently, each bounded by 5,000 ms, so an offline node
    cannot stall the change or hub shutdown.
  - Offline holders (Decision 5), stated here for the leaf:
    - a failed holder does not block the change;
    - the route result lists each failed holder with its routing code in
      `failed_holders`;
    - the failure is logged with the machine id;
    - the holder stays in the set for the next invalidation.

    The node's 300-second idle close bounds the orphaned connection, and the
    fingerprint makes the next request use the fresh spec. A hub restart
    loses the in-memory holder set, with the same fallback.
  - `Backend::status` adds each holder's `mcp.status` under its machine id,
    or its routing code when unreachable.
  - Discovery and calls follow the same rule, so a node-only command is also
    discovered on its node.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gdaemon/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gdaemon pin in `src/gobby/install/version_pins.py`. It copies no binary;
    the Orchestrator rebuilds and cuts over.
- Planned verification:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test cargo nextest run -p gobby-daemon --features mcp-fixture --test mcp_backend_placement`.

**Acceptance:**

- 3.3.1 - On an in-process hub-node pair, a stdio call whose caller is on the node runs there as `mcp.request` (`whoami` reports the node's `GOBBY_HOME`), while a URL server call from the same caller stays on the hub pool. test: `crates/gdaemon/tests/mcp_backend_placement.rs::stdio_follows_caller_machine_url_stays_on_hub`.
- 3.3.2 - A call with no caller or from the hub's own machine runs on the local pool. test: `crates/gdaemon/tests/mcp_backend_placement.rs::local_and_callerless_calls_stay_local`.
- 3.3.3 - An unknown machine, a machine without a checkout and an offline node answer `machine_not_found`, `checkout_unresolved` and `node_offline` and run nothing on the hub. test: `crates/gdaemon/tests/mcp_backend_placement.rs::routing_refusals_never_fall_back_to_hub`.
- 3.3.4 - With two node holders of one stdio server, a disconnect for that server and a disconnect for all each reach both holders and the local pool; with one holder offline the disconnect still completes, `failed_holders` names that holder and its code, and the holder stays tracked; status merges both nodes' `mcp.status`. test: `crates/gdaemon/tests/mcp_backend_placement.rs::lifecycle_fans_out_to_every_holder`.
- 3.3.5 - A node-placed call waits outer `min(remaining, 600000)` ms and sends inner 5,000 ms less, so a requested 600,000 ms becomes 595,000 inner; with delayed dispatch the caller receives the pool's typed `timeout`, not `node_timeout`; a `node_timeout` sends one `mcp.cancel` and no replay; `Backend::cancel` for a node-placed call reaches that node. test: `crates/gdaemon/tests/mcp_backend_placement.rs::node_budgets_nest_and_cancel_follows_the_call`.

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
  - The proof runs through the transitional Python front door
    (`ToolProxyService` and the 2.1 forwarder) with Rust secrets, OAuth and
    placement behind it. #24884 2.1.6 re-proves the native policy path when
    the gateway lands, and #25283 takes over the paging tools 4.1.3 uses.
  - The fixture is a Python MCP SDK server with `whoami`, `sleep`, `big` and
    `read_file`.
  - The hub registers it three times:
    - as a project-scoped stdio server whose env holds a `$secret:`
      reference to a project-scoped secret;
    - as a streamable-HTTP server the test hosts behind a static bearer from
      a `$secret:` header reference;
    - as a streamable-HTTP server with `requires_oauth`. Its host also serves
      a refresh-grant token endpoint, rejects a stale bearer with 401 and
      `WWW-Authenticate`, and records each bearer it receives. The test seeds
      the stored `OAuthState` in the isolated hub with that token endpoint,
      a client, a stale access token and a refresh token.
  - The OAuth case crosses the real seam: gdaemon's typed `unauthorized`,
    1.6's reload-then-refresh in `Backend`, and the pool's reconnect with the
    new bearer. It adds no auth mechanism.
  - The test runs on macOS and Linux and has no latency assertions.
- Planned verification:
  `GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py -v -m e2e`;
  `pair_hub` and `pair_node` start their own isolated daemons and state.

**Acceptance:**

- 4.1.1 - From gmcp on the node, the stdio `whoami` reports the node's `GOBBY_HOME` and the project-scoped secret value in its env, and `read_file` reads a file that exists only under the node's checkout. test: `tests/e2e/test_hub_node_pair.py::test_node_stdio_tool_runs_on_node`.
- 4.1.2 - From gmcp on the node, the hub-hosted HTTP connector succeeds with its bearer and an internal `gobby-tasks` call succeeds. test: `tests/e2e/test_hub_node_pair.py::test_node_reaches_hub_connector_and_internal_tool`.
- 4.1.3 - From gmcp on the node, `get_tool_schema` returns the stdio tool's schema and an oversized `big` result pages through `gobby-results:get_tool_result`. test: `tests/e2e/test_hub_node_pair.py::test_node_discovers_schema_and_pages_oversized_result`.
- 4.1.4 - From gmcp on the node, a short tool timeout and a client cancel each end the call, and the fixture records `notifications/cancelled` for both the node stdio and hub HTTP servers. test: `tests/e2e/test_hub_node_pair.py::test_node_timeout_and_cancel_reach_upstream`.
- 4.1.5 - From gmcp on the node, a call to the OAuth HTTP connector succeeds after one refresh: the fixture records the stale bearer answered 401 with `WWW-Authenticate`, one refresh grant, then the new bearer, and storage holds the new token. test: `tests/e2e/test_hub_node_pair.py::test_node_oauth_connector_recovers_from_401`.

## P5: gclient mcp-proxy carrier
`kind: framing`

### 5.1 `gclient mcp-proxy` group and read verbs [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gclient/Cargo.toml`
- `crates/gclient/src/command.rs::*` — scope-reason: add the mcp-proxy long options and the repeatable-option list to the shared parser, extract the token read into resolve_token, declare the mcp_proxy module and route `mcp-proxy` before verbs::Action::parse
- `crates/gclient/src/command/help.rs::VERBS`
- `crates/gclient/src/command/mcp_proxy.rs`
- `crates/gclient/src/command/mcp_proxy/client.rs`
- `crates/gclient/src/command/mcp_proxy/read.rs`
- `crates/gclient/tests/command_mode.rs::VERBS`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: serve and record the /api/mcp read routes
- `crates/gclient/tests/mcp_proxy_verbs.rs`
- `crates/gclient/tests/fixtures/mcp_proxy_golden.json`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gclient pin

**Research context:**
- Observed: `.gobby/plans/client-operator-verbs.md` (S3.1 amendment, #24954)
  assigns the row `mcp-proxy` (carrier `gclient mcp-proxy`,
  `carried = false`) to #21566. The owner delivers every member through Rust,
  "HTTP/MCP route parity alone does not fulfil a CLI row", and its 1.7 gate
  executes every member through the Rust entry point without Python.
- Observed: gclient command mode is hand-written.
  - `crates/gclient/src/command.rs::dispatch_inner` parses global flat
    `VALUE_FLAGS` and `SWITCH_FLAGS` and rejects any unknown short flag.
  - `command/help.rs::VERBS` lists each verb, and
    `tests/command_mode.rs::every_verb_has_its_own_help` checks every entry
    of `tests/command_mode.rs::VERBS`.
  - With `--json`, a verb prints its result compactly; otherwise it prints
    plain text.
  - Usage errors exit 2 and connection failures exit 3.
  - `--daemon-url` defaults to `gobby_core::daemon_url::daemon_url()`, and
    `--token-file` defaults to `gobby_core::local_token::read_api_key()`.
  - gclient has no generic JSON HTTP helper and no MCP code.
    `daemon/rest.rs::RestClient` is private to the TUI.
- Observed: client-operator-verbs 1.1 proposes `command/api.rs` but has not
  landed. Like the Projects family, this plan does not wait on #21571,
  because its 1.7 gate already waits on #21566.
- Observed: `crates/gclient/src/command.rs::parse` is the one parser for
  every verb.
  - It accepts only the long options in `VALUE_FLAGS` and `SWITCH_FLAGS`.
  - It refuses a repeated option.
  - It puts every other argument in `positions`.

  `dispatch_inner` takes `--json`, `--daemon-url` and `--token-file`, then
  calls `verbs::Action::parse` and reads the token. client-operator-verbs
  1.1 through 1.4 extend this same parser and plan to extract the token read
  into `resolve_token`.
- Observed: `src/gobby/cli/installers/shared.py::registered_project_id`
  takes the `id` from the working directory's `.gobby/project.json` and
  keeps it only when the `projects` table has that row.
- Observed: the Python read verbs live in `src/gobby/cli/mcp_proxy.py` and
  `src/gobby/cli/mcp_proxy_templates.py`:

  | Verb | Arguments and options | Route |
  | --- | --- | --- |
  | `list-servers` | `--json` | `GET /api/mcp/servers[?project_id=]` |
  | `list-tools` | `-s/--server`, `--json` | `GET /api/mcp/tools?server_filter=&project_id=` |
  | `get-schema` | `SERVER_NAME TOOL_NAME` | `POST /api/mcp/tools/schema` |
  | `search-tools` | `QUERY`, `-k/--top-k` (10), `-s/--min-similarity` (0.0), `-S/--server`, `--json` | `POST /api/mcp/tools/search` with `cwd`, 120 s |
  | `recommend-tools` | `TASK_DESCRIPTION`, `-a/--agent`, `-m/--mode` (`llm`, `semantic`, `hybrid`; default `llm`), `-k/--top-k` (10), `--json` | `POST /api/mcp/tools/recommend` with `cwd`, 120 s |
  | `status` | `--json` | `GET /api/mcp/status` |
  | `list-templates` | `--global`, `--json` | `GET /api/mcp/templates?scope=&project_id=` |
  | `show-template` | `NAME`, `--global` | `GET /api/mcp/templates?...`, filtered by name on the client |

  - Every verb first checks daemon health.
  - Requests carry `X-Gobby-CLI-Internal: 1` and a 30-second default
    timeout. A non-200 prints `Error: <body>` to stderr and exits 1.
  - Project scope comes in two forms:
    - best-effort: `_cwd_project_id`, the registered project of the working
      directory, or none;
    - required: `resolve_cli_mcp_project`, which returns the global scope
      with `--global` and otherwise needs a registered project.
- Chosen approach:
  - `command.rs` keeps one parser for every verb:
    - `VALUE_FLAGS` and `SWITCH_FLAGS` gain the long options of the
      `mcp-proxy` verbs.
    - A new `REPEAT_FLAGS` list (`--set`, `--server`, `--arg`) collects
      repeated values in order, read with `Parsed::take_all`. A verb that
      takes one value refuses a second with exit 2.
    - `dispatch_inner` sends `mcp-proxy` to `command/mcp_proxy.rs` after it
      takes `--json`, `--daemon-url` and `--token-file`, and before
      `verbs::Action::parse`.
    - The token read moves into `resolve_token(token_file)`, the extraction
      client-operator-verbs 1.1 also plans. Whichever leaf lands first adds
      it, and the other reuses it.
  - `mcp-proxy` is one row in `help.rs::VERBS`. Its first position names the
    Python subcommand (`list-servers`, `get-schema` and so on). An unknown or
    missing subcommand exits 2 before any request.
  - Every Python verb name, positional argument, long option, choice list and
    default carries over. Python's single-letter aliases (`-s`, `-k`, `-a`
    and the rest) are dropped. AGENTS.md rule 10 keeps no backward
    compatibility, and Python gives `-s` different meanings in different
    verbs.
    - Rejected: a separate `clap` parser for the subtree. It would add a
      dependency and a second parser beside the one every gclient verb
      shares.
  - The cwd project is the `id` in `<root>/.gobby/project.json`, where
    `root` comes from `gobby_core::project::find_project_root(cwd)`.
    - It is kept only when `GET /api/projects` lists it, as
      `registered_project_id` does.
    - It never reads `gcode.json` and never opens the database.
    - When a verb requires a project and it is missing or unregistered, the
      verb exits 2 with Python's message.
  - `client.rs` sends one JSON request with the bearer from `--token-file`,
    `X-Gobby-CLI-Internal: 1` and the verb's timeout. It checks health first,
    as Python does. An unreachable daemon exits 3. A non-2xx reply prints
    `Error: <body>` and exits 1.
  - `read.rs` implements the eight read verbs. `--json` prints the route's
    reply. Plain text prints the same lines the Python verb prints for the
    same reply. Golden entries in `crates/gclient/tests/fixtures/mcp_proxy_golden.json`
    pin that text; they are captured once from the Python verbs against the
    same canned replies.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gclient/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gclient pin in
    `src/gobby/install/version_pins.py`. gclient is unpublished, so the
    checkout build installs it. The leaf copies no binary.
- Planned verification:
  `cargo nextest run -p gobby-client --test mcp_proxy_verbs --test command_mode`.

**Acceptance:**

- 5.1.1 - `list-servers`, `list-tools`, `get-schema` and `status` send Python's route, query and body against the mock daemon, with and without a cwd project, and print the route reply with `--json` and the golden text otherwise. test: `crates/gclient/tests/mcp_proxy_verbs.rs::server_tool_and_status_reads_match_python`.
- 5.1.2 - `search-tools` and `recommend-tools` send `cwd`, every option with its default, and the 120-second timeout, and print the golden text. test: `crates/gclient/tests/mcp_proxy_verbs.rs::search_and_recommend_match_python`.
- 5.1.3 - `list-templates` and `show-template` resolve scope from `--global` or the cwd project, and exit 2 with Python's message outside a project without `--global`; `show-template` prints the matched template and its parameter lines. test: `crates/gclient/tests/mcp_proxy_verbs.rs::template_reads_resolve_scope_like_python`.
- 5.1.4 - Every read verb accepts Python's long options and defaults. An unknown option, an unknown subcommand and a repeated single-value option each exit 2 before any request. An unreachable daemon exits 3, and a non-2xx reply exits 1 with `Error: <body>`. A `.gobby/project.json` id that `GET /api/projects` does not list is treated as no project. test: `crates/gclient/tests/mcp_proxy_verbs.rs::read_verbs_share_python_long_options_and_exits`.
- 5.1.5 - `gclient help mcp-proxy` documents the group, and `mcp-proxy` is in the command-mode verb list. test: `crates/gclient/tests/command_mode.rs::every_verb_has_its_own_help`.

### 5.2 `gclient mcp-proxy` write and local verbs [category: code] (depends: 5.1, #24467)
`kind: deliverable`

Targets:
- `Cargo.lock`
- `crates/gclient/Cargo.toml`
- `crates/gclient/src/command/mcp_proxy.rs`
- `crates/gclient/src/command/mcp_proxy/write.rs`
- `crates/gclient/src/command/mcp_proxy/auth.rs`
- `crates/gclient/tests/mock_daemon/mod.rs::*` — scope-reason: serve and record the /api/mcp write routes and the secrets route
- `crates/gclient/tests/mcp_proxy_verbs.rs`
- `crates/gclient/tests/fixtures/mcp_proxy_golden.json`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gclient pin

**Research context:**
- Observed: the Python verbs, all in `src/gobby/cli/mcp_proxy.py` except
  `auth` and `oauth-shape` in `src/gobby/cli/mcp_oauth.py`:

  | Verb | Arguments and options | Route or operation |
  | --- | --- | --- |
  | `add-server` | `NAME`, `-t/--transport` (`http`, `sse`, `stdio` after 2.1), `-u/--url`, `-c/--command`, `-A/--args` (JSON array), `-e/--env` (JSON), `--headers` (JSON), `--oauth`, `--disabled`, `--template`, `--set` (multiple `k=v`), `--description`, `--global` | `POST /api/mcp/servers` |
  | `remove-server` | `NAME`, `--global`, `--yes` confirmation | `DELETE /api/mcp/servers/{name}?scope=&project_id=` |
  | `import-server` | `-p/--from-project`, `-g/--github`, `-q/--query`, `-s/--server` (multiple), `--json`, `--global` | `POST /api/mcp/servers/import` |
  | `refresh` | `-f/--force`, `-s/--server`, `--json` | `POST /api/mcp/refresh` with `cwd`, 300 s |
  | `call-tool` | `SERVER_NAME TOOL_NAME`, `-a/--arg` (multiple `k=v`, values JSON-parsed, overriding `-j`), `-j/--json-args`, `--raw` | `POST /api/mcp/tools/call` with `offload: false` under `--raw` |
  | `auth` | `NAME`, `--global`, `--timeout` (at least 1, default 300) | 1.7's `gdaemon mcp-auth`, then `PUT /api/mcp/servers/{name}` |
  | `oauth-shape` | `NAME`, `--global` | 1.7's `gdaemon mcp-auth --shape`; no daemon check |

- Observed: `add_server` handles a reply's `missing_secrets` two ways.
  - On a TTY it prompts for each secret with hidden input, stores it with
    `store_cli_secret` (a direct database write in the instance's scope),
    then sends `POST /api/mcp/refresh {server, scope, project_id}`.
  - Off a TTY it prints `needs_configuration` and the reply's `configure`
    lines and exits 0.
- Observed: `call_tool` with `--raw` prints the full reply. Otherwise it
  prints the `result` field as JSON or text. `success: false` goes to stderr
  and exits 1.
- Observed: `src/gobby/servers/routes/configuration_secrets.py::save_secret`
  (`POST /api/config/secrets`) writes only the global scope today. gclient
  never reads the database (client-operator-verbs Constraints), so the
  prompt needs a scoped route. #24467 (runtime-family 5.10.6) adds an
  optional `project_id` to that route and passes it to 1.4's `set_in`. This
  leaf waits on it and does not edit the route.
- Observed: gclient has no helper that locates sibling binaries.
  `gobby_core::gobby_home` names the home whose `bin/` holds `gdaemon`.
- Chosen approach:
  - `write.rs` implements `add-server`, `remove-server`, `import-server`,
    `refresh` and `call-tool` with Python's long options, payloads, timeouts,
    output and exits. It uses 5.1's `client.rs` and scope rules.
  - The `add-server` TTY prompt reads each secret with hidden input and
    sends `POST /api/config/secrets {name, value, category: "general",
    project_id}` in the instance's scope, then the same refresh. The
    `project_id` field comes from #24467 (5.10.6).
  - `remove-server` without `--yes` asks for confirmation on the terminal.
    A refusal or end of input aborts with exit 1, as click's confirmation
    does.
  - `auth.rs` runs `auth` and `oauth-shape` by executing
    `<gobby_home>/bin/gdaemon mcp-auth <name> [--project-id <id>]` with
    inherited stdio.
    - `auth` checks daemon health first and adds `--timeout`. After exit 0
      it sends `PUT /api/mcp/servers/{name} {requires_oauth: true, scope,
      project_id}` and prints `Authorized MCP server: <name>`.
    - `oauth-shape` adds `--shape` and prints the subcommand's JSON.
    - A nonzero subcommand exit returns exit 1 after the subcommand's own
      message.
    - A missing `gdaemon` exits 3 naming the expected path.
  - Golden text for the write verbs is captured from the Python verbs, as in
    5.1.
  - Shipping: load the `rust` skill. This leaf's commit bumps
    `crates/gclient/Cargo.toml` by 0.0.1 and updates `Cargo.lock` and the
    gclient pin in `src/gobby/install/version_pins.py`. It copies no binary.
- Planned verification:
  `cargo nextest run -p gobby-client --test mcp_proxy_verbs`.

**Acceptance:**

- 5.2.1 - `add-server` sends Python's payload for stdio, HTTP and template forms; off a TTY a `missing_secrets` reply prints `needs_configuration` and the configure lines and exits 0; on a pseudo-terminal it prompts, posts each secret with the instance's `project_id`, then posts the refresh. test: `crates/gclient/tests/mcp_proxy_verbs.rs::add_server_matches_python_including_secret_prompt`.
- 5.2.2 - `remove-server` sends the scoped DELETE only after `--yes` or a confirmed prompt, and a refused prompt exits 1 with no request. test: `crates/gclient/tests/mcp_proxy_verbs.rs::remove_server_requires_confirmation`.
- 5.2.3 - `import-server` and `refresh` send Python's payloads, including `cwd`, the multiple `--server` values and the 300-second refresh timeout. test: `crates/gclient/tests/mcp_proxy_verbs.rs::import_and_refresh_match_python`.
- 5.2.4 - `call-tool` merges `--json-args` with the repeated `--arg` values (each `--arg` value JSON-parsed and overriding), sends `offload: false` only under `--raw`, prints the result or full reply, and exits 1 with stderr on `success: false`. test: `crates/gclient/tests/mcp_proxy_verbs.rs::call_tool_matches_python`.
- 5.2.5 - `auth` executes a stub `gdaemon` with `mcp-auth <name>`, the scope and the timeout, sends the `requires_oauth` PUT only after exit 0, and sends nothing after a nonzero exit; `oauth-shape` executes `--shape` without a daemon health check and prints its JSON. test: `crates/gclient/tests/mcp_proxy_verbs.rs::auth_and_shape_exec_gdaemon_mcp_auth`.
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

2026-10-28 — Consensus between the Plan Writer (gobby#18268) and the Plan
Adversary (gobby#18271) on the narrative reviewed at
`e9ce2f79c7ce243db9c973c718a9cb81fe80c71b`. Six blocking findings were
raised and resolved: PA-01 (node-held connections now follow every
lifecycle change through `mcp.disconnect` and `mcp.status`, with an explicit
offline-holder fallback), PA-02 (a typed `MCPCallCancelled` carries
`CANCELLED` through `classify_error`), PA-03 (bounded owner-scoped
tombstones cover every cancel ordering), PA-04 (one deadline with nested
node budgets and cancel on outer loss), PA-05 (secret delivery separated
from non-disclosure, upstream text treated as untrusted content) and PA-06
(each leaf body carries the shared rules, shipping steps and isolated test
commands it needs). No governing decision remains open.

2026-10-28 — Rust-policy revision under #25281 (Plan Writer gobby#18393).
Josh ruled at 08:08 CDT: "If it's not on the hub-node path it needs to be
rust. When we finish 1.0 there will be no python." The Orchestrator accepted
the change list and ruled D1-D6.
- Moved from Python to Rust in this plan:
  - `$secret:` resolution (new 1.4 scoped store, 1.5 resolver);
  - OAuth bearer, 401 recovery, refresh and keepalive (new 1.6);
  - interactive login and DCR as `gdaemon mcp-auth` (new 1.7, D1);
  - placement, the holder set and fan-out (3.3 rewritten in Rust);
  - loopback-browser call policy and untrusted-value cleaning (1.5).
- Old 2.1 (Python bearer helpers) is gone, and old 2.2 became the thin
  transitional forwarder 2.1. The loopback route moved from 1.1 to 1.5, so
  1.1 is pool-only.
- New edges:
  - #24884: discovery, leases, validation, offload and the error contract;
    its new 2.1.9 adds the native cancel route (D4) and the external
    inventory cache, passive `GET /api/mcp/tools` and external
    `schema_hash` (D5).
  - #24151 through #25256's s2-11 7.14 and 7.7: the rules hook.
  - #21570, stated as blocking: registry CRUD, templates, import, sync,
    `recommend_tools`, MCP metrics and deletion of the forwarder before 1.0
    (D5).
  - #25283: `gobby-results` paging (D3).
  - #24466 5.9.10: consumes 1.4's `set_in` (D2).
  - #24467 5.10.6, an owner edit by the Orchestrator: the optional
    `project_id` on `POST /api/config/secrets` that 5.2 sends. 5.2 is
    blocked by #24467.
  - #21564: placement precedent.
- New P5 carries the `gclient mcp-proxy` CLI row that client-operator-verbs
  assigns to #21566 (D6).
- The stale M1 manifest was removed because every section hash changed; the
  Plan Adversary derives a new one from the consensus bytes.

## V2 Verification
`kind: verification`

- Base plan validation: `uv run gobby plans validate .gobby/plans/external-mcp-transport-multiplexer.md -p /Users/josh/Projects/gobby`.
- Expansion validation stays unrun until the Plan Adversary derives and applies
  `## M1 Task Manifest` from the consensus bytes.
- Each deliverable owns its focused checks. No full pytest or whole-workspace
  Rust suite is required.
