# Cross-project queries

Load when finding tasks or recent sessions across projects in the connected
hub. Fetch the applicable `gobby-hub` schema before calling:

- `list_cross_project_tasks({"limit": 50})` returns tasks ordered by most
  recently updated. Optional `state` filters the stored projected state bucket.
  Each result's `state` is a structured lifecycle object, not that filter string.
- `list_cross_project_sessions({"limit": 20})` returns sessions ordered by
  creation time, excluding `source="system"`. It does not mean active sessions
  only and does not filter to the current machine.

Choose a small positive limit for the question. `count` is the number returned,
not a total. These tools expose neither offset nor cursor, so they cannot
provide a guaranteed exhaustive paginated scan. Do not invent a continuation
argument. Narrow follow-up retrieval through the relevant project/task/session
service and inspect its own pagination contract.

Preserve `project_id` and the full object UUID for follow-up. Check session
`machine_id` before any machine-local action. Task discovery does not claim
work, and seeing a claim does not transfer ownership. Use `gobby-tasks` for
task lifecycle and `gobby-sessions` for transcripts/handoffs after loading
their operation guidance.

An empty successful result is not an unavailable database. An unknown state
can simply match no rows; inspect current task-state guidance instead of
guessing state names. These queries do not join the project visibility filter,
so rows from system or soft-deleted projects can appear. Do not infer that
their projects are selectable ordinary checkouts.

For database errors, retain the exact error and restore service health before
retrying. See [hub queries](../../../../../../../../docs/guides/shared-stack.md#hub-queries)
for result scope and operator boundaries.
