//! Shared configuration-resolution boundary.
//!
//! This module is the public home for lightweight configuration contracts that
//! are shared across Gobby Rust crates. Concrete service resolution is added in
//! focused follow-up modules so this baseline crate remains small.

mod daemon_source;
mod machine_config;
mod resolve;
mod runtime_contract;
mod types;

/// FalkorDB graph name owned by the gcode code graph projection.
pub const CODE_GRAPH_NAME: &str = "gobby_code";

pub use daemon_source::{DaemonOrPrimary, DaemonServedConfig};
pub use machine_config::{MachineConfig, RUNTIME_CONFIG_PATH, fetch_machine_config};
pub use resolve::{
    ConfigSource, EnvOnlySource, INDEXING_EXTRA_EXCLUDES_KEY, INDEXING_RESPECT_GITIGNORE_KEY,
    LayeredConfigSource, contains_secret_marker, decode_config_value, reject_secret_marker,
    resolve_ai_setting, resolve_ai_tuning, resolve_capability_binding, resolve_capability_routing,
    resolve_embedding_config, resolve_embedding_config_from_binding,
    resolve_embedding_config_resolution, resolve_env_pattern, resolve_falkordb_config,
    resolve_indexing_config, resolve_indexing_config_from_source, resolve_qdrant_config,
    secret_marker_prefix,
};
pub use runtime_contract::{
    CodecVector, DynamicSegmentError, decode_dynamic_segment, encode_dynamic_segment,
    invalid_dynamic_segments, is_machine_config_key, is_registered_runtime_key,
    is_secret_reference_key, runtime_contract_codec_vectors,
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
