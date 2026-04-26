# Pipelines

Pipelines are Gobby's deterministic automation layer. They execute an ordered
list of steps, track execution state in the database, and optionally pause for
approval or wait on completion events. They are provider- and CLI-agnostic:
the same pipeline can coordinate Claude, Codex, or Gemini workers because it
operates through Gobby's normalized MCP surface.

For the broader system model, see [Workflows Overview](./workflows-overview.md).

## What Pipelines Are For

Pipelines are the right tool when you need:

- ordered, repeatable automation
- typed data flow between steps
- approval gates
- execution records that survive daemon restarts
- orchestration across tasks, agents, worktrees, and nested pipelines

Use an agent when you want open-ended reasoning. Use a pipeline when you want
the control flow itself to be explicit.

## Public Pipeline Tools

Current pipeline execution and definition management live on
`gobby-workflows`.

### Execution

- `run_pipeline`
- `get_pipeline_status`
- `list_pipeline_executions`
- `search_pipeline_executions`
- `wait_for_completion`
- `resume_pipeline`
- `approve_pipeline`
- `reject_pipeline`
- `cancel_pipeline`

### Definition Management

- `list_pipelines`
- `get_pipeline`
- `create_pipeline`
- `update_pipeline`
- `delete_pipeline`
- `export_pipeline`

### Pipeline Helper Tools

- `pipeline_eval`
- `fail_pipeline`

Those helper tools are mainly used inside pipelines themselves.

## Definition Shape

Pipeline definitions are stored as `workflow_type='pipeline'`.

### Top-Level Fields

| Field | Purpose |
| --- | --- |
| `name` | Unique pipeline name |
| `type` | Must be `pipeline` |
| `version` | Optional version string |
| `description` | Human-readable summary |
| `inputs` | Default input values |
| `outputs` | Output expressions built from final execution context |
| `steps` | Ordered list of pipeline steps |
| `webhooks` | Optional notifications for approval, completion, and failure |
| `expose_as_tool` | Register the pipeline as a dynamic tool |
| `resume_on_restart` | Resume eligible executions after daemon restart |

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
        agent: "${{ inputs.reviewer_agent }}"
        prompt: "Review task ${{ inputs.task_id }}"
        isolation: "none"

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
| `exec` | Runs a command |
| `prompt` | Sends a prompt through the configured LLM service |
| `mcp` | Calls a specific MCP tool directly |
| `invoke_pipeline` | Executes another pipeline |
| `activate_workflow` | Activates a step workflow during pipeline execution |
| `wait` | Blocks on a completion event |

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

Runs a shell command. Gobby executes the command directly rather than through a
full interactive shell unless you explicitly invoke one.

Typical output:

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0
}
```

If `stdout` parses as JSON and contains an object, Gobby merges those keys into
the step output as a convenience.

### `prompt`

Runs an LLM step with the current execution context rendered into the prompt.
Use this when you need bounded reasoning inside an otherwise deterministic
pipeline.

### `mcp`

Calls a tool directly:

```yaml
- id: ready_tasks
  mcp:
    server: gobby-tasks
    tool: list_ready_tasks
    arguments:
      parent_task_id: "${{ inputs.task_id }}"
      limit: 5
```

This is the main way pipelines coordinate the rest of Gobby.

### `invoke_pipeline`

Runs another pipeline by name or by `{name, arguments}`. Current Gobby now
surfaces nested pipeline outputs back to the parent step output, so later steps
can reference them.

### `activate_workflow`

This is a **pipeline step type**, not a public `gobby-workflows` MCP tool.
Use it inside a pipeline when the pipeline needs to activate a step workflow on
an existing session.

### `wait`

Blocks on a completion event. The `completion_id` is usually:

- an agent `run_id` returned by `spawn_agent`
- a pipeline `execution_id` returned by `run_pipeline`

This is the pipeline-level counterpart to the public
`gobby-workflows:wait_for_completion` tool.

## Data Flow

Pipeline execution context exposes:

- `inputs`
- `steps.<step_id>.output`
- session and pipeline metadata used by the renderer

### Expressions

Gobby supports Jinja-style expressions using `${{ ... }}` in step fields and
outputs:

```yaml
exec: "pytest ${{ inputs.test_path }}"
```

### Output References

Pipeline outputs can use either:

- `${{ ... }}` expressions
- `$step.output` style references

Example:

```yaml
outputs:
  status: "${{ steps.wait_for_reviewer.output.status }}"
  report: $wait_for_reviewer.output
```

## Execution Lifecycle

Current pipeline execution works like this:

1. `run_pipeline` validates the definition and creates an execution record.
2. The call returns immediately with an `execution_id`.
3. The pipeline runs in the background.
4. Each step is tracked as `pending`, `running`, `completed`, `failed`,
   `waiting_approval`, or `cancelled`.
5. If an approval gate fires, the execution pauses until
   `approve_pipeline` or `reject_pipeline`.
6. On completion, failure, or cancellation, the execution record stores final
   outputs and can notify waiting subscribers.

## Waiting, Approval, And Resume

### Waiting

Use `wait_for_completion` when an external caller needs to block on an agent or
pipeline run:

```python
call_tool("gobby-workflows", "wait_for_completion", {
    "completion_id": execution_id,
    "timeout": 1200
})
```

### Approval

Approval gates are step-level:

```yaml
approval:
  required: true
  message: "Approve merge to main?"
  timeout_seconds: 3600
```

When a step pauses for approval, Gobby stores the token and transitions the
execution to `waiting_approval`.

### Resume

`resume_pipeline` re-runs a failed execution from its failure point, or from an
explicit `from_step`.

## Dispatch Boundary

Pipelines are no longer the main autonomous task-dispatch loop. Current task
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
- enforcing `allow_automation`, `yolo`, isolation, and stage skips
- bounded worker spawning under the global agent-slot cap

Retired orchestration pipelines remain in place only as disabled tombstones so
bundled-template sync preserves installed DB rows. See
[Orchestration](./orchestration.md) for the current dispatch model.

## Related Guides

- [Agents](./agents.md) for worker definitions and spawning
- [Rules](./rules.md) for hook-time enforcement
- [Orchestration](./orchestration.md) for how pipelines drive multi-agent flows
