use super::contracts::DEFAULT_SCHEMA;
use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub standalone: bool,
    /// Setup-only PostgreSQL URL. It is passed to the live connection path,
    /// redacted from `Debug`, and never persisted to JSON output.
    #[serde(skip_serializing)]
    pub database_url: Option<String>,
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
    #[serde(skip_serializing)]
    pub embedding_api_key: Option<String>,
    pub falkordb_host: Option<String>,
    pub falkordb_port: Option<u16>,
    /// Setup-only FalkorDB secret. It is used during provisioning, redacted
    /// from `Debug`, and never persisted to JSON output.
    #[serde(skip_serializing)]
    pub falkordb_password: Option<String>,
    pub qdrant_url: Option<String>,
}

// Keep this Debug impl in sync when StandaloneSetupRequest fields change; setup
// secrets must stay redacted.
impl fmt::Debug for StandaloneSetupRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StandaloneSetupRequest")
            .field("standalone", &self.standalone)
            .field(
                "database_url",
                &self.database_url.as_ref().map(|_| "<redacted>"),
            )
            .field("no_services", &self.no_services)
            .field("overwrite_code_index", &self.overwrite_code_index)
            .field("schema", &self.schema)
            .field("embedding_provider", &self.embedding_provider)
            .field("embedding_api_base", &self.embedding_api_base)
            .field("embedding_model", &self.embedding_model)
            .field("embedding_query_prefix", &self.embedding_query_prefix)
            .field("embedding_vector_dim", &self.embedding_vector_dim)
            .field(
                "embedding_api_key",
                &self.embedding_api_key.as_ref().map(|_| "<redacted>"),
            )
            .field("falkordb_host", &self.falkordb_host)
            .field("falkordb_port", &self.falkordb_port)
            .field(
                "falkordb_password",
                &self.falkordb_password.as_ref().map(|_| "<redacted>"),
            )
            .field("qdrant_url", &self.qdrant_url)
            .finish()
    }
}

impl StandaloneSetupRequest {
    pub fn new(standalone: bool, database_url: Option<String>, schema: Option<String>) -> Self {
        Self {
            standalone,
            database_url,
            no_services: false,
            overwrite_code_index: false,
            schema: schema.unwrap_or_else(|| DEFAULT_SCHEMA.to_string()),
            embedding_provider: None,
            embedding_api_base: None,
            embedding_model: None,
            embedding_query_prefix: None,
            embedding_vector_dim: None,
            embedding_api_key: None,
            falkordb_host: None,
            falkordb_port: None,
            falkordb_password: None,
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
        request.embedding_api_key = Some("secret-embedding-key".to_string());
        request.falkordb_password = Some("secret-falkor-password".to_string());

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
