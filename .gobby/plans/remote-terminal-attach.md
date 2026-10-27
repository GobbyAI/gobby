Plan artifact: `.gobby/plans/remote-terminal-attach.md`

# Remote terminal attach: hub-wide roster, relay routing, and single-use relay tokens

**Plan ID:** remote-terminal-attach

## Overview
`kind: framing`

#20202 (Stage 4 S4.3 under #21546) plans remote gclient attach: a terminal
that runs on one machine is listed, opened, viewed and typed into from a
gclient on another machine of the same owner. Its validation criteria name four
items, and each maps to sections here:

| #20202 item | Sections |
| --- | --- |
| Hub-wide terminal roster | 3.1, 4.1 |
| `terminal.machine_id` to endpoint resolution | 2.2 (live node channel or typed `node_offline`) |
| Per-terminal attach token mint, expiry and revocation | 1.1, 2.1 |
| Hub-node relay with backpressure | 1.2, 2.2, 3.2, 3.3 |

**Landed prerequisites, stated as prerequisites:** the gclient frame-source
trait and `ProxyFrameSource` with `FrameDelivery` Auto/Direct/Proxy
(`crates/gclient/src/frame_source.rs`, `crates/gclient/src/frame_source/proxy.rs`),
and the daemon-addressable client (`--daemon-url`). `FrameDelivery::Auto` already
picks proxy for a row with no direct locator, and `ProxyFrameSource::send_input`
already routes proxied keystrokes through the daemon's `terminal_input`.

**Carried deferrals:**
- herdr-client-completion D2 (`.gobby/plans/completed/herdr-client-completion.md`,
  original acceptance items 5.1.2 and 5.1.3) deferred remote attach and its
  capability tokens to #20202. 2.1 owns the token lifecycle and 3.2 the remote
  attach.
- herdr-terminal-client C1 deferred attach tokens until a remote or web-direct
  surface exists. Remote attach is that surface; D2 below chooses the token.
- gclient-workspace-sidebar D1 deferred 3.2.4: "an agent row of another machine
  appears under `all`, attaches through the hub, and shows the machine token".
  4.1 carries it. The roster feeds the views as follows: hub-mode 3.1/3.2 already
  give every roster agent its `machine_id`, so `machine_admits` shows another
  machine's agents under `all` and under that machine's own filter. 3.1 makes the
  terminal inventory owner-wide so their terminal rows resolve on the hub.

**Inputs weighed:** #20203 (hosted terminal-relay privacy stance), #25068 (native
agent-token and runtime-grant admission seam), #24452 (capability-token
verification), #25187 (gclient arrange and move-pane). Their dispositions are in
the Decision Record.

## Decision Record
`kind: framing`

D1 through D5 are Orchestrator rulings (gobby#17281, relayed by Triage
gobby#17480 on 2026-10-27) made on Josh's behalf while he was offline. Each is
reversible at plan approval.

1. **D1 — Hub relay only (Orchestrator ruling on Josh's behalf).** A client never
   dials a node. Every gclient already reaches the hub: a node front door splices
   every WS and relays every HTTP request to the hub
   (`crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`). The hub resolves a
   row's `machine_id` to that node's live channel in
   `crates/gdaemon/src/nodes/registry.rs::Registry`, or answers a typed
   `node_offline`. `machines` gains no endpoint column (ROADMAP decision 17: a
   thin node holds no hub credentials and serves no hub data).
   - Rejected: a node endpoint column with client-to-node dials. It needs node
     listeners reachable from every client, a second authentication surface on
     each node, and a column nothing else reads.
   - Consequence: a gclient on node B viewing node B's own terminal also goes
     through the hub (a hairpin). The hub holds no host epoch or socket path for
     B, so it cannot hand out a direct locator for B, and one path keeps the
     owner check in one place.

2. **D2 — Single-use relay token (Orchestrator ruling on Josh's behalf; Josh to
   confirm at plan approval).** The hub-mode plan's relations note
   (`.gobby/plans/hub-mode.md`) requires the attach-token model as its own
   decision record entry with Josh. For each remote attach the hub mints 32
   random bytes (base64url, no padding) and sends them in the new node command
   `terminal.relay`. The node opens a dedicated relay WebSocket back to the hub
   at `/api/nodes/terminal-relay`, authenticated with its machine key and
   carrying the token. The hub accepts a token once, within 10 s of minting,
   only from the minted machine on the minted channel connection, and binds the
   socket to the minted terminal and attachment. The hub keeps only the
   token's SHA-256 digest, in memory. Revocation: the waiting attach removes its
   entry on every exit (success, node error, timeout, client close, channel
   loss), and a live relay ends when its attachment finalizes, its channel closes,
   or the channel's 25 s key recheck closes the channel for a revoked key.
   Frames use a second connection because the node channel is command/reply
   with a closed `CommandFrame` enum and a key recheck; bulk terminal bytes on it
   would delay commands and heartbeats.
   - Rejected (b): stateless signed capability tokens aimed at a future
     web-direct surface. The same hub process mints and checks the token, single
     use and revocation need state anyway, and a signed token would require a
     node to hold the hub's signing key. No web-direct surface exists to justify
     it. #24452's HMAC agent tokens are not reused for the same reasons.
   - Rejected (c): amend #20202's criterion to drop tokens and accept any
     WebSocket from an authenticated node key. A node key alone cannot say which
     terminal and attachment a socket serves, so a compromised node could attach
     its stream to another attachment of the same machine.

3. **D3 — Open the remote terminal in the owning machine's workspace
   (Orchestrator ruling on Josh's behalf).** Workspaces are per machine:
   `src/gobby/terminals/workspace_pane_access.py::WorkspacePaneAccess._adoptable`
   refuses a terminal not live on the workspace's node, and #25187 refuses
   cross-machine move-pane. gclient switches its attach target to the owning
   machine's workspace and opens the terminal there.
   - Rejected (b): an unpersisted viewer tab in the local workspace. It invents
     a second pane kind that workspace ops refuse to place.

4. **D4 — Rust only, on S2.8 M2 (Orchestrator ruling on Josh's behalf).** The hub
   side builds on the native terminal family of
   `.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md` P2 and is blocked
   by #24575 (inventory), #24577 (WS state machine), #24578 (bounded proxy relay),
   #24579 (WS input) and #24580 (activation). Remote viewing goes live at Native
   activation. No transitional Python relay is built.

5. **D5 — In-memory relay, no payload persistence or logging (Orchestrator ruling
   on Josh's behalf).** The hub and node relay frames in memory only, persist and
   log no frame or input payload and no token, and front-door TLS protects the
   transport when configured. Listing, attach and input check that the caller
   owns the row's machine. #20203's hosted stance stays separate, with no edge.

6. **Crate seam.** `gobby-terminals` cannot depend on `gobby-daemon`. The terminal
   family therefore takes one injected `RemoteTerminals` trait object (new, in
   `crates/gterminals/src/remote.rs`) with three operations: open a relay, write
   input and resize the PTY. gdaemon implements it over the node registry. It is the only
   production implementation; the trait exists because the dependency points
   the other way, and tests use a fake.

7. **Input path.** A proxied holder's `terminal_input` for a remote attachment is
   sent as the existing node command `terminal.write`. The relay socket never
   carries input (memory b59e4ce9: proxied panes stay daemon-bound). #25201 owns
   the Python-originated remote write branch in `RustWriteCoordinatorClient`;
   this plan's WS input never passes through that client, so the two do not
   overlap.

8. **Agent-token callers.** A request admitted on a `gobby-agent-v1.` bearer has no
   `x-gobby-user-id`, so it keeps today's local-machine inventory and cannot
   attach a remote row. #25068's seam may later give such callers a user; nothing
   here waits on it.

## Constraints
`kind: framing`

- Monolith ceiling: `crates/gdaemon/src/node_commands.rs` is 979 lines, so 1.1
  splits its terminal command types into a new module. No other targeted
  hand-maintained production file reaches 850 lines.
- Every deliverable that changes `gdaemon` or its path dependency
  `gobby-terminals` bumps `gdaemon` by 0.0.1 in `crates/gdaemon/Cargo.toml`,
  `Cargo.lock` and the `gdaemon` value of `MANAGED_BIN_VERSION_PINS`. 4.1 bumps
  `gclient` the same way. Because those three files are shared, the deliverables
  form one dependency chain.
- `src/gobby/install/version_pins.py::*` is targeted file-wide: the file holds one
  pin table, `UNPUBLISHED_MANAGED_BINS` and `is_published`, and a bump changes one
  dict value. Consumer sweep (`gcode grep -w MANAGED_BIN_VERSION_PINS src tests`,
  2026-10-27) found readers that look pins up by binary name and need no edit:
  `src/gobby/cli/install_setup_{gclient,gcode,gdaemon,ghook,gmcp,gterm,versions}.py`,
  `src/gobby/code_index/gcode_versions.py`, `src/gobby/hooks/runtime_compat.py`,
  `src/gobby/install/{bin_freshness_models,distribution}.py`, and their tests.
- Files created by blocking work are bare-path Targets: `crates/gterminals/src/inventory.rs`
  and `crates/gterminals/tests/inventory.rs` (#24575), `crates/gterminals/src/ws.rs` (#24577), `crates/gterminals/src/relay.rs`
  (#24578), and `crates/gdaemon/src/nodes/agents/commands.rs` (#24376). If one of
  them is indexed before expansion, the expansion pass rescopes that Target to
  exact symbols.
- Node terminals are run-backed. On a node,
  `crates/gdaemon/src/nodes/agents/state.rs::RunRecord` maps the hub
  `terminal_id` to `host_terminal_id`, `AgentState.host_epoch_id` holds the live
  host epoch, and the host's frames socket is `<GOBBY_HOME>/gterm-frames.sock`
  (`crates/gterminals/src/host.rs::check_socket_paths`).
- Tests never touch the live daemon or its database. The live proof (5.2) runs an
  isolated hub and node with temporary homes, ports and databases, and registers
  nothing in the live daemon.

## Cross-plan edges set at expansion
`kind: framing`

Heading `(depends: #N)` refs become `blocked-by` edges at expansion. This table
records each external edge and the related work that gets no edge.

| Leaf | External work | Edge | Reason |
| --- | --- | --- | --- |
| 1.2 | #24376 (node-halves 2.7, command dispatch over the node channel) | blocked by | 1.2 adds the `terminal.relay` arm to the node dispatcher `crates/gdaemon/src/nodes/agents/commands.rs` that #24376 creates, and edits `channel.rs`, where #24376 changes `run_client`. |
| 3.1 | #24575 (S2.8 2.14, terminal inventory) | blocked by | 3.1 changes the inventory query and handlers #24575 creates. |
| 3.1 | #24577 (S2.8 2.2, native WS state machine) | blocked by | 3.1 changes `terminal_list` in the `ws.rs` #24577 creates. |
| 3.2 | #24578 (S2.8 2.3, bounded proxy relay) | blocked by | 3.2 gives the relay pump #24578 creates its remote source. |
| 3.2 | #24580 (S2.8 2.13, family registration and Native activation) | blocked by | 3.2 wires the remote link into the family #24580 registers. |
| 3.3 | #24579 (S2.8 2.12, WS operator input) | blocked by | 3.3 branches the `terminal_input` handler #24579 creates. |
| none | #25201 (Rust write client keeps hub-mode's remote write branch) | relation | It covers Python-originated writes; 3.3 covers WS input (Decision 7). |
| none | #24149 (S2.7 hub half: roster, attention, dispatch in Rust) | relation | Its roster port must keep each agent's `machine_id`, which 4.1's filter and token read. |
| none | node-halves 3.4 (#24388, `RemoteTerminalRuntime`) | relation | Its `attach_locator` keeps raising for remote terminals; the Rust family never calls it. |
| none | #20203, #25068, #24452, #25187 | relation | Decisions 2, 3, 5 and 8. |

**Granularity:** Eleven deliverables, one per independently testable outcome:
the wire contract, the node relay executor, the node resize executor, the hub
token store and endpoint, the remote link, the inventory, remote attach, remote
input and sizing, the gclient flow, the ROADMAP fix and the live proof. The token store (2.1) and the link (2.2) are two
lifecycle owners, so they stay separate.

## P1: Node-commands contract and node relay half
`kind: framing`

### 1.1 `terminal.relay` and `terminal.resize` in node-commands contract version 5 [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: move the terminal command types out, add the Relay and Resize variants to CommandFrame, and set CONTRACT_VERSION to 5
- `crates/gdaemon/src/node_commands/terminal.rs`
- `crates/gdaemon/src/node_commands/frame_dispatch.rs::command_frame`
- `crates/gdaemon/src/nodes/registry.rs::*` — scope-reason: add the Relay and Resize arms to the request-id match in CommandReceiver::recv
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: add the terminal.relay and terminal.resize commands, replies and malformed messages to the shared corpus
- `crates/gdaemon/tests/node_command_parity.rs::*` — scope-reason: add the version 5 message list and its round-trip test
- `crates/gdaemon/tests/node_internal_route.rs::malformed_requests_send_nothing`
- `src/gobby/hub/node_commands.py::*` — scope-reason: set CONTRACT_VERSION to 5 and add the terminal.relay and terminal.resize models to the command union
- `tests/hub/test_node_command_parity.py::*` — scope-reason: add the version 5 message list and its round-trip test
- `docs/contracts/node-commands.md`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gdaemon/src/node_commands.rs` (979 lines) holds every frame
  type. `CommandFrame` is a closed untagged enum (`Spawn`, `Cancel`, `Write`,
  `Merge`, `Remove`, `Status`, `Read`). `CONTRACT_VERSION` is 4.
  `crates/gdaemon/src/node_commands/checkout_read.rs` is the precedent for a
  command in its own module using the file's `literal_string!` macro.
  `frame_dispatch.rs::command_frame` maps the `command` string to a variant, and
  `registry.rs::CommandReceiver::recv` matches each variant for its `request_id`.
- Observed: Python mirrors the contract in `src/gobby/hub/node_commands.py`
  (`CONTRACT_VERSION = 4`, `_contract_version_only`). `tests/hub/test_node_command_corpus.py::test_corpus_round_trips`
  round-trips every corpus message through Python, so a new command needs Python
  models even though Python never sends it. The Rust internal route
  (`crates/gdaemon/src/nodes/internal_api.rs::handle`) stamps `v` itself, so
  Python's sends need no change.
- Split: move `WriteKind`, `WritePayload`, `WriteCommand` and the
  `WriteCommandName` literal out of `crates/gdaemon/src/node_commands.rs` into
  the new `crates/gdaemon/src/node_commands/terminal.rs`, re-exported from the
  parent so every path stays valid. Add there `RelayCommandName`
  (`"terminal.relay"`), `RelayPayload { terminal_id: String, attachment_id: String,
  token: String }` with `deny_unknown_fields` and non-empty strings, and
  `RelayCommand`. The reply is `EmptyResult` (`{}`). `RelayPayload` implements
  `Debug` by hand and prints the token as `<redacted>`; serde stays transparent,
  so the wire carries the raw token (D5). Add `ResizeCommandName`
  (`"terminal.resize"`), `ResizePayload { terminal_id: String, rows: u16, cols:
  u16 }` with `deny_unknown_fields`, a non-empty `terminal_id` and `rows` and
  `cols` of at least 1, and `ResizeCommand`, also replying `EmptyResult`. No
  existing command resizes a node PTY:
  `src/gobby/hub/remote_runtime.py::RemoteTerminalRuntime.resize` raises.
- Contract text for `terminal.resize`: the node applies the size to the run's
  host terminal and replies `{}`; `not_found` for an unknown or unlaunched
  terminal; `command_failed` with stage `"none"` when the host refuses or is
  unavailable. A resize never writes PTY input, so no stage is `"partial"`.
- Contract text for `terminal.relay`: the node replies `{}` only after its relay socket is accepted;
  `not_found` for an unknown or unlaunched terminal; `command_failed` with stage
  `"none"` when the host is unavailable, the attach is refused, or the hub refuses
  the relay socket. Document the relay socket: `GET /api/nodes/terminal-relay`,
  bearer node key, header `x-gobby-relay-token`, one encoded frame
  (`gobby_terminals::frames::encode_frame`) per binary message, hub-to-node
  messages limited to `SetViewport`, `SetScrollOffset`, `BindAttachment`,
  `SetTerminalTheme` and `Detach`.
- Planned verification: `cargo nextest run -p gobby-daemon -E 'test(node_command) | test(malformed_requests)'`
  and `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_parity.py tests/hub/test_node_command_corpus.py -v`.

**Acceptance:**

- 1.1.1 - `terminal.relay` and `terminal.resize` commands and their `{}` and error replies round-trip byte-identically in Rust at contract version 5. test: `crates/gdaemon/tests/node_command_parity.rs::v5_terminal_commands_round_trip`.
- 1.1.2 - The same corpus messages round-trip through the Python models at version 5. test: `tests/hub/test_node_command_parity.py::test_v5_terminal_commands_round_trip`.
- 1.1.3 - A malformed `terminal.relay` request (unknown field, empty token) or `terminal.resize` request (zero rows, unknown field) sends nothing to the node. test: `crates/gdaemon/tests/node_internal_route.rs::malformed_requests_send_nothing`.
- 1.1.4 - `node_commands.rs` stays under 1,000 lines with the terminal types in their own module. file: `crates/gdaemon/src/node_commands/terminal.rs`.
- 1.1.5 - The contract documents `terminal.relay`, `terminal.resize`, their replies and the relay socket. behavior: "terminal.resize" in `docs/contracts/node-commands.md`.
- 1.1.6 - `RelayPayload`'s `Debug` output prints the token as `<redacted>`, while serialization carries it unchanged. test: `crates/gdaemon/tests/node_command_parity.rs::relay_payload_debug_redacts_token`.

### 1.2 Node relay executor (depends: 1.1, #24376) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/relay.rs`
- `crates/gdaemon/src/nodes/agents/mod.rs::*` — scope-reason: declare the relay module
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/src/nodes/channel.rs::connect_client`
- `crates/gdaemon/tests/node_relay.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: #24376 makes `run_client` route each `command` frame to
  `commands::dispatch`, which runs commands of one run in order. `terminal.write`
  resolves `terminal_id` to a run and answers `not_found` for an unknown one.
- Observed: `crates/gdaemon/src/nodes/channel.rs::connect_client` reads the node
  key (`gobby_core::local_token::read_api_key_at`), dials `node.relay` with TLS
  when configured, and upgrades to `/api/nodes/channel` with a bearer header.
  Extract the dial and upgrade into `dial_hub(node, path, extra_headers)` in the
  same file; `connect_client` keeps its hello and ack.
- Chosen approach in the new `crates/gdaemon/src/nodes/agents/relay.rs`:
  1. Under the state lock, find the `RunRecord` whose `terminal_id` matches and
     read its `host_terminal_id` and `AgentState.host_epoch_id`. No run or no host
     terminal id answers `not_found`. Relay setup never holds the per-run queue,
     because a long-lived relay must not block that run's cancel or write.
  2. Connect `<home>/gterm-frames.sock`, split the stream so no host read is ever
     cancelled mid-frame, and run `FrameClient::handshake` with the node's own key
     and epoch. Send `AttachTerminal` for the host terminal and wait for
     `Attached`. Failure answers `command_failed` with stage `"none"`.
  3. `dial_hub(node, "/api/nodes/terminal-relay", x-gobby-relay-token)`. A refusal
     answers `command_failed` with stage `"none"` and detaches the host.
  - Setup bound: steps 2 and 3 run on their own task under one 10 s deadline
    from command receipt, which matches the hub's open deadline. The task races
    that deadline against the closure of the reply sender, the writer mpsc
    that #24376's `run_client` feeds, whose `closed()` resolves at channel end.
    The deadline answers `command_failed` with stage `"none"`. Channel loss
    answers nothing. Every exit before the `{}` reply drops the host stream,
    sending `Detach` first once `Attached` arrived, and closes a dialed hub
    socket. `FrameClient` sets no timeout of its own
    (`crates/gterminals/src/frames.rs::FrameClient::recv`), so this deadline is
    the only bound on a silent host.
  4. Reply `{}`, then pump on its own task. Host to hub: each `ServerMessage` is
     encoded and sent as one binary message. Hub to node: each binary message is
     decoded as one `ClientMessage`; only `SetViewport`, `SetScrollOffset`,
     `BindAttachment`, `SetTerminalTheme` and `Detach` reach the host.
     `BindAttachment` is allowed because the hub's theme relay binds the stream
     before `SetTerminalTheme` (the `terminal_set_theme` row of
     `docs/contracts/gterm-protocols.md`). Binding grants nothing, since `Input`
     and `Paste` stay refused. Any other variant, a text
     message or an undecodable payload closes the relay with code 1008. One frame
     is in flight per direction, so a stalled hub stops host reads and the host's
     droppable delta queue (64 entries, 2 MiB, keyframe resync) absorbs it.
  5. Either side ending sends `Detach` to the host and closes both. Nothing is
     logged except the terminal id and the close reason.
- Log proof: gdaemon logs with `eprintln!` and has no tracing subscriber, so the
  test re-runs one of its own cases with `--exact <case> --nocapture` and reads
  that run's stderr, the pattern of
  `crates/gdaemon/tests/heartbeat.rs::panic_is_logged_with_the_registrant_name`.
- Fixtures: `crates/gdaemon/tests/node_support/fake_gterm.rs` and
  `node_support/mod.rs` provide a fake host; the new test file serves a stub
  hub relay endpoint.
- Planned verification: `cargo nextest run -p gobby-daemon -E 'test(node_relay)'`.

**Acceptance:**

- 1.2.1 - `terminal.relay` for a launched run attaches the host terminal, opens the relay socket with the token header, then answers `{}`. test: `crates/gdaemon/tests/node_relay.rs::relay_attaches_and_dials_before_reply`.
- 1.2.2 - An unknown or unlaunched terminal answers `not_found`; an unavailable host or a refused relay socket answers `command_failed` with stage `"none"` and leaves no host attachment. test: `crates/gdaemon/tests/node_relay.rs::relay_failure_dispositions`.
- 1.2.3 - Host frames reach the hub as one encoded frame per binary message, and only the five allowed client messages reach the host; any other message closes the relay with 1008. test: `crates/gdaemon/tests/node_relay.rs::relay_forwards_frames_and_filters_client_messages`.
- 1.2.4 - A live relay does not block `terminal.write` or `agent.cancel` for the same run. test: `crates/gdaemon/tests/node_relay.rs::relay_does_not_hold_the_run_queue`.
- 1.2.5 - Closing either side detaches the host terminal exactly once. test: `crates/gdaemon/tests/node_relay.rs::relay_close_detaches_once`.
- 1.2.6 - Across dispatch, a refused relay socket, a relayed frame and close, the node's stderr names the terminal id and close reason but never the token, its digest or frame payload bytes. test: `crates/gdaemon/tests/node_relay.rs::relay_stderr_carries_no_secrets`.
- 1.2.7 - A host that never sends `Welcome`, a host that never sends `Attached`, and channel loss during setup each end the setup task within 10 s, leaving no host attachment and no hub socket. The deadline cases answer `command_failed` with stage `"none"`. test: `crates/gdaemon/tests/node_relay.rs::relay_setup_is_bounded_and_cleans_up`.

### 1.3 Node `terminal.resize` executor (depends: 1.2) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/tests/node_resize.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: #24376's `terminal.write` resolves `terminal_id` to a run and runs
  under that run's queue. `crates/gdaemon/src/nodes/agents/host.rs::HostControl::call`
  runs one control operation with the node's epoch authority and a ready
  deadline. `crates/gterminals/src/control.rs::ControlClient::resize` sends the
  host's `resize` for `host_terminal_id`, `rows` and `cols`, which changes the PTY
  size. `SetViewport` on a frame stream changes only that attachment's render
  size (`crates/gterminal/src/host/state.rs`).
- Chosen approach: `terminal.resize` takes the run's queue like `terminal.write`,
  so resizes and writes for one run apply in arrival order. It reads the run's
  `host_terminal_id` and calls `HostControl::call` with `ControlClient::resize`.
  An unknown or unlaunched terminal answers `not_found`. A host refusal,
  including a stale epoch, or an unavailable host answers `command_failed` with
  stage `"none"`. Only the terminal id and the outcome are logged.
- Fixtures: `crates/gdaemon/tests/node_support/fake_gterm.rs` serves the fake
  host's control socket.
- Planned verification: `cargo nextest run -p gobby-daemon -E 'test(node_resize)'`.

**Acceptance:**

- 1.3.1 - `terminal.resize` for a launched run sends the host `resize` for its host terminal and answers `{}`. test: `crates/gdaemon/tests/node_resize.rs::resize_reaches_host_control`.
- 1.3.2 - An unknown or unlaunched terminal answers `not_found`; a host refusal or an unavailable host answers `command_failed` with stage `"none"`. test: `crates/gdaemon/tests/node_resize.rs::resize_failure_dispositions`.
- 1.3.3 - `terminal.resize` and `terminal.write` for one run apply in arrival order. test: `crates/gdaemon/tests/node_resize.rs::resize_orders_with_writes`.

## P2: Hub relay endpoint and remote link
`kind: framing`

### 2.1 Relay tokens and the hub relay endpoint (depends: 1.3) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/terminal_relay.rs`
- `crates/gdaemon/src/nodes/mod.rs::*` — scope-reason: declare terminal_relay and give HubNodes its relay token store
- `crates/gdaemon/src/nodes/registry.rs::*` — scope-reason: add CommandSender::closed, which resolves when the channel's receiver drops
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: construct HubNodes with an empty relay store; two cfg-gated functions share the name run
- `crates/gdaemon/tests/common/channel.rs::*` — scope-reason: construct HubNodes with an empty relay store
- `crates/gdaemon/tests/terminal_relay.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `FrontDoor::handle` authenticates, which stamps `x-gobby-user-id`,
  `x-gobby-machine-id` and `x-gobby-key-id` from the key
  (`crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`), then hands
  `/api/nodes/channel` to `nodes::channel::handle` when the door is
  `NativeRoutes::Hub`. Route `/api/nodes/terminal-relay` the same way, hub only.
- Observed: `HubNodes { db, registry, timing }` is built in `serve.rs` and in
  `tests/common/channel.rs`. `Registry::connection_id(machine)` names the live
  channel. `CommandSender` wraps an `mpsc::Sender`, whose `closed()` resolves when
  the channel's `CommandReceiver` drops at channel end.
- Chosen approach: new `RelayTokens` (`Default`, a mutex over a `HashMap` keyed by
  the SHA-256 digest of the token) in `crates/gdaemon/src/nodes/terminal_relay.rs`:
  - `mint(machine, connection, terminal_id, attachment_id)` draws 32 bytes from
    `rand`'s OS RNG, stores `{machine, connection, terminal_id, attachment_id,
    expires_at: now + 10 s, deliver: oneshot::Sender<WebSocket>}`, and returns the
    base64url token with a guard whose drop removes the entry. It first counts
    that channel connection's unexpired entries; at `MAX_PENDING_RELAYS` (a
    constant of 32, above one workspace restore's simultaneous attaches) it
    stores nothing and returns `RelayBusy`. The ceiling bounds pending setups
    only; an established relay is bounded by its attachment.
  - `handle(nodes, request)` reads `x-gobby-relay-token`, removes the entry
    (single use), and accepts only when it is unexpired, its machine equals
    `x-gobby-machine-id`, and its connection equals `Registry::connection_id`.
    Every refusal answers the same 401 `relay_token_invalid`, so the endpoint is
    no oracle. On acceptance it upgrades with a 2 MiB plus 4 byte message limit
    and delivers the socket.
  - No sweeper: each entry lives only as long as its waiter's guard.
  - Tokens, digests and payloads are never logged or persisted (D5).
- Planned verification: `cargo nextest run -p gobby-daemon -E 'test(terminal_relay)'`.

**Acceptance:**

- 2.1.1 - A minted token is accepted once from its machine on its channel within 10 s, and the socket reaches the waiter. test: `crates/gdaemon/tests/terminal_relay.rs::token_accepts_once`.
- 2.1.2 - A reused, expired, unknown, wrong-machine or wrong-connection token gets the same 401 `relay_token_invalid`. test: `crates/gdaemon/tests/terminal_relay.rs::invalid_tokens_are_indistinguishable`.
- 2.1.3 - Dropping the waiter's guard revokes its token. test: `crates/gdaemon/tests/terminal_relay.rs::dropped_waiter_revokes_token`.
- 2.1.4 - `CommandSender::closed` resolves when the node channel ends. test: `crates/gdaemon/tests/terminal_relay.rs::sender_closed_follows_channel_end`.
- 2.1.5 - The relay endpoint is served only by a hub door. test: `crates/gdaemon/tests/terminal_relay.rs::relay_route_is_hub_only`.
- 2.1.6 - A channel connection holding 32 pending relays gets `RelayBusy` and no entry; a dropped guard or an expired entry frees a slot, and another connection's pending relays do not count. test: `crates/gdaemon/tests/terminal_relay.rs::pending_relays_are_capped_per_connection`.

### 2.2 Remote terminal link over the node channel (depends: 2.1) [category: code]
`kind: deliverable`

Targets:
- `crates/gterminals/src/remote.rs`
- `crates/gterminals/src/lib.rs::*` — scope-reason: declare the remote module
- `crates/gdaemon/src/nodes/terminal_relay.rs`
- `crates/gdaemon/tests/terminal_relay.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- New in `crates/gterminals/src/remote.rs`: trait `RemoteTerminals` (Decision 6)
  with `open(RemoteTarget { user_id, machine_id, terminal_id, attachment_id })`
  returning `RemoteFrames`, `write(RemoteWrite { user_id, machine_id,
  terminal_id, kind, data, submit })` and `resize(RemoteResize { user_id,
  machine_id, terminal_id, rows, cols })`. `RemoteFrames` holds a capacity-1
  `mpsc::Receiver<ServerMessage>` and a capacity-1 `mpsc::Sender<ClientMessage>`.
  `RemoteError` is `NodeOffline`, `MachineNotOwned`, `MachineNotFound`,
  `NotFound`, `CommandFailed { stage }`, `RelayTimeout` or `RelayBusy`, each with
  its wire code.
  `relayable(&ClientMessage)` is the five-message allowlist.
- gdaemon's `HubRemoteTerminals` (in `terminal_relay.rs`) implements it:
  1. Owner check: `SELECT owner_user_id FROM machines WHERE id = $1` must equal
     the caller; otherwise `machine_not_found` or `machine_not_owned`, the codes
     `src/gobby/hub/caller_scope.py::require_machine_owned` uses.
  2. `Registry::command_sender` and `connection_id`, else `node_offline`.
  3. `open`: mint (`RelayBusy` answers `relay_busy` with no command sent), send
     `terminal.relay` with a 10 s deadline, and select over the
     reply error, the delivered socket, the deadline (`relay_timeout`) and
     `CommandSender::closed` (`node_offline`). A late `{}` reply after delivery is
     ignored.
  4. A bridge task decodes each binary message into the receiver and encodes each
     allowlisted sent message. Capacity 1 propagates backpressure to TCP. It
     ends when either side closes or `CommandSender::closed` resolves, which
     closes the receiver.
  5. `write`: owner check, then `terminal.write` with the caller's payload; node
     errors map to `RemoteError`.
  6. `resize`: owner check, then `terminal.resize` (1.1, 1.3); node errors map
     to `RemoteError`.
- Log proof: the stderr re-run pattern of 1.2. The stub node receives
  `terminal.relay`, so the test knows the raw token.
- Planned verification: `cargo nextest run -p gobby-daemon -E 'test(terminal_relay)'`
  and `cargo nextest run -p gobby-terminals`.

**Acceptance:**

- 2.2.1 - `open` for an owned machine with a live channel returns frames from the node's relay socket, and sent allowlisted messages reach the node. test: `crates/gdaemon/tests/terminal_relay.rs::open_relays_frames_both_ways`.
- 2.2.2 - Another owner's machine answers `machine_not_owned`, an unknown machine `machine_not_found`, a machine with no channel `node_offline`, and a connection at its pending ceiling `relay_busy`, with no command sent. test: `crates/gdaemon/tests/terminal_relay.rs::open_refusals_send_nothing`.
- 2.2.3 - A node error reply, a missing socket after 10 s, and channel loss during the wait each fail the open and revoke the token. test: `crates/gdaemon/tests/terminal_relay.rs::open_failures_revoke_token`.
- 2.2.4 - Channel loss during a live relay closes the frame receiver. test: `crates/gdaemon/tests/terminal_relay.rs::channel_loss_ends_live_relay`.
- 2.2.5 - `write` sends `terminal.write` after the owner check and maps node errors. test: `crates/gdaemon/tests/terminal_relay.rs::write_sends_terminal_write`.
- 2.2.6 - Across `terminal.relay` dispatch through the registry, an invalid-token refusal, an accepted upgrade, a frame and a write in each direction, and close, the hub's stderr never carries the raw token, its SHA-256 digest in hex or base64url, or the frame and input payload bytes. test: `crates/gdaemon/tests/terminal_relay.rs::relay_stderr_carries_no_secrets`.
- 2.2.7 - `resize` sends `terminal.resize` after the owner check and maps node errors. test: `crates/gdaemon/tests/terminal_relay.rs::resize_sends_terminal_resize`.

## P3: Hub terminal family
`kind: framing`

### 3.1 Owner-scoped hub-wide terminal inventory (depends: 2.2, #24575, #24577) [category: code]
`kind: deliverable`

Targets:
- `crates/gterminals/src/inventory.rs`
- `crates/gdaemon/src/front_door/terminal_api.rs::*` — scope-reason: owner-scope the inventory handlers #24575 adds beside the existing write route
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/tests/inventory.rs`
- `docs/contracts/gterm-protocols.md`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: Python `GET /api/terminals` and `/api/terminals/{id}` scope to
  `require_machine_id()` (`src/gobby/servers/routes/terminals.py`), and #24575
  ports that local scope to Rust, so another machine's row answers 404.
  `src/gobby/storage/terminals.py::TerminalManager.list_page` already filters by
  `machine_id`.
- Chosen approach: when the request carries `x-gobby-user-id`, the inventory page
  query, the single-row read and WS `terminal_list` return rows whose `machine_id`
  belongs to a machine with that `owner_user_id`, ordered as #24575 orders them.
  Rows on the hub's own machine keep their direct `attach` locator. Rows on other
  machines carry `machine_id` and `attach: null`, so `FrameDelivery::Auto` picks
  proxy. Without a user (standalone, agent token), the scope stays the local
  machine (Decision 8).
- Planned verification: `cargo nextest run -p gobby-terminals -E 'test(inventory)'`.

**Acceptance:**

- 3.1.1 - A hub caller's inventory lists rows from every machine it owns and none from another owner's machine. test: `crates/gterminals/tests/inventory.rs::inventory_is_owner_wide`.
- 3.1.2 - Remote rows carry `machine_id` and `attach: null`; local rows keep their locator. test: `crates/gterminals/tests/inventory.rs::remote_rows_have_no_locator`.
- 3.1.3 - The single-row read answers an owned remote row and 404 for another owner's row. test: `crates/gterminals/tests/inventory.rs::row_read_is_owner_scoped`.
- 3.1.4 - A caller without a user identity keeps the local-machine scope. test: `crates/gterminals/tests/inventory.rs::no_user_keeps_local_scope`.
- 3.1.5 - The WS protocol documents owner-wide listing and `attach: null` rows. behavior: "attach: null" in `docs/contracts/gterm-protocols.md`.

### 3.2 Remote attach through the hub relay (depends: 3.1, #24578, #24580) [category: code]
`kind: deliverable`

Targets:
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/src/relay.rs`
- `crates/gterminals/src/family.rs::*` — scope-reason: hold the optional RemoteTerminals link and hand it to the WS connection state
- `crates/gdaemon/src/state_services.rs::ActiveServices::start`
- `crates/gdaemon/src/state.rs::*` — scope-reason: carry the hub's remote link through RuntimeState::prepare and RuntimeState::run
- `crates/gdaemon/src/serve.rs::*` — scope-reason: build HubNodes before RuntimeState::prepare and pass its remote link; two cfg-gated functions share the name run
- `crates/gterminals/tests/remote_attach.rs`
- `docs/contracts/gterm-protocols.md`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: #24578 starts one relay task per WS connection and one pump per
  attachment in `relay.rs`, with a bounded per-socket frame queue, a reserved
  lifecycle lane and one idempotent finalizer. #24577's `ws.rs` owns
  `terminal_attach`. `crates/gdaemon/src/state_services.rs::ActiveServices::start`
  builds `TerminalFamily`; `serve.rs` builds `HubNodes` after
  `RuntimeState::prepare`.
- Chosen approach:
  - `TerminalFamily` gains `with_remote(Arc<dyn RemoteTerminals>)`; only a hub
    passes one.
  - In `terminal_attach`, a row whose `machine_id` is not the local machine calls
    `open` with the caller's `x-gobby-user-id`. A `direct` request for it gets the
    refusal #24577 gives a row with no locator. `open` errors answer the attach
    with their wire code (`node_offline`, `machine_not_owned`,
    `machine_not_found`, `not_found`, `command_failed`, `relay_timeout`,
    `relay_busy`).
  - The pump's frame source becomes local host or `RemoteFrames`; everything after
    the source (history before live, per-socket accounting, overflow closing only
    that socket, the single finalizer) is #24578's unchanged code. Finalization
    sends `Detach` and drops `RemoteFrames`. A closed receiver finalizes like host
    EOF.
  - The hub keeps no extra buffer: the pump reads the next frame only when its
    queue accepts one.
- Planned verification: `cargo nextest run -p gobby-terminals -E 'test(remote_attach)'`
  and `cargo nextest run -p gobby-daemon`.

**Acceptance:**

- 3.2.1 - A proxied attach of an owned remote row delivers the node's history and live frames as `terminal_frame` messages. test: `crates/gterminals/tests/remote_attach.rs::remote_attach_streams_frames`.
- 3.2.2 - `node_offline`, `machine_not_owned`, `relay_busy` and a `direct` request fail the attach with their codes and leave no attachment. test: `crates/gterminals/tests/remote_attach.rs::remote_attach_refusals`.
- 3.2.3 - A stalled client socket closes only that socket, and the link is not read while its queue is full. test: `crates/gterminals/tests/remote_attach.rs::remote_backpressure_closes_only_the_slow_socket`.
- 3.2.4 - Detach sends `Detach` to the node, and channel loss finalizes the attachment exactly once. test: `crates/gterminals/tests/remote_attach.rs::remote_close_finalizes_once`.
- 3.2.5 - A hub wires the link into the family; a standalone daemon does not. test: `crates/gterminals/tests/remote_attach.rs::only_hub_family_has_remote_link`.
- 3.2.6 - The WS protocol documents remote attach and its error codes. behavior: "node_offline" in `docs/contracts/gterm-protocols.md`.

### 3.3 Remote terminal input and sizing through node commands (depends: 3.2, #24579) [category: code]
`kind: deliverable`

Targets:
- `crates/gterminals/src/ws.rs`
- `crates/gterminals/tests/remote_attach.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gdaemon pin

**Research context:**
- Observed: `crates/gclient/src/frame_source/proxy.rs::ProxyFrameSource::send_input`
  refuses host input, so a proxied pane types through the daemon's
  `terminal_input`. #24579 handles `terminal_input` under the route action hold
  through the local write route.
- Chosen approach: `terminal_input` for a remote attachment calls
  `RemoteTerminals::write` with the attachment's terminal and the caller's user,
  instead of the local write route. `terminal_paste` does the same with kind
  `paste`. Errors answer with their wire code. Input payloads are never logged
  (D5). #25201 is unrelated (Decision 7).
- Observed: #24577's `ws.rs` elects sizing through
  `crates/gterminals/src/leases.rs::Registry::resize` (web over gclient,
  re-election on detach) and applies the elected `Sizing` to the row's runtime
  on attach, `terminal_resize` and detach. The oracle is
  `src/gobby/servers/websocket/terminal_sizing.py::_handle_terminal_resize`.
- Chosen approach for sizing: election stays on the hub unchanged, because the
  hub holds every attachment of a remote row. For a remote row the sizing
  applier calls `RemoteTerminals::resize` with the elected geometry. A losing
  requester changes only its own viewport and sends nothing to the node. A
  failed remote resize fails like a local one in #24577, with the remote wire
  code.
- The input-grant applier does nothing for a remote row. Every remote attachment
  is proxied (3.2 refuses `direct`), so no attachment on the node host holds a
  grant, and `terminal.write` writes through the node daemon's control socket.
- Every other client action is accounted for. Theme, viewport and scroll offset
  travel on the relay stream (1.2's allowlist). Focus and take or release
  control change only the hub's lease. `terminal_create` and `terminal_kill`
  are terminal lifecycle, not attach, and stay outside #20202's four items.
- Planned verification: `cargo nextest run -p gobby-terminals -E 'test(remote_attach)'`.

**Acceptance:**

- 3.3.1 - Input on a remote attachment reaches the link as one write with its kind, data and submit flag, and never the local write route. test: `crates/gterminals/tests/remote_attach.rs::remote_input_uses_terminal_write`.
- 3.3.2 - Input to a node that went offline or to a machine the caller does not own answers its code and writes nothing. test: `crates/gterminals/tests/remote_attach.rs::remote_input_refusals`.
- 3.3.3 - `terminal_paste` on a remote attachment reaches the link as one `paste` write. test: `crates/gterminals/tests/remote_attach.rs::remote_paste_uses_terminal_write`.
- 3.3.4 - An elected `terminal_resize`, an attach that changes the election and a detach that re-elects each send the elected geometry to the link once; a losing requester changes only its viewport and sends nothing. test: `crates/gterminals/tests/remote_attach.rs::remote_sizing_follows_the_election`.
- 3.3.5 - Take and release control on a remote attachment change only the lease and call no host grant or revoke. test: `crates/gterminals/tests/remote_attach.rs::remote_control_applies_no_host_grant`.

## P4: gclient
`kind: framing`

### 4.1 Open another machine's terminal in its owning workspace (depends: 3.3) [category: code]
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/terminal_location.rs::locate_terminal_workspace`
- `crates/gclient/src/ui/sidebar/agents.rs::agent_candidate`
- `crates/gclient/src/ui/sidebar/terminals.rs::terminal_rows`
- `crates/gclient/tests/mock_daemon/workspace.rs::*` — scope-reason: record the node argument of workspace list, snapshot and attach, seed a workspace on another machine, and attach a machine's default workspace by project
- `crates/gclient/tests/remote_agent_open.rs`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the gclient pin

Consumers unchanged:
- `crates/gclient/src/app/live_loop/projects.rs` — no-edit-reason: agent_pane and the new-tab path call locate_terminal_workspace with the terminal id; the owning machine is resolved inside it.
- `crates/gclient/src/ui/sidebar.rs` — no-edit-reason: it re-exports and draws terminal_rows unchanged.
- `crates/gclient/src/ui/sidebar_rows/tests.rs` — no-edit-reason: its terminal-row cases attach the local machine's workspace, whose machine equals the local machine, so the gate change keeps their rows.

**Research context:**
- Observed: `locate_terminal_workspace` searches only
  `workspace.attach_target().node` with `WorkspaceOp::WorkspaceList`,
  `workspace_snapshot` and `attach_workspace`, then sets the attach target's
  workspace. Each `AgentEntry` carries `machine_id`
  (`crates/gclient/src/app/sidebar_model.rs::AgentEntry`), and the attached
  workspace record carries its `machine_id` (`crates/gclient/src/daemon/workspace.rs`).
  On the hub, `src/gobby/storage/workspaces.py::WorkspaceManager.resolve_node`
  resolves the owner's other machines.
- Observed: `agent_rows` already filters by `machine_admits`, so another
  machine's agents show under `all` (hub-mode 3.2). Rows carry no machine token.
  `machine_rows` labels a remote machine with `short_terminal_id(machine)`.
  `terminal_rows` gates on `local_machine`, so after switching to another
  machine's workspace the bare terminal rows would vanish under the default
  filter.
- Chosen approach:
  - `locate_terminal_workspace` looks up the terminal's `machine_id` from the
    sidebar agents. When it differs from the local machine, it searches and
    attaches with `node = Some(machine_id)` and sets the attach target's `node` to
    it; for a local terminal it uses `None`. Pane attach is unchanged: the row has
    no locator, so `FrameDelivery::Auto` uses `ProxyFrameSource`.
  - Fallback (adversary RT-F3): when no workspace on the owning machine holds
    the terminal's pane, the roster check's final `Ok(true)` would let
    `projects.rs::agent_pane` open the pane in the current workspace, which may
    belong to another machine. Before that return, a terminal whose machine
    differs from the attached workspace's machine attaches that machine's
    default workspace for the agent's project (`attach_workspace(Some(machine),
    None, Some(project))`), applies the snapshot, and sets the attach target's
    `node` and `workspace`. The caller then places the pane there. A failed
    attach warns and returns `false` before applying anything, so the previous
    workspace and attach target stay unchanged. The search loop's existing
    "Terminal moved; select it again" refusal already runs after it switched
    to an owning-machine workspace. The `reuse_client_pane` shortcut is safe:
    `src/gobby/terminals/workspace_pane_access.py::WorkspacePaneAccess._adoptable`
    refuses a pane for a terminal not live on the workspace's node.
  - `agent_candidate` prefixes the row's `reference` with the machine token
    `short_terminal_id(machine_id)` and a space when the agent's machine is not
    the local one. `agent_reference` stays unchanged because the pane chrome
    already shows the machine in its address.
  - `terminal_rows` gates on the attached workspace's `machine_id`, falling back
    to the local machine before a snapshot arrives.
- Planned verification: `cargo nextest run -p gobby-client -E 'test(remote_agent_open)'`
  and `cargo clippy -p gobby-client`.

**Acceptance:**

- 4.1.1 - Selecting another machine's agent attaches that machine's workspace (node argument set to its machine) and focuses the agent's pane. test: `crates/gclient/tests/remote_agent_open.rs::remote_agent_opens_in_owning_workspace`.
- 4.1.2 - Selecting a local agent afterwards returns to the local workspace with no node argument. test: `crates/gclient/tests/remote_agent_open.rs::local_agent_returns_to_local_workspace`.
- 4.1.3 - Another machine's agent row appears under `all` with its machine token and is hidden under the default filter (carries gclient-workspace-sidebar 3.2.4). test: `crates/gclient/tests/remote_agent_open.rs::remote_agent_row_shows_machine_token`.
- 4.1.4 - Bare terminal rows follow the attached workspace's machine. test: `crates/gclient/tests/remote_agent_open.rs::terminal_rows_follow_attached_machine`.
- 4.1.5 - When no owning-machine workspace holds the remote agent's pane, selecting it attaches that machine's default workspace for the agent's project and places the pane there; the previous machine's workspace gets no pane. test: `crates/gclient/tests/remote_agent_open.rs::unplaced_remote_agent_opens_in_owning_default_workspace`.
- 4.1.6 - A pane that moves between snapshot and attach answers the retry warning, and a failed owning-workspace attach leaves the previous workspace and attach target unchanged; neither opens a pane in the previous machine's workspace. test: `crates/gclient/tests/remote_agent_open.rs::remote_open_never_lands_in_previous_machine_workspace`.

## P5: Documentation and live proof
`kind: framing`

### 5.1 ROADMAP corrections for remote attach [category: docs]
`kind: deliverable`

Targets:
- `ROADMAP.md`

**Research context:**
- Observed: #19600 (two-machine end-to-end acceptance) closed `wont_do` on
  2026-10-08 when Josh retired the physical M0 acceptance path. `ROADMAP.md`
  still calls it open or a gate in the Shared datastores bullet ("PRE remains open
  for #19600"), the PRE status row, the M0 paragraph ("#19600 ... is its only
  open deliverable"), the Stage 0 paragraph ("#20202 remote roster and attach
  planning remains open and waits on #19600") and the S4.3 row ("planning waits
  on #19600"). Decision 23 is a dated record and stays as written.
- Observed: the S1.4 row says `machines` gains platform, capabilities, heartbeat
  and endpoint columns. Migrations added only `ref` (440), `last_heartbeat_at` and
  `node_version` (458), and D1 rules out an endpoint column.
- Edit each listed passage to state #19600's closure; point the Stage 0 paragraph
  and the S4.3 row at `.gobby/plans/remote-terminal-attach.md`; correct the S1.4
  column list.
- Planned verification: `gcode grep -F "#19600" ROADMAP.md` shows only the closure
  and Decision 23.

**Acceptance:**

- 5.1.1 - The S4.3 row and the Stage 0 paragraph name this plan and no #19600 wait. behavior: "remote-terminal-attach" in `ROADMAP.md`.
- 5.1.2 - No passage outside Decision 23 calls #19600 open or a gate. file: `ROADMAP.md`.
- 5.1.3 - The S1.4 row lists the landed `machines` columns and no endpoint column. behavior: "node_version" in `ROADMAP.md`.

### 5.2 Live hub-to-node remote attach proof (depends: 4.1) [category: test]
`kind: deliverable`

Targets:
- `tests/e2e/test_remote_terminal_attach_live.py`
- `tests/e2e/conftest.py::_TERMINAL_E2E_BINARIES`

**Research context:**
- Observed: `tests/e2e/test_hub_node_pair.py` provides `pair_hub` (a hub-mode
  `spawn_daemon_instance` over the e2e harness's isolated config and database,
  with a self-signed certificate) and `pair_node` (an enrolled bare
  `gdaemon serve` with `GOBBY_HOME` in `tmp_path`), plus `_wait_connection`.
  These are imported, not edited. Node terminals are run-backed, so the proof
  needs an agent run on the node.
- Real provider (Orchestrator ruling gobby#17281 via Triage, 2026-10-27, for
  Josh to see at approval): Josh's 2026-10-20 correction on #24604 requires
  real providers on the hub-node path; fake providers are never an accepted
  final proof. The module therefore starts one real Claude run on the node
  under managed SRT (`tests/e2e/conftest.py::link_operator_srt` in both homes,
  failing instead of skipping as hub-mode Decision 22 does). The proof uses
  real provider usage. It runs only behind the admission gate of
  `tests/e2e/test_composer_live_proof.py`: the `GOBBY_COMPOSER_PROOF_EXECUTION`
  grant and a reviewed admission file loaded through
  `tests/e2e/composer_proof_admission.py::load_admission`. Rejected:
  depending on #24604 and extending `tests/e2e/test_hub_node_live.py`, which
  would couple 5.2 to #24604's blocked scope.
- Admission (the existing gate, unchanged): the module repeats
  `tests/e2e/test_composer_live_proof.py`'s `pytestmark` skip and the checks of
  its autouse `composer_admission` fixture, calling `load_admission` and
  `tests/e2e/composer_proof.py::require_private_root`. An admitted run needs the
  same separate PD admission as the composer proof: an owner-private (0600, not
  a symlink) file in the existing `Admission` shape, with `reviewed_commit` and
  `reviewed_tree` equal to the clean checkout's `HEAD` and `HEAD^{tree}`,
  `issued_at` at most 300 s before the run, the exclusion message id and complete
  exclusion map, all five native binary specs (gcode, gdaemon, ghook, gterm,
  gclient), both `claude` and `codex` provider specs (the schema requires both
  although this proof starts only Claude), the support tools, an owner-private
  `artifact_dir`, and `worktree_test_notice_id` when run from a linked worktree.
- Chosen approach: one test module with a hub and a node from those fixtures and
  one real Claude run on the node. Every typed marker goes into the composer
  without submitting and is then cleared, so the composer draft is preserved. A WS client authenticated as the owner drives the hub:
  list, attach with `frame_delivery: proxy`, read frames, type through
  `terminal_input` and see the echo, detach, then stop the node and attach again.
  Everything uses temporary homes, ports and databases; nothing is registered in
  the live daemon.
- Observed for the gclient case: `tests/e2e/gclient_driver.py::GclientDriver`
  runs the selected gclient under a PTY. gclient trusts only webpki roots
  (`rustls-tls-webpki-roots` in `crates/gclient/Cargo.toml`), so it cannot
  dial the hub's self-signed front door. The node's loopback origin
  (`pair_node.origin`) forwards HTTP and splices every upgrade, `/ws` included,
  to the hub (`crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`,
  `Door::Node`), which is D1's hairpin path. gclient takes its local machine from
  `GOBBY_HOME`'s machine id (`crates/gclient/src/views/mod.rs::run_ready`).
- Chosen approach for the gclient case: launch `GclientDriver` with the hub
  instance's environment, so its local machine is the hub's, plus `--daemon-url`
  set to `pair_node.origin` and `--token-file` holding an owner key minted for
  the hub's machine id, as `test_node_enrolls_and_relays` mints its observer key.
  Set the sidebar machine filter to `all`, select the node's agent row by its
  machine token, and open it. Assert three things: the hub's `workspace_snapshot`
  for the node's machine holds the agent's pane, the screen shows the node
  terminal's marker, and a typed second marker echoes on screen. The WS cases
  above stay for exact error assertions. `_TERMINAL_E2E_BINARIES` maps the new
  module to gterm and gclient so the run header reports their provenance.
- Planned verification (admitted run): `GOBBY_TEST_PROTECT=1 GOBBY_COMPOSER_PROOF_EXECUTION=PD_AUTHORIZED_ISOLATED_EXECUTION GOBBY_COMPOSER_PROOF_ADMISSION=<admission file> uv run pytest tests/e2e/test_remote_terminal_attach_live.py -v -rs --junitxml=<artifact_dir>/remote-terminal-attach.xml`.
  5.2 counts as delivered only when that run executes all five tests with five
  passed and none skipped. A run without the grant skips every test and proves
  nothing. The JUnit report in the admission's `artifact_dir` is the recorded
  evidence of the admitted run.

**Acceptance:**

- 5.2.1 - The hub's inventory lists the node's terminal with `attach: null`, and a proxied attach streams its frames. test: `tests/e2e/test_remote_terminal_attach_live.py::test_remote_attach_streams_node_frames`.
- 5.2.2 - `terminal_input` on the remote attachment appears in the node terminal's next frames. test: `tests/e2e/test_remote_terminal_attach_live.py::test_remote_input_round_trips`.
- 5.2.3 - After the node stops, attach answers `node_offline` and the live attachment finalizes. test: `tests/e2e/test_remote_terminal_attach_live.py::test_node_loss_finalizes_and_refuses`.
- 5.2.4 - A real gclient whose local machine is the hub opens the node's agent from the `all` sidebar into the node's workspace, shows its relayed output, and echoes typed input. test: `tests/e2e/test_remote_terminal_attach_live.py::test_gclient_opens_remote_agent`.
- 5.2.5 - Resizing the gclient window changes the node PTY size that the node host's control `list` reports (`rows`, `cols`; `crates/gterminal/src/host/helpers.rs`), and a second, losing WS observer's `terminal_resize` leaves it unchanged. test: `tests/e2e/test_remote_terminal_attach_live.py::test_remote_pty_follows_elected_size`.

## V1 Plan Changelog
`kind: framing`

2026-10-27 — Narrative draft by the Plan Writer under the Orchestrator's D1–D5
rulings. Enhancement and adversarial review have not run.

2026-10-27 — The Orchestrator (gobby#17281, relayed by Triage) ruled that all
three enhancer edits be implemented as placed. E-RT-01: a redacted relay-token
`Debug` (1.1.6) and stderr proofs that no token, digest or payload is logged
(1.2.6, 2.2.6). gdaemon logs with `eprintln!`, so the ruled tracing capture is a
stderr capture. E-RT-02: a ceiling of 32 pending relays per channel connection
with typed `relay_busy` (2.1.6, 2.2.2, 3.2). E-RT-03: a live gclient case
through the node's loopback origin (5.2.4), because gclient cannot trust the
hub's self-signed certificate. Base validation passes.

2026-10-27 — Adversary findings applied. RT-F1: node relay setup runs under a
10 s deadline and is cancelled on channel loss (1.2.7). RT-F2: contract v5 adds
`terminal.resize`, deliverable 1.3 executes it on the node, and the hub's sizing
election sends the elected geometry to the node (2.2.7, 3.3.3–3.3.5, 5.2.5).
RT-F3: gclient attaches the owning machine's default workspace before opening an
unplaced remote terminal (4.1.5–4.1.6). Per the Orchestrator's ruling, 5.2 now
uses one real Claude run behind the composer-proof admission gate in place of a
fake provider. RT-F4: 5.2 spells out the admitted invocation with both gate
variables and the admission prerequisites, and counts only a run with five
passed and none skipped.

2026-10-27 — Consensus. The Plan Writer (gobby#18199) and the Plan Adversary
(gobby#18200) agree on the narrative at
50b35e87a567987854a91b85c1a7c66c6209f1ed. RT-F1 through RT-F4 are resolved, no
decisions remain open, and base validation passes. The existing moved-terminal
refusal stays after gclient switches to the owning machine's workspace; it does
not restore the previous workspace. D2 (single-use relay token) and 5.2's real
Claude usage await Josh's confirmation at approval.

## V2 Verification
`kind: verification`

- Base plan validation: `uv run gobby plans validate .gobby/plans/remote-terminal-attach.md -p /Users/josh/Projects/gobby`.
- Expansion validation stays unrun until the Plan Adversary derives and applies
  `## M1 Task Manifest` from consensus bytes.
- Focused checks belong to their deliverables. No full pytest or whole-workspace
  Rust suite is required.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: '`terminal.relay` and `terminal.resize` in node-commands contract version
    5'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `terminal.relay` and `terminal.resize` commands and
    their `{}` and error replies round-trip byte-identically in Rust at contract version
    5. test: `crates/gdaemon/tests/node_command_parity.rs::v5_terminal_commands_round_trip`.

    1.1.2: The same corpus messages round-trip through the Python models at version
    5. test: `tests/hub/test_node_command_parity.py::test_v5_terminal_commands_round_trip`.

    1.1.3: A malformed `terminal.relay` request (unknown field, empty token) or `terminal.resize`
    request (zero rows, unknown field) sends nothing to the node. test: `crates/gdaemon/tests/node_internal_route.rs::malformed_requests_send_nothing`.

    1.1.4: `node_commands.rs` stays under 1,000 lines with the terminal types in their
    own module. file: `crates/gdaemon/src/node_commands/terminal.rs`.

    1.1.5: The contract documents `terminal.relay`, `terminal.resize`, their replies
    and the relay socket. behavior: "terminal.resize" in `docs/contracts/node-commands.md`.

    1.1.6: `RelayPayload`''s `Debug` output prints the token as `<redacted>`, while
    serialization carries it unchanged. test: `crates/gdaemon/tests/node_command_parity.rs::relay_payload_debug_redacts_token`.'
  labels:
  - covers:remote-terminal-attach:1.1:1.1.1
  - covers:remote-terminal-attach:1.1:1.1.2
  - covers:remote-terminal-attach:1.1:1.1.3
  - covers:remote-terminal-attach:1.1:1.1.4
  - covers:remote-terminal-attach:1.1:1.1.5
  - covers:remote-terminal-attach:1.1:1.1.6
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Node relay executor
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `terminal.relay` for a launched run attaches the host
    terminal, opens the relay socket with the token header, then answers `{}`. test:
    `crates/gdaemon/tests/node_relay.rs::relay_attaches_and_dials_before_reply`.

    1.2.2: An unknown or unlaunched terminal answers `not_found`; an unavailable host
    or a refused relay socket answers `command_failed` with stage `"none"` and leaves
    no host attachment. test: `crates/gdaemon/tests/node_relay.rs::relay_failure_dispositions`.

    1.2.3: Host frames reach the hub as one encoded frame per binary message, and
    only the five allowed client messages reach the host; any other message closes
    the relay with 1008. test: `crates/gdaemon/tests/node_relay.rs::relay_forwards_frames_and_filters_client_messages`.

    1.2.4: A live relay does not block `terminal.write` or `agent.cancel` for the
    same run. test: `crates/gdaemon/tests/node_relay.rs::relay_does_not_hold_the_run_queue`.

    1.2.5: Closing either side detaches the host terminal exactly once. test: `crates/gdaemon/tests/node_relay.rs::relay_close_detaches_once`.

    1.2.6: Across dispatch, a refused relay socket, a relayed frame and close, the
    node''s stderr names the terminal id and close reason but never the token, its
    digest or frame payload bytes. test: `crates/gdaemon/tests/node_relay.rs::relay_stderr_carries_no_secrets`.

    1.2.7: A host that never sends `Welcome`, a host that never sends `Attached`,
    and channel loss during setup each end the setup task within 10 s, leaving no
    host attachment and no hub socket. The deadline cases answer `command_failed`
    with stage `"none"`. test: `crates/gdaemon/tests/node_relay.rs::relay_setup_is_bounded_and_cleans_up`.'
  labels:
  - covers:remote-terminal-attach:1.2:1.2.1
  - covers:remote-terminal-attach:1.2:1.2.2
  - covers:remote-terminal-attach:1.2:1.2.3
  - covers:remote-terminal-attach:1.2:1.2.4
  - covers:remote-terminal-attach:1.2:1.2.5
  - covers:remote-terminal-attach:1.2:1.2.6
  - covers:remote-terminal-attach:1.2:1.2.7
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Node `terminal.resize` executor
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: `terminal.resize` for a launched run sends the host
    `resize` for its host terminal and answers `{}`. test: `crates/gdaemon/tests/node_resize.rs::resize_reaches_host_control`.

    1.3.2: An unknown or unlaunched terminal answers `not_found`; a host refusal or
    an unavailable host answers `command_failed` with stage `"none"`. test: `crates/gdaemon/tests/node_resize.rs::resize_failure_dispositions`.

    1.3.3: `terminal.resize` and `terminal.write` for one run apply in arrival order.
    test: `crates/gdaemon/tests/node_resize.rs::resize_orders_with_writes`.'
  labels:
  - covers:remote-terminal-attach:1.3:1.3.1
  - covers:remote-terminal-attach:1.3:1.3.2
  - covers:remote-terminal-attach:1.3:1.3.3
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Relay tokens and the hub relay endpoint
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.1.1: A minted token is accepted once from its machine on
    its channel within 10 s, and the socket reaches the waiter. test: `crates/gdaemon/tests/terminal_relay.rs::token_accepts_once`.

    2.1.2: A reused, expired, unknown, wrong-machine or wrong-connection token gets
    the same 401 `relay_token_invalid`. test: `crates/gdaemon/tests/terminal_relay.rs::invalid_tokens_are_indistinguishable`.

    2.1.3: Dropping the waiter''s guard revokes its token. test: `crates/gdaemon/tests/terminal_relay.rs::dropped_waiter_revokes_token`.

    2.1.4: `CommandSender::closed` resolves when the node channel ends. test: `crates/gdaemon/tests/terminal_relay.rs::sender_closed_follows_channel_end`.

    2.1.5: The relay endpoint is served only by a hub door. test: `crates/gdaemon/tests/terminal_relay.rs::relay_route_is_hub_only`.

    2.1.6: A channel connection holding 32 pending relays gets `RelayBusy` and no
    entry; a dropped guard or an expired entry frees a slot, and another connection''s
    pending relays do not count. test: `crates/gdaemon/tests/terminal_relay.rs::pending_relays_are_capped_per_connection`.'
  labels:
  - covers:remote-terminal-attach:2.1:2.1.1
  - covers:remote-terminal-attach:2.1:2.1.2
  - covers:remote-terminal-attach:2.1:2.1.3
  - covers:remote-terminal-attach:2.1:2.1.4
  - covers:remote-terminal-attach:2.1:2.1.5
  - covers:remote-terminal-attach:2.1:2.1.6
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Remote terminal link over the node channel
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: `open` for an owned machine with a live channel returns
    frames from the node''s relay socket, and sent allowlisted messages reach the
    node. test: `crates/gdaemon/tests/terminal_relay.rs::open_relays_frames_both_ways`.

    2.2.2: Another owner''s machine answers `machine_not_owned`, an unknown machine
    `machine_not_found`, a machine with no channel `node_offline`, and a connection
    at its pending ceiling `relay_busy`, with no command sent. test: `crates/gdaemon/tests/terminal_relay.rs::open_refusals_send_nothing`.

    2.2.3: A node error reply, a missing socket after 10 s, and channel loss during
    the wait each fail the open and revoke the token. test: `crates/gdaemon/tests/terminal_relay.rs::open_failures_revoke_token`.

    2.2.4: Channel loss during a live relay closes the frame receiver. test: `crates/gdaemon/tests/terminal_relay.rs::channel_loss_ends_live_relay`.

    2.2.5: `write` sends `terminal.write` after the owner check and maps node errors.
    test: `crates/gdaemon/tests/terminal_relay.rs::write_sends_terminal_write`.

    2.2.6: Across `terminal.relay` dispatch through the registry, an invalid-token
    refusal, an accepted upgrade, a frame and a write in each direction, and close,
    the hub''s stderr never carries the raw token, its SHA-256 digest in hex or base64url,
    or the frame and input payload bytes. test: `crates/gdaemon/tests/terminal_relay.rs::relay_stderr_carries_no_secrets`.

    2.2.7: `resize` sends `terminal.resize` after the owner check and maps node errors.
    test: `crates/gdaemon/tests/terminal_relay.rs::resize_sends_terminal_resize`.'
  labels:
  - covers:remote-terminal-attach:2.2:2.2.1
  - covers:remote-terminal-attach:2.2:2.2.2
  - covers:remote-terminal-attach:2.2:2.2.3
  - covers:remote-terminal-attach:2.2:2.2.4
  - covers:remote-terminal-attach:2.2:2.2.5
  - covers:remote-terminal-attach:2.2:2.2.6
  - covers:remote-terminal-attach:2.2:2.2.7
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Owner-scoped hub-wide terminal inventory
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '3.1.1: A hub caller''s inventory lists rows from every machine
    it owns and none from another owner''s machine. test: `crates/gterminals/tests/inventory.rs::inventory_is_owner_wide`.

    3.1.2: Remote rows carry `machine_id` and `attach: null`; local rows keep their
    locator. test: `crates/gterminals/tests/inventory.rs::remote_rows_have_no_locator`.

    3.1.3: The single-row read answers an owned remote row and 404 for another owner''s
    row. test: `crates/gterminals/tests/inventory.rs::row_read_is_owner_scoped`.

    3.1.4: A caller without a user identity keeps the local-machine scope. test: `crates/gterminals/tests/inventory.rs::no_user_keeps_local_scope`.

    3.1.5: The WS protocol documents owner-wide listing and `attach: null` rows. behavior:
    "attach: null" in `docs/contracts/gterm-protocols.md`.'
  labels:
  - covers:remote-terminal-attach:3.1:3.1.1
  - covers:remote-terminal-attach:3.1:3.1.2
  - covers:remote-terminal-attach:3.1:3.1.3
  - covers:remote-terminal-attach:3.1:3.1.4
  - covers:remote-terminal-attach:3.1:3.1.5
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Remote attach through the hub relay
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: A proxied attach of an owned remote row delivers the
    node''s history and live frames as `terminal_frame` messages. test: `crates/gterminals/tests/remote_attach.rs::remote_attach_streams_frames`.

    3.2.2: `node_offline`, `machine_not_owned`, `relay_busy` and a `direct` request
    fail the attach with their codes and leave no attachment. test: `crates/gterminals/tests/remote_attach.rs::remote_attach_refusals`.

    3.2.3: A stalled client socket closes only that socket, and the link is not read
    while its queue is full. test: `crates/gterminals/tests/remote_attach.rs::remote_backpressure_closes_only_the_slow_socket`.

    3.2.4: Detach sends `Detach` to the node, and channel loss finalizes the attachment
    exactly once. test: `crates/gterminals/tests/remote_attach.rs::remote_close_finalizes_once`.

    3.2.5: A hub wires the link into the family; a standalone daemon does not. test:
    `crates/gterminals/tests/remote_attach.rs::only_hub_family_has_remote_link`.

    3.2.6: The WS protocol documents remote attach and its error codes. behavior:
    "node_offline" in `docs/contracts/gterm-protocols.md`.'
  labels:
  - covers:remote-terminal-attach:3.2:3.2.1
  - covers:remote-terminal-attach:3.2:3.2.2
  - covers:remote-terminal-attach:3.2:3.2.3
  - covers:remote-terminal-attach:3.2:3.2.4
  - covers:remote-terminal-attach:3.2:3.2.5
  - covers:remote-terminal-attach:3.2:3.2.6
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Remote terminal input and sizing through node commands
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: Input on a remote attachment reaches the link as one
    write with its kind, data and submit flag, and never the local write route. test:
    `crates/gterminals/tests/remote_attach.rs::remote_input_uses_terminal_write`.

    3.3.2: Input to a node that went offline or to a machine the caller does not own
    answers its code and writes nothing. test: `crates/gterminals/tests/remote_attach.rs::remote_input_refusals`.

    3.3.3: `terminal_paste` on a remote attachment reaches the link as one `paste`
    write. test: `crates/gterminals/tests/remote_attach.rs::remote_paste_uses_terminal_write`.

    3.3.4: An elected `terminal_resize`, an attach that changes the election and a
    detach that re-elects each send the elected geometry to the link once; a losing
    requester changes only its viewport and sends nothing. test: `crates/gterminals/tests/remote_attach.rs::remote_sizing_follows_the_election`.

    3.3.5: Take and release control on a remote attachment change only the lease and
    call no host grant or revoke. test: `crates/gterminals/tests/remote_attach.rs::remote_control_applies_no_host_grant`.'
  labels:
  - covers:remote-terminal-attach:3.3:3.3.1
  - covers:remote-terminal-attach:3.3:3.3.2
  - covers:remote-terminal-attach:3.3:3.3.3
  - covers:remote-terminal-attach:3.3:3.3.4
  - covers:remote-terminal-attach:3.3:3.3.5
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Open another machine's terminal in its owning workspace
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '4.1.1: Selecting another machine''s agent attaches that machine''s
    workspace (node argument set to its machine) and focuses the agent''s pane. test:
    `crates/gclient/tests/remote_agent_open.rs::remote_agent_opens_in_owning_workspace`.

    4.1.2: Selecting a local agent afterwards returns to the local workspace with
    no node argument. test: `crates/gclient/tests/remote_agent_open.rs::local_agent_returns_to_local_workspace`.

    4.1.3: Another machine''s agent row appears under `all` with its machine token
    and is hidden under the default filter (carries gclient-workspace-sidebar 3.2.4).
    test: `crates/gclient/tests/remote_agent_open.rs::remote_agent_row_shows_machine_token`.

    4.1.4: Bare terminal rows follow the attached workspace''s machine. test: `crates/gclient/tests/remote_agent_open.rs::terminal_rows_follow_attached_machine`.

    4.1.5: When no owning-machine workspace holds the remote agent''s pane, selecting
    it attaches that machine''s default workspace for the agent''s project and places
    the pane there; the previous machine''s workspace gets no pane. test: `crates/gclient/tests/remote_agent_open.rs::unplaced_remote_agent_opens_in_owning_default_workspace`.

    4.1.6: A pane that moves between snapshot and attach answers the retry warning,
    and a failed owning-workspace attach leaves the previous workspace and attach
    target unchanged; neither opens a pane in the previous machine''s workspace. test:
    `crates/gclient/tests/remote_agent_open.rs::remote_open_never_lands_in_previous_machine_workspace`.'
  labels:
  - covers:remote-terminal-attach:4.1:4.1.1
  - covers:remote-terminal-attach:4.1:4.1.2
  - covers:remote-terminal-attach:4.1:4.1.3
  - covers:remote-terminal-attach:4.1:4.1.4
  - covers:remote-terminal-attach:4.1:4.1.5
  - covers:remote-terminal-attach:4.1:4.1.6
  tdd: true
  source_section: '4.1'
  implementation_domain: frontend
- title: ROADMAP corrections for remote attach
  category: docs
  task_type: chore
  depends_on: []
  validation_criteria: '5.1.1: The S4.3 row and the Stage 0 paragraph name this plan
    and no #19600 wait. behavior: "remote-terminal-attach" in `ROADMAP.md`.

    5.1.2: No passage outside Decision 23 calls #19600 open or a gate. file: `ROADMAP.md`.

    5.1.3: The S1.4 row lists the landed `machines` columns and no endpoint column.
    behavior: "node_version" in `ROADMAP.md`.'
  labels:
  - covers:remote-terminal-attach:5.1:5.1.1
  - covers:remote-terminal-attach:5.1:5.1.2
  - covers:remote-terminal-attach:5.1:5.1.3
  tdd: false
  source_section: '5.1'
  assigned_agent: tech-writer
- title: Live hub-to-node remote attach proof
  category: test
  task_type: task
  depends_on:
  - '4.1'
  validation_criteria: '5.2.1: The hub''s inventory lists the node''s terminal with
    `attach: null`, and a proxied attach streams its frames. test: `tests/e2e/test_remote_terminal_attach_live.py::test_remote_attach_streams_node_frames`.

    5.2.2: `terminal_input` on the remote attachment appears in the node terminal''s
    next frames. test: `tests/e2e/test_remote_terminal_attach_live.py::test_remote_input_round_trips`.

    5.2.3: After the node stops, attach answers `node_offline` and the live attachment
    finalizes. test: `tests/e2e/test_remote_terminal_attach_live.py::test_node_loss_finalizes_and_refuses`.

    5.2.4: A real gclient whose local machine is the hub opens the node''s agent from
    the `all` sidebar into the node''s workspace, shows its relayed output, and echoes
    typed input. test: `tests/e2e/test_remote_terminal_attach_live.py::test_gclient_opens_remote_agent`.

    5.2.5: Resizing the gclient window changes the node PTY size that the node host''s
    control `list` reports (`rows`, `cols`; `crates/gterminal/src/host/helpers.rs`),
    and a second, losing WS observer''s `terminal_resize` leaves it unchanged. test:
    `tests/e2e/test_remote_terminal_attach_live.py::test_remote_pty_follows_elected_size`.'
  labels:
  - covers:remote-terminal-attach:5.2:5.2.1
  - covers:remote-terminal-attach:5.2:5.2.2
  - covers:remote-terminal-attach:5.2:5.2.3
  - covers:remote-terminal-attach:5.2:5.2.4
  - covers:remote-terminal-attach:5.2:5.2.5
  tdd: false
  source_section: '5.2'
  assigned_agent: developer
```
