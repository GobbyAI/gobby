# Retention and resets

Load before inspecting retention or invoking any metrics reset/cleanup. Fetch
the relevant `gobby-metrics` schema. Start with `get_retention_stats`; its age
and count fields describe aggregate tool metrics, not every telemetry table.

For an authorized reset, establish the calling project and the exact server/tool
scope. `reset_metrics` requires at least one server or tool filter.
`reset_tool_metrics` accepts optional filters; omitting both resets the calling
project's tool metrics. Always provide the intended filters for a specific tool.
Both require project context. Neither accepts an arbitrary target project as a
tool parameter.

Resets atomically remove matching aggregate rows, daily summaries, and
`tool_call` event rows. They do not erase rule/skill events or reset process
OpenTelemetry instruments. The returned deletion count counts aggregate rows;
zero does not prove no daily/event rows were deleted. Record the scope and
reason before resetting; there is no undo tool. Recovery requires an applicable
backup and the operator recovery procedure.

`cleanup_old_metrics` is a different operation: its retention interval defaults
to seven days and must be at least one day. It applies across projects, using
aggregate rows' last-call timestamps. It atomically rolls old aggregates into
daily summaries before deleting them. It is not a per-event TTL, a project-only
reset, or a purge of all telemetry history. Inspect scope and obtain the
authority appropriate to that cross-project change before invoking it.

The daemon also runs aggregate cleanup at startup. Separate loops handle span
retention and dashboard snapshots; the snapshot loop normally captures every
60 seconds and keeps 24 hours of snapshots. Do not infer active settings from
these defaults. Inspect active configuration and background-task health.

If cleanup fails, preserve the error and inspect database/daemon health before
retrying. Rehearse mutating examples with isolated fixtures or a temporary
daemon pointed at the test hub. Never use the user's live state as a test.

See the [observability guide](../../../../../../../../docs/guides/observability.md).
