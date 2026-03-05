---
name: build-pipeline
description: "Use when user asks to 'build pipeline', 'create pipeline', 'author pipeline', 'write pipeline', 'design pipeline'. Interactive guide for authoring Gobby pipeline YAML definitions."
version: "1.0.0"
category: authoring
triggers: build pipeline, create pipeline, author pipeline, write pipeline, design pipeline
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby build-pipeline — Pipeline Authoring Skill

Guide users through authoring a Gobby pipeline YAML definition. Asks what the pipeline does, which step types it needs, how data flows between steps, and whether it needs approval gates or webhooks. Generates valid YAML and validates the result.

## Workflow Overview

1. **Gather Requirements** — What does the pipeline do?
2. **Design Steps** — Which step types, what order, what data flows
3. **Generate YAML** — Produce the pipeline definition
4. **Validate** — Check structure, references, and common mistakes
5. **Install** — Import into Gobby

---

## Step 1: Gather Requirements

Ask the user:

1. **"What should this pipeline do?"** — One sentence describing the goal.
2. **"Does it need human approval before any step?"** — Identifies approval gates.
3. **"Should it be callable as an MCP tool?"** — Determines `expose_as_tool`.
4. **"Does it need to notify external services?"** — Identifies webhook needs.

From the answers, determine:
- Pipeline name (kebab-case, e.g., `deploy-staging`)
- Description
- Whether approval gates are needed
- Whether webhooks are needed
- Whether `expose_as_tool: true` is appropriate

---

## Step 2: Design Steps

For each step the pipeline needs, determine the step type:

### Step Type Decision Matrix

| Need | Step Type | When to Use |
|------|-----------|-------------|
| Run a shell command | `exec` | Build, test, deploy, file operations |
| Ask an LLM to reason | `prompt` | Analysis, summarization, code generation |
| Call an MCP tool directly | `mcp` | Task operations, agent spawning, memory queries |
| Run another pipeline | `invoke_pipeline` | Reusable sub-workflows, recursive patterns |
| Activate a step workflow on a session | `activate_workflow` | Setting up agent behavior after spawning |
| Wait for an async process to finish | `wait` | Blocking until a spawned agent completes |

### Common Step Patterns

**Spawn agent and wait:**
```yaml
- id: spawn_worker
  mcp:
    server: gobby-agents
    tool: spawn_agent
    arguments:
      agent: "developer"
      task_id: "${{ inputs.task_id }}"

- id: wait_worker
  wait:
    completion_id: "${{ steps.spawn_worker.output.run_id }}"
    timeout: 600
```

**Conditional step:**
```yaml
- id: deploy_prod
  condition: "${{ inputs.environment == 'production' }}"
  exec: deploy --env production
```

**MCP tool call:**
```yaml
- id: scan_tasks
  mcp:
    server: gobby-tasks
    tool: list_tasks
    arguments:
      parent_task_id: "${{ inputs.epic_id }}"
      status: "open"
```

**Approval gate:**
```yaml
- id: deploy
  exec: deploy-to-prod
  approval:
    required: true
    message: "Approve production deployment?"
```

**LLM analysis:**
```yaml
- id: analyze
  prompt: |
    Analyze the test results: ${{ steps.test.output.stdout }}
    Summarize failures and suggest fixes.
  tools:
    - Read
    - Grep
```

Ask the user to describe each step. For each one:
1. Determine the step type
2. Identify inputs it needs from prior steps
3. Identify what it produces for later steps

---

## Step 3: Generate YAML

### Pipeline Definition Template

```yaml
name: <pipeline-name>
type: pipeline
version: "1.0"
description: <description>

inputs:
  # Parameters with defaults (overridden at runtime)
  param_name: default_value

outputs:
  # Map pipeline outputs from step results
  result: $<step_id>.output

steps:
  - id: <step_id>
    <step_type>: <value>
    # Optional: condition, approval, tools

webhooks:                        # Only if needed
  on_complete:
    url: <webhook_url>

expose_as_tool: false            # Set true if callable via MCP
resume_on_restart: false         # Set true if steps are idempotent
```

### Data Flow Rules

1. **Template expressions** use `${{ }}` syntax in step fields:
   ```yaml
   exec: "deploy --version ${{ steps.build.output.version }}"
   ```

2. **Output references** use `$step.output` in pipeline-level `outputs`:
   ```yaml
   outputs:
     version: $build.output.version
   ```

3. **Available variables in templates:**
   - `inputs.<param>` — Pipeline input parameters
   - `steps.<step_id>.output` — Output from a completed step
   - `steps.<step_id>.output.<field>` — Nested field access
   - `env.<VAR_NAME>` — Environment variables (sensitive values filtered)
   - `session_id` — Pipeline's own session ID
   - `parent_session_id` — Caller's session ID

4. **MCP argument type coercion** — After rendering, string values are auto-coerced:
   - `"true"`/`"false"` → boolean
   - `"null"`/`"none"` → None
   - `"600"` → int, `"3.14"` → float

### Step Field Reference

Each step must have **exactly one** execution type (`exec`, `prompt`, `mcp`, `invoke_pipeline`, `activate_workflow`, `wait`).

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | **Required.** Unique within pipeline |
| `exec` | string | Shell command (no pipes/redirects without `bash -c`) |
| `prompt` | string | LLM prompt template |
| `mcp` | object | `{server, tool, arguments?}` |
| `invoke_pipeline` | string/dict | Pipeline name or `{name, arguments}` |
| `activate_workflow` | object | `{name, session_id, variables?}` |
| `wait` | object | `{completion_id, timeout?}` |
| `condition` | string | Expression; step skipped if false |
| `approval` | object | `{required: true, message?, timeout_seconds?}` |
| `tools` | list | Tool restrictions for `prompt` steps |
| `input` | string | Explicit input reference (Lobster compat) |

---

## Step 4: Validate

After generating the YAML, check for these common mistakes:

### Validation Checklist

1. **Each step has exactly one execution type** — Can't have both `exec` and `mcp` on the same step.

2. **All `${{ }}` references resolve** — Every `steps.<id>.output` reference must point to a step that runs before the referencing step.

3. **No forward references** — Step B can't reference Step C's output if C runs after B.

4. **Step IDs are unique** — No duplicate `id` values.

5. **Conditions use `${{ }}` syntax** — Not bare Jinja2 `{{ }}`.

6. **MCP steps have `server` and `tool`** — Both required.

7. **`wait` steps have `completion_id`** — Required field.

8. **Approval gates are on the right steps** — Only makes sense on steps with side effects (deploy, merge, delete).

9. **Pipeline name is kebab-case** — Convention: `my-pipeline`, not `myPipeline`.

10. **`invoke_pipeline` with arguments uses dict form** — `{name: "...", arguments: {...}}`.

### Report

```
Pipeline Validation:
✓ All steps have exactly one execution type
✓ All template references resolve
✓ No forward references
✓ Step IDs are unique
✓ Conditions use correct syntax
✓ MCP steps have required fields
✓ Pipeline name follows convention

Ready to install.
```

---

## Step 5: Install

Save and import the pipeline:

```python
# Option 1: Save as YAML file and import
Write(".gobby/pipelines/<name>.yaml", yaml_content)
# Then: gobby pipelines import .gobby/pipelines/<name>.yaml

# Option 2: Create directly via MCP
call_tool("gobby-workflows", "create_pipeline", {
    "name": "<pipeline-name>",
    "definition": { ... }  # The pipeline definition dict
})
```

Tell the user:
```
Pipeline created! To run it:

  gobby pipelines run <name>
  gobby pipelines run <name> -i key=value    # With inputs

To check status:
  gobby pipelines status <execution_id>
```

---

## Production Examples

Reference these for patterns:

### Simple: Build-Test-Deploy
```yaml
name: deploy
type: pipeline
description: Build, test, and deploy

steps:
  - id: build
    exec: npm run build

  - id: test
    exec: npm test

  - id: deploy
    exec: deploy-to-prod
    approval:
      required: true
      message: "Approve production deployment?"
```

### Agent Orchestration: Spawn-Wait-Validate
```yaml
name: expand-task
type: pipeline
description: Spawn researcher, wait, validate, execute

inputs:
  task_id: null
  agent: "expander"

steps:
  - id: spawn_researcher
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: "${{ inputs.agent }}"
        task_id: "${{ inputs.task_id }}"

  - id: wait_researcher
    wait:
      completion_id: "${{ steps.spawn_researcher.output.run_id }}"
      timeout: 600

  - id: validate
    mcp:
      server: gobby-tasks
      tool: validate_expansion_spec
      arguments:
        task_id: "${{ inputs.task_id }}"

  - id: check_valid
    condition: "${{ not steps.validate.output.valid }}"
    exec: "echo 'Spec validation failed' && exit 1"

  - id: execute
    mcp:
      server: gobby-tasks
      tool: execute_expansion
      arguments:
        parent_task_id: "${{ inputs.task_id }}"

outputs:
  created: "${{ steps.execute.output.created }}"
```

### Recursive: Loop Until Done
```yaml
name: orchestrator
type: pipeline
description: Loop with iteration counter

inputs:
  session_task: null
  max_iterations: 200
  _current_iteration: 0

steps:
  - id: check_limit
    condition: "${{ inputs._current_iteration >= inputs.max_iterations }}"
    exec: "echo 'Max iterations reached' && exit 1"

  - id: do_work
    mcp:
      server: gobby-tasks
      tool: list_tasks
      arguments:
        parent_task_id: "${{ inputs.session_task }}"
        status: "open"

  - id: next_iteration
    condition: "${{ steps.do_work.output.tasks | length > 0 }}"
    invoke_pipeline:
      name: orchestrator
      arguments:
        session_task: "${{ inputs.session_task }}"
        _current_iteration: "${{ inputs._current_iteration + 1 }}"
```

---

## Key Gotchas

1. **`exec` has no shell features** — No pipes, redirects, or globs. Use `bash -c '...'` if you need them.
2. **Nested pipeline outputs don't propagate** — Downstream steps only see `execution_id` and `status`, not the nested pipeline's step outputs.
3. **Conditions fail-open** — If a condition expression errors, the step **runs** (not skipped). This is intentional.
4. **Sensitive env vars are filtered** — Variables ending in `_SECRET`, `_KEY`, `_TOKEN`, `_PASSWORD` etc. are stripped from `env.*` references.
5. **`wait` timeout defaults to 600s** — Set higher for long-running agents.
6. **Resume after approval replays from start** — Completed steps are auto-skipped, but the replay means all steps must be idempotent if you use `resume_on_restart`.

## See Also

- [Pipelines Guide](docs/guides/pipelines.md) — Full reference
- [Workflows Overview](docs/guides/workflows-overview.md) — How pipelines fit with rules and agents
- [Orchestrator Guide](docs/guides/orchestrator.md) — The orchestrator pipeline pattern
