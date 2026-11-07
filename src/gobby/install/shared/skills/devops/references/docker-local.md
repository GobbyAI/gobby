# Local Docker Services

Check the containers a project runs locally: resource use, health, stale leftovers.
This is local operations, not image building or cluster deployment.

## Discover

```bash
docker info --format '{{.OperatingSystem}} cpus={{.NCPU}} mem={{.MemTotal}}'
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
docker ps -a --filter status=exited --format '{{.Names}}\t{{.Status}}'
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}'
docker compose ls
```

Map each container to its owner: the project's compose files, Gobby's optional services
(Postgres, FalkorDB, Qdrant, observability), a test fixture or another project. Record
the map in the host profile.

## Resource Checks

| Check | How | Concern |
|---|---|---|
| VM memory headroom | Sum of `docker stats` usage versus `docker info` memory | Above about 80% risks swapping and OOM kills |
| One container dominating | `docker stats` | A graph or vector store growing without a limit |
| VM CPU on the host | Host `ps` for the VM process | Wakeups from idle containers still cost |
| Restart loops | `docker ps` status, `docker inspect -f '{{.RestartCount}}' <name>` | A crash loop burns CPU |
| Slow queries | The service's own logs: `docker logs --since 10m <name>` | Timeouts seen by the daemon |
| Health | `docker inspect -f '{{.State.Health.Status}}' <name>` | Unhealthy but still running |

For a database container, check its own views (for Postgres: `pg_stat_activity` for
long or idle-in-transaction sessions) through the project's read-only access, never by
handling credentials yourself.

## Stale Containers

A container is a cleanup candidate when no compose project, running test or recorded
experiment owns it: for example a test fixture named after an old task, or a research
stack from a finished study.

1. List candidates with name, image, age, size and owner evidence.
2. Check for named volumes they use (`docker inspect -f '{{json .Mounts}}' <name>`).
3. Send the list to the operator. Stop and remove only the approved items; keep their
   named volumes unless separately approved.

## Limits And Tuning

Recommend, don't apply: memory limits in the project's compose file, the VM's memory and
CPU allocation, or moving a service to its native install. Each recommendation names
the measured problem it fixes.

## Images And Builds

When a project builds its own images, keep them small and cache-friendly: multi-stage
builds, a `.dockerignore`, pinned base image versions, and a non-root user in anything
that runs beyond local development. Treat a rebuild loop (an image rebuilt on every
start) as load to report.

_Version: 1.0.0_
_Last verified: 2026-11-06_
