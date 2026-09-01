//! Configuration resolution for gcode.

mod context;
mod layers;
mod runtime_contract;
mod services;

#[cfg(test)]
mod tests;

pub use context::{
    CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorConfigError, CodeVectorSettings, Context,
    EmbeddingConfig, FALKORDB_GRAPH_NAME, FalkorConfig, ProjectIdentitySource, ProjectIndexScope,
    QdrantConfig, ServiceConfigSelection, detect_project_root, detect_project_root_from,
    resolve_project_identity, warn_project_identity,
};

pub(crate) use context::validate_parent_code_index;
pub(crate) use layers::read_config_layers;
pub(crate) use services::{EmbeddingConfigDetails, resolve_embedding_config_details};

#[cfg(test)]
pub(crate) use services::resolve_embedding_config_from_source;
