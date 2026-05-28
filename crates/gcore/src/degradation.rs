//! Shared degradation vocabulary boundary.
//!
//! Degradation types describe partial availability without forcing every
//! command to treat optional service absence as fatal. Detailed contracts live
//! here so lightweight consumers can share the same vocabulary.

use serde::{Deserialize, Serialize};

/// Service availability state, returned alongside results from adapters.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ServiceState {
    /// Service is connected and responding.
    Available,
    /// Service is not configured because no config was found from any source.
    NotConfigured,
    /// Service is configured but could not be reached.
    Unreachable {
        /// Adapter-provided diagnostic message for the failed connection.
        message: String,
    },
}

impl ServiceState {
    /// Returns true when the backing service is connected and responding.
    pub fn is_available(&self) -> bool {
        matches!(self, Self::Available)
    }
}

/// Setup validation issue with actionable guidance.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SetupIssue {
    /// Name of the missing, invalid, or degraded resource.
    pub object_name: String,
    /// Store or service that owns the resource.
    pub store: String,
    /// Structured remediation guidance for callers to render.
    pub guidance: Guidance,
}

/// Structured guidance text for setup issues.
///
/// Callers render these fields; `gobby-core` does not format CLI output.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Guidance {
    /// What is missing or wrong.
    pub problem: String,
    /// What the user should do.
    pub action: String,
    /// Optional command suggestion.
    pub command_hint: Option<String>,
}

/// Fatal errors that prevent a command from completing.
#[derive(Debug, Serialize, Deserialize, thiserror::Error)]
pub enum CoreError {
    /// Configuration was present but invalid for the requested operation.
    #[error("invalid configuration: {0}")]
    InvalidConfig(String),
    /// A service required by this command could not be used.
    #[error("required service unavailable: {service} — {message}")]
    RequiredServiceUnavailable {
        /// Required service name.
        service: String,
        /// Diagnostic message explaining the unavailability.
        message: String,
    },
    /// A write operation failed after validation began.
    #[error("write failed: {0}")]
    WriteFailed(String),
    /// Input could not be parsed or failed integrity checks.
    #[error("corrupted input: {0}")]
    CorruptedInput(String),
}

/// Degradation states for partial results.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum DegradationKind {
    /// An optional service was unavailable during this operation.
    ServiceUnavailable {
        /// Optional service name.
        service: String,
        /// Availability state observed by the caller.
        state: ServiceState,
    },
    /// Search completed with fewer sources than requested.
    PartialSearch {
        /// Source names that contributed results.
        available: Vec<String>,
        /// Source names that could not contribute results.
        unavailable: Vec<String>,
    },
    /// Index data may be stale because of content drift or age thresholds.
    StaleIndex {
        /// Paths whose indexed data may be stale.
        paths: Vec<String>,
    },
    /// Some artifacts were skipped during indexing.
    SkippedArtifacts {
        /// Number of skipped artifacts.
        count: usize,
        /// Human-readable reason the artifacts were skipped.
        reason: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn optional_service_degradation_is_not_fatal() {
        let unconfigured = ServiceState::NotConfigured;
        let unreachable = ServiceState::Unreachable {
            message: "connection refused".to_string(),
        };

        assert!(!unconfigured.is_available());
        assert!(!unreachable.is_available());

        let degradation = DegradationKind::ServiceUnavailable {
            service: "qdrant".to_string(),
            state: unconfigured,
        };
        let fatal = CoreError::RequiredServiceUnavailable {
            service: "postgres".to_string(),
            message: "hub is required for writes".to_string(),
        };

        assert!(matches!(
            degradation,
            DegradationKind::ServiceUnavailable {
                service,
                state: ServiceState::NotConfigured
            } if service == "qdrant"
        ));
        assert_eq!(
            fatal.to_string(),
            "required service unavailable: postgres — hub is required for writes"
        );
    }

    #[test]
    fn guidance_is_structured() {
        let guidance = Guidance {
            problem: "BM25 index missing".to_string(),
            action: "run attached setup validation".to_string(),
            command_hint: Some("gobby setup validate".to_string()),
        };

        assert_eq!(guidance.problem, "BM25 index missing");
        assert_eq!(guidance.action, "run attached setup validation");
        assert_eq!(
            guidance.command_hint.as_deref(),
            Some("gobby setup validate")
        );
    }

    #[test]
    fn core_error_serialization_roundtrip() {
        let invalid_config = CoreError::InvalidConfig("missing project id".to_string());
        let encoded = serde_json::to_string(&invalid_config).expect("serialize invalid config");
        let decoded: CoreError =
            serde_json::from_str(&encoded).expect("deserialize invalid config");
        assert!(matches!(
            decoded,
            CoreError::InvalidConfig(message) if message == "missing project id"
        ));

        let unavailable = CoreError::RequiredServiceUnavailable {
            service: "postgres".to_string(),
            message: "connection refused".to_string(),
        };
        let encoded = serde_json::to_string(&unavailable).expect("serialize unavailable");
        let decoded: CoreError = serde_json::from_str(&encoded).expect("deserialize unavailable");
        assert!(matches!(
            decoded,
            CoreError::RequiredServiceUnavailable { service, message }
                if service == "postgres" && message == "connection refused"
        ));
    }
}
