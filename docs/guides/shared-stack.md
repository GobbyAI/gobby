# Shared Daemon On Tailscale

Gobby supports a daemon shared over a trusted Tailscale network. The daemon
host owns the required local Docker Compose stack—PostgreSQL, Qdrant, and
FalkorDB—and remote machines access Gobby through its authenticated HTTP and
WebSocket interfaces.

External PostgreSQL hosts and direct remote hub connections are not supported.
Instrumented coding, transcript capture, worktrees, clones, and spawned agent
execution also remain on the daemon host.

## Topology

```text
client laptop --+
workstation ----+-- Tailscale ACL -- gobby-box
phone/browser --+                   |-- Gobby daemon: http://<box>.ts.net:60887
                                    |-- WebSocket:     <box>.ts.net:60888
                                    |-- Web UI:        http://<box>.ts.net:60889
                                    `-- local Docker-managed datastores
```

Do not expose the daemon, WebSocket, or UI on a public interface without an
equivalent trusted network boundary. The datastore ports should remain local
to the daemon host.

## Daemon Host

Install Gobby with Docker Compose v2 available, then run the full installer.
The installer provisions every managed datastore profile regardless of the
embedding-provider selection:

```bash
uv run gobby install
uv run gobby start
```

Bind the daemon to its Tailscale address in `~/.gobby/bootstrap.yaml`. Keep the
PostgreSQL connection local:

```yaml
hub_backend: postgres
database_url: "postgresql://gobby:<password>@127.0.0.1:60891/gobby"
daemon_port: 60887
bind_host: "100.x.y.z"
websocket_port: 60888
ui_port: 60889
auth_mode: required
```

`bind_host` is a listen address, not a client URL. Use the host's Tailscale IP
to listen only on Tailscale, or `0.0.0.0` only when host firewall rules and
Tailscale ACLs already restrict access.

Add the trusted tailnet origin to runtime CORS configuration for browser use:

```yaml
cors_origins:
  - http://localhost:*
  - https://localhost:*
  - http://gobby-box.tailnet.ts.net:*
```

Restart Gobby after changing bootstrap or CORS settings. Startup will fail
before the daemon launches unless all three managed services are configured and
healthy.

## Remote Clients

Point remote clients at the daemon API:

```bash
export GOBBY_DAEMON_URL="http://gobby-box.tailnet.ts.net:60887"
```

For Rust clients such as gcode and gwiki, a non-empty `GOBBY_DAEMON_URL`
selects daemon runtime mode without requiring a local service installation.
The selection is cached for one process invocation. Restart long-lived client
processes after changing the URL or installing/removing a service.

`GOBBY_RUNTIME_MODE=standalone` has higher precedence and is intended for an
explicit local standalone stack. Leave it unset or set it to `auto` on remote
clients. Unknown values are configuration errors.

Daemon mode remains daemon mode when the remote daemon is stopped, returns
401, or returns a 5xx response. Those conditions are hard client errors; they
do not expose local full-`gcore.yaml` fallback values. Remote DSN resolution is
environment, daemon-served DSN, then local `bootstrap.yaml`. Normally the
daemon-served DSN is authoritative for Rust client operations.

Do not put the daemon host's PostgreSQL URL in a remote client bootstrap. The
runtime bootstrap contract accepts only local Docker-managed PostgreSQL hosts.

Daemon HTTP and WebSocket authentication is required by default. Copy the
daemon host's install-scoped token to each trusted client at
`$GOBBY_HOME/local_cli_token` (normally `~/.gobby/local_cli_token`) and preserve
owner-only permissions. Rotate it on the daemon host with:

```bash
gobby auth token --rotate
```

Then redistribute the new token and restart affected clients.

## Operational Checks

On the daemon host:

```bash
gobby status
gobby health
docker compose -f ~/.gobby/services/docker-compose.yml ps
```

`gobby start` brings up the `postgres`, `qdrant`, and `falkordb` profiles with
Compose health waiting. Failure to start any profile prevents daemon launch.
After readiness, a later Qdrant or FalkorDB outage degrades health/status but
does not terminate the daemon; PostgreSQL remains the core datastore.

If remote clients cannot connect, check the daemon bind address, Tailscale ACLs,
host firewall rules, CORS origins, and the shared API token. Datastore
troubleshooting should be performed locally on the daemon host.

_Last verified: 2026-07-24_
