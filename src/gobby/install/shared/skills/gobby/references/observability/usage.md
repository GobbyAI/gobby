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
5. For per-call rows in one session or one task, call `gobby-metrics:get_usage_ledger`
   with exactly one of `session_id` or `task_id`. The operator route
   `GET /api/admin/usage/ledger` takes the same parameters as UUIDs. Each call returns
   one page of `token_events` rows in `(event_at, session_id, message_key)` order and a
   `next_cursor`. Continue with the same scope and `cursor`. A malformed cursor, or a
   missing or doubled scope, is an error.

The ledger's totals, spend, durations, attribution and coverage cover the whole scope
and are recomputed on every page. Paging is a live traversal: a call appended after
page one raises the totals and can land on a later page. A row sorted before the
cursor, such as a late subagent row, counts in totals but never appears on a later page.
A traversal does not repeat rows. Read `coverage` before any ledger number. Each source
gives `tokens`, `api_calls` and `spend` as a `mode` of `exact`, `reported` or `unknown`,
with a `reason` when unknown. An unknown mode is not a zero. `spend_observed` is unknown
with reason `no reported run` when a source has rows but no provider run.

`spend.reported` sums provider-reported amounts per unit, as strings. A run counts toward
a task only when one of its claim intervals covers the run's span and no other interval
in its session opens inside that span. Any other run that touches the task is listed in
`spend.unattributed` and is never split across tasks. The ledger reads only
`token_events`; rows that retention has moved to `token_event_retention_totals` are not
counted.

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

For one session's or one task's ledger on the operator CLI, `uv run gobby tokens ledger
--session SESSION` (or `--task TASK`, exactly one) prints the same scope totals, per-source
coverage and one spend line per source. Unknown spend prints as `unknown (<reason>)`, never
zero, and incomplete provider runs are named. `--json` prints the whole page, rows included.
`uv run gobby tokens quota [PROVIDER...]` reads the running daemon's capacity snapshot for
each provider (all six when none is named): state, each window's used, limit, used percent
and reset time, and every key of each component. It exits 1 when the daemon is unreachable.

See [token usage and savings](../../../../../../../../docs/guides/observability.md#token-usage-and-savings).

_Version: 1.2.1_
