# Webhooks And Plugin Extension Guide

Gobby 0.4.0 has two shipped webhook surfaces:

1. Hook extension webhooks that POST normalized hook events to external services.
2. Pipeline webhooks that notify external services when pipeline approval,
   completion, or failure events occur.

Gobby 0.4.0 does not expose a custom Python plugin workflow action API. Treat
older plugin-action examples as retired documentation patterns.

## Choosing A Webhook Surface

| Use case | Surface | Configuration |
| --- | --- | --- |
| Notify or gate on CLI hook activity | Hook extension webhooks | `hook_extensions.webhooks` |
| Notify on pipeline approval, completion, or failure | Pipeline webhooks | `webhooks` inside a `type: pipeline` workflow |
| Call HTTP from custom workflow action code | Webhook action model | `src/gobby/workflows/webhook.py` and `webhook_executor.py` support models, but there is no general YAML action dispatcher wired for docs users |

## Hook Extension Webhooks

Hook extension webhooks are configured under `hook_extensions.webhooks` in the
daemon config. They receive normalized hook events from the runtime hook manager.

```yaml
hook_extensions:
  webhooks:
    enabled: true
    default_timeout: 10.0
    async_dispatch: true
    endpoints:
      - name: slack-alerts
        url: "${SLACK_WEBHOOK_URL}"
        events:
          - before_tool
          - after_tool
        headers:
          Content-Type: "application/json"
        timeout: 10.0
        retry_count: 3
        retry_delay: 1.0
        can_block: false
        enabled: true
```

Config values are expanded when configuration loads. Supported references are
`${VAR}`, `${VAR:-default}`, and `$secret:NAME`; secret references resolve
through the encrypted secrets store before environment fallback.

### Endpoint Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | string | required | Unique endpoint name |
| `url` | string | required | HTTP endpoint that receives JSON POSTs |
| `events` | list[string] | `[]` | Empty list means all hook events |
| `headers` | dict[string,string] | `{}` | Merged with Gobby's JSON and event headers |
| `timeout` | float | `10.0` | Per-request timeout, 1 to 60 seconds |
| `retry_count` | int | `3` | Retries after the first attempt, 0 to 10 |
| `retry_delay` | float | `1.0` | Initial retry delay, doubled after each retry |
| `can_block` | bool | `false` | Allows a webhook response to block the hook action |
| `enabled` | bool | `true` | Per-endpoint toggle |

The dispatcher adds these headers:

```http
Content-Type: application/json
User-Agent: Gobby-Webhook/1.0
X-Gobby-Event: <event_type>
```

Custom headers override or extend those defaults.

### Event Names

Endpoint matching normalizes hyphens and underscores, so `before-tool` and
`before_tool` match the same internal hook event. Common event payload values
include:

| Event | Purpose |
| --- | --- |
| `session_start` | Session creation and bootstrap |
| `session_end` | Session archival or end lifecycle |
| `before_agent` | Runtime pre-turn hook |
| `after_agent` | Runtime post-turn hook |
| `stop` | Runtime stop hook where supported |
| `before_tool` | Tool request approval and pre-tool handling |
| `after_tool` | Tool result processing |
| `pre_compact` | Pre-compaction handling |
| `post_compact` | Post-compaction handling |
| `notification` | Provider notification events |

For workflow rule authoring, use semantic lifecycle events such as `turn_start`
and `turn_end`. Raw events such as `before_agent`, `after_agent`, and `stop` are
provider/runtime details exposed by hook payloads and adapters. Agent
termination is separate from ending a turn; an agent run that is instructed to
finish must still call `gobby-agents:end_agent_run`.

### Payload Shape

Hook webhooks receive a JSON object with this shape:

```json
{
  "event_type": "before_tool",
  "session_id": "019e...",
  "source": "codex",
  "timestamp": "2026-05-07T16:00:00+00:00",
  "data": {},
  "machine_id": "machine-id",
  "cwd": "/path/to/project",
  "project_id": "project-uuid",
  "task_id": "task-uuid",
  "metadata": {}
}
```

`data` contains the adapter-native hook payload. Keep receivers tolerant of
source-specific fields because Claude, Codex, Droid, Gemini, and Qwen do not
send identical raw hook data.

### Blocking Webhooks

Set `can_block: true` only for endpoints that are allowed to affect hook
decisions. Blocking endpoints are evaluated before normal handler execution. A
blocking endpoint can deny the action with a 2xx JSON response:

```json
{
  "decision": "block",
  "reason": "Deployment tools are disabled for this project."
}
```

`decision: "deny"` is also treated as a block. If the response omits `reason`,
Gobby supplies a fallback reason in logs and hook output. Webhook transport
errors fail open so that an unavailable external service does not break local
agent operation.

### CLI And HTTP Management

Use the CLI to inspect and test configured hook webhooks:

```bash
uv run gobby webhooks list
uv run gobby webhooks list --json
uv run gobby webhooks test slack-alerts
uv run gobby webhooks test slack-alerts --event before_tool
```

The CLI calls these daemon routes:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/webhooks` | List configured hook webhook endpoints |
| `POST` | `/api/webhooks/test` | Send a test payload to one endpoint |

## Pipeline Webhooks

Pipeline webhooks live inside a `type: pipeline` workflow definition. They are
notifications for pipeline lifecycle events, not hook-event webhooks.

```yaml
name: release-check
type: pipeline

steps:
  - id: run_checks
    exec: "uv run pytest tests/release -v"

webhooks:
  on_approval_pending:
    url: "${REVIEW_WEBHOOK_URL}"
    method: POST
    headers:
      Authorization: "Bearer ${REVIEW_WEBHOOK_TOKEN}"
  on_complete:
    url: "${PIPELINE_WEBHOOK_URL}"
    method: POST
  on_failure:
    url: "${PIPELINE_WEBHOOK_URL}"
    method: POST
```

Pipeline webhook endpoints support:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | required | Target URL |
| `method` | string | `POST` | Runtime sender supports `POST` and `PUT` |
| `headers` | dict[string,string] | `{}` | Header values expand `${VAR}` from the environment |

Approval-pending payloads include `execution_id`, `pipeline_name`, `step_id`,
`token`, `message`, `approve_url`, `reject_url`, and `status`. Completion
payloads include `execution_id`, `pipeline_name`, `status`, `outputs`, and
`completed_at`. Failure payloads include `execution_id`, `pipeline_name`,
`status`, and `error`.

Pipeline webhook delivery logs failures but does not retry or block pipeline
state transitions.

## Workflow Webhook Action Models

The source tree still contains webhook action support models:

- `src/gobby/workflows/webhook.py` defines `WebhookAction`, `RetryConfig`, and
  `CaptureConfig`.
- `src/gobby/workflows/webhook_executor.py` executes direct URLs and registered
  webhook IDs with retry logic, secret interpolation in headers, and optional
  callbacks.
- `docs/guides/webhook-action-schema.md` records the model schema.

Those files are useful source references for contributors, but this guide should
not imply that arbitrary workflow YAML can currently run `action: webhook`
through a shipped action dispatcher. For user-facing automation in 0.4.0, use
hook extension webhooks or pipeline webhooks.

## Plugin Development Status

Python plugin actions are not a supported 0.4.0 extension surface. The current
CLI registers `hooks` and `webhooks` extension commands, but the runtime hook
plugin API described in older docs is not present in the active source tree.

Use these supported extension points instead:

| Need | Supported path |
| --- | --- |
| Add reusable agent guidance | Create or install a skill |
| Add deterministic automation | Create a workflow rule or pipeline |
| Expose a callable operation | Add an MCP tool to the appropriate server |
| Integrate an external notification system | Configure hook extension or pipeline webhooks |
| Change CLI behavior | Add or update a Click command under `src/gobby/cli/` |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `gobby webhooks list` shows disabled | Set `hook_extensions.webhooks.enabled: true` |
| No endpoints are listed | Add entries under `hook_extensions.webhooks.endpoints` |
| Endpoint does not receive one event | Check the endpoint `events` list; empty means all events |
| Test call fails | Run `uv run gobby webhooks test <name> --json` and inspect `error`, `status_code`, and `response_time_ms` |
| Blocking webhook does not block | Confirm `can_block: true` and return 2xx JSON with `decision: "block"` or `"deny"` |
| Pipeline notification does not arrive | Confirm the webhook is under the pipeline's top-level `webhooks`, not `hook_extensions.webhooks` |
| Old plugin examples fail | Replace them with a supported skill, rule, pipeline, MCP tool, or CLI extension |

## See Also

- [Rules Guide](./rules.md) - Semantic rule events and rule effects.
- [Hook Schemas](./hook-schemas.md) - Provider hook mappings and lifecycle
  normalization.
- [Pipelines](./pipelines.md) - Pipeline definitions, steps, approvals, and
  webhook notifications.
- [HTTP Endpoints](./http-endpoints.md) - Webhook management routes.
- [Configuration](./configuration.md) - Daemon config shape and expansion rules.

_Last verified: 2026-05-07_
