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
  unless the family sets it with `with_detail`.

`DomainError::internal(source)` builds an `Internal` error with the fixed message
`an internal error occurred`. It drops `source`, so no adapter can render driver
text. Callers log the source before converting it.

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
