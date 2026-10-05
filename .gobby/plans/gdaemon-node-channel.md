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
front-door identity, and shared-token cutover), approved by Josh at `b0f2ce2`.

**The problem.** A node bootstrap (`datastore_mode: remote`) can enroll against
a hub (`gobby auth login`, 4.5), but nothing runs on the node afterwards. 4.6
refuses the Python daemon on a node, and gdaemon's front door can only proxy to
loopback Python. key-cutover makes a node's gdaemon answer every `gobby_`
bearer with 503 `key_resolver_unavailable` (its Decision 15) until this plan
lands. The hub has no record of which nodes are live, and no route lists
machines.

**When the leaves close:**
- gdaemon's Rust bootstrap parser derives the same run mode as Python and
  rejects `(remote, true)` (1.1).
- A node's front door relays every HTTP request and WebSocket upgrade to its
  hub over one pooled connection pinned to `hub_cert`. It answers only the
  interactive handshake challenge itself, with its own key (1.2).
- The node holds a WebSocket channel to the hub. The hub registers it,
  records heartbeats, replaces it atomically on reconnect, and closes it when
  the node's key is revoked (1.3).
- `GET /api/machines` and `GET /api/machines/{id}` list the caller's machines
  with their connection state (1.4).
- A hub-node pair test proves the whole path over self-signed TLS (1.5).

## Decision Record
`kind: framing`

Plan Writer decisions on 2026-10-05, drawn from the parent's settled D2
design, the plan of record's 4.4, and the PD disposition in
`.gobby/plans/gdaemon-front-door.md` (lines 430-446). The Orchestrator flags
Decisions 2 and 3 to Josh at approval, because they change behavior that the
approved key-cutover plan specified.

1. **Every leaf waits on key-cutover's live activation.** The Orchestrator
   ruled on 2026-10-05 at 11:13 CT that implementation leaves depending on the
   key cutover take blocks edges to #23523 (Live activation of the key
   cutover). All five leaves depend on it:
   - 1.1 edits `HubDatabaseBootstrap`, which key-cutover 1.3 (#23519) gives
     `api_key` and a manual `Debug`.
   - 1.2 to 1.4 edit `serve.rs`, `front_door/mod.rs`, `front_door/proxy.rs`,
     `front_door/ws.rs`, and the gdaemon test harness, all #23519 targets. They
     also rely on its key resolver and its resolved identity headers.
   - 1.5 runs against a hub whose front door validates keys.

   The plan parser cannot name another plan's task, so the Orchestrator wires
   the five edges at expansion (the precedent is the 4.3-on-3.3 edges in
   `.gobby/plans/gdaemon-front-door.md`, lines 427-428).
2. **Node mode supersedes key-cutover Decision 15 and is exempt from its
   Decision 6.** A node's gdaemon validates nothing and holds no table. It
   builds no key resolver and needs no `GOBBY_FRONT_DOOR_SECRET`, because no
   Python backend sits behind it to trust that secret. It forwards the bearer
   untouched. The hub's front door validates the key and forwards the resolved
   identity to the hub's Python. The 503 `key_resolver_unavailable` answer for
   node-mode `gobby_` bearers is removed.
3. **The interactive handshake challenge stays native on a node.** gcore's
   grant client checks the proof against its own bearer: `expected =
   HMAC(bearer, nonce)` (`crates/gcore/src/grant/handshake.rs`, lines 217-221).
   On a node that bearer is the node's `api_key`. If the challenge were
   relayed, the hub would sign it with the hub's key and the client would
   refuse. So a node answers `POST /api/runtime/handshake/challenge` of kind
   `interactive` with key-cutover's native handler, which reads `api_key` from
   the node's own bootstrap. It relays everything else, including the
   `managed` kind and the authenticated handshake that follows the challenge.
4. **A node holds no datastore credentials.** The S1.4 roadmap row
   (`docs/architecture/gobby-v1.0.0-roadmap.md`, "Nodes hold no datastore
   credentials"), key-cutover Decision 15, and the bootstrap that
   `tests/e2e/test_auth_login.py::_node_home` writes all agree that a node has
   no `database_url`. api-keys Decision 2's remark that "a node's
   `database_url` is the hub's" describes the pre-4.6 lease hazard, not a
   requirement. In node mode gdaemon never reads `database_url`.
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
   - **Peer header.** `observe_peer` still runs on the node, and the hub
     overwrites `X-Forwarded-For` with the node's address. So the loopback
     break-glass (key-cutover Decision 4) is never admitted through a node,
     which is intended: break-glass is host-local.
6. **Pinning.**
   - An `https` hub requires `hub_cert`. The relay and the channel trust only
     that certificate, through `front_door/tls.rs::pinned_client_config`.
   - An `http` hub, which `gobby auth login` permits for loopback hubs or with
     `--insecure`, is relayed in plaintext. `hub_cert` is ignored there,
     because login removes it for such hubs.
   - A node refuses to start without `api_key` (it has not enrolled), or with
     an `https` hub and no `hub_cert`. Both messages name `gobby auth login`.
7. **The typed 503 names the relay target.** `health::unavailable` and
   `health::bad_gateway` take the target as text. Loopback callers pass the
   socket address's text, so their output is byte-identical. A node passes
   `hub_daemon_url`.
8. **Node routes are native mode routes, not route families.**
   `front_door/routes.rs` families default to `proxy` and forward to Python,
   which has no node or machines routes. So `/api/nodes/channel` and
   `/api/machines` are mounted natively by mode, ahead of the route table and
   of the WebSocket splice:

   | Mode | `/api/nodes/channel` | `/api/machines` |
   | --- | --- | --- |
   | Hub | served natively | served natively |
   | Standalone | not mounted, so the request reaches Python, which answers 404 | served natively |
   | Node | relayed to the hub | relayed to the hub |
9. **Channel protocol.**
   - **Open.** The node opens `GET /api/nodes/channel` (WebSocket) with
     `Authorization: Bearer <api_key>`. key-cutover's front door validates the
     key before the upgrade.
   - **Hello and ack.** The node sends
     `{"type":"hello","node_version":"<gdaemon version>","platform":"<os>/<arch>"}`
     and the hub answers `{"type":"ack","machine_id":"<uuid>"}`. The hub
     validates `platform` and does not store it in Stage 1.
   - **Heartbeat.** The node sends a WebSocket ping every 30 s. Only hello,
     ack, and ping/pong exist in Stage 1; S2.7 and S2.8 add commands.
   - **Close codes.** 4401 when the key is revoked (Python's
     authentication-close code), 4409 when a newer connection replaces the
     channel, 4408 when the hub hears nothing within the idle timeout, and
     1008 when the first frame is not a valid hello.
   - **Reconnect.** The node reconnects after any close or error with backoff:
     1 s, doubling to 60 s, and reset after an ack. It reads `api_key` from its
     bootstrap on each attempt, so a rotation (key-cutover 1.4) takes effect
     at the next reconnect.
10. **Heartbeat bookkeeping on the hub.**
    - The hub writes `machines.last_heartbeat_at` and `node_version` at hello,
      then on a ping only when at least 60 s have passed since its last write.
    - On every ping it re-reads the key row. A revoked or missing row closes
      the channel with 4401.
    - A database error on a ping keeps the channel open and is retried at the
      next ping. Relayed requests already fail closed through the resolver's
      503, and the channel carries no commands in Stage 1.
    - The hub closes a channel that sends nothing within 90 s (three ping
      intervals), so a partitioned node stops reading as `connected`.
11. **Timings are constructor arguments, not configuration.**
    - `ChannelTiming` carries the ping interval, the backoff start and
      ceiling, the ack and hello timeouts, the idle timeout, and the heartbeat
      write interval. `ChannelTiming::default()` holds the values above.
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
      machine", so the hub's own row is `false`.
    - Errors use gdaemon's existing body `{"error": msg, "code": code}`:
      - a request with no resolved key identity is 401 `missing_auth`;
      - an unknown, foreign, or malformed id is 404 `machine_not_found`;
      - a query error is 503 `machines_unavailable`.
13. **Dependencies.** These follow the `crates/gdaemon/Cargo.toml` rule "ring
    only … aws-lc-rs stays out of the build". Every crate below is already in
    `Cargo.lock`:
    - 1.2 adds
      `hyper-rustls = { version = "0.27", default-features = false, features = ["http1", "ring", "tls12"] }`
      (lockfile 0.27.7), for the pooled pinned relay client.
    - 1.3 adds `"ws"` to gdaemon's `axum` features (lockfile 0.8.9), for the
      hub endpoint, and
      `tokio-tungstenite = { version = "0.26", default-features = false, features = ["handshake"] }`
      (lockfile 0.26.2), for the node client over a stream gdaemon dials and
      pins itself.
14. **Run-mode parity uses one shared vector file.**
    - `tests/fixtures/bootstrap_run_modes.json` holds bootstrap mappings with
      either the expected mode and normalized `hub_daemon_url` or the expected
      error text. The Python and Rust tests both read it, following the
      `tests/fixtures/terminal_ws_golden` precedent that
      `crates/gdaemon/tests/ws_golden_proxy.rs` reads through
      `CARGO_MANIFEST_DIR`.
    - Rust `parse_hub_origin` lowercases the host, as Python's `urlparse`
      `hostname` does.
    - Every vector sets `bind_host` explicitly, because the default bind hosts
      differ: Python uses `localhost` and Rust uses `127.0.0.1`. That divergence
      predates this plan and is reported to the Orchestrator as found work.
    - Every local vector carries `files_home`, which only Python's parser
      requires.
15. **No activation section.** Each leaf validates against isolated daemons
    and never restarts the running daemon. The Orchestrator promotes the
    coherent binary set and restarts under the per-landing restart rule
    (`.gobby/roles/roster.md`, `2940eb9d01`). No node runs today, and until P5
    (#21554, Singleton lease and backend lifecycle in Rust) an operator starts
    a node by hand with `gdaemon serve`.
16. **The mode container arrives with its first mode-only handle.** 1.2
    creates `crates/gdaemon/src/state.rs`:
    - `AppState` holds `ModeServices::{Standalone, Hub, Node(NodeServices)}`
      with no `Option` mode fields. This is the run-modes slice's former 2.2
      obligation (`.gobby/plans/completed/gdaemon-run-modes.md`, Decision 4).
    - In 1.2 `NodeServices` holds the relay.
    - 1.3 adds `HubServices` (the resolver pool and the registry) and the
      node's channel task.
    - 1.4 gives `Standalone` the pool.

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
  - `cargo build`, `clippy`, and `nextest` are heavy work under
    `.gobby/roles/_common.md`. They pause while the heavy-work hold is in
    force and need no other admission.
  - After each dependency change, `cargo tree -p gdaemon -i aws-lc-rs` prints
    nothing.
- **No secret values in logs.** No new code logs the API key, a relayed
  `Authorization` value, or a channel bearer. The manual `Debug` that
  key-cutover gives `HubDatabaseBootstrap` redacts `api_key`, and 1.1's new
  fields are not secret.
- **No backward compatibility.** 0.5.0 has not shipped.
- **Monolith ceiling.** The targeted production files are small:
  - gdaemon `serve.rs` is 390 lines, `front_door/mod.rs` 117,
    `front_door/proxy.rs` 137, `front_door/health.rs` 148, and
    `front_door/ws.rs` 66.
  - gcore `bootstrap.rs` is 906 lines, about 380 of them production code
    before `#[cfg(test)]`. `bootstrap/front_door.rs` is 282.

  key-cutover 1.3 grows several of these first, so each leaf re-measures and
  splits any file it would push past 850 production lines.
- **key-cutover names.**
  - Names that key-cutover 1.3 creates come from its approved plan at
    `b0f2ce2`: the resolved identity headers `X-Gobby-User-Id`,
    `X-Gobby-Machine-Id`, and `X-Gobby-Key-Id`, the auth state that
    `FrontDoorState` carries, the resolver pool, the native challenge
    handler, and `read_api_key_at`.
  - Each leaf re-resolves them with `gcode` after #23519 lands and does not
    target the files that 1.3 creates.
- **Consumer sweeps.**
  - Targets were swept read-only on `0.5.0` at `973b1f4aa8` (2026-10-05).
  - `gcode grep -w` for `HubDatabaseBootstrap`, `unavailable`, `bad_gateway`,
    `FrontDoorState`, `serve`, `parse_hub_origin`, `read_files_home_view`,
    and `yaml_bool`.
  - `gcode grep` for `/api/machines`, `/api/nodes`, `hub_cert`, and
    `last_heartbeat_at`.
  - No `/api/machines` or `/api/nodes` consumer exists in `src/`, `crates/`,
    `tests/`, or `web/`.

## Parent D2 Mapping
`kind: framing`

Every api-keys D2 item maps to an item here:

| api-keys item | Here | Note |
| --- | --- | --- |
| D2.1 (4.4.1) | 1.2.1 | |
| D2.2 (4.4.2) | 1.3.1 | |
| D2.3 (4.4.3) | 1.3.2 | |
| D2.4 (4.4.4) | 1.4.1 | A test replaces the plan of record's file artifact. |
| D2.5 (4.4.5) | 1.5.1 | The "no maintenance loop" clause is dropped (PD disposition, plan of record lines 437-441). |
| D2.6 (4.4.6) | 1.2.3 | |
| D2.7 | 1.1.1, 1.1.2 | Rust in 1.1.1; Python on the same vectors in 1.1.2. |

## P1: Node relay, channel, and machines API
`kind: framing`

**Goal**: an enrolled node reaches its hub through a pinned relay and a live
channel, and the hub can list machines with their connection state.

**Granularity:** five leaves. Mode parsing (1.1) is a gcore parser with its own
parity vectors. The relay (1.2) and the channel (1.3) are separate lifecycle
owners: per-request forwarding versus a long-lived registry and a reconnecting
client. Each is independently testable. The machines API (1.4) is a read model
over the registry. The pair test (1.5) needs all three. Each leaf stays at or
below six acceptance items.

### 1.1 Rust bootstrap derives the run mode the way Python does [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: `HubDatabaseBootstrap` gains `datastore_mode`, `hub`, `hub_daemon_url`, `hub_cert`, and `run_mode()`; `parse_hub_database_bootstrap` calls the mode parser; `parse_hub_origin` lowercases the host; registers and re-exports `run_mode`
- `crates/gcore/src/bootstrap/run_mode.rs`
- `crates/gcore/src/bootstrap/front_door.rs::*` — scope-reason: `yaml_bool` becomes `pub(super)` so `hub` parses with the same YAML-bool rules as `front_door.enabled`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: the default `HubDatabaseBootstrap` literal in `load_enabled_bootstrap` gains the new fields
- `tests/fixtures/bootstrap_run_modes.json`
- `crates/gcore/tests/bootstrap_run_modes.rs`
- `tests/config/test_bootstrap_run_modes.py`

**Research context:**
- `crates/gcore/src/bootstrap.rs` defines the following:
  - `HubDatabaseBootstrap` (line 43) has `database_url`, `daemon_url`,
    `bind_host`, `daemon_port`, `websocket_port`, and `front_door`.
    key-cutover 1.3 adds `api_key` and a manual `Debug`, so this leaf extends
    that `Debug` with the four new fields.
  - `parse_hub_database_bootstrap` (line 180) reads `bind_host` first, then
    the front-door block.
  - `FilesHomeView` and `parse_files_home_view` (line 261) already parse
    `datastore_mode` as `DatastoreMode::{Local, Remote}`, and
    `hub_daemon_url` through `parse_hub_origin` (line 339). `parse_hub_origin`
    trims a trailing `/` and rejects `@`, `?`, `#`, and `/`, but it neither
    lowercases the host nor checks this process's own origin.
  - `read_files_home_view` has no caller outside `bootstrap.rs`.
- Python (`src/gobby/config/bootstrap.py`) derives the mode as follows:
  - `BootstrapConfig.run_mode()` (line 118) returns `node` for `remote`, then
    `hub` when `hub` is set, else `standalone`.
  - `bootstrap_from_mapping` (line 193) parses `hub` with `_parse_yaml_bool`.
  - `_parse_mode_owner_fields` (line 403) applies these rules:
    - On local it rejects `hub_daemon_url` with "hub_daemon_url is not allowed
      on a local bootstrap; this process is the owner" and requires
      `files_home`.
    - On remote with `hub` set it raises "hub: true requires datastore_mode:
      local".
    - On remote it requires `hub_daemon_url` and rejects this process's own
      origin with "hub_daemon_url must not be this process's own origin".
  - `_own_origins` (line 463) collects `http` and `https` at
    `bind_host:daemon_port`, plus `daemon_url`.
  - `_parse_hub_daemon_url` (line 445) lowercases the host through `urlparse`
    and brackets an IPv6 host.
- Probed on `0.5.0`:
  - `{datastore_mode: remote, hub_daemon_url: "https://HUB.Example:443/"}`
    yields `node` and `https://hub.example:443`.
  - `hub: "yes"` on local yields `hub`.
  - `(remote, hub: true)` raises.
  - A remote bootstrap whose `bind_host: localhost` matches its
    `hub_daemon_url` raises the own-origin error.
- `crates/gcore/src/bootstrap/front_door.rs::yaml_bool` (line 126) is private.
  `bootstrap.rs::front_door_enabled_matches_python` already proves its parity
  with `_parse_yaml_bool`.
- Precedent for a fixture read from a crate test:
  `crates/gdaemon/tests/ws_golden_proxy.rs::corpus` reads
  `../../tests/fixtures/terminal_ws_golden` through `CARGO_MANIFEST_DIR`.

**Implementation:**
- New `crates/gcore/src/bootstrap/run_mode.rs`:
  - `pub enum RunMode { Standalone, Hub, Node }`.
  - `pub(super) fn parse_mode_fields(map, bind_host, daemon_port, daemon_url)`
    returns `(DatastoreMode, bool, Option<String>, Option<String>)` for
    `datastore_mode`, `hub`, `hub_daemon_url` (normalized by
    `super::parse_hub_origin`), and `hub_cert`.
  - It applies Python's rules and messages, including `(remote, true)`, the
    remote requirement, the local rejection, and the own-origin rejection over
    the same origin set as `_own_origins`.
  - It does not require `files_home`, which gdaemon never reads.
- `HubDatabaseBootstrap` gains the four fields and
  `pub fn run_mode(&self) -> RunMode`, mirroring `BootstrapConfig.run_mode`.
- `parse_hub_origin` lowercases the host, so `FilesHomeView` and the mode
  parser normalize alike.
- `tests/fixtures/bootstrap_run_modes.json` holds a `cases` array. Each entry
  has a `name`, a `bootstrap` mapping with an explicit `bind_host`, and either
  `mode` with `hub_daemon_url` or `error`. Cases:
  - standalone, and hub with `true` and with `"yes"`;
  - node, with host lowercasing, a trailing slash, and an IPv6 literal hub;
  - `(remote, true)`;
  - remote without `hub_daemon_url`;
  - local with `hub_daemon_url`;
  - own origin, both through `bind_host` and through `daemon_url`;
  - a hub URL with userinfo, with a query, and with a path.

  Local cases carry `files_home`. Errors compare by Python's message text.
- `crates/gcore/tests/bootstrap_run_modes.rs` and
  `tests/config/test_bootstrap_run_modes.py` each load the file and assert
  every case.

**Focused verification (planned):**
`cargo nextest run -p gobby-core --test bootstrap_run_modes` and
`cargo nextest run -p gobby-core bootstrap` (heavy work), then
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap_run_modes.py tests/config/test_bootstrap.py -q`.

**Acceptance:**

- 1.1.1 - The Rust parser derives the mode, normalized `hub_daemon_url`, and `hub_cert` that every shared vector expects. It rejects `(remote, true)`, a remote bootstrap without `hub_daemon_url`, a local one with it, and this process's own origin, each with Python's message. test: `crates/gcore/tests/bootstrap_run_modes.rs::shared_vectors_match_python`.
- 1.1.2 - Python's `bootstrap_from_mapping` and `BootstrapConfig.run_mode` produce the same outcome on every case of the same vector file. test: `tests/config/test_bootstrap_run_modes.py::test_shared_vectors_match_rust`.

### 1.2 Node relay: a node's front door forwards to its hub over the pinned connection [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/state.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/front_door/relay.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: registers `relay`; `FrontDoor` holds the `AppState`; in node mode `FrontDoor::handle` sends an `interactive` handshake challenge to key-cutover's native handler and relays every other request and upgrade without key authentication
- `crates/gdaemon/src/front_door/ws.rs::*` — scope-reason: `splice` is split so the upgrade exchange runs over a stream the caller has connected, and the relay reuses it over TLS
- `crates/gdaemon/src/front_door/health.rs::*` — scope-reason: `unavailable` and `bad_gateway` take the target as text
- `crates/gdaemon/src/front_door/proxy.rs::*` — scope-reason: `forward` passes its target's text to `unavailable` and `bad_gateway`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` builds the `AppState` from the bootstrap's run mode and, in node mode, requires `api_key` (and `hub_cert` for an `https` hub) but no front-door secret or key resolver; `serve` takes the `AppState`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/tests/nodes.rs`
- `crates/gdaemon/tests/common/mod.rs::*` — scope-reason: its `serve` calls pass a standalone `AppState`, and it gains `start_node_front_door` and a pinned TLS hub stub that counts accepted connections
- `crates/gdaemon/tests/heartbeat.rs::*` — scope-reason: its `serve` call passes a standalone `AppState`
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: `start_front_door` passes a standalone `AppState`

**Research context:**
- `crates/gdaemon/src/front_door/mod.rs`:
  - `FrontDoorState` (line 22) holds `target: SocketAddr`, `backend_state`,
    and `client: ProxyClient`, and is built per listener in `serve.rs::serve`
    from each `PublicListener.backend`.
  - `FrontDoor::handle` (line 56) runs `observe_peer`, then `ws::splice` for
    an upgrade, or else `RouteTable::dispatch`.
  - key-cutover 1.3 adds authentication before both branches and an auth
    state on `FrontDoorState`.
- `crates/gdaemon/src/front_door/proxy.rs`:
  - `client()` builds `Client<HttpConnector, Body>` with a 2 s pool idle
    timeout.
  - `forward` rewrites the URI to `http://<target>`, strips hop-by-hop
    fields and trailers, and maps a connect error to
    `health::unavailable(target, state.backend_state)`.
- `crates/gdaemon/src/front_door/ws.rs::splice` connects a `TcpStream` to the
  target, runs the HTTP/1 handshake with upgrades, sends the request with its
  path and query, and joins both upgrades with `copy_bidirectional`. Close
  codes, ping/pong, and extensions pass through untouched.
- `crates/gdaemon/src/front_door/health.rs::unavailable` (line 39) writes
  `{"status":"unavailable","backend":{"state","target"}}` with
  `target.to_string()`. Its callers are `proxy::forward` and `ws::splice`.
  `crates/gdaemon/tests/front_door.rs` (line 247) asserts the loopback body,
  which stays byte-identical.
- `crates/gdaemon/src/front_door/tls.rs::pinned_client_config` (line 110) trusts
  only the given leaf and sets ALPN `http/1.1`. The test helpers
  `crates/gdaemon/tests/common/mod.rs::{self_signed, start_tls_front_door, tls_connect, refused_addr, ws_backend, frame, read_exact_into}`
  already exist.
- `serve.rs::run` (line 45) loads the bootstrap, binds `daemon_port` and
  `websocket_port` with their backend ports, and calls `serve`. The `serve`
  callers are `run`, `tests/common/mod.rs` (two calls), `tests/heartbeat.rs`,
  and `tests/http_contracts.rs::start_front_door`. key-cutover 1.3 makes
  `run` refuse to start without `GOBBY_FRONT_DOOR_SECRET` and gives `serve`
  an auth state.
- Hub side: `src/gobby/servers/_app_ui.py::_mount_ws_endpoint` serves `/ws` and
  `/ws/{path}` on the HTTP port with the path discarded, and closes 4401 when
  authentication fails.
- `crates/gcore/src/grant/handshake.rs` (lines 207-221) verifies the
  challenge proof as `HMAC(bearer, nonce)` (Decision 3). key-cutover 1.3's
  native challenge handler reads `api_key` from the bootstrap on every
  request.
- `src/gobby/cli/auth_login.py::_publish` writes `api_key`, `api_key_id`, and
  `hub_cert` for an `https` hub, and removes `hub_cert` for an `http` one.

**Implementation:**
- `state.rs` contains the following:
  - `pub struct AppState { pub mode: ModeServices }`.
  - `pub enum ModeServices { Standalone, Hub, Node(NodeServices) }`.
  - `pub struct NodeServices { pub relay: Arc<HubRelay> }`.
  - `AppState::from_bootstrap(&HubDatabaseBootstrap) -> Result<AppState>`.
    For `RunMode::Node` it refuses a missing `api_key` and an `https` hub
    without `hub_cert`, naming `gobby auth login`. It then reads the PEM at
    `hub_cert` and builds the relay.
  - `lib.rs` registers `state`.
- New `front_door/relay.rs` defines `HubRelay { origin, authority, host, tls, client }`:
  - `client` is
    `hyper_util::client::legacy::Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>`,
    built from `pinned_client_config(pem)` with `https_or_http()` and
    `enable_http1()`. Its pool idle timeout is 90 s, so sequential requests
    reuse one TLS connection.
  - `HubRelay::forward(request)` rewrites the URI to `origin` plus the
    original path and query, and sets `Host` to `authority`. It strips
    hop-by-hop fields and trailers with `proxy::strip_hop_by_hop` and
    `strip_trailers`. A connect error maps to `unavailable(origin, Down)`, and
    a TLS or protocol error to `bad_gateway(origin, error)`.
  - `HubRelay::splice(request, to_root_ws: bool)` dials `authority`. For
    `https` it wraps the stream with `tokio_rustls::TlsConnector` and the
    pinned config, using the host as the server name. It sets `Host`,
    rewrites the path to `/ws` (keeping the query) when `to_root_ws`, and
    hands the stream to the split `ws` upgrade exchange.
- `ws.rs` splits `splice` into a connect step and
  `splice_over(stream, request, target: &str)`, so loopback and relay share
  one upgrade exchange.
- `FrontDoor` holds `Arc<AppState>` and knows whether its listener is the
  WebSocket port. In node mode `handle` does the following:
  1. It runs `observe_peer`.
  2. A `POST /api/runtime/handshake/challenge` whose effective kind is
     `interactive` goes to key-cutover's native challenge handler.
  3. Any other upgrade goes to `relay.splice(request, is_ws_listener)`.
  4. Everything else goes to `relay.forward(request)`.

  The node never authenticates a key, never strips or sets identity headers,
  and never consults the route table.
- `serve.rs::run` skips the front-door secret and the key resolver in node
  mode and builds the `AppState` before binding. `serve` takes
  `Arc<AppState>` and passes it to each `FrontDoor`. The node still binds
  both public ports, but the backend ports are unused.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gdaemon --test nodes --test front_door --test ws_golden_proxy --test http_contracts --test heartbeat`
and `cargo clippy -p gdaemon --all-targets -- -D warnings` (heavy work), then
`cargo tree -p gdaemon -i aws-lc-rs` prints nothing.

**Acceptance:**

- 1.2.1 - A node relays two sequential requests to a TLS hub stub over one pinned connection, with the bearer, path, and query unchanged and `Host` set to the hub's authority. When the hub refuses connections it answers 503 with `backend.target` equal to `hub_daemon_url`. test: `crates/gdaemon/tests/nodes.rs::node_relays_over_pinned_tls`.
- 1.2.2 - A node whose `hub_cert` names a different certificate than the hub presents answers 502 and never forwards the request. test: `crates/gdaemon/tests/nodes.rs::node_refuses_an_unpinned_hub`.
- 1.2.3 - A WebSocket upgrade relayed through the node to the hub stub replays the terminal golden corpus and a 1000 close frame byte-equal in both directions. An upgrade on the HTTP listener keeps its path, and an upgrade on the WebSocket listener reaches `/ws` with its query. test: `crates/gdaemon/tests/nodes.rs::node_relays_ws_over_pinned_tls`.
- 1.2.4 - A node answers an `interactive` handshake challenge with `HMAC(node api_key, nonce)` without contacting the hub, and relays a `managed` challenge. test: `crates/gdaemon/tests/nodes.rs::node_answers_interactive_challenge_with_its_own_key`.
- 1.2.5 - `AppState::from_bootstrap` refuses a node bootstrap with no `api_key`, or with an `https` hub and no `hub_cert`, and the error names `gobby auth login`. A spawned `gdaemon serve` with a node bootstrap and no `GOBBY_FRONT_DOOR_SECRET` starts and answers a relayed request. test: `crates/gdaemon/tests/nodes.rs::node_startup_requires_enrollment_but_no_secret`.

### 1.3 Node channel: the hub registers, heartbeats, replaces, and revokes node channels [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/mod.rs`
- `crates/gdaemon/src/nodes/channel.rs`
- `crates/gdaemon/src/nodes/registry.rs`
- `crates/gdaemon/src/state.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: in hub mode `FrontDoor::handle` routes `/api/nodes/channel` to the native channel endpoint after authentication and before `ws::splice`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: hub mode builds `HubServices` from the resolver's pool; node mode spawns the channel client and stops it at shutdown
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/tests/nodes.rs`

**Research context:**
- key-cutover 1.3's front door handles every request as follows:
  - It resolves a `gobby_` bearer against `api_keys` joined to `machines`
    through a pool built from `database_url`.
  - It strips client `X-Gobby-User-Id`, `X-Gobby-Machine-Id`,
    `X-Gobby-Key-Id`, and `X-Gobby-Front-Door` on every path, then sets them
    on success.
  - It authenticates WebSocket upgrades the same way before `ws::splice`.

  So after authentication, those headers inside gdaemon are present only for
  a key-authenticated caller.
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

**Implementation:**
- `nodes/registry.rs` defines the following:
  - `Registry` wraps `std::sync::Mutex<HashMap<Uuid, ChannelHandle>>`.
  - `ChannelHandle { connection_id: Uuid, close: oneshot::Sender<(u16, &'static str)> }`.
  - `register(machine_id) -> (connection_id, close_rx)` assigns a new
    `connection_id`, swaps the entry, and fires the old handle's `close`
    with 4409.
  - `remove(machine_id, connection_id)` removes the entry only while the
    stored `connection_id` is its own.
  - `is_connected(machine_id) -> bool` and `connected_ids()` report the
    entries.
  - No lock is held across an await.
- `nodes/channel.rs` holds the hub endpoint, the node client, and
  `ChannelTiming`:
  - **Hub endpoint.** An axum handler takes `WebSocketUpgrade` and reads
    `X-Gobby-Machine-Id` and `X-Gobby-Key-Id`. A request without them is 401
    `missing_auth` before the upgrade. After the upgrade the handler runs
    these steps:
    1. It waits up to `hello_timeout` for a valid hello and otherwise closes
       1008.
    2. It registers the machine.
    3. It writes `last_heartbeat_at = now()` and `node_version`.
    4. It sends the ack.
    5. It loops on `recv` under `idle_timeout`, closing 4408 when the timeout
       elapses.
    6. On each ping it re-reads `SELECT 1 FROM api_keys WHERE id = $1 AND
       revoked_at IS NULL`. No row closes 4401. A query error keeps the
       channel. It writes the heartbeat when `heartbeat_write` has elapsed.
    7. When it fires, the registry's close signal closes the socket with its
       code.
    8. On exit it calls `registry.remove(machine_id, connection_id)`.

    `node_version` and `platform` are bounded to 64 characters.
  - **Node client.** A task loops `connect → hello → ack → ping every
    ping_interval` with these rules:
    - Each attempt reads `api_key` with key-cutover's `read_api_key_at`.
    - It dials `wss://<authority>/api/nodes/channel` (or `ws://` for an
      `http` hub) over the same pinned TLS setup as the relay, using
      `tokio_tungstenite::client_async_with_config`.
    - After any close or error it sleeps for the backoff, which starts at
      `backoff_start`, doubles up to `backoff_max`, and resets after an ack.
  - **`ChannelTiming`.** Its defaults are a 30 s ping, 1 s and 60 s backoff,
    10 s ack and hello timeouts, a 90 s idle timeout, and a 60 s heartbeat
    write interval.
- `state.rs` changes as follows:
  - `ModeServices::Hub(HubServices { db: Pool, registry: Arc<Registry> })`.
  - `NodeServices` gains the channel task's `JoinHandle`.
  - `lib.rs` registers `nodes`.
- In hub mode, `FrontDoor::handle` sends a request for `/api/nodes/channel`
  to the channel router after authentication and before the upgrade branch.
- `serve.rs` builds `HubServices` from key-cutover's resolver pool in hub
  mode, spawns the node client in node mode, and aborts it at shutdown.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gdaemon --test nodes`
and `cargo clippy -p gdaemon --all-targets -- -D warnings` (heavy work), then
`cargo tree -p gdaemon -i aws-lc-rs` prints nothing.

**Acceptance:**

- 1.3.1 - A key-authenticated channel registers its machine and receives an ack carrying the machine id, and its hello writes `last_heartbeat_at` and `node_version`. A ping within the heartbeat write interval writes nothing. A second connection for the same machine replaces the first, which closes with 4409. When the first channel's cleanup runs after the second ack, the second stays registered. test: `crates/gdaemon/tests/nodes.rs::channel_registers_and_replaces`.
- 1.3.2 - After the channel's key is revoked, the next ping closes the channel with 4401 and the machine leaves the registry. test: `crates/gdaemon/tests/nodes.rs::revoked_key_closes_channel_on_heartbeat`.
- 1.3.3 - The node client sends a hello with the gdaemon version and `<os>/<arch>`, pings at its interval, and reconnects after a close. Against a refusing hub its delays double from the start value to the ceiling, and they reset after an ack. It reads the key again on each attempt. test: `crates/gdaemon/tests/nodes.rs::node_channel_reconnects_with_backoff`.
- 1.3.4 - A channel whose first frame is not a valid hello closes with 1008, and a registered channel silent past the idle timeout closes with 4408 and leaves the registry. test: `crates/gdaemon/tests/nodes.rs::silent_or_malformed_channel_is_closed`.
- 1.3.5 - On a hub, an upgrade to `/api/nodes/channel` without a key identity is refused with 401 before any upgrade. A standalone front door does not mount the route and proxies the request to its backend. test: `crates/gdaemon/tests/nodes.rs::channel_requires_a_key_on_a_hub`.

### 1.4 `/api/machines` lists the caller's machines with connection state [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/machines_api.rs`
- `crates/gdaemon/src/nodes/mod.rs`
- `crates/gdaemon/src/state.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: in standalone and hub modes `FrontDoor::handle` routes `/api/machines` and `/api/machines/{id}` to the native machines router after authentication and before the route table
- `crates/gdaemon/src/serve.rs::*` — scope-reason: standalone mode builds its services with the resolver's pool
- `crates/gdaemon/tests/nodes.rs`
- `docs/guides/configuration.md`

**Research context:**
- `src/gobby/servers/routes/api_keys.py::list_keys` returns `{"keys": [...]}`
  scoped to the caller's user, with ISO timestamps (`_key_summary`). The
  machines API follows that shape (Decision 12).
- Identity: the same key-cutover headers as 1.3, read after authentication.
  The `machines` columns are listed in 1.3.
- `docs/guides/configuration.md` documents the bootstrap keys, including
  `datastore_mode` and `hub_daemon_url`. `docs/guides/shared-stack.md` holds
  the client setup that `gobby auth login` cites.
- No `/api/machines` consumer exists in `src/`, `web/`, `crates/`, or
  `tests/`.

**Implementation:**
- `nodes/machines_api.rs` builds an axum router over `(Pool, Option<Arc<Registry>>)`:
  - `GET /api/machines` selects the columns in Decision 12 where
    `owner_user_id = $1`, ordered by `first_seen, id`. It adds
    `connected = registry.is_connected(id)`, or `false` without a registry.
  - `GET /api/machines/{id}` does the same for one id. An unparsable id, a
    missing row, or a foreign owner is 404 `machine_not_found`.
  - Without `X-Gobby-User-Id` it is 401 `missing_auth`, and a query error is
    503 `machines_unavailable`.
- `ModeServices::Standalone` becomes `Standalone(StandaloneServices { db: Pool })`.
  `FrontDoor::handle` routes the machines paths in standalone and hub modes.
  A node never reaches it, because it relays.
- `docs/guides/configuration.md` gains a "Nodes" section that covers:
  - the node bootstrap fields (`datastore_mode: remote`, `hub_daemon_url`,
    and `api_key`, `api_key_id`, `hub_cert` from `gobby auth login`);
  - that a node runs only `gdaemon serve`;
  - the relay, including the native challenge and the `/ws` target;
  - the channel's heartbeat and close codes;
  - `/api/machines`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 cargo nextest run -p gdaemon --test nodes`
(heavy work).

**Acceptance:**

- 1.4.1 - `GET /api/machines` lists only the caller's machines with the fields in Decision 12, and `connected` is true exactly for machines with a registered channel. `GET /api/machines/{id}` returns one such row and answers 404 `machine_not_found` for another user's machine, an unknown id, and a malformed id. test: `crates/gdaemon/tests/nodes.rs::machines_api_lists_rows_with_connection_state`.
- 1.4.2 - Without a key identity both routes answer 401 `missing_auth`, and a query error answers 503 `machines_unavailable`. test: `crates/gdaemon/tests/nodes.rs::machines_api_requires_a_key`.
- 1.4.3 - The configuration guide documents running a node, its relay and channel, and `/api/machines`. behavior: the "Nodes" section in `docs/guides/configuration.md`.

### 1.5 Hub-node pair test over self-signed TLS [category: test] (depends: 1.2, 1.3, 1.4)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_pair.py`

**Research context:**
- `tests/e2e/conftest.py::spawn_daemon_instance` (line 1092) takes
  `tls="self-signed"` and writes the front door's TLS block into the isolated
  bootstrap. The e2e bootstrap template sets no `hub` flag.
- `tests/e2e/test_auth_login.py` provides the pattern for this test:
  - `self_signed_hub` spawns the hub with `tls="self-signed"`.
  - `_node_home` writes a node bootstrap with only `datastore_mode` and
    `hub_daemon_url`, so the node's ports would default to 60887 and 60888.
  - `_login` runs `gobby auth login` with the e2e password.
  - `e2e_pre_daemon_setup` seeds the login user.
- `tests.fixtures.gdaemon_binary.select_test_gdaemon` selects the gdaemon
  binary under test.
- The hub's `/api/health` returns `"mode": server.bootstrap_config.run_mode()`
  (`src/gobby/servers/routes/admin/_health.py`).
- The hub's WebSocketServer answers `{"type":"ping"}` with `{"type":"pong"}`
  (`src/gobby/servers/websocket/handlers/core.py::_handle_ping`).
- `src/gobby/storage/api_keys.py::ApiKeyManager` has `mint(user_id,
  machine_id, label)` and `revoke(key_id, user_id)`.

**Implementation:**
- The test does its own setup:
  - A fixture writes `hub: true` into the isolated hub bootstrap and spawns
    the hub with `spawn_daemon_instance(..., tls="self-signed")`.
  - It writes a node home with
    `datastore_mode: remote`, `hub_daemon_url: https://127.0.0.1:<hub port>`,
    `bind_host: 127.0.0.1`, and two free ports.
  - It enrolls with `gobby auth login`, accepting the hub's fingerprint.
  - It starts a bare `gdaemon serve` from `select_test_gdaemon()`, with
    `GOBBY_HOME` set to the node home and no `GOBBY_PARENT_FD` or
    `GOBBY_FRONT_DOOR_SECRET`.
  - It imports `_login` and `TEST_EMAIL` from `tests.e2e.test_auth_login`
    instead of copying them.
- Then it asserts these steps in order:
  1. Through the node's loopback front door with the node's `api_key`,
     `/api/health` reports `"mode": "hub"`.
  2. `/api/machines` lists the node's machine as `connected`.
  3. A WebSocket on the node's `websocket_port` receives a pong for a ping.
  4. The test revokes the node's key with `ApiKeyManager.revoke`. Within
     40 s, the node's machine reads `connected: false` through an observer
     key that the test minted for the same user with `ApiKeyManager.mint`,
     dialing the hub directly over the pinned certificate with
     `ssl.create_default_context(cafile=…)`.
  5. The next relayed request with the node's key answers 401.
- The test cleans up by terminating the node gdaemon.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_pair.py -q`
after `cargo build -p gdaemon` (heavy work).

**Acceptance:**

- 1.5.1 - Over self-signed TLS, an enrolled bare `gdaemon serve` node shows its machine as `connected` and relays one request (which reports the hub's mode) and one WebSocket upgrade (which receives the hub's pong). Revoking its key closes the channel within 40 s and makes the next relayed request fail with 401. test: `tests/e2e/test_hub_node_pair.py::test_node_enrolls_and_relays`.

Consumers unchanged:
- `tests/contracts/test_http_corpus.py` — no-edit-reason: uses the single-daemon `daemon_instance`; the hub and node pairing belongs to the pair test.
- `tests/e2e/test_qa_23120_tmux_address.py` — no-edit-reason: same.
- `tests/e2e/conftest.py` — no-edit-reason: the pair test writes the hub flag and the node bootstrap itself.
- `crates/gdaemon/tests/front_door.rs` — no-edit-reason: the loopback 503 and 502 bodies stay byte-identical (Decision 7), and it spawns `serve` without a node bootstrap.
- `crates/gdaemon/tests/ws_golden_proxy.rs` — no-edit-reason: the loopback splice keeps its behavior; 1.2.3 replays the same corpus through a node.
- `src/gobby/cli/auth_login.py` — no-edit-reason: the node bootstrap it writes is the one 1.2 consumes.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15434, on the
  Orchestrator's 11:13 CT start with the Adversary gobby#15401. Targets and
  consumers were swept read-only on `0.5.0` at `973b1f4aa8`. The draft is
  narrative only, with no M1.

## V2: Verification
`kind: verification`

After every leaf has passed:

1. Run the focused suites of 1.1 to 1.4 together against the test hub, with
   `DATABASE_URL` set.
2. Rerun `cargo nextest run -p gdaemon --test front_door --test ws_golden_proxy --test http_contracts`
   (heavy work), and confirm the loopback bodies are unchanged.
3. Run `tests/e2e/test_hub_node_pair.py` and `tests/e2e/test_auth_login.py`
   against isolated daemons.
4. Confirm `cargo tree -p gdaemon -i aws-lc-rs` prints nothing.
