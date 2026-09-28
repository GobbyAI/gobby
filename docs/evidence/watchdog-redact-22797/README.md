# Watchdog alerts through `gobby comms send --redact` (#22797)

The machine-local watchdog (`~/.gobby/watchdog/watchdog.sh`, launchd
`com.gobby.watchdog`) quotes errors.log lines in its Telegram alert since #22977.
This change sends that alert with `gobby comms send --redact`. Secrets, URL
credentials, and the home-directory path are scrubbed before the text leaves the
machine, and the message is cut to Telegram's 4,096-character limit.

| File | Content |
| --- | --- |
| `watchdog.diff` | One-line diff from the installed script to the candidate |
| `test_watchdog.sh` | #22977 fixture tests plus a check that the alert passes `--redact gobby-telegram`; usage `bash test_watchdog.sh <path-to-watchdog.sh>` |

## Producer inventory

The task scope named session notifications, monitor alarms, and cron failure
alerts. On 0.5.0 after ba7caac10a (#22855), these are the Telegram producers:

| Producer | Cites a log path | Treatment |
| --- | --- | --- |
| Watchdog monitor alarm (`ALARM[errors]`, `ALARM[load]`) | Yes | Quotes the new errors.log lines and sends through `--redact` (this change) |
| Assistant session messages | Sometimes | `.gobby/roles/assistant.md` requires quoting the cited lines, redacted and short |
| Session lifecycle notifications | Removed | ba7caac10a deleted `session_notifications.py`, `session_events.py`, and the router |
| Cron failure alerts | Removed | ba7caac10a deleted the cron-to-comms bridge (`tests/test_runner_cron_communications.py`); `src/gobby/scheduler` has no comms send |
| In-daemon replies (`responder.py`, `telegram_actions.py`, `telegram_fallback.py`) | No | Fixed text or agent replies; no log path |

The only built-in producer that cites a log is the watchdog. Every other
message is authored by an agent, and the Assistant role covers those.

## Live proof

comms_messages `dedc3eb8`, 2026-09-28T18:57:29Z, status `sent`, 543 characters.
It is a natural watchdog alarm and quotes `errors.log: 10151 lines (+650)` with
the alarm signature inline. No secrets.

## Hashes

- Installed before the change (#22977): `5ea725b64e39997567457cad6748337c5e9d1fae084a8c9746c0e2815cc41e39`
- Candidate: `7de4c8017dca62fd14a43d0340fc4416915fb5640e1b0b69e3763ff2e25f4257`

## Activation order

The `gobby` CLI is an editable install of the main checkout. The watchdog must
be swapped only after the `--redact` option has landed there. Before that, an
unknown option makes the Telegram send fail, and the watchdog falls back to a
macOS notification. The swap follows the #22977 procedure: verify the installed
hash, keep a rollback copy, stage the candidate with mode 700, move it into
place atomically, then verify the hash and run `bash -n`.

## Test isolation

The fixture tests run the script under a temporary HOME. A fake
`~/.local/bin/gobby` records its arguments and the alert instead of sending
anything. Redaction itself is covered by the Python tests in
`tests/communications/test_redaction.py`, `tests/communications/test_communications_cli.py`,
and `tests/utils/test_terminal_output.py`. The candidate passes 17 of 17 checks.
The installed script fails only the `--redact` check.
