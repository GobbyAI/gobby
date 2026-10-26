use gobby_core::domain_error::{cli, mcp, DomainError, DomainErrorClass};
use serde_json::json;

/// Driver, SQL and connection-string text that must never reach an operator
/// through any transport when the error is classified `internal`.
const DRIVER_SENTINELS: &[&str] = &[
    "connection refused",
    "SELECT * FROM",
    "hunter2",
    "postgres://gobby:hunter2@127.0.0.1:5432/gobby",
];

/// Explicit list so per-class checks cannot pass vacuously against an empty `ALL`.
const CLASSES: [DomainErrorClass; 7] = [
    DomainErrorClass::Validation,
    DomainErrorClass::Missing,
    DomainErrorClass::Forbidden,
    DomainErrorClass::Conflict,
    DomainErrorClass::Capacity,
    DomainErrorClass::Unavailable,
    DomainErrorClass::Internal,
];

fn driver_failure() -> std::io::Error {
    std::io::Error::other(
        "driver error: connection refused; SELECT * FROM tasks WHERE id = $1; \
         postgres://gobby:hunter2@127.0.0.1:5432/gobby",
    )
}

#[test]
fn classes_are_exactly_the_seven_contract_names() {
    let names: Vec<&str> = DomainErrorClass::ALL.iter().map(|c| c.as_str()).collect();

    assert_eq!(
        names,
        [
            "validation",
            "missing",
            "forbidden",
            "conflict",
            "capacity",
            "unavailable",
            "internal",
        ]
    );
}

#[test]
fn error_carries_class_safe_message_and_optional_detail() {
    let err = DomainError::new(DomainErrorClass::Validation, "title is required")
        .with_detail(json!({"field": "title"}));

    assert_eq!(err.class(), DomainErrorClass::Validation);
    assert_eq!(err.message(), "title is required");
    assert_eq!(err.detail(), Some(&json!({"field": "title"})));

    let bare = DomainError::new(DomainErrorClass::Conflict, "already claimed");
    assert_eq!(bare.detail(), None);
}

#[test]
fn cli_exit_code_is_nonzero_and_distinct_per_class() {
    let codes: Vec<i32> = CLASSES
        .iter()
        .map(|class| cli::exit_code(*class))
        .collect();

    assert!(
        codes.iter().all(|code| *code != 0),
        "every class needs a nonzero exit code: {codes:?}"
    );
    let mut unique = codes.clone();
    unique.sort_unstable();
    unique.dedup();
    assert_eq!(
        unique.len(),
        CLASSES.len(),
        "exit codes must be distinct per class: {codes:?}"
    );
}

#[test]
fn cli_stderr_carries_the_safe_message_for_every_class() {
    for class in &CLASSES {
        let message = format!("{} safe message", class.as_str());
        let err = DomainError::new(*class, message.clone());
        let stderr = cli::stderr(&err);

        assert!(
            stderr.contains(&message),
            "{class:?} stderr lacks the safe message: {stderr:?}"
        );
    }
}

#[test]
fn mcp_error_result_marks_error_with_class_and_safe_message_for_every_class() {
    for class in &CLASSES {
        let err = DomainError::new(*class, "safe message");
        let result = mcp::error_result(&err);

        assert_eq!(result["isError"], json!(true), "{class:?} must set isError");
        assert_eq!(
            result["structuredContent"]["class"],
            json!(class.as_str()),
            "{class:?} must carry its class"
        );
        assert_eq!(
            result["structuredContent"]["message"],
            json!("safe message"),
            "{class:?} must carry the safe message"
        );
        let text = result["content"][0]["text"].as_str().unwrap_or_default();
        assert!(
            text.contains("safe message"),
            "{class:?} text content lacks the safe message: {text:?}"
        );
    }
}

#[test]
fn internal_error_hides_driver_text_on_cli_and_mcp() {
    let err = DomainError::internal(driver_failure());
    let stderr = cli::stderr(&err);
    let mcp_text = mcp::error_result(&err).to_string();

    assert_eq!(err.class(), DomainErrorClass::Internal);
    assert!(
        !err.message().is_empty(),
        "internal errors still need a safe message"
    );
    for sentinel in DRIVER_SENTINELS {
        assert!(
            !err.message().contains(sentinel),
            "message leaks {sentinel:?}: {:?}",
            err.message()
        );
        assert!(
            !stderr.contains(sentinel),
            "CLI stderr leaks {sentinel:?}: {stderr:?}"
        );
        assert!(
            !mcp_text.contains(sentinel),
            "MCP payload leaks {sentinel:?}: {mcp_text}"
        );
    }
}

#[test]
fn contract_doc_names_every_class() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../docs/contracts/domain-errors.md"
    );
    let doc = std::fs::read_to_string(path)
        .unwrap_or_else(|err| panic!("{path} must exist as the shared contract: {err}"));

    for class in &CLASSES {
        assert!(
            doc.contains(class.as_str()),
            "docs/contracts/domain-errors.md must name class {}",
            class.as_str()
        );
    }
}
