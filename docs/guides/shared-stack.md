# Shared Stack On Tailscale

This guide covers one daemon and one PostgreSQL hub on a trusted Tailscale box,
with other machines pointing their Gobby clients at that stack.

This is a management and metadata-plane setup. Tasks, sessions, memory, config,
MCP proxy access, hook ingestion, and direct hub readers can share one backend.
Instrumented local coding, transcript capture, worktree paths, clone paths, and
spawned agent execution still belong to the daemon box until the remote-machine
execution model is redesigned.

## Topology

```text
client laptop --+
workstation ----+-- Tailscale ACL -- gobby-box
phone/browser --+                   |-- Gobby daemon: http://<box>.ts.net:60887
                                    |-- WebSocket:     <box>.ts.net:60888
                                    |-- Web UI:        http://<box>.ts.net:60889
                                    `-- PostgreSQL:    <box>.ts.net:60891
```

Use Tailscale ACLs as the primary security boundary. Do not expose the daemon,
WebSocket, UI, or PostgreSQL ports on a public interface, router port-forward, or
open LAN without an equivalent trusted network boundary.

## Daemon Box

Install and start Gobby on the machine that will own the daemon, service
process, local worktrees, clones, transcripts, and agent execution.

In `~/.gobby/bootstrap.yaml`, bind the daemon to the Tailscale interface or all
interfaces:

```yaml
hub_backend: "postgres"
database_url: "postgresql://gobby:<password>@127.0.0.1:60891/gobby"
postgres_install_mode: "docker"
daemon_port: 60887
bind_host: "100.x.y.z"
websocket_port: 60888
ui_port: 60889
```

`bind_host` is only a listen address. It is not the client dial URL. Use the
box's Tailscale IP when you want the daemon to listen only on Tailscale, or
`0.0.0.0` when the host firewall and Tailscale ACLs already restrict access.

Add the tailnet origin to runtime config so browsers can call the daemon from a
Tailscale hostname:

```yaml
cors_origins:
  - http://localhost:*
  - https://localhost:*
  - http://*.ts.net
  - https://*.ts.net
  - http://*.ts.net:*
  - https://*.ts.net:*
```

Prefer exact host patterns such as `https://gobby-box.tailnet.ts.net:*` when the
box has a stable MagicDNS name. The broad `*.ts.net` form trusts any matching
Tailscale DNS origin that can reach the daemon.

Restart the daemon after changing `bootstrap.yaml`. Restarting is also safest
after changing runtime CORS config so every route uses the new value.

## PostgreSQL

PostgreSQL must accept connections from the tailnet as well as from the daemon
box. Configure the server to listen on the Tailscale interface, then allow only
the users or tailnet ranges that should share the hub.

Typical PostgreSQL settings:

```conf
listen_addresses = '127.0.0.1,100.x.y.z'
```

Typical `pg_hba.conf` entry:

```conf
host    gobby    gobby    100.64.0.0/10    scram-sha-256
```

Use a real password in `database_url`, keep `~/.gobby/bootstrap.yaml` mode
`0600`, and avoid publishing PostgreSQL outside Tailscale. If the Postgres
container or service can bind a specific host interface, bind it to the
Tailscale IP instead of every interface.

## Client Machines

Each remote machine needs both a daemon dial URL and a hub database URL.

For shell-only use, set:

```bash
export GOBBY_DAEMON_URL="http://gobby-box.tailnet.ts.net:60887"
```

For durable use, write `~/.gobby/bootstrap.yaml` on the client:

```yaml
hub_backend: "postgres"
database_url: "postgresql://gobby:<password>@gobby-box.tailnet.ts.net:60891/gobby"
daemon_url: "http://gobby-box.tailnet.ts.net:60887"
daemon_port: 60887
bind_host: "localhost"
websocket_port: 60888
ui_port: 60889
```

`daemon_url` tells local clients, hooks, and helper binaries where to dial.
`database_url` tells direct-hub tools where the shared metadata lives. Set both.

Keep client `bind_host` as `localhost` unless that machine is also running its
own daemon. `GOBBY_PORT` and `GOBBY_DAEMON_PORT` only override the port used for
a local daemon URL; they do not name the remote host. Prefer `GOBBY_DAEMON_URL`
or bootstrap `daemon_url` for shared-stack clients.

Quick checks from a client:

```bash
curl http://gobby-box.tailnet.ts.net:60887/api/admin/health
psql "postgresql://gobby:<password>@gobby-box.tailnet.ts.net:60891/gobby" -c "select 1"
```

## Security Model

This setup trusts the tailnet. Anyone allowed by Tailscale ACLs and PostgreSQL
auth can reach privileged local-first control surfaces.

The Tailscale ACL should allow only the operator trust group to reach ports
`60887`, `60888`, `60889`, and `60891` on the daemon box.

With no UI credentials configured, the HTTP API is unauthenticated. Daemon API
access allows callers to read and mutate tasks, sessions, memory, configuration,
MCP proxy state, and admin status. Agent spawn routes can start processes on the
daemon box with access to daemon-local projects, worktrees, clones, credentials,
and provider CLIs. Treat daemon API access as command execution on the daemon
machine.

PostgreSQL access allows direct reads and writes to hub metadata. Restrict
`pg_hba.conf`, rotate the hub password if it leaks, and avoid sharing the DSN
outside the same operator trust group.

UI username/password auth can protect many HTTP API routes, but several local
integration surfaces are intentionally public or separately scoped. Do not use
UI auth as the only boundary for a shared stack. Tailscale ACLs and Postgres
auth remain the boundary.

## Secrets

Secret values live encrypted in the hub. In this shared-stack model, remote
clients must not receive plaintext secret values, the DEK, or KEK material. Do
not copy `~/.gobby/.secret_kek` or distribute a KEK passphrase to ordinary
clients.

Secret-backed configuration should be used through the daemon or through trusted
local binaries that the operator deliberately provisions for standalone
direct-hub mode. That standalone path is outside this ordinary remote-client
model; treat those binaries as part of the trusted operator environment.

## Current Limitations

Works today:

- One shared PostgreSQL hub for tasks, sessions, memory, config, rules, skills,
  pipelines, metrics, and other metadata.
- Remote daemon API access through a Tailscale dial URL.
- Remote browser access when CORS allows the tailnet origin.
- Hook and helper clients that send data to the remote daemon.
- Direct-hub metadata tools when the client has `database_url`.

Still daemon-local:

- Spawned agents run on the daemon box.
- Worktree and clone paths are daemon-box paths.
- Provider CLIs, credentials, sandboxes, and shell environments are those of the
  daemon box.
- Local coding sessions started on a remote laptop are not fully instrumented as
  remote-machine execution.
- Transcript capture, per-machine agent execution, remote filesystem scoping,
  cross-machine worktree ownership, and multi-user auth are deferred
  rearchitecture work.

The honest operating model: use the shared stack for central state and trusted
remote control. Run code-affecting agents where the daemon can see the files and
where you are comfortable granting command execution.

## Troubleshooting

If a client dials `127.0.0.1`, set `GOBBY_DAEMON_URL` or bootstrap
`daemon_url`. A wildcard daemon `bind_host` such as `0.0.0.0` is normalized to a
loopback dial host for local clients, so remote clients need an explicit URL.

If browser requests fail CORS, add the exact MagicDNS origin or an appropriate
`*.ts.net` pattern to `cors_origins`, then restart or reload the daemon.

If PostgreSQL rejects clients, check Tailscale ACLs, host firewall rules,
PostgreSQL `listen_addresses`, `pg_hba.conf`, and the password embedded in the
client `database_url`.

If secret-backed config works on the daemon box but fails from a remote client,
that client is trying to decrypt or resolve secrets locally. Route the operation
through the daemon or deliberately provision the trusted standalone binary path.
