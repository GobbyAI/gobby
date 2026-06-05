//! Shared degradation vocabulary boundary.
//!
//! Degradation types describe partial availability without forcing every
//! command to treat configured-service outages or explicitly degraded paths as
//! fatal. Detailed contracts live here so lightweight consumers can share the
//! same vocabulary.

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
    /// Two reachable hub DSNs point at different PostgreSQL clusters or databases.
    #[error(
        "conflicting Gobby PostgreSQL hubs: existing recorded hub identifies {existing_identity}; daemon hub identifies {daemon_identity}"
    )]
    HubConflict {
        /// DSN from the existing standalone/subset configuration.
        #[serde(serialize_with = "serialize_redacted_database_url")]
        existing_database_url: String,
        /// Cluster/database identity observed for the existing DSN.
        existing_identity: String,
        /// DSN reported by the daemon bootstrap/broker.
        #[serde(serialize_with = "serialize_redacted_database_url")]
        daemon_database_url: String,
        /// Cluster/database identity observed for the daemon DSN.
        daemon_identity: String,
    },
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

fn serialize_redacted_database_url<S>(database_url: &str, serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    serializer.serialize_str(&redact_database_url(database_url))
}

pub fn redact_database_url(database_url: &str) -> String {
    let without_fragment = database_url
        .split_once('#')
        .map_or(database_url, |(head, _)| head);
    let without_query = without_fragment
        .split_once('?')
        .map_or(without_fragment, |(head, _)| head);
    if let Some((scheme, rest)) = without_query.split_once("://") {
        let host_and_path = rest
            .rsplit_once('@')
            .map_or(rest, |(_, host_and_path)| host_and_path);
        format!("{scheme}://{host_and_path}")
    } else {
        redact_keyword_database_url(without_query)
    }
}

fn redact_keyword_database_url(database_url: &str) -> String {
    split_keyword_dsn_tokens(database_url)
        .into_iter()
        .map(|token| {
            let Some((key, _value)) = token.split_once('=') else {
                return token.to_string();
            };
            if is_sensitive_keyword_dsn_key(key) {
                format!("{key}=<redacted>")
            } else {
                token.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn split_keyword_dsn_tokens(database_url: &str) -> Vec<&str> {
    let mut tokens = Vec::new();
    let mut start = None;
    let mut in_single_quote = false;
    let mut escaped = false;

    for (index, ch) in database_url.char_indices() {
        if start.is_none() {
            if ch.is_whitespace() {
                continue;
            }
            start = Some(index);
        }

        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        if ch == '\'' {
            in_single_quote = !in_single_quote;
            continue;
        }
        if ch.is_whitespace()
            && !in_single_quote
            && let Some(token_start) = start.take()
        {
            tokens.push(&database_url[token_start..index]);
        }
    }

    if let Some(token_start) = start {
        tokens.push(&database_url[token_start..]);
    }
    tokens
}

fn is_sensitive_keyword_dsn_key(key: &str) -> bool {
    matches!(
        key.to_ascii_lowercase().as_str(),
        "password" | "passfile" | "sslpassword"
    )
}

/// Degradation states for partial results.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum DegradationKind {
    /// A configured service was unavailable during this operation.
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
    /// Operation completed with capped or otherwise incomplete data.
    PartialData {
        /// Component whose data was incomplete.
        component: String,
        /// Human-readable detail about the partial data.
        message: String,
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
    fn service_unavailable_degradation_is_not_fatal() {
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

        let hub_conflict = CoreError::HubConflict {
            existing_database_url: "postgres://existing".to_string(),
            existing_identity: "existing-cluster/existing-db".to_string(),
            daemon_database_url: "postgres://daemon".to_string(),
            daemon_identity: "daemon-cluster/daemon-db".to_string(),
        };
        let encoded = serde_json::to_string(&hub_conflict).expect("serialize hub conflict");
        let decoded: CoreError = serde_json::from_str(&encoded).expect("deserialize hub conflict");
        assert!(matches!(
            decoded,
            CoreError::HubConflict {
                existing_database_url,
                existing_identity,
                daemon_database_url,
                daemon_identity,
            } if existing_database_url == "postgres://existing"
                && existing_identity == "existing-cluster/existing-db"
                && daemon_database_url == "postgres://daemon"
                && daemon_identity == "daemon-cluster/daemon-db"
        ));
    }

    #[test]
    fn hub_conflict_display_and_json_redact_database_urls() {
        let conflict = CoreError::HubConflict {
            existing_database_url: "postgresql://user:secret@standalone/gobby?sslmode=require#frag"
                .to_string(),
            existing_identity: "cluster-a/gobby".to_string(),
            daemon_database_url: "postgresql://daemon:secret@daemon/gobby?application_name=gobby"
                .to_string(),
            daemon_identity: "cluster-b/gobby".to_string(),
        };

        let message = conflict.to_string();
        assert!(message.contains("cluster-a/gobby"));
        assert!(message.contains("cluster-b/gobby"));
        assert!(!message.contains("postgresql://"));
        assert!(!message.contains("secret"));
        assert!(!message.contains("sslmode"));
        assert!(!message.contains("application_name"));

        let encoded = serde_json::to_string(&conflict).expect("serialize hub conflict");
        assert!(encoded.contains("postgresql://standalone/gobby"));
        assert!(encoded.contains("postgresql://daemon/gobby"));
        assert!(!encoded.contains("secret"));
        assert!(!encoded.contains("sslmode"));
        assert!(!encoded.contains("application_name"));
        assert!(!encoded.contains("frag"));
    }

    #[test]
    fn keyword_database_url_redacts_sensitive_values_case_insensitively() {
        let redacted = redact_database_url(
            "host=localhost user=app PASSWORD='secret value' dbname=gobby sslpassword=topsecret",
        );

        assert!(redacted.contains("host=localhost"));
        assert!(redacted.contains("user=app"));
        assert!(redacted.contains("dbname=gobby"));
        assert!(redacted.contains("PASSWORD=<redacted>"));
        assert!(redacted.contains("sslpassword=<redacted>"));
        assert!(!redacted.contains("secret value"));
        assert!(!redacted.contains("topsecret"));
    }

    #[test]
    fn hub_conflict_json_redacts_keyword_database_urls() {
        let conflict = CoreError::HubConflict {
            existing_database_url: "host=standalone user=app password=secret dbname=gobby"
                .to_string(),
            existing_identity: "cluster-a/gobby".to_string(),
            daemon_database_url: "HOST=daemon USER=daemon PASSFILE='/tmp/pgpass' dbname=gobby"
                .to_string(),
            daemon_identity: "cluster-b/gobby".to_string(),
        };

        let encoded = serde_json::to_string(&conflict).expect("serialize hub conflict");

        assert!(encoded.contains("host=standalone"));
        assert!(encoded.contains("password=<redacted>"));
        assert!(encoded.contains("PASSFILE=<redacted>"));
        assert!(!encoded.contains("secret"));
        assert!(!encoded.contains("/tmp/pgpass"));
    }
}
