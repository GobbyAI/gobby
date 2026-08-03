# Webhook Transport And Workflow Action Model

This guide distinguishes the workflow action data model from Gobby's live
webhook delivery stack.

The current source paths are:

- `src/gobby/workflows/webhook.py`: parses and serializes `WebhookAction`,
  `RetryConfig`, and `CaptureConfig` values.
- `src/gobby/utils/webhook_transport.py`: performs bounded outbound HTTP
  delivery for runtime webhook callers.
- `src/gobby/hooks/webhooks.py`: matches configured hook endpoints, builds
  payloads, invokes the shared transport, and interprets blocking decisions.
- `src/gobby/hooks/dispatchers/webhook.py`: connects the hook manager to
  synchronous blocking delivery and asynchronous observer delivery.
- `src/gobby/workflows/pipeline_webhooks.py`: sends pipeline notifications
  through the shared transport.

`WebhookAction` is currently a helper model with no runtime workflow action
dispatcher. Parsing an action dictionary does not send a request. In particular,
the model's `webhook_id`, callback, response-capture, and retry fields are stored
configuration; no shipped runtime resolves or executes them as a generic YAML
`action: webhook` step.

The action model is also separate from rule effects. Rules use effect types such
as `mcp_call`, `set_variable`, and `block`; `webhook` is not a rule effect type.

## Action Model Schema

Webhook action dictionaries are parsed with `WebhookAction.from_dict(data)`.

### Required Fields

Exactly one target field is required by the model:

| Field | Type | Current behavior |
| --- | --- | --- |
| `url` | string | Must use the `http` or `https` scheme. Stored for serialization. |
| `webhook_id` | string | Stored as an identifier. The current runtime has no registry resolver for this field. |

Providing both fields or neither field raises `ValueError`.

### Optional Fields

| Field | Type | Default | Current behavior |
| --- | --- | --- | --- |
| `method` | string enum | `POST` | Parsed case-insensitively and stored uppercase. |
| `headers` | object | `{}` | Stored as supplied. The model performs no interpolation. |
| `payload` | string, object, or null | `null` | Stored as supplied. The model performs no rendering. |
| `timeout` | integer | `30` | Must be between 1 and 300 seconds. |
| `retry` | object or null | `null` | Parsed into `RetryConfig`; the model does not schedule requests. |
| `on_success` | string or null | `null` | Stored and serialized only. |
| `on_failure` | string or null | `null` | Stored and serialized only. |
| `capture_response` | object or null | `null` | Parsed into `CaptureConfig` and serialized only. |

Valid methods are `GET`, `POST`, `PUT`, `PATCH`, and `DELETE`.

## Retry And Capture Metadata

A non-empty `retry` mapping is parsed with these defaults:

```yaml
retry:
  max_attempts: 3
  backoff_seconds: 1
  retry_on_status: [429, 500, 502, 503, 504]
```

`max_attempts` must be between 1 and 10. An omitted or empty mapping becomes
`null`. These values are model metadata until a caller explicitly maps them to
the transport API.

Response-capture names are parsed in the same way:

```yaml
capture_response:
  status_var: webhook_status
  body_var: webhook_body
  headers_var: webhook_headers
```

The model does not write workflow variables or call success/failure callbacks.

## Live Transport API

Runtime callers use `WebhookTransport.execute()` from
`src/gobby/utils/webhook_transport.py`. It accepts a URL, method, headers,
payload, timeout, response-size limit, retry bounds, retry statuses, and an
optional shared `httpx.AsyncClient`.

The transport validates methods, headers, URLs, and numeric limits; resolves and
pins target addresses; disables redirects and environment proxy inheritance;
caps response bodies; and sanitizes transport errors. Address policy is chosen
by the caller through `allow_private_addresses`.

`execute()` returns `WebhookTransportResult`, containing:

- `success`
- `status_code`
- `body`
- `headers`
- `error` and structured failure diagnostics
- `attempts`

`WebhookTransportResult.json_body()` returns a JSON object when the response
body contains one. The transport defaults to one attempt. Callers opt into
additional attempts and backoff explicitly.

## Hook-Dispatcher Stack

Hook extension webhooks are configured under `hook_extensions.webhooks`.
`WebhookDispatcher` in `src/gobby/hooks/webhooks.py` filters enabled endpoints by
event, builds the normalized payload, expands configured URL and header values,
and calls `WebhookTransport.execute()`.

For each hook endpoint, the dispatcher maps `retry_count` to
`max_attempts = retry_count + 1`. Blocking endpoints are awaited within the
shared blocking-effect deadline and may return a blocking decision. Fail-closed
endpoints also block when delivery fails. Observer endpoints are scheduled by
`src/gobby/hooks/dispatchers/webhook.py` for background delivery.

## Pipeline Notifications

`WebhookNotifier` in `src/gobby/workflows/pipeline_webhooks.py` sends approval,
completion, and failure notifications through the same transport. It uses the
transport's one-attempt default and logs delivery failures without changing the
pipeline transition.

## Parse-Only Example

The following dictionary is valid input to `WebhookAction.from_dict()` and can
be serialized back with `WebhookAction.to_dict()`:

```yaml
url: "https://api.example.com/events"
method: PUT
headers:
  Authorization: "Bearer ${secrets.API_TOKEN}"
payload:
  event: "session_end"
timeout: 60
retry:
  max_attempts: 3
  backoff_seconds: 2
  retry_on_status: [429, 500, 502]
on_success: log_success
capture_response:
  status_var: response_status
```

The `${secrets.API_TOKEN}` value remains literal in this model. Runtime hook and
pipeline surfaces perform their own configuration expansion before calling the
transport.

## Validation And Serialization

`WebhookAction.from_dict()` validates target exclusivity, URL scheme, HTTP
method, timeout, and retry attempt bounds. `WebhookAction.to_dict()` serializes
populated optional fields plus the stored `method` and `timeout`.

## Related Files

- `src/gobby/workflows/webhook.py`
- `src/gobby/utils/webhook_transport.py`
- `src/gobby/hooks/webhooks.py`
- `src/gobby/hooks/dispatchers/webhook.py`
- `src/gobby/workflows/pipeline_webhooks.py`
- `tests/workflows/test_webhook_action.py`
- [Webhooks And Plugins](./webhooks-and-plugins.md)
- [Rules](./rules.md)

_Last verified: 2026-08-02_
