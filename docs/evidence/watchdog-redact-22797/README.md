# Watchdog alerts carry the cited log as a redacted document (#22797)

The machine-local watchdog (`~/.gobby/watchdog/watchdog.sh`, launchd
`com.gobby.watchdog`) raises `ALARM[errors]` from new errors.log lines. Josh
decided on 2026-09-28 that such an alert attaches the log content or omits it;
the Orchestrator ruled option A. The candidate:

- Alert text: signature and counts only (`• 4x 17:21:54 <logger.function> - <message with <id>>`).
  No exception line, session ids, traceback text, or host path. Absolute and
  `~/` paths in a log message become `<path>`; `--redact` alone rewrites only
  the home directory.
  An orphan event is a bad-string line whose entry started before the window. Its
  signature is that line, cut to 200 characters. This is intentional: without it
  the alert could not name the event.
  It is still sent with `gobby comms send --redact`.
- Attachment: after the alert reaches Telegram, the exact new errors.log window
  is piped to `gobby comms attach --caption "errors.log new lines, redacted"
  gobby-telegram errors-new.txt`. The CLI scrubs secrets and home paths. When
  the redacted content is at most 64 KiB, it sends a `text/plain` document
  (Telegram `sendDocument`). Over the cap it sends one line instead:
  `Log attachment omitted: over the 64 KiB cap.` This is fixed text, so a
  caller's filename never reaches the channel.
- When the Telegram send fails, the macOS notification fallback runs and
  nothing is attached.

| File | Content |
| --- | --- |
| `watchdog.sh` | The candidate script; `diff -u ~/.gobby/watchdog/watchdog.sh watchdog.sh` shows the change |
| `test_watchdog.sh` | Fixture tests; usage `bash test_watchdog.sh <path-to-watchdog.sh>` |

## Delivery surface

`POST /api/comms/attachment` takes `{channel_name, filename, content, caption}`.

- The content is caller-supplied text. The daemon reads no caller path.
- The filename must be a bare `.txt` or `.log` name (`[A-Za-z0-9_-][A-Za-z0-9._-]{0,59}\.(txt|log)`),
  so the document can only arrive as text.
- The content must be at most 64 KiB in UTF-8 bytes; over that, the route returns 413.
- The content type is forced to `text/plain`, so a caller cannot reach `sendPhoto` or `sendVoice`.
- The daemon writes the content to a private temporary directory, calls
  `CommunicationsManager.send_attachment`, and removes the directory.
- Errors follow `/api/comms/send`: 404 for an unknown channel, 400 for invalid
  input, and 502 for a failed delivery.

The existing MCP `send_attachment` was not reused. It accepts only a file inside
the checkout or a registered worktree. To use it, the launchd job would have to
write log content into the source tree, run from the checkout so it resolves a
project, clean the file up afterwards, and still get redaction from somewhere.
That is more mechanism, and it puts log content inside the repository. The route
adds no reach: a caller could already send the same text through
`/api/comms/send`.

## Producer inventory

The task scope named session notifications, monitor alarms, and cron failure
alerts. On 0.5.0 after ba7caac10a (#22855), these are the Telegram producers:

| Producer | Cites a log path | Treatment |
| --- | --- | --- |
| Watchdog monitor alarm (`ALARM[errors]`) | Yes | Signature and counts in the text; the redacted window attached, or omitted over 64 KiB (this change) |
| Watchdog `ALARM[load]`, `[runs]`, `[cargo]`, `[vector]`, `[db]` | No | Counts only; nothing attached |
| Assistant session messages | Sometimes | `.gobby/roles/assistant.md` requires quoting the cited lines, redacted and short |
| Session lifecycle notifications | Removed | ba7caac10a deleted `session_notifications.py`, `session_events.py`, and the router |
| Cron failure alerts | Removed | ba7caac10a deleted the cron-to-comms bridge (`tests/test_runner_cron_communications.py`); `src/gobby/scheduler` has no comms send |
| In-daemon replies (`responder.py`, `telegram_actions.py`, `telegram_fallback.py`) | No | Fixed text or agent replies; no log path |

The only built-in producer that cites a log is the watchdog. Every other
message is authored by an agent, and the Assistant role covers those.

## Hashes

- Installed now (the `--redact` swap from the earlier round of this task): `7de4c8017dca62fd14a43d0340fc4416915fb5640e1b0b69e3763ff2e25f4257`
- Candidate: `8fcb532d27c9c26ea5e9e65f4eeb5fe2fe86cdc05b438271399b29c444a0f4a2`

## Activation order

The `gobby` CLI is an editable install of the main checkout. Swap the watchdog
only after `gobby comms attach` has landed there and the daemon has restarted
with `/api/comms/attachment`. Before that, the attach step fails, the failure
is logged to `watchdog.log`, and the text alert is unaffected. The swap follows
the #22977 procedure:

1. Verify the installed hash.
2. Keep a rollback copy.
3. Stage this `watchdog.sh` with mode 700.
4. Move it into place atomically.
5. Verify the hash and run `bash -n`.

## Test isolation

The fixture tests run the script under a temporary HOME, using the isolated test
hub as its read-only database. A fake `~/.local/bin/gobby` records the alert
and the attach call's arguments and stdin; nothing is sent. A fake `osascript`
comes first on PATH, so the failed-send fixture raises no real notification.
The candidate passes 25 of 25 checks. The installed script fails the
new-contract checks: the exception line, the session ids, the two attachment
checks, and the two host-path checks.

The Python tests cover redaction and the cap:

- `tests/communications/test_redaction.py`: the 64 KiB byte cap, UTF-8 counting, redaction without truncation.
- `tests/communications/test_communications_cli.py`: `comms attach` redaction, the over-cap omission note, failure exit.
- `tests/servers/routes/test_servers_routes_communications.py`: the temporary document and `text/plain`, bare filenames, 413, 404, 502.

## Live proof

This is pending: it needs a natural `ALARM[errors]` after activation that shows
the document, or the omission line, on Telegram. The earlier inline receipt
(comms_messages `dedc3eb8`) predates this decision and does not count.
