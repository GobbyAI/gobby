# Validate pipeline behavior

Load before installing or executing new/changed automation. Discover the target
with `get_pipeline`, and use `export_pipeline` to inspect its complete definition.

Parse YAML as a mapping and validate `PipelineDefinition`. Exactly one of `exec`,
`prompt`, `mcp`, `invoke_pipeline`, or `wait` belongs on each step. The removed
`activate_workflow` field is rejected. Require unique IDs and backward references;
outputs can reference any step. Quote ambiguous YAML scalars intentionally.

`evaluate_pipeline(name=...)` on `gobby-workflows`, or operator
`gobby pipelines check NAME --json`, checks loading/model/loader constraints
without executing. Current evaluation skips agent-style step semantic checks.
Loader reference checks cover prompt, condition, input, exec and string outputs;
inspect nested MCP/wait arguments separately. A valid result does not prove
external tools, credentials, input types, commands or side effects will succeed.

Use isolated test hub/temporary daemon fixtures to create, run and inspect a
harmless example. Exercise conditions, output types, failure, approval and resume
where relevant. Fetch actual target-tool schemas for MCP examples. Never use the
user's live jobs to test a new schedule. The full pytest suite needs an explicit
request; select the relevant pipeline, scheduler, API and CLI test files.

Check that input defaults and explicit arguments have intended types. Skipped
outputs are null; guard access. Check completion status before using child output.
`pipeline_eval(data=...)` shallowly normalizes boolean/numeric strings; it does not
execute arbitrary code. `fail_pipeline(message=...)` returns an error that fails
its pipeline step. Neither helper replaces validation of the next operation.

On validation failure, fix the definition and repeat the check. On unavailable
integration, repair setup or use an isolated service; do not treat unexecuted
checks as successful workflow evidence.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#data-flow).

_Last verified: 2026-09-12_
