# Daemon snapshot before restart: PID 25724, 2026-09-24 17:50:29 CDT

Researcher gobby#14435, captured read-only for the PD's uptime-degradation question
(Q1). The daemon started 13:06:35 CT (restart #26) and had been up 4h43m54s.

## Process

| Measure | Value |
| --- | --- |
| RSS | 2,085,888 KB (1.99 GiB) |
| VSZ | 491,066,704 KB |
| CPU (ps, instantaneous) | 108% |
| %MEM | 1.6 |
| Threads (`ps -M`) | 77 |
| Open FDs (`lsof -p`) | 190 |
| Machine load average | 13.06 / 11.04 / 11.04 |

Open FDs by type: 107 REG, 29 unix, 29 IPv4, 11 PIPE, 5 PSXSEM, 4 IPv6, 3 DIR,
2 KQUEUE, and 1 each of systm, NPOLICY and CHR.

## Hub DB connections (`pg_stat_activity`, this database)

| application_name | state | count | longest in state |
| --- | --- | ---: | --- |
| gobby-hub-8f2b4499bdfe95a9-72fda76f | idle | 10 | 0.08 s |
| gobby-hub-8f2b4499bdfe95a9-72fda76f-listener | idle | 3 | 4h43m41s (LISTEN connections, expected) |
| gobby-hub-8f2b4499bdfe95a9-72fda76f | idle in transaction | 1 | 0.005 s |
| gobby-lease-v1:f7787eaf-...:324ffc82 | idle | 1 | 1.8 s |
| psql (this probe) | active | 1 | |

There were 16 connections in total and 1 idle-in-transaction. No connection leaked or
went stale. The `metric_snapshots` gauges showed `database_pool_waiting` at 0 for every
minute from 05:00 to 17:48.

## Not captured

- **py-spy dump:** refused with "This program requires root on OSX". I did not escalate,
  and I did not run `sample` because the PD asked for no profiling.

## Survives the restart

- The per-minute series is kept: `metric_snapshots` holds uptime, RSS, CPU, pool
  waits and `hooks_total`.
- `~/.gobby/logs/errors.log*` keeps the slow-hook warnings.
- Q1 analysis continues from those.
