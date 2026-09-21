# Daemon lifecycle

Load before operator start, stop, restart, service-manager changes, or datastore
lifecycle. Discover options with `uv run gobby start --help`, `stop --help`,
`restart --help`, and `service --help`. Begin with `gobby status` and identify
the serving checkout, Gobby home, local/remote mode, and active work.

1. Announce a restart or cutover with a `global` `gobby-agents:send_message`
   (every live session on this machine, across projects) and wait for a quiet
   window with no live spawned worker or close validator before disrupting
   active sessions. Inspect pending handoffs and protected cron runs.
2. Start from the main checkout. Startup checks native schema identity and
   dependencies, acquires daemon singleton ownership or a service reservation,
   and starts local managed services. Remote mode skips their local lifecycle.
3. Startup uses an installed OS service manager when available, otherwise the
   direct runner. Check health and completed startup readiness; an accepted
   service request alone does not prove that the daemon became ready.
4. Stop or restart through the CLI. Maintenance ownership refuses daemon stop.
   Protected cron runs and pending handoffs can also refuse shutdown. `--wait`
   defers eligible protected work; `--force` and `--wait` are mutually exclusive.
   `--force` interrupts protected cron runs and bypasses unresolved handoffs, but
   does not bypass other shutdown gates.
5. Verify readiness and affected client reconnection after restarting. A schema
   mismatch can reject restart before stopping the current daemon; repair the
   coherent native installation rather than repeatedly restarting it.

Native gterm terminals survive ordinary daemon stop/restart and are adopted on
startup. `--terminals` explicitly drains them. `--docker` controls local
datastore shutdown; remote nodes do not stop the hub's containers. Never infer
that the user's terminal or datastore contents may be destroyed from a request
to diagnose or restart the daemon.

Use service installation, status, enable, disable, and uninstall commands for
host service-manager configuration. Service environments differ from interactive
shells; use bootstrap, managed configuration, and supported secret handling.
For lease diagnostics, fetch current status before attempting handoff, promotion,
or recovery. An active daemon's promote endpoint is a no-op, recovery refuses an
already active owner, and cooperative handoff requires local quiescence.

See [admin operations](../../../../../../../../docs/guides/admin-operations.md)
and [lifecycle CLI](../../../../../../../../docs/guides/cli-commands.md).
