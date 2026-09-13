# Token usage and savings

Load when examining model token volume, source/model breakdowns, or differences
between usage reports and dashboard charts. Discover `gobby-metrics` and fetch
`get_usage_report` for its day-based report. It accepts a day count, not a
project filter. The configured tracker normally reads token events; its
session-storage fallback has different source data.

1. State the period and scope with the result. Preserve input, output, cache
   creation, and cache read counts separately rather than inventing a combined
   billable total.
2. Inspect source/model groups and session counts. The report does not return
   a priced invoice; token volume alone does not establish provider spend.
3. Use the session capability's usage breakdown when its filters match the
   investigation. Fetch that tool's schema rather than transferring parameters
   from the metrics report.
4. For operator/dashboard checks, `/api/admin/usage` accepts hours and project
   scope; `/api/admin/tokens/timeseries` adds a supported bucket granularity.
   Both accept zero hours for all recorded history. Do not compare differently
   scoped windows as though they were the same report.

An unconfigured usage tracker returns an explicit error. The current admin
usage route instead logs a storage failure and returns empty totals; consult
logs before concluding that an empty dashboard means no activity. Repair the
data source and rerun the bounded report. Do not modify token events to force
agreement between views.

Savings are estimates recorded by the relevant subsystem and are separate from
tokens actually consumed. Report their units and source. Context occupancy
after compaction is also distinct from cumulative input/output/cache usage;
do not add a context snapshot to historical token totals.

For operator ledger diagnosis, `uv run gobby tokens stats --project PROJECT`
reports stored totals and `uv run gobby tokens audit --session SESSION` compares
transcript events, ledger rows, and cached session totals. Prefer one explicit
session. Explicit unknown projects fail before token-ledger access. `--all` is
broader; inspect scope and transcript availability first.
`--fix` replaces events and cached usage transactionally and is an intentional
repair, not a read-only audit. Inspect skipped-session diagnostics and the
audited/drifted/repaired summary. Rehearse repairs with isolated transcripts and
test state; use the admin recovery procedure for live-state restoration.

See [token usage and savings](../../../../../../../../docs/guides/observability.md#token-usage-and-savings).
