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
