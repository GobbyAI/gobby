# Hook ingress contract

`POST /api/hooks/execute` accepts a ghook envelope and returns the provider's
hook response. This document freezes the Python hub route for contributors
porting the route or implementing node forwarding. The HTTP corpus records the
hub wire behavior; it does not prove node parity.

## Request

Send `Content-Type: application/json` and `Authorization: Bearer <key>`.
ghook obtains the key through `gobby_core::local_token`; on a node this is the
machine-bound node key in `bootstrap.yaml`. Credentials are supplied at runtime
and never committed to the corpus.

The body is an envelope v1 JSON object:

```json
{
  "schema_version": 1,
  "hook_type": "Notification",
  "input_data": {"message": "contract probe"},
  "source": "claude",
  "critical": false,
  "enqueued_at": "2026-01-01T00:00:00Z",
  "response_capability": "hook-response.v1"
}
```

`hook_type` and `source` are required. Supported sources are `claude`, `codex`,
`grok`, `droid` and `agy`. `input_data` defaults to an empty object; `critical`
defaults to false. `enqueued_at` and `response_capability` are request metadata.
`X-Gobby-Envelope-Id` identifies the spool file stem and enables deduplication.
`X-Gobby-Session-Id` supplies the platform session identity; it may be absent.

The route rejects a non-object with HTTP 400 `{"detail":"JSON object required"}`.
A missing or unsupported schema version gets HTTP 400, for example:

```json
{"detail":"Unsupported schema_version: None. Supported: 1"}
```

Missing required fields and an unsupported source also return HTTP 400:

```json
{"detail":"hook_type required"}
{"detail":"source required"}
{"detail":"Unsupported source: unsupported. Supported: claude, grok, codex, droid, agy"}
```

The unsupported source value in that detail is the submitted value.

The corpus's `schema_version: 2` describes the recording format, independently
of envelope `schema_version: 1`.

Authority: [`_normalize_hook_request`](../../src/gobby/servers/routes/mcp/hooks.py)
and [`runtime_compat.py`](../../src/gobby/hooks/runtime_compat.py).

## Capability floor and successful response

The producer must advertise exactly `response_capability: "hook-response.v1"`.
Missing or other values are below the floor. Before claiming an envelope, the
route returns HTTP 200 with a provider-specific graceful allow response and the
message `Gobby hook error (non-fatal): hook-response capability below floor.
Tool execution will proceed normally.` The provider contract determines whether
the message is context or a system message.

Normal HTTP 200 carries the provider hook response unchanged, with an optional
`_gobby_delivery_receipt`:

```json
{
  "receipt_id": "<receipt UUID>",
  "original_envelope_id": "<envelope file stem>",
  "delivery_generation": 1
}
```

That object is the value of `_gobby_delivery_receipt`, not the entire hook
response. A receipt requires a database and a resolved session; a request with
no session row can return 200 without one. If writing a receipted response fails,
`ReceiptGuardedJSONResponse` releases the receipt for redelivery.

Authority: [`_graceful_error_response`](../../src/gobby/servers/routes/mcp/hook_responses.py)
and [`attach_delivery_receipt`](../../src/gobby/hooks/receipt_redelivery.py).

## Duplicate envelopes and replay

With `X-Gobby-Envelope-Id`, the route claims the envelope before adapter
execution and renews its processing lease. A duplicate with a stored terminal
response returns that same response with HTTP 200. A vanished or stale
processing marker may be reclaimed and executed again.

STOP and `turn_end` **block** responses have a 120-second grace: replay is
allowed only within that grace and when `processed_at` is at or after
`hooks/inbox/processed/.stop_replay_epoch`. Session start advances the epoch.
Aged or pre-epoch stop blocks are removed so execution can re-evaluate. This
bound does not expire ordinary PreToolUse denials.

Unreclaimed duplicates without a replayable response get HTTP 409:

```json
{"status":"processing","reason":"duplicate envelope already processing"}
{"status":"processed","reason":"duplicate envelope previously processed"}
{"status":"malformed_marker","reason":"duplicate envelope marker malformed"}
```

Authority: [`execute_hook`](../../src/gobby/servers/routes/mcp/hooks.py) and
[`envelope_terminal_response`](../../src/gobby/hooks/envelope_dedupe.py).

## Retryable failures

An uninitialized hook manager returns HTTP 503
`{"detail":"HookManager not initialized"}`. This is not a retry envelope;
it has no `status` or `retry_kind` fields.

Retryable failures release the processing claim and return HTTP 503 with one
of these bodies. ghook retains the spool envelope for retry:

```json
{"status":"retry","retry_kind":"preflight_timeout"}
{"status":"retry","retry_kind":"ingress_backpressure"}
{"status":"retry","retry_kind":"adapter_timeout"}
```

Authority: [`execute_hook`](../../src/gobby/servers/routes/mcp/hooks.py).

## Receipt acknowledgement file

ghook spools `<gobby_home>/hooks/inbox/{c|n}-<ts13>-<uuid>.json`; the envelope
ID is the file stem. After a successful 2xx response with a receipt and successful
provider output, ghook atomically replaces that same file with:

```json
{
  "kind": "delivery-receipt",
  "receipt_id": "<receipt UUID>",
  "original_envelope_id": "<envelope file stem>",
  "delivery_generation": 1
}
```

A successful response without a receipt deletes the envelope file. A failed
provider-output write keeps the original envelope. Receipt acknowledgement is
generation-specific; this file is not another hook request.

Authority: [`envelope_filename`](../../crates/ghook/src/transport.rs) and
[`settle_delivered_inbox`](../../crates/ghook/src/dispatch.rs).

## Node forwarding

[Node-halves plan section 1.5](../../.gobby/plans/node-halves.md#15-node-hook-ingress-arm-category-code-depends-11-12-14)
owns the node forwarding implementation. It requires forwarding ghook's bearer
key and hub bytes, local claim/replay/renewal, receipt sweeping before forwarding,
finalization on hub 2xx, and release of claims on retryable outcomes. Hub 409
`processed` marks the local envelope processed; other failures release it.
A missing envelope ID or below-floor capability passes through without a local
claim. A client disconnect still permits finalization and releases an attached
receipt when delivery fails. Node behavior is proved against a fake hub in that
plan; these hub corpus cases make no node parity claim.

The recorded `hooks` family is `parity: "proxy"`, `origin: "python"`; see the
[HTTP corpus](../../tests/contracts/http/README.md).

_Last verified: 2026-10-14_
