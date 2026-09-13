# Admin

Load for installation, daemon lifecycle, health, authentication, secrets,
backups, recovery, portability, or client access. `$gobby admin references`
lists operation topics; help and menus never install, restart, rotate, or restore.

There is no dedicated admin MCP service. Use domain MCP tools for agent work:
config for runtime settings, observability for reports, and tasks for task
lifecycle. Fetch the applicable tool schema before its first call. The machine
and hub operations below are operator procedures; documenting them does not
authorize executing them against the user's running installation.

1. Load `installation.md` for setup, repair, component installation, or removal.
2. Load `daemon.md` before changing daemon, service, or datastore lifecycle.
3. Load `health.md` to diagnose readiness, authentication, or degraded services.
4. Load `authentication.md` for browser credentials and daemon client tokens.
5. Load `secrets.md` for credential scope, storage, deletion, or key rewrapping.
6. Load `backups.md` for restore-verified hub snapshots and retention decisions.
7. Load `recovery.md` for failed startup, schema maintenance, and restore.
8. Load `portability.md` for pack/unpack and hub/node file ownership.
9. Load `clients.md` for browser, native terminal, and provider client access.

Identify the checkout, machine, Gobby home, hub, and operation before acting.
Preserve active sessions and their changes. Coordinate disruptive operations
through `gobby-agents:send_message`, and use isolated fixtures or temporary
daemon state for mutating verification. Installed database rows determine active
rules, profiles, workflows, and configuration; bundled files are templates.

See [admin operations](../../../../../../../../docs/guides/admin-operations.md).
