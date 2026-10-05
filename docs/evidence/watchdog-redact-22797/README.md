# Watchdog alerts carry the cited log as a redacted document (#22797)

The machine-local watchdog (`~/.gobby/watchdog/watchdog.sh`, launchd
`com.gobby.watchdog`) raises `ALARM[errors]` from new errors.log lines. Josh
decided on 2026-09-28 that such an alert attaches the log content or omits it;
the Orchestrator ruled option A. The candidate:

- Alert text: signature and counts only (`• 4x 17:21:54 <logger.function> - <message with <id>>`).
  No exception line, session ids, traceback text, or host path. Absolute and
  `~/` paths in a log message become `<path>`. That covers a path after a space,
  a quote, a bracket, `=`, `:` or `,`, including `cwd:/x` and `file:///x`.
  `--redact` alone rewrites only the home directory.
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
| Assistant session messages | Sometimes | `.gobby/roles/assistant.md` requires sending the cited lines through `gobby comms attach`: a redacted document, or the omission note over 64 KiB. The message text carries no log lines or path |
| Session lifecycle notifications | Removed | ba7caac10a deleted `session_notifications.py`, `session_events.py`, and the router |
| Cron failure alerts | Removed | ba7caac10a deleted the cron-to-comms bridge (`tests/test_runner_cron_communications.py`); `src/gobby/scheduler` has no comms send |
| In-daemon replies (`responder.py`, `telegram_actions.py`, `telegram_fallback.py`) | No | Fixed text or agent replies; no log path |

The only built-in producer that cites a log is the watchdog. Every other
message is authored by an agent. The Assistant role routes those through the
same `gobby comms attach` surface.

## Hashes

- Installed now (the `--redact` swap from the earlier round of this task): `7de4c8017dca62fd14a43d0340fc4416915fb5640e1b0b69e3763ff2e25f4257`
- Candidate: `6cace777c5d74263c3f91aa74bfa6f905f6e9250f9070c179b9d5a6d923ccd19`

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

Observed on 2026-09-29:

- After the restart that activated the route (daemon PID 99308), a route check
  sent nothing: `gobby comms attach gobby-telegram x.sh < /dev/null` returned 400
  `filename must be a bare .txt or .log name`.
- The watchdog was then swapped by the #22977 procedure. `shasum -a 256` read
  `7de4c801` on the installed file before the swap. After it, the installed file
  read `6cace777` and `watchdog.sh.rollback-22797` read `7de4c801`.
- A later daemon-only restart for #23063 left the route in place and did not
  touch the watchdog. The alarm below ran under daemon PID 93307.
- At 01:59:40 CT a natural `ALARM[errors]` (+97 lines) sent alert comms_messages
  `6c390203`. Its text is the counts plus
  `• 1x 01:54:42 communications.polling._poll_loop - Error polling channel 'gobby-telegram': ReadTimeout (backing off 5s)`,
  with no traceback, path, or id.
- It then sent a redacted document, comms_messages `5127611f`, caption
  `errors.log new lines, redacted`. The window was under 64 KiB, so no omission
  note was needed.
- `watchdog.log` shows `Message sent to gobby-telegram` followed by
  `errors-new.txt attached to gobby-telegram`.

The earlier inline receipt (comms_messages `dedc3eb8`) predates this decision and
is superseded.

## Five-minute load alarm (#23532, 2026-10-05)

The load alarm now uses the five-minute average and a strict `> 30` comparison.
It reports `ALARM[load]: 5-min load <reading> > 30`. This alarm uses one reading;
the separate heavy-work hold requires two consecutive five-minute breaches.

The fixture's fake `sysctl` supplies deterministic load readings. Added checks
cover five-minute load 30.01 (alarm), exactly 30 (no alarm), and one-minute load
40 with five-minute load 29 (no alarm). The original 25 alert, attachment,
redaction, quiet-run and fallback checks remain included.

TDD command, before and after the script change:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 bash docs/evidence/watchdog-redact-22797/test_watchdog.sh docs/evidence/watchdog-redact-22797/watchdog.sh
```

RED: 26 checks passed; the five-minute breach and one-minute-spike checks failed.
GREEN: all 28 passed. ShellCheck and `bash -n` passed for both shell files.
The test-quality auditor cannot analyze Bash (`NO_ANALYZABLE_FILES`); validation
uses the native fixture runner. No Python or Rust tests or implementation changed.

The installed script was replaced using the activation procedure above:

- Pre-swap SHA-256: `6cace777c5d74263c3f91aa74bfa6f905f6e9250f9070c179b9d5a6d923ccd19`.
- Rollback: `~/.gobby/watchdog/watchdog.sh.pre-23532`, with that same hash.
- Staged with `install -m 700`, checked with `bash -n` and `cmp`, then atomically
  renamed over the installed script in the same directory.
- Post-swap installed and repo SHA-256:
  `b621ca6447b6c9cbd3cfc5f507706fa6bbb50faeb2d62bbbb6a2d54836507e3f`.
- Installed mode: 700; post-swap `cmp` and `bash -n` passed.

No daemon restart was needed. No real credential file was read or printed;
fixtures use their own temporary configuration and fake communications CLI.
