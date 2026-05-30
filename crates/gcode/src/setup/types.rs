use super::contracts::DEFAULT_SCHEMA;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StandaloneSetupRequest {
    pub standalone: bool,
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
    pub falkordb_password: Option<String>,
    pub qdrant_url: Option<String>,
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
