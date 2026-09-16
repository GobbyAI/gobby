# Gobby Observability Stack

[`compose.yaml`](compose.yaml) runs Prometheus and Grafana against a local Gobby
daemon, plus an optional Loki and `otelcol-contrib` pair under the `logs`
profile. Prometheus scrapes `/api/admin/metrics` every 15 s and Grafana
provisions one dashboard over it, so pool saturation, MCP and HTTP latency,
restarts, and error-log rate survive a daemon restart instead of being
discarded with the process.

The stack is operator-managed. Gobby does not start, supervise, or health-check
these containers, and nothing here changes daemon configuration.
See the [operator observability guide](../../guides/observability.md#prometheus-and-admin-routes)
for the metric taxonomy behind the panels.

## Prerequisites

1. **A running daemon.** Prometheus targets `host.docker.internal:60887`. That
   port is the daemon's `http.port`; the installed value lives in
   `~/.gobby/bootstrap.yaml`, whose default is `60887`. Read only the port from
   that file — it also holds the PostgreSQL `database_url`, which must never be
   echoed, pasted, or committed. When your port differs, edit the `targets`
   entry in [`prometheus.yml`](prometheus.yml): Prometheus does not interpolate
   environment variables into its own configuration. Apply an edit without
   losing history by reloading in place, which the `--web.enable-lifecycle` flag
   in `compose.yaml` enables:

   ```bash
   curl -X POST http://127.0.0.1:9090/-/reload
   ```

2. **`telemetry.metrics_enabled` and `telemetry.exporter.prometheus_enabled`
   both true.** Both default to true. The route itself is not gated — it always
   returns a 200 — but `create_metric_readers` in
   `src/gobby/telemetry/exporters.py` attaches the `PrometheusMetricReader` only
   when both settings are on. With either one off, `/api/admin/metrics` returns
   nothing but the `prometheus_client` process defaults and every Gobby series
   is missing, so the target reads UP while all panels stay empty. Confirm the
   endpoint really carries Gobby series before blaming the stack:

   ```bash
   curl -sf -H "Authorization: Bearer $(cat ~/.gobby/local_cli_token)" \
     http://127.0.0.1:60887/api/admin/metrics | grep -c '^daemon_uptime_seconds'
   ```

   A `1` means both settings are on. A `0` means they are not; change them in
   the Web UI under Settings -> Observability, or through the `gobby-config`
   MCP tools. There is no `gobby config` CLI.

3. **A readable `~/.gobby/local_cli_token`.** Prometheus bind-mounts that file
   read-only at `/etc/prometheus/secrets/gobby_token` and sends its contents as
   the bearer credential. The token is never copied into this directory, into
   `prometheus.yml`, or into any image. On Docker Desktop, `~/.gobby` must be
   inside a shared file path (Settings -> Resources -> File sharing).

## Start And Stop

Run from the repository root:

```bash
docker compose -f docs/examples/observability/compose.yaml up -d
```

- Prometheus: <http://127.0.0.1:9090> (targets at `/targets`)
- Grafana: <http://127.0.0.1:3000>, dashboard **Gobby / Gobby Daemon Health**

Both ports publish on loopback only. Grafana allows anonymous `Viewer` access so
the dashboard opens without a credential in the compose file; editing still
requires signing in with the built-in `admin` account, which Grafana forces you
to re-password on first sign-in.

Add the log pipeline with the profile. Create the collector's checkpoint
directory first; it is a host directory rather than a named volume, for the
reason the compose file records next to the mount:

```bash
mkdir -p ~/.gobby/otelcol
docker compose -f docs/examples/observability/compose.yaml --profile logs up -d
```

Stop, keeping history:

```bash
docker compose -f docs/examples/observability/compose.yaml --profile logs down
```

Discard history as well:

```bash
docker compose -f docs/examples/observability/compose.yaml --profile logs down -v
```

Check the configuration without starting anything:

```bash
docker compose -f docs/examples/observability/compose.yaml config
```

## What Each Panel Answers

**Database saturation** — is a stalled `spawn_agent` the pool, or the caller?

| Panel | Question |
| --- | --- |
| Pool acquire wait p95 | How long callers wait for a PostgreSQL connection. |
| Pool waiters and executor queue depth | Whether callers are blocked (`database_pool_waiting`, `database_executor_queued`, `database_executor_active`, `database_pool_available`). |
| Executor oldest queue age | Which series crosses an MCP or HTTP client timeout first. |
| Executor queue wait p95 | Time spent queued, separate from time spent working. |

**MCP and HTTP latency** — is one tool or route slow, or is everything slow?

| Panel | Question |
| --- | --- |
| MCP tool call p95 by tool | Per-`tool_name` latency. Flat here with rising pool waits means the database, not the tool. |
| HTTP request p95 by route | Per-`http_target` latency. Targets are route templates, so path parameters do not fan out series. |

**Daemon health** — did the daemon stay up, and did it log errors?

| Panel | Question |
| --- | --- |
| Daemon uptime | Every reset to zero is a restart; a gap is an outage Prometheus could not scrape. |
| ERROR and CRITICAL log records by surface | `rate(logging_records_total{severity=~"ERROR\|CRITICAL"}[5m])` by `surface`, alongside `hooks_failed_total`. |

**Logs** (collapsed, needs `--profile logs`) — daemon and hook lines on the same
time axis as the metrics.

### Histogram Resolution

The daemon's histograms use OpenTelemetry's default explicit bucket boundaries
(`0, 5, 10, 25, ... 10000`), which were chosen for millisecond-valued data.
Gobby records these three in seconds, so almost every observation lands in the
first non-empty bucket and `histogram_quantile` interpolates inside `(0, 5]`.
Read the p95 panels as a seconds-scale upper bound that reliably catches
regressions into multi-second territory, not as sub-second precision. The
counts and gauges on the same rows are exact.

## Retention

| Knob | Default here | Where |
| --- | --- | --- |
| `PROMETHEUS_RETENTION_TIME` | `15d` | environment, read by `compose.yaml` |
| `PROMETHEUS_RETENTION_SIZE` | `2GB` | environment, read by `compose.yaml` |
| Loki retention | unlimited | `compose.yaml` runs Loki's packaged `local-config.yaml`; capping it needs `limits_config.retention_period` in an operator-owned config |

Prometheus applies whichever limit trips first. Override either per invocation:

```bash
PROMETHEUS_RETENTION_TIME=30d \
  docker compose -f docs/examples/observability/compose.yaml up -d
```

History lives in the named volumes `gobby-observability_prometheus-data`,
`gobby-observability_grafana-data`, and `gobby-observability_loki-data`, so it
survives `down` and restarts. Only `down -v` removes it. Collector checkpoints
live in `~/.gobby/otelcol` and `down -v` leaves them alone.

## Log Profile Details

The `logs` profile reuses
[`../otel-collector/gobby-logs.yaml`](../otel-collector/gobby-logs.yaml)
unchanged, keeping its `GOBBY_LOG_DIR`, `GOBBY_OTEL_STORAGE_DIR`, and
`GOBBY_OTLP_ENDPOINT` contract; the compose file supplies
`http://loki:3100/otlp` for the last of these. Validate that configuration
against its pinned image exactly as the collector reference documents:

```bash
docker run --rm -v "$PWD/docs/examples/otel-collector/gobby-logs.yaml:/etc/otelcol-contrib/config.yaml:ro" otel/opentelemetry-collector-contrib:0.156.0 validate --config=/etc/otelcol-contrib/config.yaml
```

Every receiver uses `start_at: end`, so the first run collects only lines
written after it starts. OTLP attributes reach Loki as structured metadata;
the dashboard's logs panel filters on `gobby_log_surface`. If it comes back
empty, run `{service_name=~".+"}` in Explore against the Gobby Loki datasource
and read the actual stream labels off a returned line.

## Portability

`host.docker.internal` is a Docker Desktop convenience. On macOS and Windows its
traffic is proxied from the host, so it reaches a daemon bound to `127.0.0.1` —
which is what Gobby binds by default. On Linux the `extra_hosts` entry resolves
to the real host gateway address, which a loopback-bound daemon does not answer.
There, either bind the daemon's HTTP listener to an address the container can
reach, or run Prometheus with host networking.
