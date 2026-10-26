use gobby_core::domain_error::{mcp, DomainError, DomainErrorClass};
use serde_json::json;

/// Driver, SQL and connection-string text that must never reach an operator
/// through any transport when the error is classified `internal`.
const DRIVER_SENTINELS: &[&str] = &[
    "connection refused",
    "SELECT * FROM",
    "hunter2",
    "postgres://gobby:hunter2@127.0.0.1:5432/gobby",
];

fn driver_failure() -> std::io::Error {
    std::io::Error::other(
        "driver error: connection refused; SELECT * FROM tasks WHERE id = $1; \
         postgres://gobby:hunter2@127.0.0.1:5432/gobby",
    )
}

fn driver_detail() -> serde_json::Value {
    json!({
        "driver": "connection refused",
        "sql": "SELECT * FROM tasks WHERE id = $1",
        "dsn": "postgres://gobby:hunter2@127.0.0.1:5432/gobby",
    })
}

fn assert_internal_carries_no_driver_text(err: &DomainError) {
    assert!(
        err.detail().is_none(),
        "Internal errors must carry no detail: {:?}",
        err.detail()
    );
    let payload = mcp::error_result(err);
    assert!(
        payload["structuredContent"].get("detail").is_none(),
        "MCP structuredContent must omit detail: {payload}"
    );
    let payload = payload.to_string();
    for sentinel in DRIVER_SENTINELS {
        assert!(
            !err.message().contains(sentinel),
            "message leaks {sentinel:?}: {:?}",
            err.message()
        );
        assert!(
            !payload.contains(sentinel),
            "MCP payload leaks {sentinel:?}: {payload}"
        );
    }
}

#[test]
fn internal_from_source_drops_caller_detail() {
    let err = DomainError::internal(driver_failure()).with_detail(driver_detail());

    assert_internal_carries_no_driver_text(&err);
}

#[test]
fn internal_class_ignores_detail_on_caller_message() {
    let err = DomainError::new(DomainErrorClass::Internal, "an internal error occurred")
        .with_detail(driver_detail());

    assert_internal_carries_no_driver_text(&err);
}
