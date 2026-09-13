# Interpret hub aggregates

Load before reporting hub totals or comparing them with project listings.
Fetch `gobby-hub:hub_stats`, then call with `{}`. Read `success` before `stats`.

| Field | Actual scope |
| --- | --- |
| `project_count` | Distinct non-null project IDs appearing in tasks or non-system sessions; not all initialized projects. |
| `tasks.total`, `tasks.by_state` | All task rows, grouped by stored projected state bucket. |
| `sessions.total`, `sessions.by_status` | All non-system-source sessions, grouped by status. |
| `memories` | Count of memory rows; a memory-query error is also reported as zero. |

An initialized empty project appears in `list_all_projects` but not this
`project_count`. Hub aggregates do not use that listing's system-name or
soft-delete filters. Its per-project session counts also include system-source
sessions, unlike `hub_stats`.

The HTTP project API reports live-session counts and open-task counts, which
are different again. These are separate queries, not a single transactionally
consistent snapshot of activity. Explain the scope and time of a comparison
instead of treating differing totals as corruption.

If memory count is unexpectedly zero, use memory statistics and service
diagnostics to distinguish an empty store from the swallowed query error.
Other query failures return `success=false`; report the error, not zero totals.
For capacity, token usage, and performance use the observability capability;
hub row counts do not measure those quantities.

See [hub queries](../../../../../../../../docs/guides/shared-stack.md#hub-queries)
and [project HTTP contract](../../../../../../../../docs/guides/http-endpoints.md#project-identity-and-checkouts).
