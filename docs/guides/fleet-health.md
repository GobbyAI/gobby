# Fleet health watcher

`gdaemon fleet-watch` samples the daemon and the host every 30 seconds and writes one
state file. Spawns and pipeline launches read that file and refuse with
`fleet_unhealthy` unless the fleet is healthy. The watcher is its own OS process. It
never runs inside the Python daemon, and it keeps sampling while the daemon restarts.

## What it writes

`GOBBY_HOME/state/fleet-health.json` (default `~/.gobby/state/fleet-health.json`) holds
the state, the sample time, the reasons, the ramp in force and the threshold source.
Each write goes to a sibling temp file, is synced, then renamed over the old file.

- `state`: `healthy`, `degraded` or `unstable`.
- `sampled_at_unix`: when the sample was taken. A file older than 90 seconds counts as
  `unstable`.
- `reasons`: the signals that set the state.
- `ramp`: `max_new_seats` and `ramp_window_seconds`, copied from `fleet_health`.
- `thresholds_source`: `project_policy`, `last_good` or `defaults`.

Thresholds come from the installed `project_policy` rule row, `fleet_health` block. The
watcher reads it directly from PostgreSQL. A failed read keeps the last good value, and
the first failed read falls back to the compiled defaults.

Admission also keeps `GOBBY_HOME/state/fleet-admissions.json`, the ledger of seats
admitted in the current ramp window.

## Supervision

The watcher runs under a launchd LaunchAgent, `com.gobby.fleet-watch`. The agent is
set up by hand. `gobby install` does not create it.

Create `~/Library/LaunchAgents/com.gobby.fleet-watch.plist`, replacing `USER`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.gobby.fleet-watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USER/.gobby/bin/gdaemon</string>
    <string>fleet-watch</string>
    <string>--project-id</string>
    <string>PROJECT_UUID</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/USER/.gobby/logs/fleet-watch.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USER/.gobby/logs/fleet-watch.log</string>
</dict>
</plist>
```

Start it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gobby.fleet-watch.plist
```

Check it: `launchctl print gui/$(id -u)/com.gobby.fleet-watch` shows it running, and the
`sampled_at_unix` field in the state file advances about every 30 seconds.

Stop it:

```bash
launchctl bootout gui/$(id -u)/com.gobby.fleet-watch
```

`KeepAlive` restarts the watcher if it exits. Its binary is `~/.gobby/bin/gdaemon`.
Promotion replaces that binary, but a running watcher keeps the old image until it
restarts. After promoting a new `gdaemon`, restart the watcher with `bootout`, then
`bootstrap`. The daemon's own restarts do not require this.

## Single sample

`gdaemon fleet-watch --once` takes one sample, writes the state file and exits. Use it
to check the probe and the thresholds without starting the agent. An error (for example
an unreadable log) exits non-zero.

## Options

- `--project-id <uuid>`: the project whose `project_policy` rows are read, in addition to
  the global rows. Without it only global rows are read, so pass the governing project's
  UUID in the LaunchAgent. A value that is not a UUID is refused.
- `--logs-dir <dir>`: the directory holding `daemon.log`. Resolution order is this flag,
  then `GOBBY_LOGGING_DIR`, then `GOBBY_HOME/logs`. A blank value counts as unset, and a
  leading `~` expands to the user home. The daemon's `logging.dir` in its config is not
  read, so an operator with a custom log directory passes this flag.
- `GOBBY_HOME`: the state and bootstrap home. Blank means the default. `~` expands.
- `GOBBY_DATABASE_URL`: overrides the database URL that the bootstrap file supplies for
  the `project_policy` read.

The watcher reads `bootstrap.yaml` under the home for the daemon's port and bind host.
It dials the bind host, and a wildcard or empty bind host is dialled on `127.0.0.1`.
Without a database URL the thresholds fall back to the compiled defaults, and the state
file says so in `thresholds_source`.

## Failures in the loop

A sample that fails (for example an unreadable log) is reported on stderr and skipped.
Nothing is written, so the state file ages past 90 seconds and admission fails closed. The
next tick tries again, and the watcher does not exit on one bad sample.

A watcher restart starts with an empty log window. Log lines written before the watcher
started are not read back, so log-derived signals rebuild over the next five minutes.

## Log

The watcher writes to `GOBBY_HOME/logs/fleet-watch.log` through launchd, next to the
daemon logs in `GOBBY_HOME/logs/`.
