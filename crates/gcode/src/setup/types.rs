use super::contracts::DEFAULT_SCHEMA;
use serde::{Deserialize, Serialize};
use std::fmt;

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub standalone: bool,
    #[serde(skip_serializing)]
    pub database_url: Option<String>,
    pub no_services: bool,
    pub overwrite_code_index: bool,
    pub schema: String,
    pub embedding_provider: Option<String>,
    pub embedding_api_base: Option<String>,
    pub embedding_model: Option<String>,
    pub embedding_vector_dim: Option<usize>,
    pub embedding_api_key_env: Option<String>,
    pub falkordb_host: Option<String>,
    pub falkordb_port: Option<u16>,
    #[serde(skip_serializing)]
    pub falkordb_password: Option<String>,
    pub qdrant_url: Option<String>,
}

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
            .field("embedding_vector_dim", &self.embedding_vector_dim)
            .field("embedding_api_key_env", &self.embedding_api_key_env)
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
            embedding_vector_dim: None,
            embedding_api_key_env: None,
            falkordb_host: None,
            falkordb_port: None,
            falkordb_password: None,
            qdrant_url: None,
        }
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
    pub vector_dim: usize,
    pub api_key_env: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupStatus {
    pub namespace: String,
    pub schema: String,
    pub created: Vec<String>,
    pub skipped: Vec<String>,
    pub failed: Vec<(String, String)>,
    pub config_file: Option<String>,
    pub services: Option<StandaloneServicesStatus>,
    pub embedding: Option<StandaloneEmbeddingStatus>,
}
