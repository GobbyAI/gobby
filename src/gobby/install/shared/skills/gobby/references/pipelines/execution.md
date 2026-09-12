# Run and compose pipelines

Load before starting a pipeline or choosing step execution behavior. Inspect
`get_pipeline`/`export_pipeline` and current enabled state. Use
`gobby-workflows:run_pipeline(name=..., inputs=..., continuation_prompt=...)` with
caller session context; project/executor resolution comes from that context.

An enabled definition with `expose_as_tool` can register `pipeline:<name>`
when the registry is built. Discover its actual schema; it shares the run path.
Do not assume changing the flag instantly registers a new tool.

Retain `execution_id`. The call returns immediately and subscribes the caller and
lineage to completion. Yield the turn; inspect `get_pipeline_status` after the
notification or for bounded diagnosis. Starting a second run is not a wait.

- `exec`: parsed with `shlex.split`, executed without an implicit shell. Uses
  daemon working directory; a template `project_path` does not change it. Use
  absolute paths or an explicit working-directory command. Exec timeout defaults
  to 300 seconds; step `timeout_seconds` accepts a positive value or expression.
- `prompt`: one configured LLM feature call. Accepted `tools` metadata does not
  turn it into an agent tool loop; use an agent when that behavior is needed.
- `mcp`: explicit server/tool/arguments; the executor prefetches the target schema.
  Rendered null arguments are omitted. Authorization remains operation-specific.
- `invoke_pipeline`: inline child execution; explicit arguments replace inherited
  parent inputs. Inspect child output/status; failed children fail the parent step.
  Nesting obeys configured depth and cycle checks.
- `wait`: completion ID and timeout (default 600 seconds). This blocks within the
  pipeline; validate the delivered status before dependent side effects.

Use MCP `run_pipeline` followed by `wait` for a separately controlled child
approval workflow. Spawned agents still terminate through `end_agent_run`.
Input declarations provide defaults, not runtime type enforcement. Nonzero exec
exit codes and error outputs fail steps; JSON object stdout is merged into output.

On start failure, inspect scope, enabled state and executor availability. On a
wait timeout, inspect the child before retrying. Operator CLI run accepts string
`-i key=value` inputs and can fall back locally without MCP access; agents use MCP.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#step-details).

_Last verified: 2026-09-12_
