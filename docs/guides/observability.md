# Observability

Gobby observability spans daemon health, MCP tool metrics, traces, token usage,
savings, dashboard charts, Prometheus exposition, and admin diagnostics.

## Mental Model

The daemon records operational data at several layers:

- Logs explain immediate runtime behavior.
- OpenTelemetry spans describe traced operations.
- SQLite-backed metrics aggregate MCP tool calls and event history.
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
http://localhost:60889/#dashboard
http://localhost:60889/#traces
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

## Dashboard

The dashboard aggregates:

- System health, uptime, memory, CPU, background tasks, and service health.
- Task counts and readiness.
- Session totals by source.
- Token usage by provider/model.
- Savings totals and efficiency.
- Memory counts.
- HTTP, MCP, system resource, and latency charts.

Primary routes:

```text
/api/admin/status
/api/admin/stats
/api/admin/usage
/api/admin/savings
/api/admin/tokens/timeseries
/api/metrics/snapshots
```

## Traces

Tracing is implemented through `src/gobby/telemetry/tracing.py`. Code can use
`@traced` or explicit span helpers. When tracing is enabled, Gobby exports spans
through `GobbySpanExporter`, persists them to SQLite through the span store, and
broadcasts trace events to the UI.

Use traces when you need causality across services, such as an agent spawn, a
workflow transition, a provider call, or a scheduled pipeline run.

## Metrics

`src/gobby/mcp_proxy/metrics.py` records MCP tool activity. Tool calls are
triple-written to:

- SQLite aggregate metrics.
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

Savings are exposed by:

```text
/api/admin/savings
/api/admin/savings/cumulative
/api/admin/savings/record
```

Use token data to understand provider/model spend and volume. Use savings data to
understand how much code index, compression, discovery, or other systems avoided
sending to a model.

## Prometheus And Admin Routes

Prometheus exposition is served at:

```text
/api/admin/metrics
```

Other useful admin routes include:

```text
/api/admin/health
/api/admin/startup-progress
/api/admin/status
/api/admin/setup-state
```

`/api/admin/status` is the richest single diagnostic endpoint. It reports daemon
process state, background tasks, MCP servers, projects, sessions, tasks, memory,
skills, pipelines, provider models, savings, agents, file descriptors, database
state, and last shutdown information.

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
- `src/gobby/telemetry/tracing.py`: tracing helpers and exporter.
- `src/gobby/telemetry/span_store.py`: persisted span storage.
- `src/gobby/servers/routes/admin/_health.py`: health, status, Prometheus.
- `src/gobby/servers/routes/admin/_usage.py`: token usage routes.
- `src/gobby/servers/routes/admin/_savings.py`: savings routes.
- `src/gobby/servers/routes/metrics.py`: dashboard metrics snapshots.
- `web/src/components/dashboard/`: dashboard cards and charts.
- `web/src/components/traces/`: trace UI.

## See Also

- [web-ui.md](web-ui.md)
- [cron-scheduler.md](cron-scheduler.md)
- [mcp-tools.md](mcp-tools.md)
- [testing.md](testing.md)

_Last verified: 2026-05-08_
