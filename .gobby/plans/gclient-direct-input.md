# gclient direct input: keystrokes bypass the daemon

Plan artifact: `.gobby/plans/gclient-direct-input.md`

**Plan ID:** gclient-direct-input

**Plan kind:** implementation

## P0: Decision and scope
`kind: framing`

### 0.1 Decision record
`kind: framing`

Josh, 2026-09-19 (memory `b59e4ce9`): "gclient typing cannot go through the daemon."
This supersedes item 1 of the 2026-09-16 workspaces decision (memory `be35449d`) for
the input path only. The daemon still owns leases, layout and workspaces.

Evidence for why the daemon path cannot be made good enough (task #22557 removed the
seven Postgres transactions per keystroke, commit 9a88ea3ab7, and typing stayed slow):

- `send_live_write` in `crates/gclient/src/app/live_loop/control.rs` awaits a daemon
  websocket round trip per keystroke inside the render loop, so daemon latency freezes
  the whole window and the 5 s `REQUEST_DEADLINE` (`crates/gclient/src/daemon/live.rs`)
  flips the pane to `ControlState::UncertainReadOnly` ("read-only").
- The daemon websocket server handles messages serially per connection
  (`src/gobby/servers/websocket/server.py`, warning "later messages on this connection
  waited"), so one slow write stalls every pane in the window.
- The keystroke path shares the daemon event loop with agent spawning, hooks and DB
  work; five concurrent close validators put the daemon at 92% CPU and keys took 1 s.

Decisions taken in this plan (recommended defaults; Josh has not yet confirmed them
individually, see the checkpoint at the end of the plan):

1. Transport: gclient writes on the frame stream it already holds directly to
   `gterm-frames.sock` (`UnixSocketFrameSource`, `Transport::Direct`). No new socket, no
   control-token sharing, no fd passing.
2. Authority: gterm accepts `Input`/`Paste` on a frame attachment only while the daemon
   has granted that attachment. The grant is keyed by the daemon's attachment id (the
   uuid `TerminalLeaseRegistry` issues); the frame attachment claims that id with a new
   `BindAttachment` message after `Attached`. The daemon issues `grant_input` /
   `revoke_input` over `gterm-control.sock` on every lease holder change, so the lease
   model (take-back, web viewer exclusivity) is preserved while the daemon leaves the
   data path.
3. Single holder per terminal in gterm (`TerminalSlot.input_grant: Option<String>`),
   mirroring the single lease holder. A new grant replaces the old one. Grants survive
   the daemon's control connection dropping: a daemon restart must not stop typing; the
   fresh daemon re-grants when the pane retakes control.
4. Daemon knowledge of typing is kept through gterm `input_activity` events on the
   existing `subscribe_events` stream, one per accepted `Input`/`Paste`, carrying the
   interrupt classification (`esc`, `ctrl_c`, or null) and byte count instead of the
   payload. The turn observer (`record_mediated_input`) and the write coordinator's
   quarantine lift (`_release_quarantine`) consume them off the keystroke path.
5. No per-write outcome for direct panes: gclient never enters `UncertainReadOnly` for a
   direct native pane. Read-only now means the grant is missing (typed `InputRefused`
   from gterm) or the stream closed (existing re-attach path). gterm answers an
   ungranted or oversize `Input` with a typed `InputRefused { code }` on the same stream.
6. Ordering: the daemon completes `grant_input` before it replies
   `terminal_control_result`, and the reply carries `host_input_granted` (bool, null when
   not applicable). gclient sends `BindAttachment` lazily, right before the first `Input`
   after a grant, so an older gterm never sees an unknown message.
7. Out of scope, unchanged: tmux panes, web viewers, proxied (`frame_delivery: proxy`)
   and remote panes keep the daemon write path; PTY resize and scroll offset are
   unchanged; `PROTOCOL_VERSION` stays 1 because every new wire variant is appended and
   never sent to a peer that has not proven it understands it (decision 6).

Rejected alternatives: pipelining daemon writes off the render loop (still routes
through the daemon, ruled out by Josh); handing gclient the control token (any client
could write any terminal regardless of lease); gterm accepting Input from any frame
attachment (two windows on one terminal would both write, web exclusivity breaks);
SCM_RIGHTS PTY fd passing (bypasses gterm's per-terminal write serialisation and
bracketed paste); bumping `PROTOCOL_VERSION` (it would refuse an unrebuilt gclient that
still works fine through the daemon).

Relation to #22557: its daemon-side work is delivered; its live criterion "gclient
typing is fast" is carried by this plan's verification section.

### 0.2 Constraints and evidence
`kind: framing`

- Load the `rust` skill before editing crates. A crate change is live only after
  `cargo build --release`, `promote_workspace_binary_set` for the coherent set, and a
  separate gclient rebuild/promotion; then an announced `gobby restart --wait` from the
  main checkout with no live spawned worker or close validator (memory `f71268af`).
- Wire golden corpus: any wire shape change regenerates
  `crates/gterminal/tests/fixtures/wire_golden/` in the same commit
  (`docs/contracts/gterm-protocols.md`, Versioning).
- Python frame client `src/gobby/terminals/frame_client.py` encodes `Hello`,
  `AttachTerminal`, `SetViewport`, `SetScrollOffset`, `Detach` by hand. Appending new
  variants at the end of `ClientMessage`/`ServerMessage` keeps every existing tag and
  field layout; the daemon's observer stream never sends or receives the new variants,
  so it needs no encoder change.
- Consumer sweeps run on 0.5.0 in the main checkout with the live index:
  `gcode grep -w terminal_input src/ crates/gclient/src web/src -m 40` (producers:
  `crates/gclient/src/app/live_loop/control.rs:249`, `crates/gclient/src/app/mod.rs:546`,
  `web/src/hooks/tmuxSessionMessages.ts:179`; consumer:
  `src/gobby/servers/websocket/server.py:419`);
  `gcode grep -F '"terminal_input"' crates/gclient/tests -m 80` (client_loop.rs asserts
  daemon writes at lines 643, 731, 857, 886, 913, 1378, 1457, 1503-1586, 1964, 3246,
  3275, 3955, 4232-4306, 4605-4616, 6077; attention_flow.rs:223);
  `gcode grep -F frame_channel_is_read_only crates` (only
  `crates/gterminal/tests/frame_protocol.rs:195`);
  `gcode grep -F sent_host_input crates/gclient` (`crates/gclient/tests/copy_paste.rs:45,192`);
  `gcode grep -F control_result tests/servers/test_terminal_ws_golden.py`
  (`tests/fixtures/terminal_ws_golden/control_result.json` pinned at lines 392, 594);
  `gcode grep -F TerminalHostManager( src/gobby` (only
  `src/gobby/runner_init/terminal_wiring.py:57`);
  `gcode grep -w _apply_terminal_sizing src/gobby` (seven lease transition sites; the
  grant reconcile deliberately hooks the registry instead of each site).
- Size ceiling: `crates/gclient/src/app/mod.rs` (989 lines) and
  `src/gobby/terminals/host_manager.py` (985 lines) are already above the 850-line
  decomposition trigger; the deliverables that touch them name their split.

## P1: Host and daemon
`kind: framing`

### 1.1 gterm accepts granted input on the frame stream [category: code]
`kind: deliverable`

Targets:
- `crates/gterminal/src/protocol/wire_types.rs::ClientMessage`
- `crates/gterminal/src/protocol/wire_types.rs::ServerMessage`
- `crates/gterminal/src/host/state.rs::TerminalSlot`
- `crates/gterminal/src/host/state.rs::Attachment`
- `crates/gterminal/src/host/state.rs::HostState::attach`
- `crates/gterminal/src/host/frames.rs::handle_connection`
- `crates/gterminal/src/host/control.rs::dispatch`
- `crates/gterminal/src/host/write.rs::HostState::write`
- `crates/gterminal/src/host/events.rs::HostEvents::emit_terminal_exited`
- `crates/gterminal/tests/frame_protocol.rs::frame_channel_is_read_only`
- `crates/gterminal/tests/control_protocol.rs::control_surface_round_trip`
- `crates/gterminal/tests/wire_golden.rs::golden_corpus_bytes_and_fragmented_reads`
- `crates/gterminal/tests/fixtures/wire_golden/frame_bind_attachment.bin`
- `crates/gterminal/tests/fixtures/wire_golden/frame_input.bin`
- `crates/gterminal/tests/fixtures/wire_golden/frame_paste.bin`
- `crates/gterminal/tests/fixtures/wire_golden/frame_input_refused.bin`
- `crates/gterminal/tests/fixtures/wire_golden/control_grant_input.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_revoke_input.json`
- `crates/gterminal/tests/fixtures/wire_golden/control_input_activity.json`

Research context:

- Observed: `ClientMessage` (`wire_types.rs:210-254`) ends with `SetScrollOffset`; the
  three `Legacy*` variants are rejected by `ClientMessage::is_legacy_unknown` in
  `frames.rs::handle_connection` with `unknown_message`. `ServerMessage`
  (`wire_types.rs:482-514`) ends with `Attached`. Append, never reorder: bincode encodes
  the variant index.
- Observed: `handle_connection` is one `tokio::select!` loop per stream with
  `attachment_id: Option<u64>` and the outbound `FrameMailbox`; `AttachTerminal` goes
  through `embed::attach_frame`, `Detach` through `embed::detach_frame`, the final
  cleanup detaches and half-closes then drains the peer (keep that).
- Observed: `HostState::write` (`write.rs:39-93`) resolves `by_host_id` -> `terminals`,
  refuses tmux slots with `not_native`, decodes `utf8-b64`, caps at `MAX_WRITE_BYTES`
  (1 MiB), then `child.runtime.try_send_bytes(Bytes)` for keys/text and
  `write_paste` -> `child.runtime.try_send_paste(text)` for pastes, behind
  `#[cfg(feature = "vt-engine")]`. Both silently ignore `TrySendError::Full`.
- Observed: `HostEvents::emit` stamps `epoch` and `seq`, keeps a 256-entry /
  `event_queue_bytes` ring, and fans out to subscribers; `emit_terminal_exited` is the
  only emitter. `HostState::subscribe_events` is reached from `control.rs::dispatch`.
- Observed: `control.rs::is_mutating` lists the ledgered verbs (`spawn`, `kill`,
  `resize`, `write`, `write_batch`). `reserve_observer`/`release_observer` are
  idempotent and unledgered; the new verbs follow them.
- Observed: `TerminalSlot` (`state.rs:67-104`) has `user_attachments: HashSet<u64>` and
  `locator: Option<PaneLocator>` (Some means tmux); `Attachment` (`state.rs:106-121`)
  is created in `HostState::attach`.

Implementation:

- New wire variants appended: `ClientMessage::BindAttachment { attachment_id: String }`,
  `ClientMessage::Input { data: Vec<u8> }`, `ClientMessage::Paste { text: String }`;
  `ServerMessage::InputRefused { code: String }`. Codes: `attach_required` (no
  attachment on this stream), `input_not_granted`, `not_native`, `request_too_large`
  (over `MAX_WRITE_BYTES`), `pty_busy` (`TrySendError::Full`), `terminal_gone`.
- State: `Attachment.client_attachment_id: Option<String>` (set by `BindAttachment`,
  reset by a later `AttachTerminal` on the same stream); `TerminalSlot.input_grant:
  Option<String>`.
- Control verbs in `dispatch`: `grant_input { host_terminal_id, attachment_id }` sets
  `input_grant` (reply `{ok:true, granted:true, previous: <old id or null>}`);
  `revoke_input { host_terminal_id, attachment_id? }` clears it when it matches or when
  `attachment_id` is omitted (reply `{ok:true, revoked: bool}`); both answer `not_found`
  for unknown terminals and `not_native` for tmux slots; neither is ledgered.
  `HostState::on_control_disconnect` leaves grants alone (decision 3).
- Frame handler: `Input`/`Paste` resolve the stream's attachment, compare
  `client_attachment_id` with the slot's `input_grant`, then reuse the same
  `try_send_bytes`/`try_send_paste` calls `HostState::write` uses; every refusal is an
  `InputRefused` on the same stream and never closes it. Factor the shared native
  delivery out of `HostState::write` into one helper both paths call so the vt-engine
  gate and size cap live once.
- Event: after an accepted `Input`/`Paste`, emit `{"event":"input_activity",
  "terminal_id", "host_terminal_id", "attachment_id", "kind": "input"|"paste",
  "bytes": n, "interrupt": "esc"|"ctrl_c"|null}`; `interrupt` is set only when the
  whole `Input` payload is exactly `\x1b` or exactly `\x03` (mirrors
  `is_interrupt_input` in `src/gobby/sessions/terminal_turn_observer.py`). Per-message
  emission is deliberate: no coalescing state machine, and the daemon handler is O(1).
- Tests: rewrite `frame_channel_is_read_only` as the granted/ungranted pair (ungranted
  `Input` -> `InputRefused{input_not_granted}` and the child sees nothing; after
  `grant_input` over control and `BindAttachment`, `Input` reaches the child, then
  `revoke_input` refuses again); extend `control_surface_round_trip` with both verbs and
  the `input_activity` event; extend the golden corpus with the seven fixtures above.
- Verification (planned): `cargo test -p gobby-terminal --test frame_protocol --test
  control_protocol --test wire_golden`, `cargo clippy -p gobby-terminal --all-targets`.
  Observed before this plan: none of these tests know the new messages.

Consumers unchanged:
- `crates/gterminal/src/host/mod.rs` — no-edit-reason: Only spawns handle_connection per accepted stream with the same signature; BindAttachment, Input and Paste are dispatched inside the handler.

**Acceptance:**

- 1.1.1 - `ClientMessage` gains appended `BindAttachment`, `Input`, `Paste` and
  `ServerMessage` gains appended `InputRefused`, with existing variant order and the
  legacy rejection unchanged. symbol: `ClientMessage`. file: `crates/gterminal/src/protocol/wire_types.rs`.
- 1.1.2 - `grant_input` and `revoke_input` control verbs set and clear
  `TerminalSlot.input_grant`, refuse tmux slots with `not_native`, and stay out of the
  operation ledger. symbol: `dispatch`. file: `crates/gterminal/src/host/control.rs`.
- 1.1.3 - The frame handler delivers `Input`/`Paste` to the PTY only when the stream's
  bound attachment id equals the slot's grant, and answers every refusal with a typed
  `InputRefused` without closing the stream. symbol: `handle_connection`.
- 1.1.4 - Accepted input emits one `input_activity` event with kind, byte count and
  interrupt classification and no payload. symbol: `HostEvents::emit_terminal_exited`. file: `crates/gterminal/src/host/events.rs`.
- 1.1.5 - Frame protocol test proves ungranted input is refused and granted input
  writes. test: `crates/gterminal/tests/frame_protocol.rs::ungranted_input_is_refused_and_granted_input_writes`.
- 1.1.6 - Control protocol test covers grant, revoke and the event. test: `crates/gterminal/tests/control_protocol.rs::grant_input_binds_one_holder_and_emits_input_activity`.
- 1.1.7 - Golden corpus covers the new frame and control messages. test: `crates/gterminal/tests/wire_golden.rs::golden_corpus_bytes_and_fragmented_reads`.

**Granularity:** seven acceptance items and nine production files in one crate because
the wire variants, the handler and the control verbs are one protocol change that must
land in one commit with its golden corpus; splitting them would leave an
uncompilable or contract-violating intermediate state.

### 1.2 Daemon grants input on lease transitions and consumes input activity [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py::HostClient`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime`
- `src/gobby/terminals/input_grants.py`
- `src/gobby/terminals/leases.py::TerminalLeaseRegistry`
- `src/gobby/terminals/leases.py::ControlResult`
- `src/gobby/servers/websocket/terminal_ws_control.py::TerminalControlMixin`
- `src/gobby/terminals/host_events.py::*` — scope-reason: the event union, decoder and stream type all change shape for the second event kind
- `src/gobby/terminals/host_manager.py::TerminalHostManager`
- `src/gobby/terminals/host_event_reader.py`
- `src/gobby/terminals/write_coordinator.py::WriteCoordinator`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `tests/terminals/host_fakes.py::FakeControlClient`
- `tests/terminals/test_wire_golden.py::test_control_goldens_match_rust_emitter`
- `tests/terminals/test_wire_golden.py::test_control_client_matches_golden_corpus`
- `tests/terminals/test_host_client.py::*` — scope-reason: grant_input and revoke_input round-trip tests join the existing HostClient tests, which all share the FakeControlClient contract that changes
- `tests/terminals/test_native_runtime.py::*` — scope-reason: grant and revoke pass-through tests plus the not_native and not_found refusal mapping join the runtime tests
- `tests/terminals/test_host_manager.py::*` — scope-reason: the event reader tests follow the split into host_event_reader.py and gain the input_activity routing case
- `tests/terminals/test_write_coordinator.py::*` — scope-reason: observe_operator_input tests extend the quarantine release cases already in the file
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: every existing take, release and finalize case gains grant or revoke assertions and the new transition test joins them
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: the pinned control_result shape changes, so both pinning tests and the corpus loader re-assert it
- `tests/fixtures/terminal_ws_golden/control_result.json::*` — scope-reason: regenerated fixture carrying host_input_granted

Research context:

- Observed lease transitions: `TerminalLeaseRegistry.take_control` (`leases.py:386`),
  `release_control` (`:416`) and `finalize` (`:437`) mutate `lease.holder` under
  `self.lock(terminal_id)` and return `ControlResult`/`FinalizedEvent` carrying a
  `sizing` decision that the websocket mixins apply at seven call sites
  (`gcode grep -w _apply_terminal_sizing src/gobby`). The grant must not be a seventh
  copy: hook the registry once.
- Observed: `_Attachment` (`leases.py:120`) carries `frame_delivery` ("direct" for a
  gclient direct attach, `proxy` otherwise), `viewer` and `backend`; that is enough to
  decide whether the holder is a direct native gclient pane.
- Observed: `TerminalControlMixin._handle_terminal_take_control`
  (`terminal_ws_control.py:34`) replies `terminal_control_result` with `granted`,
  `reason`, `lease_generation`; the golden fixture
  `tests/fixtures/terminal_ws_golden/control_result.json` pins that shape.
- Observed: `HostClient.write` (`host_client.py:463`) shows the request shape; unledgered
  verbs go through `_roundtrip`, ledgered through `_mutating_roundtrip`.
  `NativeTerminalRuntime._host_id(terminal)` maps a row to `host_terminal_id`;
  `HostManagerControl` proxies the live client.
- Observed: `decode_host_event` (`host_events.py:34`) raises on anything but
  `terminal_exited`; `HostEvent = TerminalExitedEvent`; `TerminalHostManager
  ._apply_host_event` (`host_manager.py:837`) calls `manager.settle_exit` for every
  event; `_event_reader_loop` (`:866`) is the consumer. `host_manager.py` is 985 lines.
- Observed: `WriteCoordinator._write_locked` (`write_coordinator.py:408`) calls
  `_release_quarantine(terminal)` only for a `Delivered` operator write;
  `_release_quarantine` (`:529`) already coalesces to one UPDATE per quarantine episode.
  `record_mediated_input(terminal_id, payload, outcome, input_seq=None)` in
  `src/gobby/sessions/terminal_turn_observer.py:85` needs only an exact `\x1b` or
  `\x03` payload to arm a candidate and any other payload to pop it.
- Observed wiring: `init_terminal_wiring` (`terminal_wiring.py`) builds the lease
  registry before the native runtime and the write coordinator after both, and is the
  only constructor of `TerminalHostManager`. Resolve where the turn observer is built
  with `gcode grep -w terminal_turn_observer src/gobby -m 20` before wiring the sink.

Implementation:

- `HostClient.grant_input(host_terminal_id, attachment_id)` and
  `revoke_input(host_terminal_id, attachment_id=None)` (unledgered `_roundtrip`);
  `NativeTerminalRuntime.grant_input(terminal, attachment_id)` /
  `revoke_input(terminal, attachment_id=None)` map the row through `_host_id`.
- New `src/gobby/terminals/input_grants.py`: `async def sync_host_input_grant(runtime,
  terminal, holder) -> bool | None` returns None when the terminal is not native or the
  holder is not a `frame_delivery == "direct"` attachment (after revoking any grant),
  True when `grant_input` succeeded, False when the host refused or was unavailable
  (`HostCommandError`, `HostUnavailableError` caught and logged once per transition).
- `TerminalLeaseRegistry` gains `set_holder_observer(callback)`; `take_control`,
  `release_control` and `finalize` await it after mutating the holder and before
  returning, still under the terminal lock, and `ControlResult` gains
  `host_input_granted: bool | None`. `finalize` logs and ignores the result.
- `_handle_terminal_take_control` and `_handle_terminal_release_control` add
  `host_input_granted` to `terminal_control_result`; regenerate `control_result.json`
  and its assertion in `test_terminal_ws_golden.py`.
- Events: `InputActivityEvent(terminal_id, host_terminal_id, attachment_id, kind,
  bytes, interrupt, epoch, seq)`; `HostEvent = TerminalExitedEvent | InputActivityEvent`;
  `decode_host_event` dispatches on `event`. Move `_connect_event_stream`,
  `_arm_events`, `_apply_host_event`, `_recover_event_gap` and `_event_reader_loop` from
  `host_manager.py` into the new `src/gobby/terminals/host_event_reader.py` (split
  named for the size ceiling) and give the manager an `input_activity_sink:
  Callable[[InputActivityEvent], None] | None`; exited events keep calling
  `settle_exit`, input events call the sink and never touch the DB from the reader.
- `WriteCoordinator.observe_operator_input(terminal_id)` loads the row through the
  store and calls `_release_quarantine`; it is the quarantine lift for direct input.
- `init_terminal_wiring` sets the holder observer (runtime resolved from the registry's
  native runtime) and the sink: sink calls `record_mediated_input(terminal_id,
  "\x1b" if interrupt == "esc" else "\x03" if interrupt == "ctrl_c" else "", "delivered")`
  then `observe_operator_input(terminal_id)`.
- Tests: `FakeControlClient.grant_input/revoke_input` recording calls; host client
  request shape; runtime mapping; lease test that a direct gclient take grants, a web
  takeover revokes, release revokes, socket loss revokes, and the result carries
  `host_input_granted`; host manager test that an `input_activity` event reaches the
  sink and never `settle_exit`; coordinator test that `observe_operator_input` lifts a
  quarantine once.
- Verification (planned):
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_host_client.py tests/terminals/test_native_runtime.py tests/terminals/test_host_manager.py tests/terminals/test_write_coordinator.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_golden.py -q`,
  `uv run mypy src/`, `uv run ruff check src/ tests/`, the test-types and test-quality
  audits on the touched test files, and the suppression ratchet.

Consumers unchanged:
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/agents/lifecycle_monitor_terminals.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/agents/spawn_executor_support.py` — no-edit-reason: WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/agents/spawn_models.py` — no-edit-reason: WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/agents/watchdog/recovery.py` — no-edit-reason: WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/app_context.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/runner.py` — no-edit-reason: TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods.
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: Init_terminal_wiring keeps its signature; the observer and sink wiring happen inside it.
- `src/gobby/servers/routes/terminals.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/servers/websocket/broadcast.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/servers/websocket/server.py` — no-edit-reason: TerminalControlMixin keeps its handler names and mixin composition because the grant runs inside the registry, not the mixin.
- `src/gobby/servers/websocket/terminal_sizing.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/servers/websocket/terminal_ws.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/servers/websocket/tmux.py` — no-edit-reason: TerminalControlMixin keeps its handler names and mixin composition because the grant runs inside the registry, not the mixin.
- `src/gobby/servers/websocket/tmux_activation.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/servers/websocket/workspace_ws.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `src/gobby/terminals/services.py` — no-edit-reason: WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/terminals/sync_bridge.py` — no-edit-reason: WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `src/gobby/terminals/workspace_ops.py` — no-edit-reason: WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/agents/test_lifecycle_monitor_extra.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/agents/test_native_spawn.py` — no-edit-reason: HostClient gains grant_input and revoke_input as additive methods and its constructor is unchanged; NativeTerminalRuntime gains grant_input and revoke_input as additive methods and every existing method keeps its signature; TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/agents/test_terminal_prompt_monitor.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/agents/test_tmux_integration.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/agents/watchdog/test_composer_probe.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/communications/test_native_plan_actions.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/e2e/test_terminal_client_stack.py` — no-edit-reason: HostClient gains grant_input and revoke_input as additive methods and its constructor is unchanged.
- `tests/events/test_wake_native_terminal.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/mcp_proxy/test_registries.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/mcp_proxy/test_sessions_terminal_tools.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_response.py` — no-edit-reason: NativeTerminalRuntime gains grant_input and revoke_input as additive methods and every existing method keeps its signature.
- `tests/runner_init/test_probe_composer.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/test_attention_respond.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/test_terminal_list_watermark.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_terminal_ws_attach_honesty.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/test_terminal_ws_create.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_terminal_ws_input.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/test_terminal_ws_list.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_terminal_ws_resize.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; TerminalControlMixin keeps its handler names and mixin composition because the grant runs inside the registry, not the mixin; its _SizingServer builds a bare registry with no holder observer, so take and release behave exactly as today.
- `tests/servers/test_terminal_ws_viewport.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_tmux_activation.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/test_tmux_bridge_authority.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_tmux_mixin.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_websocket_server_disconnects.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/test_workspace_ws.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/servers/websocket/test_broadcast.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/servers/websocket/test_server.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/terminals/acceptance/conftest.py` — no-edit-reason: NativeTerminalRuntime gains grant_input and revoke_input as additive methods and every existing method keeps its signature; TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods.
- `tests/terminals/acceptance/test_native_writes.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/terminals/fakes.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/terminals/test_backend_selection.py` — no-edit-reason: NativeTerminalRuntime gains grant_input and revoke_input as additive methods and every existing method keeps its signature.
- `tests/terminals/test_composition_roots.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/terminals/test_host_shutdown_preservation.py` — no-edit-reason: TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods; FakeControlClient gains grant_input and revoke_input handlers while its existing verbs and recorded call shapes stay the same.
- `tests/terminals/test_lease_authority.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None.
- `tests/terminals/test_runtime_contract.py` — no-edit-reason: NativeTerminalRuntime gains grant_input and revoke_input as additive methods and every existing method keeps its signature; TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods.
- `tests/terminals/test_sync_bridge.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/terminals/test_workspace_ops.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/terminals/test_write_input.py` — no-edit-reason: NativeTerminalRuntime gains grant_input and revoke_input as additive methods and every existing method keeps its signature; TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/terminals/test_write_outcomes.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/test_runner_init.py` — no-edit-reason: TerminalLeaseRegistry keeps its no-argument constructor and the take_control, release_control and finalize signatures; the holder observer is installed only by init_terminal_wiring and ControlResult.host_input_granted defaults to None; WriteCoordinator gains observe_operator_input as an additive method and its constructor and write signatures are unchanged.
- `tests/test_runner_lifecycle_processes.py` — no-edit-reason: TerminalHostManager takes input_activity_sink as an optional constructor argument defaulting to None and the event reader split keeps its public methods.

**Acceptance:**

- 1.2.1 - Host client and native runtime expose `grant_input`/`revoke_input`. symbol: `HostClient`. file: `src/gobby/terminals/native_runtime.py`.
- 1.2.2 - The lease registry notifies one holder observer on take, release and finalize, and `ControlResult` reports `host_input_granted`. symbol: `TerminalLeaseRegistry`. file: `src/gobby/terminals/leases.py`.
- 1.2.3 - `sync_host_input_grant` grants only a direct native holder and revokes otherwise. file: `src/gobby/terminals/input_grants.py`.
- 1.2.4 - `terminal_control_result` carries `host_input_granted` and the golden fixture matches. file: `tests/fixtures/terminal_ws_golden/control_result.json`.
- 1.2.5 - `input_activity` events decode and reach the sink from the split event reader without touching the DB. file: `src/gobby/terminals/host_event_reader.py`.
- 1.2.6 - The sink arms or pops turn-interrupt candidates and lifts the automatic-write quarantine. symbol: `WriteCoordinator`. file: `src/gobby/runner_init/terminal_wiring.py`.
- 1.2.7 - Lease tests cover grant on direct take, revoke on web takeover, release and socket loss. test: `tests/servers/test_terminal_ws_lease.py::test_direct_gclient_holder_is_granted_and_revoked_on_transitions`.
- 1.2.8 - Host manager test routes input activity to the sink. test: `tests/terminals/test_host_manager.py::test_input_activity_reaches_sink_not_settle_exit`.
- 1.2.9 - The Python control client encodes `grant_input` and `revoke_input` byte-for-byte against the shared golden corpus and decodes `input_activity`. test: `tests/terminals/test_wire_golden.py::test_control_client_matches_golden_corpus`.

**Granularity:** nine items and ten production files, kept as one leaf because the
grant reconcile and the event consumer share the `HostClient`, the registry hook and
the wiring; each half alone leaves the daemon either granting without observing typing
or observing without granting, neither of which is a shippable state.

## P2: Client
`kind: framing`

### 2.1 gclient sends keystrokes on the frame stream and never awaits the daemon per key [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/frame_source.rs::*` — scope-reason: the FrameSource trait gains send_input, every implementor in the file (Unix socket, scripted, pane wrapper) gains it, the outbound channel widens, and ScriptedFrameSource::send exists as both an inherent and a trait method so it cannot be named exactly
- `crates/gclient/src/frame_source/proxy.rs::ProxyFrameSource::send`
- `crates/gclient/src/app/pane.rs::*` — scope-reason: the pane gains grant and bind fields, a `direct_input` predicate and its read-only predicates change meaning
- `crates/gclient/src/app/live_loop/control.rs::send_live_write`
- `crates/gclient/src/app/live_loop/control.rs::request_live_control`
- `crates/gclient/src/app/live_loop/control.rs::apply_live_write_outcome`
- `crates/gclient/src/app/live_loop.rs::route_live_input`
- `crates/gclient/src/app/live_attach.rs::install_direct_source`
- `crates/gclient/src/app/live_attach.rs::recover_live_frame_error`
- `crates/gclient/src/app/apply.rs::apply_control_result`
- `crates/gclient/src/app/mod.rs::Workspace::send_input`
- `crates/gclient/src/app/scripted_input.rs`
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: direct-pane keystroke tests join the loop tests and the existing daemon terminal_input assertions invert for direct panes
- `crates/gclient/tests/copy_paste.rs::*` — scope-reason: the two sent_host_input false assertions flip for direct panes and the paste path gains a host paste case
- `crates/gclient/tests/lease_control.rs::*` — scope-reason: take-control tests assert host_input_granted and the UncertainReadOnly cases for direct panes are removed
- `crates/gclient/tests/frame_source_live.rs::*` — scope-reason: the real-gterm harness gains grant, echo and revoke cases against a spawned host
- `crates/gclient/tests/mock_daemon/mod.rs::MockDaemon::enqueue_take_control_reply`

Research context:

- Observed: `send_live_write` (`control.rs:235-269`) builds `terminal_input`/
  `terminal_paste`, awaits `workspace.daemon().send(message)` and on error sets
  `ControlState::UncertainReadOnly`; `send_live_input` (`:205`) gates on
  `Pane::writable()` (`pane.rs:308`: live, not terminating, `Held`) and stages
  `pending_input` while a take is in flight; `request_live_control` (`:85-154`) reads
  `granted`/`lease_generation` from the reply and delivers the pending bytes;
  `route_live_input` (`live_loop.rs:603`) routes pastes and keys to those functions.
- Observed: `UnixSocketFrameSource::send` (`frame_source.rs:578`) allowlists
  `SetViewport`, `SetScrollOffset`, `Detach`, then awaits both the 16-slot `outbound`
  channel and the writer task's `done` oneshot; the reader task drains into a 256-slot
  `inbound` channel independently of the render loop. `ScriptedFrameSource::
  sent_host_input` (`:196`) is a stub returning false, asserted false in
  `copy_paste.rs:45,192`.
- Observed: `install_direct_source` (`live_attach.rs:138`) installs
  `PaneFrameSource::Direct` with the daemon attachment id; a daemon reconnect
  re-attaches panes and installs a fresh source, so the bind must be re-sent per
  installed source. `apply_control_result` (`apply.rs:83`) is the scripted-path twin
  of `request_live_control`; `Workspace::send_input` (`mod.rs:546`) is the scripted
  write path used by `client_loop.rs`. `mod.rs` is 989 lines.
- Observed: `MockDaemon::enqueue_take_control_reply` shapes the mock's
  `terminal_control_result`; tests that count `terminal_input` websocket requests for
  direct panes (list in 0.2) will change to assert host input instead.
- Observed: the daemon result field from 1.2 is `host_input_granted`; gterm refusals
  from 1.1 arrive as `ServerMessage::InputRefused { code }` on the frame stream.

Implementation:

- `FrameSource` gains `fn send_input(&mut self, message: &ClientMessage) ->
  Result<(), FrameError>`: non-awaiting. `UnixSocketFrameSource` implements it with
  `outbound.try_send` (capacity raised from 16 to 256) and a discarded `done`, mapping
  `TrySendError::Full` to a new `FrameError::Backpressure`; the allowlist in `send`
  admits `BindAttachment`, `Input`, `Paste` too. `ProxyFrameSource::send_input` returns
  `FrameError::Protocol` (proxied panes never take this path).
  `ScriptedFrameSource` records host input so `sent_host_input` becomes real.
- `Pane` gains `host_input_granted: bool` (from the control result) and
  `host_bound_attachment: Option<String>` (reset in `install_direct_source`), and
  `fn direct_input(&self) -> bool` = transport is `Direct`, backend native,
  `host_input_granted`.
- `send_live_write`: when `pane.direct_input()`, send `BindAttachment` once per
  installed source (when `host_bound_attachment != attachment_id`), then
  `Input { data }` or `Paste { text }` through `send_input`; no `client_write_seq`, no
  `in_flight_write`, no `UncertainReadOnly`. `Backpressure` sets the status line
  "terminal input backlog; key dropped" and keeps `Held`. Non-direct panes keep the
  daemon path unchanged.
- `request_live_control` and `apply_control_result` read `host_input_granted`; on a
  direct native pane `granted && host_input_granted != Some(true)` leaves the pane
  `Observe` with `take_back` and status "host input grant unavailable; take control
  again", so the next key retries the take.
- Frame receive: `InputRefused { code }` sets the pane `Observe` + `take_back` with the
  code in the status line (`input_not_granted` after a daemon-side revoke is the
  expected case); it never retires the source. Route it where
  `recv_workspace_frame`/`recover_live_frame_error` classify host messages.
- Scripted path: split `crates/gclient/src/app/mod.rs` by moving `Workspace::send_input` and its paste twin into
  `crates/gclient/src/app/scripted_input.rs` (split named for the size ceiling) and
  give them the same direct branch against `ScriptedFrameSource`.
- Tests: `client_loop.rs` direct-pane tests assert `sent_host_input()` and zero
  `terminal_input` websocket requests; `lease_control.rs` covers grant missing ->
  take-back status and `InputRefused` handling; `copy_paste.rs` asserts a direct paste
  reaches the host as `Paste`; `frame_source_live.rs` drives a real gterm: spawn a
  native terminal, `grant_input` over control, bind, `Input`, observe the echo in the
  next frame, `revoke_input`, observe `InputRefused`. `mock_daemon` take-control replies
  gain `host_input_granted`.
- Verification (planned): `cargo test -p gobby-client --test client_loop --test
  lease_control --test copy_paste --test frame_source_live`, `cargo clippy -p
  gobby-client --all-targets`.

Consumers unchanged:
- `crates/gclient/src/app/live_loop/actions.rs` — no-edit-reason: Both mouse write arms call send_live_write, which routes direct panes to the host internally, so the call sites stay as they are.

**Acceptance:**

- 2.1.1 - Direct native panes send `BindAttachment` once per installed source and then `Input`/`Paste` on the frame stream without awaiting any daemon reply. symbol: `send_live_write`.
- 2.1.2 - The frame source exposes a non-awaiting `send_input` with typed backpressure. symbol: `FrameSource`. file: `crates/gclient/src/frame_source.rs`.
- 2.1.3 - A granted lease whose host grant is missing shows take-back instead of typing into the daemon. symbol: `request_live_control`.
- 2.1.4 - `InputRefused` flips the pane to observe with the code in the status line and keeps the stream. symbol: `recover_live_frame_error`.
- 2.1.5 - The scripted workspace path lives in the split module and mirrors the direct branch. file: `crates/gclient/src/app/scripted_input.rs`.
- 2.1.6 - Client loop tests assert host input for direct panes and no daemon input messages. test: `crates/gclient/tests/client_loop.rs::direct_pane_keys_reach_the_host_not_the_daemon`.
- 2.1.7 - Live frame source test proves grant, input echo and revoke against a real gterm. test: `crates/gclient/tests/frame_source_live.rs::granted_direct_input_echoes_and_revoke_refuses`.

**Granularity:** seven items and eleven production files because the render-loop
change, the frame-source write side and the control-result handling form one behaviour
(a key typed into a held direct pane reaches the PTY without the daemon) that cannot be
tested in halves.

## P3: Contracts
`kind: framing`

### 3.1 Protocol contract, guides and decision memory reflect granted direct input [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `docs/contracts/gterm-protocols.md`
- `docs/guides/gterminal-development-guide.md`
- `docs/guides/gclient-user-guide.md`

Research context:

- Observed: `docs/contracts/gterm-protocols.md` Sockets table says the frame socket
  "Can write a PTY: no" and that a frame client "cannot reach the writing surface";
  Frame protocol lists no client input; Control protocol lists the verbs; Daemon
  WebSocket messages says "Neither a direct locator nor a frame grants write authority".
- Observed: `docs/guides/gterminal-development-guide.md:105` and `:270` describe the
  frame socket as frames only; `docs/guides/gclient-user-guide.md:370` and `:614` define
  `◌ read-only` as an unknown write outcome.
- Observed: memory `be35449d` item 1 says gterm stays "PTY plus frames" and gclient
  mutates through the daemon; memory `b59e4ce9` records the superseding decision.

Implementation: rewrite the Sockets row ("yes, for the attachment the daemon granted"),
add `BindAttachment`/`Input`/`Paste`/`InputRefused` to the frame protocol with the
refusal codes, `grant_input`/`revoke_input` and the `input_activity` event to the
control protocol, `host_input_granted` to `terminal_control_result`, and the grant
survival across control disconnect and daemon restart; update both guides; update
memory `be35449d` item 1 through `gobby-memory:update_memory` to point at this plan
and `b59e4ce9`.

**Acceptance:**

- 3.1.1 - The protocol contract documents granted frame input, the new verbs, the event and the result field. behavior: "granted input" in `docs/contracts/gterm-protocols.md`.
- 3.1.2 - The gterm development guide and the gclient user guide describe the direct input path and the new meaning of read-only. file: `docs/guides/gclient-user-guide.md`.
- 3.1.3 - Memory `be35449d` item 1 no longer claims gclient mutates through the daemon for input. behavior: "memory be35449d updated" in `docs/contracts/gterm-protocols.md`.

## P4: Verification
`kind: framing`

### 4.1 Live verification
`kind: verification`

After 1.1 to 3.1 land: rebuild `gterm` and `gclient` (`cargo build --release`), promote
the coherent set through `promote_workspace_binary_set` and gclient separately, verify
`sha256` from `~/.gobby/bin/`, announce a `global` `send_message`, confirm no live
spawned worker or close validator, `uv run gobby restart --wait` from the main
checkout, relaunch gclient, and have Josh type fast into a native pane while a close
validator is running. Pass: no window freeze, no `read-only` flip, keys echo at
terminal speed, `~/.gobby/logs/gclient.log` shows no `terminal_input` requests, and
`daemon.log` shows `input_activity` events consumed. Then take control from the web
terminal and confirm the gclient pane shows take-back and its next key is refused with
`input_not_granted`. This also settles #22557's live criterion.

## V1 Plan Changelog
`kind: framing`

- 2026-09-19: first narrative draft (gobby#13879), awaiting Josh's confirmation of
  decisions 1 to 7 in 0.1; no enhancement or adversarial review run yet.
