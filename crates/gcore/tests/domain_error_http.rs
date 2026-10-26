#![cfg(feature = "route-family")]

use gobby_core::domain_error::{http, DomainError, DomainErrorClass};
use serde_json::json;

/// Driver, SQL and connection-string text that must never reach an HTTP body
/// when the error is classified `internal`.
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
fn http_status_maps_every_class() {
    let expected = [
        (DomainErrorClass::Validation, 400),
        (DomainErrorClass::Missing, 404),
        (DomainErrorClass::Forbidden, 403),
        (DomainErrorClass::Conflict, 409),
        (DomainErrorClass::Capacity, 503),
        (DomainErrorClass::Unavailable, 503),
        (DomainErrorClass::Internal, 500),
    ];

    assert_eq!(expected.len(), DomainErrorClass::ALL.len());
    for (class, status) in expected {
        assert_eq!(http::status(class).as_u16(), status, "{class:?}");
    }
}

#[test]
fn http_default_envelope_is_detail_with_safe_message_for_every_class() {
    for class in &CLASSES {
        let err = DomainError::new(*class, "safe message");
        let (status, body) = http::parts(&err);

        assert_eq!(status, http::status(*class), "{class:?}");
        assert_eq!(body, json!({"detail": "safe message"}), "{class:?}");
    }
}

#[test]
fn family_declared_envelope_replaces_body_but_keeps_class_status() {
    let err = DomainError::new(DomainErrorClass::Missing, "tool not found");

    let (status, body) = http::parts_with(&err, |e| {
        json!({"error": e.message(), "code": e.class().as_str()})
    });

    assert_eq!(status, http::status(DomainErrorClass::Missing));
    assert_eq!(body, json!({"error": "tool not found", "code": "missing"}));
}

#[test]
fn internal_error_hides_driver_text_in_default_and_declared_envelopes() {
    let err = DomainError::internal(driver_failure());

    assert!(
        !err.message().is_empty(),
        "internal errors still need a safe message"
    );
    let (status, default_body) = http::parts(&err);
    assert_eq!(status, http::status(DomainErrorClass::Internal));
    assert_eq!(default_body, json!({"detail": err.message()}));
    let (_, declared_body) = http::parts_with(&err, |e| {
        json!({"error": e.message(), "code": e.class().as_str()})
    });

    for (label, body) in [("default", default_body), ("declared", declared_body)] {
        let text = body.to_string();
        for sentinel in DRIVER_SENTINELS {
            assert!(
                !text.contains(sentinel),
                "{label} HTTP body leaks {sentinel:?}: {text}"
            );
        }
    }
}
