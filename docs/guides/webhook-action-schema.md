# Webhook Workflow Action Schema

This guide documents the webhook action model implemented by
`src/gobby/workflows/webhook.py` and the request executor implemented by
`src/gobby/workflows/webhook_executor.py` for Gobby 0.4.0.

The webhook action schema is a workflow helper model. It is separate from:

- `hook_extensions.webhooks`, which dispatches hook events to configured HTTP
  endpoints.
- Pipeline `webhooks`, which notify on pipeline approval, completion, and
  failure.
- Rule effects. Rules use effect types such as `mcp_call`, `set_variable`, and
  `block`; `webhook` is not a rule effect type.

When authoring rules that run near agent lifecycle boundaries, target semantic
events such as `turn_start` and `turn_end`. Raw `before_agent`, `after_agent`,
and `stop` events remain provider/runtime details. Agent termination is a
separate lifecycle action and still requires `gobby-agents:end_agent_run`.

## Schema

Webhook action dictionaries are parsed with `WebhookAction.from_dict(data)`.

### Required Fields

Exactly one of these fields is required:

| Field | Type | Description |
| --- | --- | --- |
| `url` | string | Literal `http://` or `https://` URL to call. |
| `webhook_id` | string | Key looked up in a caller-provided webhook registry. |

Providing both fields raises a validation error. Providing neither field also
raises a validation error.

### Optional Fields

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `method` | string enum | `POST` | HTTP method. Parsed case-insensitively and stored uppercase. |
| `headers` | object | `{}` | Request headers. The executor interpolates `${secrets.NAME}` in string header values. |
| `payload` | string, object, or null | `null` | Request body. Dict payloads are sent as JSON; string payloads are sent as raw data. |
| `timeout` | integer | `30` | Total request timeout in seconds. Must be 1-300. |
| `retry` | object or null | `null` | Retry configuration. No retry is attempted when omitted. |
| `on_success` | string or null | `null` | Parsed and serialized by the model. Executor callers provide callbacks directly. |
| `on_failure` | string or null | `null` | Parsed and serialized by the model. Executor callers provide callbacks directly. |
| `capture_response` | object or null | `null` | Names for response fields a caller may capture after execution. |

Valid methods are:

- `GET`
- `POST`
- `PUT`
- `PATCH`
- `DELETE`

## Retry Configuration

`retry` is optional. If it is omitted, the executor performs one request attempt.
If `retry: {}` is provided, these model defaults apply:

```yaml
retry:
  max_attempts: 3
  backoff_seconds: 1
  retry_on_status: [429, 500, 502, 503, 504]
```

`max_attempts` is the total number of attempts, including the first request. The
model accepts values from 1 through 10. Retry sleeps use exponential backoff:

```text
delay = backoff_seconds * 2 ** (attempt - 1)
```

The executor retries these failure classes until attempts are exhausted:

- HTTP responses whose status code is in `retry_on_status`.
- `TimeoutError`.
- `aiohttp.ClientError`.

Non-2xx HTTP responses outside `retry_on_status` return immediately as failures.

## Response Capture

`capture_response` is parsed as a `CaptureConfig`:

```yaml
capture_response:
  status_var: webhook_status
  body_var: webhook_body
  headers_var: webhook_headers
```

The executor returns a `WebhookResult` with:

- `success`
- `status_code`
- `body`
- `headers`
- `error`

`WebhookResult.json_body()` returns a parsed JSON object when the body is valid
JSON and returns `None` otherwise. The action model stores the capture variable
names, but the executor does not mutate workflow variables by itself; the caller
is responsible for writing returned fields into state.

## Interpolation

Current interpolation is intentionally narrow:

| Location | Behavior |
| --- | --- |
| `headers` | String values support `${secrets.NAME}` interpolation through the executor's `secrets` dict. Missing secrets raise `ValueError`. |
| string `payload` | Rendered through the provided `TemplateRenderer`, when one is supplied. |
| dict `payload` | Sent as-is; the executor does not deep-render nested strings. |
| `url` | Not interpolated by the action model or executor. Direct `url` values must already parse as `http://` or `https://`. |

Do not use `${env.NAME}` or `${secrets.NAME}` as a direct `url` value for this
action schema. The model validates the URL before any executor call.

## Registered Webhooks

`webhook_id` is resolved by `WebhookExecutor.execute_by_webhook_id()` from the
executor's in-memory registry:

```python
registry = {
    "slack_alerts": {
        "url": "https://hooks.slack.com/services/xxx",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "timeout": 30,
    }
}
```

Registry headers are merged with per-call headers, and per-call headers win on
key conflicts. `method` and `timeout` passed to `execute_by_webhook_id()`
override registry values. A missing registry key raises `ValueError`.

This registry is supplied by the caller. It is not the same schema as
`hook_extensions.webhooks.endpoints` in the daemon config.

## Examples

### Minimal Direct URL

```yaml
url: "https://api.example.com/events"
```

Parsed defaults:

```yaml
method: POST
headers: {}
payload: null
timeout: 30
retry: null
```

### Full Action Shape

```yaml
url: "https://api.example.com/events"
method: PUT
headers:
  Authorization: "Bearer ${secrets.API_TOKEN}"
  X-Custom: "value"
payload:
  event: "session_end"
  session_id: "sess-123"
timeout: 60
retry:
  max_attempts: 3
  backoff_seconds: 2
  retry_on_status: [429, 500, 502]
on_success: log_success
on_failure: alert_failure
capture_response:
  status_var: response_status
  body_var: response_body
  headers_var: response_headers
```

### Registered Webhook Reference

```yaml
webhook_id: "slack_alerts"
payload:
  text: "Build finished"
```

The registry lookup must provide the target URL. If the registry value has no
`url`, execution raises `ValueError`.

## Validation And Serialization

`WebhookAction.from_dict()` validates:

- `url` and `webhook_id` are mutually exclusive.
- one target field is present.
- direct `url` values use `http` or `https`.
- `method` is one of the supported HTTP methods.
- `timeout` is between 1 and 300.
- `retry.max_attempts` is between 1 and 10.

`WebhookAction.to_dict()` serializes only populated optional fields, plus the
stored `method` and `timeout`.

## Related Files

- `src/gobby/workflows/webhook.py`
- `src/gobby/workflows/webhook_executor.py`
- `tests/workflows/test_webhook_action.py`
- `tests/workflows/test_webhook_executor.py`
- [Rules](./rules.md)
- [Configuration](./configuration.md)

_Last verified: 2026-05-07_
