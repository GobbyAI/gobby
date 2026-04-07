# Importing External Pipeline Formats

This guide covers importing pipeline definitions from external formats (e.g., `.lobster` files) into Gobby's native pipeline system.

## Overview

Gobby's pipeline system supports importing simpler pipeline formats and converting them to native Gobby definitions, which offer additional capabilities:

- LLM-powered steps (`prompt` field with tool restrictions)
- Webhook notifications on pipeline events
- MCP tool exposure (`expose_as_tool: true`)
- Composable pipelines (`invoke_pipeline` step type)
- MCP tool steps for direct tool calls
- Session spawning and workflow activation

## Importing Pipeline Files

Convert external pipeline files to Gobby format:

```bash
# Import to .gobby/workflows/
gobby pipelines import my-pipeline.lobster

# Import to custom location
gobby pipelines import my-pipeline.lobster -o pipelines/my-pipeline.yaml
```

The converted file can then be run by name:

```bash
gobby pipelines run my-pipeline
```

## Syntax Mapping

### Field Conversions

| External Format | Gobby | Example |
|-----------------|-------|---------|
| `command` | `exec` | `exec: npm run build` |
| `stdin: $step.stdout` | `input: $step.output` | `input: $build.output` |
| `approval: true` | `approval: {required: true}` | See below |
| `args` | `inputs` | Pipeline-level parameters |
| `condition` | `condition` | Same syntax (preserved) |

### Before/After: Basic Pipeline

**External format (`ci.lobster`):**
```yaml
name: ci-pipeline
description: CI/CD pipeline

args:
  environment: staging

steps:
  - id: build
    command: npm run build

  - id: test
    command: npm test
    stdin: $build.stdout

  - id: deploy
    command: deploy --env $environment
    approval: true
```

**Gobby (`ci-pipeline.yaml`):**
```yaml
name: ci-pipeline
type: pipeline
description: CI/CD pipeline

inputs:
  environment: staging

steps:
  - id: build
    exec: npm run build

  - id: test
    exec: npm test
    input: $build.output

  - id: deploy
    exec: deploy --env $environment
    approval:
      required: true
```

### Before/After: Approval with Message

**External format:**
```yaml
- id: deploy
  command: deploy-prod
  approval:
    required: true
    message: "Deploy to production?"
```

**Gobby:**
```yaml
- id: deploy
  exec: deploy-prod
  approval:
    required: true
    message: "Deploy to production?"
```

### Before/After: Conditional Steps

**External format:**
```yaml
- id: notify
  command: send-notification
  condition: $deploy.approved
```

**Gobby:**
```yaml
- id: notify
  exec: send-notification
  condition: $deploy.approved
```

## Gobby-Exclusive Features

After migration, you can enhance your pipelines with Gobby-only features:

### LLM-Powered Steps

Add AI analysis to your pipeline:

```yaml
steps:
  - id: test
    exec: pytest --json-report

  - id: analyze
    prompt: |
      Analyze the test results in $test.output.
      Identify patterns in failures and suggest fixes.

      Return JSON: {
        "summary": "...",
        "failures": [...],
        "recommendations": [...]
      }
    tools:
      - Read
      - Grep
```

### Webhook Notifications

Get notified on pipeline events:

```yaml
name: deploy
type: pipeline

webhooks:
  on_approval_pending:
    url: https://hooks.slack.com/xxx
    method: POST
    body:
      text: "Deployment needs approval"
      execution_id: "{{ execution_id }}"

  on_complete:
    url: https://api.pagerduty.com/resolve
    headers:
      Authorization: "Bearer {{ env.PD_TOKEN }}"

steps:
  - id: deploy
    exec: deploy-app
    approval:
      required: true
```

### MCP Tool Exposure

Make pipelines callable by AI agents:

```yaml
name: run-tests
type: pipeline
description: Run test suite with optional filter

expose_as_tool: true

inputs:
  filter:
    type: string
    description: Test filter pattern
    default: ""

steps:
  - id: test
    exec: pytest -k "{{ inputs.filter }}"
```

Agents can now invoke this pipeline:
```python
mcp__gobby__call_tool(
    server_name="gobby-workflows",
    tool_name="run-tests",
    arguments={"filter": "test_api"}
)
```

### Composable Pipelines

Call one pipeline from another:

```yaml
name: full-ci
type: pipeline

steps:
  - id: unit-tests
    invoke_pipeline: run-unit-tests

  - id: integration-tests
    invoke_pipeline: run-integration-tests
    condition: $unit-tests.status == 'completed'

  - id: deploy
    invoke_pipeline: deploy-staging
```

### MCP Tool Steps

Call MCP tools directly from pipeline steps without needing an LLM:

```yaml
steps:
  - id: get-tasks
    mcp:
      server: gobby-tasks
      tool: list_tasks
      arguments:
        status: open

  - id: create-issue
    mcp:
      server: github
      tool: create_issue
      arguments:
        title: "Tasks summary"
        body: ${{ steps.get-tasks.output }}
```

### Run from Workflow Actions

Trigger pipelines from lifecycle or step workflows:

```yaml
# In a lifecycle workflow
type: lifecycle

triggers:
  on_session_start:
    - action: run_pipeline
      name: setup-environment
      inputs:
        session_id: "{{ session_id }}"
```

## Step-by-Step Conversion Example

### 1. Start with External Pipeline File

```yaml
# deploy.lobster
name: deploy
description: Deploy application

args:
  env: staging
  version: latest

steps:
  - id: checkout
    command: git checkout $version

  - id: build
    command: npm run build
    stdin: $checkout.stdout

  - id: test
    command: npm test

  - id: deploy
    command: |
      deploy-app \
        --env $env \
        --version $version
    approval: true
    condition: $test.status == 'success'

  - id: notify
    command: |
      curl -X POST https://slack.com/webhook \
        -d '{"text": "Deployed $version to $env"}'
    condition: $deploy.approved
```

### 2. Import to Gobby

```bash
gobby pipelines import deploy.lobster
```

### 3. Review Converted File

```yaml
# .gobby/workflows/deploy.yaml
name: deploy
type: pipeline
version: '1.0'
description: Deploy application

inputs:
  env: staging
  version: latest

steps:
  - id: checkout
    exec: git checkout $version

  - id: build
    exec: npm run build
    input: $checkout.output

  - id: test
    exec: npm test

  - id: deploy
    exec: |
      deploy-app \
        --env $env \
        --version $version
    approval:
      required: true
    condition: $test.status == 'success'

  - id: notify
    exec: |
      curl -X POST https://slack.com/webhook \
        -d '{"text": "Deployed $version to $env"}'
    condition: $deploy.approved
```

### 4. Enhance with Gobby Features (Optional)

```yaml
name: deploy
type: pipeline
version: '1.0'
description: Deploy application

inputs:
  env: staging
  version: latest

# NEW: Webhook notifications
webhooks:
  on_approval_pending:
    url: https://slack.com/webhook
    body:
      text: "Deployment to {{ inputs.env }} needs approval"

# NEW: Expose as MCP tool
expose_as_tool: true

steps:
  - id: checkout
    exec: git checkout $version

  - id: build
    exec: npm run build
    input: $checkout.output

  - id: test
    exec: npm test

  # NEW: AI-powered test analysis
  - id: analyze-tests
    prompt: |
      Review the test output: $test.output
      Summarize any failures and assess deployment risk.
    tools:
      - Read

  - id: deploy
    exec: |
      deploy-app \
        --env $env \
        --version $version
    approval:
      required: true
      message: "Test analysis: $analyze-tests.output\n\nProceed with deployment?"
    condition: $test.status == 'success'

  - id: notify
    exec: |
      curl -X POST https://slack.com/webhook \
        -d '{"text": "Deployed $version to $env"}'
    condition: $deploy.approved
```

### 5. Run the Pipeline

```bash
# Run with defaults
gobby pipelines run deploy

# Run with custom inputs
gobby pipelines run deploy -i env=production -i version=v2.1.0
```

## CLI Command Mapping

| Command | Description |
|---------|-------------|
| `gobby pipelines import <file>` | Import and convert external pipeline file |
| `gobby pipelines run <name>` | Run an imported pipeline by name |
| `gobby pipelines approve <token>` | Approve a waiting pipeline step |
| `gobby pipelines reject <token>` | Reject a waiting pipeline step |
| `gobby pipelines status <id>` | Check execution status |
| `gobby pipelines list` | Discover available pipelines |
| `gobby pipelines show <name>` | View pipeline definition |
| `gobby pipelines history <name>` | View execution history |

## Troubleshooting

### Import Errors

**Error: "File not found"**
```bash
gobby pipelines import nonexistent.lobster
# Error: File not found: nonexistent.lobster
```
Solution: Check the file path is correct.

**Error: "Invalid YAML"**
```bash
gobby pipelines import malformed.lobster
# Error: Failed to import: ...
```
Solution: Validate your YAML syntax.

### Execution Errors

**Error: "Pipeline not found"**
```bash
gobby pipelines run unknown-pipeline
# Pipeline 'unknown-pipeline' not found.
```
Solution: Use `gobby pipelines list` to see available pipelines.

**Error: "Step failed"**
Check the execution status for details:
```bash
gobby pipelines status <execution_id> --json
```

## See Also

- [Pipelines Guide](./pipelines.md) - Full pipeline reference
- [Workflows Overview](./workflows-overview.md) - How rules, agents, and pipelines fit together
- [CLI Commands](./cli-commands.md) - Full CLI reference
