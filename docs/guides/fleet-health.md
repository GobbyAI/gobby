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

`gobby install` installs and loads a separate user service after provisioning the
native binaries and database. It uses launchd (`com.gobby.fleet-watch`, with
`KeepAlive`) on macOS and systemd (`gobby-fleet-watch.service`, with `Restart=always`)
on Linux. Windows is excluded because the native command is Unix-only.

Installation is idempotent: it replaces the same service definition and reloads the
same job. The job receives the installed native executable, Gobby home, governing
project UUID and configured daemon log directory. Installation waits up to 60 seconds
for a newly written sample less than 30 seconds old; an unchanged or missing state
file cannot satisfy readiness. A fresh sample may report an unhealthy daemon; admission
continues to fail closed until the watcher reports healthy.

Activation sequence (the operator's cutover owner performs these steps):

1. Stop any manually launched or interim watcher, then run `gobby install` from the
   governing project. Do not leave a manual sampler alongside the supervised job.
2. Confirm `sampled_at_unix` is less than 30 seconds old and exactly one
   `gdaemon fleet-watch` process runs. On macOS inspect
   `launchctl print gui/$(id -u)/com.gobby.fleet-watch`; on Linux inspect
   `systemctl --user status gobby-fleet-watch.service`. Check process command lines
   separately for a manual duplicate.
3. Perform the approved cutover. After coherent binary promotion, cutover reloads
   the installed watcher and waits for its new sample before starting the daemon.
   This makes the watcher execute the promoted inode. A normal daemon restart
   leaves the watcher running independently.

Service definitions live in `~/Library/LaunchAgents/com.gobby.fleet-watch.plist` or
`~/.config/systemd/user/gobby-fleet-watch.service`. Linux user services require a
user manager; enable lingering if the watcher must survive logout.

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

The service writes stdout and stderr to `fleet-watch.log` beside the configured
daemon log (by default `GOBBY_HOME/logs/`).
