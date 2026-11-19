# Domain Error Contract

Status: normative for the native daemon families and the native MCP gateway (#24884).

## Owner

The shared classification lives in the `gobby-core` crate (`crates/gcore`) as
module `gobby_core::domain_error`. `gobby-core` is the owner because both gdaemon
route families and the native MCP gateway already depend on it; `gruntime` is not
a dependency of either.

The HTTP adapter is compiled only with the `route-family` feature, which keeps the
`axum` dependency out of crates that do not serve HTTP (for example `ghook`).

## Classes

Every domain error carries exactly one of seven classes. The lowercase name is the
wire identifier on every transport.

| Class (`DomainErrorClass`) | Name          | HTTP status | CLI exit code |
|----------------------------|---------------|-------------|---------------|
| `Validation`               | `validation`  | 400         | 2             |
| `Missing`                  | `missing`     | 404         | 3             |
| `Forbidden`                | `forbidden`   | 403         | 4             |
| `Conflict`                 | `conflict`    | 409         | 5             |
| `Capacity`                 | `capacity`    | 503         | 6             |
| `Unavailable`              | `unavailable` | 503         | 7             |
| `Internal`                 | `internal`    | 500         | 8             |

`DomainErrorClass::ALL` lists the seven classes in this order.

## Error value

`DomainError` holds:

- `class`: one `DomainErrorClass`.
- `message`: a safe, operator-facing string. It must never contain driver text, SQL,
  connection strings or credentials.
- `detail`: optional structured JSON, such as the offending field name. Absent
  unless the family sets it with `with_detail`. `Internal` errors never carry
  detail: `with_detail` is a no-op for that class.

`DomainError::internal(source)` builds an `Internal` error with the fixed message
`an internal error occurred`. It drops `source`, so no adapter can render driver
text. Callers log the source before converting it. `DomainError::new(Internal,
message)` renders the caller's message as given; the caller owns its safety.

## Transport adapters

Each adapter maps all seven classes. A family never defines its own exception
framework or transport mapping; it maps its domain errors onto the classes and
calls the adapters.

### HTTP (`gobby_core::domain_error::http`, feature `route-family`)

- `status(class)` returns the status from the table above.
- `parts(err)` returns `(status, {"detail": <message>})`. This is the default
  envelope, matching the existing `{"detail": ...}` error bodies.
- `parts_with(err, envelope)` returns `(status(err.class()), envelope(err))`. A
  family that keeps an intentional existing envelope declares it here. The
  declaration replaces only the body; the status still comes from the class.
- `coded(err, code)` returns `(status(err.class()), {"error": <message>, "code":
  <code>})`, the coded envelope of the Python auth middleware and of the native
  machines routes.

#### Python-parity presets (`http::python`)

These render the fixed bodies of the Python daemon's global exception handler
(`src/gobby/servers/exception_handlers.py`), for routes whose oracle is that daemon:

- `internal_500(err)` returns `(500, {"status": "error", "message": "Internal server
  error", "error_logged": true})`. The error is never rendered; the caller logs it
  first.
- `hub_unavailable()` returns `(503, {"status": "error", "message": "Hub temporarily
  unavailable"})`, the hub pool-outage body.
- `hook_acknowledged(err)` returns `Some((200, {"status": "error", "message":
  "Internal error occurred but request acknowledged", "error_logged": true}))` for an
  `Internal` error and `None` for every other class. It is the only status that does
  not come from the class, and it applies only to the `/api/hooks` routes, whose
  oracle acknowledges an internal failure instead of failing the hook.

#### Converted routes

The machines routes render their 503 `machines_unavailable` (class `Unavailable`) and
404 `machine_not_found` (class `Missing`) answers through `coded`; their bodies and the
503 retry hint are unchanged. Their 401 `missing_auth` and 405 `method_not_allowed`
answers stay outside the classes. Other families adopt the adapters when they port.

#### Not domain errors

401 authentication rejections, 405 method answers, 413 body limits, 502 upstream
failures and the 503 backend-state body are transport statuses. They stay with their
transports and are not classified.

### MCP (`gobby_core::domain_error::mcp`)

`error_result(err)` returns the proxy's error result shape:

```json
{
  "isError": true,
  "content": [{"type": "text", "text": "<message>"}],
  "structuredContent": {"class": "<name>", "message": "<message>", "detail": "<optional>"}
}
```

`detail` appears only when the error has one.

### CLI (`gobby_core::domain_error::cli`)

- `exit_code(class)` returns the code from the table above. Every code is nonzero and
  distinct per class.
- `stderr(err)` returns `error: <message>` followed by a newline. The message is the
  only text written.

## Wire changes versus the Python oracle

A family that changes a wire response relative to the Python oracle records the change
as an exact #24963 waiver in its own contract. This contract adds no family waivers.

422 has no class: `Validation` is 400. A family whose Python oracle answers 422 (an
explicit 422 or FastAPI's request-validation answer) records an exact entry for that
route in `docs/contracts/parity-waivers.json` and tests the waived 400. The runtime
handshake route in `gobby-runtime` answers its invalid-request 422 directly, outside
the classes.
