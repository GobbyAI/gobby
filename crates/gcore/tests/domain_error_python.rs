//! The Python daemon's fixed error bodies and the coded envelope.

use axum::http::StatusCode;
use gobby_core::domain_error::http::{self, python};
use gobby_core::domain_error::{DomainError, DomainErrorClass};
use serde_json::{Value, json};

const DRIVER_TEXT: &str = "driver error: connection refused; postgres://gobby:hunter2@db/gobby";

fn driver_failure() -> std::io::Error {
    std::io::Error::other(DRIVER_TEXT)
}

#[test]
fn default_envelope_is_fastapi_detail() {
    let case: Value = serde_json::from_str(include_str!(
        "../../../tests/contracts/http/hooks_execute_bad_schema.json"
    ))
    .expect("corpus case is JSON");
    let response = &case["response"];
    let message = response["body"]["detail"]
        .as_str()
        .expect("corpus body has a detail string");

    let (status, body) = http::parts(&DomainError::new(DomainErrorClass::Validation, message));

    assert_eq!(
        Some(u64::from(status.as_u16())),
        response["status"].as_u64()
    );
    assert_eq!(body, response["body"]);
}

#[test]
fn python_internal_500_body() {
    let err = DomainError::internal(driver_failure());

    let (status, body) = python::internal_500(&err);

    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(
        body,
        json!({"status": "error", "message": "Internal server error", "error_logged": true})
    );
    let rendered = body.to_string();
    for fragment in ["connection refused", "hunter2", err.message()] {
        assert!(
            !rendered.contains(fragment),
            "{rendered} carries {fragment}"
        );
    }
}

#[test]
fn python_hub_unavailable_body() {
    assert_eq!(
        python::hub_unavailable(),
        (
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"status": "error", "message": "Hub temporarily unavailable"})
        )
    );
}

#[test]
fn python_hook_acknowledged_is_200_for_internal_only() {
    let internal = DomainError::internal(driver_failure());
    assert_eq!(
        python::hook_acknowledged(&internal),
        Some((
            StatusCode::OK,
            json!({
                "status": "error",
                "message": "Internal error occurred but request acknowledged",
                "error_logged": true
            })
        ))
    );
    for class in DomainErrorClass::ALL
        .iter()
        .copied()
        .filter(|class| *class != DomainErrorClass::Internal)
    {
        let err = DomainError::new(class, "not acknowledged");
        assert_eq!(python::hook_acknowledged(&err), None, "{class:?}");
    }
}

#[test]
fn coded_envelope_takes_class_status() {
    let forbidden = DomainError::new(DomainErrorClass::Forbidden, "x");
    assert_eq!(
        http::coded(&forbidden, "forbidden_principal"),
        (
            StatusCode::FORBIDDEN,
            json!({"error": "x", "code": "forbidden_principal"})
        )
    );
    let missing = DomainError::new(DomainErrorClass::Missing, "Machine not found");
    assert_eq!(
        http::coded(&missing, "machine_not_found"),
        (
            StatusCode::NOT_FOUND,
            json!({"error": "Machine not found", "code": "machine_not_found"})
        )
    );
}
