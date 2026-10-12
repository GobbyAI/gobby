Plan artifact: `.gobby/plans/gdaemon-node-channel.md`

# Node channel, relay backend, and `/api/machines`

**Plan ID:** gdaemon-node-channel

## Overview
`kind: framing`

This plan specifies #23274 (Node channel, relay backend, and `/api/machines`).
That task is deferred section D2 of `.gobby/plans/gdaemon-api-keys-nodes.md`
(S1.4, #21555), which carries section 4.4 of the plan of record
`.gobby/plans/gdaemon-front-door.md`. Its leaves expand under #23274, following
the #23273 precedent. The parent plan's D2 text stays unchanged. In this plan,
"api-keys D2.n" names the parent's acceptance items, and "key-cutover" names
`.gobby/plans/gdaemon-key-cutover.md` (#23273, Hub-side key validation,
front-door identity, and shared-token cutover). Its code landed on `0.5.0`
through #23519 and #23520, and #23523 (Live activation of the key cutover)
closed on 2026-10-11. #23457 (Atomic gdaemon lifecycle ownership and native
standby cutover) also closed on 2026-10-11. Its landed runtime is this plan's
baseline (see Landed #23457 Baseline).

**The problem.** A node bootstrap (`datastore_mode: remote`) can enroll against
a hub (`gobby auth login`, 4.5), but nothing useful runs on the node
afterwards:
- gcore's bootstrap parser derives a run mode, but unlike Python it accepts
  `(remote, hub: true)`, neither requires nor normalizes `hub_daemon_url`, and
  has no `hub_cert`.
- On a node, `gdaemon serve` takes no lease, but
  `crates/gdaemon/src/state.rs::RuntimeState::prepare` still builds the
  backend supervisor. So the node launches the Python daemon that 4.6 refuses
  on a node. Python's own refusal (#23269) has not reached `0.5.0`; #24075
  lands it.
- The node's front door can only proxy to loopback Python. A node bootstrap
  carries no `database_url`, so the front door answers every `gobby_` bearer
  with 503 `key_resolver_unavailable`.
- The hub has no record of which nodes are live, and no route lists machines.

**When the leaves close:**
- gcore's bootstrap parser derives the run mode and the normalized
  `hub_daemon_url` exactly as Python does, rejects `(remote, true)`, and
  carries `hub_cert` (1.1).
- A node's `gdaemon serve` takes only the PID claim. It starts no lease,
  backend, key resolver, retention task, or terminal host, and it answers the
  interactive handshake challenge itself with its own key (1.2).
- A node's front door relays every other HTTP request and WebSocket upgrade to
  its hub over one pooled connection pinned to `hub_cert` (1.3).
- The node holds a WebSocket channel to the hub. The hub registers it,
  records heartbeats, replaces it atomically on reconnect, and closes it when
  the node's key is revoked (1.4).
- `GET /api/machines` and `GET /api/machines/{id}` list the caller's machines
  with their connection state (1.5).
- A hub-node pair test proves the whole path over self-signed TLS (1.6).

## Decision Record
`kind: framing`

Plan Writer decisions on 2026-10-05, refreshed on 2026-10-10 against the landed
key cutover and on 2026-10-11 against the landed #23457 runtime. They draw on
the parent's settled D2 design, the plan of record's 4.4, and the PD
disposition in `.gobby/plans/gdaemon-front-door.md` (lines 430-446). The
Orchestrator flags Decisions 2 and 3 to Josh at approval, because they change
behavior that the approved key-cutover plan specified.

1. **Every leaf builds on landed code, with no external edge.** Each leaf
   builds on the landed key cutover and the landed #23457 runtime:
   - 1.1 extends `HubDatabaseBootstrap`, which already carries `api_key`,
     `mode`, and a manual `Debug`.
   - 1.2 to 1.5 edit `state.rs`, `serve.rs`, `front_door/mod.rs`, and the
     gdaemon test harness around `state::RuntimeState`,
     `front_door::auth::AuthState`, `front_door::challenge::answer`, and the
     resolved identity headers.
   - 1.6 runs against a hub whose front door validates keys.

   The Orchestrator ruled on 2026-10-05 at 12:11 CT that
   `docs/contracts/plan-coverage.md` (lines 386-389) has no exception for
   external edges. So the six deliverables stay active, fully specified leaves
   with no external edges and no typed deferrals. Expansion waited for #23523
   (Live activation of the key cutover), which closed on 2026-10-11, so that
   gate is met. #23523 changed no source in `crates/gdaemon` or `crates/gcore`
   (Orchestrator, 2026-10-10). On 2026-10-11 the Orchestrator ruled that this
   plan targets the landed #23457 code, and that #24075 (lands #23269's node
   refusal on `0.5.0`) is an edge the Orchestrator adds itself. This plan
   carries no edge or deferral for #24075, and no leaf needs it: after 1.2 a
   node never launches Python.
2. **Node mode supersedes key-cutover Decision 15 and is exempt from its
   Decision 6.** A node's gdaemon validates nothing and holds no table. It
   builds no `AuthState` at all: no key resolver and no front-door secret,
   because no Python backend sits behind it to trust that secret. It forwards
   the bearer untouched. The hub's front door validates the key and forwards
   the resolved identity to the hub's Python. So a node never answers 503
   `key_resolver_unavailable`; it relays (1.3). Since #23457, `gdaemon serve`
   refuses a standalone or hub bootstrap without `database_url`
   (`state.rs::RuntimeState::prepare`). So only an in-process `serve` door
   lacks a key resolver, and that door still answers a `gobby_` bearer with
   that 503 (`crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`).
3. **The interactive handshake challenge stays native on a node.** gcore's
   grant client checks the proof against its own bearer: `expected =
   HMAC-SHA256(HMAC-SHA256(bearer, "gobby-interactive-proof-v1"), nonce)`,
   hex-encoded (`crates/gcore/src/grant/handshake.rs`, lines 217-224). On a
   node that bearer is the node's `api_key`. If the challenge were relayed,
   the hub would sign it with the hub's key and the client would refuse. So a
   node sends `POST /api/runtime/handshake/challenge` to
   `crates/gdaemon/src/front_door/challenge.rs::answer`, which reads `api_key`
   from the node's own bootstrap and answers the `interactive` kind with that
   derivation. `answer` already passes the `managed` kind through unchanged,
   and the node relays it, together with the authenticated handshake that
   follows the challenge. The plan of record already routes the interactive
   challenge natively in all modes (`.gobby/plans/gdaemon-front-door.md`, line
   972); this decision keeps that route on a node instead of relaying it.

   1.6 proves the split against the real hub consumer: the native challenge,
   then the relayed authenticated handshake, which returns a grant whose
   principal carries the node's machine id. That holds because
   `src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`
   (line 115) requires the body's `machine_id` to equal the forwarded machine.
4. **A node holds no datastore credentials.** The S1.4 roadmap row
   (`docs/architecture/gobby-v1.0.0-roadmap.md`, "Nodes hold no datastore
   credentials"), key-cutover Decision 15, and the bootstrap that
   `tests/e2e/test_auth_login_e2e.py::_node_home` writes all agree that a node
   has no `database_url`. api-keys Decision 2's remark that "a node's
   `database_url` is the hub's" describes the pre-4.6 lease hazard, not a
   requirement. In node mode gdaemon never reads `database_url`: it takes no
   lease and builds no key resolver, retention or terminal pool, or node pool,
   even when the key is present.
5. **Relay semantics.** Each rule names the behavior it preserves.
   - **Target.** The HTTP listener relays to `hub_daemon_url` with the path and
     query unchanged.
   - **`/ws` rewrite.** The WebSocket listener (`websocket_port`) relays every
     upgrade to `<hub_daemon_url>/ws` with the query kept. The hub mounts `/ws`
     and `/ws/{path}` on its HTTP port and discards the path
     (`src/gobby/servers/_app_ui.py::_mount_ws_endpoint`), so no second hub
     port is configured.
   - **`Host`.** `Host` is rewritten to the hub's authority, because the hub's
     Python sees the request as addressed to itself.
   - **Break-glass.** The node removes `X-Gobby-Break-Glass` from every relayed
     request and upgrade. `observe_peer` still runs on the node, and the hub
     overwrites `X-Forwarded-For` with its own transport peer. That peer is
     loopback whenever the node shares the hub's host and dials it at a
     loopback address, over `http` (Decision 6) or `https` (the pair test
     dials `https://127.0.0.1`). Python admits break-glass for a matching
     header from a loopback peer
     (`src/gobby/servers/auth_service.py::AuthService.break_glass_admits`), and
     the hub's front door forwards the header untouched: it strips only
     `front_door::auth::IDENTITY_HEADERS`. So only the node's removal keeps
     break-glass from being admitted through a node. Break-glass is
     host-local, and direct loopback break-glass at the hub is unchanged.
6. **Pinning.**
   - An `https` hub requires `hub_cert`. The relay and the channel trust only
     that certificate, through `front_door/tls.rs::pinned_client_config`.
   - An `http` hub, which `gobby auth login` permits for loopback hubs or with
     `--insecure`, is relayed in plaintext. `hub_cert` is ignored there,
     because login removes it for such hubs.
   - A node refuses to start without `api_key` (it has not enrolled; 1.2), or
     with an `https` hub and no `hub_cert` (1.3). Both messages name
     `gobby auth login`.
7. **The typed 503 names the relay target.** `health::unavailable` and
   `health::bad_gateway` take `target: impl std::fmt::Display` and render it
   with `to_string()`, as today. Every existing caller passes a `SocketAddr`
   unchanged: `proxy::forward`, `ws::splice`, and `routes.rs::compare`. So
   their output is byte-identical and they need no edit for this. A node
   passes `hub_daemon_url`. 1.2 makes the change, because its node front door
   answers everything but the challenge with
   `unavailable(hub_daemon_url, Down)` until 1.3 replaces that arm with the
   relay.
8. **Node routes are native mode routes, not route families.**
   `front_door/routes.rs` families default to `proxy` and forward to Python,
   which has no node or machines routes. So `/api/nodes/channel` and
   `/api/machines` are mounted natively by mode, after authentication and
   ahead of the WebSocket splice and the route table:

   | Mode | `/api/nodes/channel` | `/api/machines` |
   | --- | --- | --- |
   | Hub | served natively | served natively |
   | Standalone | not mounted, so the request reaches Python, which answers 404 | served natively |
   | Node | relayed to the hub | relayed to the hub |

   #23457's runtime gates in `FrontDoor::handle` run before authentication, so
   a standby hub or standalone front door, or one whose backend was refused,
   answers these paths with its 503 as it does every other path.
9. **Channel protocol.**
   - **Open.** The node opens `GET /api/nodes/channel` (WebSocket) with
     `Authorization: Bearer <api_key>`. The hub's front door validates the
     key before the upgrade.
   - **Hello and ack.** The node sends
     `{"type":"hello","node_version":"<gdaemon version>","platform":"<os>/<arch>"}`
     and the hub answers `{"type":"ack","machine_id":"<uuid>"}`. The hub
     validates `platform` and does not store it in Stage 1.
   - **Heartbeat.** The node sends a WebSocket ping every 30 s. Only hello,
     ack, and ping/pong exist in Stage 1; S2.7 and S2.8 add commands.
   - **Close codes.** 4401 when the key is revoked (Python's
     authentication-close code), 4409 when a newer connection replaces the
     channel, 4408 when the hub hears nothing within the idle timeout, 1008
     when the first frame is not a valid hello, and 1011 when admission
     cannot prove the key or record the hello before the ack (Decision 10).
   - **Reconnect.** The node reconnects after any close or error with backoff:
     1 s, doubling to 60 s, and reset after an ack. It reads `api_key` from its
     bootstrap on each attempt, so a rotation (key-cutover 1.4) takes effect
     at the next reconnect.
   - **Every node await is bounded.** An attempt's dial, TLS handshake,
     upgrade, hello send, and ack receipt share one 20 s attempt timeout from
     the attempt's start. After the ack, the node treats the hub as gone when
     it receives no frame within the 90 s idle timeout. The hub answers each
     ping with a pong, so a live hub resets that timer every ping interval.
     Each ping send runs under the same idle deadline, so a stalled write
     cannot hold the loop. Either expiry drops the transport and enters the
     backoff above.
10. **Heartbeat bookkeeping on the hub.**
    - The hub writes `machines.last_heartbeat_at` and `node_version` at hello,
      then on a ping only when at least 60 s have passed since its last write.
    - **Revocation runs on the hub's clock, within 29 s.** The Orchestrator
      ruled this schedule on 2026-10-05 at 12:38 CT. It keeps the parent's
      30 s bound (`.gobby/plans/gdaemon-api-keys-nodes.md`, lines 1020 and
      1027) and NC-E05 unchanged:
      - Peer traffic does not drive revocation. The hub re-reads the key row
        every 25 s (the key-recheck interval), on ticks anchored at the
        upgrade, whether or not the peer sends anything.
      - Each key check has a hard 4 s deadline (the recheck deadline) covering
        both pool acquisition and the query.
      - A revoked or missing row removes the channel's registry entry and
        then closes it with 4401.
      - So a revocation closes the channel within 25 s + 4 s = 29 s at any
        phase, including during hello and before the ack. A silent peer, or
        one sending only Pong frames, is closed the same way.
    - **Admission proves the key before the ack.** The upgrade's key check
      happens in the front door. The hub then waits for hello, registers the
      machine, and writes its hello bookkeeping, all within the 10 s hello
      timeout from the upgrade. It checks the key once more under the 4 s
      deadline and only then sends the ack. The ack send must finish by the
      admission end, which is the upgrade plus the hello timeout plus the
      recheck deadline (14 s). A send still pending then removes the entry
      and drops the transport. Admission therefore ends within 14 s, before
      the first recheck tick at 25 s.
      - A recheck tick that finds the key revoked shares one absolute cycle
        deadline, the tick plus the recheck deadline, between its check and
        its close. The handler removes the entry at once and tries the 4401
        close only for the budget that remains. It then drops the transport
        at that deadline, so teardown stays within 29 s.
      - Every other close-frame send, during admission or after it, is
        bounded by its own recheck deadline. The handler then drops the
        transport, so no close path stalls the handler.
      - Each close that ends a registered connection's entry follows that
        entry's removal.
      - A key revoked at any point before that check closes 4401 with no ack.
      - A missed hello timeout, a failed hello write, or a check that errors
        or times out closes 1011 with no ack. An unproven key never yields an
        acknowledged channel.
      - A registry entry reads `connected` only after its ack send succeeds,
        and only while it is still the machine's current connection.
      - Admission watches for replacement at every await, including the
        pre-ack check and the ack send. A connection replaced there closes
        4409, sends no ack once it observes the replacement, and never reads
        `connected`. A failed ack send leaves no registry entry.
    - **The database-error exception.** On an established channel, a recheck
      that errors or misses its 4 s deadline keeps the channel open, and the
      next tick retries it. This is the one case in which the 29 s bound can
      be exceeded: relayed requests already fail closed through the
      resolver's 503, and the channel carries no commands in Stage 1.
    - **Bookkeeping never blocks the key timer.** Every `machines` write runs
      in a spawned task, and the channel's select loop never awaits one. At
      most one heartbeat write is in flight per connection. A due write is
      skipped while the previous one is still running.
    - The hub closes a channel that sends nothing within 90 s (three ping
      intervals), so a partitioned node stops reading as `connected`. Any
      received frame resets that timer. It governs liveness only, not
      revocation.
    - **A replaced connection cannot overwrite its successor's bookkeeping.**
      - The registry keeps one async write gate per machine, shared by that
        machine's successive connections, for the hub's lifetime.
      - A spawned write takes the gate and runs only if the registry still
        names its `connection_id`. It holds the gate until the database
        operation actually completes, even after its channel has closed.
      - A write in flight at replacement therefore finishes before the
        successor's hello write, which waits on the gate. A write that starts
        after replacement is skipped.
      - The successor sends its ack only after its own hello write. No schema
        field is added.
11. **Timings are constructor arguments, not configuration.**
    - `ChannelTiming` carries the ping interval, the backoff start and
      ceiling, the node's attempt timeout, the hub's hello timeout, the idle
      timeout, the key-recheck interval and deadline, and the heartbeat write
      interval. `ChannelTiming::default()` holds the values above. The node's
      20 s attempt timeout covers the hub's 14 s admission plus 6 s for the
      dial, TLS handshake, and upgrade. A slower attempt ends without an ack
      and backs off; the hub's ack send then fails, or its read sees the
      close, and either path removes the entry (Decision 10).
    - Tests build short timings.
    - No bootstrap key is added.
12. **`/api/machines` follows the API-key routes' house style.**
    - Scope. It returns only rows whose `owner_user_id` is the caller's
      resolved user, as `routes/api_keys.py::list_keys` does.
    - Shapes. The list is `{"machines": [...]}` and the item route returns the
      object.
    - Fields. Each row has `id`, `ref`, `hostname`, `os`, `label`,
      `tailscale_name`, `first_seen`, `last_seen`, `last_heartbeat_at`,
      `node_version`, and `connected`. Timestamps are RFC 3339 or `null`.
    - `connected` means "this hub's registry holds a live channel for the
      machine", and nothing else. A machine with no channel reads `false`.
      The hub's own machine has no special case: admission keys only on the
      bearer's machine, so its row reads `false` unless a process holding
      that machine's key opens a channel, and then `true` is accurate.
    - Errors use gdaemon's existing body `{"error": msg, "code": code}`:
      - a request with no resolved key identity is 401 `missing_auth`;
      - an unknown, foreign, or malformed id is 404 `machine_not_found`;
      - a query error, or a standalone door with no pool, is 503
        `machines_unavailable`. `gdaemon serve` always gives a standalone door
        a pool, because it refuses a standalone bootstrap without
        `database_url` (Decision 2). Only an in-process `serve` door has no
        pool, and it has no key resolver either, so no caller there carries a
        key identity and it answers only 401 in practice.
13. **Dependencies.** These follow the `crates/gdaemon/Cargo.toml` rule "ring
    only … aws-lc-rs stays out of the build". Every crate below is already in
    `Cargo.lock`:
    - 1.3 adds
      `hyper-rustls = { version = "0.27", default-features = false, features = ["http1", "ring", "tls12"] }`
      (lockfile 0.27.7), for the pooled pinned relay client.
    - 1.4 adds `"ws"` to gdaemon's `axum` features (lockfile 0.8.9), for the
      hub endpoint, and
      `tokio-tungstenite = { version = "0.26", default-features = false, features = ["handshake"] }`
      (lockfile 0.26.2), for the node client over a stream gdaemon dials and
      pins itself.
    - 1.4 moves `futures-util = "0.3"` from gdaemon's `[dev-dependencies]`
      (`crates/gdaemon/Cargo.toml`, line 53) to `[dependencies]`, without
      adding another stream abstraction, and updates `Cargo.lock` if
      resolution changes.
    - 1.3 builds the pooled client from a clone of the pinned config with its
      ALPN cleared, because hyper-rustls asserts an empty list; the original
      config keeps `http/1.1` for manually dialed streams.
14. **Run-mode parity uses one shared vector file.**
    - `tests/fixtures/bootstrap_run_modes.json` holds bootstrap mappings with
      either the expected mode and normalized `hub_daemon_url` or the expected
      error text. The Python and Rust tests both read it, following the
      `tests/fixtures/terminal_ws_golden` precedent that
      `crates/gdaemon/tests/ws_golden_proxy.rs` reads through
      `CARGO_MANIFEST_DIR`.
    - Rust `parse_hub_origin` lowercases the host, as Python's `urlparse`
      `hostname` does.
    - `parse_files_home_view` rejects `hub_daemon_url` on a local bootstrap
      with "hub_daemon_url is not allowed on a local bootstrap"
      (`crates/gcore/src/bootstrap.rs`, line 328), without Python's "; this
      process is the owner" suffix. 1.1 changes that literal to Python's text,
      so every Rust parser reports the rule identically. No test asserts the
      old text.
    - #23457's inline mode match reports a non-boolean `hub` as "bootstrap hub
      must be a boolean" and an unknown `datastore_mode` as "bootstrap
      datastore_mode must be local or remote". 1.1 adopts Python's "hub must
      be a boolean" and "datastore_mode must be one of: local, remote", and the
      vectors cover both. The only test of those errors,
      `crates/gcore/tests/bootstrap_mode.rs`, checks only that they fail.
    - Every local vector carries `files_home`, which only Python's parser
      requires.
15. **No activation section.** Each leaf validates against isolated daemons
    and never restarts the running daemon. The Orchestrator promotes the
    coherent binary set and restarts under the per-landing restart rule
    (`.gobby/roles/roster.md`, `2940eb9d01`). No node runs today, and an
    operator starts one by hand with `gdaemon serve`. #23269's Python refusal
    of a node bootstrap is not on `0.5.0` at `093e210fb7`: its commit
    `91a483db0d` never landed, and #24075 lands it (Decision 1). After 1.2 a
    node's `gdaemon serve` never launches Python, whether or not that refusal
    has landed.
16. **The landed #23457 runtime is the mode-aware owner, and no second
    container exists.** #23457 landed `crates/gdaemon/src/state.rs::RuntimeState`
    and gcore's `RunMode` instead of the `AppState` and `ModeServices` that
    this plan's 2026-10-10 settlement specified. #23457's task records the
    re-settlement (gobby#16181, through Lane 2's gobby#15997):
    - gcore owns one `RunMode` parser that matches Python;
    - gdaemon's state owns the PID claim, the lease, and the backend
      lifecycle;
    - a node holds no `ActiveDaemonLease`;
    - #23274 consumes that parser and state and creates no second container.

    So this plan builds no `AppState`:
    - 1.1 completes the landed parser's Python parity in place and adds no
      mode enum.
    - 1.2 makes `RuntimeState::prepare` return `Prepared::Node(PidFileClaim)`
      for a node, before any lease or supervisor exists, and
      `Prepared::Backend(Arc<RuntimeState>, BackendSupervisor)` otherwise. A
      node's own services live in `state.rs::NodeServices`, which 1.3 extends
      with the relay.
    - Hub and standalone native routes ride `FrontDoorState`, which already
      carries the runtime projection. 1.4 adds a `native: nodes::NativeRoutes`
      field that `FrontDoorState::new` defaults, so `FrontDoor::new` and the
      public `serve` keep their signatures and the in-process callers need no
      edit.
    - The parent's D2 prose names `AppState` as the mode container
      (`.gobby/plans/gdaemon-api-keys-nodes.md`, line 1003). No api-keys item
      D2.1 to D2.7 names it, so this plan maps every parent item (Parent D2
      Mapping) without building it.
17. **The node routes build their own pool.**
    `front_door::auth::PostgresKeyResolver` keeps its pool private and wrapped
    in `Result`, and `crates/gdaemon/src/state_services.rs::ActiveServices::start`
    builds the retention and terminal pools only while the lease is active.
    The node routes follow the own-pool precedent: one
    `gobby_core::postgres_pool::Pool` per process, built once in
    `serve.rs::run`'s backend arm, with `max_size` 4, `application_name`
    `gobby-gdaemon-nodes`, and `acquire_timeout` 2 s, inside the 4 s recheck
    deadline. It lives as long as the process, because #23457's runtime gates
    already answer these routes with 503 while the lease is inactive
    (Decision 8). `Pool` is not `Clone`, so it is shared through `Arc`. Lane
    2's developer raised no objection (2026-10-10).

## Landed #23457 Baseline
`kind: framing`

#23457 (Atomic gdaemon lifecycle ownership and native standby cutover) closed
on 2026-10-11 and is this plan's baseline. It supersedes points (a) and (b) of
this plan's 2026-10-10 Lifecycle Ownership Settlement: no `AppState` or
`ModeServices` exists, and no `#23457 blocked_by` edge is needed. Points (c)
and (d) survive as the rules at the end of this section.

**What landed.**
- `crates/gcore/src/bootstrap.rs`: `HubDatabaseBootstrap` carries
  `mode: RunMode` and a raw `hub_daemon_url: Option<String>`, and
  `RunMode::{Standalone, Hub, Node}` exists. `parse_hub_database_bootstrap`
  derives the mode inline. `crates/gdaemon/src/lease/mod.rs` re-exports
  `RunMode` as `LeaseMode`, and `ActiveDaemonLease::new` refuses a node.
- `crates/gdaemon/src/state.rs::RuntimeState { mode, lease, lease_state,
  backend }`:
  - `RuntimeState::prepare(bootstrap, home, secret)` claims
    `<home>/gobby.pid` first and returns `Ok(None)` when another process holds
    it. For standalone and hub it then takes an `ActiveDaemonLease`, which
    requires `database_url`. It builds a `BackendSupervisor` that owns the
    claim in every mode, node included.
  - `RuntimeState::run` loops on the lease, starts
    `state_services.rs::ActiveServices` (the retention task and the terminal
    host, each over its own pool) while the lease is active, and runs the
    supervisor.
- `crates/gdaemon/src/serve.rs::run` generates the front-door secret (it no
  longer reads `GOBBY_FRONT_DOOR_SECRET`), loads the bootstrap, and builds the
  key resolver and `AuthState` in every mode. It then calls `prepare`, binds
  both public ports, spawns the owner task, and serves through the private
  `serve_with_runtime`. The public
  `serve(listeners, routes, tls, auth, shutdown)` passes no runtime.
- `FrontDoorState` holds `runtime: Option<Arc<RuntimeState>>`.
  `FrontDoor::handle` runs `observe_peer`. With a runtime it then serves the
  native lease-admin routes (`lease::admin_api`) and answers 503 for a refused
  backend and for standby. Then it runs the native challenge,
  `AuthState::authenticate`, and `ws::splice` or `RouteTable::dispatch`.

**What this plan changes on that baseline.**
- 1.1 completes the parser's Python parity (Decision 14).
- 1.2 fixes the node path. `prepare` returns before the supervisor for a node,
  so a node never launches Python, and `run` builds `AuthState` only for a
  backend mode.
- 1.4 and 1.5 add `FrontDoorState.native` and serve the native routes after
  authentication, so #23457's gates answer first.

**Rules that survive the settlement.**
- (c) Each plan edits only its own code. `RuntimeState`'s lease and backend
  lifecycle, `ActiveServices`, the lease-admin routes, and the runtime gates
  stay #23457's. This plan adds the `Prepared` split, `NodeServices`,
  `serve_doors`, the node door, `FrontDoorState.native`, and the native
  routes. Whichever change in these files lands second rebases onto the
  other's code.
- (d) Node mode takes the same PID claim as the other modes and nothing else:
  no lease, backend, front-door secret, retention task, or terminal host. The
  claim stays #23457's code; 1.2 only returns earlier.

## Constraints
`kind: framing`

- **Isolation.**
  - Every pytest run uses
    `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
    from the main checkout root, never the full suite.
  - Rust database cases follow `crates/gdaemon/tests/lease.rs::database_case`.
    That helper requires the protected test hub, creates a throwaway schema,
    and skips only when `DATABASE_URL` is unset. Validation therefore always
    sets `DATABASE_URL`.
  - No leaf restarts the running daemon, reads or writes
    `~/.gobby/bootstrap.yaml` or `~/.gobby/local_cli_token`, or touches port
    60891.
- **Rust.**
  - Load the `rust` skill before editing `crates/`, where `crates/CLAUDE.md`
    governs.
  - `cargo build`, `clippy`, and `nextest` are heavy work. They run under the
    heavy-run cap the Release Manager grants and pause while the heavy-work
    hold is in force.
  - After each dependency change, `cargo tree -p gobby-daemon -i aws-lc-rs` prints
    nothing.
- **No secret values in logs.** No new code logs the API key, a relayed
  `Authorization` value, or a channel bearer. The manual `Debug` on
  `HubDatabaseBootstrap` redacts `api_key`, and the fields 1.1 adds to it are
  not secret.
- **No backward compatibility.** 0.5.0 has not shipped.
- **Monolith ceiling.** Measured on `0.5.0` at `093e210fb7`, production lines
  before the first `#[cfg(test)]`:
  - gdaemon `serve.rs` 383 of 425, `state.rs` 129, `front_door/mod.rs` 181,
    `front_door/health.rs` 105 of 152, `front_door/proxy.rs` 114 of 137,
    `front_door/ws.rs` 66, and `front_door/challenge.rs` 144.
  - gcore `bootstrap.rs` 416 of 963, and `bootstrap/front_door.rs` 282.

  `crates/AGENTS.md` counts inline tests toward the 1,000-line ceiling, so
  `bootstrap.rs` (963 lines in total) takes no new parsing logic. 1.1 puts it
  in a new `bootstrap/run_mode.rs`, and `bootstrap.rs` stays under 1,000 lines
  in total. No leaf brings a targeted file near 850 production lines. Each
  leaf re-measures and splits any file it would push past that.
- **Landed key-cutover names.** `front_door::auth::{AuthState, KeyIdentity,
  KeyResolver, PostgresKeyResolver, IDENTITY_HEADERS}`,
  `front_door::challenge::answer`, and
  `gobby_core::local_token::read_api_key_at` exist on `0.5.0`. The identity
  headers are `x-gobby-user-id`, `x-gobby-machine-id`, `x-gobby-key-id`, and
  `x-gobby-front-door`.
- **Landed #23457 names.** `state::RuntimeState::{prepare, run, standby}`,
  `state_services::ActiveServices`,
  `lifecycle::pid_file::{claim_from_environment, PidFileClaim}`,
  `lifecycle::backend::{BackendSupervisor, BackendStatus}`, `lease::admin_api`,
  and `FrontDoorState.runtime` exist on `0.5.0`.
- **Consumer sweeps.**
  - Targets were swept read-only on `0.5.0` at `093e210fb7` (2026-10-11).
  - `gcode grep -w` and `-F` for `HubDatabaseBootstrap {`, `RunMode`,
    `hub_daemon_url`, `datastore_mode`, `FrontDoor::new`,
    `FrontDoorState::new`, `FrontDoorState {`, `.runtime =`,
    `RuntimeState {`, `RuntimeState::prepare`, `claim_from_environment`,
    `serve_with_runtime`, `AuthState::new`, `PublicListener`, `serve(`,
    `CARGO_BIN_EXE_gdaemon`, `unavailable(`, `bad_gateway(`,
    `read_files_home_view`, `FilesHomeView`, and `yaml_bool`.
  - Beyond each leaf's research context, the sweep found the following:
    - `RuntimeState::prepare`'s only caller is `serve.rs::run`.
    - The only `RuntimeState` literal is in `crates/gdaemon/tests/lease.rs`
      (line 58), which builds a standalone projection that no leaf changes.
    - No `FrontDoorState` literal exists outside `serve_with_runtime`'s
      functional update.
    - Every spawned `gdaemon serve` test is in
      `crates/gdaemon/tests/front_door.rs` (lines 312, 994, and 1715). Two of
      them write node bootstraps (1.1, 1.2).
  - `gcode grep` for `/api/machines`, `/api/nodes`, `hub_cert`, and
    `last_heartbeat_at`.
  - No `/api/machines` or `/api/nodes` consumer exists in `src/`, `crates/`,
    `tests/`, or `web/`.

## Parent D2 Mapping
`kind: framing`

Every api-keys D2 item maps to an item here:

| api-keys item | Here | Note |
| --- | --- | --- |
| D2.1 (4.4.1) | 1.3.1 | |
| D2.2 (4.4.2) | 1.4.1 | |
| D2.3 (4.4.3) | 1.4.2 | |
| D2.4 (4.4.4) | 1.5.1 | A test replaces the plan of record's file artifact. |
| D2.5 (4.4.5) | 1.6.1 | The "no maintenance loop" clause is dropped (PD disposition, plan of record lines 437-441). |
| D2.6 (4.4.6) | 1.3.3 | |
| D2.7 | 1.1.1, 1.1.2 | Rust in 1.1.1; Python on the same vectors in 1.1.2. |

## P1: Node relay, channel, and machines API
`kind: framing`

**Goal**: an enrolled node reaches its hub through a pinned relay and a live
channel, and the hub can list machines with their connection state.

**Granularity:** six leaves. Mode parsing (1.1) is a gcore parser with its own
parity vectors. Node startup (1.2) decides which lifecycle owners a node
starts, and a spawned `gdaemon serve` tests it on its own. The relay (1.3) and
the channel (1.4) are separate lifecycle owners: per-request forwarding versus
a long-lived registry and a reconnecting client. Each is independently
testable. The machines API (1.5) is a read model over the registry. The pair
test (1.6) needs all of them. 1.3 and 1.4 each name more than six Target
files, because a new module ships with its registration, its wiring in
`state.rs` or `nodes/mod.rs`, `front_door/mod.rs`, and `serve.rs`, and its
`Cargo.toml` change; splitting them would leave an unwired module. Each leaf
stays at or below six acceptance items.

### 1.1 Rust bootstrap derives the run mode the way Python does [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: `HubDatabaseBootstrap` gains `hub_cert`, and its manual `Debug` prints `mode`, `hub_daemon_url`, and `hub_cert`; `parse_hub_database_bootstrap` takes the mode and the normalized `hub_daemon_url` from `run_mode::parse_mode_fields` and reads `hub_cert`; `parse_hub_origin` lowercases the host; `parse_files_home_view` takes Python's local-rejection text; declares `mod run_mode`
- `crates/gcore/src/bootstrap/run_mode.rs`
- `crates/gcore/src/bootstrap/front_door.rs::yaml_bool`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the default `HubDatabaseBootstrap` literal in `load_enabled_bootstrap` gains `hub_cert: None`; later leaves edit the same file under `::*`
- `crates/gcore/tests/bootstrap_mode.rs::native_mode_matches_python_bootstrap`
- `crates/gdaemon/tests/front_door.rs::spawn_serve`
- `crates/gdaemon/tests/front_door.rs::spawn_tls_serve`
- `tests/fixtures/bootstrap_run_modes.json`
- `crates/gcore/tests/bootstrap_run_modes.rs`
- `tests/config/test_bootstrap_run_modes.py`

**Research context:**
- `crates/gcore/src/bootstrap.rs` (963 lines in total, inline tests from line
  417) defines the following:
  - `HubDatabaseBootstrap` (line 42) has `mode: RunMode`, `hub_daemon_url:
    Option<String>`, `database_url`, `api_key`, `daemon_url`, `bind_host`,
    `daemon_port`, `websocket_port`, and `front_door`. Its manual `Debug`
    (line 65) redacts `api_key` and omits `mode` and `hub_daemon_url`.
  - `RunMode::{Standalone, Hub, Node}` (line 57).
  - `parse_hub_database_bootstrap` (line 207) reads `bind_host` first, then
    the front-door block. It derives the mode inline: `remote` always yields
    `Node`, even with `hub: true`. A local or absent `datastore_mode` requires
    `hub` to be a YAML boolean ("bootstrap hub must be a boolean"), and any
    other value fails with "bootstrap datastore_mode must be local or
    remote". It reads `hub_daemon_url`, `database_url`, and `api_key` as
    unvalidated optional strings.
  - `FilesHomeView` (line 99) and `parse_files_home_view` (line 299) already
    parse `datastore_mode` as `DatastoreMode::{Local, Remote}`, and
    `hub_daemon_url` through `parse_hub_origin` (line 377).
    `parse_hub_origin` trims a trailing `/` and rejects `@`, `?`, `#`, and
    `/`, but it neither lowercases the host nor checks this process's own
    origin. `parse_files_home_view`'s local rejection (line 328) lacks
    Python's suffix (Decision 14).
  - `read_files_home_view` has no caller outside `bootstrap.rs`;
    `.gobby/plans/gobby-backup.md` plans its first consumer, so it stays.
  - Struct literals of `HubDatabaseBootstrap` exist only in
    `parse_hub_database_bootstrap` and
    `crates/gdaemon/src/serve.rs::load_enabled_bootstrap` (line 152).
  - Readers of `mode` are `crates/gdaemon/src/state.rs::RuntimeState::prepare`
    and the `lease::LeaseMode` re-export. The type and values of `mode` stay
    unchanged, so this leaf edits neither; 1.2 edits `state.rs` for the node
    path. Nothing reads `hub_daemon_url` yet.
- Python (`src/gobby/config/bootstrap.py`) derives the mode as follows:
  - `BootstrapConfig.run_mode()` (line 119) returns `node` for `remote`, then
    `hub` when `hub` is set, else `standalone`.
  - `bootstrap_from_mapping` (line 194) parses `hub` with `_parse_yaml_bool`
    (line 270), which raises "hub must be a boolean", and reads `hub_cert` as
    an optional string in every mode. `_parse_datastore_mode` (line 517)
    raises "datastore_mode must be one of: local, remote".
  - `_parse_mode_owner_fields` (line 404) applies these rules:
    - On local it rejects `hub_daemon_url` with "hub_daemon_url is not allowed
      on a local bootstrap; this process is the owner" and requires
      `files_home`.
    - On remote with `hub` set it raises "hub: true requires datastore_mode:
      local".
    - On remote it requires `hub_daemon_url` and rejects this process's own
      origin with "hub_daemon_url must not be this process's own origin".
  - `_own_origins` (line 464) collects `http` and `https` at
    `bind_host:daemon_port`, plus `daemon_url`.
  - `_parse_hub_daemon_url` (line 446) lowercases the host through `urlparse`
    and brackets an IPv6 host.
- Probed on `0.5.0` (Python is unchanged since):
  - `{datastore_mode: remote, hub_daemon_url: "https://HUB.Example:443/"}`
    yields `node` and `https://hub.example:443`.
  - `hub: "yes"` on local yields `hub`.
  - `(remote, hub: true)` raises.
  - A remote bootstrap whose `bind_host: localhost` matches its
    `hub_daemon_url` raises the own-origin error.
- `crates/gcore/src/bootstrap/front_door.rs::yaml_bool` (line 126) is private.
  `bootstrap.rs::front_door_enabled_matches_python` (line 809) already proves
  its parity with `_parse_yaml_bool`.
- Rust tests that the stricter parser reaches:
  - `crates/gcore/tests/bootstrap_mode.rs::native_mode_matches_python_bootstrap`
    (from #23457) expects `datastore_mode: remote` with `hub: true`, and a
    bare `datastore_mode: remote`, to yield `Node`. Python rejects both.
  - `crates/gdaemon/tests/front_door.rs::spawn_serve` (line 970) and
    `spawn_tls_serve` (line 1704) spawn `gdaemon serve` over a
    `datastore_mode: remote` bootstrap with no `hub_daemon_url`, which the
    stricter parser refuses at startup.
  - `bootstrap.rs::reads_bootstrap_with_datastore_mode` (line 484) writes a
    bare remote bootstrap but reads it through `read_daemon_endpoint_at`, which
    this leaf does not change.
- Precedent for a fixture read from a crate test:
  `crates/gdaemon/tests/ws_golden_proxy.rs::corpus` reads
  `../../tests/fixtures/terminal_ws_golden` through `CARGO_MANIFEST_DIR`.

**Implementation:**
- New `crates/gcore/src/bootstrap/run_mode.rs`:
  - `pub(super) fn parse_mode_fields(map, bind_host, daemon_port, daemon_url)`
    returns `(RunMode, Option<String>)`: the existing `super::RunMode` and the
    `hub_daemon_url` normalized by `super::parse_hub_origin`, present only for
    a node. It adds no mode enum.
  - It parses `hub` with `super::front_door::yaml_bool` and applies Python's
    rules and messages: the boolean and `datastore_mode` errors,
    `(remote, true)`, the remote requirement, the local rejection, and the
    own-origin rejection over the same origin set as `_own_origins`.
  - It does not require `files_home`, which gdaemon never reads.
- `bootstrap.rs`:
  - `parse_hub_database_bootstrap` replaces its inline match with
    `run_mode::parse_mode_fields` and reads `hub_cert` with
    `optional_string_field`, in every mode, as Python does.
  - `HubDatabaseBootstrap` gains `hub_cert: Option<String>`, and `Debug`
    prints `mode`, `hub_daemon_url`, and `hub_cert`.
  - `parse_hub_origin` lowercases the host, so `FilesHomeView` and the mode
    parser normalize alike. `parse_files_home_view`'s local-rejection literal
    becomes Python's text (Decision 14).
  - The file stays under 1,000 lines in total.
- `front_door.rs::yaml_bool` becomes `pub(super)`.
- `load_enabled_bootstrap`'s default literal sets `hub_cert: None`.
- `tests/fixtures/bootstrap_run_modes.json` holds a `cases` array. Each entry
  has a `name`, a `bootstrap` mapping, and either `mode` with
  `hub_daemon_url` or `error`. Cases:
  - standalone, and hub with `true` and with `"yes"`;
  - node, with host lowercasing, a trailing slash, and an IPv6 literal hub;
  - `(remote, true)`;
  - a non-boolean `hub` and an unknown `datastore_mode`;
  - remote without `hub_daemon_url`;
  - local with `hub_daemon_url`;
  - own origin, both through `bind_host` and through `daemon_url`;
  - a hub URL with userinfo, with a query, and with a path.

  Local cases carry `files_home`. Errors compare by Python's message text.
- `crates/gcore/tests/bootstrap_run_modes.rs` and
  `tests/config/test_bootstrap_run_modes.py` each load the file and assert
  every case.
- `crates/gcore/tests/bootstrap_mode.rs::native_mode_matches_python_bootstrap`
  gives its remote case a `hub_daemon_url` and moves `(remote, true)` and the
  bare remote bootstrap to its failing cases.
- `spawn_serve` and `spawn_tls_serve` add an `http` loopback
  `hub_daemon_url` on a port other than their own to their node bootstraps.

**Focused verification (planned):**
`cargo nextest run -p gobby-core --test bootstrap_run_modes --test bootstrap_mode`
and `cargo nextest run -p gobby-core bootstrap`, then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gobby-daemon --test front_door`
(heavy work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap_run_modes.py tests/config/test_bootstrap.py -q`.

Consumers unchanged:
- `crates/gcore/src/local_token.rs` — no-edit-reason: `read_api_key_at` reads through `parse_hub_database_bootstrap`, which now also validates the mode fields; every bootstrap that `gobby auth login` or the installer writes already satisfies Python's identical rules.
- `crates/gcore/src/ai/effective_config.rs` — no-edit-reason: reads `database_url` through `postgres_database_url_from_bootstrap_file`, whose field it reads is unchanged.
- `crates/gdaemon/src/main.rs` — no-edit-reason: same `database_url` read as `effective_config.rs`.
- `crates/gdaemon/src/front_door/challenge.rs` — no-edit-reason: reads `api_key` through the same parser; the field is unchanged.
- `crates/gdaemon/src/lease/mod.rs` — no-edit-reason: re-exports the unchanged `RunMode` as `LeaseMode`.

**Acceptance:**

- 1.1.1 - The Rust parser derives the mode and normalized `hub_daemon_url` that every shared vector expects, and reads `hub_cert`. It rejects `(remote, true)`, a non-boolean `hub`, an unknown `datastore_mode`, a remote bootstrap without `hub_daemon_url`, a local one with it, and this process's own origin, each with Python's message. test: `crates/gcore/tests/bootstrap_run_modes.rs::shared_vectors_match_python`.
- 1.1.2 - Python's `bootstrap_from_mapping` and `BootstrapConfig.run_mode` produce the same outcome on every case of the same vector file. test: `tests/config/test_bootstrap_run_modes.py::test_shared_vectors_match_rust`.

### 1.2 Node startup: a node takes only the PID claim and answers the challenge itself [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/state.rs::*` — scope-reason: adds `Prepared` and `NodeServices`; `RuntimeState::prepare` returns `Prepared::Node` for a node right after the PID claim; the module comment describes the node path
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` matches `Prepared`, builds the key resolver and `AuthState` only in the backend arm, and serves node doors; `serve_with_runtime` hands its doors to the new public `serve_doors`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: `FrontDoor` holds a private `Door` enum with a backend arm (today's state and route table) and a node arm; `FrontDoor::node` builds the node arm; in the node arm `handle` sends the handshake challenge to `challenge::answer` with the node's bootstrap path and answers everything else with `health::unavailable(hub_daemon_url, Down)`
- `crates/gdaemon/src/front_door/health.rs::unavailable`
- `crates/gdaemon/src/front_door/health.rs::bad_gateway`
- `crates/gdaemon/tests/run_modes.rs`
- `crates/gdaemon/tests/front_door.rs::spawn_serve`
- `crates/gdaemon/tests/front_door.rs::spawn_tls_serve`
- `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_node_mode_fail_closed`

**Research context:**
- `crates/gdaemon/src/state.rs::RuntimeState::prepare` (line 27):
  - It claims `<home>/gobby.pid` through
    `lifecycle::pid_file::claim_from_environment` and returns `Ok(None)` when
    another live process holds the claim.
  - For standalone and hub it reads `database_url` (else "native runtime
    ownership requires bootstrap database_url"), the machine id, and the
    deployment token, and takes an `ActiveDaemonLease`. For a node it takes
    none.
  - In every mode it then builds `BackendConfig::from_environment(home)`, sets
    `GOBBY_FRONT_DOOR_SECRET` in that config's environment, and builds
    `BackendSupervisor::with_lease(lease, claim, config)`, which owns the
    claim from then on.
  - `RuntimeState::run` (line 64) starts `ActiveServices` and runs the
    supervisor, which launches `GOBBY_BACKEND_COMMAND` or else Python's
    runner. So a node launches Python today.
- `lifecycle::pid_file::PidFileClaim` (line 65) holds the lock until it is
  dropped or released.
- `crates/gdaemon/src/serve.rs::run` (line 53) does the following:
  - It generates the secret, loads the bootstrap, and builds
    `PostgresKeyResolver` when `database_url` is set, then
    `AuthState::new(secret, resolver, path)`, in every mode.
  - It computes the backend ports, warns about unimplemented route families,
    and loads TLS.
  - Inside the runtime it calls `prepare`, binds both public ports with
    `bind`, spawns `owner_run`, and calls
    `serve_with_runtime(listeners, routes, tls, auth, Some(state), shutdown)`.
    The shutdown future completes on `shutdown_signal()` or when the owner
    task ends.
- `serve.rs::serve_with_runtime` (line 252) starts the `HeartbeatHost`. For
  each listener it builds a `FrontDoorState` carrying the runtime, a
  `RouteTable`, and an `accept` loop over `FrontDoor::new(state, table)`. It
  then selects over the shutdown and the accept loops. The public `serve`
  (line 233) calls it with no runtime. Its in-process callers are
  `crates/gdaemon/tests/common/mod.rs` (lines 32 and 77), `tests/heartbeat.rs`
  (line 323), `tests/http_contracts.rs` (line 273), and `tests/front_door.rs`
  (line 424). None changes here.
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle` (line 68) runs as
  the baseline describes. `state.auth` is read only by the challenge and by
  `authenticate`. `FrontDoor::new(state, table)` keeps its signature.
- `crates/gdaemon/src/front_door/challenge.rs::answer` (line 39) takes a
  bootstrap `&Path`. It rejects any `Authorization` header with 401
  `credential_before_proof`, passes the `managed` kind through as `Ok`, and
  answers the `interactive` kind as `Err(response)` with Decision 3's proof,
  reading `api_key` from that bootstrap on each request.
- `crates/gdaemon/src/front_door/health.rs::unavailable` (line 39) writes
  `{"status":"unavailable","backend":{"state","target"}}` with
  `target.to_string()`, and `bad_gateway` (line 49) writes
  `{"status":"bad_gateway","backend":{"target"}}` the same way. Both take
  `target: SocketAddr`. Their production callers are `proxy::forward`,
  `ws::splice`, and `routes.rs::compare`, and each passes a `SocketAddr`,
  which the `Display` bound accepts unchanged. So this leaf does not edit
  them; 1.3 edits `proxy.rs` and `ws.rs` for the relay.
- `crates/gdaemon/tests/front_door.rs`:
  - `postgres_resolver_and_node_mode_fail_closed` (line 236) calls a
    resolver-less `AuthState` door "node" and asserts that it answers a
    `gobby_` bearer with 503 `key_resolver_unavailable`. Under Decision 2 that
    door is an in-process standalone door, so only its names change.
  - `spawn_serve` and `spawn_tls_serve` spawn `gdaemon serve` over a node
    bootstrap with `GOBBY_BACKEND_COMMAND` set to `/bin/sleep 600`, because a
    node needs no `database_url` for a lease. Their callers,
    `native_owner_survives_parent_eof_and_frees_ports_on_stop` (line 1042) and
    `self_signed_generated_once_and_reused` (line 1752), assert that serve
    outlives parent EOF, stops on SIGTERM with its ports freed, and answers
    `/api/health` over self-signed TLS with 503. A node that launches no
    backend keeps each of those outcomes, but it needs `api_key` to start.
  - `resolver_error_is_503_and_missing_authority_refuses_start` (line 312)
    spawns serve over a standalone bootstrap without `database_url` and
    expects the `prepare` refusal. Building `AuthState` after `prepare` keeps
    that outcome.
- A node bootstrap from `gobby auth login` carries `datastore_mode: remote`,
  `hub_daemon_url`, `api_key`, `api_key_id`, and `hub_cert` for an `https`
  hub (`src/gobby/cli/auth_login.py::_publish`, line 275).

**Implementation:**
- `state.rs`:
  - `pub enum Prepared { Backend(Arc<RuntimeState>, BackendSupervisor),
    Node(PidFileClaim) }`.
  - `RuntimeState::prepare` returns `Result<Option<Prepared>>`. For a node it
    returns `Prepared::Node(claim)` right after the claim, before it reads
    `database_url`, takes a lease, or builds a `BackendConfig` or supervisor.
    Standalone and hub keep today's path, always take the lease, and return
    `Prepared::Backend`.
  - `pub struct NodeServices { pub bootstrap_path: PathBuf, pub
    hub_daemon_url: String }`.
    `NodeServices::from_bootstrap(bootstrap: &HubDatabaseBootstrap, path:
    &Path) -> Result<Arc<NodeServices>>` fails without `api_key` with "node
    bootstrap <path> has no api_key; run `gobby auth login` to enroll". It
    takes the `hub_daemon_url` that 1.1 guarantees on every node bootstrap,
    and it reads no `database_url`.
  - The module comment says that a node takes only the PID claim and
    `NodeServices`.
- `front_door/mod.rs`:
  - `FrontDoor` wraps a private `enum Door { Backend { state: FrontDoorState,
    table: Arc<RouteTable> }, Node(Arc<NodeServices>) }`.
    `FrontDoor::new(state, table)` builds the backend arm, unchanged.
    `FrontDoor::node(node: Arc<NodeServices>)` builds the node arm.
  - `handle` runs `observe_peer` for both arms. The backend arm then runs
    today's sequence unchanged. The node arm sends `POST
    /api/runtime/handshake/challenge` to
    `challenge::answer(&node.bootstrap_path, request)` and returns its `Err`
    response. Everything else, including a `managed` challenge that `answer`
    passes through, is answered with
    `health::unavailable(&node.hub_daemon_url, BackendState::Down)`. 1.3
    replaces that answer with the relay.
- `health::unavailable` and `health::bad_gateway` take
  `target: impl std::fmt::Display` (Decision 7).
- `serve.rs`:
  - New `pub async fn serve_doors(doors: Vec<(TcpListener, FrontDoor)>, tls:
    Option<Arc<ServerConfig>>, shutdown: impl Future<Output = ()>) ->
    Result<()>` holds today's `HeartbeatHost` start and stop, the accept
    loops, and the select. `serve_with_runtime` builds the backend doors as
    today and calls it.
  - `run` keeps the backend ports, the family warnings, and the TLS load
    before `prepare` in every mode, and then matches the result:
    - `None` returns `Ok(())`, as today.
    - `Prepared::Node(claim)` builds `NodeServices::from_bootstrap`, binds
      both public ports, and calls `serve_doors` with
      `FrontDoor::node(node.clone())` for every listener and
      `shutdown_signal()`. It holds `claim` until `serve_doors` returns. The
      backend ports that `bind` records go unused.
    - `Prepared::Backend(state, supervisor)` builds the key resolver and
      `AuthState`, moved from before `prepare`, and then runs today's owner
      task and `serve_with_runtime`.
- `tests/run_modes.rs` builds its bootstraps with
  `gobby_core::bootstrap::parse_hub_database_bootstrap`. Every node bootstrap
  there uses an `http` `hub_daemon_url` at a refused loopback port, so 1.3's
  `hub_cert` rule leaves it valid. Its standalone and hub bootstraps carry no
  `database_url`. Each `prepare` call runs in its own temporary home.
- `tests/front_door.rs`:
  - `spawn_serve` and `spawn_tls_serve` add `api_key` to their node
    bootstraps.
  - `postgres_resolver_and_node_mode_fail_closed` is renamed
    `postgres_resolver_and_resolverless_door_fail_closed`, its `node` binding
    becomes `resolverless`, and its `"node auth"` message becomes
    `"resolverless auth"`. Its assertions stay.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gobby-daemon --test run_modes --test front_door --test lease --test ws_golden_proxy --test http_contracts --test heartbeat`
and `cargo clippy -p gobby-daemon --all-targets -- -D warnings` (heavy work).

Consumers unchanged:
- `crates/gdaemon/src/front_door/routes.rs` — no-edit-reason: `compare` passes `state.target`, a `SocketAddr`, to `health::bad_gateway`, which accepts it through `impl std::fmt::Display` with byte-identical output (Decision 7); its tests build `FrontDoorState` through `new`, whose signature is unchanged.
- `crates/gdaemon/src/front_door/challenge.rs` — no-edit-reason: `answer` already takes the bootstrap path, which the node arm supplies.
- `crates/gdaemon/src/front_door/auth.rs` — no-edit-reason: `AuthState` is built as today, only in the backend arm.
- `crates/gdaemon/tests/lease.rs` — no-edit-reason: it builds its `RuntimeState` literal and its doors directly and never calls `prepare`, whose result type is the only change it could see.

**Acceptance:**

- 1.2.1 - `RuntimeState::prepare` returns `Prepared::Node` for a node bootstrap without `database_url`, and fails for standalone and hub bootstraps without `database_url` with the "native runtime ownership requires bootstrap database_url" message. `NodeServices::from_bootstrap` refuses a node bootstrap without `api_key` with a message naming `gobby auth login`. test: `crates/gdaemon/tests/run_modes.rs::prepare_takes_only_the_claim_for_a_node`.
- 1.2.2 - A spawned `gdaemon serve` whose node bootstrap carries an `http` `hub_daemon_url`, `api_key`, `database_url: "not a PostgreSQL DSN"`, and `front_door.routes.terminal_ws: native`, with `GOBBY_BACKEND_COMMAND` set to a command that creates a marker file, starts. It answers an `interactive` challenge on its loopback front door with the hex `HMAC-SHA256(HMAC-SHA256(api_key, "gobby-interactive-proof-v1"), nonce)`, and answers another request with 503 whose `backend.target` is `hub_daemon_url`. Afterwards the process is still running and the marker is absent, so the node launched no backend and started no `ActiveServices`, whose pool would refuse that `database_url`. A second `gdaemon serve` on the same home exits 0 while the first holds the PID claim. test: `crates/gdaemon/tests/run_modes.rs::node_serve_takes_only_the_claim`.
- 1.2.3 - A resolver-less standalone door still answers a `gobby_` bearer with 503 `key_resolver_unavailable`, as a door with a failing PostgreSQL resolver does. test: `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_resolverless_door_fail_closed`.

### 1.3 Node relay: a node's front door forwards to its hub over the pinned connection [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/relay.rs`
- `crates/gdaemon/src/state.rs::*` — scope-reason: `NodeServices` replaces `hub_daemon_url` with `relay`, and `NodeServices::from_bootstrap` enforces the `hub_cert` rule and builds the relay
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: registers `relay`; the node arm of `Door` gains `ws_listener`, and `handle` relays a passed-through challenge, every upgrade, and every other request in place of 1.2's 503
- `crates/gdaemon/src/front_door/ws.rs::*` — scope-reason: `splice` is split so the upgrade exchange runs over a stream the caller has connected, and the relay reuses it over TLS
- `crates/gdaemon/src/front_door/proxy.rs::*` — scope-reason: `strip_trailers` becomes `pub(super)` so the sibling `relay` module reuses it; `strip_hop_by_hop` is already `pub`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run`'s node arm passes each listener's `ws_listener` flag to `FrontDoor::node`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/tests/nodes.rs`

**Research context:**
- After 1.2, `state.rs::NodeServices` carries `bootstrap_path` and
  `hub_daemon_url`, `serve.rs::run`'s node arm serves `FrontDoor::node` doors
  through `serve_doors`, and the node arm of `FrontDoor::handle` answers the
  native challenge and returns `health::unavailable(&hub_daemon_url, Down)`
  for everything else.
- `crates/gdaemon/src/front_door/proxy.rs`:
  - `client()` (line 34) builds `Client<HttpConnector, Body>` with a 2 s pool
    idle timeout.
  - `forward` (line 43) rewrites the URI to `http://<target>`, strips
    hop-by-hop fields and trailers, and maps a connect error to
    `health::unavailable(target, state.backend_state)`.
  - `strip_hop_by_hop` (line 71) is `pub`, and `strip_trailers` (line 99) is
    private.
- `crates/gdaemon/src/front_door/ws.rs::splice` (line 28) connects a
  `TcpStream` to the target, runs the HTTP/1 handshake with upgrades, sends
  the request with its path and query, and joins both upgrades with
  `copy_bidirectional`. Close codes, ping/pong, and extensions pass through
  untouched.
- `crates/gdaemon/src/front_door/tls.rs::pinned_client_config` (line 110)
  trusts only the given leaf and sets ALPN `http/1.1`. hyper-rustls 0.27.7's
  `ConnectorBuilder::with_tls_config` panics unless that list is empty
  (`connector/builder.rs`, lines 60-64). The test helpers
  `crates/gdaemon/tests/common/mod.rs::{self_signed, start_tls_front_door, tls_connect, refused_addr, ws_backend, frame, read_exact_into}`
  already exist.
- `crates/gdaemon/src/serve.rs::bind` binds a port and, when it applies, its
  companion address; both listeners share the port number. `PublicListener`
  carries no listener kind, but `run` calls `bind` once for `daemon_port` and
  once for `websocket_port`, so its node arm knows each listener's kind.
- Hub side: `src/gobby/servers/_app_ui.py::_mount_ws_endpoint` serves `/ws` and
  `/ws/{path}` on the HTTP port with the path discarded, and closes 4401 when
  authentication fails.
- Break-glass: the hub's front door strips only
  `front_door::auth::IDENTITY_HEADERS`, so `X-Gobby-Break-Glass` reaches the
  hub's Python, which admits it from a loopback peer
  (`src/gobby/servers/auth_service.py::AuthService.break_glass_admits`).
- `src/gobby/cli/auth_login.py::_publish` writes `api_key`, `api_key_id`, and
  `hub_cert` for an `https` hub, and removes `hub_cert` for an `http` one.

**Implementation:**
- New `front_door/relay.rs` defines `HubRelay { origin, authority, host, dial, tls, client }`:
  - Origin parsing. `authority` is kept for `Host` and URL construction. `dial`
    is derived separately from the unbracketed host and the explicit port,
    defaulting to 443 for `https` and 80 for `http`. `host` is the unbracketed
    DNS name or IP literal, from which rustls `ServerName` is built. The relay
    splice and the 1.4 channel client share these parsed origin values.
  - `tls` is the config from `pinned_client_config(pem)`, with its `http/1.1`
    ALPN, and serves the manually dialed WebSocket and channel streams.
  - `client` is
    `hyper_util::client::legacy::Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>`.
    It is built from a clone of `tls` whose `alpn_protocols` alone is cleared,
    passed to `with_tls_config(...).https_or_http().enable_http1()`, because
    hyper-rustls 0.27.7 asserts an empty ALPN list and supplies HTTP/1 itself.
    Its pool idle timeout is 90 s, so sequential requests reuse one TLS
    connection.
  - `HubRelay::forward(request)` rewrites the URI to `origin` plus the
    original path and query, sets `Host` to `authority`, and removes
    `X-Gobby-Break-Glass` (Decision 5). It strips hop-by-hop fields and
    trailers with `proxy::strip_hop_by_hop` and `proxy::strip_trailers`. A
    connect error maps to `unavailable(origin, Down)`, and a TLS or protocol
    error to `bad_gateway(origin, error)`.
  - `HubRelay::splice(request, to_root_ws: bool)` dials `dial`. For
    `https` it wraps the stream with `tokio_rustls::TlsConnector` and `tls`,
    using `host` as the server name. It sets `Host`, removes
    `X-Gobby-Break-Glass`, rewrites the path to `/ws` (keeping the query) when
    `to_root_ws`, and hands the stream to the split `ws` upgrade exchange.
- `ws.rs` splits `splice` into a connect step and
  `splice_over(stream, request, target: &str)`, so loopback and relay share
  one upgrade exchange.
- `proxy.rs::strip_trailers` becomes `pub(super)`.
- `state.rs`:
  - `NodeServices` replaces `hub_daemon_url` with `relay: HubRelay`, whose
    `origin` is that URL.
  - `NodeServices::from_bootstrap` fails for an `https` hub without
    `hub_cert` with "node bootstrap <path> has no hub_cert for an https hub;
    run `gobby auth login` again". It then reads the PEM at `hub_cert` and
    builds the relay. An `http` hub ignores `hub_cert` (Decision 6).
- `front_door/mod.rs`:
  - The node arm becomes `Node { node: Arc<NodeServices>, ws_listener: bool }`,
    and `FrontDoor::node(node, ws_listener)` takes the flag.
  - In the node arm, `handle` still answers the native challenge first. It
    then sends any upgrade to `relay.splice(request, ws_listener)` and
    everything else, including a passed-through `managed` challenge, to
    `relay.forward(request)`.
  - The node never authenticates a key, never sets identity headers, and
    never consults the route table. The only header it removes beyond
    hop-by-hop fields is `X-Gobby-Break-Glass`; the hub's front door strips
    and sets the identity headers itself.
- `run`'s node arm passes `ws_listener` false for the listeners that `bind`
  returns for `daemon_port`, and true for those it returns for
  `websocket_port`.
- `crates/gdaemon/Cargo.toml` adds `hyper-rustls` (Decision 13).
  `Cargo.lock` is updated if resolution changes.
- `tests/nodes.rs` declares `mod common;` and keeps its node and TLS hub-stub
  helpers local, because no other test file uses them. It serves its node
  doors through `serve::serve_doors`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gobby-daemon --test nodes --test run_modes --test front_door --test ws_golden_proxy --test http_contracts`
and `cargo clippy -p gobby-daemon --all-targets -- -D warnings` (heavy work), then
`cargo tree -p gobby-daemon -i aws-lc-rs` prints nothing. This leaf leaves
`crates/gdaemon/tests/run_modes.rs` unedited: 1.2.2's interactive challenge
stays native, and its `http` hub needs no `hub_cert`. It also leaves
`crates/gdaemon/tests/front_door.rs` unedited. Its in-process doors are
backend doors, whose behavior and constructor are unchanged here. Its spawned
node serves use an `http` `hub_daemon_url` at a refused port, so the relay
answers their `/api/health` with the same 503.

Consumers unchanged:
- `crates/gdaemon/tests/ws_golden_proxy.rs` — no-edit-reason: the loopback splice keeps its behavior; 1.3.3 replays the same corpus through a node.

**Acceptance:**

- 1.3.1 - A node relays two sequential requests to a TLS hub stub over one pinned connection, with the bearer, path, and query unchanged and `Host` set to the hub's authority. When the hub refuses connections it answers 503 with `backend.target` equal to `hub_daemon_url`. test: `crates/gdaemon/tests/nodes.rs::node_relays_over_pinned_tls`.
- 1.3.2 - A node whose `hub_cert` names a different certificate than the hub presents answers 502 and never forwards the request. test: `crates/gdaemon/tests/nodes.rs::node_refuses_an_unpinned_hub`.
- 1.3.3 - A WebSocket upgrade relayed through the node to the hub stub replays the terminal golden corpus and a 1000 close frame byte-equal in both directions. An upgrade on the HTTP listener keeps its path, and an upgrade on the WebSocket listener reaches `/ws` with its query. The same test also checks origin-to-dial-address derivation for omitted ports and bracketed IPv6, without binding privileged ports. test: `crates/gdaemon/tests/nodes.rs::node_relays_ws_over_pinned_tls`.
- 1.3.4 - A node relays a `managed` handshake challenge to the hub stub, and still answers an `interactive` one with its own key while the stub accepts no connection. test: `crates/gdaemon/tests/nodes.rs::node_relays_managed_challenge_and_answers_interactive_natively`.
- 1.3.5 - `NodeServices::from_bootstrap` refuses a node bootstrap with an `https` hub and no `hub_cert`, with a message naming `gobby auth login`, and accepts an `http` hub without one. A spawned `gdaemon serve` with an enrolled node bootstrap relays a request to the TLS hub stub. test: `crates/gdaemon/tests/nodes.rs::node_startup_requires_a_pinned_hub`.
- 1.3.6 - A node on the same host as a loopback TLS hub stub relays a request and a WebSocket upgrade that both carry `X-Gobby-Break-Glass`, and the stub receives neither with that header. test: `crates/gdaemon/tests/nodes.rs::node_strips_break_glass_before_the_hub`.

### 1.4 Node channel: the hub registers, heartbeats, replaces, and revokes node channels [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/mod.rs`
- `crates/gdaemon/src/nodes/channel.rs`
- `crates/gdaemon/src/nodes/registry.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: `FrontDoorState` gains `native: nodes::NativeRoutes`, which `FrontDoorState::new` defaults; on a hub `handle` routes `/api/nodes/channel` to the channel endpoint after authentication and before `ws::splice`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `serve_with_runtime` takes the `NativeRoutes` for its doors and the public `serve` passes the default; `run`'s backend arm builds `HubNodes` for a hub; its node arm spawns the channel client and aborts it after `serve_doors` returns
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/tests/nodes.rs`

**Research context:**
- The hub's front door handles every request through
  `front_door::auth::AuthState::authenticate`:
  - It resolves a `gobby_` bearer through `PostgresKeyResolver`, against
    `api_keys` joined to `machines`.
  - It strips client `x-gobby-user-id`, `x-gobby-machine-id`,
    `x-gobby-key-id`, and `x-gobby-front-door` (`IDENTITY_HEADERS`) on every
    path, then sets them on success.
  - It authenticates WebSocket upgrades the same way, before `ws::splice`.

  So after authentication, those headers inside gdaemon are present only for
  a key-authenticated caller.
- `PostgresKeyResolver`'s pool is private (`pool: Result<Pool, PoolError>`),
  so the channel builds its own (Decision 17) with
  `gobby_core::postgres_pool::Pool::build(url, PoolSettings)`
  (`crates/gcore/src/postgres_pool/mod.rs`, line 88). `Pool` is not `Clone`.
- Schema:
  - `machines` has `id`, `hostname`, `os`, `label`, `tailscale_name`,
    `owner_user_id`, `first_seen`, `last_seen` (baseline), `ref` (440), and
    `last_heartbeat_at` and `node_version` (458).
  - `api_keys` (458) has `id`, `user_id`, `machine_id`, `key_hash`,
    `key_hint`, `label`, `created_at`, `last_used_at`, and `revoked_at`.
  - `gobby_daemon_runtime` holds SELECT, INSERT, UPDATE, and DELETE on both
    tables.
- axum's `WebSocketUpgrade` extracts the `OnUpgrade` that hyper places in the
  request extensions, which `FrontDoor::handle` preserves. `ws::splice`
  already relies on it through `hyper::upgrade::on`.
- `crates/gdaemon/tests/lease.rs::database_case` is the precedent for Rust
  database tests. It checks the protected test hub, takes an advisory lock,
  creates a throwaway schema with the needed tables, and points the pool at it
  through the URL's options.
- The `gdaemon` version is `env!("CARGO_PKG_VERSION")`, and the platform is
  `std::env::consts::{OS, ARCH}`.
- `gobby_core::local_token::read_api_key_at` reads `api_key` from a bootstrap
  path.
- `FrontDoorState::new` callers are `serve.rs::serve_with_runtime`, the
  `routes.rs` tests (lines 263, 297, and 319), `tests/front_door.rs` (lines
  77, 105, 243, and 408), and `tests/lease.rs` (line 67). The only
  `FrontDoorState` literal is `serve_with_runtime`'s functional update of the
  runtime field (line 268). So a field that `new` defaults needs no caller
  edit.
- #23457's runtime gates in `FrontDoor::handle` run before the challenge and
  authentication, so a standby hub answers `/api/nodes/channel` with its 503
  before the channel endpoint is reached (Decision 8).
- `RuntimeState::prepare` already refuses a hub bootstrap without
  `database_url`, so `run`'s backend arm always has one on a hub.

**Implementation:**
- `nodes/registry.rs` defines the following:
  - `Registry` wraps `std::sync::Mutex<HashMap<Uuid, Slot>>`, with
    `Slot { current: Option<ChannelHandle>, gate: Arc<tokio::sync::Mutex<()>> }`.
    A machine's slot, and so its gate, lives for the hub's lifetime, bounded
    by the `machines` rows (Decision 10).
  - `ChannelHandle { connection_id: Uuid, close: oneshot::Sender<(u16, &'static str)> }`.
  - `register(machine_id) -> (connection_id, close_rx)` assigns a new
    `connection_id`, swaps `current`, and fires the old handle's `close`
    with 4409.
  - `remove(machine_id, connection_id)` clears `current` only while the
    stored `connection_id` is its own.
  - `persist(machine_id, connection_id, write)` clones the slot's gate, awaits
    it, and runs `write` only if `current` still names `connection_id`. It
    otherwise returns `Skipped` without touching the database. Callers run it
    in a spawned task, which keeps the gate until the database operation
    completes.
  - `ChannelHandle` also carries `live: bool`, false at `register`.
    `mark_live(machine_id, connection_id) -> bool` sets it only while
    `current` still names `connection_id`, and returns whether it did. It
    never touches a successor's handle. The handler calls it only after its
    ack send succeeds. `is_connected(machine_id) -> bool` and
    `connected_ids()` report only live entries.
  - No `std` lock is held across an await.
- `nodes/channel.rs` holds the hub endpoint, the node client, and
  `ChannelTiming`:
  - **Hub endpoint.** An axum handler takes `WebSocketUpgrade` and reads
    `x-gobby-machine-id` and `x-gobby-key-id`. A request without them is 401
    `missing_auth` before the upgrade. After the upgrade the handler runs
    these steps:
    1. At the upgrade it creates the recheck timer with
       `tokio::time::interval_at(upgrade + key_recheck, key_recheck)` and
       `MissedTickBehavior::Delay`. It also sets two deadlines: the hello
       deadline, `upgrade + hello_timeout`, and the admission end, `hello
       deadline + recheck_deadline`.
    2. It waits for a valid hello until the hello deadline. An invalid
       first frame closes 1008, and a missed deadline closes 1011.
    3. It registers the machine. In a spawned task through
       `registry.persist`, it writes `last_heartbeat_at = now()` and
       `node_version`. It waits for that task's result, its close signal, or
       the hello deadline:
       - The close signal (it was replaced) closes with that code and no ack.
       - A write error or a missed deadline removes the entry and closes 1011
         with no ack. A still-running write keeps its gate until it completes.
    4. It runs the key check `SELECT 1 FROM api_keys WHERE id = $1 AND
       revoked_at IS NULL` under `recheck_deadline`, which covers pool
       acquisition and the query. A biased select polls its close signal
       first:
       - The close signal (it was replaced) closes with that code and no ack,
         and drops the in-flight check.
       - No row removes the entry and closes 4401 with no ack.
       - An error or a timeout removes the entry and closes 1011 with no ack.
       - A proven key continues to step 5.
    5. It runs this step as the public `send_ack(sink, registry, machine_id,
       connection_id, close, admission_end)`, which is generic over
       `Sink<Message> + Unpin` and returns the outcome the handler acts on.
       A biased select polls the close signal first, then the admission end,
       then the send:
       - The close signal closes with that code. The channel never goes live.
       - The admission end, with the send still pending, removes the entry
         and drops the transport without a close frame. Nothing was
         published.
       - A failed send removes the entry and exits. Nothing was published.
       - After a successful send it calls `registry.mark_live`. If that
         returns `false`, a successor replaced it after the send, so it closes
         4409 and never reads `connected`.
    6. It then selects over four events and never awaits bookkeeping:
       - **A received frame.** Any frame resets the `idle_timeout` deadline.
         On a ping, when `heartbeat_write` has elapsed since the last write
         started and no heartbeat write is in flight, it spawns the
         `last_heartbeat_at` and `node_version` write through
         `registry.persist`.
       - **A recheck tick.** It runs the public `key_tick(sink, pool,
         registry, machine_id, connection_id, key_id, cycle_deadline)`, which
         is generic over `Sink<Message> + Unpin`. `cycle_deadline` is the
         tick plus `recheck_deadline`, and it bounds the check, the close,
         and the drop together:
         - The step 4 key check runs until `cycle_deadline`.
         - No row removes the registry entry at once, then sends 4401 until
           `cycle_deadline`, and then drops the transport.
         - An error or a timeout keeps the channel until the next tick
           (Decision 10's database-error exception).
       - **The idle deadline.** It removes the entry and closes 4408.
       - **The close signal.** It closes with the signal's code (4409).
    7. On exit it calls `registry.remove(machine_id, connection_id)`, which is
       a no-op when the entry is already gone or replaced.

    The revoked-tick close runs only until `cycle_deadline`. Every other
    close-frame send runs under its own `recheck_deadline`. In both cases the
    handler then drops the transport. `node_version` and `platform` are
    bounded to 64 characters.
  - **Node client.** The public `run_client(node: Arc<NodeServices>, timing:
    ChannelTiming)` loops `connect → hello → ack → ping every ping_interval`
    with these rules:
    - Each attempt reads `api_key` with
      `read_api_key_at(&node.bootstrap_path)`.
    - It dials the relay's `dial` address and requests
      `wss://<authority>/api/nodes/channel` (or `ws://` for an `http` hub)
      over the relay's `tls` config with `host` as the server name, using
      `tokio_tungstenite::client_async_with_config`. It shares the relay's
      parsed origin values (1.3): the URI authority for `Host` and the URL,
      the separate dial address with the 443 or 80 default, and the
      unbracketed host.
    - It sends and receives with `futures_util::{SinkExt, StreamExt}`, because
      `WebSocketStream` implements `Sink` and `Stream` rather than inherent
      send and receive methods.
    - The dial, `client_async_with_config`, the hello send, and the ack
      receipt run inside one `tokio::time::timeout(attempt_timeout, ..)`
      (Decision 9).
    - After the ack it selects over the next received frame, the ping tick,
      and `idle_deadline`, which is the last received frame plus
      `idle_timeout`. Any received frame, a pong included, moves the
      deadline. Each ping send runs under
      `tokio::time::timeout_at(idle_deadline, ..)`.
    - After any close or error, or at either expiry, it drops the transport
      and sleeps for the backoff, which starts at `backoff_start`, doubles up
      to `backoff_max`, and resets after an ack.
  - **`ChannelTiming`.** Its defaults are a 30 s ping, 1 s and 60 s backoff,
    a 20 s attempt timeout, a 10 s hello timeout, a 90 s idle timeout, a 25 s
    key recheck with a 4 s recheck deadline, and a 60 s heartbeat write
    interval.
- `nodes/mod.rs` registers `channel` and `registry` and defines:
  - `pub struct HubNodes { pub db: Pool, pub registry: Registry }`.
  - `#[derive(Clone, Default)] pub enum NativeRoutes { #[default] Standalone,
    Hub(Arc<HubNodes>) }`, the mode-native routes a backend door serves.
- `lib.rs` registers `nodes`.
- `front_door/mod.rs`:
  - `FrontDoorState` gains `pub native: NativeRoutes`, which
    `FrontDoorState::new` sets to `NativeRoutes::default()`.
    `FrontDoor::new(state, table)` keeps its signature.
  - In the backend arm, after authentication and before the upgrade branch,
    `handle` sends `/api/nodes/channel` to the channel endpoint when
    `state.native` is `Hub`. A standalone door does not match it, so the
    request proxies to Python (Decision 8).
- `serve.rs`:
  - `serve_with_runtime` gains `native: NativeRoutes` and sets it, with the
    runtime, on each door's state. The public `serve` passes
    `NativeRoutes::default()`.
  - `run`'s backend arm passes
    `NativeRoutes::Hub(Arc::new(HubNodes { db, registry }))` when
    `bootstrap.mode` is `RunMode::Hub`, with the pool built from
    `database_url` under Decision 17's settings and an empty registry, and
    `NativeRoutes::default()` otherwise. A `Pool::build` error fails startup.
  - `run`'s node arm spawns `nodes::channel::run_client(node.clone(),
    ChannelTiming::default())` before `serve_doors`, and aborts it after
    `serve_doors` returns.
- `tests/nodes.rs` builds each hub door as `FrontDoorState { native:
  NativeRoutes::Hub(..), ..FrontDoorState::new(..) }` with
  `FrontDoor::new`, and serves hub and node doors through
  `serve::serve_doors`.
- `crates/gdaemon/Cargo.toml` adds the `axum` `"ws"` feature and
  `tokio-tungstenite` (Decision 13), and moves `futures-util = "0.3"` from
  `[dev-dependencies]` to `[dependencies]`. `Cargo.lock` is updated if
  resolution changes.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gobby-daemon --test nodes --test run_modes --test front_door --test lease`
and `cargo clippy -p gobby-daemon --all-targets -- -D warnings` (heavy work), then
`cargo tree -p gobby-daemon -i aws-lc-rs` prints nothing. This leaf leaves
`crates/gdaemon/tests/front_door.rs`, `run_modes.rs`, and `lease.rs`
unedited: they build every `FrontDoorState` through `new`, whose new field
defaults, and they call the public `serve` or `prepare`, whose signatures are
unchanged.

**Acceptance:**

- 1.4.1 - A key-authenticated channel registers its machine and receives an ack carrying the machine id, and its hello writes `last_heartbeat_at` and `node_version`. A ping within the heartbeat write interval writes nothing. After one heartbeat write interval, a ping advances `last_heartbeat_at` and preserves the channel's `node_version`; another ping within the next interval writes nothing. The test uses short constructor timings and reads the isolated schema's machine row to observe both writes and the suppression. A second connection for the same machine replaces the first, which closes with 4409. When the first channel's cleanup runs after the second ack, the second stays registered. test: `crates/gdaemon/tests/nodes.rs::channel_registers_and_replaces`.
- 1.4.2 - The test uses a short key-recheck interval R anchored at the upgrade, a recheck deadline D below 0.75R, and an idle timeout above 2R:
  - **First-check proof.** It revokes the channel's key R/4 after its upgrade completes. The 4401 close arrives within R + D of the revocation on a monotonic clock, and the machine is already absent from the registry. The first recheck after the revocation falls 0.75R after it and the second 1.75R after it, so only the first scheduled recheck fits that bound. This runs for three peers: the node client sending its scheduled pings, a raw peer silent after hello, and a raw peer sending only Pong frames.
  - **Bookkeeping never blocks revocation.** While a test transaction holds a row lock on the machine's `machines` row, a due heartbeat write blocks. The key is then revoked, and the channel still closes 4401 within R + D.
  - **Unproven checks.** While a test transaction holds `ACCESS EXCLUSIVE` on `api_keys`, a tick's check times out after D and the channel stays open. After the lock is released, the next tick proves the key again.
  - **Teardown shares the tick's budget.** The test revokes the key, then holds `ACCESS EXCLUSIVE` on `api_keys` and calls the public `key_tick` with a `futures_util::sink` whose send never completes. It releases the lock D/2 after the tick, so the check returns no row late. The registry entry is gone as soon as `key_tick` reads that result, and `key_tick` returns with the transport dropped by the tick plus D on a monotonic clock. That is the remaining budget, not D again.
  - It does not pause tokio time, because paused time auto-advances past the timers while the hub awaits its database query. test: `crates/gdaemon/tests/nodes.rs::revoked_key_closes_channel_on_heartbeat`.
- 1.4.3 - The node client sends a hello with the gdaemon version and `<os>/<arch>`, pings at its interval, and reconnects after a close. Against a refusing hub its delays double from the start value to the ceiling, and they reset after an ack. It reads the key again on each attempt. With short constructor timings, two stalled test peers end their attempts and the client reconnects with the key read again: a peer that accepts TCP and never answers ends the attempt within the attempt timeout, and a peer that sends the ack and then neither reads nor writes is dropped within the idle timeout of its last frame. test: `crates/gdaemon/tests/nodes.rs::node_channel_reconnects_with_backoff`.
- 1.4.4 - A channel whose first frame is not a valid hello closes with 1008, and a registered channel silent past the idle timeout closes with 4408 and leaves the registry. test: `crates/gdaemon/tests/nodes.rs::silent_or_malformed_channel_is_closed`.
- 1.4.5 - On a hub, an upgrade to `/api/nodes/channel` without a key identity is refused with 401 before any upgrade. A standalone front door does not mount the route and proxies the request to its backend. A key revoked after the upgrade but before hello, or while the hello write is blocked by a test row lock that is then released, closes 4401 with no ack, and leaves no registry entry and no `connected` machine. While a test transaction holds `ACCESS EXCLUSIVE` on `api_keys` and blocks a registered channel's pre-ack check, the machine reads `connected: false`. A raw peer that resets its TCP connection while that check is blocked makes the ack send fail after the lock is released, which leaves no registry entry and the machine never reading `connected`. A `mark_live` call under a superseded `connection_id` returns `false` and leaves the successor not live. The bounded ack send is driven through the public `send_ack` with a `futures_util::sink` and a test-built registry. A sink whose send never completes makes `send_ack` return at the admission end, with no registry entry left for that `connection_id` and the machine never reading `connected`. With the close signal already fired, `send_ack` returns that code without sending, and the successor's entry is unchanged. A sink that completes accepts the ack while the machine still reads `connected: false`, and the machine reads `connected` once `send_ack` returns. test: `crates/gdaemon/tests/nodes.rs::channel_requires_a_key_on_a_hub`.
- 1.4.6 - While a test transaction holds a row lock on the machine's `machines` row, a first connection's hello write blocks. A second connection for the same machine then registers. The first connection closes with 4409 and no ack while its write stays blocked, and the second's ack does not arrive while the lock is held. After the test releases the lock, the first write completes and releases the gate, the second receives its ack, and the row holds the second connection's `node_version` with the registry naming the second connection. A `registry.persist` call under the superseded `connection_id` returns `Skipped` and leaves the row unchanged. In the pre-ack variant, two upgrades authenticate first, and a test transaction then holds `ACCESS EXCLUSIVE` on `api_keys`. The test uses a recheck deadline long enough to keep the first connection's pre-ack check blocked through the replacement. The first connection's hello and write complete and its pre-ack check blocks. The second connection's hello then registers and replaces it. The first closes 4409 with no ack while its check is still blocked, and the machine reads `connected: false`. After the lock is released, the second connection receives its ack and becomes the live entry. test: `crates/gdaemon/tests/nodes.rs::replaced_channel_cannot_overwrite_successor_bookkeeping`.

### 1.5 `/api/machines` lists the caller's machines with connection state [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/machines_api.rs`
- `crates/gdaemon/src/nodes/mod.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: in the backend arm `handle` routes `/api/machines` and `/api/machines/{id}` to the machines router after authentication and before the route table, for both `NativeRoutes` variants
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run`'s backend arm builds the standalone machines pool into `NativeRoutes::Standalone`
- `crates/gdaemon/tests/nodes.rs`
- `docs/guides/configuration.md`

**Research context:**
- `src/gobby/servers/routes/api_keys.py::list_keys` returns `{"keys": [...]}`
  scoped to the caller's user, with ISO timestamps (`_key_summary`). The
  machines API follows that shape (Decision 12).
- Identity: the front door strips client `IDENTITY_HEADERS` on every path and
  sets them only for a key-authenticated caller. So after authentication,
  `x-gobby-user-id` inside gdaemon is the caller's resolved user, and its
  absence means no key identity.
- After 1.4, `FrontDoorState.native` carries
  `NativeRoutes::{Standalone, Hub(Arc<HubNodes>)}` to every backend door, and
  `HubNodes` holds the hub's pool and registry. `NativeRoutes::default()` is
  `Standalone`, which `FrontDoorState::new` sets and the public `serve`
  passes.
- `RuntimeState::prepare` refuses a standalone bootstrap without
  `database_url`, so `run`'s backend arm always has one for a standalone
  front door.
- Schema: `machines` has `id` (uuid), `hostname`, `os`, `label`,
  `tailscale_name`, `owner_user_id`, `first_seen`, `last_seen` (baseline),
  `ref` (migration 440), and `last_heartbeat_at` and `node_version` (migration
  458). `gobby_daemon_runtime` holds SELECT on it.
- `docs/guides/configuration.md` documents the bootstrap keys, including
  `datastore_mode` and `hub_daemon_url`. `docs/guides/shared-stack.md` holds
  the client setup that `gobby auth login` cites.
- No `/api/machines` consumer exists in `src/`, `web/`, `crates/`, or
  `tests/`.

**Implementation:**
- `nodes/machines_api.rs` builds an axum router over `(Option<&Pool>,
  Option<&Registry>)`. Its contract (Decision 12) is the following:
  - **Fields.** Each machine object has exactly `id`, `ref`, `hostname`, `os`,
    `label`, `tailscale_name`, `first_seen`, `last_seen`,
    `last_heartbeat_at`, `node_version`, and `connected` (bool). `id` is the
    uuid's text. Timestamps are RFC 3339 strings, or `null` when the column
    is null. Other nullable columns are `null` when null.
  - **`GET /api/machines`** selects those columns where
    `owner_user_id = $1` (the caller's `x-gobby-user-id`), ordered by
    `first_seen, id`, and returns `{"machines": [...]}`.
  - **`GET /api/machines/{id}`** returns the one object, not wrapped. An
    unparsable id, a missing row, or another user's machine is 404
    `machine_not_found`.
  - **`connected`** is `registry.is_connected(id)`: true only while this
    hub's registry holds a live channel for the machine (Decision 12). A
    standalone front door has no registry, so every row reads `false`. The
    hub's own machine has no special case.
  - **Errors** use gdaemon's existing body `{"error": msg, "code": code}`.
    Without `x-gobby-user-id` both routes are 401 `missing_auth`, checked
    first. Without a pool, or on a query or pool error, they are 503
    `machines_unavailable`.
- `nodes/mod.rs` registers `machines_api`, and `NativeRoutes::Standalone`
  becomes `Standalone(Option<Arc<Pool>>)`, with a manual `Default` returning
  `Standalone(None)`. A hub serves the machines routes from `HubNodes`'s pool
  and registry.
- `run`'s backend arm passes
  `NativeRoutes::Standalone(Some(Arc::new(pool)))` for a standalone
  bootstrap, with the pool built from `database_url` under Decision 17's
  settings. A `Pool::build` error fails startup. `FrontDoor::handle` routes
  the machines paths in the backend arm. A node never reaches it, because it
  relays.
- `docs/guides/configuration.md` gains a "Nodes" section that covers:
  - the node bootstrap fields (`datastore_mode: remote`, `hub_daemon_url`,
    and `api_key`, `api_key_id`, `hub_cert` from `gobby auth login`);
  - that a node runs only `gdaemon serve`, takes only the PID claim, launches
    no backend, and holds no `database_url`;
  - the relay, including the native challenge and the `/ws` target;
  - the channel's heartbeat and close codes;
  - `/api/machines`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gobby-daemon --test nodes --test front_door --test run_modes --test lease --test http_contracts --test heartbeat --test ws_golden_proxy`
(heavy work). `ws_golden_proxy`, `front_door`, and `http_contracts` include
`crates/gdaemon/tests/common/mod.rs`. This leaf leaves those files,
`run_modes.rs`, and `lease.rs` unedited: `NativeRoutes::Standalone` gains its
payload only behind `FrontDoorState::new`'s default, so no caller names it.

**Acceptance:**

- 1.5.1 - `GET /api/machines` returns `{"machines": [...]}` with only the caller's machines, ordered by `first_seen, id`, each with exactly the eleven fields listed in this section's Implementation and RFC 3339 or `null` timestamps. `connected` is true exactly for machines with an acknowledged live channel, and false for every row on a standalone front door. `GET /api/machines/{id}` returns one such unwrapped object and answers 404 `machine_not_found` for another user's machine, an unknown id, and a malformed id. test: `crates/gdaemon/tests/nodes.rs::machines_api_lists_rows_with_connection_state`.
- 1.5.2 - Without a key identity both routes answer 401 `missing_auth`, and a query error, or a standalone door without a pool, answers 503 `machines_unavailable`. test: `crates/gdaemon/tests/nodes.rs::machines_api_requires_a_key`.
- 1.5.3 - The configuration guide documents running a node, its relay and channel, and `/api/machines`. behavior: the "Nodes" section in `docs/guides/configuration.md`.

### 1.6 Hub-node pair test over self-signed TLS [category: test] (depends: 1.3, 1.4, 1.5)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_pair.py`

**Research context:**
- `tests/e2e/conftest.py::spawn_daemon_instance` (line 1174) takes
  `tls="self-signed"` and writes the front door's TLS block into the isolated
  bootstrap. The e2e bootstrap template sets no `hub` flag.
- `tests/e2e/test_auth_login_e2e.py` provides the pattern for this test:
  - `self_signed_hub` (line 41) spawns the hub with `tls="self-signed"`.
  - `_node_home` (line 59) writes a node bootstrap with only
    `datastore_mode` and `hub_daemon_url`, so the node's ports would default
    to 60887 and 60888.
  - `_login` (line 80) runs `gobby auth login` with the e2e password, and
    `TEST_EMAIL` (line 27) names the login user.
  - Its `e2e_pre_daemon_setup` fixture (line 33) seeds the login user,
    overriding the default in `tests/e2e/conftest.py`.
- `tests.fixtures.gdaemon_binary.select_test_gdaemon` selects the gdaemon
  binary under test.
- The hub's `/api/health` returns `"mode": server.bootstrap_config.run_mode()`
  (`src/gobby/servers/routes/admin/_health.py`, line 388).
- The hub's WebSocketServer answers `{"type":"ping"}` with `{"type":"pong"}`
  (`src/gobby/servers/websocket/handlers/core.py::_handle_ping`, line 154).
- `src/gobby/storage/api_keys.py::ApiKeyManager` has `mint(user_id,
  machine_id, label)` (line 71) and `revoke(key_id, user_id)` (line 100).
- `tests/e2e/conftest.py::e2e_config` seeds the project
  `00000000-0000-0000-0000-000000000e2e` through `_seed_e2e_runtime_state`
  (line 566). The hub's handshake admits a project with a live `projects` row
  (`src/gobby/runner_init/servers.py::_project_admitted`, line 599).
- `src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`
  (line 115) rejects a body `machine_id` that differs from the forwarded
  machine with `claims_mismatch`, and issues an `interactive` principal
  carrying that machine id.

**Implementation:**
- The test does its own setup:
  - It imports `_login`, `TEST_EMAIL`, and `e2e_pre_daemon_setup` from
    `tests.e2e.test_auth_login_e2e`, the fixture through an explicit
    same-name import, instead of copying them.
  - The pair-test hub fixture takes `e2e_config` and `e2e_pre_daemon_setup` as
    fixture dependencies, so the isolated schema is reset, the E2E project is
    seeded, and the login user exists before the fixture writes `hub: true`
    into the isolated hub bootstrap and spawns the hub with
    `spawn_daemon_instance(..., tls="self-signed")`. All setup stays in
    `tests/e2e/test_hub_node_pair.py`.
  - It writes a node home with
    `datastore_mode: remote`, `hub_daemon_url: https://127.0.0.1:<hub port>`,
    `bind_host: 127.0.0.1`, and two free ports.
  - It enrolls with `gobby auth login`, accepting the hub's fingerprint. The
    node's machine id is the node home's `machine_id` file, which login
    creates through `require_machine_id`.
  - It mints an observer key with
    `ApiKeyManager.mint(TEST_USER_ID, "21000000-0000-4000-8000-000000000002", "pair-observer")`.
    `TEST_USER_ID` comes from `tests.fixtures.postgres`, and the machine is
    the one `_seed_e2e_runtime_state` inserts. The observer dials the hub
    directly over the pinned certificate with
    `ssl.create_default_context(cafile=…)`.
  - It starts a bare `gdaemon serve` from `select_test_gdaemon()`, with
    `GOBBY_HOME` set to the node home and stderr captured. The node takes its
    PID claim in the node home, apart from the hub's. A fixture finalizer
    terminates the node, and kills it after 5 s, on success, failure, or
    timeout.
- **Readiness.** Listener readiness and channel registration are observed
  separately, each bounded to 30 s:
  - The test polls the node's `/api/health` through its loopback front door
    until it answers 200.
  - It then polls the observer's `GET /api/machines/{node id}` until it reads
    `connected: true`. That means the hub has sent the ack, because an entry
    reads `connected` only once acknowledged (1.4).
  - A readiness timeout fails the test with the node's captured stderr.
- Then it asserts these steps in order:
  1. Through the node's loopback front door with the node's `api_key`,
     `/api/health` reports `"mode": "hub"`.
  2. `/api/machines` through the node lists the node's machine as
     `connected`.
  3. A WebSocket on the node's `websocket_port` receives a pong for a ping.
  4. Through the node's front door, the test sends
     `POST /api/runtime/handshake/challenge` with
     `{"nonce": <base64url of 32 random bytes>, "kind": "interactive"}` and no
     `Authorization`, and checks the proof equals the hex
     `HMAC-SHA256(HMAC-SHA256(node api_key, "gobby-interactive-proof-v1"), nonce)`.
     It then sends `POST /api/runtime/handshake` through the node with the
     node's bearer and
     `{"machine_id": <node machine id>, "project_id": "00000000-0000-0000-0000-000000000e2e", "session_id": null}`,
     and asserts the returned `grant.principal.machine_id` is the node's
     machine id. Both requests follow
     `crates/gcore/src/grant/handshake.rs::challenge_and_handshake` and reuse
     the test's HTTP client.
  5. The test revokes the node's key with `ApiKeyManager.revoke`. Within one
     default heartbeat interval (30 s), the node's machine reads
     `connected: false` through the observer. The hub's schedule (Decision 10)
     closes the channel within 29 s of a revocation at any phase, so the test
     needs no phase control. From the revocation's return, it polls the
     observer's `GET /api/machines/{node id}` every 0.25 s on a monotonic
     clock. It passes only on a response received before the 30 s deadline
     that reads `connected: false`. A response received after the deadline
     proves nothing, and the test fails.
  6. The next relayed request with the node's key answers 401.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py -q`
after `cargo build -p gobby-daemon` (heavy work).

Consumers unchanged:
- `tests/contracts/test_http_corpus.py` — no-edit-reason: uses the single-daemon `daemon_instance`; the hub and node pairing belongs to the pair test.
- `tests/e2e/test_qa_23120_tmux_address.py` — no-edit-reason: uses the single-daemon `daemon_instance`; the hub and node pairing belongs to the pair test.
- `tests/e2e/conftest.py` — no-edit-reason: the pair test writes the hub flag and the node bootstrap itself.
- `tests/e2e/test_auth_login_e2e.py` — no-edit-reason: the pair test imports its helpers and fixture unchanged.
- `src/gobby/cli/auth_login.py` — no-edit-reason: the node bootstrap it writes is the one 1.2 and 1.3 consume.

**Acceptance:**

- 1.6.1 - Over self-signed TLS, an enrolled bare `gdaemon serve` node, observed ready and registered within bounded waits, shows its machine as `connected` and relays one request (which reports the hub's mode) and one WebSocket upgrade (which receives the hub's pong). The native interactive challenge and the relayed authenticated handshake also complete together and return a grant bound to the enrolled node. Revoking its key closes the channel within one default heartbeat interval (30 s), proved only by an observation received before that deadline, and makes the next relayed request fail with 401. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15434, on the
  Orchestrator's 11:13 CT start with the Adversary gobby#15401. Targets and
  consumers were swept read-only on `0.5.0` at `973b1f4aa8`. The draft is
  narrative only, with no M1.
- 2026-10-05: The single enhancer pass (run `852b6f37`) proposed NC-E01 to
  NC-E08, and the Orchestrator gobby#14972 accepted all eight at 11:56 CT:
  - the pooled client clears ALPN on a clone (NC-E01);
  - Cargo selects `-p gobby-daemon` (NC-E02);
  - the pair test depends on `e2e_pre_daemon_setup` (NC-E03);
  - `futures-util` becomes a normal dependency (NC-E04);
  - the revocation bound is one heartbeat interval, 30 s in the pair test
    (NC-E05);
  - the dial address is derived separately (NC-E06);
  - 1.3.1 asserts the post-interval write (NC-E07);
  - the pair test proves the challenge and handshake split with a grant bound
    to the node (NC-E08).
- 2026-10-05: On the Orchestrator's 12:11 CT ruling, Decision 1 drops the
  external blocks edges. The five leaves stay active, and the plan expands
  only after #23523 (Live activation of the key cutover) closes. The Adversary
  gobby#15401 reviewed `53a7172` and raised seven blocking findings, all
  accepted:
  - NC-A1: the node removes `X-Gobby-Break-Glass` (Decision 5, 1.2.6).
  - NC-A2: the hub rechecks the key on its own 30 s clock from the ack, so
    silent and Pong-only peers are revoked too (Decision 10, 1.3.2).
  - NC-A3: 1.3.2 revokes a quarter interval after the ack and asserts the
    close within one interval. The pair test revokes at a controlled phase,
    and only an observation received before the deadline counts.
  - NC-A4: the health helpers take `impl Display`, `routes.rs` is a verified
    no-edit consumer, and `strip_trailers` becomes `pub(super)`.
  - NC-A5: 1.4 carries the full machines contract.
  - NC-A6: a per-machine write gate fences replaced connections' bookkeeping
    (Decision 10, 1.3.6).
  - NC-A7: the pair test observes listener readiness and channel registration
    separately, with bounded waits.
- 2026-10-05: The Adversary's recheck of `9713912` reopened NC-A2 and NC-A3
  and raised NC-A8. The Orchestrator's final ruling at 12:40 CT keeps the
  30 s bound and NC-E05, and supersedes its interim message.
  - The timing contract: rechecks every 25 s anchored at the upgrade, a 4 s
    budget per check covering pool acquisition and the query, timeouts and
    errors under the parent's database-error exception, and a normal close
    within 29 s. Repairs follow that contract.
  - NC-A2: `machines` bookkeeping runs in spawned tasks that hold the gate
    until the write completes, and the select loop never awaits it.
  - NC-A8: admission rechecks the key before the ack within a 14 s
    admission, and `connected` starts only at the ack.
  - NC-A3: 1.3.2 adds blocked-write and timeout coverage, and the pair test
    drops its phase logic and requires an observation before 30 s.
- 2026-10-05: The Adversary's recheck of `5e57e4d` confirmed that NC-A2,
  NC-A3, and NC-A8 are resolved, and raised NC-A9. Its repair:
  - Admission watches the close signal during the pre-ack check and the ack
    send.
  - `mark_live` checks the current generation and runs only after a
    successful ack send.
  - A superseded or failed attempt never goes live.
  - 1.3.5 and 1.3.6 cover publication, ack failure, and pre-ack replacement,
    and 1.4.1 reads "acknowledged live channel".
- 2026-10-05: The Adversary's recheck of `db0635d` confirmed NC-A9 and found
  that the ack send had no deadline. The repair:
  - The ack send is bounded by the admission end, which is the upgrade plus
    the hello timeout plus the recheck deadline (14 s). No new timing is
    added.
  - On expiry the handler removes the entry and drops the transport.
  - Every close-frame send is bounded by the recheck deadline.
  - 1.3.5 drives a pending send through the public `send_ack`.
  - The Adversary's preliminary recheck found that this close bound gave the
    revoked-tick close a fresh 4 s, which allowed teardown at 33 s. The
    Orchestrator confirmed at 12:54 CT that the 29 s bound stands. The
    revoked tick's check, 4401 close, and drop now share one absolute
    deadline, the tick plus 4 s, through the public `key_tick`, and 1.3.2
    covers a slow check with a pending close.
- 2026-10-05: V1 consensus on `939ed67`. Seats: Plan Writer gobby#15434,
  Adversary gobby#15401, Orchestrator gobby#14972.
  - The Orchestrator accepted the enhancer's NC-E01 to NC-E08 at 11:56 CT.
  - The Adversary's NC-A1 to NC-A9 are resolved, and no blocking or
    proportionality finding remains.
  - Disagreements resolved:
    - Decision 1: there are no external edges and no deferrals. Expansion
      waits until #23523 (Live activation of the key cutover) closes
      (Orchestrator, 12:11 CT).
    - Revocation timing: the Orchestrator's final ruling at 12:40 CT
      superseded its interim 35 s message. Rechecks run every 25 s from the
      upgrade, with a 4 s budget per check covering pool acquisition and the
      query. A timeout or error falls under the parent's database-error
      exception. Teardown completes within 29 s, which keeps NC-E05 and the
      parent's 30 s bound. The Orchestrator confirmed the shared tick budget
      at 12:54 CT.
  - Routing:
    - 1.1 to 1.4 are code leaves: `task_type` feature,
      `implementation_domain` backend, and `tdd` true.
    - 1.5 is a test leaf: `task_type` chore, `assigned_agent`
      backend-developer, and `tdd` false.
    - Dependencies follow the headings: 1.1 has none, 1.2 depends on 1.1,
      1.3 on 1.2, 1.4 on 1.3, and 1.5 on 1.2, 1.3, and 1.4. There are no
      external edges.
  - Decisions 2 and 3 change key-cutover behavior, so Josh approves them
    explicitly along with the stamped bytes.
- 2026-10-10: Refresh by the Plan Writer gobby#15987 for the Orchestrator
  gobby#15977 (Lane 2, Rust Front Door Stage 1, #21543), against `0.5.0` at
  `879ca705d8`, after #23519 and #23520 landed the key cutover. No enhancer
  ran (Orchestrator ruling). The 2026-10-05 consensus and its M1 are
  superseded: the stale M1 is withdrawn, and the Adversary derives a new one
  at consensus. Earlier entries keep the five-leaf numbering. Changes:
  - A new leaf 1.2 (mode container) precedes the relay, so #23457 (atomic
    gdaemon lifecycle) can build on `state.rs`. The old 1.2 to 1.5 become 1.3
    to 1.6, and the D2 mapping, Decision 16, Granularity, and every
    cross-reference follow. The node's startup checks, the native
    interactive challenge, and the `health` `Display` change move into 1.2.
  - A new framing section, Lifecycle Ownership Settlement (#23457), records
    the Orchestrator-approved settlement (a) to (d), the post-1.2 contract,
    the `handle` order, and field ownership.
  - Decision 3 and the pair test's step 4 carry the landed proof,
    `HMAC-SHA256(HMAC-SHA256(api_key, "gobby-interactive-proof-v1"), nonce)`,
    in place of `HMAC(api_key, nonce)`.
  - Decision 2 states that a node builds no `AuthState`, while a
    resolver-less standalone or hub door keeps its 503. Decision 4 adds that a
    node starts no retention task or terminal host.
  - Decision 17 gives the node routes their own pool, because the resolver's
    pool is private. Hub mode now requires `database_url`.
  - `tests/front_door.rs` becomes a Target (its in-process `serve` call, the
    renamed resolver-less test, and its `FrontDoor::new` calls).
  - Landed key-cutover names are stated in the present tense, file sizes and
    line anchors are re-measured, `tests/e2e/test_auth_login.py` is now
    `tests/e2e/test_auth_login_e2e.py`, and Decision 14 aligns
    `parse_files_home_view`'s local-rejection text with Python's.
- 2026-10-10: The Adversary gobby#15989 reviewed `f814cd6a9f` and raised
  NC-R10 to NC-R13, all accepted:
  - NC-R10: 1.5 targets the four in-process callers whose standalone
    `AppState` literals gain `db: None`, and its focused run compiles them.
    `tests/run_modes.rs` matches with `{ .. }` and constructs no variant.
    Settlement point (c) states that a field added to a variant or to
    `BackendServices` updates every direct constructor.
  - NC-R11: one 20 s attempt timeout bounds the node's dial, TLS handshake,
    upgrade, hello, and ack, and replaces the 15 s ack timeout. After the
    ack, the node drops a hub silent past the idle timeout, and each ping
    send runs under that deadline (Decisions 9 and 11, 1.4). 1.4.3 adds a
    peer that stalls before the upgrade and one that goes silent after the
    ack.
  - NC-R12: `connected` means only that a live channel exists. The hub's
    own machine has no special case (Decision 12, 1.5).
  - NC-R13: Decision 14 drops the stale bind-host divergence. Both parsers
    default to `127.0.0.1`, and the vectors no longer require `bind_host`.
  - V2 states that its checks are completion gates that have not run.
- 2026-10-10: The Adversary's recheck of `4acd5ecd7b` confirmed NC-R10 to
  NC-R13 and raised NC-R14: every Consumers unchanged inventory sat after its
  Acceptance block, where the validator ignores it. Each inventory now
  precedes Acceptance. Entries that another leaf edits leave the inventories,
  and their reasons move into prose: `proxy.rs` and `ws.rs` in 1.2's research
  context, `run_modes.rs` and `tests/front_door.rs` in 1.3's focused
  verification, and `run_modes.rs` in 1.5's focused verification.
- 2026-10-10: Consensus on `2ee7957aef`. Seats: Plan Writer gobby#15987,
  Adversary gobby#15989, Orchestrator gobby#15977.
  - The Adversary's NC-R10 to NC-R14 are resolved, and no blocking or
    proportionality finding remains. No enhancer ran (Orchestrator ruling).
  - Disagreements: none open. For NC-R12 the Writer took the Adversary's
    preferred option: `connected` means only that a live channel exists, with
    no special case for the hub's own machine.
  - Routing:
    - 1.1 to 1.5 are code leaves: `task_type` feature,
      `implementation_domain` backend, and `tdd` true.
    - 1.6 is a test leaf: `task_type` chore, `assigned_agent` developer, and
      `tdd` false. The Orchestrator ruled on 2026-10-10 that Lane 2's
      developer seats write the pair test.
    - Dependencies follow the headings: 1.1 has none, 1.2 depends on 1.1,
      1.3 on 1.2, 1.4 on 1.3, 1.5 on 1.4, and 1.6 on 1.3, 1.4, and 1.5.
      There are no external edges.
  - The Lifecycle Ownership Settlement (#23457) stands as written. After
    expansion the Orchestrator adds `#23457 blocked_by <the 1.2 task>`.
    Neither plan carries a manifest edge to the other.
  - Expansion still waits until #23523 (Live activation of the key cutover)
    closes (Decision 1).
  - Validation: `gobby-plans:validate_plan` passes on this revision. The
    project-aware `uv run gobby plans validate` passed on its narrative and
    otherwise times out intermittently on the daemon's slow label-filtered
    `list_tasks` (#23932, plan validation times out on slow label lookups).
  - Decisions 2 and 3 change key-cutover behavior, so Josh approves them
    explicitly along with the stamped bytes.
- 2026-10-11: Refresh by the Plan Writer gobby#16361 for the Orchestrator
  gobby#15977, against `0.5.0` at `093e210fb7`, with the Adversary
  gobby#16364. #23457 (Atomic gdaemon lifecycle ownership and native standby
  cutover) landed `RuntimeState` and gcore's `RunMode` in place of the
  settlement's `AppState` and `ModeServices`, and its task records the
  re-settlement that #23274 creates no second container. So:
  - The Lifecycle Ownership Settlement becomes Landed #23457 Baseline.
    Settlement points (a) and (b) are superseded, (c) and (d) survive, and
    the Orchestrator adds no `#23457 blocked_by` edge.
  - Decision 16 is replaced. 1.2 becomes node startup:
    `RuntimeState::prepare` returns `Prepared::Node` with only the PID claim,
    `NodeServices` holds the node's state, `run` builds `AuthState` only for
    a backend mode, and a new public `serve_doors` serves prebuilt doors.
    This also fixes the landed node path, which built a backend supervisor
    and so launched Python.
  - 1.1 completes the landed parser's Python parity in place, adopts Python's
    boolean and `datastore_mode` messages, and repairs the three Rust tests
    that write remote bootstraps Python rejects.
  - 1.3 to 1.5 rewire onto `NodeServices`, `FrontDoorState.native`, and
    `run`'s two arms. The in-process `serve` callers need no edit, and 1.4
    and 1.5 drop their test-literal Targets.
  - 1.6 drops the secret and parent-fd clauses and refreshes its anchors.
  - Decisions 1, 2, 4, 8, 12, 14, 15, and 17, the Constraints, and V2 are
    refreshed. #23523 closed on 2026-10-11, so the expansion gate is met. The
    Orchestrator manages #24075 (lands #23269's node refusal) as its own
    edge, and no leaf needs it.
  - The stale M1 block is withdrawn, and the Adversary derives a fresh one
    after consensus.
  - Decisions 2 and 3 still need Josh's explicit approval.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan
validation, none has run yet. Each runs after leaves 1.1 to 1.6 land, and each
must hold before the expanded epic closes.

1. The focused suites of 1.1 to 1.5 must pass together against the test hub,
   with `DATABASE_URL` set.
2. `cargo nextest run -p gobby-daemon --test front_door --test lease --test ws_golden_proxy --test http_contracts --test heartbeat`
   (heavy work) must pass, with the loopback bodies and #23457's runtime gates
   unchanged.
3. `tests/e2e/test_hub_node_pair.py` and `tests/e2e/test_auth_login_e2e.py`
   must pass against isolated daemons.
4. `cargo tree -p gobby-daemon -i aws-lc-rs` must print nothing.
5. `uv run gobby plans validate .gobby/plans/gdaemon-node-channel.md -p <project-root>`
   is the only planning-time check, and it must keep passing.
