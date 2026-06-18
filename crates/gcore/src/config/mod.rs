//! Shared configuration-resolution boundary.
//!
//! This module is the public home for lightweight configuration contracts that
//! are shared across Gobby Rust crates. Concrete service resolution is added in
//! focused follow-up modules so this baseline crate remains small.

mod resolve;
mod types;

/// FalkorDB graph name owned by the gcode code graph projection.
pub const CODE_GRAPH_NAME: &str = "gobby_code";

pub use resolve::{
    ConfigSource, EnvOnlySource, INDEXING_RESPECT_GITIGNORE_KEY, LayeredConfigSource,
    decode_config_value, resolve_ai_tuning, resolve_capability_binding, resolve_capability_routing,
    resolve_embedding_config, resolve_embedding_config_from_binding,
    resolve_embedding_config_resolution, resolve_env_pattern, resolve_falkordb_config,
    resolve_indexing_config, resolve_qdrant_config,
};
pub use types::{
    AiCapability, AiRouting, AiTuning, CapabilityBinding, EmbeddingConfig,
    EmbeddingConfigResolution, FalkorConfig, FeatureCandidate, IndexingConfig,
    ParseAiCapabilityError, ParseAiRoutingError, QdrantConfig, ai_keys, embedding_keys,
};

#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
mod tests;
