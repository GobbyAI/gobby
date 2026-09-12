# Inspect pipeline and cron history

Load to locate execution evidence, review outcomes or delete retained history.
On `gobby-workflows`, `list_pipeline_executions` filters by pipeline/status/session
or parent execution. `search_pipeline_executions` searches names and optionally
errors/outputs. Keep filters fixed and page using `limit`/`offset` through the
filter-scoped total. `include_steps=true` expands details; brief is the list default.

Use `get_pipeline_status(execution_id=...)` for one execution's status, steps,
outputs and errors. Retrieve offloaded content through MCP-result guidance when
needed; previews are not complete evidence. Distinguish pipeline completion from
child dispatch or acceptance, and failed/errored steps from successful effects.

`clear_pipeline_execution_history(pipeline_name=...)` defaults to preview. Review
its selected terminal tree and retained-evidence needs before authorized
`confirm=true`. Any active selected execution or descendant blocks deletion.
Definition deletion is separate and must not stand in for targeted history cleanup.

Operator CLI provides `pipelines runs list`, `runs show`, `history NAME`, and
`search QUERY`, with documented offset filters. HTTP exposes execution listing,
search and detail; do not infer CLI flag parity from MCP parameters.

Cron `list_cron_runs(job_id=..., limit=...)` is bounded recent history without
an offset argument. Its linked agent/pipeline ID is the durable child-evidence
entrypoint. Operator HTTP can inspect a specific cron run. Deleting a cron job
also removes its history. Preserve evidence before authorized deletion.

If nothing is found, verify project, filters and execution ID before broadening
the query. An unavailable/stale notification is not evidence of success; inspect
the stored result. Do not poll history as a replacement for event-driven waits.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#cli-run-history).

_Last verified: 2026-09-12_
