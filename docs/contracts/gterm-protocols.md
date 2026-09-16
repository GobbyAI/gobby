# gterm Protocols

Version 1 of the Gobby terminal host protocols. Changing a wire shape
regenerates `crates/gterminal/tests/fixtures/wire_golden/` in the same change.

## Sockets

The host binds two Unix sockets in `~/.gobby` (mode `0600`):

| Socket | Speaks | Credential | Can write a PTY |
| --- | --- | --- | --- |
| `gterm-frames.sock` | length-prefixed bincode | `~/.gobby/local_cli_token` | no |
| `gterm-control.sock` | newline-delimited JSON | `~/.gobby/gterm-control.token` | yes |

A frame client cannot reach the writing surface by reusing `local_cli_token`.
If the daemon's control connection drops, frames keep arriving and nobody can
write.

## Frame protocol (read-only)

Client → host:

- `Hello { version, encoding, local_token, cols, rows, tmux_identity? }`
- `AttachTerminal { host_terminal_id, reservation_id?, locator? }`
- `SetViewport { rows, cols }` — attachment-local render size, never `TIOCSWINSZ`
- `SetScrollOffset { rows_from_live_edge }` — attachment-local scroll, never PTY input
- `Detach`

Host → client:

- `Welcome { host_epoch }`
- `Attached { created, host_terminal_id }`
- `Frame(FrameData)` / `Terminal(TerminalFrame)` / `Graphics` / `AttachHistory`
- `ScrollOffsetApplied { applied_rows, max_rows }`
- `TerminalExited` / `Error`

`reservation_id` is required only for a daemon internal observer bind. User
and tmux attaches omit it and never present a reservation. The daemon opens one
frames stream per observer bind and drains it: a later `AttachTerminal` on the
same stream replaces that stream's attachment, and the host closes the stream
when its terminal is removed or the reader lags out. Tmux attaches carry
the pane `locator` (socket, server pid, start time, pane id). `Hello.tmux_identity`
is the client's own pane, used to refuse recursive self-view. `FrameData.modes`
carries cursor/mouse/keypad/copy-mode flags so a mode change with no cell change
still produces a frame. Typed refusals include `self_view`, `capacity`,
`copy_mode`, and `stale`. Legacy herdr `Input` / `Resize` tags are rejected as
`unknown_message` and never mutate a terminal.

Wrong protocol version or `local_token` is a typed error before any attach.

## Control protocol

After `hello { protocol_version, control_token }`, the daemon may call `ping`,
`list`, `host_shutdown`, `reserve_observer`, `release_observer`, `spawn` →
`spawn_prepared` / `spawn_commit`, `kill`, `resize`, `snapshot`, `write`
(`encoding: "utf8-b64"`), `write_batch`, and `subscribe_events`.

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

For direct delivery, `terminal_attach_result.direct` contains
`{host_epoch, frame_socket_path, host_terminal_id, pane}`. A native terminal has
`pane: null`; a tmux terminal has
`pane: {socket_path, pane_id, server_pid, server_start_time}`. The client connects
to `frame_socket_path`, performs the read-only host handshake, and verifies the
host epoch before attaching. Proxy delivery returns `direct: null`. Neither a
direct locator nor a frame grants write authority: input and PTY resize still go
through the daemon's lease checks.

The `terminal_list.snapshot` watermark is the daemon's published lifecycle
position, not a terminal screen capture. The client pins page one's watermark,
collects all pages, and reconciles buffered lifecycle events against it.
`seq` is a JSON-safe integer; epoch rotation prevents sequence overflow.

## Backpressure

Each attachment has a droppable 64-entry / 2 MiB delta queue (overflow resyncs
with a keyframe) and a 16-entry / 64 KiB control queue. Control overflow or a
2s delivery deadline closes the attachment. Delta lag timeout is 5s. A blocked
peer may miss the typed error and still sees EOF. Frame and control lines are
capped at `MAX_FRAME_SIZE` (2 MiB). Raw `write`/`paste` and aggregate decoded
`write_batch` payloads are capped at 1 MiB.

## Versioning

`PROTOCOL_VERSION` is 1. A mismatch is a typed refusal; there is no silent
fallback. Corpus regeneration: encode each listed message with the current
encoder, write `tests/fixtures/wire_golden/*`, and keep the round-trip test
green in the same commit.
