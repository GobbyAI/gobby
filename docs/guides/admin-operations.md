# Admin Operations

Admin operations cover local daemon access, authentication, secrets, service
management, config import/export, portable pack/unpack, setup state, and
diagnostics.

## Mental Model

Gobby is local-first, but the daemon still has operator responsibilities. Admin
operations change local machine state, project state, service state, or backed-up
runtime state. Treat them as operational actions, not normal code edits.

Use the CLI for machine-level operations. Use HTTP admin routes for status and UI
diagnostics. Use secrets for credentials instead of committing values into config
or docs.

The CLI examples in this guide are operator procedures. Agents use domain MCP
tools for task lifecycle and other supported domain operations. A request for
documentation or diagnostics does not authorize a live restart, secret rotation,
restore, or datastore exposure. Rehearse mutations in isolated state.

## Quick Start

Check the daemon:

```bash
uv run gobby status
```

Set local UI auth:

```bash
uv run gobby auth credentials
```

Store a secret:

```bash
uv run gobby secrets set LINEAR_API_KEY --category integration --stdin
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

Daemon API auth is required by default. `gobby install` provisions the
owner-readable `$GOBBY_HOME/local_cli_token` (default
`~/.gobby/local_cli_token`); daemon clients send it as a bearer token. Inspect
its file/hash agreement with:

```bash
uv run gobby auth token
```

Browser authentication uses the canonical user stored in PostgreSQL. Reset the
sole installed user's Argon2id password with:

```bash
uv run gobby auth credentials
```

Password reset revokes every `auth_sessions` row for that user, so existing
`gobby_session` cookies stop working. The caller must sign in again. The
browser session cookie is `gobby_session`.

HTTP auth routes:

```text
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/status
```

### Rotate The Local Token

1. Check the current state with `gobby auth token`.
2. Run `gobby auth token --rotate` on the hub machine.
3. Have clients reread the token. The daemon refreshes its credential cache on
   a request after the five-second refresh interval.
4. Copy `$GOBBY_HOME/local_cli_token` (default `~/.gobby/local_cli_token`) to
   every additional trusted client machine and set mode `0600`.
5. Re-run the verification matrix below. The old token must return `401`.

Capture and verify old-token invalidation on the hub machine:

```bash
BASE="${GOBBY_DAEMON_URL:-http://localhost:60887}"
OLD_TOKEN="$(tr -d '\r\n' < "${GOBBY_HOME:-$HOME/.gobby}/local_cli_token")"
uv run gobby auth token --rotate
sleep 6
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $OLD_TOKEN" \
  "$BASE/api/admin/status")" = 401
```

Rotation updates the file and authoritative `auth.api_token_hash`. Existing
browser sessions remain valid; the `/ws` cookie bridge uses the refreshed token
for its upstream standalone-WebSocket connection.

### Manual Verification Matrix

Run the HTTP checks from a machine holding the current token:

```bash
BASE="${GOBBY_DAEMON_URL:-http://localhost:60887}"
TOKEN="$(tr -d '\r\n' < "${GOBBY_HOME:-$HOME/.gobby}/local_cli_token")"

for path in /api/health /api/admin/startup-progress; do
  curl -fsS "$BASE$path" >/dev/null
done

test "$(curl -sS -o /dev/null -w '%{http_code}' \
  "$BASE/api/admin/status")" = 401
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  -H 'Authorization: Bearer invalid-token' \
  "$BASE/api/admin/status")" = 401
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/admin/status" >/dev/null
curl -fsS -H "X-Gobby-Local-Token: $TOKEN" \
  "$BASE/api/admin/status" >/dev/null

# FastMCP's streamable HTTP endpoint is /mcp.
curl -fsS "$BASE/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"auth-check","version":"1.0"}}}'
```

Then verify clients that acquire the token automatically:

```bash
uv run gobby tasks list --limit 1
uv run gobby mcp-proxy call-tool gobby-tasks list_tasks \
  --json-args '{"limit":1}'
```

In a disposable repository with installed Gobby hooks, create a commit and
confirm the hook completes without a `401`:

```bash
git commit --allow-empty -m "chore: verify authenticated Gobby hook"
```

Finally, open the Web UI, log in with credentials from `gobby auth credentials`,
open Chat, and confirm chat frames continue across `/ws`. This checks the browser
cookie and WebSocket bearer bridge together.

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

`secrets get NAME` checks existence; it does not reveal the value. Select scope
with `--project` or `--global`, which are mutually exclusive. Without either,
the CLI selects the registered current project when available, otherwise global
scope. An unknown explicit project fails rather than falling back to global.
The `set` command uses a hidden prompt or stripped stdin and rejects empty values.

`gobby secrets rekey --posture key-file|passphrase` rewraps the data-encryption
key without re-encrypting every secret. Preserve recovery material and arrange
supported passphrase delivery to noninteractive services before changing posture.
See the [secret contract](../contracts/secrets.md).

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

## Configuration Export And Import

The Configuration API exports and imports UI-managed configuration:

```text
POST /api/config/export
POST /api/config/import
```

## Pack And Unpack

`gobby pack` creates a portable archive of its selected state. It can include a
PostgreSQL logical dump, bootstrap config, machine ID, secret material,
transcripts, summaries, services, hooks, certs, scripts, current project
`.gobby`, hub-owned files, and Qdrant/FalkorDB Docker volumes. Its inventory is
defined in `src/gobby/cli/pack.py`; it is not a copy of every local file.

Common commands:

```bash
uv run gobby pack --dry-run
uv run gobby pack gobby-pack.tar.gz
uv run gobby pack --no-docker
uv run gobby pack --no-transcripts
uv run gobby unpack gobby-pack.tar.gz --dry-run
uv run gobby unpack gobby-pack.tar.gz
```

Packing may stop daemon or service components to create a consistent snapshot.
Coordinate that window and use `--dry-run` before a real pack on active machines.
Dry-run lists contents; it does not prove restore success.

Unpack requires the destination's existing files-home configuration and preserves
that root when merging bootstrap. It skips archived `machine_id` unless
`--restore-identity` explicitly selects same-machine disaster recovery.
`--force` suppresses overwrite confirmation; `--no-postgres` and `--no-docker`
skip those restore payloads, not every service lifecycle action. A failed
extraction can leave partial state and stopped services. Inspect the failure
before restarting or repeating the restore.

## Verified Hub Backups

Use `gobby hub-backup --output DIRECTORY --json` for a staged, restore-verified
hub backup. The output directory must be absent and outside the source files
tree. Inspect the published manifest and each store's archive/restore evidence;
an existing archive alone is insufficient. The normal command coordinates a
stopped-daemon maintenance window and restores prior daemon lifecycle.

The `--epoch` option is for a matching hub-maintenance child invocation, which
owns lifecycle and leaves the daemon stopped. Do not supply a made-up epoch.

`hub-backup restore` verifies the manifest and requires the daemon stopped plus
an explicit `--database-url` target. It restores hub files, PostgreSQL globals
and data, and reconciles principals. It does not automatically restore every
Qdrant/FalkorDB artifact. `--clean` drops database objects and `--yes` skips
confirmation. Rehearse against an isolated target and follow
[Hub Backup Disaster Recovery](cli-commands.md#hub-backup-disaster-recovery).

## Lifecycle And Recovery

Start and restart from the main checkout. Startup validates native/schema
identity, claims the singleton, starts local managed services, and waits for
health and readiness. Remote mode skips local Compose lifecycle. An accepted
OS service request alone does not establish readiness.

Stop/restart can be refused by maintenance ownership, protected cron runs, or
pending handoffs. Inspect the reported blocker. `--wait` defers eligible work;
`--force` and `--wait` are mutually exclusive and do not bypass every gate.
Native terminals survive ordinary daemon restarts; `--terminals` drains them.

For schema disagreement, use the coherent [cutover procedure](release-guide.md#local-install-check).
For interrupted destructive maintenance, inspect `gobby hub-maintenance status`
and use `resume` with the hub-recorded campaign state. `abort` records a
partial-state disposition and releases the fence; it is not a rollback.

## Setup And Diagnostics

Useful admin routes:

```text
GET  /api/health
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
uv run gobby auth ...
uv run gobby secrets ...
uv run gobby service ...
uv run gobby pack ...
uv run gobby unpack ...
uv run gobby sync ...
```

Use task lifecycle MCP tools for agent task state. Do not use admin commands as a
shortcut around task claiming, validation, commit linking, or task closure.

## HTTP

Admin HTTP routes power Web UI status, setup flows, auth checks, metrics,
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
- `src/gobby/cli/pack.py`: pack/unpack.
- `src/gobby/cli/hub_backup/`: verified hub backups and restore.
- `src/gobby/cli/hub_maintenance.py`: fenced maintenance campaigns.
- `src/gobby/servers/routes/auth.py`: auth HTTP routes.
- `src/gobby/servers/routes/admin/`: setup, health, status, metrics, usage,
  savings, and lifecycle routes.
- `src/gobby/servers/routes/configuration.py`: config export/import and prompt
  routes.
- `~/.gobby/`: global daemon state, secrets, packs, and caches.
- `.gobby/`: project-local Gobby state.

## See Also

- [configuration.md](configuration.md)
- [system-requirements.md](system-requirements.md)
- [observability.md](observability.md)
- [prompts.md](prompts.md)
- [cron-scheduler.md](cron-scheduler.md)

_Last verified: 2026-09-13_
