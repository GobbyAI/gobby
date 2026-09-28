# Watchdog alarm evidence (#22977)

Evidence for the machine-local load watchdog change that puts errors.log alarm
evidence inside the Telegram alert. The watchdog is not repository source: it
lives only at `~/.gobby/watchdog/watchdog.sh`, run every 5 minutes by launchd
(`com.gobby.watchdog`). Its bundled copy was removed in #11092.

| File | Content |
| --- | --- |
| `watchdog.diff` | Unified diff from the installed script to the candidate |
| `test_watchdog.sh` | Fixture tests; usage `bash test_watchdog.sh <path-to-watchdog.sh>` |
| `sample_alert.txt` | Candidate evidence for the real 2026-09-27 17:21 errors.log window |

## Hashes

- Installed before the change: `78cb0a4ab512b702ab84e1093301e3b622bb61e64ec20d354ecf1920d9162b3f`
- Candidate: `5ea725b64e39997567457cad6748337c5e9d1fae084a8c9746c0e2815cc41e39`
- Rollback copy after install: `~/.gobby/watchdog/watchdog.sh.pre-22977`

## Behavior

The alert now carries:

- The physical line delta: `errors.log: X lines (+L)`.
- `ALARM[errors]: N alarm events, M signatures, in +L new errors.log lines`.
  An event is one log entry. It counts when it has a traceback (excluding the
  benign `search_tool_result` case) or names a known-bad string. That rule is
  unchanged from the previous version.
- The top three signatures by count. Each shows its time range, final
  exception line, and up to three distinct session ids.

The `~/.gobby/watchdog/last.txt` path is no longer in the alert. Thresholds,
cadence, cooldown, destination, osascript fallback, and the last.txt, log, and
state writes are unchanged.

## Test isolation

The tests run the script under a temporary HOME. A fake `~/.local/bin/gobby`
records the alert instead of sending it. The database is the isolated test hub,
used read-only. The candidate passes 16 of 16 checks; the pre-change script
fails 9 of 16.
