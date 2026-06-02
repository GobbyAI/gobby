//! Configuration resolution for gcode.

mod context;
mod services;

#[cfg(test)]
mod tests;

pub use context::{
    CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorConfigError, CodeVectorSettings, Context,
    EmbeddingConfig, FALKORDB_GRAPH_NAME, FALKORDB_HOST_CONFIG_KEY, FALKORDB_PASSWORD_CONFIG_KEY,
    FALKORDB_PORT_CONFIG_KEY, FalkorConfig, GOBBY_FALKORDB_HOST_ENV, GOBBY_FALKORDB_PASSWORD_ENV,
    GOBBY_FALKORDB_PORT_ENV, MissingIdentity, ProjectIdentity, ProjectIdentitySource,
    ProjectIndexScope, QdrantConfig, detect_project_root, detect_project_root_from,
    resolve_project_identity, warn_project_identity,
};

pub(crate) use context::{resolve_daemon_url, validate_parent_code_index};
pub(crate) use services::{
    EmbeddingConfigDetails, read_standalone_config_optional, resolve_embedding_config_details,
};

#[cfg(test)]
pub(crate) use services::resolve_embedding_config_from_source;
