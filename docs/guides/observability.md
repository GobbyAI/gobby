# Observability

Gobby observability spans daemon health, MCP tool metrics, traces, token usage,
savings, dashboard charts, Prometheus exposition, and admin diagnostics.

## Mental Model

The daemon records operational data at several layers:

- Logs explain immediate runtime behavior.
- OpenTelemetry spans describe traced operations.
- Hub-backed metrics aggregate MCP tool calls and event history.
- Token events record model usage by source, model, project, and session.
- Savings events record token or character savings from code index, compression,
  discovery, and related systems.
- Admin HTTP routes expose health and dashboard data.
- The web dashboard and traces pages visualize those records.

Use dashboard and admin routes for operator status. Use `gobby-metrics` MCP tools
for agent-readable reports. Use logs and trace storage when debugging a specific
execution path.

## Quick Start

Check daemon health:

```bash
uv run gobby status
curl -sS http://localhost:60887/api/admin/status
```

Open dashboard and traces:

```text
http://localhost:60887/#dashboard
http://localhost:60887/#traces
```

Fetch Prometheus metrics:

```bash
curl -sS http://localhost:60887/api/admin/metrics
```

Ask the MCP metrics server for a report:

```text
list_tools(server_name="gobby-metrics")
get_tool_schema(server_name="gobby-metrics", tool_name="get_usage_report")
call_tool(server_name="gobby-metrics", tool_name="get_usage_report", ...)
```

## Log Files

Gobby writes logs under `logging.dir`, which defaults to `~/.gobby/logs`.
Start with the file whose surface matches the failing operation:

| File | Inspect it for |
| --- | --- |
| `daemon.log` | General daemon startup, storage, provider, HTTP, and subsystem records that do not belong to a specialized surface |
| `errors.log` | Aggregate view of every Gobby `WARNING`, `ERROR`, and `CRITICAL` record |
| `runtime.log` | Raw daemon process stdout/stderr, including failures before formatted logging is available |
| `hooks.log` | Hook ingestion, dispatch, adapter, and lifecycle behavior |
| `llm.log` | Feature LLM calls, candidate routing, latency, outcomes, and provider circuit-breaker events |
| `mcp.log` | MCP proxy, server, client, discovery, and MCP route behavior |
| `automation.log` | Scheduler, dispatcher, build, system automation, and pipeline-heartbeat behavior |
| `ui.log` | UI development-server stdout/stderr |
| `*-parser-error.log` | Per-CLI transcript parser diagnostics, such as `codex-parser-error.log` |

Each formatted record has exactly one primary file among `daemon.log`,
`hooks.log`, `llm.log`, `mcp.log`, and `automation.log`. Parser diagnostics use their
per-CLI parser file. Gobby also writes every `WARNING` or higher record to
`errors.log` intentionally, so the same source record appears in its primary
file and the aggregate. This duplicate write is useful for incident scanning;
`logging_records_total` still counts the source record once.

### Rotation And Size Limits

`daemon.log`, `hooks.log`, `mcp.log`, `automation.log`, `errors.log`, and the
parser-error files rotate at `logging.max_size_mb`. Gobby keeps
`logging.backup_count` numbered backups for each file. The defaults are 10 MiB
and five backups. `llm.log` uses independent `logging.llm_max_size_mb` and
`logging.llm_backup_count` settings, defaulting to 50 MiB and five backups.
`ui.log` has an independent 5 MiB limit and three backups.

`runtime.log` is an append-only capture owned by the process launcher or OS
service. `logging.runtime_max_size_mb` is a health threshold: crossing it marks
the daemon degraded and emits a warning; it does not delete, rotate, or truncate
the file. `logging.growth_warn_mb_per_interval` warns when the whole log
directory grows too quickly between resource-monitor samples. Truncating
`runtime.log` remains an operator or service-manager action.

See [Logging And Telemetry](configuration.md#logging-and-telemetry) for all
`logging.*` fields and Windows path configuration.

### Contributor Logging Convention

Ruff enforces `G001`, `G002`, `G003`, `G004`, `G010`, and `G101`. Pass dynamic
values as lazy logging arguments, such as `logger.info("Processed %s", item)`,
instead of formatting the message before the logger receives it. Keys passed in
`extra` must avoid reserved `LogRecord` attributes such as `name`; prefer a
domain-specific key such as `pipeline_name`. `G201` remains outside this policy.

## Collect Logs With OpenTelemetry

The tested [`otelcol-contrib` reference configuration](../examples/otel-collector/README.md)
tails all eight default surfaces with `filelog` receivers and sends them to an
operator-selected OTLP/HTTP backend. Gobby does not start or supervise the
collector. Deploy it as a separate service with:

- `GOBBY_LOG_DIR` pointing at the same directory as `logging.dir`.
- `GOBBY_OTEL_STORAGE_DIR` on durable, access-controlled storage for checkpoints
  and the persistent sending queue.
- `GOBBY_OTLP_ENDPOINT` pointing at the collector's log backend.

Every receiver uses `start_at: end`. On its first observation of a file, the
collector starts with new records and leaves existing history on disk. This is
the privacy-preserving first-start behavior, especially for parser payloads.
Afterwards, the `file_storage` extension checkpoints offsets so collector
restarts resume instead of rereading the whole file.

Receiver retries and the persistent exporter queue provide at-least-once
delivery, so backends must tolerate duplicates. The reference sets
`on_truncate: read_whole_file`: when an operator or service truncates an
included file in place, the collector rereads it to avoid losing new bytes.
That recovery can replay records exported before truncation.

Three endpoints have separate owners:

- `telemetry.exporter.otlp_endpoint` is Gobby's in-process span destination.
- The collector `filelog` receivers read paths under `GOBBY_LOG_DIR`.
- Collector `GOBBY_OTLP_ENDPOINT` configures its backend log exporter.

Changing one does not configure the other two.

### Default Collector Exclusions

The reference collector intentionally excludes these adjacent diagnostic
surfaces:

- `code-index-maintenance.log`, the code-index maintenance event log.
- `recall_signal.jsonl`, which is structured recall data rather than a log.
- Standalone `ghook` stderr outside the daemon-managed hook surface.
- Gwiki vault `log.md` and `_meta/` dumps.
- PostgreSQL `pgaudit` records.

Add separate receivers only after choosing parsing, access, and retention rules
for each format. They can contain different or more sensitive data than the
default eight-file set.

### Windows Collector Paths

Set `logging.dir` and `GOBBY_LOG_DIR` to the same absolute directory. Use a
quoted forward-slash path such as `"C:/Users/name/.gobby/logs"` so the reference
receiver globs remain portable. Run `otelcol-contrib.exe` directly and give its
service account read access to the log directory plus write access to the
absolute `GOBBY_OTEL_STORAGE_DIR` checkpoint directory.

### Privacy And Retention

Logs can contain local paths, project and session identifiers, provider errors,
tool context, and transcript-parser payloads. Restrict the log and checkpoint
directories to the operator account, secure collector transport and backend
credentials, and set backend retention deliberately. Local rotation controls
disk retention only; it does not delete records already exported. The
`errors.log` aggregate also duplicates `WARNING+` records in local and exported
log volume.

## Dashboard

The dashboard aggregates:

- System health, uptime, memory, CPU, background tasks, and service health.
- Task counts and readiness.
- Session totals by source.
- Token usage by provider/model.
- Memory counts.
- HTTP, MCP, system resource, and latency charts.

Primary routes:

```text
/api/admin/status
/api/admin/stats
/api/admin/usage
/api/admin/tokens/timeseries
/api/metrics/snapshots
```

## Traces

Tracing is implemented through `src/gobby/telemetry/tracing.py`. Code can use
`@traced` or explicit span helpers. When tracing is enabled, Gobby exports spans
through `GobbySpanExporter`, persists them to the hub database through the span
store, and broadcasts trace events to the UI.

Use traces when you need causality across services, such as an agent spawn, a
workflow transition, a provider call, or a scheduled pipeline run.

## Metrics

`src/gobby/mcp_proxy/metrics.py` records MCP tool activity. Tool calls are
triple-written to:

- Hub aggregate metrics.
- A tool event log.
- OpenTelemetry metrics/spans when configured.

The `gobby-metrics` MCP server includes tools for:

- Tool metrics and top tools.
- Failing tools.
- Tool success rates.
- Session tool usage.
- Rule and skill metrics.
- Usage reports.
- Time-series metrics.
- Retention cleanup and retention stats.

## Token Usage And Savings

Token usage is exposed by:

```text
/api/admin/usage
/api/admin/tokens/timeseries
```

Use token data to understand provider/model spend and volume.

## Prometheus And Admin Routes

Prometheus exposition is served at:

```text
/api/admin/metrics
```

Other useful admin routes include:

```text
/api/health
/api/admin/startup-progress
/api/admin/status
```

`/api/admin/status` is the richest single diagnostic endpoint. It reports daemon
process state, background tasks, MCP servers, projects, sessions, tasks, memory,
skills, pipelines, provider models, savings, agents, file descriptors, database
state, and last shutdown information.

When `telemetry.metrics_enabled` and
`telemetry.exporter.prometheus_enabled` are enabled, the endpoint also exposes
the bounded health counters:

- `logging_records_total{surface,severity}` uses surfaces `daemon`, `hooks`,
  `mcp`, `automation`, and `parser`, with severities `WARNING`, `ERROR`, and
  `CRITICAL`. `errors.log` is an aggregate file and is not a metric surface.
- `automation_events_total{component,outcome}` uses `cron` outcomes `fired`,
  `succeeded`, and `failed`; `dispatcher` outcomes `succeeded`, `failed`, and
  `skipped`; and `pipeline-heartbeat` outcomes `recovered` and `failed`.

Useful PromQL queries:

```promql
sum by (surface, severity) (rate(logging_records_total[5m]))
increase(logging_records_total{severity=~"ERROR|CRITICAL"}[15m])
sum by (component, outcome) (rate(automation_events_total[5m]))
increase(automation_events_total{component="pipeline-heartbeat",outcome="failed"}[15m])
```

`automation.log` explains individual scheduler and dispatcher decisions.
Persisted `cron_runs` remains the source of truth for cron run history; use the
`gobby-cron` run-history tools when exact run status and output matter.

## CLI

Use the CLI for operator-level checks:

```bash
uv run gobby status
uv run gobby restart
```

For test runs that exercise daemon behavior, keep tests isolated from the user's
running daemon and always prefix pytest with:

```bash
GOBBY_TEST_PROTECT=1
```

## HTTP

HTTP is the primary observability interface for the Web UI and external
dashboards. Prefer admin routes for daemon state and metrics routes for chart
data. If a browser card is stale, verify the matching HTTP route first, then
inspect the React hook that owns the card.

## MCP

Agents should prefer `gobby-metrics` for metrics and reports, and native servers
for domain state:

- `gobby-tasks` for task status.
- `gobby-cron` for scheduled jobs and run history.
- `gobby-plans` for plan records.
- `gobby-memory` for persisted knowledge.
- `gobby-skills` for skill loading and metrics.

Follow progressive discovery before each new tool family.

## File Locations

- `src/gobby/mcp_proxy/metrics.py`: MCP metrics collection.
- `src/gobby/mcp_proxy/tools/metrics.py`: `gobby-metrics` MCP tools.
- `src/gobby/telemetry/logging.py`: file routing, rotation, and the metrics handler.
- `src/gobby/telemetry/health_metrics.py`: bounded logging and automation counters.
- `src/gobby/telemetry/tracing.py`: `@traced` decorator and span helpers.
- `src/gobby/telemetry/span_store.py`: `GobbySpanExporter` and persisted span storage.
- `src/gobby/servers/routes/admin/_health.py`: health, status, Prometheus.
- `src/gobby/servers/routes/admin/_usage.py`: token usage routes.
- `src/gobby/servers/routes/metrics.py`: dashboard metrics snapshots.
- `web/src/components/dashboard/`: dashboard cards and charts.
- `web/src/components/traces/`: trace UI.

## See Also

- [web-ui.md](web-ui.md)
- [configuration.md](configuration.md#logging-and-telemetry)
- [OpenTelemetry log collector reference](../examples/otel-collector/README.md)
- [cron-scheduler.md](cron-scheduler.md)
- [mcp-tools.md](mcp-tools.md)
- [testing.md](testing.md)

_Last verified: 2026-07-17_
