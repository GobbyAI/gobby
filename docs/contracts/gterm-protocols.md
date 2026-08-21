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

- `Hello { version, encoding, local_token, cols, rows }`
- `AttachTerminal { host_terminal_id, reservation_id? }`
- `SetViewport { rows, cols }` — attachment-local render size, never `TIOCSWINSZ`
- `SetScrollOffset { rows_from_live_edge }` — attachment-local scroll, never PTY input
- `Detach`

Host → client:

- `Welcome { host_epoch }`
- `Frame(FrameData)` / `Terminal(TerminalFrame)` / `Graphics` / `AttachHistory`
- `ScrollOffsetApplied { applied_rows, max_rows }`
- `TerminalExited` / `Error`

`reservation_id` is required only for a daemon internal observer bind. User
attaches omit it. Legacy herdr `Input` / `Resize` tags are rejected as
`unknown_message` and never mutate a terminal.

Wrong protocol version or `local_token` is a typed error before any attach.

## Control protocol

After `hello { protocol_version, control_token }`, the daemon may call `ping`,
`list`, `host_shutdown`, `reserve_observer`, `release_observer`, `spawn` →
`spawn_prepared` / `spawn_commit`, `kill`, `resize`, `snapshot`, `write`
(`encoding: "utf8-b64"`), and `subscribe_events`.

`write` / `kill` / `resize` / `spawn` carry a per-connection monotonic
`operation_seq`. A gap is `operation_gap`; an evicted seq is
`operation_expired`; a fingerprint mismatch is `operation_conflict`. Across a
reconnect the ledger is new: `spawn` reconciles, `kill`/`resize` may retry,
`write` is indeterminate and must not be blind-retried.

## Backpressure

Each attachment has a droppable 64-entry / 2 MiB delta queue (overflow resyncs
with a keyframe) and a 16-entry / 64 KiB control queue. Control overflow or a
2s delivery deadline closes the attachment. Delta lag timeout is 5s. A blocked
peer may miss the typed error and still sees EOF. Frame and control lines are
capped at `MAX_FRAME_SIZE` (2 MiB). Raw `write`/`paste` is 1 MiB UTF-8.

## Versioning

`PROTOCOL_VERSION` is 1. A mismatch is a typed refusal; there is no silent
fallback. Corpus regeneration: encode each listed message with the current
encoder, write `tests/fixtures/wire_golden/*`, and keep the round-trip test
green in the same commit.
