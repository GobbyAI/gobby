# Metrics and activity

Load when diagnosing tool reliability or latency, examining session activity,
or interpreting rule/skill charts. Discover `gobby-metrics`; fetch the schema
of the report needed for the question. Tool schemas own parameter details.

1. Select scope explicitly. `get_tool_metrics`, `get_top_tools`, and
   `get_failing_tools` accept a project filter; omission can aggregate projects.
   `get_tool_success_rate` requires project, server, and tool identifiers.
2. Start with totals and sample counts. An absent success rate is not a zero
   success rate. `get_top_tools` supports call count, success count, and average
   latency ordering; success count does not rank by success percentage.
3. Use `get_failing_tools` for failure-rate triage, then inspect the relevant
   server and failing operation. Do not infer causes from a ranking alone.
4. Use `get_session_tools` for a session's event-based per-tool breakdown.
   Put the current caller session on the outer proxy call; pass a different
   target session inside tool arguments when investigating that session.
5. Use `get_rule_metrics` for historical blocks and latency. Allow outcomes
   live in process telemetry; this report is not a complete rule evaluation
   ledger. Use `get_skill_metrics` for recorded searches and invocations.
6. Use `get_metrics_timeseries` for time buckets, with an event type, supported
   range, and optional name/session filters. Those filters are not a project
   selector. Preserve the report's bucket/window context when sharing findings.

Aggregates and event history are written separately. Partial recording failures
can make their counts diverge; consult daemon logs before treating a mismatch
as application behavior. Reports requiring an event store return an explicit
error when it is unavailable. Repair the dependency and rerun the same bounded
query. Do not reset counters to make a discrepancy disappear.

Operator HTTP `/api/admin/stats` provides dashboard summaries. Its
task/session/memory project filter does not scope the current metrics-event
summaries, and `unique_tools` counts the top-five rows. Use dedicated reports
for complete tool counts or project-scoped tool analysis.

Load `retention.md` before `reset_metrics`, `reset_tool_metrics`, or
`cleanup_old_metrics`. Configuration and installed enabled state belong to the
rules/skills capabilities; historical usage does not establish current state.

See [metrics](../../../../../../../../docs/guides/observability.md#metrics)
and [Prometheus](../../../../../../../../docs/guides/observability.md#prometheus-and-admin-routes).
