//! Shared domain-error classification and transport adapters.
//!
//! Every family maps its domain errors onto [`DomainErrorClass`] and renders them
//! through the adapter for its transport. The contract lives in
//! `docs/contracts/domain-errors.md`.

use std::error::Error;
use std::fmt;

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
    pub const ALL: &[DomainErrorClass] = &[
        DomainErrorClass::Validation,
        DomainErrorClass::Missing,
        DomainErrorClass::Forbidden,
        DomainErrorClass::Conflict,
        DomainErrorClass::Capacity,
        DomainErrorClass::Unavailable,
        DomainErrorClass::Internal,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            DomainErrorClass::Validation => "validation",
            DomainErrorClass::Missing => "missing",
            DomainErrorClass::Forbidden => "forbidden",
            DomainErrorClass::Conflict => "conflict",
            DomainErrorClass::Capacity => "capacity",
            DomainErrorClass::Unavailable => "unavailable",
            DomainErrorClass::Internal => "internal",
        }
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

    /// Builds an `Internal` error with a fixed safe message. The source is dropped
    /// here, so no adapter ever sees driver text; callers log it before converting.
    pub fn internal(_source: impl Error + Send + Sync + 'static) -> Self {
        Self {
            class: DomainErrorClass::Internal,
            message: "an internal error occurred".to_string(),
            detail: None,
        }
    }

    pub fn class(&self) -> DomainErrorClass {
        self.class
    }

    pub fn message(&self) -> &str {
        &self.message
    }

    pub fn detail(&self) -> Option<&Value> {
        self.detail.as_ref()
    }
}

impl fmt::Display for DomainError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl Error for DomainError {}

/// CLI adapter: exit code per class plus the safe message on stderr.
pub mod cli {
    use super::{DomainError, DomainErrorClass};

    pub fn exit_code(class: DomainErrorClass) -> i32 {
        match class {
            DomainErrorClass::Validation => 2,
            DomainErrorClass::Missing => 3,
            DomainErrorClass::Forbidden => 4,
            DomainErrorClass::Conflict => 5,
            DomainErrorClass::Capacity => 6,
            DomainErrorClass::Unavailable => 7,
            DomainErrorClass::Internal => 8,
        }
    }

    pub fn stderr(err: &DomainError) -> String {
        format!("error: {}\n", err.message())
    }
}

/// MCP adapter: the proxy's error result shape with the class and safe message.
pub mod mcp {
    use serde_json::{Value, json};

    use super::DomainError;

    pub fn error_result(err: &DomainError) -> Value {
        let mut structured = json!({
            "class": err.class().as_str(),
            "message": err.message(),
        });
        if let Some(detail) = err.detail() {
            structured["detail"] = detail.clone();
        }
        json!({
            "isError": true,
            "content": [{"type": "text", "text": err.message()}],
            "structuredContent": structured,
        })
    }
}

/// HTTP adapter: status per class plus a JSON envelope. The default envelope is
/// `{"detail": <safe message>}`; a family may declare its own envelope.
#[cfg(feature = "route-family")]
pub mod http {
    use axum::http::StatusCode;
    use serde_json::{Value, json};

    use super::{DomainError, DomainErrorClass};

    pub fn status(class: DomainErrorClass) -> StatusCode {
        match class {
            DomainErrorClass::Validation => StatusCode::BAD_REQUEST,
            DomainErrorClass::Missing => StatusCode::NOT_FOUND,
            DomainErrorClass::Forbidden => StatusCode::FORBIDDEN,
            DomainErrorClass::Conflict => StatusCode::CONFLICT,
            DomainErrorClass::Capacity => StatusCode::SERVICE_UNAVAILABLE,
            DomainErrorClass::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
            DomainErrorClass::Internal => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    pub fn parts(err: &DomainError) -> (StatusCode, Value) {
        (status(err.class()), json!({"detail": err.message()}))
    }

    pub fn parts_with<F>(err: &DomainError, envelope: F) -> (StatusCode, Value)
    where
        F: FnOnce(&DomainError) -> Value,
    {
        (status(err.class()), envelope(err))
    }
}
