use super::contracts::DEFAULT_SCHEMA;
use serde::{Deserialize, Serialize};

#[derive(Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Redacted(Option<String>);

impl Redacted {
    pub fn new(value: Option<String>) -> Self {
        Self(value)
    }

    pub fn as_deref(&self) -> Option<&str> {
        self.0.as_deref()
    }

    pub fn is_some(&self) -> bool {
        self.0.is_some()
    }

    pub fn clone_inner(&self) -> Option<String> {
        self.0.clone()
    }
}

impl From<Option<String>> for Redacted {
    fn from(value: Option<String>) -> Self {
        Self::new(value)
    }
}

impl std::fmt::Debug for Redacted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.0 {
            Some(_) => f.write_str("<redacted>"),
            None => f.write_str("None"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub standalone: bool,
    /// Setup-only PostgreSQL URL. It is passed to the live connection path,
    /// redacted from `Debug`, and never persisted to JSON output.
    #[serde(skip_serializing, default)]
    pub database_url: Redacted,
    pub no_services: bool,
    pub overwrite_code_index: bool,
    pub schema: String,
    pub embedding_provider: Option<String>,
    pub embedding_api_base: Option<String>,
    pub embedding_model: Option<String>,
    pub embedding_query_prefix: Option<String>,
    pub embedding_vector_dim: Option<usize>,
    /// Setup-only embedding secret. It is redacted from `Debug`; standalone
    /// setup persists it only to the user's local gcore.yaml.
    #[serde(skip_serializing, default)]
    pub embedding_api_key: Redacted,
    pub falkordb_host: Option<String>,
    pub falkordb_port: Option<u16>,
    /// Setup-only FalkorDB secret. It is used during provisioning, redacted
    /// from `Debug`, and never persisted to JSON output.
    #[serde(skip_serializing, default)]
    pub falkordb_password: Redacted,
    pub qdrant_url: Option<String>,
}

impl StandaloneSetupRequest {
    pub fn new(standalone: bool, database_url: Option<String>, schema: Option<String>) -> Self {
        Self {
            standalone,
            database_url: Redacted::new(database_url),
            no_services: false,
            overwrite_code_index: false,
            schema: schema.unwrap_or_else(|| DEFAULT_SCHEMA.to_string()),
            embedding_provider: None,
            embedding_api_base: None,
            embedding_model: None,
            embedding_query_prefix: None,
            embedding_vector_dim: None,
            embedding_api_key: Redacted::default(),
            falkordb_host: None,
            falkordb_port: None,
            falkordb_password: Redacted::default(),
            qdrant_url: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standalone_setup_request_debug_redacts_secrets() {
        let mut request = StandaloneSetupRequest::new(
            true,
            Some("postgres://user:secret-db@localhost/gobby".to_string()),
            None,
        );
        request.embedding_api_key = Some("secret-embedding-key".to_string()).into();
        request.falkordb_password = Some("secret-falkor-password".to_string()).into();

        let debug = format!("{request:?}");

        assert!(debug.contains("<redacted>"));
        assert!(!debug.contains("secret-db"));
        assert!(!debug.contains("secret-embedding-key"));
        assert!(!debug.contains("secret-falkor-password"));
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneServicesStatus {
    pub provisioned: bool,
    pub compose_file: Option<String>,
    pub health_checks: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneEmbeddingStatus {
    pub provider: String,
    pub api_base: String,
    pub model: String,
    pub query_prefix: Option<String>,
    pub vector_dim: usize,
    pub api_key_present: bool,
    pub api_key_fingerprint: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneFailure {
    pub name: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupStatus {
    pub namespace: String,
    pub schema: String,
    pub created: Vec<String>,
    pub skipped: Vec<String>,
    pub failed: Vec<StandaloneFailure>,
    pub config_file: Option<String>,
    pub services: Option<StandaloneServicesStatus>,
    pub embedding: Option<StandaloneEmbeddingStatus>,
}
