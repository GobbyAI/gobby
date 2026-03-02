# Pipelines

Pipelines provide **deterministic, sequential execution** with typed data flow between steps. They run to completion or pause at approval gates — no event-driven state machine, no interactive agent guidance. Think CI/CD, not chatbot orchestration.

## When to Use Pipelines

| Use Case | System |
|----------|--------|
| Deterministic automation (CI/CD, deploy, data processing) | **Pipeline** |
| Interactive agent guidance with tool restrictions | Step Workflow |
| Stateless event-driven enforcement (block tools, inject context) | Rule |

### Pipeline vs Step Workflow vs Rule

| Feature | Pipeline | Step Workflow | Rule |
|---------|----------|---------------|------|
| Execution model | Sequential, runs to completion | Event-driven state machine | Single-pass event handler |
| Data flow | Explicit `$step.output` references | Workflow variables | `set_variable` effect |
| Approval gates | Built-in with resume tokens | Custom via actions | N/A |
| State persistence | DB records (survives restarts) | Workflow instances | Stateless |
| Typical use | Automation, orchestration | Agent guidance | Enforcement, blocking |

## Quick Start

Create a pipeline YAML:

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

Install and run:

```bash
gobby pipelines import deploy.yaml
gobby pipelines run deploy
```

## YAML Schema

### Pipeline Definition

```yaml
name: string              # Required. Pipeline name (unique)
type: pipeline            # Required. Must be "pipeline"
version: string           # Optional. Default: "1.0"
description: string       # Optional

inputs:                   # Optional. Default input values (overridden at runtime)
  timeout: 300
  environment: staging

outputs:                  # Optional. Output mapping using $step.output references
  result: $deploy.output
  version: $build.output.version

steps:                    # Required. At least one step
  - id: step1
    exec: echo hello

webhooks:                 # Optional. Event notifications
  on_approval_pending: ...
  on_complete: ...
  on_failure: ...

expose_as_tool: false     # Optional. Register as dynamic MCP tool
```

**Source**: `src/gobby/workflows/definitions.py` — `PipelineDefinition` (line 464)

### Step Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | **Required.** Unique identifier within the pipeline |
| `exec` | string | Shell command (mutually exclusive with other types) |
| `prompt` | string | LLM prompt template |
| `invoke_pipeline` | string \| dict | Nested pipeline name or `{name, arguments}` |
| `mcp` | object | MCP tool call: `{server, tool, arguments?}` |
| `activate_workflow` | object | Activate workflow: `{name, session_id, variables?}` |
| `condition` | string | Expression; step skipped if false |
| `input` | string | Explicit input reference (Lobster compat) |
| `approval` | object | Approval gate: `{required, message?, timeout_seconds?}` |
| `tools` | list | Tool restrictions for prompt steps |

Each step must have **exactly one** execution type. Validation enforces this at parse time.

**Source**: `src/gobby/workflows/definitions.py` — `PipelineStep` (line 420)

## Step Types

### exec — Shell Command

Runs a command via `asyncio.create_subprocess_exec` with `shlex.split`. No shell features (pipes, redirects, globs) — the command is executed directly.

```yaml
- id: build
  exec: npm run build
  condition: "${{ inputs.skip_build != 'true' }}"
```

**Output format**: `{stdout: string, stderr: string, exit_code: int}`

Default timeout: 300 seconds (configurable via `context.timeout_seconds`).

**Source**: `src/gobby/workflows/pipeline/handlers.py` — `execute_exec_step` (line 102)

### prompt — LLM Step

Sends a prompt to the default LLM provider. Requires `llm_service` to be configured (available when running via daemon, not CLI fallback).

```yaml
- id: analyze
  prompt: |
    Analyze the test results: ${{ steps.test.output.stdout }}
    Summarize failures and suggest fixes.
  tools:
    - Read
    - Grep
```

**Output format**: `{response: string, error?: string}`

**Source**: `src/gobby/workflows/pipeline/handlers.py` — `execute_prompt_step` (line 152)

### invoke_pipeline — Nested Pipeline

Invokes another pipeline definition. Two forms:

**Simple** — inherits parent inputs:
```yaml
- id: run_tests
  invoke_pipeline: test-suite
```

**Dict form** — explicit arguments:
```yaml
- id: run_tests
  invoke_pipeline:
    name: test-suite
    arguments:
      filter: "test_api"
      verbose: true
```

When using dict form, `arguments` replace the parent's inputs entirely. The `session_id` is always propagated.

**Output format**: `{pipeline: string, execution_id?: string, status?: string, error?: string}`

**Limitation**: Nested pipeline outputs (the inner pipeline's `outputs` map) are not propagated to the parent context. Downstream steps can only see `execution_id` and `status`, not the nested pipeline's actual data.

**Source**: `src/gobby/workflows/pipeline_executor.py` — `_execute_nested_pipeline` (line 433)

### mcp — MCP Tool Call

Calls an MCP tool directly via the tool proxy.

```yaml
- id: create-issue
  mcp:
    server: github
    tool: create_issue
    arguments:
      title: "Bug report from pipeline"
      body: "${{ steps.analyze.output.response }}"
```

The `arguments` dict supports template rendering and automatic type coercion (see [Type Coercion](#type-coercion-in-mcp-arguments)).

If the MCP tool returns `isError: true` or `success: false`, a `RuntimeError` is raised and the step fails.

**Output format**: `{result: string}` for SDK responses, or the raw dict for internal tools.

**Source**: `src/gobby/workflows/pipeline/handlers.py` — `execute_mcp_step` (line 11)

### activate_workflow — Activate Workflow on Session

Activates an on-demand workflow on a specific session.

```yaml
- id: setup-workflow
  activate_workflow:
    name: developer
    session_id: "${{ steps.spawn.output.session_id }}"
    variables:
      session_task: "#123"
```

**Required fields**: `name`, `session_id`. Optional: `variables`.

**Source**: `src/gobby/workflows/pipeline/handlers.py` — `execute_activate_workflow_step` (line 59)

## Data Flow

Steps communicate through the execution context. Each completed step's output is stored at `context["steps"][step_id]["output"]`.

### Reference Syntax

Two reference mechanisms:

**Output references** (`$step.output`) — Used in pipeline `outputs` mapping:
```yaml
outputs:
  result: $deploy.output
  version: $build.output.version
```

Resolved by `StepRenderer.resolve_reference()`. Supports dotted field paths for nested dict access.

**Template expressions** (`${{ expr }}`) — Used in step fields (`exec`, `prompt`, `mcp.arguments`, `invoke_pipeline.arguments`):
```yaml
- id: deploy
  exec: "deploy --env ${{ inputs.environment }} --version ${{ steps.build.output.version }}"
```

Converted to Jinja2 `{{ expr }}` internally and rendered with the full context.

### Available Template Variables

| Variable | Description |
|----------|-------------|
| `inputs.<param>` | Pipeline input parameters (merged: definition defaults + runtime overrides) |
| `steps.<step_id>.output` | Output from a completed step |
| `steps.<step_id>.output.<field>` | Nested field from step output (dict access) |
| `env.<VAR_NAME>` | Environment variables (sensitive values filtered) |
| `session_id` | Pipeline's own session (child of the caller session) |
| `parent_session_id` | Session that triggered the pipeline (the caller) |

**Source**: `src/gobby/workflows/pipeline/renderer.py` — `StepRenderer` (line 57)

## Template Rendering

### Expression Syntax

Use `${{ expression }}` in step fields. The renderer converts `${{ }}` to `{{ }}` for Jinja2:

```yaml
- id: greet
  exec: "echo Deploying ${{ inputs.app }} to ${{ inputs.environment }}"
```

### Environment Variable Filtering

Environment variables are available via `${{ env.VAR_NAME }}` but sensitive values are stripped.

**Filtered by suffix** (case-insensitive): `_SECRET`, `_KEY`, `_TOKEN`, `_PASSWORD`, `_CREDENTIAL`, `_PRIVATE_KEY`, `_AUTH`, `_OAUTH`, `_API_KEY`

**Filtered by name**: `DATABASE_URL`, `AWS_SECRET_ACCESS_KEY`, `API_KEY`, `AUTH_TOKEN`, `OAUTH_TOKEN`

### Type Coercion in MCP Arguments

After Jinja2 rendering, all values are strings. For `mcp.arguments`, the renderer automatically coerces:

| String | Coerced To | Type |
|--------|-----------|------|
| `"true"` / `"false"` | `True` / `False` | bool |
| `"null"` / `"none"` | `None` | NoneType |
| `""` (empty) | `None` | NoneType |
| `"600"` | `600` | int |
| `"3.14"` | `3.14` | float |

This coercion applies recursively to nested dicts and lists within MCP arguments.

**Source**: `src/gobby/workflows/pipeline/renderer.py` — `_coerce_value` (line 137)

## Conditions

Steps can be conditionally skipped using the `condition` field:

```yaml
- id: deploy-prod
  exec: deploy --env production
  condition: "${{ inputs.environment == 'production' }}"
```

Conditions are evaluated using `SafeExpressionEvaluator` — a secure AST-based evaluator (not `eval()`).

### Available in Conditions

- **Comparisons**: `==`, `!=`, `<`, `>`, `<=`, `>=`, `in`, `not in`
- **Boolean ops**: `and`, `or`, `not`
- **Attribute access**: `inputs.param`, `steps.build.output.field`
- **Functions**: `len()`, `bool()`, `str()`, `int()`

### Fail-Open Behavior

If condition evaluation fails (syntax error, missing variable), the step **runs by default**. Set `strict_conditions: true` on the renderer to raise errors instead.

**Source**: `src/gobby/workflows/pipeline/renderer.py` — `should_run_step` (line 229)

## Approval Gates

Approval gates pause execution until human approval via token.

```yaml
- id: deploy
  exec: deploy-to-prod
  approval:
    required: true
    message: "Approve production deployment?"
    timeout_seconds: 3600  # Field exists but not enforced at runtime
```

### Approval Flow

1. Pipeline reaches a step with `approval.required: true`
2. `ApprovalManager` generates a unique token (`secrets.token_urlsafe(24)`)
3. Step status → `WAITING_APPROVAL`, execution status → `WAITING_APPROVAL`
4. If webhooks configured, notification sent with approve/reject URLs
5. `ApprovalRequired` exception raised — execution pauses
6. External actor approves or rejects via token

### Approve/Reject

**CLI**:
```bash
gobby pipelines approve <token>
gobby pipelines reject <token>
```

**HTTP API**:
```bash
curl -X POST http://localhost:60887/api/pipelines/approve/<token>
curl -X POST http://localhost:60887/api/pipelines/reject/<token>
```

**MCP Tools**:
```python
call_tool("gobby-pipelines", "approve_pipeline", {"token": "<token>"})
call_tool("gobby-pipelines", "reject_pipeline", {"token": "<token>"})
```

On approval, the pipeline resumes from where it paused. If another approval gate is hit, a new token is returned. On rejection, the step is marked `FAILED` and execution is `CANCELLED`.

**Source**: `src/gobby/workflows/pipeline/gatekeeper.py` — `ApprovalManager` (line 25)

## Webhooks

Pipelines can trigger HTTP notifications on events:

```yaml
webhooks:
  on_approval_pending:
    url: https://hooks.slack.com/services/xxx
    method: POST
    headers:
      Authorization: "Bearer ${SLACK_TOKEN}"

  on_complete:
    url: https://api.example.com/notify
    method: POST

  on_failure:
    url: https://api.example.com/alert
```

### Header Environment Variable Expansion

Webhook headers support `${VAR_NAME}` patterns (note: `${}`, not `${{}}`) that expand from environment variables at send time.

### Event Payloads

**on_approval_pending**: `{execution_id, pipeline_name, step_id, token, message, approve_url, reject_url, status}`

**on_complete**: `{execution_id, pipeline_name, status, outputs, completed_at}`

**on_failure**: `{execution_id, pipeline_name, status, error}`

Webhook failures are logged but do not fail the pipeline.

**Source**: `src/gobby/workflows/pipeline_webhooks.py` — `WebhookNotifier` (line 25)

## MCP Tool Exposure

Pipelines with `expose_as_tool: true` are registered as dynamic MCP tools:

```yaml
name: run-tests
type: pipeline
description: Run the test suite
expose_as_tool: true

inputs:
  test_filter:
    type: string
    description: Filter tests by pattern

steps:
  - id: test
    exec: "pytest -k ${{ inputs.test_filter }}"
```

The pipeline becomes available as `pipeline:run-tests` in the MCP tool registry. Agents can invoke it:

```python
call_tool("gobby-pipelines", "pipeline:run-tests", {"test_filter": "test_api"})
```

## Execution Model

### State Machine

**ExecutionStatus** (pipeline-level):
```
PENDING → RUNNING → COMPLETED
                  → FAILED
                  → WAITING_APPROVAL → (resume) → RUNNING → ...
                  → CANCELLED (via reject)
```

**StepStatus** (step-level):
```
PENDING → RUNNING → COMPLETED
                  → FAILED
                  → WAITING_APPROVAL
                  → SKIPPED (condition false)
```

### Background Execution

When run via MCP tools (`run_pipeline`), pipelines execute as background `asyncio` tasks:

- `wait=False` (default): Returns `execution_id` immediately. Poll with `get_pipeline_status`.
- `wait=True`: Blocks up to `wait_timeout` seconds (default 300). If timeout, returns partial status and pipeline continues in background.

Background tasks are tracked in a module-level set and cleaned up on daemon shutdown.

### Resume After Approval

When execution pauses at an approval gate, the full execution state is persisted to the database. On approval, the executor reloads the pipeline definition and replays from the beginning — but completed/skipped steps are detected from DB records and skipped automatically.

**Source**: `src/gobby/workflows/pipeline_executor.py` — `PipelineExecutor` (line 38)

## Storage

### Database Tables

**pipeline_executions**:
```sql
id TEXT PRIMARY KEY,          -- Format: pe-{12hex}
pipeline_name TEXT,
project_id TEXT,
status TEXT,                  -- pending/running/waiting_approval/completed/failed/cancelled
inputs_json TEXT,
outputs_json TEXT,
created_at TEXT,
updated_at TEXT,
completed_at TEXT,
resume_token TEXT UNIQUE,     -- Current approval token
session_id TEXT,              -- Session that triggered execution
parent_execution_id TEXT      -- For nested pipeline invocations
```

**step_executions**:
```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
execution_id TEXT,            -- FK to pipeline_executions
step_id TEXT,                 -- Step ID from pipeline definition
status TEXT,                  -- pending/running/waiting_approval/completed/failed/skipped
started_at TEXT,
completed_at TEXT,
input_json TEXT,
output_json TEXT,
error TEXT,
approval_token TEXT UNIQUE,   -- Per-step approval token
approved_by TEXT,
approved_at TEXT,
UNIQUE(execution_id, step_id)
```

**Source**: `src/gobby/storage/pipelines.py` — `LocalPipelineExecutionManager`

## CLI Reference

### List Pipelines

```bash
gobby pipelines list
gobby pipelines list --json
```

### Show Pipeline Details

```bash
gobby pipelines show deploy
gobby pipelines show deploy --json
```

### Run Pipeline

```bash
# Run by name
gobby pipelines run deploy

# With inputs
gobby pipelines run deploy -i env=prod -i version=1.0

# Run Lobster file directly
gobby pipelines run --lobster ci.lobster

# JSON output
gobby pipelines run deploy --json
```

The CLI tries the daemon HTTP API first (full-featured: MCP tools, LLM prompts). If the daemon is unavailable, it falls back to a local executor that can only run `exec` steps.

### Check Status

```bash
gobby pipelines status pe-abc123
gobby pipelines status pe-abc123 --json
```

### Approve/Reject

```bash
gobby pipelines approve <token>
gobby pipelines reject <token>
```

### Execution History

```bash
gobby pipelines history deploy
gobby pipelines history deploy --limit 10
gobby pipelines history deploy --json
```

### Import Lobster Files

```bash
gobby pipelines import ci.lobster
gobby pipelines import ci.lobster -o custom/path.yaml
```

**Source**: `src/gobby/cli/pipelines.py`

## HTTP API

### POST /api/pipelines/run

Run a pipeline.

**Request**:
```json
{"name": "deploy", "inputs": {"env": "prod"}, "project_id": "optional"}
```

**200** (completed):
```json
{"status": "completed", "execution_id": "pe-abc123", "pipeline_name": "deploy"}
```

**202** (waiting approval):
```json
{
  "status": "waiting_approval",
  "execution_id": "pe-abc123",
  "step_id": "deploy",
  "token": "approval-token-xyz",
  "message": "Approve production deployment?"
}
```

### GET /api/pipelines/{execution_id}

Get execution status with step details.

**200**:
```json
{
  "id": "pe-abc123",
  "pipeline_name": "deploy",
  "status": "completed",
  "steps": [
    {"step_id": "build", "status": "completed"},
    {"step_id": "test", "status": "completed"},
    {"step_id": "deploy", "status": "completed"}
  ]
}
```

### POST /api/pipelines/approve/{token}

**200** (completed) or **202** (needs another approval).

### POST /api/pipelines/reject/{token}

**200**: `{"status": "cancelled", "execution_id": "pe-abc123"}`

**Source**: `src/gobby/servers/routes/pipelines.py`

## MCP Tools

The `gobby-pipelines` MCP server provides:

| Tool | Description |
|------|-------------|
| `list_pipelines` | List available pipeline definitions |
| `run_pipeline` | Run a pipeline with inputs. Supports `wait` and `wait_timeout` params |
| `approve_pipeline` | Approve a waiting execution by token |
| `reject_pipeline` | Reject a waiting execution by token |
| `get_pipeline_status` | Get execution status with step details |

Pipeline CRUD operations (`create_pipeline`, `update_pipeline`, `delete_pipeline`, `export_pipeline`) are on the `gobby-workflows` server, not `gobby-pipelines`.

**Source**: `src/gobby/mcp_proxy/tools/pipelines/`

## Lobster Compatibility

The `LobsterImporter` converts Lobster-format pipeline YAML to Gobby format.

### Field Mappings

| Lobster | Gobby |
|---------|-------|
| `command` | `exec` |
| `stdin: $step.stdout` | `input: $step.output` |
| `approval: true` | `approval: {required: true}` |
| `args` | `inputs` |
| `condition` | `condition` (preserved as-is) |

### Usage

```bash
# Import and convert
gobby pipelines import ci.lobster

# Run directly without importing
gobby pipelines run --lobster ci.lobster
```

**Source**: `src/gobby/workflows/lobster_compat.py` — `LobsterImporter`

## Examples

### CI/CD Pipeline

```yaml
name: ci-cd
type: pipeline
description: Build, test, and deploy

inputs:
  environment: staging

steps:
  - id: install
    exec: npm ci

  - id: lint
    exec: npm run lint

  - id: test
    exec: npm test

  - id: build
    exec: npm run build

  - id: deploy-staging
    exec: "deploy --env staging"
    condition: "${{ inputs.environment == 'staging' }}"

  - id: deploy-prod
    exec: "deploy --env production"
    condition: "${{ inputs.environment == 'production' }}"
    approval:
      required: true
      message: "Deploy to production?"
```

### MCP-Driven Pipeline

```yaml
name: triage-issue
type: pipeline
description: Auto-triage a GitHub issue

inputs:
  issue_number: 0

steps:
  - id: fetch
    mcp:
      server: github
      tool: get_issue
      arguments:
        issue_number: "${{ inputs.issue_number }}"

  - id: classify
    prompt: |
      Classify this GitHub issue:
      ${{ steps.fetch.output.result }}

      Return JSON: {"priority": "high|medium|low", "labels": ["..."], "summary": "..."}

  - id: label
    mcp:
      server: github
      tool: add_labels
      arguments:
        issue_number: "${{ inputs.issue_number }}"
        labels: "${{ steps.classify.output.response }}"
```

### Nested Pipeline with Arguments

```yaml
name: deploy-all
type: pipeline
description: Deploy to staging then production

steps:
  - id: deploy-staging
    invoke_pipeline:
      name: deploy
      arguments:
        environment: staging

  - id: verify-staging
    exec: "curl -sf https://staging.example.com/health"

  - id: deploy-prod
    invoke_pipeline:
      name: deploy
      arguments:
        environment: production
    approval:
      required: true
      message: "Staging verified. Deploy to production?"

outputs:
  staging: $deploy-staging.output
  production: $deploy-prod.output
```

### Command Listener (P2P Messaging)

Pipelines automatically create a child session, establishing the parent-child ancestry required by `send_command`/`wait_for_command`. The `session_id` template variable is the pipeline's own session, and `parent_session_id` is the caller.

```yaml
name: command-listener
type: pipeline

inputs:
  wait_timeout: 600
  max_iterations: 50
  _current_iteration: 0

steps:
  - id: notify_ready
    mcp:
      server: gobby-agents
      tool: send_message
      arguments:
        from_session: "${{ session_id }}"           # pipeline's child session
        to_session: "${{ parent_session_id }}"       # caller who started the pipeline

  - id: wait_command
    mcp:
      server: gobby-agents
      tool: wait_for_command
      arguments:
        session_id: "${{ session_id }}"
        timeout: "${{ inputs.wait_timeout }}"

  - id: next_iteration
    condition: "${{ steps.wait_command.output.command }}"
    invoke_pipeline:
      name: command-listener
      arguments:
        wait_timeout: "${{ inputs.wait_timeout }}"
        _current_iteration: "${{ inputs._current_iteration + 1 }}"
```

## See Also

- [Workflows Guide](./workflows.md) — Step-based workflows and rule engine
- [Workflow Rules](./workflow-rules.md) — Declarative rule enforcement
- [Workflow Actions](./workflow-actions.md) — Action handlers
- [Pipeline Issues](./pipeline-issues.md) — Known issues, bugs, and limitations
- [Lobster Migration](./lobster-migration.md) — Migrating from Lobster format
- [CLI Commands](./cli-commands.md) — Full CLI reference
- [HTTP Endpoints](./http-endpoints.md) — API documentation
