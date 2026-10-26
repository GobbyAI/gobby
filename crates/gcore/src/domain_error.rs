//! Shared domain-error classification and transport adapters.
//!
//! Behavior-free stubs for the test-first pass: every item compiles so the
//! contract tests in `crates/gcore/tests/domain_error*.rs` run and fail on
//! assertions. The Developer replaces these bodies.

use std::error::Error;

use serde_json::Value;

/// The seven classes every family maps its domain errors onto.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DomainErrorClass {
    Validation,
    Missing,
    Forbidden,
    Conflict,
    Capacity,
    Unavailable,
    Internal,
}

impl DomainErrorClass {
    pub const ALL: &[DomainErrorClass] = &[];

    pub fn as_str(self) -> &'static str {
        ""
    }
}

/// A classified error with a safe operator-facing message and optional detail.
#[derive(Debug, Clone, PartialEq)]
pub struct DomainError {
    class: DomainErrorClass,
    message: String,
    detail: Option<Value>,
}

impl DomainError {
    pub fn new(class: DomainErrorClass, message: impl Into<String>) -> Self {
        Self {
            class,
            message: message.into(),
            detail: None,
        }
    }

    pub fn with_detail(mut self, detail: Value) -> Self {
        self.detail = Some(detail);
        self
    }

    /// Builds an `Internal` error. The source is never exposed by any adapter.
    pub fn internal(source: impl Error + Send + Sync + 'static) -> Self {
        let _ = source;
        Self {
            class: DomainErrorClass::Internal,
            message: String::new(),
            detail: None,
        }
    }

    pub fn class(&self) -> DomainErrorClass {
        self.class
    }

    pub fn message(&self) -> &str {
        ""
    }

    pub fn detail(&self) -> Option<&Value> {
        self.detail.as_ref()
    }
}

/// CLI adapter: exit code per class plus the safe message on stderr.
pub mod cli {
    use super::{DomainError, DomainErrorClass};

    pub fn exit_code(_class: DomainErrorClass) -> i32 {
        0
    }

    pub fn stderr(_err: &DomainError) -> String {
        String::new()
    }
}

/// MCP adapter: the proxy's error result shape with the class and safe message.
pub mod mcp {
    use serde_json::Value;

    use super::DomainError;

    pub fn error_result(_err: &DomainError) -> Value {
        Value::Null
    }
}

/// HTTP adapter: status per class plus a JSON envelope. The default envelope is
/// `{"detail": <safe message>}`; a family may declare its own envelope.
#[cfg(feature = "route-family")]
pub mod http {
    use axum::http::StatusCode;
    use serde_json::Value;

    use super::{DomainError, DomainErrorClass};

    pub fn status(_class: DomainErrorClass) -> StatusCode {
        StatusCode::OK
    }

    pub fn parts(_err: &DomainError) -> (StatusCode, Value) {
        (StatusCode::OK, Value::Null)
    }

    pub fn parts_with<F>(_err: &DomainError, _envelope: F) -> (StatusCode, Value)
    where
        F: FnOnce(&DomainError) -> Value,
    {
        (StatusCode::OK, Value::Null)
    }
}
