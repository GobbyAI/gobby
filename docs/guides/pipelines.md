# Pipelines

Pipelines are Gobby's deterministic automation layer. They execute ordered
steps, persist execution state in the database, and can pause for approval or
wait on completion events. A pipeline is useful when the control flow should be
explicit and repeatable.

For the broader system model, see [Workflows Overview](./workflows-overview.md).
For automated task dispatch, see [Orchestration](./orchestration.md).

## What Pipelines Are For

Use a pipeline when you need:

- ordered, repeatable automation
- typed data flow between steps
- approval gates
- execution records that survive daemon restarts
- nested runs with explicit completion waiting
- standalone maintenance, merge, or expansion helpers

Use an agent when you need open-ended reasoning. Use dispatch when you need to
advance task lifecycle stages. Use a pipeline when the sequence itself is the
contract.

## Public Pipeline Tools

Pipeline execution and definition management live on `gobby-workflows`.

### Execution

- `run_pipeline`
- `get_pipeline_status`
- `resume_pipeline`
- `approve_pipeline`
- `reject_pipeline`
- `cancel_pipeline`

`run_pipeline` takes `name`, optional `inputs`, and optional
`continuation_prompt`. It returns immediately with an `execution_id`; the
pipeline continues in the background and completion subscribers are notified
when it finishes.

### Definition Management

- `list_pipelines`
- `get_pipeline`
- `create_pipeline`
- `update_pipeline`
- `delete_pipeline`
- `export_pipeline`

Definition tools store pipeline YAML in `pipeline_definitions`. Validate a
definition without executing it with `evaluate_pipeline`. This performs loading/model
and loader-reference checks; it does not execute steps or prove tool availability,
credentials, input types, or successful side effects.

### Pipeline Helper Tools

- `pipeline_eval`
- `fail_pipeline`

These helper tools are mainly for use inside pipeline `mcp` steps.

### CLI Run History

Agents use `list_pipeline_executions` and `search_pipeline_executions` on
`gobby-workflows`. Both support offset pagination and filter-scoped totals;
keep filters fixed and advance by returned rows until the total is exhausted.
`include_steps=true` expands step details; search can include outputs explicitly.
`get_pipeline_status` retrieves one execution with its step outputs and errors.

`clear_pipeline_execution_history(pipeline_name=...)` previews deletion by default.
Only pass `confirm=true` for authorized deletion after reviewing the preview.
Active selected executions or descendants block deletion. History deletion is
separate from deleting a definition.

Operators use the CLI run-history surface:

```bash
gobby pipelines runs list [--status STATUS] [--name NAME] [--limit N] [--offset N] [--json]
gobby pipelines runs show RUN [--json]
gobby pipelines history NAME [--limit N] [--offset N] [--json]
gobby pipelines search QUERY [--status STATUS] [--no-errors] [--limit N] [--offset N] [--json]
```

Use `gobby pipelines runs show RUN` to inspect a specific execution.
Use `get_pipeline_status` when an MCP caller already has a specific
`execution_id`.

## Definition Shape

Pipeline definitions are YAML workflow definitions with `type: pipeline`.

### Top-Level Fields

| Field | Purpose |
| --- | --- |
| `name` | Unique pipeline name |
| `type` | Must be `pipeline` |
| `version` | Version string; numeric YAML values are coerced to strings |
| `description` | Human-readable summary |
| `enabled` | Whether the definition is active |
| `priority` | Definition priority for discovery and ordering |
| `inputs` | Default input values or input metadata |
| `outputs` | Output expressions built from final execution context |
| `steps` | Required ordered list of pipeline steps |
| `webhooks` | Optional notifications for approval, completion, and failure |
| `expose_as_tool` | Register the pipeline as a dynamic MCP tool named `pipeline:<name>` |
| `resume_on_restart` | Re-queue a running execution after daemon restart |

## Minimal Example

```yaml
name: review-loop
type: pipeline
description: Spawn a reviewer and wait for completion

inputs:
  task_id: null
  reviewer_agent: "qa-reviewer"

steps:
  - id: spawn_reviewer
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        prompt: "Review task ${{ inputs.task_id }}"
        agent: "${{ inputs.reviewer_agent }}"
        task_id: "${{ inputs.task_id }}"
        isolation: "none"
        parent_session_id: "${{ session_id }}"

  - id: wait_for_reviewer
    wait:
      completion_id: "${{ steps.spawn_reviewer.output.run_id }}"
      timeout: 1200

outputs:
  reviewer_status: "${{ steps.wait_for_reviewer.output.status }}"
  reviewer_result: "${{ steps.wait_for_reviewer.output.result }}"
```

## Step Types

Each step must declare exactly one execution type.

| Type | What it does |
| --- | --- |
| `exec` | Runs a command via `asyncio.create_subprocess_exec` |
| `prompt` | Sends a rendered prompt through the configured LLM service |
| `mcp` | Calls a specific MCP tool directly |
| `invoke_pipeline` | Executes another pipeline |
| `wait` | Blocks on a completion event |

The definition model rejects the removed `activate_workflow` field.

### Step Fields

| Field | Purpose |
| --- | --- |
| `id` | Unique step identifier |
| `condition` | Optional expression; false skips the step |
| `approval` | Optional approval gate before execution |
| `tools` | Accepted metadata; the current prompt handler makes one LLM feature call, without a tool-running agent loop |
| `input` | Accepted reference metadata; use rendered arguments for operational data flow |
| `timeout_seconds` | Positive exec timeout or a full template expression; default 300 seconds |

## Step Details

### `exec`

Runs a command. Gobby parses the command with `shlex.split` and executes it
directly, so shell features require explicitly invoking a shell. It inherits the
daemon process working directory; `project_path` in the template context does
not change that directory. Use absolute paths or an explicit command that sets
its working directory. Never interpolate untrusted text into shell syntax.

```yaml
- id: check_status
  exec: "git status --short"
```

Typical output:

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0
}
```

If `stdout` parses as a JSON object, Gobby merges those keys into the step
output. A non-zero `exit_code` fails the step and the pipeline execution.

### `prompt`

Runs an LLM step with the current execution context rendered into the prompt.
Use this for bounded reasoning inside an otherwise deterministic sequence.

```yaml
- id: summarize
  prompt: "Summarize the result: ${{ steps.check_status.output.stdout }}"
```

### `mcp`

Calls a tool through the MCP proxy:

```yaml
- id: ready_tasks
  mcp:
    server: gobby-tasks
    tool: list_ready_tasks
    arguments:
      parent_task_id: "${{ inputs.task_id }}"
      limit: 5
```

Pipeline MCP steps prefetch the target tool schema before calling the tool so
they satisfy progressive discovery rules. The step output is the tool result
with redundant `success` fields stripped.

### `invoke_pipeline`

Runs another pipeline by name or with explicit arguments:

```yaml
- id: expand
  invoke_pipeline:
    name: "child-check"
    arguments:
      value: "${{ inputs.value }}"
```

Register `child-check` before using this example. Nested execution is awaited
inline; omitted `arguments` inherit parent inputs. Output includes the child
`execution_id`, `status`, and parsed child `output`. Child failures fail the parent
step. Nesting is bounded by configured depth and cross-pipeline cycle checks.
For an independently controlled child approval workflow, use `run_pipeline` in
an MCP step followed by a `wait` step and inspect that child execution.

### `wait`

Blocks on a completion event:

```yaml
- id: wait_run
  wait:
    completion_id: "${{ steps.start_run.output.run_id }}"
    timeout: 600
```

The `completion_id` is usually an agent `run_id`, an expansion `run_id`, or a
pipeline `execution_id`. `timeout` defaults to 600 seconds when omitted or
invalid.

## Data Flow

Pipeline execution context exposes:

- `inputs`
- `steps.<step_id>.output`
- flattened step aliases such as `<step_id>.output`
- `session_id`
- `parent_session_id`
- `project_id`
- `project_path`
- `current_branch`
- filtered `env`

### Expressions

Gobby supports `${{ ... }}` expressions in step fields and outputs:

```yaml
exec: "uv run pytest ${{ inputs.test_path }}"
```

Pure expressions are evaluated as native values where possible, so rendered
numbers and booleans can stay typed for MCP arguments. Rendered null MCP arguments
are omitted. Input metadata supplies defaults; it is not runtime type validation.
Explicit inputs override defaults. Skipped steps expose `output: null`; guard
downstream access. A completion event can report failed work, so check its status
before performing dependent side effects.

### Output References

Pipeline outputs can use either expression syntax or `$step.output` references:

```yaml
outputs:
  status: "${{ steps.wait_for_reviewer.output.status }}"
  report: $wait_for_reviewer.output
```

References must point backward; pipeline outputs may reference any step in the
same definition. Loader checks cover prompt, condition, input, exec, and string
outputs. Review nested MCP/wait arguments separately: structural validation does
not exhaustively check every embedded expression.

## Execution Lifecycle

Current pipeline execution works like this:

1. `run_pipeline` validates and loads the definition.
2. Gobby creates a `pipeline_executions` row and returns an `execution_id`.
3. The pipeline runs in a background task.
4. Each step is tracked as `pending`, `running`, `completed`, `failed`,
   `waiting_approval`, `skipped`, or `cancelled`.
5. If an approval gate fires, the execution pauses until `approve_pipeline` or
   `reject_pipeline` receives the approval token.
6. On completion, failure, cancellation, or interruption, the execution record
   stores final status and outputs where available.

Execution statuses are `pending`, `running`, `waiting_approval`, `completed`,
`failed`, `cancelled`, and `interrupted`.

## Waiting, Approval, And Resume

### Waiting

Pipeline `wait` steps block inside a pipeline. MCP `run_pipeline` and
`resume_pipeline` subscribe the caller and its session lineage to completion;
keep the execution ID and yield the turn. Use `get_pipeline_status` to inspect
the delivered result or diagnose a specific run, not in a polling loop. For an
agent run, use `gobby-agents:wait_for_agent` with its run ID. A wait timing out
does not establish that the child stopped; inspect the child before retrying.

### Approval

Approval gates are step-level:

```yaml
approval:
  required: true
  message: "Approve merge?"
  timeout_seconds: 3600
```

When a gate fires, Gobby stores an approval token and marks the execution
`waiting_approval`. Approve or reject the token through:

```python
call_tool("gobby-workflows", "approve_pipeline", {
    "token": token,
    "approved_by": "operator"
})
```

```python
call_tool("gobby-workflows", "reject_pipeline", {
    "token": token,
    "rejected_by": "operator"
})
```

Approval tokens are single-use and project-scoped by the execution manager.
Approval consumes the token and executes the gated step, then continues until
completion or another approval. The captured definition is used when available;
editing the installed definition does not rewrite an already paused run.
Rejecting marks the gate failed and the execution cancelled. Configured approval
timeouts are enforced by daemon maintenance (normally a 60-second sweep), with
the same failed-step/cancelled-execution outcome. Inspect a stale token's run;
do not retry the token or manufacture an approval identity.

### Resume

`resume_pipeline` only resumes executions whose status is `failed`. Without an
explicit `from_step`, Gobby resets from the first failed or errored step and
re-runs from there. It uses stored inputs and the currently loaded enabled
definition. Inspect changes to that definition and already performed side effects
before resuming. A concurrent resume loses the atomic claim and must inspect the
existing run rather than starting a duplicate.

`resume_on_restart: true` is separate. On daemon startup, Gobby re-queues
running executions for definitions that opt in. Running executions for
definitions without that flag are marked stale and surfaced to subscribers as
interrupted. Neither `interrupted` nor `cancelled` is eligible for public
`resume_pipeline`; reconcile side effects before authorizing a fresh run.
Native Ask owns its separate recovery state machine; use its Ask operations.

`cancel_pipeline(execution_id=...)` cancels the registered background task and
attempts to terminate agents owned by the pipeline child session. It does not
roll back completed effects. Inspect remaining children, external commands and
step output before declaring cleanup complete.

## Installation And Operator Boundaries

Runtime loading reads `pipeline_definitions` in PostgreSQL. Files under
`.gobby/workflows/pipelines/`, `~/.gobby/workflows/pipelines/`, and the bundled
`src/gobby/install/shared/workflows/pipelines/` are authoring/sync inputs, not
proof of installed or enabled state. Inspect `get_pipeline` before running;
use `export_pipeline` for complete YAML because `get_pipeline` summarizes only
some step fields. `list_pipelines` is discovery, not a complete activation audit.

Agent creation uses `create_pipeline(yaml_content=..., project_id=...)` with an
explicit intended project UUID. Omitted project scope creates a global definition.
Updates/export/deletion accept `definition_id`; use it for project definitions.
Their name-only resolver selects the global row; runtime loading selects the
project row before global, including a disabled project override.
Full YAML updates validate the model; explicit update fields override YAML fields.
Existing names require update, and bundled modifications belong in a custom copy.
Deletion is soft; the bundled delete guard requires `force=true`. Ordinary sync
preserves explicit enabled pins; unpinned bundled state follows its template.
Do not treat forced deletion as a permanent replacement for removing the source.
MCP create/update do not automatically write a project YAML file: export it
explicitly when the deliverable needs a file. After authorized source changes,
`reload_cache(project_path=..., project_id=...)` imports workflow files and syncs
bundled rules, agents, pipelines, variables and detection manifests before clearing
cache. This mutates installed state beyond one pipeline: inspect all returned
sync errors/counts and coordinate shared-state changes; success alone does not
mean every import succeeded. Project imports update only their own scope; a
same-named global definition remains unchanged. With no project path, imported sync scans all
locally checked-out projects; an explicit path requires its project UUID.

Operator `gobby pipelines import PATH [-o OUTPUT]` converts the supported external
format to a YAML file (default `.gobby/workflows/NAME.yaml`); it does not register
that file in the runtime database. Inspect the conversion and install validated
YAML through the definition API. It is not a database restore operation.
The HTTP definition API also supports templates, duplication, scope moves,
soft-delete restoration, and restoration of the bundled definition body; see
[HTTP endpoints](./http-endpoints.md#pipeline-definitions).

Operator `gobby pipelines run NAME -i key=value` tries the daemon first and can
fall back to an executor without MCP access when it is unavailable. CLI inputs
are strings. An HTTP read timeout means the daemon run may still be active;
inspect history rather than launching again. Returned failed, cancelled, or
interrupted statuses exit nonzero in text and JSON output. Approval wait is not
successful completion. Agents use MCP execution and event-driven completion.

Enabled installed definitions marked `expose_as_tool` can register a dynamic
`pipeline:<name>` tool when the workflow registry is built. Discover and lease
its actual schema; do not assume editing the flag instantly registers a tool.
The dynamic tool uses the same project-aware run and completion path.

## Dispatch Boundary

Pipelines are not the primary autonomous task-dispatch loop. Current task
automation starts with `gobby build` and continues through deterministic
dispatch rules in `src/gobby/dispatch/rules.py`.

Use pipelines for:

- deterministic multi-step sequences
- approval gates
- nested runs with explicit completion waiting
- standalone maintenance jobs
- reusable merge or expansion helpers

Use dispatch for:

- scanning opted-in tasks
- lifecycle-stage advancement
- enforcing `allow_automation`, unattended policy, isolation, and the resolved stage
  manifest
- bounded worker spawning under the global agent-slot cap

Hook and workflow rules are authored against semantic workflow events such as
`turn_start` and `turn_end`; dispatch rules are deterministic manifest-state
rules evaluated on heartbeat ticks. Provider/runtime hook names are
compatibility details, while agent termination remains a separate lifecycle
step through `gobby-agents:end_agent_run`.

## Related Guides

- [Agents](./agents.md) for worker definitions and spawning
- [Rules](./rules.md) for hook-time enforcement
- [Orchestration](./orchestration.md) for stage-manifest task automation
- [Workflows Overview](./workflows-overview.md) for the complete workflow model
- [MCP Tools](./mcp-tools.md) for current server and tool signatures

_Last verified: 2026-09-12_
