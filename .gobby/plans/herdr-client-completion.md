# Herdr Client Completion

**Plan ID:** herdr-client-completion

## Overview
`kind: framing`

`herdr-foundation-landing` (epic #21120) put the `gterm` host, the `terminals` schema,
`TerminalRuntime`, and the `gobby-client` crate skeleton on `0.5.0` and deferred
everything after the original plan's §3.3 join. On the landed tree `gclient` probes
`/api/health`, enters and leaves raw mode, and exits 0: `views::run_ready` builds
`Workspace::scripted()` and returns; `Workspace` hardcodes `ScriptedDaemon` and
`ScriptedFrameSource` as struct fields; `tokio-tungstenite` is declared and never
imported; `UnixSocketFrameSource::connect` handshakes and drops the stream; the
~12.9k-LOC (12,881 at v0.8.0; ~13.5k is the clone's HEAD, 93 commits past the tag)
herdr chrome import never happened (`src/ui/*.rs` are 11–70-line stubs); the
23 crate tests pass in 110 ms against in-`src` mocks.

This epic builds the client for real on the landed tree — the QA plan's P6 scope
(`.gobby/plans/herdr-terminal-client-qa-fixes.md`, kept as source material) plus §3.5
startup — and makes it **daemon-addressable**: `gclient --daemon-url` with a bearer
token, and a cell-mode proxy frame source so a tailnet-exposed daemon can be viewed
remotely without Stage 3 (`docs/architecture/evolution.md`). The client never carries a
VT engine: the host emulates, `FrameData::to_ratatui_buffer`
(`crates/gterminal/src/protocol/wire_types.rs`) ships Zig-free, and every backend —
native and tmux — renders through the host's frame stream. Before the client pins the
wire byte-for-byte, the WS defects found after landing on that same contract (#21191,
#21207, #21209) are fixed and the golden corpus moves to its canonical path.

## Constraints
`kind: framing`

**Decision Record (confirmed 2026-08-29).**

1. Depth: Full. This file is the artifact; enhancement and adversarial review follow
   the draft checkpoint. `.gobby/plans/herdr-terminal-client-qa-fixes.md` is superseded
   for the client and kept as source material.
2. Scope: the client (QA 6.1–6.4 rebuilt on `0.5.0`, §3.5 startup), a remote-capable
   data plane (`--daemon-url` + bearer; `terminal_attach.encoding` negotiated;
   `proxy_relay.py` relays cell frames), and only the daemon pieces the client
   consumes (P1). Native-runtime hardening (QA 2.3–2.9, 4.1, 4.2, 4.6, P8, P3, P5, the
   flip) is D1; hub-wide roster/attach routing and capability tokens are D2 (#20202,
   blocked by #19600 and #19647); plugins are D3 (#20201).
3. Hangers: #21191, #21207, #21209 fold in as P1. #21204, #21198, #21125, #21208,
   #21206 stay under #21120 / #21211.
4. Parity: port herdr's keep-set `TestBackend` render tests (4.1); no live-herdr screen
   diff — gclient-only screen goldens (4.2).
5. Keymap: herdr v0.8.0 defaults ported with the chrome; plugin-menu bindings reserved
   and hidden until #20201; worktree/mobile bindings omitted; add take-control,
   take-back, respond, quit; client-local overrides under `~/.gobby/client/`.
6. Topology: leaves commit directly on `0.5.0` behind Guard set H; a leaf that changes
   daemon code announces its restart via `gobby-agents:send_message` and waits for a
   quiet window. No shared epic worktree, no rehearsal database.
7. Closing leaf (5.1) creates the next planning epics along `evolution.md`'s chain and
   refreshes its remaining-path snapshot; typed `kind: deferred` sections carry the
   per-item deferrals created at expansion.
8. Found work filed at expansion, not folded into leaves unless named in a Targets
   block: `workspace.rs:71` tautology and the `ui_carve_guard.rs` non-blank disjuncts
   (fixed in 2.2 and 3.1), `copy_mode::extract_logical_line` ignoring `wrap_cols`
   (3.3), dead `input.rs` (3.2), `gclient --version` exiting 1 so
   `probe_native_bin_version` can never succeed (3.4), and the duplicate
   `_handle_terminal_input` in `terminal_ws.py` and `handlers/core.py` (1.1).
9. Standing rules: the binary stays `gclient` until the Stage-2 rename and the Python
   `gobby` CLI neither wraps nor execs it; `gobby-client` builds without Zig and
   depends on `gobby-terminal` with `default-features = false`; the `impeccable` skill
   is loaded before any theme or chrome edit and `.impeccable.md` is the palette
   authority; no new blanket `allow` attributes; every hand-maintained file under
   `crates/gclient/src/` and every production `.py`/`.ts` stays under 1,000 lines at
   every leaf close; `0.5.0` is unshipped, so replaced symbols are deleted, never
   aliased.

**Wire facts the plan builds on (verified on `0.5.0` at `001e243887`; re-verified at
`a446bf15cc` — the only intervening changes under the audited paths are #21214/#21215's
`TerminalRuntimeRegistry` work in `src/gobby/terminals/`, which moves no fact below).**

- `terminal_attach_result` today carries `type`, `request_id`, `attachment_id`, `rows`,
  `cols`, `backend`, `frame_delivery`, `lease_generation`, `success` and no `direct` block
  (`src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_attach`).
- `ProxyHub.start_proxy` hardcodes `handshake(locator, encoding="terminal_ansi")`; the
  host's default `RenderEncoding::SemanticFrame` maps to empty `terminal_output`
  through `_map_host_frame`. The daemon's `frame_client.py` already decodes both
  encodings (`_decode_server`).
- `_handle_terminal_list` parses cursors (`parse_list_cursor`, `encode_page`) but
  publishes no `snapshot`; `terminal_event` carries no `seq`; there is no
  `daemon_epoch`. `TerminalLeaseRegistry` (`src/gobby/terminals/leases.py`) already
  owns `next_message_seq` and is process-wide.
- `terminal_output` is emitted from `BroadcastMixin.broadcast_terminal_output` (tmux PTY
  bridge; `attachment_id` defaults to `None`) and from `proxy_relay._map_host_frame`
  (correct ids). `_handle_terminal_input` is defined twice
  (`terminal_ws.py:484`, `handlers/core.py:285`); `server.py:361` binds the name
  unqualified.
- The golden corpus is 31 files at `tests/servers/fixtures/terminal_ws_golden/` with no
  `manifest.json`; `test_python_matches_terminal_ws_golden_corpus` proves only
  `encode(decode(raw)) == raw`. `crates/gclient/tests/ws_golden.rs` and
  `crates/gclient/src/daemon/ws.rs::GOLDEN_NAMES` replay the same 31 by path.
- `TmuxTerminalRuntime.attach_locator` returns the host-observer shape
  (`frame_host_epoch`, `socket_path`, `pane_id`, `server_pid`, `server_start_time`);
  `NativeTerminalRuntime.reserve_observer` and `HostClient.reserve_observer` exist; the
  running host is started with the config-driven `--tmux-poll-interval-ms` (default 150,
  `src/gobby/config/terminal_host.py`). The Python
  `AttachLocator` (`src/gobby/storage/terminals.py`) carries `host_socket` for both
  backends.
- `machines` records `id`, `hostname`, `os`, `tailscale_name`, `label`, `owner_user_id`,
  `first_seen`, `last_seen` and no daemon endpoint or cross-machine credential; `hub_daemon_url` in
  bootstrap is node→hub only. Remote attach in this epic therefore targets a daemon the
  operator has bound to its tailnet address, with that machine's `local_cli_token`.

**Guard set H.** Every leaf's close gate runs from the `0.5.0` checkout with
`DATABASE_URL` pointed at the isolated test hub
(`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
`GOBBY_TEST_PROTECT=1`:

1. `cargo build --release -p gobby-client && cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings && cargo nextest run -p gobby-client`
2. `cargo nextest run -p gobby-terminal` (the embed suite's `gclient_views` source
   assertion and the host contract stay green)
3. `uv run pytest tests/terminals tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_native_web_proxy.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_attention_respond.py tests/servers/websocket/test_broadcast.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/storage/test_terminals.py` (DB-backed; `GOBBY_POSTGRES_TEST_DSN` exported)
4. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/ && uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
5. `cd web && npx vitest run src/hooks src/components/activity`
6. From 4.3 close onward: `uv run pytest tests/e2e/test_terminal_client_stack.py`
   against `gclient` and `gterm` rebuilt from the tree and installed via new inode
   (`cp` to a dotfile, `mv -f` over the name, per
   `docs/guides/gterminal-development-guide.md` § "Rebuild and reinstall").
7. Host leak check: the set of `gterm host` PIDs after groups 2, 3, and 6 equals the
   set before.

Mirror this block into `docs/guides/gterminal-development-guide.md` under a "Guard set
H" heading in 4.4 so later leaves execute against the checked-in copy.

**Named defaults.** Proxy cell-frame message `terminal_frame` with
`encoding: "bincode-b64"`; `terminal_attach.encoding` ∈ {`semantic_frame`,
`terminal_ansi`}, default `terminal_ansi` (the browser's value, so the web hook needs no
change); `--daemon-url` overrides `gobby-core` bootstrap discovery, `--token-file`
defaults to `~/.gobby/local_cli_token`; `LiveDaemon` subscriber channel 256 entries,
lifecycle replay buffer 1,024, request deadline 5 s, control-request deadline 2 s,
`Detaching` deadline 2 s, reconnect budget 5 attempts with 250 ms → 4 s doubling
backoff; direct frame receive channel 256 entries, overflow closes the source with typed
`FrameError::Lag` (the crate's existing variant); input channel 256, render tick 16 ms; workspace snapshot
`~/.gobby/client/<project_id>/workspace.json`; log `~/.gobby/logs/gclient.log`
(tracing, daily rotation, never stdout); screen-golden geometry 120×40; e2e PTY
geometry 120×40.

**Consumer sweep (2026-08-29, `uv run gobby plans validate`).** Exact-symbol Targets
whose remaining consumers are unchanged by design: `TerminalRuntime` /
`TmuxTerminalRuntime` / `NativeTerminalRuntime` gain a verb, so callers of the existing
verbs (`agents/capture.py`, `agents/spawn_executor*.py`, `agents/tmux/pane_monitor.py`,
`agents/lifecycle_monitor.py`, `mcp_proxy/tools/agents_query_tools.py`,
`mcp_proxy/tools/agents_termination.py`, `runner_lifecycle_reconcile.py`,
`runner_init/orchestration.py`, `terminals/__init__.py`, `terminals/services.py`,
`terminals/web_spawn.py`, and their tests) do not change; `AttachLocator` gains a method,
so its constructors and readers (`agents/spawn_models.py`, `agents/tmux/spawner.py`,
`runner_init/servers.py`, the roster and e2e tests) do not change;
`TerminalLeaseRegistry` gains two members, so `tmux_activation.py` and the lease/viewport
tests do not change; `FrameClient.read_message` keeps its signature, so its consumers —
`ProxyHub._pump` (`proxy_relay.py`), the `connect` handshake, and the three test files
that drive it (`test_external_terminal_attach.py`, `test_frame_client.py`,
`test_runtime_contract.py`) — do not change. A leaf
that finds one of these consumers changing adds it to its own Targets before close.

**Source-plan provenance.** Acceptance in this plan re-satisfies the original plan's
3.3.1–3.3.22, 3.5.1–3.5.3, and the client half of 3.4 (`herdr-terminal-client.md`), and
QA 6.1–6.4, 7.1 (`herdr-terminal-client-qa-fixes.md`); QA 7.2 is intentionally not
carried (Decision 4).

## P1: Wire hygiene
`kind: framing`

**Goal**: the WebSocket terminal contract gclient will pin byte-for-byte is honest,
carries the ids and watermarks a client needs, can deliver cell frames, and lives in one
canonical golden corpus that real emitters are compared against.

### 1.1 Deliver `terminal_input` bytes as key codes through a `write_input` runtime verb [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/runtime.py::*` — scope-reason: the runtime protocol gains the write_input verb, so the module's contract changes rather than one member; existing verbs and their callers are untouched
- `src/gobby/terminals/tmux_runtime.py::*` — scope-reason: the tmux implementation gains the write_input body (tmux send-keys -H, 512 B chunks) alongside its existing verbs
- `src/gobby/terminals/native_runtime.py::*` — scope-reason: the native implementation gains the write_input body (host kind=input) alongside its existing verbs
- `src/gobby/terminals/write_coordinator.py::WriteCoordinator._dispatch`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._deliver_operator_write`
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._deliver_operator_write`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_input`
- `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_terminal_input`
- `src/gobby/servers/websocket/server.py::*` — scope-reason: the dispatch table binds the single surviving `_handle_terminal_input`
- `tests/terminals/fakes.py::*` — scope-reason: every fake runtime implements `write_input`
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: the cross-backend contract suite gains `write_input` cases
- `tests/terminals/test_tmux_runtime.py::*` — scope-reason: `send-keys -H` chunking assertions
- `tests/terminals/test_native_runtime.py::*` — scope-reason: host `kind="input"` write assertions
- `tests/terminals/test_write_input.py`
- `tests/servers/test_terminal_ws_input.py`
- `tests/servers/test_native_web_proxy.py::*` — scope-reason: Guard-set-H RecordingRuntime gains write_input and existing terminal_input assertions follow the raw-input route

Task #21191, verified live on the landed daemon: a browser `terminal_input` of raw
keystroke bytes (`\r`, `\x03`, `\x04`, `\x7f`, `\x1b[A`) reaches
`TmuxTerminalRuntime.write_text(row, payload, submit=False)` →
`paste_literal_text_to_tmux_target` (`tmux set-buffer` + `paste-buffer -p`), and tmux
3.7c pastes control bytes as caret text (`^D`; cat never sees EOF; `\x03` does not
interrupt). `write_key` delivers named keys via `send_named_key_to_tmux_target` and uses `tmux
send-keys -t <target> -H <hex>` only as the cursor/keypad fall-through.

Add `async def write_input(self, terminal: Terminal, data: bytes) -> WriteOutcome` to
the `TerminalRuntime` protocol and both runtimes:

- tmux: `send-keys -t <target> -H` over the UTF-8 bytes, chunked at 512 bytes per
  invocation (tmux's argv is bounded and `-H` takes one hex token per byte);
  `InputPayloadTooLargeError` above 64 KiB. `write_key`'s outcome mapping is a
  *one-shot* mapping and does not carry over unchanged: a chunk loop is a multi-step
  external write with three distinguishable outcomes, and collapsing them permits a
  caller to resend bytes the pane already received. Check the return code of every
  invocation and track whether any chunk landed. Zero chunks delivered plus a
  deterministic failure is the ordinary failed outcome and maps exactly as `write_key`
  does. A deterministic failure after at least one chunk landed raises
  `TerminalWriteError(stage="partial")` carrying the byte count known to have been
  delivered. A *backend* timeout at any point is *indeterminate* — the prefix may
  or may not have reached the pane — and also raises `stage="partial"`, with the
  delivered count reported as unknown. Cancellation of the connection handler's own task
  is a different event and is never converted into `TerminalWriteError`: `CancelledError`
  propagates out of `write_input` unchanged, because swallowing it would leave a task
  that refuses to cancel. No caller retries a `partial` outcome automatically.
- native: raw bytes through the existing host `write` control verb by way of
  `NativeTerminalRuntime._write` with a new `kind="input"` payload (`"encoding":
  "utf8-b64"` as the host client already uses); the host writes the bytes to the PTY
  unchanged.

Route `kind="input"` operator writes (`_handle_operator_write` → `_deliver_operator_write`)
and `WriteRequest.kind == "input"` in `WriteCoordinator._dispatch` through `write_input`
(`WriteRequest.kind` is `Literal["text", "key", "paste"]` today and gains `"input"`).
Operator writes have a second raw path today that this routing must cover:
`TmuxMixin._deliver_operator_write` (`servers/websocket/tmux.py`) writes bytes straight
to the bridge PTY fd whenever `get_master_fd` yields one, and `WriteCoordinator` is only
its no-runtime fallback (`terminal_ws.py`) — the PTY fast path either already delivers
key-code bytes verbatim or routes through `write_input` with the rest.
`terminal_paste` stays on `write_paste` (bracketed paste-buffer path); `send_keys`,
attention answers, and every `submit=True` text write stay on `write_text`. `_encode_key`
is unchanged.

Both `partial` shapes reach the browser and gclient as the wire outcome the protocol
already defines: `terminal_write_outcome` with `outcome: "indeterminate"`. There is no
new outcome value, no new golden fixture family, and no new error shape on the wire —
`write_outcome_indeterminate.json` in `src/gobby/terminals/ws_protocol.py` is the
existing fixture and `crates/gclient/src/app/apply.rs` already reduces that value. The
existing `reason` string carries the distinction the operator needs:
`indeterminate_partial_delivered:<n>` when the byte count that landed is known, and the
existing `indeterminate_backend` when the delivered prefix is unknown.
`TerminalWriteError` is caught inside `_deliver_operator_write` alongside
`ConnectionError` and `OSError` and translated there; it never escapes to the
connection handler.

Complete the write ledger on every exit path. `_handle_terminal_input` today calls
`TerminalLeaseRegistry.complete_write(attachment_id, seq, outcome, reason)` only after
`_deliver_operator_write` returns normally, so any raised exception — including a
`CancelledError` from a client disconnect mid-chunk — leaves that `client_write_seq`
permanently in flight and every later write on the attachment is refused with
`write_seq_conflict`. Wrap the delivery in `try/finally` so `complete_write` runs on
every exit.

The two failure sources end differently on the wire, and conflating them is what makes
the contract unsatisfiable. A **backend** failure — deterministic, partial, or timeout —
happens while the client's WebSocket is still usable, so the handler completes the ledger
and answers `terminal_write_outcome` normally; that is the only branch that produces a
reply. **Handler-task cancellation** means the connection itself is going away, so there
is nothing left to answer: the ledger entry closes in `finally` and `CancelledError`
re-raises unchanged, with no `terminal_write_outcome` sent and none expected. A branch
cannot both re-raise cancellation and promise a reply on the socket that is disappearing,
so the two are specified and tested independently.

gclient's side is unchanged in kind: `indeterminate` leaves the pane **uncertain
read-only** until the operator clears it, and the client never resends the payload. An
`indeterminate` outcome is not a retry signal on either side of the wire.

Resolve the duplicate handler: `TerminalWsMixin._handle_terminal_input` (backend-neutral,
attachment-scoped) is the only definition; delete `HandlerMixin._handle_terminal_input`
from `handlers/core.py` together with its `run_id`-keyed legacy branch, and make the
`server.py` dispatch entry name the surviving method explicitly so MRO no longer decides.
The `2367e0fb25` guarantee — a tmux id is never parsed as an agent uuid — moves with the
handler and keeps its test.

**Acceptance:**

- 1.1.1 - `TerminalRuntime` declares `write_input(terminal, data: bytes)` and both runtimes implement it: tmux invokes `send-keys -H` with the hex of the UTF-8 bytes in ≤512-byte chunks, native sends a `kind="input"` host write; a payload over 64 KiB raises `InputPayloadTooLargeError` before any subprocess or socket write. test: `tests/terminals/test_write_input.py::test_write_input_uses_send_keys_hex_and_host_input`.
- 1.1.2 - A `terminal_input` of `\x04` to a tmux terminal running `cat > f` ends cat, `\x03` interrupts a running `sleep`, and `\x1b[A` reaches the pane as the bytes `1b 5b 41`; `terminal_paste` still uses `paste-buffer` and a 20 KB paste arrives byte-identical. test: `tests/servers/test_terminal_ws_input.py::test_input_bytes_are_key_codes_and_paste_stays_bracketed`.
- 1.1.3 - `WriteRequest(kind="input")` dispatches to `write_input`, `kind="paste"` to `write_paste`, and text with `submit=True` to `write_text`. test: `tests/terminals/test_write_input.py::test_coordinator_routes_by_kind`.
- 1.1.4 - Exactly one `_handle_terminal_input` exists under `src/gobby/servers/websocket/`, the dispatch table binds it by qualified name, and a `terminal_input` naming a tmux id is never parsed as an agent uuid. test: `tests/servers/test_terminal_ws_input.py::test_single_input_handler_is_bound_and_backend_neutral`.
- 1.1.5 - In a chunked tmux `write_input`, a deterministic failure on the first invocation yields the ordinary failed outcome with no bytes delivered, a deterministic failure on a middle or final invocation raises `TerminalWriteError(stage="partial")` reporting the bytes known to have landed, and a backend timeout at any invocation raises `stage="partial"` with an unknown delivered count — handler-task cancellation is not this branch, because `write_input` propagates `CancelledError` unchanged instead of converting it to a `TerminalWriteError`, and 1.1.7 owns that outcome; no code path resends a payload after any `partial` outcome. test: `tests/terminals/test_write_input.py::test_chunked_write_classifies_partial_delivery`.
- 1.1.6 - Over the real WebSocket path, with the connection still usable, a chunked `terminal_input` whose middle invocation fails deterministically returns `terminal_write_outcome` with `outcome="indeterminate"` and `reason="indeterminate_partial_delivered:<n>"`, and a backend timeout mid-chunk returns `outcome="indeterminate"` with `reason="indeterminate_backend"`; no new outcome value appears on the wire. test: `tests/servers/test_terminal_ws_input.py::test_partial_write_reports_indeterminate_on_the_wire`.
- 1.1.7 - Every `write_input` failure, backend timeout, and handler-task cancellation completes the `client_write_seq` in the lease ledger: after each of those outcomes a replay of the same seq with a different payload fingerprint is refused with `write_seq_conflict`, a same-fingerprint replay is served the completed entry's recorded outcome rather than joining a dead in-flight entry, and the next seq is admitted and delivered, so no attachment is left permanently unwritable. The cancellation branch is asserted separately — the ledger entry closes, `CancelledError` propagates out of the handler, and no `terminal_write_outcome` is written to the socket. test: `tests/servers/test_terminal_ws_input.py::test_failed_write_completes_ledger_and_admits_next_seq`, `tests/servers/test_terminal_ws_input.py::test_disconnect_cancellation_closes_ledger_without_replying`.

### 1.2 Make `terminal_attach` honest and put the right ids on `terminal_output` [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._start_proxy_attach`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_attach`
- `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast_terminal_output`
- `src/gobby/runner_broadcasting.py::broadcast_terminal_output`
- `src/gobby/agents/tmux/pty_bridge.py::*` — scope-reason: the tmux output emitter passes both the row id and the attachment id
- `web/src/hooks/useTmuxSessions.ts::*` — scope-reason: output is routed by `attachment_id` alone; the `|| terminal_id` fallback is deleted
- `tests/servers/test_terminal_ws_attach_honesty.py`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyHub.start_proxy`
- `tests/servers/websocket/test_broadcast.py::*` — scope-reason: `broadcast_terminal_output` assertions carry both ids
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: routing assertions for the fixed `terminal_output` shape
- `src/gobby/servers/websocket/tmux.py::TmuxMixin.broadcast_terminal_output`
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: migrate its terminal attach/output WebSocket mock to attachment_id routing
- `web/tests/terminal-colors.spec.ts::*` — scope-reason: migrate its terminal attach/output WebSocket mock to attachment_id routing

Tasks #21207 and #21209. `_start_proxy_attach` has four early returns covering five
failure conditions (no runtime for the backend, no `open_proxy_frame`,
`open_proxy_frame` itself raising, `attach_locator` raising, a non-`AttachLocator`
result); two log nothing and `_handle_terminal_attach` replies `success: true`
regardless, so a proxy client waits forever for frames. Make `_start_proxy_attach`
return `str | None` — a typed code on failure (`runtime_unavailable`,
`proxy_unavailable`, `locator_failed`, `locator_invalid`, and `host_unavailable` when
the frame handshake itself fails) — log each at warning level with the terminal id and
reason, and have `_handle_terminal_attach` answer `{"success": false, "code": <code>,
"reason": <text>}` and finalize the just-registered attachment (`registry.finalize`)
instead of leaving a ghost record. Lease loss never finalizes an attachment; attach
failure always does.

**Failing the attach is not enough — the attempt has to unwind.** `_start_proxy_attach`
opens the frame connection itself (`open_proxy_frame`) and only then calls
`ProxyHub.start_proxy`, which performs `handshake` and `attach_terminal` *before* it
registers the record in `attachments`/`by_socket` and starts the pump. Every failure
between those two points — and `host_unavailable` is exactly that failure — leaves an
open host connection and a reserved observer that no map references, so
`registry.finalize` cannot reach it: it removes the lease record and nothing else, and
the socket survives for the life of the process. Make proxy startup transactional
instead. Once `open_proxy_frame` returns, that connection is owned by the attempt: a
handshake failure, an `attach_terminal` failure, or a failure while installing the
record closes the frame and removes whatever partial map entry or pump task was already
installed, in reverse order, before the typed code is returned. `start_proxy` unwinds
its own partial registration and `_start_proxy_attach` closes the frame it opened. Both
paths are idempotent, and a close that itself raises is logged without masking the
original attach code.

`broadcast_terminal_output` today defaults `attachment_id=None` and the tmux bridge
passes the attachment id as `terminal_id`; `TmuxMixin.broadcast_terminal_output` is a
`TYPE_CHECKING` stub declaration with no body, so its signature only tracks the new
required parameter. Make `attachment_id: str` required, pass the
terminals-row id as `terminal_id`, and update the two callers
(`runner_broadcasting.broadcast_terminal_output` — its `run_id` parameter becomes
`terminal_id` and the caller resolves the row — and the PTY bridge's reader). The web
hook keys output on `attachment_id` only.

**Acceptance:**

- 1.2.1 - Each early-return branch of `_start_proxy_attach` produces a `terminal_attach_result` with `success: false`, a distinct `code`, a `reason`, a warning-level log line, and no live attachment record. With a failure injected at each boundary after the frame opens in turn — during `handshake`, during `attach_terminal`, and while installing the attachment record — the reply carries the same typed code and the attempt leaves no lease record, no entry in `attachments` or `by_socket`, no pump task, and no open frame connection; the fake host observes the connection closed. test: `tests/servers/test_terminal_ws_attach_honesty.py::test_proxy_attach_failures_are_typed_and_finalized`, `tests/servers/test_terminal_ws_attach_honesty.py::test_proxy_attach_unwinds_every_acquisition_on_failure`.
- 1.2.2 - `terminal_output` frames from the tmux bridge and the proxy relay both carry the terminals-row id in `terminal_id` and the attachment id in `attachment_id`, matching `terminal_attach_history` on the same stream. test: `tests/servers/websocket/test_broadcast.py::test_terminal_output_carries_both_ids`.
- 1.2.3 - The web hook routes output by `attachment_id` and a frame carrying only a terminal-row id is not delivered to any view. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts`.

### 1.3 Negotiate the frame encoding on `terminal_attach`, add the `direct` block, and relay cell frames [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_attach`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyAttachment`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyHub.start_proxy`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyHub._pump`
- `src/gobby/servers/websocket/proxy_relay.py::_map_host_frame`
- `src/gobby/terminals/frame_client.py::_decode_server`
- `src/gobby/terminals/frame_client.py::decode_frame`
- `src/gobby/terminals/frame_client.py::FrameClient.read_message`
- `tests/e2e/test_external_terminal_attach.py::*` — scope-reason: the external-attach e2e drives read_message end to end and covers the typed attach codes and finalize this leaf changes
- `src/gobby/storage/terminals.py::*` — scope-reason: AttachLocator gains direct_block() and the module's locator contract changes with it; existing constructors and their callers are untouched
- `tests/servers/test_native_web_proxy.py::*` — scope-reason: encoding negotiation, cell-frame relay, and `direct` block tests against the fake host
- `tests/terminals/test_frame_client.py::*` — scope-reason: decoder tests cover `raw` retention on semantic frames
- `tests/terminals/test_frame_client_semantic.py`
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._handle_terminal_attach`

Two additions to the attach handshake, both consumed by P2:

1. **Encoding.** `terminal_attach` accepts `encoding` ∈ {`terminal_ansi`,
   `semantic_frame`}, default `terminal_ansi`; any other value is `terminal_error`
   `invalid_encoding`. The value is stored on `ProxyAttachment.encoding` and
   `start_proxy` passes it to `handshake(locator, encoding=...)` instead of the
   hardcoded string. For `semantic_frame`, `_decode_server` keeps the raw bincode
   payload of every `frame`/`terminal` message under `"raw": bytes` alongside the
   decoded fields (attach-history and scroll-applied messages are unchanged), and
   `_map_host_frame` emits `{"type": "terminal_frame", "terminal_id", "attachment_id",
   "encoding": "bincode-b64", "payload": base64(raw)}` instead of `terminal_output`.
   The payload rides the existing `emit_event` → `fragment_event` path
   (`terminal_ws_fragment` with `message_seq`), so no new fragmentation code is
   written. The client decodes the payload with `gobby_terminal::protocol`'s
   `ServerMessage` codec — the same bytes the host wrote. `terminal_ansi` behaviour is
   byte-identical to today.
2. **`direct` block.** `terminal_attach_result` gains `direct`: `null` for proxy
   attachments; for `frame_delivery: "direct"` an object
   `{"host_epoch": locator.frame_host_epoch, "frame_socket_path": locator.host_socket,
   "host_terminal_id": locator.host_terminal_id, "pane": {"socket_path", "pane_id",
   "server_pid", "server_start_time"} | null}` produced by a new
   `AttachLocator.direct_block()` method — `pane` is populated for tmux rows (the host
   observer's `PaneLocator`) and `null` for native rows. The handler calls
   `runtime.attach_locator(row)` for direct attachments too (today only the proxy path
   does) and answers `success: false, code: "locator_failed"` on failure, reusing 1.2's
   path.

**Acceptance:**

- 1.3.1 - A proxy attach with `encoding: "semantic_frame"` handshakes the host with that encoding and relays each host frame as a `terminal_frame` whose base64 payload decodes to the exact bincode bytes the fake host wrote; `terminal_ansi` and an omitted `encoding` still yield `terminal_output` byte-identical to the pre-change fixture; an unknown encoding is refused `invalid_encoding`. test: `tests/servers/test_native_web_proxy.py::test_semantic_frame_proxy_relays_bincode_payloads`.
- 1.3.2 - `_decode_server` retains `raw` for semantic frames and `decode_frame` round-trips a recorded semantic keyframe from `crates/gterminal/tests/fixtures/wire_golden/`. test: `tests/terminals/test_frame_client_semantic.py::test_semantic_frame_raw_is_retained`.
- 1.3.3 - A direct attach on a native row answers a `direct` block with the host epoch, frame socket path, and host terminal id; a direct attach on a tmux row additionally carries the pane locator; a proxy attach answers `direct: null`; a locator failure answers `success: false, code: "locator_failed"`. test: `tests/servers/test_native_web_proxy.py::test_direct_attach_result_carries_locator`.
- 1.3.4 - A tmux proxy attach with `encoding: "semantic_frame"` delegates through the host/ProxyHub path, emits `terminal_frame`, and returns `direct: null`, while omitted or `terminal_ansi` encoding retains the legacy bridge. test: `tests/servers/test_native_web_proxy.py::test_tmux_semantic_proxy_uses_host_frames`.

### 1.4 Publish a lifecycle watermark: `daemon_epoch`, `seq` on lifecycle events, and `snapshot` on the first list page [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: the registry gains daemon_epoch and next_lifecycle_seq, so the module's lifecycle-watermark contract changes; existing lease verbs and their callers are untouched
- `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast_tmux_session_event`
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._broadcast_tmux_event`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_list`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._fanout_lease_lost`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyHub.emit_lifecycle`
- `src/gobby/servers/routes/terminals.py::list_terminals`
- `src/gobby/terminals/ws_protocol.py::encode_page`
- `tests/servers/websocket/test_broadcast.py::*` — scope-reason: `broadcast_tmux_session_event` assertions carry `seq` and `daemon_epoch`
- `tests/terminals/test_lease_authority.py::*` — scope-reason: registry epoch and lifecycle-seq tests
- `tests/servers/test_terminal_list_watermark.py`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_detach`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_take_control`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_create`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_kill`
- `src/gobby/servers/websocket/proxy_relay.py::_Queued`
- `src/gobby/servers/websocket/proxy_relay.py::SocketRelay`

gclient's subscribe-first reconciliation (2.1) needs to know which buffered lifecycle
events predate the roster it just fetched. Give `TerminalLeaseRegistry` — already
process-wide and the owner of `next_message_seq` — a `daemon_epoch: str` (uuid4 minted
in `__init__`) and `next_lifecycle_seq() -> int` guarded by the same lock the
broadcaster uses. Every lifecycle emission stamps `seq` and `daemon_epoch`:
`terminal_event` (`broadcast_tmux_session_event`; the legacy emitter in
`servers/websocket/tmux.py` calls it instead of building its own dict),
`terminal_lease_lost` (`_fanout_lease_lost`), and `terminal_attachment_finalized`
(`emit_lifecycle`). `_handle_terminal_list` and REST `list_terminals` take the current
`seq` under that lock before querying the first page and add
`"snapshot": {"daemon_epoch": ..., "seq": ...}` through `encode_page`; continuation
pages carry `snapshot: null`. Cursor parsing is unchanged (it already terminates).

Allocating `seq` under a lock is not sufficient, because it orders *allocation* and the
client observes *publication*. The three emitters are independent coroutines: each can
take its number and then await a fanout that completes after a later-numbered emission
already reached the socket, so a strictly increasing counter still delivers `4, 3` on
the wire and the client's reducer keeps the state written by the older event. Order the
publication itself. `TerminalLeaseRegistry` owns a single process-wide ordered lifecycle
path — one queue whose sole consumer allocates the number and performs the fanout in the
same critical section, so a lifecycle emission is numbered and published as one
indivisible step and no emitter can interleave between the two.

That queue is bounded, because a consumer that awaits each fanout is a consumer one stalled
socket can hold. It carries a fixed entry ceiling, and a submitter arriving at a full queue
waits for space rather than having its event dropped — backpressure preserves the ordering
the path exists for, where dropping would silently break it. Per entry the consumer reuses
the send deadlines the relay already enforces (`TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S`, whose
expiry shuts that relay down), so one wedged client cannot stall the path indefinitely.
Admission is the ownership boundary: once an entry is on the queue the sole consumer owns it
to completion and advances the committed high-water for it even if the submitter is
cancelled meanwhile — cancellation abandons only that submitter's wait on its own
completion, never the publication, since an admitted event the consumer abandoned would
stall every later sequence behind it. A submitter cancelled *before* admission contributes
no entry at all. All three call sites
(`broadcast_tmux_session_event`, `_fanout_lease_lost`, `emit_lifecycle`) submit through
that path rather than stamping and broadcasting themselves. The snapshot `seq` that
`_handle_terminal_list` and REST `list_terminals` publish is that path's *committed*
high-water — the highest sequence whose fanout has finished — never the allocation
counter, so a roster can never claim to include an event still in flight. The client
half of the invariant lives in 2.1, where `Workspace` — the side that applies events —
tracks the highest `seq` it has applied and discards any buffered lifecycle event at or
below it during replay, which makes the ordered path's guarantee survive reconnect
buffering.

**Acceptance:**

- 1.4.1 - The first `terminal_list` page (WS and REST) carries `snapshot.daemon_epoch` and `snapshot.seq`; continuation pages carry `snapshot: null`; a `terminal_event` emitted after the snapshot was taken has `seq` greater than the snapshot's, one emitted before has `seq` ≤ it. test: `tests/servers/test_terminal_list_watermark.py::test_snapshot_orders_lifecycle_events`.
- 1.4.2 - `terminal_event`, `terminal_lease_lost`, and `terminal_attachment_finalized` all carry `seq` and `daemon_epoch`, `seq` is strictly increasing across the three emitters, and the legacy tmux emitter produces the same shape. test: `tests/servers/test_terminal_list_watermark.py::test_every_lifecycle_emitter_is_stamped`.
- 1.4.3 - With all three emitters firing concurrently and a forced yield injected between sequence allocation and fanout in each, the bytes observed on a subscribed socket arrive in strictly increasing `seq` order and the resulting reducer state matches serial emission of the same events; the snapshot `seq` returned by a `terminal_list` interleaved with that traffic never exceeds the highest sequence whose fanout has completed. test: `tests/servers/test_terminal_list_watermark.py::test_publication_order_matches_sequence_under_forced_yields`.
- 1.4.4 - Direct detach finalization and the unregistered-requester takeover fallback both publish stamped lifecycle events through the ordered registry path, preserving strictly increasing observed seq. test: `tests/servers/test_terminal_list_watermark.py::test_direct_lifecycle_fallbacks_are_ordered`.
- 1.4.5 - A successful `terminal_create` publishes a stamped `terminal_event` carrying the full created row, resolved through the same by-id read the REST route uses, after the `terminal_create_result` is sent; a successful `terminal_kill` publishes a stamped exit event naming the terminal id. Both are stamped and ordered like every other lifecycle emission, a refused create or kill publishes nothing, and a duplicate kill of an already-exited terminal publishes at most one exit event. test: `tests/servers/test_terminal_list_watermark.py::test_create_and_kill_publish_ordered_lifecycle_events`.
- 1.4.6 - With the proxy relay sender paused after enqueue, committed lifecycle high-water does not advance and no later direct lifecycle event is observed first; releasing the sender publishes both in strictly increasing sequence order. With the queue driven to its entry ceiling the next submitter waits for space rather than dropping its event, the drain stays in strictly increasing sequence order, and an event whose submitter is cancelled after admission is still published and still advances the committed high-water. test: `tests/servers/test_terminal_list_watermark.py::test_proxy_relay_ack_precedes_committed_high_water`.

### 1.5 Move the golden corpus to `tests/fixtures/terminal_ws_golden/` and compare real emitters [category: test] (depends: 1.1, 1.2, 1.3, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/terminals/ws_protocol.py::golden_fixtures`
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: rewritten to drive the real handlers and compare replies byte-for-byte
- `crates/gclient/src/daemon/ws.rs::*` — scope-reason: `GOLDEN_NAMES` and the fixture root move to the canonical path and gain the new shapes
- `crates/gclient/tests/ws_golden.rs::*` — scope-reason: loads the canonical corpus by path
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: the vitest hook suite loads the same JSON files
- `tests/fixtures/terminal_ws_golden/manifest.json`
- `tests/fixtures/terminal_ws_golden/attach_semantic.json`
- `tests/fixtures/terminal_ws_golden/attach_result_direct.json`
- `tests/fixtures/terminal_ws_golden/terminal_frame.json`
- `tests/fixtures/terminal_ws_golden/create_result_refused.json`
- `tests/fixtures/terminal_ws_golden/list_snapshot.json`
- `tests/fixtures/terminal_ws_golden/kill_result.json`
- `tests/fixtures/terminal_ws_golden/detach_result.json`
- `tests/fixtures/terminal_ws_golden/attach.json`
- `tests/fixtures/terminal_ws_golden/attach_history.json`
- `tests/fixtures/terminal_ws_golden/attach_result.json`
- `tests/fixtures/terminal_ws_golden/attach_result_error.json`
- `tests/fixtures/terminal_ws_golden/attachment_finalized.json`
- `tests/fixtures/terminal_ws_golden/control_result.json`
- `tests/fixtures/terminal_ws_golden/create.json`
- `tests/fixtures/terminal_ws_golden/create_result.json`
- `tests/fixtures/terminal_ws_golden/detach.json`
- `tests/fixtures/terminal_ws_golden/event.json`
- `tests/fixtures/terminal_ws_golden/fragment.json`
- `tests/fixtures/terminal_ws_golden/fragment_last.json`
- `tests/fixtures/terminal_ws_golden/input.json`
- `tests/fixtures/terminal_ws_golden/kill.json`
- `tests/fixtures/terminal_ws_golden/lease_lost.json`
- `tests/fixtures/terminal_ws_golden/list.json`
- `tests/fixtures/terminal_ws_golden/output.json`
- `tests/fixtures/terminal_ws_golden/paste.json`
- `tests/fixtures/terminal_ws_golden/release_control.json`
- `tests/fixtures/terminal_ws_golden/resize.json`
- `tests/fixtures/terminal_ws_golden/scroll_offset_applied.json`
- `tests/fixtures/terminal_ws_golden/set_scroll_offset.json`
- `tests/fixtures/terminal_ws_golden/set_viewport.json`
- `tests/fixtures/terminal_ws_golden/take_control.json`
- `tests/fixtures/terminal_ws_golden/typed_error.json`
- `tests/fixtures/terminal_ws_golden/write_outcome.json`
- `tests/fixtures/terminal_ws_golden/write_outcome_capacity.json`
- `tests/fixtures/terminal_ws_golden/write_outcome_conflict.json`
- `tests/fixtures/terminal_ws_golden/write_outcome_expired.json`
- `tests/fixtures/terminal_ws_golden/write_outcome_indeterminate.json`
- `tests/fixtures/terminal_ws_golden/write_outcome_refused.json`
- `tests/servers/fixtures/terminal_ws_golden/attach.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/attach_history.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/attach_result.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/attach_result_error.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/attachment_finalized.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/control_result.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/create.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/create_result.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/detach.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/event.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/fragment.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/fragment_last.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/input.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/kill.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/lease_lost.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/list.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/output.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/paste.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/release_control.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/resize.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/scroll_offset_applied.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/set_scroll_offset.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/set_viewport.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/take_control.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/typed_error.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/write_outcome.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/write_outcome_capacity.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/write_outcome_conflict.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/write_outcome_expired.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/write_outcome_indeterminate.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited
- `tests/servers/fixtures/terminal_ws_golden/write_outcome_refused.json::*` — scope-reason: deleted whole by the corpus move to the canonical directory; the file is relocated, not edited

Move the 31 committed files from `tests/servers/fixtures/terminal_ws_golden/` to
`tests/fixtures/terminal_ws_golden/` (the path QA 4.5 and both clients name), leaving
the source directory with no files at all, add `manifest.json` listing every fixture
name, and add the shapes P1 introduced:
`attach_semantic` (`terminal_attach` with `encoding`), `attach_result_direct` (native
`direct` block), `terminal_frame`, `create_result_refused` (`success: false` with the
`reason` #21198 added, produced by driving the real `_handle_terminal_create` with a
runtime that refuses), `list_snapshot` (first page with `snapshot`), `kill_result` and `detach_result`
(`terminal_kill_result` / `terminal_detach_result` — both replies 2.1 correlates on,
neither pinned by any fixture today), and the existing
`attach_result_error` regenerated with 1.2's `reason`. `terminal_output`, `event`,
`lease_lost`, and `attachment_finalized` are regenerated with 1.2's ids and 1.4's
`seq`/`daemon_epoch` (the epoch value is the literal `"00000000-0000-4000-8000-000000000000"`
in fixtures, injected through the registry's constructor).

Delete `golden_fixtures()` from production `ws_protocol.py`; the corpus is the committed
JSON. `test_terminal_ws_golden.py` keeps the round-trip test and adds
`test_emitters_match_golden_replies`: for each request fixture, drive the real mixin
handler (fake manager, fake runtime, fake registry with the fixed epoch) and assert the
canonical JSON of the reply equals the reply fixture byte-for-byte; for each
server-initiated shape, call the real emitter (`broadcast_terminal_output`,
`broadcast_tmux_session_event`, `_fanout_lease_lost`, `emit_lifecycle`,
`_map_host_frame` + `fragment_event`) and compare the same way. `ws_golden.rs` and the
vitest suite load the manifest and replay every entry.

`manifest.json` is inventory **metadata about** the corpus, not a member of it. The
distinction has to be explicit because both replayers decode every manifest entry as a
WebSocket message, and `manifest.json` is not one — a corpus that lists itself cannot
satisfy replay. The arithmetic is therefore: the manifest holds **38 entries** (the 31
moved fixtures plus the seven new shapes), the directory holds **39 files** (those 38
plus `manifest.json`), and the parity check compares the directory *minus*
`manifest.json` against the manifest's entry list in both directions. `manifest.json`
itself is validated as metadata — well-formed JSON with the expected key set — never
replayed.

**Acceptance:**

- 1.5.1 - `tests/fixtures/terminal_ws_golden/manifest.json` lists exactly 38 fixture entries (the 31 moved fixtures plus the seven new shapes); the directory holds exactly 39 files, and the directory's contents minus `manifest.json` equal the manifest's entry list in both directions; `manifest.json` parses as well-formed JSON with the expected key set and is never listed as one of its own entries; `tests/servers/fixtures/terminal_ws_golden/` holds no files; and `golden_fixtures` no longer exists in production code. file: `tests/fixtures/terminal_ws_golden/manifest.json`.
- 1.5.2 - Every reply and server-initiated shape produced by the real Python emitters matches its fixture byte-for-byte after canonicalization, including `terminal_frame`, `attach_result_direct`, `create_result_refused`, `list_snapshot`, `kill_result`, and `detach_result`. test: `tests/servers/test_terminal_ws_golden.py::test_emitters_match_golden_replies`.
- 1.5.3 - `crates/gclient/tests/ws_golden.rs` and the vitest hook suite replay all 38 manifest entries from the canonical path as WebSocket messages and decode the new shapes, neither harness attempting to replay `manifest.json` itself; the Rust safe-integer guard holds across every sequence field rather than at one point: `message_seq`, `lease_generation`, and `client_write_seq` each survive production encoding and decoding as JSON numbers, `2^53-2` and `2^53-1` remain distinct encodings, and `2^53`, a string, and a float are each refused rather than coerced. test: `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`.

## P2: gclient data plane and frame sources
`kind: framing`

**Goal**: the client owns a real REST + single-reader WebSocket data plane behind a
`Daemon` trait, and two frame sources — the local Unix socket and the cell-mode daemon
proxy — behind the frame-source trait, with the scripted doubles re-based on the same
traits.

### 2.1 Implement the `Daemon` trait and `LiveDaemon` over REST and a single-reader WebSocket [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gclient/Cargo.toml`
- `crates/gclient/src/daemon/mod.rs::*` — scope-reason: the `Daemon` trait, typed errors and events, and `ScriptedDaemon` re-based on the trait
- `crates/gclient/src/daemon/ws.rs::*` — scope-reason: codec gains the message-shape decoders `LiveDaemon` routes on
- `crates/gclient/src/daemon/rest.rs`
- `crates/gclient/src/daemon/live.rs`
- `crates/gclient/src/daemon/live_reader.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: `Workspace<D: Daemon>` replaces the hardcoded `ScriptedDaemon` field
- `crates/gclient/src/startup.rs::*` — scope-reason: `ProbeEnv` exposes `daemon_url` and the bearer to `LiveDaemon`
- `crates/gclient/tests/mock_daemon/mod.rs`
- `crates/gclient/tests/daemon_live.rs`
- `crates/gclient/tests/reconciliation.rs::*` — scope-reason: the subscribe-first tests run against `LiveDaemon` and the mock

`daemon/mod.rs` today holds only `ScriptedDaemon`, whose "REST" calls push strings into
a vector; `daemon/ws.rs` is a codec with no socket beneath it; `tokio-tungstenite` has
zero uses. Define:

```rust
#[async_trait::async_trait]            // or hand-written boxed futures; no new crate if avoidable
pub trait Daemon: Send + Sync {
    async fn list_terminals(&self, project: &str, cursor: Option<&str>) -> Result<Page<TerminalRow>, DaemonError>;  // resolved UUID, never optional
    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError>;
    async fn respond(&self, entry: &str, attention_id: &str, answer: &Answer) -> Result<(), DaemonError>;
    async fn mark_seen(&self, entry: &str, attention_id: &str) -> Result<(), DaemonError>;
    async fn spawn(&self, req: SpawnRequest) -> Result<SpawnOutcome, DaemonError>;      // terminal_create over WS
    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError>;   // terminal_kill over WS
    fn subscribe(&self) -> (Generation, broadcast::Receiver<DaemonEvent>);
    async fn send(&self, msg: WsMessage) -> Result<WsReply, DaemonError>;   // correlated
    async fn notify(&self, msg: WsMessage) -> Result<(), DaemonError>;      // one-way (viewport, resize)
    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError>;
    async fn close(&self) -> Result<(), DaemonError>;                       // idempotent, terminal
}
pub enum DaemonError { Unauthorized, NotFound, Unavailable { retry_after: Option<Duration> }, Protocol { detail: String }, ControlRequestInFlight, Timeout }
```

`LiveDaemon` (`live.rs`, `live_reader.rs`, `rest.rs`): `reqwest` with the bearer on
every request for `GET /api/terminals` (follows `next_cursor` to termination; pins the
first page's `snapshot`), `GET /api/terminals/{id}`, `GET /api/attention/roster`,
`POST /api/attention/{entry}/respond`, `POST /api/attention/{entry}/seen`; 401/403 →
`Unauthorized`, 404 → `NotFound`, 5xx/connection → `Unavailable`, a body the golden
decoder rejects → `Protocol`. `tokio-tungstenite` against `/ws` with
`Authorization: Bearer`. Exactly one reader task per socket generation; three
correlation maps keyed the way the wire actually is — `request_id → oneshot` for
`terminal_create_result` / `terminal_kill_result` / `terminal_attach_result` /
`terminal_detach_result` / `terminal_list`, `(attachment_id, client_write_seq) →
oneshot` for `terminal_write_outcome` (which carries no `request_id`), and a
single-flight `attachment_id → oneshot` control waiter for `terminal_control_result`
(a second concurrent control request on one attachment is refused locally with
`ControlRequestInFlight`). Lifecycle messages (`terminal_event`, `terminal_lease_lost`,
`terminal_attachment_finalized`, `terminal_output`, `terminal_frame`,
`terminal_attach_history`, `terminal_scroll_offset_applied`, reassembled
`terminal_ws_fragment`s) and attention broadcasts — carried on the wire as the agent
event `attention_metadata_changed` (`agents/attention_metadata.py`), not as a message
named `attention` — fan out to subscribers through a
256-entry broadcast channel; a lagging receiver gets `DaemonEvent::Lagged` and must
re-list.

`DaemonEvent` carries `Attention { epoch, seq, payload }` and
`Disconnected { generation, error }` alongside the terminal lifecycle variants.
`Disconnected` exists because 3.2 requires the client to go read-only *before* the first
reconnect attempt, and a state transition that must happen before something else needs an
ordered signal from the component that saw the failure. The run loop selects over
terminal input, this subscription, frame-source frames, and a render tick — nothing else
tells it the daemon reader died, so without the variant the first thing to notice is the
supervisor, which is already too late. `LiveDaemon` emits exactly one `Disconnected` per
generation, after readiness is cleared and the waiter maps are failed and before any
reconnect joiner is woken, so the loop's clearing of lease, control, and `pending_input`
state is ordered ahead of recovery admission rather than racing it. `Workspace` already implements the whole attention reducer against
`ScriptedDaemon` — its own `reconcile_subscribe_first`, `install_roster`, and
`ingest_attention` with the epoch-change refetch and `seq <= self.attention.seq`
discard, fed by `ScriptedDaemon`'s `take_attention_events` and
`fetch_attention_roster` (`daemon/mod.rs`) — and it is exercised only by scripted
tests. Nothing drives it from a real socket, so the source plan's invariant that no
attention transition is lost or reordered across the roster fetch is asserted against a
double and unproven against the daemon.

The gap is the *input*, not the orchestration, and only the input changes. `Workspace`
already owns every step — subscribe, buffer, fetch, install, replay — and it owns the
attention state those steps write. Giving `LiveDaemon` a second copy of that sequence
would put snapshot installation in the transport layer, which the final `Daemon` trait
deliberately has no surface for: `roster()` returns data, and nothing on the trait
installs state. That would be a second reducer with no consumer.

So keep one mechanism. `Workspace<D: Daemon>::reconcile_subscribe_first` becomes `async`
and remains the single owner of the handshake; `LiveDaemon` supplies the real
operations it calls — the subscription that now yields `DaemonEvent::Attention`, and
`roster()` over `GET /api/attention/roster` — in place of `ScriptedDaemon`'s canned
replies. The reducer body, its epoch-change refetch, and its `seq <= self.attention.seq`
discard are unchanged. Acceptance 2.1.12 drives that same `reconcile_subscribe_first`
through `Workspace<LiveDaemon>` against the mock transport, which is what turns the
source plan's no-lost-no-reordered guarantee into a claim about the real socket. The
existing scripted reducer tests stay as secondary coverage. No parallel snapshot watch,
no callback, and no second reducer. Before
`terminal_attachment_finalized` is fanned out, every pending entry naming that
`attachment_id` in the write and control maps is failed and removed. On EOF, read
error, cancellation, `close`, or `reconnect`, every pending sender fails `Unavailable`
and the maps are cleared before the replacement socket accepts requests.

`close` is on the trait because 3.3's ordered shutdown is written against `Daemon`, not
against `LiveDaemon`: it closes the WebSocket after the bounded detach phase, and with no
trait member for that it cannot. It is also the operation this section's own cleanup rule
already names — "on EOF, read error, cancellation, `close`, or `reconnect`, every pending
sender fails `Unavailable`" — so the trait was describing a verb it did not expose.
Semantics: idempotent and terminal. It atomically stops admitting new work (every later
`send`, `notify`, and `reconnect` fails typed without touching the socket), fails and
clears every outstanding waiter in all three maps, stops the reader task, and closes the
sink. A second call is a no-op.

Rejecting *later* work is only half of terminal, because the single-flight reconnect of
this section means an episode can already be suspended inside a handshake when `close`
runs. That attempt was admitted before the latch and would otherwise resume afterwards
and install a fresh sink, a fresh reader, and a generation-ready value on a daemon the
caller has closed — a resource leak the shutdown seam has no remaining step to clean up.
So `close` fences work admitted before it as well: it cancels the in-flight reconnect
episode and awaits its completion, and every joiner waiting on that episode settles with
the same terminal close error rather than a generation. The fence is checked twice, since
cancellation alone races a task already past its last await point: every reconnect attempt
re-reads the terminal-close state immediately before it publishes a sink, a reader, or a
ready value, and abandons the connection it just built if the latch is set. `close`
returns only after that episode has settled, so nothing appears after it returns. `ScriptedDaemon` implements it with the same
observable contract so shutdown tests run on the double.

Every entry in all three maps is removed by its exact key on every terminal path, not
only on the paths that produce a reply: successful settlement, request timeout, caller
cancellation, a failure to write the request to the socket, attachment finalization, and
generation teardown. Removal is unconditional and idempotent — a timeout removes its own
key even when the reply is already in flight — and a reply arriving after removal is
logged at debug and dropped, never used to recreate the entry, because a recreated entry
has no owner and leaks for the life of the socket. The single-flight control waiter
makes this load-bearing rather than hygienic: one leaked `attachment_id` entry refuses
every later control request on that attachment with `ControlRequestInFlight` for as long
as the generation lives, which permanently disables resize, viewport, and scroll on that
pane with no error the operator can act on. The `request_id` and
`(attachment_id, client_write_seq)` maps degrade less sharply but grow without bound in
proportion to unanswered requests.

Plain removal is wrong for the control map alone, and the reason is the wire shape.
`terminal_control_result` carries `attachment_id`, `granted`, `reason`, and
`lease_generation` — no `request_id` and no other per-request discriminator — so
`attachment_id` is the whole key, and `lease_generation` cannot substitute (source 2.5.21
requires equal-generation `held` refusals and idempotent already-released results to be
*applied*, so equal generations do not identify a request). Removing a timed-out take's
entry therefore frees a key that the timed-out request can still settle: a release
issued afterwards registers under the same `attachment_id`, the old take's reply
arrives, and it settles the release — reporting the wrong outcome for an operation that
never got one. That is the cross-request alias the cleanup rule exists to prevent,
reintroduced by the cleanup itself.

Split the two cases by whether the request reached the socket. Cancellation **before**
the transport write means no reply can ever exist, so the key is released and
immediately reusable. Timeout or cancellation **after** the write leaves the outcome
genuinely unknown, so removal also **tombstones that attachment's control scope**: the
entry is gone, and no new take or release waiter may be registered for that
`attachment_id` until the attachment is retired — a detach, a
`terminal_attachment_finalized`, or a fresh attach yielding a new id. A control request
attempted against a tombstoned scope is refused locally and typed, and the pane's
recovery is the fallback path it already has, never a silent wrong answer. The
`request_id` and write maps need none of this: their keys are unique per request, so
exact-key removal is sufficient there.

`reconnect` is single-flight per observed generation, and the observation is a parameter
rather than implicit state. A parameterless `reconnect()` cannot tell a caller asking to
replace the generation it is looking at from a caller that suspended across a successful
replacement and woke up holding a stale view: both arrive at the same entry point saying
only "reconnect", so the stale one starts a second socket replacement and the
single-flight guarantee is lost precisely when it matters. Passing the observed
`Generation` closes that window and makes the operation a compare-and-replace.

The decision is made against the active generation **and its readiness**, because
"newer" alone does not mean "usable": a generation that replaced the caller's
observation and has since dropped is both newer and dead, and returning it as a success
sends the caller to a socket that will never serve it, with no error and no retry.
Readiness is therefore explicit state — published the moment the replacement socket is
connected and serving, and **cleared the moment that generation disconnects**. Four cases, decided at the
moment the call is admitted:

1. The observation equals the active generation and no attempt is in flight — this
   caller owns socket replacement: one handshake, one reader, one generation-ready value.
2. The observation equals the active generation and an attempt is already in flight for
   it — this caller joins that attempt and receives its `Generation`.
3. The observation is older than the active generation and that generation is **ready**
   — replacement already happened and succeeded, so the call returns the current
   generation immediately and replaces nothing.
4. The observation is older than the active generation and that generation is **not
   ready**, because its attempt is still in flight or because it connected and then
   dropped and readiness was cleared — the caller joins the in-flight attempt when there
   is one, and otherwise receives the same typed `Unavailable` the failed attempt
   settled on. It never receives a dead generation as a success, and it never starts a
   competing replacement: recovery for the newer generation belongs to whoever holds
   that observation.

Success publishes one generation-ready value and starts one reader; failure settles all
waiters for that observation with the same error and leaves one retryable disconnected
state.

`Workspace` keeps the client half of 1.4's ordering invariant, because it is the side
that *applies* events: it tracks the highest lifecycle `seq` it has applied within the
current `daemon_epoch` and discards any buffered event at or below that high-water during
replay, so a reconnect's replay can never rewind state the live stream already advanced
past. An epoch change resets the high-water along with the roster refetch.

The terminal roster reconciles under the same ownership rule as attention, and for the
same reason. Subscribe-first is a sequence of steps that *installs state*: buffer,
page, pin, install the roster, replay. `Workspace` owns the panes that state becomes, and
the trait deliberately has no surface for installing anything — `list_terminals` returns a
page and `subscribe()` returns a stream. Assigning the sequence to `LiveDaemon` also asks
for inputs it does not have: `list_terminals(project, cursor)` needs a project id, while
`reconnect(observed)` carries none and returns only a `Generation`, so a transport-owned
re-list would have to retain hidden project state and hold a roster no consumer reads.
That is the second reducer this section already refuses for attention.

So `Workspace<D: Daemon>` owns the subscribe-first handshake on connect and on reconnect:
subscribe, buffer lifecycle events (1,024 entries; overflow restarts the listing), page
`terminal_list` for its project, pin `snapshot{daemon_epoch, seq}` from page one, install
the roster into its own panes, replay buffered events with `seq > pinned.seq` in the same
epoch, and refetch on epoch change. `LiveDaemon` supplies exactly two things: the
subscription and the one-page REST/WS operation each traversal step calls.
**Generation-ready** stays on the transport where it belongs and means what the transport
can actually know — a socket generation is connected and serving — published as a readable
value (`subscribe()` returns it beside the stream) so a `Workspace` that subscribes late
still sees it. Roster readiness is `Workspace`'s own, reached when its handshake finishes.
`LiveDaemon` issues no `terminal_attach` of its own: `Workspace` is the sole issuer and
attaches each shown pane at most once per generation, driven by generation-ready. `ScriptedDaemon` implements the same trait with
scripted replies and faults so the existing reducer tests keep running unchanged.
`tests/mock_daemon/mod.rs` is an in-process HTTP + WS server speaking the canonical
corpus with scripted faults (401, 404, 500, drop, malformed body, withheld reply,
`cursor_stale` refusal, and a subscriber forced past the broadcast capacity).

`cursor_stale` and a `daemon_epoch` other than the pinned one converge on the one
recovery the paragraph above already specifies: discard the partial listing, restart it,
and re-pin. A `cursor_stale` refusal means the cursor is well-formed but its `epoch` no
longer matches the daemon's — everything else malformed is refused — typed `invalid_cursor` on the WS path
(`terminal_ws.py`), an untyped 400 "invalid cursor" on REST — and both surface to the
client as a `Protocol` error. The refusal is a D1 daemon deliverable and is not emitted today; the
client handles it now so that landing D1 does not turn a routine restart into a failed
listing, and the cost is one mock fault plus one branch into an existing path.

**Acceptance:**

- 2.1.1 - `Workspace<LiveDaemon>` follows `next_cursor` across three mock pages through repeated `Daemon::list_terminals` calls, yields every row once, and pins page one's `snapshot`; every page request it issues selects only pending and live rows through the comma-separated `states=pending,live` filter (`_parse_states`), no request reaches an unpaged listing, and retained history is fetched only through a separate explicit query the roster traversal never makes; `LiveDaemon::list_terminals` itself returns one page and retains no cursor, roster, or project state between calls. test: `crates/gclient/tests/daemon_live.rs::list_terminals_follows_cursor_and_pins_snapshot`.
- 2.1.2 - Every `Daemon` method on `LiveDaemon` has an authenticated success case and a typed-failure case against the mock; every request carries the bearer; 401/404/5xx/malformed map to `Unauthorized`/`NotFound`/`Unavailable`/`Protocol`; `spawn` decodes `create_result_refused` to `Refused{reason}`. test: `crates/gclient/tests/daemon_live.rs::every_method_has_success_and_typed_failure`.
- 2.1.3 - With `spawn`, `terminate`, a write, a control request, and lifecycle events interleaved on one socket, each reply settles its own waiter through the correct map, an unmatched `request_id` is dropped, a `terminal_write_outcome` without `request_id` settles by `(attachment_id, client_write_seq)`, finalization fences both attachment maps before fan-out, and a connection drop fails every pending sender before the next generation serves a request. test: `crates/gclient/tests/daemon_live.rs::single_reader_routes_replies_and_events`.
- 2.1.4 - `notify(terminal_set_viewport)` resolves once the write completes, registers no waiter, and a write on a closed generation fails `Unavailable`. test: `crates/gclient/tests/daemon_live.rs::notify_settles_without_a_reply`.
- 2.1.5 - On reconnect `LiveDaemon` tombstones its previous generation's attachment ids, establishes the replacement socket, and publishes generation-ready, issuing no listing and no attach of its own; `Workspace` driven by that signal re-subscribes, re-lists its project, replays only `seq > pinned.seq`, and issues exactly one `terminal_attach` per shown pane; a subscriber that lags or subscribes late reads the ready generation as a value and attaches once. test: `crates/gclient/tests/reconciliation.rs::reconnect_reattaches_once_per_pane`.
- 2.1.6 - `Workspace<D: Daemon>` compiles against both `LiveDaemon` and `ScriptedDaemon`, and every pre-existing reducer test passes unchanged on the scripted double. symbol: `Workspace`.
- 2.1.7 - Driven directly as a `LiveDaemon` harness (this criterion owns the single-flight property of the primitive; 3.2.9 owns pane-originated concurrency, and panes never call `reconnect` themselves): two concurrent callers passing the same observation cause one replacement WS handshake, one reader, and one generation-ready publication shared by both, and both receive that generation; the transport performs no listing and no replay of its own, which stay `Workspace`'s under 2.1.5. A third caller suspended across the ready boundary and released afterwards passes its now-stale observation, receives the already-current generation, and causes no second handshake. A scripted failed attempt settles every caller for that observation and a later call performs one retry. A caller still holding the G1 observation that arrives while G2's own recovery is in flight joins that attempt rather than starting a third handshake, and one that arrives after G2 connected and dropped receives the typed `Unavailable` — never G2 as a success — and starts no competing replacement. test: `crates/gclient/tests/reconciliation.rs::concurrent_reconnect_is_single_flight`.
- 2.1.8 - After a reconnect whose buffer holds a lifecycle event with `seq` at or below the highest `Workspace` has already applied in the same `daemon_epoch`, its replay discards the event and leaves the live-stream state intact; an epoch change resets the high-water and refetches instead of discarding; `LiveDaemon` holds no applied-seq state of its own. test: `crates/gclient/tests/reconciliation.rs::replay_never_rewinds_applied_state`.
- 2.1.9 - Across fifty requests whose replies are withheld until timeout and fifty more cancelled by the caller, every entry is removed from all three correlation maps by its exact key and each map's size returns to zero; a withheld reply that arrives after its entry was removed is dropped and recreates nothing. Reuse follows the transport-write boundary: a `request_id` or `(attachment_id, client_write_seq)` key is immediately reusable after exact-key removal on every path, and a control request that was cancelled *before* its bytes reached the socket leaves the attachment's control scope immediately reusable, so the next take or release is admitted normally. A control request whose bytes did reach the socket and then timed out or was cancelled drains its waiter and tombstones that attachment's control scope instead — 2.1.14 owns that case, and the two criteria partition the space rather than overlapping. test: `crates/gclient/tests/daemon_live.rs::correlation_maps_drain_on_every_terminal_path`.
- 2.1.10 - During `Workspace`'s subscribe-first handshake, the 1,025th buffered lifecycle event discards the partial listing and its pinned snapshot and restarts the listing from a fresh snapshot rather than replaying against a stale pin; a `cursor_stale` refusal mid-listing produces the same restart-and-re-pin, and the resulting pane set converges with the mock's roster. test: `crates/gclient/tests/reconciliation.rs::buffer_overflow_and_cursor_stale_restart_the_listing`.
- 2.1.11 - A `Workspace` driven 256 entries behind receives `DaemonEvent::Lagged`, re-subscribes, re-lists its project, and converges on the mock's current roster without reusing any pre-lag buffered event or pinned snapshot. test: `crates/gclient/tests/reconciliation.rs::lagged_subscriber_relists_and_converges`.
- 2.1.12 - `Workspace<LiveDaemon>::reconcile_subscribe_first` runs the attention handshake over the mock transport with no second reducer or snapshot watch anywhere in the client: attention broadcasts arriving before, during, and after `GET /api/attention/roster` all land, in `seq` order, with none lost and none applied twice; an attention broadcast in a different `epoch` refetches the roster instead of applying; the resulting entry set and applied-`seq` sequence match the mock's ground truth. test: `crates/gclient/tests/reconciliation.rs::live_attention_subscribe_first_no_regression`.
- 2.1.13 - Every outbound WebSocket message emitted by LiveDaemon and Workspace production send/notify paths matches its canonical request fixture byte-for-byte after canonicalization. test: `crates/gclient/tests/daemon_live.rs::outbound_messages_match_corpus`.
- 2.1.14 - A take-control request that times out after reaching the socket tombstones its attachment's control scope: a release attempted afterwards on that same `attachment_id` is refused locally with the distinct `DaemonError::ControlScopeIndeterminate` — not `ControlRequestInFlight`, which would tell the caller to wait for a reply that will never settle — rather than registered, and when the original take's `terminal_control_result` finally arrives it settles nothing and is dropped. A take cancelled *before* the transport write leaves the scope immediately reusable and the next control request is admitted normally. After a detach, a `terminal_attachment_finalized`, or a fresh attach yielding a new id, control requests are admitted again. test: `crates/gclient/tests/daemon_live.rs::late_control_reply_cannot_settle_a_newer_request`.
- 2.1.15 - `Daemon::close` is idempotent and terminal: after it returns, every new `send`, `notify`, and `reconnect` fails typed without touching the socket, every outstanding waiter in all three maps has been failed and cleared, the reader task has stopped, and the sink is closed; a second `close` is a no-op. With a reconnect episode paused inside its handshake and `close` raced against it, the episode is cancelled and awaited, every joiner settles with the terminal close error rather than a generation, and after `close` returns no sink, reader task, or generation-ready value has been installed — including when the paused attempt is released past its last await point, where the pre-publication fence check discards the connection it just built. test: `crates/gclient/tests/daemon_live.rs::close_is_idempotent_and_rejects_later_requests`.

### 2.2 Build the direct Unix-socket and cell-mode proxy frame sources [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/frame_source.rs::*` — scope-reason: the trait gains a receive side, `UnixSocketFrameSource` becomes a real client that owns its stream, and the module declares and re-exports its new `proxy` child
- `crates/gclient/src/frame_source/proxy.rs`
- `crates/gclient/src/views/mod.rs::*` — scope-reason: the `observe_tmux_pane` invoker attaches through the real direct source
- `crates/gclient/tests/frame_source_live.rs`
- `crates/gclient/tests/workspace.rs::*` — scope-reason: the epoch-refusal and EOF tests run against the real source; the tautological assertion at its line 71 is replaced by a real one
- `crates/gclient/src/app/mod.rs::*` — scope-reason: Workspace replaces its single frame source with per-pane PaneFrameSource ownership
- `crates/gclient/src/app/pane.rs::*` — scope-reason: Pane stores its Direct, Proxy, or Scripted source and one-pane fallback state
- `crates/gclient/tests/copy_paste.rs::*` — scope-reason: existing Guard-set-H assertions migrate from the removed workspace-wide `frames()` accessor to pane-specific scripted-source observability
- `crates/gterminal/src/protocol/wire_codec.rs::*` — scope-reason: the sync-only framing helpers gain async siblings over `AsyncRead`/`AsyncWrite` sharing their validation and size bounds
- `crates/gterminal/tests/wire_codec_async.rs`

`UnixSocketFrameSource::connect` today dials, sends `Hello`, reads `Welcome`, checks the
epoch, writes `AttachTerminal`, and drops the stream; `send` always returns
`NotConnected`; nothing constructs the type.

State the trait's final shape rather than extending the current one, because the current
one cannot be a shared surface. `FrameSource` today declares `connect(&mut self, locator:
&AttachLocator, cols: u16, rows: u16)`, a **synchronous** `send(&mut self, &ClientMessage)`,
and six test-introspection methods (`sent_attach`, `sent_host_input`, `sent_resize`,
`sent_mouse_report`, `sent_tiocswinsz`, `last_client_message`). Two of those members are
disqualifying. A synchronous `send` cannot await `Daemon::notify`, which is precisely
what `ProxyFrameSource` must do for viewport and scroll. And `connect` is direct-shaped:
it takes an `AttachLocator` and dials a socket, while the proxy source is constructed
*after* a `terminal_attach` reply, out of the attachment id that reply returned. A closed
dispatch enum over a trait carrying those two members is not implementable by both
variants, so the final surface is three members and nothing else:

```rust
pub trait FrameSource {
    async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError>;
    async fn recv(&mut self) -> Result<ServerMessage, FrameError>;
    fn transport(&self) -> Transport;
}
```

`connect` leaves the trait. Dialing, `Hello`/`Welcome`, and the epoch check move into
`UnixSocketFrameSource`'s own constructor, which yields either an already-attached source
or a typed `FrameError`; `ProxyFrameSource`'s constructor takes the attachment id
instead. Each concrete type gets the construction it actually needs, and the shared
surface covers only what a pane does with a source it already holds.

The six `sent_*` observers also leave the trait and remain **inherent methods on
`ScriptedFrameSource`**. They exist so scripted tests can assert what the double was
asked to send; the real sources have no use for them, and the production dispatch enum
should not carry six accessors that two of its three variants would stub. Existing
assertions keep working because the scripted tests hold a `ScriptedFrameSource`
concretely.

`Transport` does not exist anywhere today — the transport is only the `frame_delivery`
wire string — so this leaf introduces the enum, and introducing it as `Direct | Proxy`
would leave `ScriptedFrameSource::transport` with
no honest answer while 2.2.6 asserts that `transport()` reports the right variant per
pane. Do not add a third `Scripted` variant: the double stands in for a real transport,
and a pane that reports `Scripted` asserts nothing about the behaviour under test. Give
`ScriptedFrameSource` an explicit transport-emulation setting chosen at construction, so
a scripted pane declares which real transport it is standing in for and `transport()`
returns that.

The framing the async source needs does not exist yet, and naming the existing codec
hides that. `gobby_terminal::protocol`'s helpers are `write_message<W: Write, ...>` and
`read_message<R: Read, ...>` — generic over **synchronous** `std::io`, so they cannot
frame a `tokio::net::UnixStream`'s split halves. Written as-is, the direct source is
implementable only by blocking a Tokio worker on a socket, by hand-rolling a second
framing implementation beside the first, or by an adapter the plan never specifies.
Blocking a worker is a deadlock waiting for a slow host; a second implementation is two
places for the length prefix, the bincode call, and the size bound to disagree.

Add async siblings in `wire_codec.rs` over `AsyncRead`/`AsyncWrite` that reuse the
existing bincode encode/decode, the same `FramingError` variants, and the same
frame-size bound (`MAX_FRAME_SIZE` in `wire.rs`, passed into `read_message` exactly as
the sync path takes it), so the two paths share one validation contract and only their
I/O differs.

Cancellation needs an explicit answer, and the honest one is that the connection dies. A
mid-frame `read_message` future has already consumed a length prefix or part of a payload
from the stream by the time it is dropped, and those bytes cannot be pushed back: any
promise that a cancelled read leaves the stream reusable would require a persistent
codec owner holding a read buffer across calls, which is a second stateful layer this
source does not otherwise need. So the helpers stay plain and cancellation-unsafe, and the *owner* answers for
cancellation. A dropped future returns nothing — that is what dropping means — so the
cancellation outcome cannot be a return value of the helper; what survives the drop is the
task that held it. The helpers are therefore called only from the source's own reader and
writer tasks, and aborting either **retires the connection**: the source marks it unusable,
attempts no further read or write on it, reunites or drops both halves, and surfaces
`FrameError::Cancelled` — a new variant this leaf adds beside the existing
`HostEpochChanged`/`Eof`/`Lag` — at its own boundary, where recovery is the reattach path
it already has for EOF and typed lag. That keeps one bincode call and one size bound shared by both
paths, introduces no codec owner type, and moves the cost to a path the deliverable already
owns.

Shutdown has the mirror-image subtlety. `UnixStream::into_split` moves ownership into the
two halves, so there is no independent whole-socket handle to keep beside them; closing
the connection means reuniting the halves (or shutting down the write half and dropping
the read half) so the socket actually closes and the host observer is released. Cover
partial reads and partial writes, cancellation mid-frame at the prefix boundary and
inside the payload, an oversize frame refused at the same bound the sync path uses, EOF,
and shutdown while a read is pending.

Make the Unix source own a
`tokio::net::UnixStream` split into a reader task (bincode length-prefixed
`ServerMessage`s via `gobby_terminal::protocol`'s codec into a bounded channel) and the
writer for `SetViewport`, `SetScrollOffset`, `Detach`. `Hello` carries the
`TmuxClientIdentity` block — defined in `gobby_terminal::protocol::wire_types`;
`tmux_identity.rs` only imports it — when the client runs inside tmux;
`AttachTerminal` omits `reservation_id` (user attach) — `crates/gterminal/tests/embed.rs`
asserts both by source text, so `views/mod.rs` keeps the `observe_tmux_pane` name and
the literal `reservation_id: None`. Native rows attach by `host_terminal_id`; tmux rows
attach by the `pane` locator from 1.3's `direct` block, and the host creates or joins
its capture-poll observer.

Direct frames are ordered and are never dropped individually. If the 256-entry receive
channel is full, the reader closes the source with `FrameError::Lag`; closing the
socket releases the host observer, and the app handles that error through the existing
direct→proxy fallback.

The proxy source needs the same treatment, and for a sharper reason: `terminal_frame`
rides 2.1's shared 256-entry daemon broadcast channel, and a lagging receiver there gets
`DaemonEvent::Lagged`. For roster metadata the documented recovery — re-list — is
sufficient, because a fresh listing reconstructs the whole of that state. Cell frames are
not metadata. They are a keyframe followed by deltas, so a dropped frame leaves the pane
grid permanently wrong and no amount of roster relisting repairs it; the pane stays
corrupted until something forces a new state boundary. Treat proxy lag as a frame-source
failure rather than a metadata gap: on `DaemonEvent::Lagged` the `ProxyFrameSource`
fails its pane's source, the workspace detaches and tombstones the old attachment id, and
issues one fresh `terminal_attach{frame_delivery: "proxy", encoding: "semantic_frame"}`.
Rendering resumes only from the new attachment's history/keyframe boundary; no delta
received before the overflow is applied to the new grid. This is the proxy mirror of the
direct source's `FrameError::Lag` path, so the two transports recover the same way.

`ProxyFrameSource` lives at `crates/gclient/src/frame_source/proxy.rs` rather than as a
new crate-root module: `frame_source.rs` is already this deliverable's file-wide target,
so declaring `pub mod proxy;` there and re-exporting `ProxyFrameSource` registers the new
file in the crate module tree — and makes it importable from the integration tests —
without touching `lib.rs` or adding an ownership edge to a file this leaf otherwise never
opens. It is the same trait over `LiveDaemon`, and the order in which it acquires its event
stream is load-bearing. The daemon starts `ProxyHub`'s pump inside `_start_proxy_attach`,
which runs *before* `_handle_terminal_attach` sends `terminal_attach_result`, so the
attach history or first keyframe can already be on the wire when the reply arrives.
A source that subscribed after learning its `attachment_id` would start reading from a
position past that boundary and begin the pane on a delta, or never render at all — and
the same gap reopens on every lag recovery and every fresh proxy attach. So the caller
takes a `DaemonEvent` receiver **before** writing `terminal_attach` and hands that
already-buffering receiver to `ProxyFrameSource` once the reply supplies the id; the
source filters it by that `attachment_id` and applies the state boundary exactly once
before any delta. This costs nothing new — `subscribe()` already returns a buffering
receiver — it only fixes when it is called. The source then base64-decodes each
`terminal_frame.payload` and decodes it with the same `ServerMessage` codec, consuming
that attachment's `terminal_frame` / `terminal_attach_history` /
`terminal_scroll_offset_applied` events. It is the source 3.2's direct→proxy
fallback uses and the source a `--daemon-url` session uses for every pane.
`source_size.rs`'s `UnixStream::connect` chokepoint guard now allows `frame_source.rs`
only, unchanged.

Both outbound verbs are one-way. `SetViewport` sends as `notify(terminal_set_viewport)`,
as before. `SetScrollOffset` also sends as `notify(terminal_set_scroll_offset)`, not
`send`: `send` is the correlated path, and correlation needs a reply key the wire
actually carries. The daemon answers scroll with `terminal_scroll_offset_applied`, which
has no `request_id` and no other per-request discriminator, so a correlated future has
nothing to settle on and two concurrent scroll replies on one attachment cannot be told
apart. Inventing a correlation map for it would be new machinery with no caller that
needs to await a specific scroll. The applied offset is already a subscribed event on
this source, so `ProxyFrameSource` consumes `terminal_scroll_offset_applied` from its own
event stream and surfaces it through `recv` like any other server message; the reducer
reconciles the applied offset when it arrives.

`FrameSource` cannot be the collection's dispatch type as declared. `recv(&mut self) ->
impl Future` is not dyn-compatible, so `Box<dyn FrameSource>` does not exist, and
`Workspace` is generic only over `Daemon` — yet panes are independent, so one pane can be
Direct while its neighbour is Proxy after a fallback. Give every attached pane a closed
`PaneFrameSource` enum with `Direct(UnixSocketFrameSource)`, `Proxy(ProxyFrameSource)`,
and `Scripted(ScriptedFrameSource)` variants, each of `recv`/`send`/`transport` matching
on the variant and calling the concrete method. Dispatch stays static, the trait stays as
written for the concrete types, and heterogeneous panes are representable because the
enum — not the trait object — is what a pane owns.

**Acceptance:**

- 2.2.1 - The direct source connects to a real `gterm host` started by the test, verifies `Welcome.host_epoch` against the expected epoch before sending `AttachTerminal`, receives a keyframe for a native terminal it spawned through the control socket, and refuses typed `HostEpochChanged` without attaching on a mismatch. test: `crates/gclient/tests/frame_source_live.rs::direct_frames_verify_epoch_and_render`.
- 2.2.2 - The direct source attaches a tmux pane through the host observer using the pane locator, receives the `AttachHistory` frame then keyframes as the pane changes, and a client running inside the target pane is refused typed by the host. test: `crates/gclient/tests/frame_source_live.rs::tmux_pane_attaches_through_host_observer`.
- 2.2.3 - The proxy source decodes `terminal_frame` payloads from the canonical corpus into `FrameData` identical to the bincode fixture, sends both viewport and scroll through `notify` and registers no correlation waiter for either, surfaces `terminal_scroll_offset_applied` to the caller through `recv`, and reports `Transport::Proxy`. Its receiver is taken before `terminal_attach` is written, so with the mock emitting the attach history or keyframe before the result, between the result settling and source construction, and after construction, the pane applies that state boundary exactly once and before any delta in all three schedules; the same holds for a fresh attach after lag recovery. test: `crates/gclient/tests/frame_source_live.rs::proxy_source_decodes_cell_frames`.
- 2.2.4 - `workspace.rs` asserts real state at every line (no expression compared to itself) and its epoch-mismatch and frame-EOF tests drive the real direct source against the scripted daemon. test: `crates/gclient/tests/workspace.rs::direct_frame_eof_detaches_before_reattach`.
- 2.2.5 - When a fake host sends 257 incremental frames while the consumer is stalled, the source reports `FrameError::Lag`, applies no frame after the overflow boundary, releases the direct observer by closing the socket, and the workspace issues exactly one fresh proxy attach. test: `crates/gclient/tests/frame_source_live.rs::direct_frame_overflow_fails_typed_without_dropping`.
- 2.2.6 - A workspace holding one `PaneFrameSource::Direct` pane and one `PaneFrameSource::Proxy` pane simultaneously compiles and runs: each pane's `recv` yields that transport's frames, `transport()` reports the right variant per pane, and a fallback converts one pane's variant without disturbing the other. test: `crates/gclient/tests/workspace.rs::direct_and_proxy_panes_run_together`.
- 2.2.7 - With a stalled proxy consumer driven past the 256-entry broadcast bound, the source reports the lag as a frame-source failure, the workspace detaches and tombstones the lagged attachment id and issues exactly one fresh semantic proxy attach, and the first frame applied to the pane after recovery is the new attachment's keyframe — no delta buffered before the overflow reaches the new grid. test: `crates/gclient/tests/frame_source_live.rs::proxy_lag_recovers_from_a_fresh_keyframe`.
- 2.2.8 - `FrameSource` declares exactly `send`, `recv`, and `transport`; `UnixSocketFrameSource`, `ProxyFrameSource`, and `ScriptedFrameSource` all implement it and all three are constructible and drivable through it, with connection and handshake living in the direct source's own constructor and the `sent_*` observers reachable only on the scripted double. A `ScriptedFrameSource` constructed to emulate direct reports `Transport::Direct` and one constructed to emulate proxy reports `Transport::Proxy`. test: `crates/gclient/tests/frame_source_live.rs::all_three_sources_share_one_surface`.
- 2.2.9 - The async framing helpers in `wire_codec.rs` round-trip every `ServerMessage` and `ClientMessage` shape identically to the synchronous helpers, refuse an oversize frame at the same bound with the same `FramingError` variant, survive reads and writes delivered one byte at a time, and report EOF distinctly from a truncated frame — with the bincode call and the size bound written once and shared by both paths. Aborting the source's reader task after the length prefix, and aborting it inside the payload, each retire the connection: the source marks it unusable, attempts no later read or write on it, and surfaces `FrameError::Cancelled` at its own boundary, where the failure enters the reattach path it already uses for EOF; the helpers themselves return nothing when their future is dropped and are never called outside those tasks. Shutdown closes the whole socket by reuniting the split halves rather than dropping one direction, and the fake host observes the connection closed and its observer released. test: `crates/gterminal/tests/wire_codec_async.rs::async_framing_matches_sync_contract`.

## P3: Chrome, workspace, and loop
`kind: framing`

**Goal**: herdr's chrome is imported and carved into Gobby's, and `gclient` runs a real
event loop that renders host frames, follows control with focus, answers attention, and
falls back from direct to proxy frames without losing the pane.

### 3.1 Import and carve the herdr v0.8.0 chrome, theme, and keymap [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `crates/gclient/src/ui/mod.rs::*` — scope-reason: imported chrome replaces the stub, and the module declares and re-exports its new `keymap` child
- `crates/gclient/src/ui/sidebar.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/sidebar_rows.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/sidebar_tokens.rs`
- `crates/gclient/src/ui/panes.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/pane_layout.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/tabs.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/tab_surface.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/navigator.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/status.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/keybind_help.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/dialogs.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/scrollbar.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/widgets.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/text.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/settings.rs::*` — scope-reason: imported chrome replaces the stub
- `crates/gclient/src/ui/chrome.rs`
- `crates/gclient/src/ui/chrome_render.rs`
- `crates/gclient/src/ui/keymap.rs`
- `crates/gclient/src/theme.rs::*` — scope-reason: the token map is applied through herdr's `terminal_theme` mechanism
- `crates/gclient/UPSTREAM.md`
- `crates/gclient/NOTICE.md`
- `crates/gclient/tests/ui_carve_guard.rs::*` — scope-reason: the guard parses `UPSTREAM.md` and stops accepting any non-blank render
- `crates/gclient/tests/source_size.rs::*` — scope-reason: the ceiling test covers the new modules
- `crates/gclient/tests/theme.rs::*` — scope-reason: accessibility assertions over the applied token map
- `crates/gclient/tests/keymap.rs`

Reference checkout `~/.gobby/clones/herdr` at tag `v0.8.0` (`346411fa`). Bring the
keep-set of `src/ui/` into `crates/gclient/src/ui/`, preserving glyphs, layout
arithmetic, truncation rules, and focus junctions exactly, and rewiring data access from
herdr's agents/workspaces model to Gobby's roster/terminal rows:

- `ui/sidebar.rs` (2,848) → `sidebar.rs` + `sidebar_rows.rs`; `ui/sidebar/tokens.rs`
  (337) → `sidebar_tokens.rs` (the token-usage column maps to Gobby session token
  stats); `ui/panes.rs` (1,498) → `panes.rs` + `pane_layout.rs` (BSP from
  `gobby_terminal::layout`); `ui/dialogs.rs` (946) → `dialogs.rs` without plugin and
  worktree dialogs; `navigator.rs`, `tabs.rs`, `tab_surface.rs`, `status.rs`,
  `widgets.rs`, `scrollbar.rs`, `text.rs` → same names; `keybind_help.rs` → same name
  on the gclient keymap; `settings.rs` → client-local preferences only; `src/ui.rs`
  (1,561) chrome parts → `chrome.rs` + `chrome_render.rs` (frame composition, focus
  ring, split borders). Dropped and listed as rejected in `UPSTREAM.md`: herdr's
  mobile, onboarding, and release-notes modules, the plugin entries of its menus
  module, agent-detection indicators, and worktree and persistence surfaces. Every
  accepted module starts with `// upstream: herdr v0.8.0 src/ui/<file>`. No file
  reaches 1,000 lines.
- Reuse `gobby_terminal::{layout, raw_input, input, selection, terminal_theme}`; never
  copy them. Today only `layout` is actually imported; the other four are new linkage
  this carve introduces, and `NOTICE.md`'s existing line claiming `theme.rs` already
  uses `terminal_theme` overclaims and is corrected with it.
- `ui/keymap.rs`: herdr's default bindings ported verbatim for the keep-set; plugin-menu
  bindings kept in the table with `reserved: true` and hidden from `keybind_help` until
  #20201; worktree/mobile bindings absent; new bindings `take_control`, `take_back`,
  `respond`, `quit`; overrides loaded from `~/.gobby/client/keymap.toml` and merged by
  action name. The file sits under `ui/` rather than at the crate root because
  `ui/mod.rs` is already this deliverable's file-wide target and `keybind_help.rs` is its
  closest consumer, so `pub mod keymap;` there registers the module in the crate tree and
  makes it importable from `tests/keymap.rs` without adding an ownership edge to
  `lib.rs`. Reading `keymap.toml` needs a TOML parser, and `gobby-client` has none today:
  declare the workspace's existing `toml = "1"` — already a direct dependency of
  `gobby-code` and `gobby-wiki` and already resolved in `Cargo.lock` — in
  `crates/gclient/Cargo.toml`. That adds `toml` to the `gobby-client` package entry in
  `Cargo.lock`, so both carriers belong to this deliverable, and because 2.1 also edits
  `crates/gclient/Cargo.toml` this deliverable now orders after it. Hand-rolling a TOML
  parser to dodge the dependency is not on the table.
- Keymap override collisions are rejected, not resolved silently. Merging by action name
  alone leaves a hole: an override can bind an action to a chord that another *active*
  default already owns, and merging by name preserves both, so one chord maps to two
  actions and dispatch is whatever the lookup order happens to be. A keymap must map each
  active chord to exactly one action. On loading an override, after applying it by action
  name, check every active chord for duplicate ownership; on a collision, reject the
  override file with an actionable error naming the chord, the overriding action, and the
  displaced action, and run on the unmodified defaults. Rejecting is preferred over
  override-wins because silently unbinding a default the user never mentioned is a
  surprise the user cannot see, while the error tells them exactly what to change. A
  chord owned only by a `reserved: true` plugin action is not a collision — reserved
  actions are non-dispatchable — and an override naming an unknown or deferred action is
  refused as it already is.
- `theme.rs`: load the `impeccable` skill first. Map every herdr palette name to the
  `.impeccable.md` state palette (accent hue 125; info 250 / warning 75 / destructive
  350 / success by lightness) through herdr's `terminal_theme` mechanism; each pair of
  state colours differs in relative luminance enough to survive a grayscale render; the
  focus ring uses the brand accent, never a hue shift alone; dark default, light peer.
- `ui_carve_guard.rs` becomes real: it parses `UPSTREAM.md`'s accept/reject table,
  asserts every accepted module exists with its upstream header, every rejected module
  is absent, no forbidden concept appears, and — replacing the `|| !s.trim().is_empty()`
  disjuncts — that the sidebar, navigator, and status renders contain the scripted
  roster's terminal titles and attention marker.

**Acceptance:**

- 3.1.1 - Every keep-set module exists under `crates/gclient/src/ui/` with its upstream header, every dropped module is absent, and the guard fails when a rejected module is added, a header is removed, or a render omits the scripted roster data. test: `crates/gclient/tests/ui_carve_guard.rs::carve_matches_upstream_map_and_renders_data`.
- 3.1.2 - No file under `crates/gclient/src` reaches 1,000 lines and no `UnixStream::connect` exists outside `frame_source.rs`. test: `crates/gclient/tests/source_size.rs::no_src_file_at_or_above_1000_lines`, `crates/gclient/tests/source_size.rs::license_notice_workspace_and_frame_source` (the existing `UnixStream::connect` chokepoint guard lives in the latter).
- 3.1.3 - `gclient` links `gobby_terminal::{layout, raw_input, input, selection, terminal_theme}` and contains no copied `layout.rs` or `raw_input.rs`; `UPSTREAM.md` records the keep-set count and the keymap provenance. file: `crates/gclient/UPSTREAM.md`.
- 3.1.4 - `render_workspace` composes sidebar, tabs, pane surface, status bar, and an open dialog from the imported modules for a scripted workspace of two terminals and one attention prompt into a 120×40 `TestBackend` without panicking, and the keybind help lists no reserved plugin binding. test: `crates/gclient/tests/ui_carve_guard.rs::render_workspace_composes_imported_chrome`.
- 3.1.5 - Every herdr token resolves to a `.impeccable.md` value, state colours keep distinct grayscale ranks and ANSI-256 lightness order, and keyboard focus renders with the brand accent plus a position cue. test: `crates/gclient/tests/theme.rs::tokens_match_design_contract_and_survive_monochrome`.
- 3.1.6 - The default keep-set action/key pairs equal the herdr v0.8.0 map; a client-local override replaces exactly one known action by name and leaves every other default unchanged; worktree/mobile action names remain unavailable; a reserved plugin action remains non-dispatchable and absent from keybind help even when the override file names it. test: `crates/gclient/tests/keymap.rs::overrides_preserve_defaults_and_cannot_activate_deferred_actions`.
- 3.1.7 - An override binding an action to a chord another active action already owns is rejected with an error naming the chord and both actions, and the resulting keymap is the unmodified default; an override reusing a chord owned only by a `reserved: true` action is accepted; after any accepted merge every active chord dispatches to exactly one action. test: `crates/gclient/tests/keymap.rs::colliding_override_is_rejected_and_defaults_survive`.
- 3.1.8 - `gobby-client` declares `toml` as a direct dependency in `crates/gclient/Cargo.toml`, the `gobby-client` package entry in `Cargo.lock` lists it, and no hand-written TOML parsing exists under `crates/gclient/src`. file: `crates/gclient/Cargo.toml`.

### 3.2 Build the app shell, event loop, and terminal views [category: code] (depends: 3.1, 2.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/main.rs::*` — scope-reason: the binary enters the loop instead of returning after the probe
- `crates/gclient/src/views/mod.rs::*` — scope-reason: `run_ready` becomes the real loop entry
- `crates/gclient/src/views/grid.rs`
- `crates/gclient/src/app/mod.rs::*` — scope-reason: the reducer drives real transports and the fallback state machine
- `crates/gclient/src/app/apply.rs::*` — scope-reason: fragments are reassembled into frames and history instead of counted and discarded
- `crates/gclient/src/app/pane.rs::*` — scope-reason: pane state gains the attach state machine and grid
- `crates/gclient/src/app/attach.rs`
- `crates/gclient/src/app/run_loop.rs`
- `crates/gclient/src/input.rs::*` — scope-reason: dead partial encoder replaced by the full crossterm→bytes encoder the loop uses
- `crates/gclient/tests/client_loop.rs::*` — scope-reason: the loop tests drive the real `select!` loop with scripted transports
- `crates/gclient/tests/attention_flow.rs::*` — scope-reason: the respond test goes through the dialog and the trait

`run_ready` owns the loop (`app/run_loop.rs`): the existing `teardown::TerminalGuard`,
constructed and armed by `startup.rs::start_session` before `run_ready` is called on the
TTY path — `run()`'s non-TTY branch reaches `run_ready` with no guard at all today, and
gains the same guarded entry when it enters the loop — so this
deliverable consumes it rather than building it — `teardown.rs` and the rename to the
stage-aware `TerminalModeGuard` stay wholly 3.3's, which lands after this leaf; a
`ratatui::Terminal<CrosstermBackend>`, and a `tokio::select!` over terminal input (from
`gobby_terminal::raw_input::spawn_input_reader()` on its own thread feeding a 256-entry
channel), daemon events (`Daemon::subscribe`), frame-source frames, and a 16 ms render
tick; renders through `ui::render_workspace`; exits on the quit key, `SIGINT`/`SIGTERM`/
`SIGHUP`, or daemon loss after the reconnect budget. `views/grid.rs` turns a `FrameData`
into the pane's `ratatui::buffer::Buffer` with `FrameData::to_ratatui_buffer`, applies
the viewport, and letterboxes a tmux pane whose owner geometry differs from the view.
`input.rs` encodes every crossterm key (arrows, function keys, modifiers, kitty flags
where negotiated) to the byte sequence the focused pane's `terminal_input` carries.

**Attach state machine** (`app/attach.rs`): a pane is `Detached` → `Attaching{request}`
→ `Attached{attachment_id, lease_generation, transport}`. Every proxy attach takes its `DaemonEvent`
receiver before writing `terminal_attach` and passes it to `ProxyFrameSource` after the
reply (2.2), so no history or keyframe emitted by the daemon's already-running pump is
missed. A `--daemon-url` session or a
row whose `direct` block is null attaches `frame_delivery: "proxy", encoding:
"semantic_frame"`; otherwise it attaches direct and opens the Unix source with the
`direct` block. On any direct-frame failure (EOF, typed lag close, epoch loss) the pane
enters `Detaching{old_attachment_id}`, sends `terminal_detach` for the old id, and waits
for either `terminal_detach_result` or the lifecycle `terminal_attachment_finalized`
naming that id — either alone advances it — then issues a fresh proxy attach and enters
`Attached` only on a result carrying a different `attachment_id`; the old id is
tombstoned so late frames, lease events, and write outcomes for it are dropped; control
is not carried across. `Detaching` is bounded by the 2 s deadline: on expiry the pane
submits a recovery intent tagged with the generation it was attached under to the run
loop's `ReconnectSupervisor`, awaits that episode's shared result, and re-attaches on
generation-ready. The pane never calls `LiveDaemon::reconnect` itself — see **Reconnect
supervision** below. Every path that
installs a proxy attachment sends `notify(terminal_set_viewport{attachment_id, rows,
cols})` before waiting for frames. The status bar shows the active transport.

**Focus follows control**: focusing a pane sends `terminal_take_control`; focus change
sends `terminal_release_control` and keeps the pane attached; `terminal_lease_lost`
renders the herdr "observing" treatment with the take-back key; a keystroke on a pane
the client does not hold is held as the single `pending_input` while the triggered
take-control is in flight, sent once under the installed generation on `granted:true`,
discarded with the reason in the status bar on `granted:false`, deadline, lease loss,
detach, or generation change; further keystrokes during the pending request are refused
locally. A control deadline is not the end of the story, because 2.1.14 tombstones the
control scope of an attachment whose take-control bytes already reached the socket: the
pane would otherwise sit read-only forever, since every later take and release on that
`attachment_id` is refused `ControlScopeIndeterminate` and no lease event ever arrives to
clear it. So the pane treats that refusal as an attachment-level failure and retires the
attachment through the transition it already has — it enters `Detaching{old_attachment_id}`,
sends `terminal_detach`, waits for either `terminal_detach_result` or the lifecycle
`terminal_attachment_finalized` under the same 2 s bound, and re-attaches; the fresh
`attachment_id` carries an untombstoned control scope, and control is reacquired through
the ordinary take-control exchange. No new recovery machinery is introduced: this is the
same detach/finalize-or-reconnect/fresh-attach path a direct-frame failure uses. The reducer ignores `terminal_control_result` / `terminal_lease_lost` /
`terminal_attach_result` with a `lease_generation` below the highest applied for that
attachment; `terminal_attachment_finalized` is applied idempotently by exact id.
`terminal_write_outcome`: `delivered` keeps the holder writable; `indeterminate` enters
uncertain read-only and resends nothing; `refused`, `write_seq_conflict`,
`write_seq_expired`, `write_seq_capacity` clear the in-flight mark without resending.
Attention prompts render through the imported dialog chrome and answer through
`Daemon::respond`; a 409 stale episode is shown, never retried by keystroke.

**Spawn and terminate**. The `Daemon` trait carries `spawn` and `terminate`, and 2.1
proves them against the mock, but no UI action reaches them, so a client that can view
and drive terminals cannot create or destroy one — and the source plan's
`select_spawn_attach_terminate_loop` has no carrier. Bind both: a spawn key opens the
imported spawn prompt for the selected project and calls `Daemon::spawn`, and a
terminate key on the focused pane calls `Daemon::terminate` for its terminal id, behind
the same confirmation treatment herdr's chrome already uses for destructive keys. The
reply is not the state change. `spawn` returning `SpawnOutcome` records the pending
terminal id and the pane appears only when the row reconciles in through the lifecycle
stream or the next listing, which keeps one code path for locally-initiated and
externally-initiated rows; `SpawnOutcome::Refused{reason}` shows the reason in the status
bar and creates no pane. `terminate` returning `KillOutcome` marks the pane terminating
and read-only, and the pane is removed when the lifecycle removal for that terminal
arrives — never on the reply alone, so a terminate whose row survives leaves a visible
pane rather than a silent hole. Both actions are suppressed by the exit latch like every
other request.

That design only settles if the authoritative signal is guaranteed, and today it is not:
`_handle_terminal_create` and `_handle_terminal_kill` send `terminal_create_result` and
`terminal_kill_result` and publish no lifecycle event at all, so a client waiting for
reconciliation waits forever. 1.4 therefore also owns those two handlers and publishes a
stamped `terminal_event` for a successful create — carrying the full row, resolved
through the same by-id read the REST route uses, so one code path serves
locally-initiated and externally-initiated rows — and a stamped exit event for a
successful kill. The reply and the event are independent, and the client tolerates
either order: a create whose event precedes its reply reconciles the pane and then
matches the pending id; a kill whose reply precedes its event marks the pane terminating
and removes it on the event. Duplicate events are idempotent by id, and a lost event is
recovered by the next listing or reconnect reconciliation, which stays the backstop
rather than the primary path. Fixing it in the daemon rather than by having the client
re-list after each reply also fixes the browser, which has the same gap.

**Reconnect supervision** (`app/run_loop.rs`). `LiveDaemon::reconnect` performs one
attempt; the named budget of 5 attempts with 250 ms → 4 s doubling is a *policy*, and a
policy with no owner is a policy two leaves implement differently while both pass the
single-flight test. One component owns it: a `ReconnectSupervisor` living in the run
loop, the **only** caller of `LiveDaemon::reconnect` anywhere in the client and the
single place the budget is counted.

Ownership admits no exemption, including the detach-deadline path. A pane whose
`Detaching` deadline expires submits a recovery intent carrying the generation it
observed and awaits the episode result the supervisor publishes; the supervisor
coalesces every intent naming the same observed generation into one episode. A
detach-deadline caller that reached `reconnect` directly would be the ordinary case, not
an edge case — a daemon drop expires every attached pane's deadline at once — and each
such call spends budget the supervisor is not counting, so the client would exhaust
five attempts' worth of sockets while the supervisor believes it is on its first. The
intent channel is what makes the budget real.

Its semantics, complete. Attempts are counted per disconnected episode rather than per
pane, so five panes noticing the same loss consume one budget. The first attempt is
issued **immediately**, with no preceding delay; the supervisor then sleeps 250 ms,
500 ms, 1 s, and 2 s before attempts two, three, four, and five respectively. Five
attempts have four inter-attempt sleeps, and the fifth failure ends the episode with no
trailing sleep — there is no 4 s delay in the schedule, only a 4 s clamp ceiling. A
`DaemonError::Unavailable{retry_after}` returned by failure N replaces the computed
delay before attempt N+1 and nothing else, clamped into `[250 ms, 4 s]` so a hostile or
absent value can neither stall nor spin the client; a `retry_after` on the fifth and
final failure is discarded, because no attempt follows it. The counter and the delay
schedule reset only when `Workspace` reports its roster handshake complete for the new
generation, never at generation-ready alone. Generation-ready is the transport saying the
socket is connected and serving, which is one step short of usable: the client stays
read-only until `Workspace` has re-listed, installed, and replayed. Resetting at the earlier
boundary would turn a daemon that accepts sockets and then fails every listing into an
infinite loop — each connect restores the full budget while every pane stays read-only
forever, which is precisely the state the budget exists to terminate. So a typed failure in
`Workspace`'s handshake (`Unauthorized`, `NotFound`, `Unavailable`, `Protocol`, or a
malformed page) is fed back into the *same* episode as that attempt's failure, spends the
same budget, and follows the same schedule; the episode ends successfully only once install
and replay succeed, and that is the moment counter and schedule reset. One supervisor, one
budget, two stages inside it. Failure on the fifth
attempt is daemon loss: it sets the exit latch immediately and enters the shutdown seam
with that reason logged. Cancellation is immediate — when the exit latch sets during a
backoff sleep the supervisor abandons the sleep and issues no further `reconnect`.

**Daemon loss is immediately read-only, and rendering continues.** Source 3.3.2 requires
that "an unreachable daemon leaves panes rendering frames while read-only," and the
retry budget above makes that window long — up to five attempts with backoff before the
client gives up. Leaving pane state unspecified for that window is what allows the worst
behaviour: a pane that still believes it holds the lease, accepting keystrokes that go
nowhere or arrive at a terminal whose ownership has since changed.

On daemon reader loss the client therefore transitions at once, before the first
reconnect attempt. The trigger is 2.1's `DaemonEvent::Disconnected { generation, error }`,
which the run loop consumes off the subscription it already selects over: on receipt it
clears every lease and control state, discards `pending_input` with the reason shown, and
suppresses terminal writes and control requests for the duration — and only then does it
submit the generation-tagged recovery intent to the `ReconnectSupervisor`, so the
read-only transition is ordered strictly ahead of the first reconnect attempt rather than
racing it. Direct `FrameSource` panes keep receiving and rendering throughout —
their frames come from the host socket, which the daemon's absence does not touch, and
that is precisely the property the source clause names. Proxy panes have no frame path
without the daemon and show the disconnected treatment. On generation-ready the client
does not resume its old state. `Workspace` re-runs its own subscribe-first handshake for
the new generation — re-subscribe, re-list the project, re-pin the snapshot, replay — and
only then re-attaches: old attachment ids are invalid, so each shown pane performs a fresh
attach and then reacquires control through the ordinary take-control exchange, exactly as
after any other reconnect. Generation-ready is the transport saying the socket serves
again; the roster is reconciled by the side that owns it. On budget exhaustion the exit latch sets
and the shutdown seam runs.

**Exit latch** (`app/mod.rs` state, set in `app/run_loop.rs`, read in `app/attach.rs`).
Every non-panic exit cause sets one `exiting` latch before leaving the select loop, and
once latched the loop and the attach state machine issue no reconnect, reattach,
take-control, release-control, viewport, or write request; later exit notifications are
inert. The latch is owned here rather than in 3.3 because the transitions it suppresses
are all in this deliverable's carriers — the run loop, the attach state machine, and the
reducer — and an owner that cannot reach the request-producing code cannot enforce the
guarantee. 3.3 consumes the latch: it owns what happens *after* it sets, the ordered
`shutdown(workspace, daemon, deadline)` phase and the local terminal restore.

**Acceptance:**

- 3.2.1 - Against the scripted daemon and scripted frame source the loop renders frames into the pane grid, routes a keystroke to `terminal_input` only when the client holds the lease, keeps rendering and applying events across idle ticks, and exits on the quit key with the already-armed `TerminalGuard` restored — its stage-aware successor is 3.3's to deliver. test: `crates/gclient/tests/client_loop.rs::loop_routes_input_and_frames`.
- 3.2.2 - Focus-follows-control: take on focus, release on focus change with the pane still attached, read-only with take-back on `terminal_lease_lost`, a lower-generation delayed grant ignored, the triggering keystroke sent exactly once on `granted:true` and discarded with a shown reason on every other exit. test: `crates/gclient/tests/client_loop.rs::focus_moves_control_and_settles_pending_input_once`.
- 3.2.3 - Direct→proxy fallback: after a direct-frame failure the pane detaches the old id, advances on either finalization signal, attaches afresh with `frame_delivery: "proxy", encoding: "semantic_frame"`, notifies the viewport, installs the new id and generation, drops late events for the tombstoned id, holds no control until take-control succeeds; a swallowed finalization expires the 2 s deadline, reconnects, and re-attaches on generation-ready with exactly one attach per pane. test: `crates/gclient/tests/client_loop.rs::proxy_fallback_uses_fresh_attachment`.
- 3.2.4 - `terminal_write_outcome` transitions: `delivered` writable, `indeterminate` uncertain read-only with no resend, refusals and seq errors clear the in-flight mark without a second write; `terminal_attachment_finalized` mid-fragment drops the stale slice. test: `crates/gclient/tests/client_loop.rs::write_outcomes_drive_pane_state`.
- 3.2.5 - An actionable prompt in the roster opens the imported dialog and its answer reaches `Daemon::respond` with the correct `attention_id`; a 409 renders the stale-episode notice. test: `crates/gclient/tests/attention_flow.rs::respond_reaches_daemon`.
- 3.2.6 - `input.rs` encodes arrows, function keys, and modifier combinations to the bytes crossterm's kitty and legacy protocols define, and the loop uses it for every keystroke. test: `crates/gclient/tests/client_loop.rs::input_encoder_covers_named_keys`.
- 3.2.7 - Under a paused clock the supervisor issues attempt one with no preceding delay, then sleeps exactly 250 ms, 500 ms, 1 s, and 2 s before attempts two through five, and ends the episode on the fifth failure with no trailing sleep — five `reconnect` calls and four sleeps for one episode, however many panes observed the loss; a completed `Workspace` roster handshake between attempts resets both counter and schedule while a bare generation-ready does not, so a daemon whose socket connects on every attempt but whose listing fails each time exhausts the same five-attempt budget instead of resetting it; an `Unavailable{retry_after}` from failure N replaces only the sleep before attempt N+1, clamped into `[250 ms, 4 s]`, and one returned by the fifth failure is discarded; the fifth failure sets the exit latch and enters the shutdown seam once; and setting the latch during a backoff sleep abandons the sleep with no further `reconnect`. test: `crates/gclient/tests/client_loop.rs::reconnect_supervisor_counts_delays_resets_and_cancels`.
- 3.2.8 - After the exit latch sets, an exit injected during reconnect and an exit injected during direct→proxy fallback each produce no replacement attachment and no take-control, release-control, viewport, or write request; a lifecycle event arriving post-latch changes no pane state. test: `crates/gclient/tests/client_loop.rs::latched_exit_issues_no_further_requests`.
- 3.2.9 - Three panes whose `Detaching` deadlines expire concurrently in the run loop each submit a recovery intent and each receive the same episode result; the scripted daemon observes exactly one `reconnect` per attempt and the supervisor's counter accounts for every call, with no pane reaching `LiveDaemon::reconnect` directly. test: `crates/gclient/tests/client_loop.rs::detach_deadlines_recover_through_the_supervisor`.
- 3.2.10 - From a selected project, the spawn action issues `Daemon::spawn`, the resulting row reconciles into a new pane, that pane attaches and renders its first frame, the terminate action issues `Daemon::terminate` for it, and on the lifecycle removal the pane is dropped and the roster converges while a second pane keeps streaming frames throughout; `create_result_refused` shows the refusal reason and creates no pane. The same convergence holds under every schedule: reply-before-event, event-before-reply, a duplicate event, and a lost event recovered by the next listing or by reconnect reconciliation — in each case exactly one pane appears or disappears and no action stays pending. test: `crates/gclient/tests/client_loop.rs::select_spawn_attach_terminate_loop`.
- 3.2.11 - On daemon reader loss `LiveDaemon` emits exactly one `DaemonEvent::Disconnected` for that generation after clearing readiness and failing its waiters and before waking any reconnect joiner; with the supervisor's first attempt paused, the run loop has already cleared lease and control state and discarded `pending_input` by the time that attempt is admitted, and for the whole backoff window a direct pane keeps receiving and rendering host frames while every keystroke, control request, and write is suppressed rather than queued; on generation-ready `Workspace` re-lists and replays before any pane attaches, panes stay read-only until that handshake succeeds, each shown pane then performs a fresh attach and reacquires control through take-control rather than resuming its old lease, and on budget exhaustion — counted across socket failures and roster-handshake failures alike — the exit latch sets. test: `crates/gclient/tests/client_loop.rs::daemon_loss_renders_read_only_until_recovery`.
- 3.2.12 - A take-control whose bytes reached the socket and then hit the control deadline leaves that attachment's control scope tombstoned: the pane retires the attachment through the bounded `Detaching` path, re-attaches, and reacquires control on the new `attachment_id`, and no take or release is ever retried against the tombstoned id. test: `crates/gclient/tests/client_loop.rs::control_tombstone_retires_the_attachment`.

### 3.3 Copy mode, paste, persistence, teardown, and logging [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/copy_mode.rs::*` — scope-reason: real history navigation and selection replace the stub
- `crates/gclient/src/persist.rs::*` — scope-reason: the snapshot carries the layout tree and is quarantined on corruption
- `crates/gclient/src/teardown.rs::*` — scope-reason: the guard is split from the async shutdown phase
- `crates/gclient/src/logging.rs`
- `crates/gclient/src/lib.rs`
- `crates/gclient/tests/copy_paste.rs::*` — scope-reason: the tests run against the real copy mode and frame sources
- `crates/gclient/tests/persist.rs::*` — scope-reason: layout round-trip and corrupt-file quarantine
- `crates/gclient/tests/teardown.rs::*` — scope-reason: panic, signal, and graceful shutdown ordering
- `crates/gclient/src/app/mod.rs::*` — scope-reason: Workspace layout, tab-order, and focus mutation paths invoke atomic snapshot persistence

Copy mode reads history through the frame source (`AttachHistory` for tmux panes,
`SetScrollOffset{rows_from_live_edge}` + `ScrollOffsetApplied` for native panes),
selects with `gobby_terminal::selection`, copies through OSC 52, keeps two observers at
independent offsets, shows a new-output indicator while scrolled away, and
`extract_logical_line` honours `wrap_cols` so a soft-wrapped wide-grapheme line copies
as one logical line (herdr `952729ee`; `UPSTREAM.md` already records that cherry-pick,
so this leaf implements the recorded behavior without touching the provenance file)
while a hard newline stays a boundary. Paste is
lease-gated `terminal_paste` with `paste_payload` bracketing (herdr's `src/pane.rs`
helper — a pane concern, not part of its copy mode); oversize (> 1 MiB) is
refused locally with the server's reason shape; paste into copy-search stays local.

`persist.rs` writes `~/.gobby/client/<project_id>/workspace.json` (layout tree, tab
order, focused pane, durable `terminal_id`s) atomically on change: each update is
serialized to a uniquely named temporary file in the same directory and renamed over the
snapshot, so successful replacement leaves no temporary file and any failure before
replacement preserves the last valid snapshot. Restore re-attaches
live terminals and drops dead rows; a corrupt file is renamed to `workspace.json.corrupt-<ts>`
and logged. `teardown.rs`: `TerminalModeGuard` is a synchronous idempotent RAII guard
restoring the local terminal only (leave alt screen, raw mode off, bracketed paste off,
cursor shown), armed before the loop and run on return, `?`, and panic. Arming is
stage-aware rather than all-or-nothing. Entering terminal mode is several mutations —
enable raw mode, enter the alternate screen, enable bracketed paste, hide the cursor —
and the guard today marks itself armed only in `arm()`, after the backend's `enter()`
has returned as a whole (`enter()` itself never touches the armed flag), so a
failure in any later stage returns through `?` with the earlier stages already applied
and `Drop` believing there is nothing to restore: the user is left in raw mode with no
TUI. Rollback responsibility must exist before the first mutation, not after the last.
The guard records the obligation as it goes — it is constructed unarmed, and each stage
marks its own completion in the guard immediately after that stage succeeds — so `Drop`
restores exactly the stages that completed, in reverse, whether `enter()` finished or
failed halfway. Restoration stays idempotent, so a partial rollback followed by the
normal exit path restores nothing twice.

Restoration itself can fail, and the completion marks must survive that. A stage's mark
is an *outstanding obligation*, cleared only after that stage's restoration actually
succeeds — clearing it on attempt rather than on success would let one failed
`disable_raw_mode` be recorded as done and never retried, which is the same abandoned
terminal the stage-aware arming exists to prevent. Restoration walks the outstanding
obligations in reverse and is best-effort across them: a failure to leave the alternate
screen must not skip disabling raw mode, because raw mode is the stage whose loss makes
the shell unusable. Every obligation that fails stays outstanding, so a later `Drop`
retries it after an explicit restore already ran. Explicit restoration returns the
aggregate of the errors it hit and the caller logs them; `Drop` never propagates and
**never panics** — a panic in `Drop` during unwind aborts the process and guarantees the
terminal is never restored, turning a recoverable cleanup failure into the worst
available outcome. Remote cleanup
is the async `shutdown(workspace, daemon, deadline)` phase the loop runs on quit,
`SIGINT`/`SIGTERM`/`SIGHUP`, and daemon loss — release every held lease, detach every
attachment, await results, and call `Daemon::close` (the trait member 2.1 adds for exactly
this step, which fails and clears any waiter the deadline left outstanding), then drop the
runtime so the guard's local restore happens last. The 2 s deadline is one **outer** bound
over that whole sequence, not a bound on the detach phase alone. `close` is an await like
the others — 2.1 has it cancel and join an in-flight reconnect episode, stop the reader, and
close the sink — so a deadline that ended before `close` was called would leave the exit
unbounded at exactly the step meant to guarantee it. On expiry the phase stops awaiting,
aborts the reconnect and reader tasks and drops the sink outright, and proceeds directly to
the synchronous terminal restore; a panic skips the async phase by design and the
daemon finalizes on socket close. 3.2 owns the `exiting` latch and the suppression of
every request-producing transition once it sets, because those transitions live in its
run loop, attach state machine, and reducer; this deliverable owns what the latch leads
to. Every non-panic exit cause enters the same `shutdown(workspace, daemon, deadline)`
call exactly once, and quit, `SIGINT`, `SIGTERM`, `SIGHUP`, and reconnect-budget
exhaustion differ only in the reason logged. Panic remains the stated RAII-only path.
`logging.rs` routes `tracing` to
`~/.gobby/logs/gclient.log` with daily rotation and nothing to stdout.

**Acceptance:**

- 3.3.1 - Copy from history: a later-joining pane seeds copy mode from the `AttachHistory` it received, two observers hold independent offsets, a native pane scrolls through `SetScrollOffset`, wide-grapheme soft-wrapped lines copy as one logical line and a hard newline does not, no PTY resize, input, or tmux mutation occurs, the selection leaves the client as the exact OSC 52 payload, and live output arriving while an observer is scrolled away renders the new-output indicator without moving that observer's offset. test: `crates/gclient/tests/copy_paste.rs::scrollback_copy_is_lease_independent`.
- 3.3.2 - Paste with the lease arrives as one bracketed unit through `terminal_paste`; without the lease it is refused and nothing is sent; oversize is refused typed; `indeterminate` enters uncertain read-only without resend; copy-search paste stays local. test: `crates/gclient/tests/copy_paste.rs::paste_is_lease_gated_and_bracketed`.
- 3.3.3 - Workspace layout round-trips through `workspace.json`, restore drops dead terminals, and a corrupt file is quarantined without crashing. A reader racing repeated saves observes only complete old-or-new JSON, a successful save leaves no temp file, and an injected replacement failure preserves the previous valid snapshot. test: `crates/gclient/tests/persist.rs::workspace_round_trip_and_corrupt_file`, `crates/gclient/tests/persist.rs::workspace_write_is_atomic`.
- 3.3.4 - On quit and on `SIGTERM` the async shutdown releases every lease and detaches every attachment with awaited results within the deadline before the runtime drops, the guard restores after; a daemon that never answers does not hang the exit past the deadline at any stage — with a permanent stall injected in turn at the detach await, at `Daemon::close`'s reconnect-episode join, at reader shutdown, and at sink close, each run still exits within the bound with the terminal restored; on panic the guard restores synchronously and nothing async is awaited from `Drop`. test: `crates/gclient/tests/teardown.rs::graceful_exit_releases_and_detaches_within_deadline`.
- 3.3.5 - Logs land in `~/.gobby/logs/gclient.log` and nothing is written to stdout while the TUI runs. file: `crates/gclient/src/logging.rs`.
- 3.3.6 - Every named non-panic exit cause produces the same ordered trace — latch exit, release held leases, detach attachments, await or hit the deadline, close the WS, restore the local terminal — exactly once; panic performs only synchronous guard restoration. test: `crates/gclient/tests/teardown.rs::every_exit_cause_uses_one_shutdown_seam`.
- 3.3.7 - With a failure injected after each terminal-mode entry stage in turn — after raw mode is enabled, after the alternate screen is entered, after bracketed paste is enabled — `enter()` returns the error and the guard restores exactly the stages that completed and no others, leaving the terminal in its pre-entry state each time; a successful entry followed by the normal exit restores each stage exactly once. test: `crates/gclient/tests/teardown.rs::partial_arming_rolls_back_completed_stages`.
- 3.3.8 - With a failure injected at each restore stage in turn — leaving the alternate screen, disabling raw mode, disabling bracketed paste, showing the cursor — restoration continues through the remaining stages instead of stopping at the first error, the failed stage stays outstanding while every succeeding stage is cleared, a subsequent `Drop` retries only the still-outstanding stages, explicit restoration returns the aggregate error and it is logged, and `Drop` neither propagates nor panics under a failing restore during unwind. test: `crates/gclient/tests/teardown.rs::restore_failures_stay_outstanding_and_never_panic`.
- 3.3.9 - Shutdown closes the daemon through the trait: `Daemon::close` is called exactly once after the bounded detach phase and before the runtime drops, no request or reconnect is issued after it returns, and a deadline that leaves a detach unanswered still ends with every waiter failed and cleared by the close. test: `crates/gclient/tests/teardown.rs::shutdown_closes_the_daemon_through_the_trait`.
- 3.3.10 - Workspace layout, tab-order, and focus mutations invoke atomic persistence, and a racing reader observes only complete old-or-new workspace.json snapshots. test: `crates/gclient/tests/persist.rs::workspace_mutations_persist_atomically`.

### 3.4 Startup: `--daemon-url`, `--token-file`, `--version`, and host-state diagnosis [category: code] (depends: 2.1, 3.2)
`kind: deliverable`

Targets:
- `crates/gclient/src/startup.rs::*` — scope-reason: argument surface and probe grow the remote and version paths
- `crates/gclient/src/views/mod.rs::*` — scope-reason: `run_ready` is the sole consumer of `Ready.project` and today branches on it as an `Option`, so making the project mandatory has to land in this module too; file-wide to match 2.2's and 3.2's ownership of the same file
- `crates/gclient/tests/startup.rs::*` — scope-reason: new flags and the version probe

`parse_args` accepts `--project`, `--daemon-url <url>`, `--token-file <path>`
(default `~/.gobby/local_cli_token`), `--version` (prints `gclient <Cargo version>` and
exits 0 — today it exits 1 with "unknown argument", so
`src/gobby/cli/install_setup_gclient.py`'s `probe_native_bin_version` can never succeed),
and `--help`. Discovery order: `--daemon-url` wins; otherwise `gobby_core::daemon_url`
from bootstrap.

**Project resolution.** `GET /api/terminals` requires `project_id` and
`workspace.json` is keyed by project, so a client that reaches terminal mode without a
concrete project UUID cannot make its first request or write its first snapshot. Startup
therefore resolves exactly one project UUID before any terminal-mode mutation, and
`Daemon::list_terminals` takes it as a required `&str` rather than an `Option`, which is
what makes the omission impossible to carry into the loop. `--project` accepts either a
project UUID or a filesystem path to a project root; a UUID is used as given, a path is
resolved through `gobby_core::project::read_project_id`. With no flag, startup discovers
the project containing the current working directory with
`gobby_core::project::find_project_root` and reads its id with `read_project_id`. There
is no global or project-less workspace: when discovery finds no project root, or the root
it finds has no readable id, `gclient` exits with an actionable error naming the
directory it searched from and telling the user to run `gobby init` or pass `--project`,
before raw mode and before any daemon request.

The resolution has one consumer and it has to change with it. `crates/gclient/src/views/mod.rs`'s
`run_ready` today reads `Ready.project` as an `Option<String>` and enters the workspace
through `if let Some(project) = ready.project`, silently skipping project selection when
it is absent. Once startup guarantees a resolved UUID, `Ready.project` becomes a plain
`String` and `run_ready` selects unconditionally; leaving the `Option` branch in place
would preserve exactly the project-less path this decision exists to make
unrepresentable. That file is 3.2's carrier for the run loop, which is why this
deliverable depends on 3.2 and takes the same file-wide scope.

**Host state.** The health probe stays before any terminal-mode mutation and, when the
daemon reports the `gterm` host absent or degraded, prints running/adopted, epoch,
restart count, and last error and refuses. `--daemon-url` narrows that refusal but does
not remove it. The reason a local degraded host is fatal is that the direct Unix-socket
path is unusable, and a remote session never attempts a direct attach anyway — so a
remote session can run proxy-only where a local one cannot. What it cannot do is run
without a host at all: `ProxyHub` sources semantic frames by handshaking `gterm` and
answers `host_unavailable` when that handshake fails, so proxy frames do not exist
independently of the host. The exception is therefore conditioned on the remote host
being *usable*.

Both "proxy-capable" and "degraded" must be derived from the payload the daemon already
sends, because neither is a field. `TerminalHostManager.health_state` returns exactly
`enabled`, `running`, `adopted`, `host_epoch`, `protocol_version`, `restart_count`,
`backoff_seconds`, `live_terminals`, `orphaned_terminals`, and `last_error`, surfaced as
`gterm_host` by `src/gobby/servers/routes/admin/_health.py` (and in `/api/status` from
the same module). Adding a producer-side
capability flag would be new daemon surface for a question the existing fields answer, so
`gclient` derives both:

- **proxy-capable** — `running` is true and `protocol_version` equals the version this
  build pins. Those are the two conditions under which `ProxyHub`'s `gterm` handshake
  succeeds; anything else answers `host_unavailable`. `gclient` must actually
  deserialize `protocol_version` — its `GtermHostState` today drops four of the health
  fields (`protocol_version`, `backoff_seconds`, `live_terminals`,
  `orphaned_terminals`), and this derivation plus the degraded notice need the first
  two.
- **degraded** — a *diagnostic* label, never a usability verdict. `last_error` is
  history, not current state: `TerminalHostManager` clears it only in `start()` and
  never in `_health_loop`, which sets it on a failed health check and leaves it set
  through every subsequent success. A host that hiccups once and recovers therefore
  carries that string for the rest of the daemon's life. Deriving "currently faulted"
  from it would make one transient blip permanently disable the client against a host
  that is running and answering. So a running host at the pinned version whose
  `last_error` is non-null renders a notice carrying that error, with `restart_count`
  and `backoff_seconds` as context, and is otherwise treated as fully usable.

Usability is therefore `running` and `protocol_version`, and nothing else — on both
paths. Locally, a host that is absent, not running, or at a mismatched version is the
actionable refusal, quoting the daemon-reported state including the version seen and the
version expected; a running host at the pinned version proceeds, notice and all, even
with a stale `last_error`. Under `--daemon-url` the same derivation yields a proxy-only
session with the status-bar notice, and the same three states are the same refusal.
Refusing a host that is running and answering because of a diagnostic string from an
hour ago is a worse failure than any it could prevent.

**Acceptance:**

- 3.4.1 - `gclient --version` exits 0 with `gclient <version>` and the installer's version probe reads it. test: `crates/gclient/tests/startup.rs::version_flag_prints_and_exits_zero`.
- 3.4.2 - `--daemon-url` and `--token-file` override bootstrap discovery, the bearer is read from the file, and an unreachable daemon produces the actionable error before raw mode; with no `--token-file` flag and a controlled home, startup reads `~/.gobby/local_cli_token` as the bearer, and a default token file that is missing or unreadable fails actionably before raw mode and before any daemon request. test: `crates/gclient/tests/startup.rs::daemon_url_overrides_bootstrap_before_raw_mode`.
- 3.4.3 - `gclient` derives host usability from `running` and a deserialized `protocol_version` alone, with no new producer field: a host that is running at the pinned version proceeds on both the local and `--daemon-url` paths **even when `last_error` is set from an earlier failure**, showing a notice that carries that error rather than refusing; a host that is absent, not running, or running at a mismatched `protocol_version` is the same actionable refusal on both paths, naming the version seen and the version expected. `last_error` never by itself makes a matching running host unusable. test: `crates/gclient/tests/startup.rs::test_reports_degraded_host_state` (the test exists today asserting the old blanket refusal and is rewritten to this contract).
- 3.4.4 - Startup with no flag resolves the project containing the working directory and passes its UUID to the first `list_terminals`; `--project <uuid>` and `--project <path>` both resolve to that same UUID and override discovery; started outside any project root, and inside a root whose id is unreadable, `gclient` exits with an actionable error naming the search directory before raw mode and before any daemon request. test: `crates/gclient/tests/startup.rs::project_is_resolved_before_raw_mode`.
- 3.4.5 - `run_ready` takes `Ready.project` as a `String` and selects that project unconditionally, with no `Option` branch that can enter the workspace project-less. test: `crates/gclient/tests/startup.rs::ready_carries_a_resolved_project`.

## P4: Verification
`kind: framing`

**Goal**: the imported chrome is pinned by herdr's own render tests, whole screens are
pinned by gclient goldens, and the client is proven end to end against an isolated daemon
and a live host over all three transports.

### 4.1 Port herdr's keep-set component render tests [category: test] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gclient/tests/parity.rs`
- `crates/gclient/tests/parity/mod.rs`
- `crates/gclient/tests/parity/fixtures.rs`
- `crates/gclient/tests/parity/token_map.rs`
- `crates/gclient/tests/parity/sidebar.rs`
- `crates/gclient/tests/parity/panes.rs`
- `crates/gclient/tests/parity/tabs.rs`
- `crates/gclient/tests/parity/dialogs.rs`
- `crates/gclient/tests/parity/navigator.rs`
- `crates/gclient/tests/parity/status.rs`
- `crates/gclient/tests/parity/chrome.rs`
- `crates/gclient/tests/parity/upstream_tests.txt`
- `crates/gclient/UPSTREAM.md`

herdr at the pinned commit carries 147 render tests under `src/ui` and `src/ui.rs` that
render into a `TestBackend` and assert row text. 21 belong to the dropped `mobile.rs`,
`onboarding.rs`, `release_notes.rs`, and `menus.rs`, and 13 more live in keep-set
modules but render surfaces 3.1 drops — worktree dialogs, the sidebar's worktree and
git-space workspace grouping, and mobile layouts — leaving a ported keep-set of 113
(`sidebar.rs` 35, `ui.rs` 31, `panes.rs` 17, `sidebar/tokens.rs` 6, `navigator.rs` 5,
`tabs.rs` 5, `dialogs.rs` 4, `status.rs` 4, `keybind_help.rs` 2, `tab_surface.rs` 2,
`text.rs` 2). Port every keep-set test with its row-text
expectations verbatim; `fixtures.rs` maps herdr state 1:1 onto Gobby state (agent →
roster entry / terminal row, workspace → gclient workspace, attention → attention
prompt); `token_map.rs` normalises colours so glyphs, layout, truncation, and focus
junctions must match while theme values are the one allowed divergence. Tests of dropped
modules and the 13 pinned dropped-surface tests are not ported and are listed in
`UPSTREAM.md`.

`parity/upstream_tests.txt` is the completeness oracle, and an oracle the same leaf
authors is not one. If the file, the ported tests, and the `UPSTREAM.md` counts are all
written by this deliverable, a test omitted from all three is invisible: every
cross-check passes and 21 fewer tests ship than the keep-set requires. The file is
therefore pinned to a tree outside the leaf's control. It is the deterministic extraction
of herdr commit `346411fa21afd297f5ed3b3fa56f9e3fbf7654b7`, generated by exactly this
rule: take every path from `git ls-tree -r <commit> -- src/ui src/ui.rs`, drop
`src/ui/mobile.rs`, `src/ui/onboarding.rs`, `src/ui/release_notes.rs`, and
`src/ui/menus.rs`, and in each remaining file match every `#[test]` or `#[tokio::test]`
attribute and the name of the `fn` it precedes (allowing intervening attributes and the
`pub` and `async` qualifiers — `tab_surface.rs`'s render tests are all `#[tokio::test]`,
so a rule matching only `#[test]` would silently port none of them); emit one
`<source_path>::<test_name>` per line, remove the 13 pinned dropped-surface identities
below — keep-set-file tests of the worktree, git-space-grouping, and mobile surfaces
3.1 drops, pinned by exact name because a pattern rule would silently widen — and sort
ascending, newline-terminated:

- `src/ui.rs::configured_mobile_width_threshold_controls_layout_switch`
- `src/ui.rs::mobile_background_tabs_use_mobile_terminal_area`
- `src/ui.rs::mobile_config_diagnostic_keeps_command_visible`
- `src/ui.rs::mobile_width_uses_header_and_full_width_terminal`
- `src/ui/dialogs.rs::new_worktree_error_renders_fatal_stderr_line`
- `src/ui/dialogs.rs::new_worktree_hit_test_geometry_matches_modal_size`
- `src/ui/sidebar.rs::desktop_worktree_connector_uses_full_list_at_viewport_boundary`
- `src/ui/sidebar.rs::desktop_worktree_tree_aligns_parents_and_marks_children`
- `src/ui/sidebar.rs::linked_only_worktree_members_do_not_form_parentless_group`
- `src/ui/sidebar.rs::space_row_gap_preserves_compact_worktree_children`
- `src/ui/sidebar.rs::workspace_list_entries_group_multiple_workspaces_in_same_git_space`
- `src/ui/sidebar.rs::workspace_list_entries_group_non_contiguous_explicit_members`
- `src/ui/tab_surface.rs::mobile_full_app_semantic_frame_is_characterized`

Run
against that commit this yields 113 lines whose SHA-256 is
`6d3cb09874a9c2b47a0b6982b4a1ccb920c77e435933bfada7e26d9ef116d412`, recorded here at plan
revision time from the pinned tree. `mod.rs` asserts that digest over the committed
`upstream_tests.txt` before comparing anything, then compares the exact set of ported
`source_path::test_name` identities against it and reports the per-module counts recorded
in `UPSTREAM.md`. Editing the inventory to match an incomplete port breaks the digest, so
the three artifacts can no longer agree with each other while disagreeing with upstream.

**Acceptance:**

- 4.1.1 - Every keep-set render test exists under `crates/gclient/tests/parity/` with unchanged row-text expectations and passes. test: `crates/gclient/tests/parity.rs`.
- 4.1.2 - `parity/upstream_tests.txt` has 113 lines and SHA-256 `6d3cb09874a9c2b47a0b6982b4a1ccb920c77e435933bfada7e26d9ef116d412`, the ported identity set equals it exactly, and its per-module counts equal `UPSTREAM.md`. Omitting a keep-set identity fails even when the inventory and `UPSTREAM.md` are edited together to match the shortened port, because the digest no longer matches the pinned tree; removing, duplicating, or substituting a test fails even when the total count is unchanged. test: `crates/gclient/tests/parity/mod.rs::ported_set_matches_upstream_inventory`.
- 4.1.3 - A glyph or alignment change in an imported module fails its parity test while a theme-value change does not. file: `crates/gclient/tests/parity/token_map.rs`.

### 4.2 gclient screen goldens [category: test] (depends: 3.3, 4.1)
`kind: deliverable`

Targets:
- `crates/gclient/tests/screens.rs`
- `crates/gclient/tests/fixtures/screens/empty_workspace.txt`
- `crates/gclient/tests/fixtures/screens/roster_attention.txt`
- `crates/gclient/tests/fixtures/screens/split_live.txt`
- `crates/gclient/tests/fixtures/screens/help_dialog.txt`

Render four scripted states — empty workspace; roster with one attention prompt; split
panes with a live (scripted) frame; keybind help with a dialog open — into a 120×40
`TestBackend` through the real `render_workspace`, serialise one line per row with cells
mapped through `token_map`, and compare against the committed captures. Regeneration is
`GOBBY_UPDATE_SCREENS=1 cargo nextest run -p gobby-client --test screens`, and a
regenerated capture must be byte-identical across two consecutive runs.

**Acceptance:**

- 4.2.1 - The four captures are produced deterministically (two runs byte-identical) and any chrome change outside the terminal-content region fails the test. test: `crates/gclient/tests/screens.rs::screens_match_committed_captures`.

### 4.3 End-to-end: gclient against an isolated daemon and live host over three transports [category: test] (depends: 3.4, 3.3, 2.2, P1)
`kind: deliverable`

Targets:
- `tests/e2e/test_terminal_client_stack.py::*` — scope-reason: today a single Python-WsSession test that never spawns the gclient binary; it stays and the gclient PTY workflows are added
- `tests/e2e/gclient_driver.py`

`gclient_driver.py` starts the installed `~/.gobby/bin/gclient` in a 120×40 PTY with the
stdlib `pty`/`os` modules (no new dependency), reads the screen with a deadline, and
sends keys. Against the module's isolated daemon (temporary state, ports, and database;
`gterm` installed via new inode; `GOBBY_NATIVE_BIN_DIR` honoured) add:

- `test_gclient_reaches_workspace`: `gclient --project <tmp>` reaches the workspace
  screen (sidebar title and status bar present), never exits 0 after the probe.
- `test_gclient_renders_tmux_row_through_host`: a tmux terminal created through
  `terminal_create` appears in the roster; selecting it renders the pane's `echo
  GCLIENT-ROW-OK` output through the host observer (direct transport in the status
  bar).
- `test_gclient_renders_native_row_direct_and_types`: a `backend: native` terminal
  renders direct; taking control and typing `echo GCLIENT-NATIVE-OK` shows the output;
  `\x03` interrupts a `sleep` (1.1 through the client).
- `test_gclient_remote_session_uses_proxy`: `gclient --daemon-url http://127.0.0.1:<port>
  --token-file <tmp token>` against the same daemon renders the native row through the
  proxy in `semantic_frame` encoding (status bar shows proxy), takes control, and types.
- `test_gclient_direct_failure_falls_back_to_proxy`: killing the host's frame socket
  connection for an attached pane moves that pane to proxy without losing the roster,
  and the old attachment is finalised on the daemon.
- `test_gclient_remote_refuses_absent_host`: with the daemon's `gterm` host stopped, the
  same `--daemon-url` invocation exits with the actionable refusal quoting the reported
  host state instead of reaching a workspace whose panes can never receive a frame. The
  paired case is `test_gclient_remote_session_uses_proxy` above, which runs with the host
  up; together they cover both sides of 3.4's narrowed remote exception.

**Acceptance:**

- 4.3.1 - `gclient` started against the isolated daemon reaches the workspace screen. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_reaches_workspace`.
- 4.3.2 - A tmux row renders through the host observer and a native row renders direct, with control taken and keystrokes delivered as key codes. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_renders_native_row_direct_and_types`.
- 4.3.3 - A `--daemon-url` session against a host the daemon reports `running` at the pinned `protocol_version` renders through the cell-mode proxy and can type; the same invocation against a stopped host, and against a host reporting a mismatched `protocol_version`, each exit with the actionable refusal quoting the reported state and never reach the workspace. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_remote_session_uses_proxy`, `tests/e2e/test_terminal_client_stack.py::test_gclient_remote_refuses_absent_host`.
- 4.3.4 - A direct-frame failure falls back to proxy with the old attachment finalised on the daemon. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_direct_failure_falls_back_to_proxy`.
- 4.3.5 - Selecting a tmux terminal in gclient renders its output through the gterm host observer and reports the direct transport. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_renders_tmux_row_through_host`.
- 4.3.6 - Against the isolated daemon, the spawn action creates a real terminal row that the daemon lists, gclient reconciles it into a pane and renders its first frame, the terminate action removes the row from the daemon's listing, and the pane disappears from gclient while a second pane keeps rendering. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_spawns_and_terminates_a_terminal`.
- 4.3.7 - A running host with a mismatched protocol_version causes --daemon-url to exit before workspace connection and reports the expected and observed versions. test: `tests/e2e/test_terminal_client_stack.py::test_gclient_remote_refuses_protocol_mismatch`.

### 4.4 Documentation: client status, remote use, and Guard set H [category: docs] (depends: 4.3)
`kind: deliverable`

Targets:
- `docs/guides/gterminal-development-guide.md`
- `docs/contracts/gterm-protocols.md`

Replace the guide's "Landing status" with a "Client status" section (what `gclient`
does, the three transports, `--daemon-url` remote use with the operator steps: bind the
daemon to its tailnet address, copy `local_cli_token`, run `gclient --daemon-url`),
add a "Guard set H" section carrying the block reproduced below, and document the
`terminal_attach.encoding` / `terminal_frame` / `direct` / `snapshot` additions in
`docs/contracts/gterm-protocols.md`. That contract today documents only the host
protocols and has no daemon WS message table, so 4.4.2 creates the table rather than
extending one.

The Guard set H text to write into the guide is reproduced here in full rather than
referenced, because `## Constraints` is a framing section and expansion carries only a
deliverable's own section body into its leaves — an instruction to "copy it verbatim from
`## Constraints`" is unfollowable by the agent that executes this leaf. The `## Constraints`
copy remains the plan-level framing; this is the authoritative text to check in:

> **Guard set H.** Every leaf's close gate runs from the `0.5.0` checkout with
> `DATABASE_URL` pointed at the isolated test hub
> (`postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`) and
> `GOBBY_TEST_PROTECT=1`:
>
> 1. `cargo build --release -p gobby-client && cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings && cargo nextest run -p gobby-client`
> 2. `cargo nextest run -p gobby-terminal` (the embed suite's `gclient_views` source
>    assertion and the host contract stay green)
> 3. `uv run pytest tests/terminals tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_create.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_viewport.py tests/servers/test_native_web_proxy.py tests/servers/test_tmux_bridge_authority.py tests/servers/test_attention_respond.py tests/servers/websocket/test_broadcast.py tests/mcp_proxy/test_sessions_terminal_tools.py tests/storage/test_terminals.py` (DB-backed; `GOBBY_POSTGRES_TEST_DSN` exported)
> 4. `uv run ruff check src/ && uv run ruff format --check src/ && uv run mypy src/ && uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new`
> 5. `cd web && npx vitest run src/hooks src/components/activity`
> 6. From 4.3 close onward: `uv run pytest tests/e2e/test_terminal_client_stack.py`
>    against `gclient` and `gterm` rebuilt from the tree and installed via new inode
>    (`cp` to a dotfile, `mv -f` over the name, per this guide's § "Rebuild and
>    reinstall"). macOS kills processes that exec an in-place-overwritten signed binary,
>    so overwriting the installed path directly is not an option.
> 7. Host leak check: the set of `gterm host` PIDs after groups 2, 3, and 6 equals the
>    set before.

**Acceptance:**

- 4.4.1 - The guide carries "Client status" and a "Guard set H" section reproducing all seven numbered checks with the `DATABASE_URL` and `GOBBY_TEST_PROTECT` environment, the new-inode install rule, and the host-PID leak check, so a later leaf can execute the gate from the checked-in copy alone. behavior: "Client status" in `docs/guides/gterminal-development-guide.md`.
- 4.4.2 - The protocol contract gains a daemon WS message table (none exists today) documenting `encoding`, `terminal_frame`, the `direct` block, and `snapshot`. behavior: "terminal_frame" in `docs/contracts/gterm-protocols.md`.

## P5: Landing and follow-on seeding
`kind: framing`

**Goal**: the epic closes with the next planning epics along `evolution.md` created and
the architecture snapshot refreshed, so nothing deferred here floats.

### 5.1 Seed the follow-on planning epics and refresh the evolution snapshot [category: docs] (depends: P4)
`kind: deliverable`

Targets:
- `docs/architecture/evolution.md`

**Deferral tasks are settled at expansion, before this leaf runs.** This leaf consumes
already-stable D1, D2, and D3 refs and does not create, adopt, or re-point anything.

That placement is forced by the Plan-Coverage Contract, not chosen for tidiness. The
contract requires every `kind: deferred` task to carry `deferred-from:<plan-id>:<section-id>`
provenance, to be parented under this plan's own epic as tail work, and to hold a
`blocked-by` edge on each external prerequisite — and it states that "the open-task,
provenance, and dependency-closure gates above apply from expansion validation onward."
5.1 is the closing leaf and depends on all of P4, so a repair scheduled here runs long
after expansion validation has already rejected the plan for the very gaps it would fix.
Adoption therefore belongs to the expansion/finalization step that creates the other
deferral tasks, alongside them.

**Who performs it is the coordinator, by hand, through `gobby-tasks`.** That has to be
stated rather than implied, because there is no automated surface behind it: the contract
compiler serializes typed deferrals into the expansion spec (`_contract_deferrals` →
`spec["deferrals"]`), but expansion apply reads only `phases`, `tasks`, and
`dependencies` — nothing in the repository consumes the compiled field. A step named only
as "expansion/finalization" would therefore have no owner at all. Building that surface is
out of scope here: it is expansion-engine work with its own blast radius, and this plan's
own expansion could not use it anyway, since it would have to exist before this epic
expands. So the owner is the session running expansion, and the mutations are ordinary
`create_task` / `update_task` / dependency calls on the `gobby-tasks` MCP server. That
choice also sets what the closing leaf can honestly assert: end state and convergence on
rerun, checked by inspection, rather than injected-failure evidence a docs leaf could
never produce.

What that step does, per section. **D1** has no pre-existing owner, and it still needs the
same zero/one/many decision the other two get, because *create* with no lookup in front of
it is exactly what a rerun duplicates. Search for an open `planning` task carrying
`deferred-from:herdr-client-completion:D1`. On **zero**, create the epic once with
`category: planning`, that provenance, a description naming the QA-plan sections it owns
(2.3–2.9, 4.1, 4.2, 4.6, P8, P3, P5 and the flip) and the landed state it starts from,
parented as tail work, then add its `blocked-by` edge on 5.1. On **exactly one**, converge
it rather than skipping: verify and repair the parent, the description, the validation
criteria, and the edge, and mutate nothing that already holds. On **more than one**, stop
and report — the same unexpected-candidate branch D2 and D3 use. D1 has no legacy
predecessor, so this one key is its whole lookup.

**D2 and D3 both have pre-existing owners that must be adopted**, and both need the same
three repairs rather than only the label. **The section ids do not line up across the two
plans, and assuming they do is what breaks the lookup.** The previous plan numbered its
deferrals by a different ordering: `herdr-terminal-client` D1 is the *plugin* epic and
D2 is remote attach, so live #20202 carries `deferred-from:herdr-terminal-client:D2`
while live #20201 carries `deferred-from:herdr-terminal-client:**D1**` — not D3. The
mapping is therefore explicit and asymmetric: **current D2 adopts #20202 from legacy D2,
and current D3 adopts #20201 from legacy D1.** A same-section `:D3` search would return
zero and stop the step under the unexpected-candidate branch below. An exact search for
`deferred-from:herdr-client-completion:D2` or `:D3` returns zero on the current task
graph. Creating on that zero would produce a duplicate epic while the plan names the
original as its `task_ref`. Legacy adoption therefore comes before creation for both:
resolve the new provenance first, and on zero matches require an unambiguous legacy
owner — exactly one open task carrying the previous plan's provenance for the *mapped*
legacy section: `deferred-from:herdr-terminal-client:D2` resolving to #20202 for current
D2, and `deferred-from:herdr-terminal-client:D1` resolving to #20201 for current D3, each
with `category: planning`. Adoption is
idempotent and writes three things: the new `deferred-from:herdr-client-completion:<D>`
provenance alongside the legacy label, the tail-work parent under this plan's epic, and
the dependency closure. That closure is not only external. The Plan-Coverage Contract
requires a deferral task to carry "dependencies on any internal leaves the deferred work
needs" as well as its external prerequisites, and all three here are tail work by
definition: D1 is planned on the tree this epic lands, D2 consumes the proxy source and
the daemon-addressable client, D3 consumes the imported chrome and its reserved bindings.
Without an internal edge each becomes dispatchable the moment expansion creates or adopts
it, which is exactly the floating follow-up the contract forbids. Each of D1, D2, and D3
therefore takes one `blocked-by` edge on **5.1**; since 5.1 already depends on all of P4,
that single edge is the smallest ordering that guarantees the complete client epic has
landed first. On top of it, D2 keeps `blocked-by` #19600 and #19647, and D3 takes the
`blocked-by` edge on whatever public-API prerequisite it names, or none if it has no
external prerequisite. Stop and report rather than create or mutate if any check fails —
more than one legacy candidate, a candidate that is not the expected task, a task that is
closed or no longer a planning task, or more than one new-provenance match.

`original_acceptance_items` is exactly the set each typed `deferral` object declares, and
the two must not drift: **D1 = [5.1.2]**, **D2 = [5.1.2, 5.1.3]**, **D3 = [3.1.4,
5.1.3]**. Each adopted task's description and validation criteria preserve the named
artifacts of every item in its own set, so an implementer reading the adoption step and
an implementer reading the deferral object derive the same obligations.

**The adoption is a multi-write migration, so its completion marker is written last.**
Provenance, parent, content, validation criteria, and dependency edges are separate
mutations, and the provenance label is also what routes the *next* run: a crash between
writing the label and finishing the rest would send the retry down the new-provenance
path, which would find the task and do nothing, leaving it unparented or unblocked
forever. Order the work so that cannot happen. Preflight both tasks and the full
dependency closure first; add the `blocked-by` edges, which are idempotent; converge the
parent, the rewritten content, and the validation criteria; and write the new
`deferred-from:herdr-client-completion:<D>` label only after every other postcondition
holds. The new-provenance branch is not a skip — it re-verifies each postcondition and
repairs any that is missing, so a run that resolves through the label still converges a
task some earlier crash left half-migrated. Every step is idempotent, so a rerun after a
failure at any boundary reaches the same final state.

**#20202's own content is stale and is rewritten as part of adoption.** It was written
when the cell-mode proxy source was unbuilt, so its title, description, and validation
criteria still claim that source as work in scope. This plan's 2.2 delivers it. Leaving
the text as-is would have #20202 re-litigate a landed deliverable and would make its
close gate unsatisfiable-by-inspection. Rewrite it so the proxy frame source and the
daemon-addressable client are stated as **landed prerequisites**, and the remaining scope
is exactly: the hub-wide roster assembled from hub data, resolving `terminal.machine_id`
to that machine's daemon endpoint, short-lived per-terminal attach-capability tokens
minted at the daemon boundary, and the hub↔node relay. Its validation criteria are
rewritten to those four items. #20201's content stays as it is — only its provenance,
parent, and dependency edges are repaired.

This leaf's own work is the document. With the stable D1/D2/D3 refs known, update
`docs/architecture/evolution.md`: "Where we are" records the landed client and the three
transports; "Remaining path" replaces item 1 (Stage 0 rework) with the two new epics and
the Stage-3 gate they wait on; the citations table gains this plan.

**Acceptance:**

- 5.1.1 - `evolution.md` names the client as landed, lists the D1 and D2 epics by task ref in the remaining path, and cites this plan. behavior: "Remaining path" in `docs/architecture/evolution.md`.
- 5.1.2 - By the time this leaf runs, D1, D2, and D3 each resolve to exactly one open `planning` task carrying `deferred-from:herdr-client-completion:<D>` provenance, parented under this plan's epic as tail work, with its dependency closure recorded as `blocked-by` edges — one internal edge on 5.1 for each of D1, D2, and D3, plus #19600 and #19647 for D2 — so none of the three is dispatchable before this epic has landed; `evolution.md` names those stable refs and this leaf creates, adopts, and re-points nothing. behavior: "follow-on epics" in `docs/architecture/evolution.md`.
- 5.1.3 - D2 resolves to the adopted #20202, which carries `deferred-from:herdr-terminal-client:D2` beside the new `deferred-from:herdr-client-completion:D2`, and D3 resolves to the adopted #20201, which carries `deferred-from:herdr-terminal-client:D1` — the previous plan's section id for the plugin epic, not D3 — beside the new `deferred-from:herdr-client-completion:D3`; no duplicate epic exists for either, and a lookup keyed on the current section id rather than the mapped legacy one stops the step instead of creating one; #20202's title, description, and validation criteria state the cell-mode proxy source and the daemon-addressable client as landed prerequisites and scope it to the hub roster, endpoint resolution, capability-token lifecycle, and hub↔node relay; re-running adoption resolves the same refs through the new provenance and mutates nothing further; an ambiguous or unexpected legacy candidate stops the step instead of creating or mutating; and each adopted task's `original_acceptance_items` match its typed deferral object exactly — D1 [5.1.2], D2 [5.1.2, 5.1.3], D3 [3.1.4, 5.1.3]. behavior: "follow-on epics" in `docs/architecture/evolution.md`.
- 5.1.4 - Adoption is performed by the coordinator running expansion through `gobby-tasks` MCP calls, and it converges rather than skipping: re-running it against a task that already carries the new `deferred-from:herdr-client-completion:<D>` provenance re-verifies every other postcondition and repairs any that is missing — so a task left with the label but no tail-work parent, no internal `blocked-by` edge on 5.1, or an unrewritten #20202 description is brought to the same final state as a first run, and a fully converged task is mutated no further. D1's create branch is guarded by that same provenance lookup, so a second run adopts and converges the epic the first run made instead of creating a duplicate, and more than one match stops the step. behavior: "follow-on epics" in `docs/architecture/evolution.md`.

## D1 Native runtime completion
`kind: deferred`

The daemon- and host-side hardening the QA plan specified — pending-row lifecycle, typed
native failures, host respawn, `SpawnResult` tmux-alias removal (2.3–2.5); host control
safety, commit barrier, hygiene, client correctness (2.6–2.9); `WriteCoordinator` as the
single write authority and `send_keys`/attention routing (4.1, 4.2); the web input path
(4.6); host backpressure and real host-driven acceptance tests (P8); CI, packaging, and
the installer short-circuit (P3); the honest flip gate, weekly producer, and the flip
itself (P5, D1) — is a separate epic planned on the tree this epic lands. #21202 already
landed the stale-pending reaper shell; the epic starts from the landed state, not from
the QA plan's assumptions.

```yaml
deferral:
  task_ref: "#TBD-created-at-expansion"
  reason: "Native-runtime hardening and the default flip are daemon/host work with their own blast radius; the client epic consumes today's opt-in native path and must not carry a second subsystem rework."
  owner: "backend-developer"
  original_acceptance_items:
    - 5.1.2
```

## D2 Hub-wide roster, attach routing, and capability tokens
`kind: deferred`

Story B's remaining terminal pieces, and only those four: a roster of every machine's
terminals assembled from hub data, resolving `terminal.machine_id` to that machine's
daemon endpoint (which `machines` does not record today), short-lived per-terminal
attach-capability tokens minted at the daemon boundary, and the hub↔node relay. The
frame-source trait, the cell-mode proxy source, and the `--daemon-url` client this epic
ships are **landed prerequisites** rather than part of that scope; #20202's text still
claims them and is rewritten at adoption to match. Blocked by the M0 two-machine smoke
(#19600) and the hub/node authority research (#19647).

```yaml
deferral:
  task_ref: "#20202"
  reason: "Cross-machine routing and credentials depend on Stage 3 machine registration and API keys; this epic delivers the daemon-addressable client and proxy source they will reuse."
  owner: "backend-developer"
  original_acceptance_items:
    - 5.1.2
    - 5.1.3
```

## D3 Plugin system for gclient
`kind: deferred`

herdr's manifest-driven plugin packages are not imported; plugin-menu keymap entries are
reserved and hidden by 3.1 until the Gobby plugin system hosts on the public API. #20201 is
this plan's D3 but the previous plan's **D1**, so its live label is
`deferred-from:herdr-terminal-client:D1` and adoption looks it up by that legacy id, never
by `:D3`. Its scope is unchanged and its description is not rewritten; adoption repairs only
its provenance, its tail-work parent, and its dependency closure — including the internal
`blocked-by` edge on 5.1 — which the contract requires of every deferral target this plan
names.

```yaml
deferral:
  task_ref: "#20201"
  reason: "Plugins host on the public CLI + daemon API in Rust and are stage-independent; the chrome keeps their bindings reserved so the import needs no rework."
  owner: "backend-developer"
  original_acceptance_items:
    - 3.1.4
    - 5.1.3
```

## L1 Landing
`kind: verification`

Every leaf closes on `0.5.0` behind Guard set H, so there is no merge. Landing is the
close of 5.1 after: `uv run gobby install` and `uv run gobby restart` from the main
checkout with the rebuilt `gterm` and `gclient` (new inode), a `gclient` session that
attaches one tmux row and one native row locally, one `gclient --daemon-url
http://127.0.0.1:60887` session against the same daemon rendering through the proxy, a
browser attach to the same native row alongside the client, and ten minutes of
`~/.gobby/logs/` with no traceback. The last push to `0.5.0` before close runs
`rust-ci.yml`'s `gobby-client` steps; a red job is found work. Publishing `gclient-v*`
tags and Homebrew formulae stays operator work.

## V1 Plan Changelog
`kind: verification`

**Draft** `kind: verification`

- source: `.gobby/plans/herdr-foundation-landing.md`, `.gobby/plans/herdr-terminal-client.md`
  §3.3–3.6, `.gobby/plans/herdr-terminal-client-qa-fixes.md` P4–P8, the 2026-08-29 crate
  audit of `crates/gclient` on `0.5.0` at `001e243887`, `docs/architecture/evolution.md`,
  tasks #21191 #21207 #21209 #20202 #20201
- decisions: Decision Record 1–11 in `## Constraints` (confirmed 2026-08-29)
- rounds: enhancement round 1 (2026-08-29)

**Round 1** `kind: enhancement`

- enhancer_run: c0e3d0ea-07f1-4102-9b9f-0125291b93e6
- enhancer_session: 19dc5fed-3d5b-48f0-8306-11634f0ad3e2
- converged: false
- suggestions_presented: 7
- accepted:
  - E1 / better / one `exiting` latch so every named non-panic exit runs `shutdown(...)` exactly once (rung 6)
  - E2 / better / pin the direct frame channel at 256 and fail typed `FrameError::Lagged` instead of dropping frames (rung 6)
  - E3 / better / make `LiveDaemon::reconnect()` single-flight per observed generation (rung 6)
  - E4 / better / acceptance coverage for the keymap merge contract and reserved plugin actions (rung 2)
  - E5 / better / atomic workspace save via temp file plus rename (rung 4)
  - E6 / better / resolve D1/D2 by provenance so re-running the seeding step creates no duplicate epic (rung 2)
  - E7 / better / pin parity by upstream `source_path::test_name` identity rather than count alone (rung 6)
- declined:
  - none — every suggestion stopped at or below the rung its mechanism required, so no `over-mechanism` decline applied
- resolution_notes: All seven accepted by the user and folded in. New acceptance items
  2.1.7, 2.2.5, 3.1.6, 3.3.6; 3.3.3, 4.1.2 and 5.1.2 restated; `direct frame receive
  channel 256 entries` added to **Named defaults**; new Targets
  `crates/gclient/tests/keymap.rs` (3.1) and `crates/gclient/tests/parity/upstream_tests.txt`
  (4.1). Prose changes: single-flight reconnect contract in 2.1, no-drop frame rule in
  2.2, temp-plus-rename persistence and the exit latch in 3.3, identity-set parity in
  4.1, provenance-keyed create-or-reuse seeding in 5.1. No Decision Record item was
  reopened and no deferral boundary moved. Enhancement is capped at one round.

Adversarial review round 1 returned needs_review with 19 blocking findings across all three lanes (20 candidates, 1 dismissed; attestation valid, shadow manifest valid with 16 entries). All 19 were accepted; zero declines. Four factual claims were independently verified against HEAD before the vote: the golden corpus holds 31 committed fixtures rather than the 32 the plan asserted; `gobby-client` carries no TOML dependency; `TmuxMixin._handle_terminal_attach` returns its own attach result for every tmux row unless `frame_delivery` is `direct`, so it never reads `encoding` and never starts ProxyHub; and 4.2 consumes the `token_map.rs` that 4.1 creates without declaring the edge. Three findings required a decision on the shape of the fix rather than a simple accept. For `lifecycle-publication-order` the full ordered publication path was chosen over a client-only high-water, because `seq` is global across three emitters and a client-side high-water would discard a late-arriving event belonging to a different object. For `exit-latch-target-owner` acceptance 3.3.6 moves into 3.2, which already owns `run_loop.rs` and `attach.rs`, instead of widening 3.3's Targets into them; this costs zero new ownership edges and preserves the deliberate absence of shared-target coupling across the P3 chain. For `project-id-resolution`, `keymap-chord-collision`, and `retry-supervisor` the adversary's recommended answers were adopted: startup resolves one concrete project UUID before raw mode via explicit `--project` then `find_project_root` plus `read_project_id` and otherwise fails actionably; a keymap override reusing a chord already owned by another active action is rejected with a diagnostic while defaults are retained; and retry supervision is assigned to a named run-loop component with five attempts, exponential 250ms to 4s delays, clamped `retry_after`, reset on generation-ready, and immediate cancellation when the exit latch sets. Machine-applicable repairs were applied for `screen-golden-dependency` and `tmux-semantic-override`; the `exit-latch-target-owner` repair was deliberately not auto-applied because its accepted shape differs from the emitted `add_targets` form. The remaining sixteen findings were hand-applied as prose, Target, and acceptance revisions.

```json plan-review-round
{"evidence_id":"0a7b41b8-a75a-4fe7-a720-3c590c009abe","plan_hash":"8d7c6271e4332c6fa7d7d90b5105441733470a02da0373750248c2752b7e1ab4","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"01289ce19818a1292a7bcc4b43c73a4a161da0473c72250f1c6d63ed66b4d10c","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":19,"total":20},"evidence_id":"0a7b41b8-a75a-4fe7-a720-3c590c009abe","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":10,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"2beff2c456c8641e370659ab91141620ff60c1d5dd7230c3d0fdeb976e3302d1","status":"valid"},"source_digest":"4cd8259d25259c3b506b5f7bf526d9b7db28dd239f835fdf99a461fb80e03e14","version":1},"findings":[{"category":"unhandled-edge","check_key":"deferred-seeding.legacy-owner-adoption","description":"Exact lookup of `deferred-from:herdr-client-completion:D2` returns zero on the current task graph, so the plan creates a second D2 task even though it also requires re-pointing #20202 and records D2's task_ref as #20202.","finding_id":"d2-provenance-adoption","fix":"Resolve exact new provenance first. On zero matches, require #20202 to be the sole open planning task with the expected legacy D2 provenance and identity, then add the new provenance, tail-work parent, and blockers #19600/#19647. Stop on any conflicting legacy or new-provenance candidate; later runs resolve #20202 by the new provenance.","location":"Opening seeding algorithm and acceptance 5.1.2","prevention":"For each provenance migration, exercise zero, one, and many matches against live pre-migration state and specify legacy adoption before creation.","principle":"An idempotent create-or-reuse workflow must identify the designated legacy object before its create-on-zero branch.","root_cause":"The algorithm searches only the new provenance even though the named owner #20202 currently carries only the old D2 provenance.","section_id":"5.1","severity":"blocking"},{"category":"missing-requirement","check_key":"startup.project-resolution","description":"The plan leaves three unanswered questions: whether no-flag startup discovers the current repository or opens a global workspace; what values `--project` accepts and how they resolve to a UUID; and what happens outside an initialized Gobby project. Current `/api/terminals` requires `project_id`, while `workspace.json` also requires one.","finding_id":"project-id-resolution","fix":"Make startup resolve one concrete project UUID before raw mode: resolve explicit `--project`, otherwise use `gobby_core::project::find_project_root` plus `read_project_id`, and fail actionably outside a project. Make `LiveDaemon::list_terminals` require the resolved ID and add acceptance for no-flag discovery, explicit override, and the outside-project error.","location":"`parse_args`/discovery contract, `Daemon::list_terminals`, and workspace snapshot path","participating_section_ids":["2.1","3.3","3.4"],"prevention":"Trace every optional startup value through mandatory API parameters and filesystem paths, including the no-flag and outside-repository cases.","principle":"Startup must resolve every identifier required by the first API request and persistence write before entering terminal mode.","root_cause":"The plan preserves `Option<&str>` and an optional `--project` without defining how `None` becomes the required REST `project_id` and snapshot directory key.","section_id":"3.4","severity":"blocking"},{"category":"weak-testability","check_key":"parity.external-oracle","description":"The accepted identity-based parity enhancement remains self-referential: a leaf can omit the same upstream test from `upstream_tests.txt`, the port, and the count table and still satisfy 4.1.2.","finding_id":"upstream-parity-oracle","fix":"Anchor `upstream_tests.txt` to herdr commit `346411fa21afd297f5ed3b3fa56f9e3fbf7654b7` with a deterministic extraction check or a digest computed from that pinned tree during plan revision. Acceptance must fail when an upstream keep-set identity is omitted even if the ported list and `UPSTREAM.md` are changed together.","location":"`upstream_tests.txt` generation prose and acceptance 4.1.2","prevention":"For every parity inventory, pin an independently derived digest or deterministic extractor output from the named upstream revision before comparing the port.","principle":"A completeness test must compare implementation-owned output with an independently pinned oracle.","root_cause":"The inventory file, ported identities, and `UPSTREAM.md` counts are produced by the same implementation leaf and only cross-check one another.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"screens.token-map-provider-order","description":"The derived manifest can schedule screen goldens before the parity token mapper exists.","finding_id":"screen-golden-dependency","fix":"Add 4.1 to 4.2's dependency list. If 4.2 is intended to own a distinct mapper, name and target that separate artifact instead.","location":"4.2 heading dependency list and `token_map` consumer prose","participating_section_ids":["4.1","4.2"],"prevention":"For every named cross-deliverable helper, add a provider-to-consumer edge and verify it survives manifest derivation.","principle":"A deliverable must depend on the deliverable that creates an artifact it consumes.","repairs":[{"kind":"add_dependency","on":["4.1"],"section_id":"4.2"}],"root_cause":"4.2 consumes `crates/gclient/tests/parity/token_map.rs`, which 4.1 creates, while declaring only 3.3 as a dependency.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"goldens.move-target-inventory","description":"HEAD contains 31 golden JSON fixtures, while the plan claims 32. Moving the directory changes all 31 source paths and all 31 destination paths, yet 1.5 targets none of the sources and only a subset of destinations.","finding_id":"golden-corpus-targets","fix":"Reconcile the baseline to 31 files or identify the genuinely missing 32nd fixture, then enumerate every source deletion and destination addition in parser-supported Targets. Keep `manifest.json` authoritative for the final exact directory contents.","location":"Current-corpus fact, 1.5 Targets, and corpus-move paragraph","prevention":"Before approving a corpus move, inventory the committed directory and reconcile every source and destination path with Targets.","principle":"Every source deletion and destination addition in a move must be owned by the deliverable, and baseline counts must match the repository.","root_cause":"The prose names a whole-directory move that the deterministic target parser cannot infer from a short hand-picked fixture list.","section_id":"1.5","severity":"blocking"},{"category":"traceability","check_key":"terminal-attach.override-parity","description":"A tmux `encoding: semantic_frame` proxy attach never reaches `TerminalWsMixin`, never starts `ProxyHub`, ignores encoding, and omits the new result contract. Remote tmux panes therefore cannot use the cell-frame source promised for every pane.","finding_id":"tmux-semantic-override","fix":"Make the tmux override delegate `semantic_frame` proxy attaches through the host/ProxyHub base path while preserving the omitted/`terminal_ansi` legacy bridge. Cover `terminal_frame`, negotiated encoding, and `direct: null` on the live tmux override.","location":"1.3 Targets and encoding-negotiation behavior","prevention":"For every overridden handler, trace the live MRO and test each backend through the actual dispatch entry.","principle":"When runtime dispatch resolves through an override, contract changes must cover and test that override.","repairs":[{"entries":["`src/gobby/servers/websocket/tmux.py::TmuxMixin._handle_terminal_attach`"],"kind":"add_targets","section_id":"1.3"},{"items":[{"artifact":"test: `tests/servers/test_native_web_proxy.py::test_tmux_semantic_proxy_uses_host_frames`","prose":"A tmux proxy attach with `encoding: \"semantic_frame\"` delegates through the host/ProxyHub path, emits `terminal_frame`, and returns `direct: null`, while omitted or `terminal_ansi` encoding retains the legacy bridge."}],"kind":"add_acceptance","section_id":"1.3"}],"root_cause":"`TmuxMixin._handle_terminal_attach` intercepts tmux proxy attaches before the targeted base handler and retains the legacy ANSI bridge behavior.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"rust.module-registration","description":"Neither new file is guaranteed to compile into `gobby-client`; integration tests cannot import code absent from the crate module tree.","finding_id":"rust-root-module-registration","fix":"Choose one explicit ownership shape. Either add `crates/gclient/src/lib.rs` to both owners and order 3.1 after 2.2, or relocate each file beneath an already-targeted conventional parent and state its exact declaration/re-export. Use the form with the fewest new ownership edges.","location":"Root-level `frame_source_proxy.rs` and `keymap.rs` Targets","participating_section_ids":["2.2","3.1"],"prevention":"For each new Rust source file, trace its `mod` declaration and public import path into an existing targeted carrier.","principle":"Every new Rust module needs an explicitly owned declaration/re-export carrier.","root_cause":"The plan creates two root-level modules while targeting neither `lib.rs` nor a conventional already-targeted parent that will register them.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"keymap.toml-dependency-carriers","description":"Using the workspace's existing `toml` crate—the lowest complete rung—changes `crates/gclient/Cargo.toml` and the package entry in `Cargo.lock`; neither belongs to 3.1, and 2.1 already owns the former without an ordering edge.","finding_id":"keymap-toml-carriers","fix":"Target `crates/gclient/Cargo.toml` and `Cargo.lock` in 3.1, declare the existing workspace TOML parser directly, and add an ordering path from 2.1 to 3.1. Do not introduce a hand-rolled TOML parser.","location":"Keymap override implementation and 3.1 Targets","prevention":"For every new serialized format, verify a direct existing dependency or target the package manifest and lockfile that must change.","principle":"A required file format must have an owned parser dependency and all manifest carriers in Targets.","root_cause":"`gobby-client` has no direct TOML parser dependency, while 3.1 requires `keymap.toml` and omits both Cargo carriers.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"shutdown.latch-owner-targets","description":"Edits confined to persistence, teardown, logging, and `lib.rs` cannot prevent replacement attaches during reconnect and direct-to-proxy fallback as 3.3.6 requires.","finding_id":"exit-latch-target-owner","fix":"Give 3.3 ownership of the run loop, workspace/reducer state, and attach state carrier used to set the latch and gate every request-producing transition.","location":"3.3 Targets and acceptance 3.3.6","participating_section_ids":["3.2","3.3"],"prevention":"Trace every cross-cutting latch from the transition that sets it to each side-effect boundary it suppresses.","principle":"A deliverable's Targets must include the state and request-emission boundaries needed to enforce its acceptance.","repairs":[{"entries":["`crates/gclient/src/app/mod.rs::*` — scope-reason: the workspace owns the exit latch that gates request-producing transitions","`crates/gclient/src/app/run_loop.rs`","`crates/gclient/src/app/attach.rs`"],"kind":"add_targets","section_id":"3.3"}],"root_cause":"3.3 owns the exit-latch behavior, while 3.2 exclusively owns the run loop and attach/reconnect carriers where the latch must be set and checked.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"ws.scroll-correlation","description":"`terminal_scroll_offset_applied` cannot settle the declared `send` future safely, and concurrent scroll replies cannot be matched as written.","finding_id":"scroll-reply-correlation","fix":"Use `notify(terminal_set_scroll_offset)` and consume `terminal_scroll_offset_applied` from the already-subscribed frame-source event stream. Update 2.2.3 accordingly and test that scroll application arrives through `recv`; add new correlation machinery only if an actual caller requires per-request awaiting.","location":"`ProxyFrameSource` scroll send path","participating_section_ids":["2.1","2.2"],"prevention":"Classify every WS verb as correlated or one-way from the live reply shape before assigning it to `send` or `notify`.","principle":"Every awaited correlated request must have a reply key defined by the wire contract.","root_cause":"The plan calls correlated `Daemon::send` for a request whose reply has no `request_id` and no planned correlation map.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconnect.observed-generation","description":"A detach-timeout caller that resumes just after another caller publishes the new generation can start a second socket replacement, defeating the accepted single-flight guarantee.","finding_id":"reconnect-generation-fence","fix":"Change the contract to `reconnect(observed_generation)`: join an in-flight attempt for that observation, return the already-newer generation when stale, and replace only while the observation equals the active generation. Extend 2.1.7 with the across-ready-boundary stale caller.","location":"`Daemon::reconnect` signature, single-flight prose, and acceptance 2.1.7","participating_section_ids":["2.1","3.2"],"prevention":"For each generation-scoped recovery API, test a caller suspended across successful replacement and released afterward.","principle":"A compare-and-replace operation scoped to observed state must carry that observation across the async boundary.","root_cause":"Parameterless `reconnect()` cannot distinguish a stale caller from a request to replace the current generation.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"framesource.heterogeneous-carrier","description":"The plan cannot represent simultaneous direct and proxy panes using the shown trait contract.","finding_id":"frame-source-erasure","fix":"Specify that every attached pane owns a closed `PaneFrameSource` enum with `Direct`, `Proxy`, and `Scripted` variants and static dispatch for `recv`/`send`/`transport`; alternatively make the trait object-safe with boxed futures. Add a loop test with direct and proxy panes active together.","location":"`FrameSource::recv` signature and per-pane workspace ownership","participating_section_ids":["2.2","3.2"],"prevention":"For each heterogeneous trait collection, verify object safety or name the closed enum that provides static dispatch.","principle":"A runtime-polymorphic collection must choose an executable ownership and dispatch representation.","root_cause":"`recv(&mut self) -> impl Future` is not dyn-compatible, while panes can independently hold Direct or Proxy sources and `Workspace` is generic only over `Daemon`.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"startup.remote-host-executability","description":"A remote session allowed through with an absent/not-running host reaches an attach path that cannot emit a frame.","finding_id":"remote-host-health","fix":"Permit remote proxy-only mode when local Unix-socket access is inapplicable and the remote gterm host is running and proxy-capable. Keep absent/not-running host as an actionable refusal or typed attach failure. Update 3.4.3 and remote e2e coverage for both a usable host and absent-host refusal.","location":"Remote degraded-host exception and acceptance 3.4.3","participating_section_ids":["1.2","1.3","3.4","4.3"],"prevention":"Trace fallback dependencies end to end and test the exact absent/degraded state that triggers the fallback.","principle":"A fallback may bypass only the failed component; it cannot depend on that same component downstream.","root_cause":"The remote exception assumes proxy frames exist without gterm, while `ProxyHub` sources semantic frames by handshaking gterm and reports `host_unavailable` when that fails.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"write-input.partial-chunk-outcome","description":"If a later `send-keys -H` invocation fails after an earlier chunk landed, a one-shot result can falsely report no delivery or success and permit duplication.","finding_id":"chunked-input-partial","fix":"Check every tmux return code, track whether any chunk landed, classify timeout/cancellation as indeterminate, and raise `TerminalWriteError(stage=\"partial\")` after a deterministic later-chunk failure. Add first/middle/final failure and cancellation tests proving no automatic resend.","location":"Tmux `write_input` 512-byte chunk loop and acceptance 1.1.1","prevention":"For every chunked write, inject first, middle, and final operation failure plus cancellation and classify the delivered prefix.","principle":"A multi-step external write must distinguish zero delivery, partial delivery, and indeterminate delivery to prevent unsafe retries.","root_cause":"The plan applies `write_key`'s one-shot outcome mapping to a multi-invocation chunked operation.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"lifecycle.seq-publication-order","description":"Concurrent terminal, lease-loss, and finalization events can arrive out of sequence and leave stale state even though their `seq` values are strictly increasing at allocation.","finding_id":"lifecycle-publication-order","fix":"Route lifecycle allocation and publication through one ordered process-wide path, define snapshots against that path's committed high-water, and make `LiveDaemon` enforce an applied high-water during buffered replay. Add a forced-yield concurrency test spanning all three emitters.","location":"Lifecycle sequence allocation, async fanout, and replay","participating_section_ids":["1.4","2.1"],"prevention":"Force yields between sequence allocation and publication across all emitters and assert both wire order and reducer state.","principle":"A sequence number must order observable publication, not only counter allocation.","root_cause":"Separate async emitters can allocate ordered numbers and then complete awaited broadcasts in reverse order; the client has no reorder/high-water rule.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"proxy-frame.lag-recovery","description":"A lagged proxy receiver can lose a bincode frame or delta; roster relisting cannot reconstruct the pane grid, so the pane may remain corrupted indefinitely.","finding_id":"proxy-lag-recovery","fix":"Treat proxy lag as a frame-source failure: detach and tombstone the old proxy attachment, create a fresh semantic proxy attachment, and resume only from its new history/keyframe boundary. Add a stalled proxy-consumer test exceeding 256 frames and assert no stale delta is applied.","location":"Shared daemon broadcast channel and `DaemonEvent::Lagged` recovery","participating_section_ids":["2.1","2.2","3.2"],"prevention":"For every bounded channel carrying deltas, test overflow at each transport and prove recovery begins with a new keyframe or equivalent snapshot.","principle":"Dropping a stateful delta stream requires recovery from a fresh state boundary.","root_cause":"`terminal_frame` shares the 256-entry broadcast channel, while lag recovery only re-lists roster metadata and the attach state machine handles failure only from Direct transport.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"terminal-mode.partial-arm-rollback","description":"A partial arming failure can return through `?` with raw mode still enabled and `Drop` believing no restore is required; existing acceptance covers later exits only.","finding_id":"terminal-mode-arm-rollback","fix":"Make arming transactional or stage-aware: record restoration obligation before the first mutation, track completed stages, and roll them back if a later operation fails. Add injected failures after raw-mode enable and each terminal execute stage.","location":"`TerminalModeGuard` arming and teardown acceptance","participating_section_ids":["3.3","3.4"],"prevention":"Inject failure after every terminal-mode entry stage and assert exact idempotent restoration.","principle":"A multi-stage terminal mutation must establish rollback responsibility before its first successful stage.","root_cause":"The current guard marks itself armed only after `enter()` returns, while raw mode can succeed before later alternate-screen setup fails.","section_id":"3.3","severity":"blocking"},{"category":"missing-requirement","check_key":"keymap.active-chord-collision","description":"The plan leaves one product decision unanswered: does a conflicting override fail with a diagnostic, or win and unbind the displaced default? The current acceptance covers only a non-conflicting known action and reserved actions.","finding_id":"keymap-chord-collision","fix":"Choose and document one collision policy. The safer minimal contract is to reject a chord already owned by another active action with an actionable error while retaining defaults; if override-wins is desired, explicitly unbind the displaced action. Add the corresponding merge and dispatch test.","location":"Override merge rule and acceptance 3.1.6","prevention":"Test override collisions against active, reserved, unknown, and omitted action classes.","principle":"A keymap must map each active chord to one deterministic action.","root_cause":"Merging by action name preserves other defaults even when an override reuses their chord.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconnect.retry-supervisor","description":"A leaf can implement incompatible retry loops while still meeting the current single-flight test; the five-attempt 250 ms→4 s policy is not executable as written.","finding_id":"retry-supervisor","fix":"Assign retry supervision to a named run-loop component. Define five-attempt counting, exponential delays and cap, how `retry_after` overrides or is clamped, reset on generation-ready, and immediate cancellation when exit latches. Add a paused-clock test covering failures, success reset, exhaustion shutdown, `retry_after`, and exit during backoff.","location":"Named reconnect defaults, `LiveDaemon::reconnect`, run-loop exhaustion, and exit latch","participating_section_ids":["2.1","3.2","3.3"],"prevention":"For each retry policy, use paused time to test every delay, cap, reset boundary, exhaustion result, and cancellation path.","principle":"A retry budget needs one named owner and complete counting, timing, reset, and cancellation semantics.","root_cause":"The plan splits one-attempt reconnect behavior from run-loop exhaustion without assigning supervision or defining `retry_after` precedence and latch cancellation.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"#11217","round":1,"verdict":"needs_review"},"session_id":"f372f69c-f06e-456f-8364-9422225513c4"}
```

Round 2 targeted fixer-induced chains from round 1's nineteen repairs and returned needs_review with seventeen blocking findings (nineteen candidates, two dismissed). All seventeen were accepted; zero declines. Six are self-contradictions round 1 introduced: the retry budget asserted five inter-attempt sleeps for five attempts while also demanding immediate exhaustion after the fifth failure, and let a failure's own retry_after replace the delay preceding it; the ReconnectSupervisor was declared sole owner of the retry policy in a sentence that exempted the pane detach-deadline path from it; the closed PaneFrameSource enum was added over a FrameSource trait whose send is synchronous and whose connect is direct-shaped, so the Proxy variant is non-implementable and the Scripted variant has no truthful Transport; the stale-observation reconnect branch compared generation numbers alone and could hand back a generation that had since disconnected, because the published ready value is never cleared; the stage-aware TerminalModeGuard defined partial entry rollback but left a failing restore step undefined; and TerminalWriteError(stage="partial") was given no representation in the wire taxonomy the client reduces. Four are regressions against provenance this plan claims: deadline-driven correlation-map removal and the late-reply-after-timeout case from QA 6.2.10, the paging-overflow and subscriber-lag recovery acceptance from QA 6.2.8 and 6.2.9, live attention reconciliation from source 3.3.1, and the select-spawn-attach-terminate workflow from source 3.3.5. Three were verified defects in Targets or artifacts: RecordingRuntime in the Guard-set-H file tests/servers/test_native_web_proxy.py sends terminal_input but has no write_input, so leaf 1.1 broke its own close gate; section 2.2 asserted simultaneous Direct and Proxy panes while owning neither the Workspace nor the Pane carrier that would store them; and acceptance 4.3.2 cited only the native end-to-end test, letting the tmux host-observer case be skipped. Two were structural. Section 4.4 instructed the implementer to copy Guard set H verbatim from the Constraints framing section, but expanded leaves are section-scoped, so the block would never reach the leaf. The heaviest, deferral-adoption-precondition, is plan-breaking: the plan-coverage contract applies the provenance, open-task, and dependency-closure gates from expansion validation onward, while the adoption instructions for #20202 and #20201 lived in 5.1, the closing leaf. The plan would have failed expansion validation before its own repair step ran, and 5.1 additionally forbade the D3 mutations the contract requires. Four decisions settled the fixes. The deferral repair takes its full form, moving adoption into expansion and idempotently repairing both #20202 and #20201 including #20202's stale proxy-source criteria. The Ready.project ordering repair gives 3.4 the file-wide views/mod.rs scope and a dependency on 3.2 rather than the adversary's exact-symbol target, which would have collided with the file-wide form 2.2 and 3.2 already hold and reproduced the plan-wide scope-form rejection seen in round 1. The remote host-state predicates are rederived from the fields TerminalHostManager.health_state actually emits, since neither proxy-capable nor degraded exists in that payload. And cursor_stale is carried after all: it is a second trigger into the epoch-restart path 2.1 already specifies, so handling it costs one mock fault and one acceptance clause and spares the client from failing its listing with Protocol once D1 lands the refusal.

```json plan-review-round
{"evidence_id":"fd373f39-2dd6-49a4-9a02-05cb685e58bf","plan_hash":"ddfbb1b6bb195e3588352208031fbf761395e0f4b241d8dfe859f55caa0cca92","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"51c2e02090e8370bf5f16dfffb1c86684f411d8e8ec4f60922765e4ee19de116","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":17,"total":19},"evidence_id":"fd373f39-2dd6-49a4-9a02-05cb685e58bf","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"79c64f41159b22e70900a727dc791aaff2ec81014c53ad9a460e8361391d4443","status":"valid"},"source_digest":"bdcaa6e1f5ffefa71d32330dce08649e3817daefff33eff1c06859e478040576","version":1},"findings":[{"category":"traceability","check_key":"source-provenance.attention-subscribe-first","description":"The carried source requirement buffers attention events before fetching the attention roster and reconciles them by epoch/sequence. LiveDaemon lists only terminal lifecycle/frame events, and no acceptance drives live attention buffering, replay, or refetch, so attention changes can be lost during the roster race.","finding_id":"attention-live-reconciliation","fix":"Add attention events to DaemonEvent and specify a LiveDaemon subscribe-first path that buffers them, fetches the attention roster, applies only newer events in the same epoch, and refetches on epoch change. Add a LiveDaemon-backed reconciliation test; retain the scripted reducer tests as secondary coverage.","location":"LiveDaemon event inventory, subscribe-first handshake, and acceptance 2.1.5–2.1.8","prevention":"Diff every claimed source acceptance against the final event inventory and acceptance list, including non-terminal streams.","principle":"Every source acceptance the plan claims to re-satisfy needs an implementation path and an objective acceptance artifact.","root_cause":"The terminal lifecycle repair narrowed subscribe-first reconciliation to terminal events while Source-plan provenance still claims the original attention reconciliation requirement.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"source-provenance.spawn-terminate-loop","description":"The plan claims the original select → spawn → reconcile → attach → terminate workflow, yet 3.2 defines no spawn or terminate action path and 4.3 contains no such E2E. A leaf can satisfy 2.1 by testing the methods directly while shipping a client that cannot invoke them.","finding_id":"client-spawn-terminate-workflow","fix":"Specify the spawn and terminate UI actions and their run-loop/reducer transitions in 3.2. Add a scripted-loop acceptance and a 4.3 E2E that invokes both from gclient and verifies row creation, attachment, termination, and pane/roster convergence.","location":"3.2 key/event routing and 4.3 E2E workflow list","prevention":"For every user workflow in source provenance, trace command binding, reducer transition, daemon call, reconciliation, and E2E artifact.","principle":"A trait method test does not prove the user-facing workflow that invokes the method and reconciles its state.","root_cause":"Spawn and terminate survived only as Daemon trait methods; the client-loop actions and end-to-end workflow from the claimed source acceptance disappeared.","section_id":"3.2","severity":"blocking"},{"category":"weak-testability","check_key":"reconciliation.cursor-overflow-lag","description":"The claimed QA 6.2 contract requires cursor_stale restart, recovery when a 1,024-entry paging buffer receives event 1,025, and re-list convergence after a 256-entry subscriber lag. Cursor stale is absent and none of these branches has pass/fail acceptance.","finding_id":"data-plane-recovery-coverage","fix":"Restore cursor_stale to the mock fault set and define restart/re-pin behavior. Add acceptance for event 1,025 discarding partial paging state and starting a fresh snapshot, plus subscriber lag re-subscribing, re-listing, and converging without stale reuse.","location":"Subscribe-first recovery prose and acceptance 2.1.1–2.1.8","prevention":"For every bounded reconciliation buffer and paged snapshot, test stale cursors, bound+1 overflow, and live-subscriber lag to authoritative convergence.","principle":"Recovery branches that discard partial state or restart an authoritative snapshot need explicit, adversarial acceptance tests.","root_cause":"The revised section mentions buffer overflow and subscriber lag in prose but dropped cursor_stale semantics and all three source acceptance artifacts.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"e2e.tmux-host-observer-artifact","description":"The generated 4.3 criterion can pass without running `test_gclient_renders_tmux_row_through_host`, leaving the tmux observer path unverified.","finding_id":"tmux-e2e-artifact","fix":"Add the exact tmux-host-observer test to 4.3 acceptance.","location":"Acceptance 4.3.2","prevention":"Compare every named E2E scenario with the exact test references carried by acceptance and derived validation criteria.","principle":"Each materially distinct E2E transport branch must survive manifest derivation as an exact acceptance artifact.","repairs":[{"items":[{"artifact":"test: `tests/e2e/test_terminal_client_stack.py::test_gclient_renders_tmux_row_through_host`","prose":"Selecting a tmux terminal in gclient renders its output through the gterm host observer and reports the direct transport."}],"kind":"add_acceptance","section_id":"4.3"}],"root_cause":"The prose names the tmux-host-observer test, while 4.3.2 cites only the native-row test.","section_id":"4.3","severity":"blocking"},{"category":"traceability","check_key":"leaf.self-contained-guard-copy","description":"The 4.4 leaf says to copy Guard set H from Constraints but does not contain the block. Expanded leaves are section-scoped, so the implementer lacks the exact commands, environment, new-inode rule, and host-leak check its documentation acceptance requires.","finding_id":"guard-set-h-self-contained","fix":"Inline the complete Guard set H block in 4.4, including all seven checks and the environment/install details; keep the Constraints copy as framing.","location":"4.4 implementation prose","prevention":"Resolve every cross-section 'copy verbatim' instruction into the consuming deliverable before manifest handoff.","principle":"An expanded leaf must contain every instruction and exact payload needed to meet its acceptance.","root_cause":"4.4 refers to a framing section that is not part of the leaf description instead of carrying the required documentation block.","section_id":"4.4","severity":"blocking"},{"category":"gobby-format","check_key":"deferral.expansion-validity","description":"#20202 currently has only `deferred-from:herdr-terminal-client:D2`, no parent or blockers, no reference to 5.1.2, and validation criteria that still require the proxy source delivered by 2.2. #20201 likewise lacks current D3 provenance, tail-work closure, and a 3.1.4 reference. The clean mechanical plan validator does not resolve this live-task gate.","finding_id":"deferral-adoption-precondition","fix":"Move legacy adoption into expansion/finalization before deferral validation. Idempotently add current-plan provenance, required tail-work parent/dependency closure, and original-acceptance references to #20202 and #20201. Rewrite #20202 title, description, and validation criteria so the proxy source is a landed prerequisite and only hub roster/routing, endpoint resolution, capability-token lifecycle, and hub-node relay remain. Replace 5.1's D3 no-mutation instruction with this compliant adoption rule.","location":"5.1 adoption algorithm and the D2/D3 deferral blocks","prevention":"Before finalizing each deferral block, validate the referenced live task against current plan provenance, acceptance references, dependency closure, and remaining scope.","principle":"A referenced deferral task must satisfy provenance, duplicated acceptance, and recovery-epic closure when expansion validates it.","root_cause":"The plan postpones #20202 adoption until leaf execution and forbids #20201 mutation, although deferral validation runs before those instructions can repair either task; #20202 also retains obsolete proxy-source criteria.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconnect.stale-observation-readiness","description":"A stale G1 caller admitted after G2 became active returns G2 immediately under the stated three cases. If G2 has since disconnected or is retrying, that return exposes a non-ready generation and can trigger attaches against a dead socket. The readable ready value is never cleared on disconnect.","finding_id":"reconnect-readiness-state","fix":"Represent current readiness explicitly: return a newer generation immediately only while it is ready; clear the readable ready value on disconnect; when current is disconnected, join its attempt or return the typed disconnected result owned by the supervisor. Extend 2.1.7 with a stale G1 caller arriving while G2 recovery is failing/in flight.","location":"Three-case reconnect contract and generation-ready publication","prevention":"Test stale recovery callers against every current-generation state: ready, disconnected idle, and disconnected in-flight.","principle":"A generation identifier can be returned as ready only while the connection state for that generation is ready.","root_cause":"The older-observation branch compares only generation numbers and ignores that the newer active generation may have disconnected or entered another recovery attempt.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"framesource.common-async-contract","description":"The current trait carries direct `connect(locator, ...)` and test-introspection methods, while ProxyFrameSource is constructed after daemon attach. `send` cannot await proxy notify or reliable Tokio `write_all`, and `Transport` has no truthful Scripted result. The enum therefore remains non-implementable as written.","finding_id":"frame-source-common-surface","fix":"Define the final minimal common surface with async `send`, async `recv`, and `transport`; move Unix connection/handshake into the concrete direct constructor; keep `sent_*` observability as inherent scripted-double methods. Configure ScriptedFrameSource to emulate Direct or Proxy explicitly, and add compile-time plus behavior tests for all three enum variants.","location":"FrameSource extension and PaneFrameSource enum","prevention":"For each closed enum, write the final signatures and validate construction, send, receive, and transport semantics for every variant.","principle":"Every variant of a shared interface must implement one truthful common lifecycle and async boundary.","root_cause":"The repair adds Proxy and Scripted variants around the existing direct-only trait without defining its final surface; current `send` is synchronous even though proxy sends await `Daemon::notify`.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"reconnect.supervisor-single-owner","description":"Each expired pane detach deadline can retry outside episode counting, backoff, fifth-failure shutdown, and exit-latch cancellation. Single-flight coalesces simultaneous calls but does not bound repeated direct calls after failed attempts.","finding_id":"reconnect-supervisor-bypass","fix":"Have panes submit a generation-tagged recovery intent to the run-loop ReconnectSupervisor and await its shared episode result. Make the supervisor the only `LiveDaemon::reconnect` caller, coalesce all pane intents, count them in the same episode, and extend 3.2.7 to drive concurrent detach expiries through the run loop.","location":"Detaching deadline and ReconnectSupervisor paragraphs","prevention":"Enumerate every call site of a retried operation and route each through the named budget owner.","principle":"A retry policy has one owner only when every retry trigger flows through that owner.","root_cause":"The fixer introduced a supervisor but preserved a direct pane-level `LiveDaemon::reconnect` call outside its budget.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"retry.budget-delay-cardinality","description":"Five total attempts have only four inter-attempt sleeps. A 4 s sleep after the fifth failure contradicts immediate exhaustion, and `retry_after` cannot replace a delay for the attempt whose failure supplied it.","finding_id":"retry-budget-schedule","fix":"Keep the settled five-attempt budget: run attempt one immediately; sleep 250 ms, 500 ms, 1 s, and 2 s before attempts two through five; exit immediately after failure five. State that `retry_after` from failure N controls only the sleep before N+1 and remains clamped to 250 ms–4 s. Update 3.2.7 to assert that exact order.","location":"ReconnectSupervisor timing semantics and acceptance 3.2.7","prevention":"Write retry acceptance as an ordered timeline of call, result, delay, and terminal transition.","principle":"Retry attempt count, sleep count, delay placement, and exhaustion transition must form one executable timeline.","root_cause":"The repaired prose lists five total attempts and five post-failure delays while also requiring immediate exhaustion after failure five.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"terminal-mode.restore-failure-idempotence","description":"An implementation can clear a failed obligation and strand terminal state, or retain all flags and restore already-restored stages twice. Acceptance covers entry failures and clean exit only.","finding_id":"terminal-restore-failure","fix":"Track each completed mutation as an outstanding obligation. Restore all obligations in reverse order with best-effort continuation, clear one only after its restoration succeeds, retain failures for the later Drop retry, never panic in Drop, and log aggregate explicit-restore errors. Extend 3.3.7 with each restore-stage failure.","location":"TerminalModeGuard stage-aware restoration and acceptance 3.3.7","prevention":"Inject failure into every entry and restore stage and assert outstanding obligations after each transition.","principle":"A terminal-safety guard must continue best-effort restoration and preserve retry obligations when a restore step itself fails.","root_cause":"The repair defines partial entry rollback but leaves obligation clearing, later-stage continuation, explicit restore, and Drop behavior undefined on restore errors.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"write-input.partial-end-to-end","description":"A middle/final chunk failure can escape `_deliver_operator_write` before `complete_write`, leaving the sequence in flight and emitting no operator-visible result. The plan's promise that partial delivery surfaces and is never resent is therefore unenforced.","finding_id":"chunked-write-wire-outcome","fix":"Use the existing wire taxonomy: translate known and unknown partial delivery to `terminal_write_outcome.outcome=\"indeterminate\"`, preserve a known delivered-byte count in the existing reason/detail field, complete the write ledger on every failure/cancellation path, and keep gclient uncertain read-only with no resend. Extend 1.1.5 through the real WebSocket path and duplicate `client_write_seq` replay; no new outcome enum or fixture family is needed.","location":"Chunked write failure contract, `_deliver_operator_write`, golden taxonomy, and client reducer","prevention":"Trace first, middle, final, timeout, and cancellation failures through runtime result, write ledger, WebSocket reply, duplicate-sequence replay, and client state.","principle":"Every admitted external write must terminate in a durable wire outcome the client can reduce without unsafe retry.","root_cause":"`TerminalWriteError(stage=\"partial\")` is outside `WriteOutcome`; the server catches neither it nor cancellation, and the client recognizes no partial outcome.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"ws.waiter-timeout-removal","description":"A withheld reply on a live socket can leave request, write, or single-flight control entries registered indefinitely. Repeated timeouts leak entries, late replies hit abandoned oneshots, and later control requests can remain locally refused.","finding_id":"correlation-timeout-cleanup","fix":"Specify exact-key removal on success, timeout, cancellation, write failure, finalization, and generation teardown; late replies after removal are logged/dropped and never recreate state. Add repeated withheld-reply and cancellation tests for all three maps, asserting bounded size and later control admission.","location":"Correlation maps, named deadlines, and acceptance 2.1.2–2.1.4","prevention":"For every waiter map, test success, timeout, cancellation, write failure, finalization, teardown, and late reply.","principle":"A correlation registration must be removed on every terminal path, including timeout and caller cancellation while transport stays live.","root_cause":"The plan clears maps on socket teardown/finalization but never specifies removal after local deadline or future cancellation.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"runtime.fake-interface-closure","description":"`tests/servers/test_native_web_proxy.py::RecordingRuntime` has `write_text`, `write_key`, and `write_paste` but no `write_input`; its existing tests send `terminal_input`. Leaf 1.1 will reroute those calls and fail before 1.3 later owns the file.","finding_id":"input-runtime-fake-target","fix":"Add the whole test file to 1.1, implement `RecordingRuntime.write_input`, and update its existing terminal-input assertions to raw bytes. Existing 1.1→1.3 ordering resolves shared ownership.","location":"1.1 Targets and Guard set H native-proxy tests","prevention":"Search every protocol method sibling across tests and run the close-gate test inventory against each fake implementation.","principle":"A protocol change must own every exercised fake implementation before the changing leaf closes.","repairs":[{"entries":["`tests/servers/test_native_web_proxy.py::*` — scope-reason: Guard-set-H RecordingRuntime gains write_input and existing terminal_input assertions follow the raw-input route"],"kind":"add_targets","section_id":"1.1"}],"root_cause":"The consumer sweep covered shared terminal fakes but missed `RecordingRuntime` in a Guard-set-H test file already sending `terminal_input`.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"framesource.per-pane-owner-targets","description":"2.2 must close with simultaneous Direct and Proxy panes and one-pane fallback, but it cannot store that state through its current Targets. A test-only representation would satisfy compilation without delivering the promised production ownership.","finding_id":"pane-frame-source-carriers","fix":"Give 2.2 the exact Workspace and Pane scopes, store `PaneFrameSource` per pane, and implement one-pane replacement there. The existing 2.1→2.2→3.2 ordering already serializes the shared files.","location":"2.2 Targets and acceptance 2.2.6–2.2.7","prevention":"Trace each new per-entity state claim to the struct that stores it and target that carrier in the claiming deliverable.","principle":"Targets must include the production state carriers needed to enforce the deliverable's own acceptance.","repairs":[{"entries":["`crates/gclient/src/app/mod.rs::*` — scope-reason: Workspace replaces its single frame source with per-pane PaneFrameSource ownership","`crates/gclient/src/app/pane.rs::*` — scope-reason: Pane stores its Direct, Proxy, or Scripted source and one-pane fallback state"],"kind":"add_targets","section_id":"2.2"}],"root_cause":"The enum repair was placed in frame_source.rs, while current source ownership remains one Workspace-wide source and Pane has no source field; those carriers are deferred to 3.2.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"startup.required-project-consumer","description":"`Ready.project` is currently `Option<String>` and `run_ready` uses `if let Some`. 3.4 claims omission cannot reach the loop but targets only startup.rs/tests, so the optional consumer can survive or the two leaves can collide.","finding_id":"project-ready-consumer-order","fix":"Target `crates/gclient/src/views/mod.rs::run_ready` in 3.4, add `depends: 3.2`, and consume one required UUID when constructing LiveDaemon and issuing the first `list_terminals`. Alternatively move that entire construction boundary into a 3.4-owned startup symbol and make run_ready accept an already-required session; choose one explicit shape.","location":"3.4 Targets/dependencies and `views::run_ready`","prevention":"For every Option→required contract change, locate all destructuring/branching consumers and add ownership plus provider-consumer order.","principle":"When a producer makes a data field required, the leaf must own and order after the live consumer that removes the optional branch.","root_cause":"3.4 changes `Ready.project` while 3.2 independently owns the only consumer, and no ordering edge connects them.","section_id":"3.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"startup.remote-host-report-shape","description":"The live `gterm_host` payload reports `enabled`, `running`, `protocol_version`, `last_error`, and related counters; it has no proxy-capable or degraded discriminator, and current gclient drops `protocol_version`. A fake can satisfy 3.4.3 while the real remote client cannot make the promised decision.","finding_id":"remote-host-health-contract","fix":"Use the existing contract at the lowest complete rung: treat a running pinned-protocol gterm as proxy-capable, define degraded from the explicit existing state (for example a running host with `last_error`), deserialize `protocol_version`, and update 3.4.3/4.3.3 accordingly. Add a new producer capability field and Targets only if a distinct runtime capability is genuinely required.","location":"Host-state prose, acceptance 3.4.3, and 4.3.3","prevention":"For each startup health predicate, trace the producer field through deserialization and test the live payload rather than a richer fake.","principle":"Startup policy may branch only on fields the live health producer emits or on a stated deterministic derivation from them.","root_cause":"The round-one repair introduced daemon-reported `proxy-capable` and `degraded` predicates without defining them against the actual health schema.","section_id":"3.4","severity":"blocking"}],"reviewer_session":"#11218","round":2,"verdict":"needs_review"},"session_id":"f372f69c-f06e-456f-8364-9422225513c4"}
```

Round 3 returned needs_review with 16 blocking findings from 17 candidates (1 dismissed); lanes 5/3/9, attestation and 16-entry shadow manifest both valid. The human accepted all 16 with zero declines after independent verification of every finding against the repository, the Plan-Coverage Contract, and the two source plans.

Six findings were defects introduced by round 2's own repairs. Acceptance 2.1.7 still had panes calling reconnect directly after 3.2.9 made ReconnectSupervisor the exclusive caller, leaving two incompatible call graphs. The 5.1 adoption prose assigned D2 only 5.1.2 and D3 only 3.1.4 while their typed deferral objects also carried 5.1.3. The new protocol-mismatch refusal in 3.4.3 and 4.3.3 named no E2E artifact that constructs a running host at a mismatched protocol_version. Removing the workspace-wide frames() accessor in 2.2 breaks crates/gclient/tests/copy_paste.rs, which is targeted only by the later leaf 3.3, so 2.2's own Guard set H could not compile. The adoption algorithm writes the new provenance label and then routes later runs down the new-provenance path, so a crash between the label and the parent, content, or dependency writes is never repaired. And the degraded derivation read last_error as a current fault when TerminalHostManager clears it only in start(), never in _health_loop, so one transient health blip would mark a recovered host degraded permanently.

Ten findings were pre-existing. Acceptance 1.5.1 counts manifest.json as a manifest entry while 1.5.3 requires every entry to replay as a WebSocket message, which no corpus can satisfy. terminal_control_result carries only attachment_id, granted, reason, and lease_generation, so exact-key removal on a post-write timeout lets a late take reply settle a newer release waiter on the same attachment. Lifecycle ordering can be bypassed by _handle_terminal_detach and _handle_terminal_take_control, which emit finalization and the takeover fallback directly and were absent from 1.4's Targets. TmuxMixin's TYPE_CHECKING declaration of broadcast_terminal_output still types attachment_id as optional. The gterminal wire codec is generic over std::io::Read and std::io::Write, so it cannot frame tokio split halves as the rewritten direct FrameSource requires. The create and kill handlers emit only correlated results and no lifecycle event, so a successful spawn or terminate can leave the UI pending forever. The final Daemon trait exposes no close operation although 3.3 requires ordered WebSocket closure. No acceptance artifact captures gclient's production outbound bytes against the golden corpus. The live attention handshake assigned fetch, install, and replay orchestration to LiveDaemon although Workspace already owns that reducer and the trait exposes no state-installation surface. And source clause 3.3.2's requirement that an unreachable daemon leaves panes rendering frames while read-only was claimed in provenance but never carried.

Two shape calls were put to the human. For spawn and terminate convergence, P1 grows to publish ordered created and exited lifecycle events from the create and kill handlers, resolving a created row through the planned GET-by-id before fanout, rather than having the client re-list after each reply; the ordered publisher already exists and the handlers simply skip it, and the browser gets the same fix. For host health, usability derives from running and protocol_version alone on both the local and --daemon-url paths, with last_error demoted to a diagnostic notice that never by itself refuses a matching running host.

```json plan-review-round
{"evidence_id":"030f225d-9385-467c-9e78-b0eab01e95c8","plan_hash":"09433f3ffc42997ce16ac768855aa477e20d4f77492f3ee03017bf2fb1b240dc","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"431f9235c24c6edc02424038bf32219a109d4368f126419a1cf20ed1b832f5d8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":16,"total":17},"evidence_id":"030f225d-9385-467c-9e78-b0eab01e95c8","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"7b0aa002c96b684a4a56be0ac40ed8bfe792e3cc2701928dc3966da853b20812","status":"valid"},"source_digest":"60a8b446fd110749a5134669a67407cb0c1183ba761d952f65b99ead7e44aefb","version":1},"findings":[{"category":"traceability","check_key":"golden-corpus-manifest-self-membership","description":"The plan simultaneously makes manifest.json a fixture entry and requires every entry to decode as a client/server WebSocket message, so the corpus cannot satisfy both acceptance items.","finding_id":"R3-F01-golden-manifest-self-entry","fix":"Define manifest.json as inventory metadata. Require exactly 36 manifest entries (31 moved fixtures plus 5 new fixtures), exactly 37 files in the directory including manifest.json, directory-minus-manifest parity with the inventory, and Rust/Vitest replay of the 36 fixture entries.","location":"Acceptance 1.5.1 and 1.5.3","prevention":"Compare the fixture directory minus manifest.json with manifest entries, and validate the metadata file separately.","principle":"A fixture inventory contains replayable members; its own metadata file is outside that member set.","root_cause":"Acceptance 1.5.1 counts manifest.json as a manifest entry while 1.5.3 requires both harnesses to replay every manifest entry as a WebSocket fixture.","section_id":"1.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"control-timeout-late-reply-alias","description":"A late take/release control reply can settle a newer waiter for the same attachment after 2.1.9 removes the old map entry, recreating the cross-generation alias the cleanup rule intends to prevent.","finding_id":"R3-F02-control-timeout-late-reply-alias","fix":"Specify that timeout or cancellation after a control request is written tombstones that attachment's control correlation scope and requires detach/finalize/fresh-attach before another take/release waiter is registered. Keep pre-write cancellation immediately reusable. Add a test where a timed-out take reply arrives after a release waiter is attempted and prove it cannot settle the newer operation.","location":"Correlation cleanup rules and acceptance 2.1.9","prevention":"Distinguish cancellation before transport write from uncertainty after write, and retire every non-unique correlation scope after the latter.","principle":"A correlation key cannot be reused while an older request carrying that key can still produce a reply.","root_cause":"terminal_control_result carries only attachment_id; deleting a timed-out or cancelled waiter allows a later waiter for the same attachment to reuse the same key.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"production-ws-emitter-golden-parity","description":"Both harnesses can agree on hand-authored fixtures while the actual gclient send/notify path emits a different envelope or field shape.","finding_id":"R3-F03-outbound-golden-emitter-coverage","fix":"Add a production-path conformance test that captures every LiveDaemon and Workspace outbound request/notification and compares its canonicalized bytes with the corresponding golden fixture.","location":"Acceptance 1.5.3 and the 2.1 production send/notify implementation","prevention":"Map each source-plan wire-compatibility clause to a test artifact that exercises the production carrier.","principle":"Round-trip fixture decoding does not prove that production emitters serialize the canonical wire shape.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/daemon_live.rs::outbound_messages_match_corpus`","prose":"Every outbound WebSocket message emitted by LiveDaemon and Workspace production send/notify paths matches its canonical request fixture byte-for-byte after canonicalization."}],"kind":"add_acceptance","section_id":"2.1"}],"root_cause":"The source-plan golden-corpus obligation is carried only by fixture replayers; no acceptance artifact captures LiveDaemon or Workspace production outbound bytes.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","check_key":"remote-protocol-mismatch-live-branch","description":"The plan names remote protocol mismatch as a refusal case but supplies no E2E artifact that runs a healthy host with the wrong protocol version.","finding_id":"R3-F04-protocol-mismatch-e2e-gap","fix":"Add an E2E case with running=true, a mismatched protocol_version, and a distinct last_error value; assert --daemon-url exits before workspace connection and reports both expected and observed versions.","location":"Scenario inventory and acceptance 4.3.3","prevention":"For every state matrix row, verify that the named test fixture constructs all discriminating producer fields.","principle":"Each named runtime branch needs an artifact whose setup reaches that branch.","repairs":[{"items":[{"artifact":"test: `tests/e2e/test_terminal_client_stack.py::test_gclient_remote_refuses_protocol_mismatch`","prose":"A running host with a mismatched protocol_version causes --daemon-url to exit before workspace connection and reports the expected and observed versions."}],"kind":"add_acceptance","section_id":"4.3"}],"root_cause":"The cited absent-host test cannot establish behavior for a running host that reports a mismatched protocol_version.","section_id":"4.3","severity":"blocking"},{"category":"traceability","check_key":"deferral-original-acceptance-parity","description":"An implementer following the new 5.1 adoption relocation cannot know whether the target tasks must preserve the extra 5.1.3 provenance and validation obligations.","finding_id":"R3-F05-deferral-item-parity","fix":"Make the mapping exact everywhere: D1=[5.1.2], D2=[5.1.2,5.1.3], and D3=[3.1.4,5.1.3]. Require the adopted task descriptions and validation criteria to preserve the corresponding named artifacts.","location":"Adoption mapping and the D2/D3 deferral objects","prevention":"Compare the adoption algorithm's per-deferral arrays with the parsed deferral objects before review.","principle":"Operational deferral adoption and the typed deferral object must carry one exact source-acceptance provenance set.","root_cause":"The adoption prose assigns D2 only 5.1.2 and D3 only 3.1.4, while their typed objects also assign 5.1.3.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"lifecycle-emitter-target-exhaustiveness","description":"The ordered registry path can be bypassed by two existing direct handler emissions, leaving sequence monotonicity false on production paths despite the planned publisher.","finding_id":"R3-F06-lifecycle-bypass-targets","fix":"Target both handlers, route their finalization and takeover fallback emissions through the ordered registry publisher, and test monotonic sequence order for both direct paths.","location":"Targets and ordered lifecycle publisher acceptance","prevention":"Run a class-wide lifecycle-emitter search, including direct handler fallbacks, whenever publication ordering changes.","principle":"Every direct emitter of a lifecycle event must be owned by the leaf that introduces ordered publication.","repairs":[{"entries":["`src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_detach`","`src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_take_control`"],"kind":"add_targets","section_id":"1.4"},{"items":[{"artifact":"test: `tests/servers/test_terminal_list_watermark.py::test_direct_lifecycle_fallbacks_are_ordered`","prose":"Direct detach finalization and the unregistered-requester takeover fallback both publish stamped lifecycle events through the ordered registry path, preserving strictly increasing observed seq."}],"kind":"add_acceptance","section_id":"1.4"}],"root_cause":"_handle_terminal_detach emits finalization and _handle_terminal_take_control emits the unregistered-requester lease_lost fallback, yet neither method is targeted by 1.4.","section_id":"1.4","severity":"blocking"},{"category":"traceability","check_key":"structural-broadcast-signature-target","description":"Leaving the structural declaration unchanged preserves a contradictory callable contract and can hide incomplete callers from static checking.","finding_id":"R3-F07-broadcast-signature-target","fix":"Add TmuxMixin.broadcast_terminal_output to 1.2 Targets and make attachment_id: str required there alongside the implementation migration.","location":"Targets for the required attachment_id signature migration","prevention":"Search all declarations, protocols, implementations, and callers for every parameter made required.","principle":"A breaking signature migration owns structural declarations as well as runtime implementations and callers.","repairs":[{"entries":["`src/gobby/servers/websocket/tmux.py::TmuxMixin.broadcast_terminal_output`"],"kind":"add_targets","section_id":"1.2"}],"root_cause":"TmuxMixin's TYPE_CHECKING broadcast_terminal_output declaration still exposes attachment_id as optional and is outside the current Targets.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"per-pane-frame-source-test-consumer","description":"Expansion of 2.2 can delete the accessor and leave its own Guard-set-H validation uncompilable until an unrelated later leaf runs.","finding_id":"R3-F08-per-pane-test-target","fix":"Add copy_paste.rs to 2.2 and migrate its existing assertions to pane-specific ScriptedFrameSource observability; retain 3.3's later behavioral ownership.","location":"Targets versus Guard-set-H copy/paste coverage","prevention":"Search all method consumers and test assertions before deleting or narrowing a shared accessor.","principle":"The leaf deleting a shared test seam must migrate every gate consumer of that seam.","repairs":[{"entries":["`crates/gclient/tests/copy_paste.rs::*` — scope-reason: existing Guard-set-H assertions migrate from the removed workspace-wide `frames()` accessor to pane-specific scripted-source observability"],"kind":"add_targets","section_id":"2.2"}],"root_cause":"copy_paste.rs reads the workspace-wide ws.frames accessor that 2.2 removes, while the file is targeted only by later deliverable 3.3.","section_id":"2.2","severity":"blocking"},{"category":"over-engineering","check_key":"attention-handshake-single-owner","description":"Implementing the text literally requires an extra snapshot-carrying coordination mechanism around an already sufficient Workspace reducer, with no concrete consumer or interface for that mechanism.","finding_id":"R3-F09-attention-handshake-owner","fix":"Keep one mechanism: make Workspace<D: Daemon>::reconcile_subscribe_first async and let it own subscribe, buffering, roster fetch, state installation, and replay. LiveDaemon supplies the real REST/socket operations. Drive acceptance 2.1.12 through Workspace<LiveDaemon> with mocked transport; add no parallel snapshot watch or second reducer.","location":"Live attention handshake prose and acceptance 2.1.12","prevention":"Name the existing state consumer for every proposed layer and apply the restraint ladder before adding callbacks, watches, or duplicate reducers.","principle":"Snapshot installation and buffered-event replay have one state owner; the transport layer supplies inputs to that owner.","root_cause":"The new text assigns fetch/install/apply orchestration to LiveDaemon even though Workspace already owns the reducer and the final Daemon trait exposes no state-installation surface.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"tokio-codec-async-boundary","description":"The Direct source is non-implementable as written without blocking Tokio workers, duplicating framing, or introducing an unspecified adapter and shutdown contract.","finding_id":"R3-F10-async-codec-boundary","fix":"Target wire_codec.rs and its tests. Add async read/write helpers over AsyncRead/AsyncWrite that reuse the existing bincode validation and frame-size bounds, retain a whole-socket shutdown handle beside split halves, and cover partial reads/writes, cancellation, oversize frames, EOF, and shutdown.","location":"Direct FrameSource reader/writer specification","prevention":"Verify the sync/async trait bounds of every reused codec before naming it as a transport implementation.","principle":"Async I/O paths use nonblocking framing while sharing one validation and size-bounds contract with synchronous framing.","root_cause":"The existing gterminal wire codec accepts std::io::Read/Write, which cannot directly frame tokio split halves as the new trait rewrite specifies.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"spawn-terminate-authoritative-convergence","description":"Successful spawn or terminate can leave the UI pending forever, especially when no independent lifecycle watcher emits a matching row or removal.","finding_id":"R3-F11-spawn-terminate-convergence","fix":"Extend P1 to own the create and kill handler paths and publish ordered created/exited lifecycle events after successful results. Resolve a created event's full row through the already planned GET-by-id before fanout, remove exits by id, retain reconnect/list reconciliation as recovery, and test the real handlers under reply-before-event, event-before-reply, duplicate, loss, and reconnect schedules.","location":"Spawn/terminate workflow and acceptance 3.2.10","prevention":"For each effect, enumerate result/event/list loss and ordering schedules and identify the authoritative convergence path.","principle":"A pending UI mutation settles only from an authoritative signal whose production path is guaranteed after the accepted mutation.","root_cause":"Current create/kill handlers send only correlated command results; the plan defers UI mutation to lifecycle reconciliation without requiring those handlers to publish created/exited events or a guaranteed list refresh.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"daemon-close-shared-contract","description":"Workspace shutdown cannot generically close LiveDaemon, reject new requests, or trigger the specified terminal cleanup through the advertised trait.","finding_id":"R3-F12-daemon-close-contract","fix":"Add async idempotent close(&self) to Daemon and implement it for LiveDaemon and ScriptedDaemon. It atomically rejects new sends, fails and clears outstanding waiters, stops the reader, and closes the sink. Invoke it after bounded detach during shutdown, and test that no request or reconnect starts after close.","location":"The 2.1 final Daemon surface and 3.3 ordered shutdown","prevention":"Trace each generic lifecycle operation through the finalized interface before declaring the trait surface complete.","principle":"Every lifecycle operation required by generic shutdown must be expressible through the shared interface used by that shutdown path.","root_cause":"The final Daemon trait has no close operation, while 3.3 requires ordered WebSocket closure and 2.1 promises cleanup of waiters and maps on close.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"deferral-adoption-partial-recovery","description":"A crash after adding provenance but before reparenting or dependency closure sends the next run down the new-provenance path, which the plan does not require to repair the unfinished fields.","finding_id":"R3-F13-adoption-partial-recovery","fix":"Preflight both tasks and dependency closure, add idempotent dependencies first, converge parent/content/criteria, and write the new provenance label last. Require the new-provenance branch to verify and repair every postcondition rather than skip adoption. Add failure-injection rerun tests after each mutation boundary.","location":"Adoption algorithm and new-provenance branch","prevention":"Inject failure after each migration operation and prove the next run converges the complete target state.","principle":"An idempotent migration writes its completion marker only after every postcondition holds, or every rerun converges all postconditions regardless of the marker.","root_cause":"Labels, parent, rewritten content, validation criteria, and dependencies span separate mutations, while the new provenance label changes which branch a retry follows.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"reconnect-caller-ownership-parity","description":"The plan now contains two incompatible reconnect call graphs, so an implementer cannot satisfy both the 2.1 test language and the 3.2 exclusivity invariant.","finding_id":"R3-F14-reconnect-owner-contradiction","fix":"Rewrite 2.1.7 as a LiveDaemon single-flight harness test with concurrent generic callers. Keep pane-originated concurrency solely in 3.2.9, where panes submit generation-tagged intents to ReconnectSupervisor and never call LiveDaemon::reconnect.","location":"Acceptance 2.1.7 versus 3.2 ownership and 3.2.9","prevention":"After changing caller ownership, search all prose and acceptance criteria for the superseded caller.","principle":"Acceptance tests must exercise the production ownership model established by dependent deliverables.","root_cause":"Acceptance 2.1.7 still describes concurrent panes calling reconnect directly after 3.2 makes ReconnectSupervisor the only caller and panes submit intents.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"host-health-sticky-error-classification","description":"A recovered host can remain permanently degraded or refused because a historical diagnostic survives beside running=true and a matching protocol_version.","finding_id":"R3-F15-sticky-host-error","fix":"Derive usability from running and protocol_version. Treat last_error as diagnostic history that may produce a notice and never alone makes a matching running host unusable. Test a running, matching host with stale last_error for local and --daemon-url success, plus stopped and mismatched refusal cases.","location":"Host state derivation and acceptance 3.4.3","prevention":"Inspect every write and clear transition of a health field before using it as a state discriminator.","principle":"Current usability can be derived only from producer fields whose semantics represent current state.","root_cause":"TerminalHostManager writes last_error on failures and does not reliably clear it after a later successful health check or reconciliation, while the plan treats non-null last_error as a current fault.","section_id":"3.4","severity":"blocking"},{"category":"traceability","check_key":"daemon-loss-readonly-source-parity","description":"During daemon loss the client can continue direct rendering, yet the plan leaves control state and input eligibility unspecified until reconnect succeeds or the retry budget expires.","finding_id":"R3-F16-daemon-loss-readonly","fix":"On daemon reader loss, immediately clear lease/control and pending input state, continue direct FrameSource receive/render during backoff, and suppress terminal writes and control requests. After readiness, perform fresh attach/control reconciliation; on budget exhaustion exit. Add a test proving read-only rendering and write suppression across loss and recovery.","location":"Run-loop and supervisor behavior versus source-plan clause 3.3.2","prevention":"Maintain a provenance matrix that maps each source clause to one behavior statement and one named test.","principle":"Every claimed source-plan obligation needs an explicit state transition and an acceptance artifact.","root_cause":"The completion plan adds reconnect retries and eventual exit but never carries the source requirement to enter immediate read-only mode while direct terminal frames continue.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"#11223","round":3,"verdict":"needs_review"},"session_id":"f372f69c-f06e-456f-8364-9422225513c4"}
```

Round 4 (unattended coordinator judgment; no user gate). Verdict `needs_review` with 16 candidates dispositioned, 14 findings emitted (13 blocking, 1 nit), 2 dismissed. Every finding was ground against the live repository with `gcode` before its vote; all 14 accepted, two of them with a restraint-minimal fix that is smaller than the one the adversary proposed.

Seven findings are fixer-induced chains on rounds 1-3 repairs, which is the expected shape at this depth. R4-F01: round 3 added post-write control tombstones (2.1.14) without revising round 1's 2.1.9, which still requires every timed-out control request to leave the scope reusable — a direct contradiction, and §3.2 has no transition that retires a tombstoned attachment, so a pane can be permanently unable to regain control. Split the criterion at the transport-write boundary and route recovery through §3.2's existing bounded detach/finalize/fresh-attach path. R4-F09: `Daemon::close` (added round 3) rejects later `reconnect` calls but never fences a reconnect episode already paused in handshake, which can install a sink, reader, and ready generation after close returns. R4-F10: round 3's async codec siblings promise "no partially-consumed frame after a cancelled read" and an independent whole-socket handle beside consuming split halves; neither is implementable — bytes consumed from an `AsyncRead` cannot be rolled back by a stateless helper, and `into_split` moves ownership into the halves. R4-F12: round 2 merged backend timeout with task cancellation, so 1.1.6 requires an `indeterminate` reply on a cancellation whose socket is gone while the ledger prose requires `CancelledError` to re-raise. R4-F13: round 3's immediate read-only requirement names no signal — `DaemonEvent` has no disconnected variant, so the run loop has no guaranteed wakeup before the supervisor's first reconnect attempt. R4-F04 (nit): round 1 corrected §1.5's inventory to the live 31-file corpus but left Constraints at 32; `ls` and `GOLDEN_NAMES` both confirm 31.

The rest are original defects the earlier rounds missed. R4-F08 is the sharpest: §2.1 tells `LiveDaemon` to subscribe, page `terminal_list`, install the roster, replay, and publish generation-ready, but `Daemon::reconnect(observed)` carries no project while `list_terminals` requires one, and nothing on the trait installs state — the section's own attention paragraph already argues that putting snapshot installation in the transport layer is a second reducer with no consumer, and then does exactly that for the terminal roster. The fix applies the plan's own principle consistently: `Workspace` owns page traversal, roster installation, replay, and the readiness boundary; `LiveDaemon` supplies the subscription and one-page REST operation. R4-F06: `_start_proxy_attach` opens the frame connection before `ProxyHub.start_proxy`, which registers it only after `handshake` and `attach_terminal` — so the `host_unavailable` path this plan adds leaks an open host socket that `registry.finalize` cannot reach. R4-F07: `emit_lifecycle` awaits queue admission only; `SocketRelay._run` performs the send later, so committed high-water can advance on bytes that have not been published. R4-F11: `start_proxy` starts the pump before `_send_json(terminal_attach_result)`, so initial history or keyframe can broadcast before any receiver exists. R4-F05: `app/mod.rs` imports `load_snapshot` and never `save_snapshot`, and Workspace owns the layout, tab-order, and focus mutations §3.3 promises to persist.

The two deferral findings are verified against live task rows. R4-F02: #20201 carries `deferred-from:herdr-terminal-client:D1` — the source plan's D1 is the plugin epic — while §5.1 asserts it carries legacy D3, so the specified same-section lookup returns zero and the stop-on-unexpected branch blocks adoption. Mapping made explicit: current D2 adopts #20202 from legacy D2, current D3 adopts #20201 from legacy D1. R4-F03: `docs/contracts/plan-coverage.md` requires each deferral task to carry dependencies on internal leaves it needs, not only external prerequisites; one `blocked-by` edge from each of D1/D2/D3 to 5.1 is the smallest ordering that holds.

Two restraint-minimal deviations, both recorded as partial declines of the proposed mechanism rather than of the defect. R4-F10: the defect is accepted, the fix is not — dedicated reader and writer owner types are more structure than the problem needs. Cancelling a mid-frame operation retires the connection, the async siblings stay plain helpers sharing the sync codec's bincode call and size bound, and 2.2.9 changes from byte rollback to typed connection retirement plus whole-socket shutdown through reunited halves. R4-F14: `_contract_deferrals` compiles `spec["deferrals"]` and `apply_run` reads only `phases`, `tasks`, and `dependencies` — nothing consumes it, so §5.1 does name a step that does not exist. Building a deferral-application surface in the expansion engine is declined `over-mechanism`: it is a subsystem feature outside this epic, and this plan's own expansion could not use it anyway. The executable owner is named for what it actually is — the coordinator running expansion through `gobby-tasks` MCP calls — and 5.1.4 drops its failure-injection framing for the end-state repair check a coordinator can actually verify.

```json plan-review-round
{"evidence_id":"d03da42e-a575-4f1d-ab86-e8bf5aaec235","plan_hash":"1d234cb92008190d6c7852f22e864ab29d7e87fe510402d3819faa4f0c53fcb4","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"8ec711bcb82666aab763c1edd118c0a670e89a37be6c6da26ea0e8c1624ec074","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":14,"total":16},"evidence_id":"d03da42e-a575-4f1d-ab86-e8bf5aaec235","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":10,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"0dd7c3008a9d4eb4ff5ca439041863600b8c439275fabd2a3ab815140d97391f","status":"valid"},"source_digest":"990850872b52787c8aa4bf8609b434dd3dd3712b0f74d3d04a1d97b0a13416a5","version":1},"findings":[{"category":"unhandled-edge","causal_finding_id":"R3-F02-control-timeout-late-reply-alias","causal_section_ids":["2.1"],"check_key":"control-timeout.contract-and-recovery","description":"Acceptance 2.1.9 requires another control request on the same attachment to be admitted after every timeout or cancellation, while 2.1.14 requires post-write timeout or cancellation to tombstone that attachment and refuse reuse until detach, finalization, or fresh attach. Section 3.2 only discards pending input on deadline; it never retires a control-tombstoned attachment, so the pane can remain permanently unable to regain control.","finding_id":"R4-F01-control-timeout-contract","fix":"Rewrite 2.1.9 around the transport-write boundary: request-id/write-sequence keys remain reusable after exact-key removal, pre-write control cancellation remains reusable, and post-write control timeout/cancellation drains the waiter while tombstoning the attachment. Add a distinct typed tombstone/indeterminate-control result and make §3.2 drive its existing bounded detach/finalize-or-reconnect/fresh-attach path before another take or release.","introduced_in_round":3,"location":"§2.1 correlation cleanup, acceptance 2.1.9/2.1.14, and §3.2 focus-follows-control","participating_section_ids":["2.1","3.2"],"prevention":"For each correlation key, distinguish pre-write cancellation from post-write uncertainty and trace every typed refusal into a state transition that restores usability.","principle":"A non-unique correlation scope must have one satisfiable reuse policy and an application recovery path after its outcome becomes indeterminate.","root_cause":"Round 3 added post-write control tombstones without revising the earlier all-timeouts-are-reusable criterion or giving panes a transition that retires the tombstoned attachment.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"deferral.legacy-section-remap","description":"The cited source plan defines #20201 as `herdr-terminal-client` D1, and live #20201 carries `deferred-from:herdr-terminal-client:D1`. Section 5.1 says it carries legacy D3 and requires an unambiguous same-section D3 lookup; that lookup returns zero and the specified stop-on-unexpected branch prevents adoption.","finding_id":"R4-F02-deferral-legacy-id","fix":"Make the mapping explicit: current D2 adopts #20202 from legacy D2, while current D3 adopts #20201 from legacy D1. Preserve those exact legacy labels beside `deferred-from:herdr-client-completion:D2`/`:D3`, and replace the `<D>` shorthand in 5.1.3 with the two concrete pairs.","location":"§5.1 D2/D3 adoption preflight, acceptance 5.1.3, and D3 typed deferral","participating_section_ids":["5.1","D3"],"prevention":"For every adopted task_ref, compare the live legacy label and source-plan deferral section before writing a cross-plan provenance mapping.","principle":"A provenance migration must map the new section identity to the exact legacy section identity owned by the designated task.","root_cause":"The completion plan reused its new D3 label as the previous plan's lookup key, although the previous plan assigned the plugin task #20201 to D1.","section_id":"5.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"deferral.internal-tail-ordering","description":"D1 is explicitly planned on the tree this epic lands, D2 consumes the proxy source and daemon-addressable client, and D3 consumes the imported client chrome/public surface. None receives an internal leaf dependency, so the planning tasks can become dispatchable as soon as expansion creates or adopts them despite being declared tail work.","finding_id":"R4-F03-deferral-internal-order","fix":"During expansion/finalization, add one internal blocked-by edge from each D1, D2, and D3 task to §5.1; 5.1 already depends on P4, so this single common edge is the smallest ordering that guarantees the complete client epic has landed. Retain D2's #19600/#19647 external blockers and assert all edges in 5.1.2/5.1.3.","location":"§5.1 expansion/finalization adoption and D1-D3 dependency closure","participating_section_ids":["5.1","D1","D2","D3"],"prevention":"For every deferral, list each consumed deliverable and verify the adopted/created task receives an internal blocked-by edge before expansion validation.","principle":"A deferred task carries blocked-by edges for every internal leaf whose output it consumes, as well as external prerequisites.","root_cause":"The adoption prose enumerates only D2's external blockers and treats tail-work parenting as sufficient ordering.","section_id":"5.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"golden-corpus-targets","causal_section_ids":["1.5"],"check_key":"goldens.constraints-baseline-parity","description":"Constraints still says the source corpus and both consumers contain 32 files, while the executable §1.5 inventory correctly settles 31 moved fixtures, five additions, 36 replayable entries, and 37 final directory files.","finding_id":"R4-F04-golden-count-framing","fix":"Change the two stale Constraints references from 32 to 31 and leave §1.5's 31 + 5 = 36 entries / 37 total files arithmetic unchanged.","introduced_in_round":1,"location":"Constraints wire facts versus §1.5 inventory and acceptance 1.5.1/1.5.3","prevention":"When an inventory count changes, search the full immutable plan snapshot for every old count before closing the revision.","principle":"A settled repository baseline has one count across framing, Targets, implementation prose, and acceptance.","root_cause":"The round-1 inventory repair corrected §1.5 to the live 31-file corpus but left the Constraints paragraph at 32.","section_id":"1.5","severity":"nit"},{"category":"traceability","check_key":"persistence.production-save-carrier","description":"`persist.rs` can atomically serialize a supplied snapshot, but current Workspace only loads snapshots and owns the mutations §3.3 promises to save. Without `app/mod.rs`, the leaf can pass standalone helper tests while production changes never call the saver.","finding_id":"R4-F05-persistence-save-carrier","fix":"Add the existing file-wide Workspace carrier to §3.3 and require a test that mutates layout, tab order, and focus through Workspace while a racing reader observes only complete old-or-new JSON.","location":"§3.3 Targets, persistence prose, and acceptance 3.3.3","prevention":"For every 'persist on change' claim, trace the saver from each production mutation site and target both the helper and its caller.","principle":"A persistence deliverable must own the production state-mutation carrier that invokes its serializer.","repairs":[{"entries":["`crates/gclient/src/app/mod.rs::*` — scope-reason: Workspace layout, tab-order, and focus mutation paths invoke atomic snapshot persistence"],"kind":"add_targets","section_id":"3.3"},{"items":[{"artifact":"test: `crates/gclient/tests/persist.rs::workspace_mutations_persist_atomically`","prose":"Workspace layout, tab-order, and focus mutations invoke atomic persistence, and a racing reader observes only complete old-or-new workspace.json snapshots."}],"kind":"add_acceptance","section_id":"3.3"}],"root_cause":"Targets include the atomic save helper and tests but omit Workspace, where layout, tab-order, and focus mutations occur.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"proxy-attach.transactional-rollback","description":"A handshake or host-attach exception after `open_proxy_frame` leaves an open frame connection outside ProxyHub's attachment maps. The planned `registry.finalize` removes the lease record only, so the host observer/socket leaks on the new `host_unavailable` path.","finding_id":"R4-F06-proxy-attach-rollback","fix":"Make proxy startup transactional. Once frame open succeeds, close it on handshake, host-attach, or registration failure and roll back any proxy map/task installed before returning the typed attach error. Extend 1.2.1 with post-open, handshake, attach, and registration failure injection proving no lease, proxy record, reader task, or frame remains.","location":"§1.2 typed proxy-attach failures and §1.3 ProxyHub startup","participating_section_ids":["1.2","1.3"],"prevention":"Inject failure after every acquisition/registration step and assert that sockets, maps, tasks, and lease records return to their pre-attempt state.","principle":"Every resource acquired before an attach commits must be released on every later failure boundary.","root_cause":"The frame socket opens before `ProxyHub.start_proxy`, while ProxyHub registers it only after handshake and host attach; lease finalization cannot discover an unregistered frame.","section_id":"1.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"lifecycle-publication-order","causal_section_ids":["1.4","2.1"],"check_key":"lifecycle.relay-publication-ack","description":"The new ordered registry consumer can await `emit_lifecycle`, mark sequence N committed, and publish N+1 through a direct broadcaster while N remains in SocketRelay's queue. Observers can still receive N+1 before N, and a snapshot can claim a high-water whose bytes were never published.","finding_id":"R4-F07-lifecycle-relay-ack","fix":"Give lifecycle queue entries a completion future settled after WebSocket send success or failure, have `emit_lifecycle` await it, and advance committed high-water only afterward. Target the relay queue carrier and cover a paused relay followed by a direct lifecycle emission and snapshot read.","introduced_in_round":1,"location":"§1.4 ordered publisher Targets and committed-high-water acceptance","prevention":"For every asynchronous fanout adapter, trace 'await' through to the physical send and pause the downstream sender while checking sequence and snapshot high-water.","principle":"A committed lifecycle high-water advances only after the corresponding bytes have completed their actual WebSocket send.","repairs":[{"entries":["`src/gobby/servers/websocket/proxy_relay.py::_Queued`","`src/gobby/servers/websocket/proxy_relay.py::SocketRelay`"],"kind":"add_targets","section_id":"1.4"},{"items":[{"artifact":"test: `tests/servers/test_terminal_list_watermark.py::test_proxy_relay_ack_precedes_committed_high_water`","prose":"With the proxy relay sender paused after enqueue, committed lifecycle high-water does not advance and no later direct lifecycle event is observed first; releasing the sender publishes both in strictly increasing sequence order."}],"kind":"add_acceptance","section_id":"1.4"}],"root_cause":"`ProxyHub.emit_lifecycle` awaits queue admission only; `SocketRelay._run` performs the send later and queue entries carry no completion acknowledgement.","section_id":"1.4","severity":"blocking"},{"category":"over-engineering","check_key":"terminal-reconciliation.single-state-owner","description":"LiveDaemon is told to subscribe, traverse terminal pages, install the roster, replay events, and publish readiness, yet `Daemon::reconnect(observed)` carries no project and returns only a generation, while Workspace owns panes and receives no reconciled roster. Implementing this literally requires hidden retained project/state or a second transport-layer reducer with no concrete state consumer.","finding_id":"R4-F08-terminal-reconcile-owner","fix":"Use the existing Workspace ownership shape: Workspace owns project-scoped page traversal, roster installation, buffered replay, and the point at which a generation becomes ready; LiveDaemon supplies the socket subscription and one-page REST operation. Rewrite 2.1.1/2.1.5/2.1.8/2.1.10/2.1.11 and §3.2 reconnect behavior around that single owner, with no parallel roster reducer or hidden project state.","location":"§2.1 terminal subscribe/list/install/replay handshake and §3.2 Workspace run loop","participating_section_ids":["2.1","3.2","3.4"],"prevention":"For every subscribe-snapshot-replay loop, name one state owner and verify every required input and output crosses the advertised trait.","principle":"Reconciliation orchestration belongs to the component that owns the state being installed; transport supplies pages and events.","root_cause":"The plan assigns terminal roster installation and replay to LiveDaemon, which has no Workspace state sink, project argument on reconnect, or roster output in generation-ready.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R3-F12-daemon-close-contract","causal_section_ids":["2.1","3.3"],"check_key":"daemon-close.inflight-reconnect-fence","description":"An in-flight reconnect can resume after `close()` returns and install a new sink, reader, and generation-ready value. The exit latch cancels future policy attempts, but it does not define ownership of the already-admitted primitive attempt.","finding_id":"R4-F09-close-reconnect-fence","fix":"Make `close` cancel and await the in-flight reconnect episode, settle all joiners with the terminal close error, and require every reconnect attempt to recheck a terminal generation fence immediately before publishing a sink, reader, or ready value. Add a paused-handshake race test proving no resource appears after close returns.","introduced_in_round":3,"location":"§2.1 `Daemon::close`, reconnect single-flight, and acceptance 2.1.15/3.3.9","participating_section_ids":["2.1","3.2","3.3"],"prevention":"Race close against every await boundary of reconnect and assert no post-close resource installation or readiness publication.","principle":"A terminal close operation must fence work admitted before close as well as reject work admitted afterward.","root_cause":"The close contract stops current reader/sink and rejects later reconnect calls but never cancels or joins a reconnect already paused in handshake.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R3-F10-async-codec-boundary","causal_section_ids":["2.2"],"check_key":"tokio-codec.cancel-safe-owner","description":"A generic async `read_message(&mut R)` consumes bytes before its future can be cancelled and cannot restore function-local prefix/payload buffers on the next call. Likewise, splitting the owned UnixStream transfers ownership into the halves, so the stated independent whole-socket handle is absent. The current 2.2.9 promise of no partial consumption and whole-handle shutdown is not implementable as written.","finding_id":"R4-F10-async-codec-cancellation","fix":"Use dedicated reader and writer owners for the two halves, keep pure bincode validation and the size bound shared with the sync codec, and never cancel then reuse an individual mid-frame operation. On cancellation, abort/join both owners and drop/shutdown both halves so the connection is retired. Change 2.2.9 from byte rollback to typed connection retirement and test paused prefix, payload, and write boundaries.","introduced_in_round":3,"location":"§2.2 async codec prose and acceptance 2.2.9","prevention":"For every async framing API, drop its future after partial prefix, payload, and write progress and specify whether the same connection resumes or is retired.","principle":"Cancellation of a stateful framed stream either preserves parser state under a persistent owner or retires the connection; consumed bytes cannot be rolled back.","root_cause":"Round 3 requested stateless async siblings plus an independent whole-socket handle beside consuming split halves, neither of which supplies cancellation-safe ownership.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"proxy-attach.subscribe-before-send","description":"Initial attach history or keyframe can be broadcast while no attachment receiver exists, so the pane may begin with a delta or never render. The same gap recurs during lag recovery and every fresh proxy attach.","finding_id":"R4-F11-proxy-attach-subscribe-race","fix":"Obtain a daemon event receiver before writing `terminal_attach` and pass that already-buffering receiver into ProxyFrameSource after the reply; filter it by the returned attachment id. Cover history/keyframe before the result, between result settlement and source construction, and immediately afterward, applying the state boundary exactly once before any delta.","location":"§1.3 ProxyHub pump, §2.1 broadcast, §2.2 ProxyFrameSource construction, and §3.2 attach state machine","participating_section_ids":["1.3","2.1","2.2","3.2"],"prevention":"Enumerate event-before-reply, event-between-reply-and-construction, and event-after-construction schedules for every attach and reattach.","principle":"A delta-stream consumer must subscribe before the producer can emit its initial state boundary.","root_cause":"The server starts ProxyHub's pump before sending `terminal_attach_result`, while the client constructs and subscribes ProxyFrameSource only after that reply supplies the attachment id.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"chunked-write-wire-outcome","causal_section_ids":["1.1"],"check_key":"write-input.cancellation-wire-contract","description":"The prose requires a client-disconnect `CancelledError` to close the write ledger and re-raise, but 1.1.6 requires cancellation mid-chunk to return `terminal_write_outcome{outcome: indeterminate}`. The current handler sends outcomes only after awaited delivery returns, so both outcomes cannot occur on the same branch.","finding_id":"R4-F12-input-cancel-outcome","fix":"Separate backend timeout from handler cancellation. A backend timeout while the connection remains usable returns the existing indeterminate outcome; client-disconnect task cancellation completes the ledger in `finally` and re-raises without promising a reply. Rewrite 1.1.6/1.1.7 to test the two branches independently.","introduced_in_round":2,"location":"§1.1 ledger cleanup prose and acceptance 1.1.5-1.1.7","prevention":"For each cancellation source, trace whether the response transport still exists and assert ledger state independently from reply delivery.","principle":"A cancelled connection handler cannot both re-raise cancellation and promise a reply on the disappearing WebSocket.","root_cause":"The round-2 repair merged backend timeout/uncertainty with task cancellation while separately requiring `CancelledError` to propagate after ledger cleanup.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R3-F16-daemon-loss-readonly","causal_section_ids":["3.2"],"check_key":"daemon-loss.transport-signal","description":"LiveDaemon clears readiness and waiter maps on EOF/read error, but `DaemonEvent` contains no disconnected variant and the plan does not say the broadcast receiver closes per generation. The run loop therefore has no guaranteed wakeup that clears leases, control, and pending input before the supervisor issues its first reconnect.","finding_id":"R4-F13-daemon-loss-signal","fix":"Emit one `DaemonEvent::Disconnected { generation, error }` after readiness is cleared and before reconnect joiners are awakened. The run loop consumes it by clearing lease/control/input state before submitting the generation-tagged recovery intent; add a paused-first-attempt test proving that order.","introduced_in_round":3,"location":"§2.1 DaemonEvent/reader-loss contract and acceptance 3.2.11","participating_section_ids":["2.1","3.2"],"prevention":"For every 'before the first retry' requirement, identify the exact select-loop input and assert its ordering relative to recovery admission.","principle":"An immediate state transition needs an explicit, ordered signal from the component that observes the triggering failure.","root_cause":"Round 3 added immediate read-only behavior without adding a disconnect event or defining per-generation broadcast-sender closure as the run-loop wakeup.","section_id":"3.2","severity":"blocking"},{"category":"missing-requirement","check_key":"deferral.adoption-executable-owner","description":"Section 5.1 moves D1 creation and D2/D3 multi-write adoption before leaf execution, yet names no callable surface that performs it. The docs leaf explicitly mutates nothing, and 5.1.4 cites `evolution.md` as evidence for failure injection. Unanswered requirements are: which tool consumes compiled deferrals, where the all-task preflight/commit boundary lives, and which executable artifact proves rerun convergence after each mutation boundary.","finding_id":"R4-F14-deferral-adoption-owner","fix":"Name the exact expansion/finalization tool that consumes compiled deferrals and its isolated task-store test; require that surface to preflight all candidates/dependencies, converge parent/content/criteria/edges, write provenance last, and rerun after each injected boundary. If no such surface exists, add an explicit targeted deliverable for it rather than assigning migration evidence to the docs leaf.","location":"§5.1 expansion-time adoption algorithm and acceptance 5.1.2-5.1.4","participating_section_ids":["5.1","D1","D2","D3"],"prevention":"Before relocating work to expansion/finalization, trace the compiled field to the exact apply tool and an isolated failure-injection test.","principle":"A required pre-expansion migration needs a named executable owner, mutation boundary, and objective recovery artifact.","root_cause":"The compiler serializes typed deferrals, but expansion apply consumes phases, tasks, and dependency edges only; no finalization surface in the repository consumes `spec.deferrals`.","section_id":"5.1","severity":"blocking"}],"reviewer_session":"#11239","round":4,"verdict":"needs_review"},"session_id":"f372f69c-f06e-456f-8364-9422225513c4"}
```

Round 5 was the last approved round. It returned needs_review with fourteen blocking findings (fourteen candidates, zero dismissed). Thirteen were accepted and one declined, and every one was ground against the live repository before the vote.

Six are fixer-induced chains on round 4's own repairs, and the adversary named each causal finding. R4-F12's write-cancellation split left the old umbrella phrase standing in 1.1.5, so one boundary was required both to raise TerminalWriteError(stage="partial") on cancellation and to re-raise CancelledError with no reply; 1.1.5 is now scoped to backend timeout alone and handler-task cancellation belongs to 1.1.7. R4-F08's ownership reversal left three stale sentences and one acceptance clause still assigning subscribe/list/replay to a directly driven LiveDaemon, and left the four-case readiness definition attached to roster completion; the same reversal opened a second hole, since generation-ready now means socket-connected and reset the reconnect budget before Workspace's roster handshake ran — repeated socket success with repeated listing failure could reset that budget forever while every pane stayed read-only. The episode now ends only when Workspace acknowledges reconciliation, and typed handshake failure feeds the same five-attempt budget. R4-F10 chose connection retirement correctly and expressed it wrongly: a dropped Rust future cannot return FramingError::Cancelled, so the outcome moves to the source-owned reader and writer tasks that survive the drop, with the helpers left plain and cancellation-unsafe. R4-F07 gave the ordered lifecycle path a queue and a per-entry completion future with no capacity, no saturation rule, and no owner for an already-admitted entry whose submitter cancels. R4-F09 made close join an in-flight reconnect episode while 3.3's two-second exit deadline still ended before close was called, so the exit guarantee 3.3.4 states was unbounded in practice. And R4-F14 named the coordinator and the convergence rule but gave only D2 and D3 a provenance lookup, leaving D1's branch an unconditional create that a rerun would duplicate.

Five are coverage gaps against promises the plan makes in its own prose. Acceptance 1.5.3 reduced the safe-integer contract to a single 2^53 refusal though the plan claims parity across message_seq, lease_generation, and client_write_seq. Acceptance 2.1.1 checked cursor traversal alone, dropping the carried source requirement (herdr-terminal-client 3.3.21) that the roster selects pending|live, never uses an unpaged dump, and fetches retained history through a separate explicit query. Section 3.3 promises OSC 52 clipboard output and a scrolled-away new-output indicator that 3.3.1 never observes. Section 3.4 names ~/.gobby/local_cli_token as the --token-file default while 3.4.2 exercises only the explicit override, so an ignored or misresolved default passes. And 3.2 requires run_ready to arm a TerminalModeGuard whose staged implementation and teardown.rs carrier are owned by 3.3, a later leaf that already depends on 3.2.

One is a consumer-inventory miss with a typed repair, and it is the only repair applied by the server. web/tests/style-surfaces.spec.ts and web/tests/terminal-colors.spec.ts still mock terminal_attach_result with streaming_id while 1.2 makes attachment_id the sole terminal_output routing key; history-perf.spec.ts and terminal-history-scroll.spec.ts already carry attachment_id, so the inventory is exactly those two. Neither is in Guard set H, so the leaf's own close gate would not have caught the silent breakage. Both are added to 1.2 Targets file-wide.

One finding was declined on a verified false premise. R5-F05 asked for a Zig-absent build proving "the VT-engine default never enters the dependency graph," but crates/gterminal/Cargo.toml declares default = [] with vt-engine opt-in, and crates/gclient/Cargo.toml already pins gobby-terminal with default-features = false. There is no VT-engine default to exclude, the plan introduces no Zig-requiring edge, and a bespoke Zig-free PATH environment in the close gate would be mechanism guarding a state the manifest already makes unreachable.

Five further findings were accepted as defects with their proposed mechanism declined as over-mechanism, and in four of those the adversary's own fix prose already said to extend the existing criterion while its typed repair minted a second acceptance item and a second test. R5-F01 through R5-F04 are therefore repaired by extending 1.5.3, 2.1.1, 3.3.1, and 3.4.2 in place, which matches the plan's established compound-criterion style and adds no artifact. R5-F01 in particular guards a test the repository already carries: crates/gclient/tests/ws_golden.rs::seq_and_lease_generation_are_safe_integers already proves distinct 2^53-2 and 2^53-1 encodings, string and float rejection, and encode-side enforcement, so the defect is the criterion understating it, not a missing test. R5-F06 was accepted as a real ownership error and repaired at rung 1 rather than the adversary's rung 3: crates/gclient/src/teardown.rs::TerminalGuard already exists and is armed in startup.rs before run_ready is called, so 3.2 names that guard and 3.3 keeps sole ownership of the rename to the stage-aware TerminalModeGuard. Moving teardown.rs and a partial staged implementation into 3.2 would have split one file's rework across two leaves for no gain.

One defect the round did not report was fixed alongside them: 1.4's closing sentence still read that the client half of the ordering invariant lives in LiveDaemon, which R4-F08 moved to Workspace and which 2.1.8 now explicitly denies.

Round 5 reached the configured cap of five. Every finding was processed and voted before any edit, this rejection checkpoint was appended and finalized against the unchanged artifact, the accepted repairs were applied, and the artifact was base-validated. No further adversary round was launched.

```json plan-review-round
{"evidence_id":"64e3bb34-efb2-402d-ac27-7587f2f39b66","plan_hash":"6b9a331c5953fbf1894868fe85861380cfc56833a26a13433b104845d79f7c72","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f9625332c497c2ed98377bf3c294b47d65ab43c6df57d295d5e2bd76ef9a68df","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":14,"total":14},"evidence_id":"64e3bb34-efb2-402d-ac27-7587f2f39b66","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"2cfb3c0bf80b565f80a8552625e338536f058707c1f379a97bfb2a06bafadc3e","status":"valid"},"source_digest":"5ef622a56d2e4d819bb40bed1de1325a5a28fdc7f51942a32f608289559755eb","version":1},"findings":[{"category":"traceability","check_key":"source.safe-integer-roundtrip","description":"The plan claims source safe-integer parity, but 1.5.3 proves only that Rust rejects 2^53. It does not prove message_seq, lease_generation, and client_write_seq remain JSON numbers, preserve distinct 2^53-2 and 2^53-1 values, reject strings/floats, or enforce the bound when producing messages.","finding_id":"R5-F01-safe-integer-contract","fix":"At restraint rung 2, add one acceptance item that drives all three fields through decode, comparison, and production emission; proves 2^53-2 and 2^53-1 remain distinct JSON integers; and rejects 2^53, strings, and floats.","location":"Acceptance 1.5.3 and the sequence fields consumed by 2.1/3.2","participating_section_ids":["1.5","2.1","3.2"],"prevention":"For every cross-language integer field, test both largest safe values, the first invalid value, alternate JSON types, and production emission.","principle":"Protocol range requirements need positive boundary, type, and producer/consumer coverage; a single out-of-range rejection is not round-trip proof.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/ws_golden.rs::safe_integer_fields_round_trip_at_the_json_boundary`","prose":"All message_seq, lease_generation, and client_write_seq fixtures remain JSON numbers through Rust decode/comparison and production emission, preserve distinct 2^53-2 and 2^53-1 values, and reject 2^53, strings, and floats."}],"kind":"add_acceptance","section_id":"1.5"}],"root_cause":"The corpus repair retained only the old refusal-at-2^53 assertion and dropped the source obligation for all three numeric sequence fields at both safe boundary values and on outbound emission.","section_id":"1.5","severity":"blocking"},{"category":"traceability","check_key":"source.roster-filter-history-separation","description":"The source roster requirement is pending|live rows via cursor paging, with no unpaged dump and retained history fetched separately. Acceptance 2.1.1 checks generic pagination only, so a mock returning exited/history rows can pass.","finding_id":"R5-F02-roster-filter-history","fix":"At restraint rung 2, extend the existing 2.1.1 test to inspect every request, require pending|live filtering while following next_cursor, forbid the unpaged endpoint, and prove retained history is requested only through a distinct explicit operation.","location":"Workspace terminal roster traversal and acceptance 2.1.1","prevention":"For every carried source query, assert endpoint, filters, pagination, and separation from adjacent historical views.","principle":"A paginated roster contract includes its selection predicate and history boundary, not only cursor traversal.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/daemon_live.rs::roster_filters_live_rows_and_separates_history`","prose":"Every Workspace roster page request selects only pending|live rows while following next_cursor, never calls an unpaged dump, and retained history is fetched only through a distinct explicit operation."}],"kind":"add_acceptance","section_id":"2.1"}],"root_cause":"The ownership rewrite moved paging into Workspace but described a generic list operation, losing the source requirement that the live roster excludes retained history.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"copy-mode.osc52-indicator","description":"Section 3.3 promises OSC 52 clipboard output and a new-output indicator while scrolled away, but 3.3.1 checks history, offsets, wrapping, and non-mutation only. Both outputs can be absent while the named test passes.","finding_id":"R5-F03-copy-output-observability","fix":"At restraint rung 2, extend the existing copy-mode acceptance to assert the exact OSC 52 payload and that live output received while scrolled away renders the indicator without changing the observer offset.","location":"Copy-mode prose and acceptance 3.3.1","prevention":"Map every promised UI side effect to a named assertion at the surface where the user observes it.","principle":"Acceptance must observe each user-visible output promised by the behavior, not only the reducer state that precedes it.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/copy_paste.rs::copy_emits_osc52_and_scrolled_output_shows_indicator`","prose":"Copy mode emits the selected logical text as the exact OSC 52 payload, and live output received while scrolled away renders the new-output indicator without changing that observer's offset."}],"kind":"add_acceptance","section_id":"3.3"}],"root_cause":"The copy-mode criterion covers selection and scroll state but omits both external clipboard emission and the scrolled-away output indicator.","section_id":"3.3","severity":"blocking"},{"category":"weak-testability","check_key":"startup.token-file-default","description":"The plan names ~/.gobby/local_cli_token as the --token-file default, but 3.4.2 tests only an explicit override. Startup can ignore or misresolve the default credential path and still pass.","finding_id":"R5-F04-token-default-observability","fix":"At restraint rung 2, add a no-flag startup case under a controlled home that proves the default file supplies the bearer and that missing or unreadable default credentials fail actionably before terminal-mode mutation.","location":"Named defaults, startup discovery prose, and acceptance 3.4.2","prevention":"For every CLI path option, test the default under a controlled home plus explicit override and missing/unreadable failure.","principle":"A documented default needs a no-override test; testing only the override cannot detect an ignored or misresolved default.","repairs":[{"items":[{"artifact":"test: `crates/gclient/tests/startup.rs::token_file_defaults_before_raw_mode`","prose":"With no --token-file flag and a controlled home, startup reads ~/.gobby/local_cli_token as the bearer; a missing or unreadable default token fails actionably before raw mode and before any daemon request."}],"kind":"add_acceptance","section_id":"3.4"}],"root_cause":"The startup criterion exercises --token-file explicitly and never enters the default-path branch.","section_id":"3.4","severity":"blocking"},{"category":"weak-testability","check_key":"build.zig-free-gterminal-features","description":"The standing constraint requires gobby-client to build without Zig and retain gobby-terminal with default-features=false, but both leaves that edit Cargo lack acceptance pinning that contract or exercising a Zig-absent build.","finding_id":"R5-F05-zig-free-build-contract","fix":"At restraint rung 2, add one final-Cargo acceptance that requires gobby-terminal default-features=false and builds gobby-client with Zig unavailable on PATH, proving the VT-engine default never enters the dependency graph.","location":"Standing Zig-free constraint, Cargo targets in 2.1/3.1, and Guard set H","participating_section_ids":["2.1","3.1"],"prevention":"For optional native toolchains, inspect the final feature graph and run the consuming build with the tool removed from PATH.","principle":"A dependency-feature constraint is executable only when the final Cargo owner pins the feature graph and validates it without the forbidden tool.","repairs":[{"items":[{"artifact":"test: `cargo build -p gobby-client` under the Zig-free Guard set H environment","prose":"Cargo metadata keeps gobby-terminal at default-features = false, and gobby-client builds with Zig unavailable on PATH, proving the VT-engine default is absent from the client dependency graph."}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"Guard set H builds in the ambient environment and neither Cargo-owning leaf asserts default-features=false after its edits.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"terminal-guard.provider-before-consumer","description":"Section 3.2 requires run_ready to arm the final staged TerminalModeGuard and prove restoration, but 3.3 is the later leaf that owns teardown.rs and the guard's completed semantics. The dependency direction makes 3.2 non-self-contained.","finding_id":"R5-F06-terminal-guard-order","fix":"At restraint rung 3, move the minimum staged TerminalModeGuard implementation, teardown.rs target, and restoration proof into 3.2; leave 3.3 to extend teardown with ordered remote cleanup and later shutdown cases. Do not introduce a new guard abstraction or cyclic dependency.","location":"3.2 run_ready/3.2.1 versus 3.3 teardown ownership and dependency direction","participating_section_ids":["3.2","3.3"],"prevention":"For each acceptance artifact, trace every required provider target and ensure it is owned by the same or an earlier dependency leaf.","principle":"A leaf must own or depend on the implementation needed to satisfy its own close criteria.","root_cause":"3.2 requires TerminalModeGuard behavior while the guard's staged implementation remains owned by later leaf 3.3, which already depends on 3.2.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"terminal-output.playwright-consumer-inventory","description":"Two untargeted Playwright seams still return streaming_id/run_id while 1.2 makes attachment_id the sole terminal_output routing key. TerminalTab filters by the active attachment, so their output is discarded and the tests cannot reach their intended visual assertions.","finding_id":"R5-F07-playwright-output-consumers","fix":"At restraint rung 2, add the two existing specs to 1.2 Targets and update only their mocked attach/output shapes to terminal_id plus attachment_id.","location":"1.2 Targets versus web Playwright WebSocket mocks","prevention":"Search every literal terminal_attach_result and terminal_output producer when changing the routing-key contract, including browser fixtures.","principle":"When a wire-routing key changes, every production and test producer of that frame shape must migrate in the owning leaf.","repairs":[{"entries":["`web/tests/style-surfaces.spec.ts::*` — scope-reason: migrate its terminal attach/output WebSocket mock to attachment_id routing","`web/tests/terminal-colors.spec.ts::*` — scope-reason: migrate its terminal attach/output WebSocket mock to attachment_id routing"],"kind":"add_targets","section_id":"1.2"}],"root_cause":"The deterministic inventory covered the hook and server emitters but missed two browser-level mock producers still using streaming_id/run_id.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-F12-input-cancel-outcome","causal_section_ids":["1.1"],"check_key":"write-input.cancellation-wire-contract","description":"The revised body and 1.1.7 require handler-task cancellation to close the ledger, re-raise CancelledError, and send no reply, while 1.1.5 still says cancellation at any chunk invocation raises TerminalWriteError(stage=partial). The same boundary cannot satisfy both.","finding_id":"R5-F08-write-cancellation-contract","fix":"At restraint rung 2, replace 'timeout or cancellation' in 1.1.5 with 'backend timeout' and leave handler-task cancellation exclusively to 1.1.7; if a distinct backend cancellation exists, name its typed exception and state that it is not asyncio.CancelledError.","introduced_in_round":4,"location":"Acceptance 1.1.5 versus revised cancellation body and 1.1.7","prevention":"After splitting an exception branch, search every acceptance item for the old umbrella term and assign each occurrence to exactly one outcome.","principle":"Backend failure and cancellation of the owning connection task are distinct terminal paths and cannot share contradictory return contracts.","root_cause":"Round 4 separated handler cancellation from backend failure but left the earlier catch-all cancellation phrase in 1.1.5.","section_id":"1.1","severity":"blocking"},{"category":"over-engineering","causal_finding_id":"R4-F08-terminal-reconcile-owner","causal_section_ids":["2.1","3.2"],"check_key":"terminal-reconciliation.single-state-owner","description":"The new contract makes LiveDaemon transport-only and Workspace the subscribe/list/install/replay owner, yet stale readiness prose and 2.1.7 still require a directly driven LiveDaemon to perform one subscribe/list/replay sequence. Implementing that clause would add hidden project state and a second reducer with no consumer.","finding_id":"R5-F09-reconnect-owner-stale-prose","fix":"At restraint rung 1, delete the stale roster-ready/four-case wording and narrow 2.1.7 to one replacement socket handshake, one reader, and one generation-ready publication shared by joiners. Keep all roster assertions in the existing Workspace-owned criteria; add no coordination layer or transport-side roster state.","introduced_in_round":4,"location":"Readiness/four-case reconnect prose and acceptance 2.1.7 versus the new Workspace owner","participating_section_ids":["2.1","3.2"],"prevention":"When moving an orchestration boundary, search the entire section and its acceptance for every old owner/action pair, not only the primary algorithm paragraph.","principle":"Transport reconnect should publish transport state; project-scoped roster reconciliation belongs to its existing Workspace consumer.","root_cause":"Round 4 reversed ownership but retained old sentences and one acceptance clause that still assign subscribe/list/replay to LiveDaemon.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-F10-async-codec-cancellation","causal_section_ids":["2.2"],"check_key":"tokio-codec.cancel-safe-owner","description":"Plain async codec functions cannot return FramingError::Cancelled after their future is dropped, and after partial I/O they have no surviving owner that can mark the connection retired. Acceptance 2.2.9 remains unexecutable.","finding_id":"R5-F10-codec-cancellation-boundary","fix":"At restraint rung 6, keep the plain helpers cancellation-unsafe and call them only inside the existing source-owned reader/writer tasks; on cancellation abort those tasks, reunite or drop both halves, surface FrameError::Cancelled at the source boundary, and assert no later I/O. Add no dedicated codec owner types.","introduced_in_round":4,"location":"Async codec prose and acceptance 2.2.9","participating_section_ids":["2.2","3.2"],"prevention":"For every cancellation contract, identify the object that survives future drop and owns retirement, cleanup, and the observable error.","principle":"Dropping an async future cannot make that future return an error; cancellation outcome must be owned by a surviving task or connection boundary.","root_cause":"Round 4 correctly chose connection retirement over rollback but still asks the dropped plain helper itself to return FramingError::Cancelled.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-F07-lifecycle-relay-ack","causal_section_ids":["1.4"],"check_key":"lifecycle.publisher-capacity-ownership","description":"The ordered lifecycle publisher now has a queue and completion future but no capacity, saturation rule, or admitted-entry ownership. A stalled fanout can grow memory without bound or leave committed-high-water settlement ambiguous.","finding_id":"R5-F11-lifecycle-publisher-bounds","fix":"At restraint rung 6, bound the one existing queue and backpressure producers while full; after admission the sole consumer owns the entry even if the submitter cancels, while cancellation abandons only that waiter's result. Reuse existing socket send deadlines and add one saturation/cancellation test for ordered drain and committed high-water.","introduced_in_round":4,"location":"Ordered process-wide publisher and acceptance 1.4.6","participating_section_ids":["1.4","2.1"],"prevention":"For each queue, record capacity, full behavior, cancellation before/after admission, consumer failure, and completion/high-water settlement.","principle":"Every long-lived queue needs a finite bound and an explicit ownership rule when a waiting producer is cancelled.","root_cause":"Round 4 added per-entry send completion but did not specify capacity, saturation backpressure, or who finishes an already-admitted event after submitter cancellation.","section_id":"1.4","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-F09-close-reconnect-fence","causal_section_ids":["2.1","3.3"],"check_key":"shutdown.deadline-covers-close","description":"The two-second deadline bounds release/detach, then shutdown calls Daemon::close, which may await reconnect cancellation, reader shutdown, and sink close without a bound. This contradicts 3.3.4's guarantee that an unresponsive daemon cannot delay exit past the deadline.","finding_id":"R5-F12-shutdown-deadline-scope","fix":"At restraint rung 2, apply the existing outer deadline across release, detach, waiter settlement, and Daemon::close. When it expires, abort reconnect/reader tasks and drop the sink before synchronous terminal restoration; extend the existing teardown test with each close-stage await held.","introduced_in_round":4,"location":"3.3 shutdown sequence and 3.3.4/3.3.9 versus 2.1 Daemon::close","participating_section_ids":["2.1","3.3"],"prevention":"List every shutdown await under one outer deadline and inject a permanent stall at each boundary.","principle":"A promised exit deadline must bound every await before terminal restoration and process return, including cleanup added by later fixes.","root_cause":"Round 4 made close join an in-flight reconnect, but the two-second bound still ends before close is invoked.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-F14-deferral-adoption-owner","causal_section_ids":["5.1"],"check_key":"deferral.d1-zero-one-many","description":"D2 and D3 resolve new provenance before legacy adoption, but D1 only says to create the epic. The generic convergence paragraph does not define D1 zero/one/many lookup behavior, so a coordinator rerun can create a duplicate.","finding_id":"R5-F13-d1-rerun-decision","fix":"At restraint rung 2, give D1 the same explicit exact-new-provenance decision: zero creates once with parent/category/content/criteria/provenance then adds the 5.1 blocker; one converges every postcondition; more than one stops. Use only the already named gobby-tasks operations.","introduced_in_round":4,"location":"D1 creation branch versus 5.1.4 rerun convergence","prevention":"For each migration subject, spell out exact lookup, zero, one, many, partial-write rerun, and converged rerun behavior.","principle":"An idempotent migration needs an explicit zero/one/many decision for every object it can create or adopt.","root_cause":"Round 4 named the coordinator and convergence behavior but gave only D2/D3 a provenance lookup tree; D1 still unconditionally says create.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"R4-F08-terminal-reconcile-owner","causal_section_ids":["2.1","3.2"],"check_key":"reconnect.workspace-handshake-failure-budget","description":"Generation-ready now means socket-connected and resets the retry counter before Workspace's required list/install/replay handshake. The mock injects 401/404/5xx/malformed listing failures, but no transition handles them; repeated socket success plus roster failure can reset the budget forever while panes remain read-only.","finding_id":"R5-F14-workspace-handshake-budget","fix":"At restraint rung 6, keep the existing supervisor episode and read-only state active until Workspace acknowledges reconciliation success; feed typed handshake failure into that same five-attempt budget and reset the counter only after roster install/replay succeeds. Do not add a second supervisor.","introduced_in_round":4,"location":"Generation-ready reset in 3.2.7 and Workspace re-list/replay after generation-ready","participating_section_ids":["2.1","3.2"],"prevention":"For every staged recovery, inject failure at each stage and define which stage completes, retries, or exhausts the single episode budget.","principle":"Recovery is successful only after the state owner has reconciled usable state; an intermediate transport success must not erase the retry bound for the remaining required steps.","root_cause":"Round 4 split transport readiness from roster readiness but left the supervisor reset attached to the earlier socket-ready boundary and defined no outcome for Workspace handshake failure.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"#11240","round":5,"verdict":"needs_review"},"session_id":"f372f69c-f06e-456f-8364-9422225513c4"}
```

**Human handoff at review cap** `kind: verification`

- review_cap: 5
- completed_plan_review_rounds: 5
- final_round: 5 — evidence `64e3bb34-efb2-402d-ac27-7587f2f39b66`, run
  `a20a9a53-ac3c-4c56-97dc-3d35e8d33d73`, reviewer session `#11240`, verdict
  `needs_review` with fourteen blocking findings
- disposition: all fourteen processed and voted before any edit — thirteen accepted, one
  declined (`R5-F05-zig-free-build-contract`, on the verified false premise that
  `gobby-terminal` carries a VT-engine default; its manifest declares `default = []` and
  `gobby-client` already pins `default-features = false`). Five of the thirteen were
  accepted as defects with their proposed mechanism declined as over-mechanism and
  repaired at a lower rung instead.
- state: the rejection checkpoint above was appended and finalized against the artifact
  as reviewed; the one accepted typed repair was applied by
  `apply_plan_review_repairs`; the twelve prose-only repairs were hand-applied; both
  `uv run gobby plans validate` modes exit 0 with five phases, sixteen deliverables, and
  zero errors and warnings.
- next: human review. The cap is reached, so no sixth adversary round is launched and
  none may be. The plan is deliberately **not** approved: no `apply_plan_review_manifest`,
  no `## M1 Task Manifest`, and no lesson mint. Continuation requires an explicit human
  decision — `continue interactively` re-opens revision under a fresh approved round
  budget, `hand off to build` skips remaining review, and `stop` leaves this
  base-validated artifact as the canonical one.


## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
