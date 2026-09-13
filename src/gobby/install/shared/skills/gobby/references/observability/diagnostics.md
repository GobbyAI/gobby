# Diagnostics

Load when health, charts, logs, traces, or metrics disagree or fail. Start with
the smallest read-only observation: `gobby-metrics` for agent reports, domain
tools for their state, or the operator command `uv run gobby status` for daemon
health. A diagnostic request does not imply permission to restart the daemon.

For HTTP checks, use the configured host/port. `/api/health` and
`/api/admin/startup-progress` are public health surfaces. Admin status, metrics,
usage, and trace routes require authentication; use the configured local CLI
token or authenticated browser/client. Treat 401 as an authentication problem
and shutdown-related 503 as an admission problem before debugging chart data.

1. Inspect `/api/admin/status` for process, services, database, and background
   tasks. Preserve degraded/partial evidence instead of reporting only uptime.
2. Compare `/api/metrics/current` with `/api/metrics/snapshots` for point-in-time
   versus stored chart data. Snapshot history is bounded; old or missing
   snapshots can reflect retention or a failed collection loop.
3. Use `/api/admin/metrics` for Prometheus exposition. Verify active telemetry
   settings and exporter configuration before interpreting absent instruments.
4. Inspect the matching log surface under active `logging.dir`: `daemon.log`,
   `hooks.log`, `llm.log`, `mcp.log`, `automation.log`, or parser diagnostics.
   `errors.log` aggregates warnings and errors; duplicates across primary and
   aggregate files are expected. `runtime.log` captures launcher stdout/stderr.
5. Capture timestamps, the failing operation, scope, and relevant bounded logs.
   Redact credentials before sharing evidence. Repair the responsible subsystem,
   then repeat the original observation.

The operator command `gobby observations list` inspects unmodeled observations
sorted by count. Filter with `--source` or `--kind`, bound output with `--limit`
(default 50), and use `--json` for structured output. Preserve the reported count
semantics when interpreting results; these are not a replacement for traces or
per-operation metrics.

Current code still stores local spans, serves `/api/traces`, and can broadcast
trace events. The Activity panel deliberately hides the Traces tab; do not send
users to `#traces`. List/detail APIs are the current inspection surface. A
session filter takes precedence over project/status in the list route; use
separate queries when investigating those scopes. A missing trace can reflect
sampling, disabled collection, retention, or an unknown ID.

Use the config capability before changing telemetry settings. Current source
uses `telemetry.exporter` for optional OTLP export; the standard bootstrap-only
OTEL migration is separate paused work (#18996). Do not present that planned
configuration as already supported. Provider/exporter initialization can need
a coordinated daemon restart; consult the config response and admin guidance.

External Collector `filelog` setup and service lifecycle are operator-owned.
Local rotation does not delete exported logs. `runtime_max_size_mb` is a health
threshold, not automatic truncation. Load `retention.md` before cleanup and
use isolated test state for any mutating diagnostic rehearsal.

See [logs](../../../../../../../../docs/guides/observability.md#log-files),
[traces](../../../../../../../../docs/guides/observability.md#traces), and
[Collector setup](../../../../../../../../docs/guides/observability.md#collect-logs-with-opentelemetry).
