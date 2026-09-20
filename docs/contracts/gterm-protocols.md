# gterm Protocols

Version 1 of the Gobby terminal host protocols. Changing a wire shape
regenerates `crates/gterminal/tests/fixtures/wire_golden/` in the same change.

## Sockets

The host binds two Unix sockets in `~/.gobby` (mode `0600`):

| Socket | Speaks | Credential | Can write a PTY |
| --- | --- | --- | --- |
| `gterm-frames.sock` | length-prefixed bincode | `~/.gobby/local_cli_token` | yes, for the attachment the daemon granted |
| `gterm-control.sock` | newline-delimited JSON | `~/.gobby/gterm-control.token` | yes |

`local_token` alone still reaches nothing writable: a frame stream is read-only
until the daemon names one daemon attachment id in `grant_input` over the
control socket and the client binds that id. The credential proves who may
watch; the grant decides who may type. If the daemon's control connection
drops, frames keep arriving and standing grants keep working — only
`revoke_input` or removing the terminal clears one.

## Frame protocol

Client → host:

- `Hello { version, encoding, local_token, cols, rows, tmux_identity? }`
- `AttachTerminal { host_terminal_id, reservation_id?, locator? }`
- `SetViewport { rows, cols }` — attachment-local render size, never `TIOCSWINSZ`
- `SetScrollOffset { rows_from_live_edge }` — attachment-local scroll, never PTY input
- `BindAttachment { attachment_id }` — the daemon attachment id this stream
  types as
- `Input { data }` — bytes for the PTY, granted attachments only
- `Paste { text }` — bracketed by the host, granted attachments only
- `Detach`

Host → client:

- `Welcome { host_epoch }`
- `Attached { created, host_terminal_id }`
- `Frame(FrameData)` / `Terminal(TerminalFrame)` / `Graphics` / `AttachHistory`
- `ScrollOffsetApplied { applied_rows, max_rows }`
- `InputRefused { code }`
- `TerminalExited` / `Error`

`reservation_id` is required only for a daemon internal observer bind. User
and tmux attaches omit it and never present a reservation. The daemon opens one
frames stream per observer bind and drains it: a later `AttachTerminal` on the
same stream replaces that stream's attachment, and the host closes the stream
when its terminal is removed or the reader lags out. Tmux attaches carry
the pane `locator` (socket, server pid, start time, pane id). `Hello.tmux_identity`
is the client's own pane, used to refuse recursive self-view. `FrameData.modes`
carries cursor, mouse, keypad, copy-mode, and kitty keyboard protocol flags so
a mode change with no cell change still produces a frame. Typed refusals include
`self_view`, `capacity`,
`copy_mode`, and `stale`. Legacy herdr `Input` / `Resize` tags are rejected as
`unknown_message` and never mutate a terminal: the live input verbs are appended
after them, so a hand-built fork-point payload cannot alias one.

### Granted input

A frame stream carries granted input: `Input` and `Paste` reach the PTY only
when the stream's bound `attachment_id` equals the slot's standing grant. The
host compares the two on every message; nothing is cached per stream beyond the
bound id.

Every refusal is an `InputRefused { code }` on the same stream and never closes
it, so a refused key leaves output flowing:

| Code | Meaning |
| --- | --- |
| `attach_required` | the stream has no attachment yet |
| `input_not_granted` | the stream is unbound, or its id is not the slot's grant |
| `not_native` | the slot is a tmux pane, which types through its own renderer |
| `request_too_large` | over `MAX_WRITE_BYTES` (1 MiB) |
| `pty_busy` | the PTY write channel is full |
| `terminal_gone` | the attachment or its terminal is gone |

`BindAttachment` is refused the same way and is idempotent: it costs one message
per installed stream, and a later `AttachTerminal` on the same stream clears the
bound id, so a reattaching client rebinds. Binding does not check the grant —
only `Input` and `Paste` do — so a client may bind before the daemon grants and
learn the outcome from the first key.

An accepted `Input` or `Paste` emits `input_activity` on the control socket,
which is how the daemon keeps turn observation for keystrokes it never sees.

Wrong protocol version or `local_token` is a typed error before any attach.

## Control protocol

After `hello { protocol_version, control_token }`, the daemon may call `ping`,
`list`, `host_shutdown`, `reserve_observer`, `release_observer`, `spawn` →
`spawn_prepared` / `spawn_commit`, `kill`, `resize`, `snapshot`, `write`
(`encoding: "utf8-b64"`), `write_batch`, `grant_input`, `revoke_input`, and
`subscribe_events`.

`grant_input { host_terminal_id, attachment_id }` names the one daemon
attachment id allowed to type on a frame stream and answers
`{ok: true, granted: true, previous}`, where `previous` is the displaced id or
`null` — one holder per slot, so granting replaces. `revoke_input
{ host_terminal_id, attachment_id? }` answers `{ok: true, revoked}`; it clears
the grant when `attachment_id` matches or is omitted, and reports
`revoked: false` when nothing was cleared. Both answer `not_found` for an
unknown terminal and `not_native` for a tmux slot. Neither is ledgered: they
carry no `operation_seq`, and a control reconnect may reissue either one.

A grant lives in the host's slot, not in the connection. It survives a control
disconnect, a daemon restart, and the daemon's death; only `revoke_input` or
removing the terminal clears one. Nothing sweeps stale grants at startup and
nothing needs to: attachment ids are minted per attach and never persisted, so a
grant the previous daemon left names an id no client can bind, and the next take
of the lease replaces it.

A surviving grant does not mean typing survives a daemon outage. The lease is
daemon state, so a client that loses the daemon drops its panes to observing and
stops typing even though the host would still accept its input — it can no
longer know the lease is still its own. The grant removes the daemon from the
keystroke path, not from the decision.

The lease holder is the only thing that moves a grant. On every holder change
the daemon reconciles: it grants the new holder when the terminal is native and
that holder took direct frame delivery, and otherwise revokes, which covers a
release, a web or proxied holder, and a tmux backend.

`subscribe_events` also carries `input_activity`:

```json
{"event": "input_activity", "terminal_id": "t", "host_terminal_id": "ht-1",
 "attachment_id": "att-1", "kind": "input", "bytes": 1, "interrupt": null}
```

One event per accepted `Input` or `Paste`, with no coalescing. `kind` is
`input` or `paste`; `interrupt` is `esc` or `ctrl_c` when the whole payload is
exactly `\x1b` or `\x03`, else `null`. The daemon feeds it to the turn
observer as a delivered mediated input and lifts the automatic-write
quarantine, which is the only proof it gets that the operator typed.

`write` / `write_batch` / `kill` / `resize` / `spawn` carry a per-connection monotonic
`operation_seq`. A gap is `operation_gap`; an evicted seq is
`operation_expired`; a fingerprint mismatch is `operation_conflict`. Across a
reconnect the ledger is new: `spawn` reconciles, `kill`/`resize` may retry,
`write` is indeterminate and must not be blind-retried.

`write_batch` is the native wake-specific bounded write surface. One request has
at most 64 targets with unique `recipient_id` and `host_terminal_id` values. Each
target has 1–128 ordered `text` or `key` operations. Operation bytes use
`utf8-b64`; decoded bytes across the request are capped at 1 MiB. Each delay is
capped at 1,000 ms and cumulative delay per target at 5,000 ms. The host may
interleave different terminals by due time, but it never reorders operations
within a terminal. The response preserves target order and includes one result
per recipient: success reports `ok`/`written`; failure reports an actionable
`error` and `stage` (`none` or `partial`). A top-level validation failure writes
nothing. The request uses the existing connection-wide round-trip lock.

## Daemon WebSocket messages

Clients authenticate to the daemon's public `/ws` endpoint with
`Authorization: Bearer <local_cli_token>`. This JSON protocol coordinates
attachments and writes; it also relays frames when direct host access is
unavailable. Golden messages live in `tests/fixtures/terminal_ws_golden/`.

| Message | Direction | Fields and behavior |
| --- | --- | --- |
| `terminal_attach` | Client → daemon | `request_id`, `terminal_id`, `frame_delivery` (`proxy` by default, or `direct`), and `encoding`. Encoding defaults to `terminal_ansi`; `semantic_frame` selects semantic frames for the proxy. Other encodings receive `terminal_error` with `code: "invalid_encoding"`. |
| `terminal_attach_result` | Daemon → client | Correlates `request_id`; success carries `terminal_id`, `attachment_id`, `backend`, `rows`, `cols`, `frame_delivery`, `lease_generation`, and `direct`. Failure carries `success: false` and a typed `code`. |
| `terminal_frame` | Daemon → client | Semantic proxy envelope: `terminal_id`, `attachment_id`, `encoding: "bincode-b64"`, and `payload` containing a base64-encoded bincode host message. Decode with the host wire codec; it is not ANSI text. |
| `terminal_output` | Daemon → client | ANSI/text proxy envelope: `terminal_id`, `attachment_id`, and `data`. Browsers use the default `terminal_ansi` encoding. |
| `terminal_list` | Client ↔ daemon | A request supplies `request_id` and optional filters/cursor: `project_id`, `limit`, and `states` (a list drawn from `pending`, `live`, `exited`, `orphaned`; default `pending` + `live`; anything else is a `terminal_error` with code `invalid_states`). A response carries `items`, `next_cursor`, and `snapshot: {daemon_epoch, seq}` (nullable in the wire shape). Each item carries `state`, `ownership`, `backend`, and `updated_at`; a row the tmux sweep matched also carries `name`, `socket`, `attached_clients` (`#{session_attached}`), and the `pane_*` fields. The first page's snapshot pins the lifecycle watermark for roster reconciliation. |
| `terminal_event`, `terminal_lease_lost`, `terminal_attachment_finalized` | Daemon → client | Lifecycle messages carry `daemon_epoch` and `seq`. Apply events newer than the pinned snapshot in the same epoch; reconcile on an epoch change. |
| `terminal_set_scroll_offset` | Client → daemon | `terminal_id`, `attachment_id`, `rows_from_live_edge`, and `max_rows` — the client's own ceiling belief, where 0 means "not known yet" and the daemon applies what was asked. Native only: the daemon clamps, forwards `SetScrollOffset` to the host, and gterm re-renders frames from the offset. A tmux attachment scrolls through the mouse reports its renderer already writes to the attach client, and must not send this. Both `gclient` and the web terminal drive it: `crates/gclient/src/app/live_loop/control.rs` and `web/src/components/activity/terminal/scrollOffset.ts`. |
| `terminal_take_control`, `terminal_release_control` | Client → daemon | `terminal_id` and `attachment_id`; a take also accepts `takeover` to displace the current holder. |
| `terminal_control_result` | Daemon → client | `attachment_id`, `granted`, `reason`, `lease_generation`, and `host_input_granted`. The last is `true` when the host accepted the matching `grant_input`, `false` when it refused or could not be reached, and `null` when no grant applies — a tmux or web backend, a proxied holder, or a release. A direct native client that holds the lease without `host_input_granted: true` has nowhere to type and offers take-back rather than falling back to the daemon. |
| `terminal_scroll_offset_applied` | Daemon → client | `terminal_id`, `attachment_id`, `applied_rows`, and `max_rows`. A proxied attachment sees it twice — the daemon's own clamp against the proposed ceiling, then the host's, relayed, which owns the real scrollback depth. Clients mirror the offset optimistically and reconcile to `applied_rows`, clamping later requests to `max_rows`. |

For direct delivery, `terminal_attach_result.direct` contains
`{host_epoch, frame_socket_path, host_terminal_id, pane}`. A native terminal has
`pane: null`; a tmux terminal has
`pane: {socket_path, pane_id, server_pid, server_start_time}`. The client connects
to `frame_socket_path`, performs the host handshake, and verifies the host epoch
before attaching. Proxy delivery returns `direct: null`.

A direct locator still grants no write authority by itself. What changes with a
lease is narrow: taking the lease on a direct native attachment makes the daemon
call `grant_input`, and from then until the holder changes that client types on
its own frame stream with no daemon round trip per key. Everything else stays
mediated — PTY resize, tmux panes, proxied and web attachments, and every
automatic write go through the daemon's lease checks as before. Releasing the
lease, losing it to a takeover, or detaching revokes the grant, and the host
refuses the next key with `input_not_granted`.

The `terminal_list.snapshot` watermark is the daemon's published lifecycle
position, not a terminal screen capture. The client pins page one's watermark,
collects all pages, and reconciles buffered lifecycle events against it.
`seq` is a JSON-safe integer; epoch rotation prevents sequence overflow.

## Workspace messages

The same `/ws` socket serves workspaces: a node's named tabs and split-pane
layouts. The socket is authenticated as the local user, so every op runs as the
operator actor and no message field can name another actor. A connection handles
its messages in arrival order, so ops sent on one socket apply in that order, and
a long `pane.wait_for_output` delays the messages behind it on that socket.

| Message | Direction | Fields and behavior |
| --- | --- | --- |
| `workspace_attach` | Client → daemon | `request_id`, optional `workspace` and `node` references. Without `workspace`, the node's default workspace is used and created on first use; without `node`, the local node. The reply is `workspace_snapshot`, and the socket subscribes to `workspace_event:workspace_id=<id>`. |
| `workspace_snapshot` | Client ↔ daemon | A request takes the `workspace_attach` fields without subscribing. The reply carries `workspace` (the row plus `node_ref`), every `tabs` and `panes` row, and `snapshot: {daemon_epoch, seq}`. Panes whose terminals ended are swept before the rows are read. |
| `workspace_op` | Client ↔ daemon | A request carries `request_id`, `op`, and that op's fields. The reply carries `op` and `result`, the op's return value as JSON. |
| `workspace_event` | Daemon → client | Lifecycle message with `daemon_epoch`, `seq`, `timestamp`, `kind`, `workspace_id`, `project_id` (the single project the event's tabs name, else null), and the changed `workspace`, `tabs`, and `panes` rows. Delivery follows the socket's subscriptions; attaching adds the one for that workspace. |
| `workspace_error` | Daemon → client | Correlates `request_id` and carries `code` and `reason`. |

`op` is one of `workspace.create`, `workspace.rename`, `workspace.close`,
`workspace.set_focus_hints`, `tab.create`, `tab.rename`, `tab.move`, `tab.close`,
`pane.split`, `pane.swap`, `pane.move`, `pane.resize`, `pane.rename`,
`pane.close`, `pane.send_text`, `pane.send_keys`, `pane.read`, and
`pane.wait_for_output`. Each name maps to the `WorkspaceOps` method of the same
name with `_` for the first `.`, and its fields are that method's parameters
after the actor.

| `workspace_error.code` | Meaning |
| --- | --- |
| `not_found` | The node, workspace, tab, pane, or terminal does not exist, or a pane has no terminal. |
| `invalid_ref` | A reference does not parse or names the wrong kind of object. |
| `invalid_op` | Unknown op, unknown or missing field, wrong field type, or an op the layout rejects. |
| `terminal_failed` | A pane terminal could not be spawned, read, or written, or workspace ops are not configured. |
| `busy` | A pane is still spawning its terminal, or the terminal is held by another pane. |
| `forbidden` | The actor may not act on the target. |

Pin the `snapshot` watermark from the `workspace_snapshot` reply and apply
`workspace_event` messages newer than it in the same epoch, as with
`terminal_list`. Workspace events share the lifecycle sequence with
`terminal_event`, so one ordering covers both. An event whose encoding exceeds
1 MiB arrives as `terminal_ws_fragment` frames with `event: "workspace_event"`
and the workspace id in both `terminal_id` and `attachment_id`. An event above
the 16 MiB reassembly bound arrives with `workspace: null` and empty `tabs` and
`panes`; the client requests `workspace_snapshot` again.

The same rows are exposed by the `gobby workspaces`, `gobby panes`, and
`gobby nodes` commands ([cli-commands.md](../guides/cli-commands.md#workspaces))
and by the `gobby-workspaces` MCP registry ([mcp-tools.md](../guides/mcp-tools.md));
the user-facing model, refs, multi-window focus hints, and pane environment are
in the gclient user guide's [Workspaces](../guides/gclient-user-guide.md#workspaces)
section. A pane spawn exports `GOBBY_TERMINAL_ID` and `GOBBY_PANE_REF`, which
hooks report as `gobby_terminal_id` and `gobby_pane_ref`
([Terminal Context](../guides/ghook-development-guide.md#terminal-context)).

## Backpressure

Each attachment has a droppable 64-entry / 2 MiB delta queue (overflow resyncs
with a keyframe) and a 16-entry / 64 KiB control queue. Control overflow or a
2s delivery deadline closes the attachment. Delta lag timeout is 5s. A blocked
peer may miss the typed error and still sees EOF. Frame and control lines are
capped at `MAX_FRAME_SIZE` (2 MiB). Raw `write`/`paste` and aggregate decoded
`write_batch` payloads are capped at 1 MiB, as is a granted frame `Input` or
`Paste` (`request_too_large` past it).

Granted input is never awaited. A client enqueues it on its own outbound frame
queue and keeps rendering; a full queue drops that one keystroke and is reported
to the person typing, not retried, because a retried keystroke is a wrong
keystroke. `pty_busy` is the host's half of the same rule.

## Versioning

`PROTOCOL_VERSION` is 1. A mismatch is a typed refusal; there is no silent
fallback. Corpus regeneration: encode each listed message with the current
encoder, write `tests/fixtures/wire_golden/*`, and keep the round-trip test
green in the same commit.

Granted frame input was appended under `PROTOCOL_VERSION` 1: the input verbs sit
after the existing variants, so a host and a client of different builds still
agree on every older message, and an ungranted `Input` is refused rather than
misread.

## Decision record

Granted input replaces the daemon-mediated keystroke path that memory
`be35449d` item 1 described, where gclient was a pure viewer mutating only
through the daemon. Memory `be35449d` updated to point at the superseding
decision in memory `b59e4ce9` and its plan,
`.gobby/plans/gclient-direct-input.md`. The daemon still owns leases, layout,
workspaces, and every automatic write.
