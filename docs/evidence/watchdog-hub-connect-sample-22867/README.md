# Watchdog hub connection sample (#22867)

Evidence for the machine-local load watchdog change that records hub connections
and gcode processes on every run, so a code-index vector sync handshake timeout
can be read against them. The watchdog is not repository source: it lives only
at `~/.gobby/watchdog/watchdog.sh`, run every 5 minutes by launchd
(`com.gobby.watchdog`). The RM applies the live edit at activation.

| File | Content |
| --- | --- |
| `watchdog.diff` | Unified diff from the installed script to the candidate |
| `test_watchdog.sh` | Fixture tests; usage `bash test_watchdog.sh <path-to-watchdog.sh>` |

## Hashes

- Installed before the change: `6cace777c5d74263c3f91aa74bfa6f905f6e9250f9070c179b9d5a6d923ccd19`
- Candidate: `9a0ca490760d9dcb90474ad705749da503d216107c900f6ec4a9879c168efb18`
- Rollback copy after install: `~/.gobby/watchdog/watchdog.sh.pre-22867`

## Behavior

Every run's `last.txt` and `watchdog.log` now carry two more lines:

- `hub connections: N`, the server-wide `pg_stat_activity` count. It is
  written only when the hub answered the runs query, so an outage still shows
  `ALARM[db]` and no connection line.
- `gcode processes: N`, the `pgrep -x gcode` count. Each vector sync opens its
  own hub connection. This line does not depend on the hub.

Neither line is an alarm. The Telegram alert leaves both out, as it does the
cargo and vector counters. Thresholds, cadence, cooldown, destination,
osascript fallback, and the state file format are unchanged.

## Test isolation

The tests run the script under a temporary HOME. A fake `~/.local/bin/gobby`
records the alert instead of sending it. The database is the isolated test hub,
used read-only. The test hub has no `agent_runs` table, so the fixture rewrites
the script's `psql=` path to a wrapper. The wrapper gives the agent_runs queries
an empty table and sends every query to the real hub. The unreachable-hub case
uses a free local port. The candidate passes 11 of 11 checks; the pre-change
script fails 4 of 11.
