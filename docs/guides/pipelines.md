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

Definition tools store pipeline YAML as workflow definitions with
`workflow_type='pipeline'`.

### Pipeline Helper Tools

- `pipeline_eval`
- `fail_pipeline`

These helper tools are mainly for use inside pipeline `mcp` steps.

### CLI Run History

Use the CLI run-history surface for broad execution lookup:

```bash
gobby pipelines runs list [--status STATUS] [--name NAME] [--limit N] [--offset N] [--json]
gobby pipelines runs show RUN [--json]
gobby pipelines history NAME [--limit N] [--offset N] [--json]
gobby pipelines search QUERY [--status STATUS] [--no-errors] [--limit N] [--offset N] [--json]
```

`gobby pipelines status RUN` is an alias for `gobby pipelines runs show RUN`.
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

The definition model still accepts `activate_workflow`, but current pipeline
execution reports it as unsupported. Do not author new pipelines with that
field.

### Step Fields

| Field | Purpose |
| --- | --- |
| `id` | Unique step identifier |
| `condition` | Optional expression; false skips the step |
| `approval` | Optional approval gate before execution |
| `tools` | Tool restrictions for `prompt` steps |
| `input` | Explicit input reference for compatibility cases |

## Step Details

### `exec`

Runs a command. Gobby parses the command with `shlex.split` and executes it
directly, so shell features require explicitly invoking a shell.

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
    name: "expand-task"
    arguments:
      task_id: "${{ inputs.task_id }}"
      wait_timeout: 600
```

Nested pipeline output includes the child `execution_id`, `status`, and parsed
child `output` when the child pipeline produced JSON outputs.

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
numbers and booleans can stay typed for MCP arguments.

### Output References

Pipeline outputs can use either expression syntax or `$step.output` references:

```yaml
outputs:
  status: "${{ steps.wait_for_reviewer.output.status }}"
  report: $wait_for_reviewer.output
```

Step references are validated on load. A step may reference earlier steps, and
pipeline outputs may reference any step in the same definition.

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

Pipeline `wait` steps block inside a pipeline. External callers do not use a
separate public wait tool; they keep the returned ID and inspect the relevant
status surface, such as `get_pipeline_status`, `gobby-agents:get_agent_result`,
or the run-specific task/expansion tool.

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

### Resume

`resume_pipeline` only resumes executions whose status is `failed`. Without an
explicit `from_step`, Gobby resets from the first failed or errored step and
re-runs from there.

`resume_on_restart: true` is separate. On daemon startup, Gobby re-queues
running executions for definitions that opt in. Running executions for
definitions without that flag are marked stale and surfaced to subscribers as
interrupted.

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
- enforcing `allow_automation`, `yolo`, isolation, and the resolved stage
  manifest
- bounded worker spawning under the global agent-slot cap

Dispatch rules are authored against semantic workflow events such as
`turn_start` and `turn_end`. Provider/runtime hook names are compatibility
details, while agent termination remains a separate lifecycle step through
`gobby-agents:end_agent_run`.

## Related Guides

- [Agents](./agents.md) for worker definitions and spawning
- [Rules](./rules.md) for hook-time enforcement
- [Orchestration](./orchestration.md) for stage-manifest task automation
- [Workflows Overview](./workflows-overview.md) for the complete workflow model
- [MCP Tools](./mcp-tools.md) for current server and tool signatures

_Last verified: 2026-05-07_
