# Shared Datastores Across Machines

Gobby supports a `datastore_mode: remote` topology in which one hub machine owns
PostgreSQL, Qdrant, and FalkorDB while every client machine runs its own Gobby daemon.
The datastores are shared over a private Tailscale network; execution stays local to
each client.

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

Install the same Gobby version that the clients will run. The hub uses the default
local datastore mode:

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
postgres_pool:
  acquire_timeout_seconds: 5.0
  open_timeout_seconds: 30.0
daemon_port: 60887
bind_host: "localhost"
websocket_port: 60888
ui_port: 60889
```

Copy the hub's shared secret material into the client Gobby home and restrict it to the
owner:

```bash
scp <hub>:~/.gobby/.secret_kek ~/.gobby/.secret_kek
scp <hub>:~/.gobby/local_cli_token ~/.gobby/local_cli_token
chmod 600 ~/.gobby/.secret_kek ~/.gobby/local_cli_token
```

Run the remote installer and start the client daemon:

```bash
gobby install
gobby start
gobby status
gobby health
```

In remote mode, `gobby install` skips Docker checks and local datastore provisioning.
Its preflight checks the copied key and token, runs a PostgreSQL query, reads shared
configuration and secrets, checks Qdrant health, and authenticates a FalkorDB `PING`.
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

Use the [remote Docker stack live-test runbook](remote-docker-acceptance.md) for the
physical M0 acceptance run. The runbook writes captured artifacts to
`.gobby/acceptance/<UTC-run-id>/`. Record the completed checklist and its evidence
filenames in this section before closing #19600.

_Status: pending the operator-coordinated physical run._

## M0 operating boundary

PostgreSQL task/session metadata, memories, vector data, graph data, and shared
configuration follow the user between machines. Each machine still owns its daemon,
processes, tmux sessions, worktrees, clones, and transcript files. A client must never
process another machine's filesystem paths or transcripts.

Attachment metadata that points at local blobs and code-index dirty-prune rows carry
their originating `machine_id`. Cleanup and retry queries use that owner, so one daemon
cannot delete another machine's blob metadata or retry its absolute checkout path.
Indexed file paths remain project-root-relative and portable. Global `gcode prune`
still reconciles shared datastore orphans, while it skips filesystem-based stale-root
classification until #17435 and #17437 add authoritative machine-to-checkout mappings.

Before packing up on one machine:

1. Commit and push work that the next machine needs. Identical checkout paths across
   machines are strongly recommended.
2. Release or explicitly hand off active task claims. Claims are session-bound and
   survive a daemon stop.
3. Stop the first daemon before moving an unreleased claim. On the second machine,
   force-reclaim only through the explicit task-claim contract after verifying the
   first machine is stopped, then verify that the new local session owns the claim.

Cross-machine transcript continuity is deferred to #17435. Until that work lands,
pushing source and handing off claims provides task continuity; transcript files remain
on their originating machine.

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
- Hook-side `machine_id` fallback and full transcript continuity belong to #17435.
- Size PostgreSQL `max_connections` for all daemon pools plus concurrent CLI activity
  across every client. For two daemons, observe `pg_stat_activity` under realistic CLI
  churn and retain headroom for migrations and operator access. Validate the PostgreSQL
  default limit of 100 against the measured workload.

_Last verified: 2026-08-05_
