---
name: pipelines-and-cron
description: Use when choosing, authoring, scheduling, or operating Gobby pipelines and cron jobs.
version: "1.0.0"
category: authoring
metadata:
  gobby:
    audience: all
---

# Pipelines and Cron

Use one owning automation path for each responsibility:

| Need | Owner |
| --- | --- |
| Deterministic multi-step execution, data flow, or approval gates | Pipeline |
| Scheduled or one-shot execution | Cron |
| Task-lifecycle automation across claims, agents, review, and merge | `gobby build` / dispatch |

Composition is normal. A scheduled release check is a pipeline invoked by cron.
Task selection and worker dispatch stay in the build/dispatch path. That path
retains task lifecycle ownership.

## Author a Pipeline

Project pipeline files live under `.gobby/workflows/pipelines/`. Global user
pipelines live under `~/.gobby/workflows/pipelines/`. Bundled product pipelines
live under `src/gobby/install/shared/workflows/pipelines/` and require repository
tests and manifest handling.

A pipeline needs `type: pipeline`, a unique kebab-case name, and at least one
step. Each step has exactly one execution type: `exec`, `prompt`, `mcp`,
`invoke_pipeline`, or `wait`.

```yaml
name: release-check
type: pipeline
version: "1.0"
description: Test and deploy a selected environment
enabled: true

inputs:
  environment:
    type: string
    default: staging

steps:
  - id: test
    exec: uv run pytest tests/smoke/ -q

  - id: deploy
    condition: "${{ inputs.environment == 'production' }}"
    exec: ./scripts/deploy.sh "${{ inputs.environment }}"
    approval:
      required: true
      message: Approve production deployment?
      timeout_seconds: 900
```

Use `${{ inputs.<name> }}` for inputs and `${{ steps.<id>.output }}` for prior
step output. References must point backward to completed steps. Put approval gates
before side effects such as deploy, publish, merge, or delete. Make resumable
steps idempotent before enabling `resume_on_restart`.

### Pipeline Tool Workflow

Discover `gobby-workflows` through the MCP proxy before calling its tools.

1. Install validated YAML with `create_pipeline(yaml_content=...)`.
2. Start it with `run_pipeline(name=..., inputs=...)`.
3. Keep the returned `execution_id`; `run_pipeline` returns immediately.
4. Inspect steps with `get_pipeline_status(execution_id=...)`.
5. Change fields or replace YAML with `update_pipeline(name=..., ...)`.

```python
call_tool("gobby-workflows", "create_pipeline", {
    "yaml_content": pipeline_yaml,
})

run = call_tool("gobby-workflows", "run_pipeline", {
    "name": "release-check",
    "inputs": {"environment": "staging"},
})

call_tool("gobby-workflows", "get_pipeline_status", {
    "execution_id": run["execution_id"],
})

call_tool("gobby-workflows", "update_pipeline", {
    "name": "release-check",
    "enabled": True,
})
```

The broader lifecycle also includes `list_pipelines`, `get_pipeline`,
`resume_pipeline`, `approve_pipeline`, `reject_pipeline`, `cancel_pipeline`,
`list_pipeline_executions`, `search_pipeline_executions`, `delete_pipeline`, and
`export_pipeline`. Fetch each schema before first use.

## Schedule with Cron

Cron owns timing. Choose one schedule shape:

| `schedule_type` | Required field | Example |
| --- | --- | --- |
| `cron` | `cron_expr` | `0 9 * * MON` |
| `interval` | `interval_seconds` | `3600` |
| `once` | `run_at` | `2026-07-20T14:00:00Z` |

Set `timezone` explicitly when wall-clock time matters. For cron expressions,
use the five-field minute/hour/day-of-month/month/day-of-week form.

### Cron Action Shapes

| `action_type` | Core `action_config` |
| --- | --- |
| `pipeline` | `pipeline_name`, optional `inputs` |
| `agent_spawn` | `prompt`; optional provider, workflow, timeout, or current agent definition |
| `shell` | `command`; optional `args`, `cwd`, `timeout_seconds` |

Prefer a pipeline action when scheduled work has multiple steps, data flow, or
approval gates. Reserve shell actions for small, bounded commands with explicit
arguments and working directory.

```python
created = call_tool("gobby-cron", "create_cron_job", {
    "name": "weekly-release-check",
    "schedule_type": "cron",
    "cron_expr": "0 9 * * MON",
    "timezone": "America/Chicago",
    "action_type": "pipeline",
    "action_config": {
        "pipeline_name": "release-check",
        "inputs": {"environment": "staging"},
    },
    "description": "Run the release check every Monday",
})

job_id = created["job"]["id"]
```

### Cron Tool Family

| Tool | Use |
| --- | --- |
| `list_cron_jobs` | Find jobs by project or enabled state |
| `create_cron_job` | Create cron, interval, or one-shot schedule |
| `get_cron_job` | Inspect one job by `job_id` |
| `update_cron_job` | Replace schedule, action, metadata, or enabled state |
| `toggle_cron_job` | Flip enabled state |
| `delete_cron_job` | Delete the job and its run history |
| `run_cron_job` | Trigger an immediate test run |
| `list_cron_runs` | Inspect execution history for one job |

After creation, trigger a safe immediate run and inspect its result:

```python
call_tool("gobby-cron", "run_cron_job", {"job_id": job_id})
call_tool("gobby-cron", "list_cron_runs", {"job_id": job_id, "limit": 1})
```

Use `update_cron_job(job_id=..., enabled=False)` before changing a live job whose
action has side effects. Re-enable it only after the immediate test run succeeds.

## Verification

Before handing off automation:

1. Parse the YAML and validate it as a `PipelineDefinition`.
2. Confirm step IDs are unique and each step has one execution type.
3. Exercise `create_pipeline`, then run with harmless inputs.
4. Inspect `get_pipeline_status` through completion or approval wait.
5. Create the cron job with the intended timezone and action config.
6. Use `run_cron_job` and `list_cron_runs` to verify one safe execution.
7. Confirm approval gates protect every irreversible side effect.
