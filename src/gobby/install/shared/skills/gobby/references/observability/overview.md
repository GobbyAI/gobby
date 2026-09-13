# Observability

Load when investigating tool activity, provider capacity, token usage,
telemetry retention, or daemon diagnostics. `$gobby observability references`
lists topics; menus do not reset metrics, prune data, or restart services.

Discover agent-supported reports on `gobby-metrics` and fetch the required
tool schema before each first use. Read the result's success/error fields;
an unavailable event store or usage tracker is not evidence of zero activity.
Use domain capabilities for task, pipeline, session, and provider operations.

1. Load `metrics.md` for tool, rule, skill, session, and time-series reports.
2. Load `capacity.md` for normalized provider quota snapshots.
3. Load `usage.md` for token accounting and savings.
4. Load `retention.md` before any reset or cleanup operation.
5. Load `diagnostics.md` for health, logs, traces, and telemetry configuration.

Choose the project, session, time window, and data source before interpreting
a report. Persistent aggregates, event history, and process telemetry answer
different questions and have different lifetimes. Inspect first, then make
only the change authorized by the request. Installation, daemon lifecycle,
and external Collector setup are operator procedures.

See the [observability guide](../../../../../../../../docs/guides/observability.md).
