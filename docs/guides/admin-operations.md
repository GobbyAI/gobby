# Admin Operations

Admin operations cover local daemon access, authentication, secrets, service
management, config import/export, full-state pack/unpack, setup state, and
diagnostics.

## Mental Model

Gobby is local-first, but the daemon still has operator responsibilities. Admin
operations change local machine state, project state, service state, or backed-up
runtime state. Treat them as operational actions, not normal code edits.

Use the CLI for machine-level operations. Use HTTP admin routes for status and UI
diagnostics. Use secrets for credentials instead of committing values into config
or docs.

## Quick Start

Check the daemon:

```bash
uv run gobby status
```

Set local UI auth:

```bash
uv run gobby auth
```

Store a secret:

```bash
uv run gobby secrets set LINEAR_API_KEY --category integrations --stdin
```

Check service state:

```bash
uv run gobby service status
```

Dry-run a backup pack:

```bash
uv run gobby pack --dry-run
```

## Authentication

`gobby auth` configures local web authentication. It stores the username in
config and the password through the config secret store. Removing auth uses:

```bash
uv run gobby auth --remove
```

Restart the daemon after changing auth settings.

HTTP auth routes:

```text
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/status
```

The browser session cookie is `gobby_session`.

## Secrets

Use `gobby secrets` for local credentials:

```bash
uv run gobby secrets set NAME
uv run gobby secrets set NAME --stdin
uv run gobby secrets list
uv run gobby secrets get NAME
uv run gobby secrets delete NAME
```

Secret values are not printed by normal list output. Reference stored secrets as:

```text
$secret:NAME
```

Prefer secrets for API keys used by integrations, services, and workflows.

## Service Helpers

The service CLI installs and manages the daemon under the host service manager:

```bash
uv run gobby service install
uv run gobby service status
uv run gobby service enable
uv run gobby service disable
uv run gobby service uninstall
```

On macOS this uses launchd. On Linux it uses systemd. Service environments do not
inherit all shell variables, so put required values in `~/.gobby/bootstrap.yaml`,
Gobby secrets, or managed config instead of relying on an interactive shell.

## Export And Import

Resource import/export moves workflow, agent, and prompt files between projects
or the global Gobby directory:

```bash
uv run gobby export all --to /path/to/other/project
uv run gobby export prompt --global
uv run gobby import all --from-project /path/to/other/project
```

The Configuration API can also export and import UI-managed configuration:

```text
GET  /api/config/export
POST /api/config/import
```

## Pack And Unpack

`gobby pack` creates a broader snapshot than resource export. It can include the
database, bootstrap config, machine ID, secret salt, transcripts, summaries,
services, hooks, certs, canvas files, scripts, current project `.gobby`, and
Docker volumes such as Qdrant and Neo4j data.

Common commands:

```bash
uv run gobby pack --dry-run
uv run gobby pack --output gobby-pack.tar.gz
uv run gobby pack --no-docker
uv run gobby pack --no-transcripts
uv run gobby unpack gobby-pack.tar.gz
uv run gobby unpack gobby-pack.tar.gz --force
```

Packing may stop daemon or service components to create a consistent snapshot.
Use `--dry-run` before a real pack on active machines.

## Setup And Diagnostics

Useful admin routes:

```text
GET  /api/admin/setup-state
POST /api/admin/setup-state
GET  /api/admin/health
GET  /api/admin/startup-progress
GET  /api/admin/status
GET  /api/admin/metrics
```

`/api/admin/status` is the most complete runtime diagnostic endpoint. It reports
process state, background tasks, MCP servers, sessions, tasks, memory, skills,
pipelines, provider models, savings, agents, file descriptor state, database
state, and last shutdown information.

## CLI

Admin command families:

```bash
uv run gobby status
uv run gobby auth
uv run gobby secrets ...
uv run gobby service ...
uv run gobby export ...
uv run gobby import ...
uv run gobby pack ...
uv run gobby unpack ...
uv run gobby sync ...
```

Use task lifecycle MCP tools for agent task state. Do not use admin commands as a
shortcut around task claiming, validation, commit linking, or task closure.

## HTTP

Admin HTTP routes power the Web UI dashboard, setup flows, auth checks, metrics,
and config screens. Use HTTP for read-oriented diagnostics and UI workflows. Use
the CLI for machine-level service and backup operations.

## MCP

There is no single admin MCP server. Administrative state is split across
domain-specific servers:

- `gobby-config` for configuration-oriented state.
- `gobby-metrics` for metrics and usage reports.
- `gobby-tasks` for task lifecycle.
- `gobby-cron` for scheduled operations.
- `gobby-memory` for persistent knowledge.

Use progressive discovery before calling any server.

## File Locations

- `src/gobby/cli/auth.py`: auth CLI.
- `src/gobby/cli/secrets.py`: secrets CLI.
- `src/gobby/cli/service.py`: service manager CLI.
- `src/gobby/cli/export_import.py`: resource export/import.
- `src/gobby/cli/pack.py`: pack/unpack.
- `src/gobby/servers/routes/auth.py`: auth HTTP routes.
- `src/gobby/servers/routes/admin/`: setup, health, status, metrics, usage,
  savings, and lifecycle routes.
- `src/gobby/servers/routes/configuration.py`: config export/import and prompt
  routes.
- `~/.gobby/`: global daemon state, secrets, packs, canvas files, and caches.
- `.gobby/`: project-local Gobby state.

## See Also

- [configuration.md](configuration.md)
- [system-requirements.md](system-requirements.md)
- [observability.md](observability.md)
- [prompts.md](prompts.md)
- [cron-scheduler.md](cron-scheduler.md)

_Last verified: 2026-05-08_
