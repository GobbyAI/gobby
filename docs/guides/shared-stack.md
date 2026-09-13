# Shared Datastores Across Machines

Gobby supports a `datastore_mode: remote` topology in which one hub machine owns
PostgreSQL, Qdrant, and FalkorDB and every machine that uses them installs a Gobby
daemon. The datastores are shared over a private Tailscale network; execution stays
local to each machine.

**Exactly one daemon is active per shared hub.** The singleton lease is scoped to the
shared database, so a second daemon pointed at the same datastores starts as a standby
and exposes only the standby health and lease-control surface until it is promoted.
Operators inspect ownership with `gobby lease status` and use `gobby lease acquire`
or `gobby lease release` for explicit transitions. This is the current implementation;
independent simultaneously active hub/node execution remains roadmap work.

```text
workstation daemon --+
laptop daemon -------+-- Tailscale ACL -- datastore hub
                                         |-- PostgreSQL: 60891
                                         |-- Qdrant:     6333
                                         `-- FalkorDB:   16379
```

Treat the three datastore ports as private infrastructure. Allow them only from the
specific users, devices, or tags that run Gobby clients to the hub device or tag. Deny
all other tailnet sources and all public ingress. Qdrant API-key support is deferred,
so the Tailscale ACL is mandatory for Qdrant in M0.

## Hub setup

These are operator procedures. Install the same Gobby version that the clients will
run. The hub uses local datastore mode and needs an already provisioned absolute
`files_home` in bootstrap, for example `/var/lib/gobby/files`. Do not use a literal
tilde or `/`, and do not set `hub_daemon_url` on the local owner. Preserve an existing
files tree; see [the install contract](hub-install-contract.md#files-and-client-credentials).

```bash
gobby install
gobby datastores expose --bind <tailscale-ipv4> --host <hub-dns-name>
gobby start
```

`--bind` must be the hub's Tailscale IPv4 address. `--host` is the DNS name or IP that
clients use to reach the hub. The expose command stages the bind, starts and checks all
three services, publishes the Qdrant and FalkorDB client endpoints, and rolls back the
previous bind if readiness or endpoint publication fails.

Configure Tailscale ACLs to allow TCP 60891, 6333, and 16379 only from approved Gobby
clients to the hub. Leave every other datastore port denied. Keep the host firewall
closed to public interfaces as a second boundary.

The installed Compose services use `restart: unless-stopped`. Preserve that policy so
the datastore stack returns after a hub reboot, then verify readiness:

```bash
gobby status
gobby health
docker compose -f ~/.gobby/services/docker-compose.yml ps
```

Keep secure copies of `~/.gobby/.secret_kek` and `~/.gobby/local_cli_token`. Each
client needs the same two files. Never copy the hub's `machine_id`; every machine must
retain its own identity.

## Client setup

Install the exact Gobby version used by the hub. Before running the installer, create
`~/.gobby/bootstrap.yaml` with remote mode and the hub PostgreSQL DSN:

```yaml
datastore_mode: "remote"
database_url: "postgresql://gobby:<password>@<hub-dns-name>:60891/gobby"
hub_daemon_url: "https://<hub-daemon-dns-name>"
postgres_pool:
  acquire_timeout_seconds: 5.0
  open_timeout_seconds: 30.0
daemon_port: 60887
bind_host: "localhost"
websocket_port: 60888
ui_port: 60889
```

Set `hub_daemon_url` to the authenticated HTTP(S) origin of the local files owner,
not this client's origin or a datastore port. A remote bootstrap must not contain
`files_home`. The owner must be reachable for the installer's profile probe.
The singleton lease still limits which daemon can serve normal operations: remote
mode does not promise concurrent active execution or make standby an owner-file server.

Copy the hub's shared secret material into the client Gobby home and restrict the
credentials and bootstrap to the owner:

```bash
scp <hub>:~/.gobby/.secret_kek ~/.gobby/.secret_kek
scp <hub>:~/.gobby/local_cli_token ~/.gobby/local_cli_token
chmod 600 ~/.gobby/.secret_kek ~/.gobby/local_cli_token ~/.gobby/bootstrap.yaml
```

Run the remote installer and start the client daemon:

```bash
gobby install
gobby start
gobby status
gobby health
```

In remote mode, `gobby install` skips Docker checks and local datastore provisioning.
Its preflight checks the copied key and token, probes the owner's `/api/files/user-md`,
runs a PostgreSQL query, reads shared configuration and secrets, checks Qdrant health,
and authenticates a FalkorDB `PING`. The remote installer does not generate or rotate
the copied token. A profile probe against a standby or remote target is not success.
Any failed check aborts installation with endpoint-specific diagnostics.

## Tailnet-only web UI

Gobby can publish its local web UI through Tailscale Serve while the daemon remains
bound to `localhost`. The same mandatory Gobby authentication policy applies through
the HTTPS URL. The exposure choice is stored as
`ui_expose: tailscale` in the machine-local `bootstrap.yaml` and is never copied into
shared configuration.

An interactive full install offers this setup when Tailscale is running, with
**No** as the default; `--no-interactive` skips it, and component installs never
touch exposure. Change it afterwards with `gobby ui expose` and `gobby ui unexpose`.

Manage exposure explicitly with:

```bash
gobby ui expose
gobby ui status
gobby ui unexpose
```

`gobby ui expose` manages only the HTTPS port 443 root handler and preserves sibling
Serve handlers. `gobby ui unexpose` verifies root-handler removal before forgetting
Gobby's intent. Use `gobby ui unexpose --forget` only when external Serve state is
being managed separately; it clears Gobby's intent without claiming the root handler
was removed.

On each `gobby start`, saved intent is reconciled after daemon readiness. A successful
reconciliation prints the MagicDNS HTTPS URL. A Tailscale failure emits a warning and
does not block daemon startup. With no saved intent, startup makes no Tailscale calls
and leaves manual Serve configuration alone.

`gobby ui status` reports exposure as off, healthy with its URL, or degraded with the
reason. Recovery is fail-closed: ensure the Tailscale backend and MagicDNS are enabled,
remove any conflicting port-443 protocol or foreign root proxy, and disable Funnel for
the node's `host:443`; then rerun `gobby ui expose` or `gobby start`. Gobby refuses to
combine this feature with Funnel because Funnel permits public ingress beyond the
tailnet.

## M0 acceptance checklist

This is a historical physical-topology acceptance checklist, not evidence that
the current deployment passed a multi-machine rehearsal. Revalidate its runbook
against the installed version before an operator starts the campaign; the
source/fixture audit of this guide does not perform that live cutover.

Use the [remote Docker stack live-test runbook](remote-docker-acceptance.md) for the
physical M0 acceptance run, together with the hub-PC move plan
(`.gobby/plans/hub-pc-datastore-move.md`, R0-R7). The runbook writes captured artifacts
to `.gobby/acceptance/<UTC-run-id>/`. Fill in the checklist below from the runbook's
Completion record before closing #19600.

| # | Item | Runbook phase | Evidence | Result |
| --- | --- | --- | --- | --- |
| 1 | UTC run ID and exact commit/version on both machines | Topology, Phase 1 | `identity.txt`, `remote-version.txt` | |
| 2 | Local installation protected and inventoried; bootstrap hash captured | Phase 1 | `local-status-before.txt`, `local-hub-backup.json`, `local-bootstrap.sha256`, `local-stack-before.txt` | |
| 3 | Isolated PostgreSQL/Qdrant/FalkorDB stack provisioned on machine A | Phase 2 | `remote-stack-before.txt`, `docker-ports.txt` | |
| 4 | Machine A and machine B IDs, roles, and Tailscale addresses recorded | Phase 3 | `machine-a.id`, `machine-b.id` | |
| 5 | Local production runtime stopped without removal; active sessions captured | Phase 4 | `local-active-sessions-before-stop.json`, `local-stack-stop.txt` | |
| 6 | Active daemon healthy on machine B; lease owner and schema identity recorded | Phase 5 | `daemon-health.json`, `remote-mode-status.txt`, `lease-status.txt`, `schema-version.json` | |
| 7 | Embedding switch and doctor clean against the remote stack | Phase 5 | `embedding-switch-final.json`, `embedding-doctor.json` | |
| 8 | Services bind tailnet-only; machine B reaches them and non-ACL machine C is refused | Phase 6 | `qdrant-health.txt`, `tailscale-ping.txt` | |
| 9 | Task, session, memory, vector, and graph round trips through the daemon API | Phase 7 | `task-create.json`, `session-register.json`, `memory-create.json`, `vector-reindex.txt`, `graph-rebuild.txt`, `memory-recall.txt`, `graph-counts.json` | |
| 10 | Continuity across a daemon restart; lease owner before and after | Phase 7 | `task-after-restart.json`, `sessions-after-restart.json`, `memory-after-restart.txt` | |
| 11 | PostgreSQL connection count, maximum, and remaining headroom within pool capacity | Phase 8 | `postgres-capacity.txt` | |
| 12 | Docker workload returned; original bootstrap hash and containers restored | Phase 9 | `remote-stack-stop.txt`, `local-stack-start.txt`, `local-status-restored.txt`, `local-lease-restored.txt`, `local-schema-restored.json`, `local-stack-restored.txt` | |
| 13 | Five-minute clean local-daemon observation window; sessions resumed and continued | Phase 9 | `local-status-restored.txt` | |

_The original checklist recorded no results. Consult the campaign's task history
for current physical-run evidence; old blocker IDs are not a live readiness check._

## M0 operating boundary

PostgreSQL task/session metadata, memories, vector data, graph data, and shared
configuration follow the user between machines. Each machine still owns its processes,
tmux sessions, worktrees, clones, and local transcript paths. Shared metadata is not
authorization to operate another machine's filesystem. Use session services for
supported transcript and archive access.

Profile, personal files, and chat attachment bytes use the canonical hub files home;
remote access forwards to its owner. Ordinary checkout roots are registered per
machine/project, and indexed paths remain project-root-relative. Do not use another
machine's checkout root for local pruning or repair.

Before packing up on one machine:

1. Commit and push work that the next machine needs. Register the destination
   machine's checkout; identical absolute paths are not required by project identity.
2. Release or explicitly hand off active task claims. Claims are session-bound and
   survive a daemon stop.
3. Stop the first daemon before moving an unreleased claim. On the second machine,
   force-reclaim only through the explicit task-claim contract after verifying the
   first machine is stopped, then verify that the new local session owns the claim.

Use supported session handoffs and archive retrieval for continuity; a shared session
record does not make its originating local transcript path accessible everywhere.

## Upgrades: stop every daemon

All machines sharing the hub must run the same Gobby version. Mixed-version operation
is unsupported. Use this stop-the-world protocol for every upgrade:

1. Stop every Gobby daemon connected to the hub.
2. Update Gobby to the same version on every machine.
3. Start one designated migrator and wait for migration and health checks to succeed.
4. Start the remaining daemons.

Never start an older daemon after a newer schema migration has been applied. The
migration lockstep guard treats a hub schema newer than the local binary as a fatal
startup error.

The machine-scoping migration can abort when a legacy worktree, clone, agent run, or
cron run has no authoritative machine owner. Use the emitted table and row diagnostics
to investigate each unresolved row. In an operator-controlled SQL transaction, assign
a verified real machine owner or remove a confirmed stale row, then rerun the migration.
Do not invent a sentinel machine or guess ownership.

## Capacity and deferred hardening

- Qdrant API-key configuration is deferred. Restrictive Tailscale ACLs remain mandatory
  for port 6333 in M0.
- Machine IDs identify ownership, not connectivity. Inspect the destination checkout
  and supported session archive access rather than assuming shared absolute paths.
- Size PostgreSQL `max_connections` for all daemon pools plus concurrent CLI activity
  across every client. For two daemons, observe `pg_stat_activity` under realistic CLI
  churn and retain headroom for migrations and operator access. Validate the PostgreSQL
  default limit of 100 against the measured workload.

## Machine and project ownership

`gobby-hub:get_machine_id` reports the connected daemon's stable machine ID. Its
resolver reads and caches `$GOBBY_HOME/machine_id` and can create it when absent.
Do not distribute that file with shared credentials. Filesystem failures propagate;
restore the correct Gobby home rather than inventing an ownership ID.

An ordinary project's UUID is shared; `project_checkouts` maps each machine/project
pair to its validated local root. `.gobby/project.json` must identify that same project.
`_personal` is checkout-free and uses the hub-owned files home.
See [project CLI operations](cli-commands.md#gobby-projects) and
[project HTTP operations](http-endpoints.md#project-identity-and-checkouts).

## Hub queries

Agents discover and fetch schemas for the five `gobby-hub` tools. These inspect the
connected hub; they do not transfer claims, manage projects, or control foreign terminals.

| Tool | Scope and interpretation |
| --- | --- |
| `get_machine_id` | Connected daemon identity; normal resolver may initialize a missing file. |
| `list_all_projects` | Non-deleted projects sorted by name, with all task/session row counts. Default excludes names prefixed `_orphaned`, `_migrated`, `_personal`, or `_global`; `include_system=true` includes them. No checkout path. |
| `list_cross_project_tasks` | Latest updates first, default limit 50; optional stored `state` bucket filter. Result state is a structured lifecycle object. |
| `list_cross_project_sessions` | Latest creations first, default limit 20; excludes system-source sessions, not closed or foreign-machine sessions. |
| `hub_stats` | Task totals by stored state; non-system sessions by status; memory row count; project IDs represented in tasks/non-system sessions. |

Cross-project task/session results do not apply project-list visibility filters.
Their `count` is the returned count, and neither query offers an offset or cursor.
Retain full UUID and `project_id` for follow-up through the destination service;
unqualified `#N` references remain project-local. `create_task` accepts a selected
project name or UUID through `project`, subject to its normal lifecycle requirements.

`hub_stats.project_count` excludes empty projects, unlike `list_all_projects`.
Its memory count also becomes zero when that subquery fails; verify an unexpected
zero with memory diagnostics. The project HTTP API counts live sessions and open
tasks instead. Separate aggregate queries are not one consistent snapshot.
On `success=false`, inspect the error; an unavailable hub database is not an empty hub.

_Last verified: 2026-09-12_
