use gobby_core::ai_context::AiContext;
use gobby_core::config::embedding_keys;
use gobby_core::config::{AiCapability, AiRouting, CapabilityBinding, ConfigSource};
use gobby_core::provisioning::{GCORE_CONFIG_FILENAME, StandaloneConfig};
use postgres::Client;
use std::collections::HashMap;
use std::fmt;
use std::path::PathBuf;

use super::{
    CodeVectorConfigError, CodeVectorSettings, FALKORDB_GRAPH_NAME, FalkorConfig, QdrantConfig,
};
use crate::{db, secrets};

struct PostgresConfigSource<'a> {
    conn: &'a mut Client,
}

impl ConfigSource for PostgresConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        gobby_core::postgres::read_config_value(self.conn, key)
            .ok()
            .flatten()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        secrets::resolve_config_value(value, self.conn)
    }
}

struct FallbackConfigSource<'a> {
    postgres: PostgresConfigSource<'a>,
    standalone: Option<StandaloneConfig>,
}

impl ConfigSource for FallbackConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.postgres.config_value(key).or_else(|| {
            self.standalone
                .as_mut()
                .and_then(|standalone| standalone.config_value(key))
        })
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.postgres.resolve_value(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EmbeddingConfigDetails {
    pub config: super::EmbeddingConfig,
    pub namespace: &'static str,
    pub source: &'static str,
}

struct TracingFallbackConfigSource<'a> {
    postgres: PostgresConfigSource<'a>,
    standalone: Option<StandaloneConfig>,
    hits: HashMap<String, &'static str>,
}

impl TracingFallbackConfigSource<'_> {
    fn hit_source(&self, key: &str) -> Option<&'static str> {
        self.hits.get(key).copied()
    }
}

impl ConfigSource for TracingFallbackConfigSource<'_> {
    fn config_value(&mut self, key: &str) -> Option<String> {
        if let Some(value) = self.postgres.config_value(key) {
            self.hits.insert(key.to_string(), "config_store");
            return Some(value);
        }
        let value = self
            .standalone
            .as_mut()
            .and_then(|standalone| standalone.config_value(key));
        if value.is_some() {
            self.hits.insert(key.to_string(), "gcore.yaml");
        }
        value
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.postgres.resolve_value(value)
    }
}

pub(crate) fn read_standalone_config_optional() -> Option<StandaloneConfig> {
    match read_standalone_config() {
        Ok(config) => Some(config),
        Err(StandaloneConfigReadError::NotFound { .. }) => None,
        Err(error) => {
            log::warn!("{error}");
            None
        }
    }
}

#[derive(Debug)]
pub(crate) enum StandaloneConfigReadError {
    Home {
        source: anyhow::Error,
    },
    NotFound {
        path: PathBuf,
    },
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    Parse {
        path: PathBuf,
        source: anyhow::Error,
    },
}

impl fmt::Display for StandaloneConfigReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Home { source } => {
                write!(
                    f,
                    "failed to resolve Gobby home for standalone config: {source}"
                )
            }
            Self::NotFound { path } => {
                write!(f, "standalone gcode config not found at {}", path.display())
            }
            Self::Read { path, source } => write!(
                f,
                "failed to read standalone gcode config at {}: {source}",
                path.display()
            ),
            Self::Parse { path, source } => write!(
                f,
                "failed to parse standalone gcode config at {}: {source}",
                path.display()
            ),
        }
    }
}

impl std::error::Error for StandaloneConfigReadError {}

pub(crate) fn read_standalone_config() -> Result<StandaloneConfig, StandaloneConfigReadError> {
    let home = db::gobby_home().map_err(|source| StandaloneConfigReadError::Home { source })?;
    let path = home.join(GCORE_CONFIG_FILENAME);
    let contents = std::fs::read_to_string(&path).map_err(|source| {
        if source.kind() == std::io::ErrorKind::NotFound {
            StandaloneConfigReadError::NotFound { path: path.clone() }
        } else {
            StandaloneConfigReadError::Read {
                path: path.clone(),
                source,
            }
        }
    })?;
    StandaloneConfig::from_yaml_str(&contents)
        .map_err(|source| StandaloneConfigReadError::Parse { path, source })
}

#[cfg(test)]
struct ClosureConfigSource<R, S> {
    read_config_value: R,
    resolve_value: S,
}

#[cfg(test)]
impl<R, S> ConfigSource for ClosureConfigSource<R, S>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    fn config_value(&mut self, key: &str) -> Option<String> {
        (self.read_config_value)(key).and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        (self.resolve_value)(value)
    }
}

#[cfg(test)]
pub(super) fn resolve_falkordb_config_from_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> Option<FalkorConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    resolve_falkordb_config_from_source(&mut source)
}

#[cfg(test)]
pub(super) fn resolve_qdrant_config_from_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> Option<QdrantConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    gobby_core::config::resolve_qdrant_config(&mut source)
}

#[cfg(test)]
pub(super) fn resolve_embedding_config_from_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> Option<super::EmbeddingConfig>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    resolve_embedding_config_from_source(None, &mut source)
}

#[cfg(test)]
pub(super) fn resolve_code_vector_settings_from_values<R>(
    read_config_value: R,
) -> Result<CodeVectorSettings, CodeVectorConfigError>
where
    R: FnMut(&str) -> Option<String>,
{
    let mut source = ClosureConfigSource {
        read_config_value,
        resolve_value: |value: &str| Ok(value.to_string()),
    };
    resolve_code_vector_settings_from_source(&mut source)
}

/// Resolve FalkorDB configuration from config_store + env vars.
///
/// `_quiet` is reserved for future verbosity control; config resolution is currently silent.
pub(super) fn resolve_falkordb_config(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    _quiet: bool,
) -> Option<FalkorConfig> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    resolve_falkordb_config_from_source(&mut source)
}

fn resolve_falkordb_config_from_source(source: &mut impl ConfigSource) -> Option<FalkorConfig> {
    let connection = gobby_core::config::resolve_falkordb_config(source)?;

    Some(FalkorConfig {
        host: connection.host,
        port: connection.port,
        password: connection.password,
        graph_name: FALKORDB_GRAPH_NAME.to_string(),
    })
}

/// Resolve Qdrant configuration from config_store + env vars.
///
/// `_quiet` is reserved for future verbosity control; config resolution is currently silent.
pub(super) fn resolve_qdrant_config(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    _quiet: bool,
) -> Option<QdrantConfig> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    gobby_core::config::resolve_qdrant_config(&mut source)
}

/// Resolve embedding API configuration from config_store + gcore.yaml.
///
/// Returns None if no api_base is found (BM25 only).
///
/// `_quiet` is reserved for future verbosity control; config resolution is currently silent.
pub(super) fn resolve_embedding_config(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    _quiet: bool,
) -> Option<super::EmbeddingConfig> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    resolve_embedding_config_from_source(None, &mut source)
}

pub(crate) fn resolve_ai_context(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
    project_id: Option<String>,
) -> AiContext {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    AiContext::resolve(project_id, &mut source)
}

pub(crate) fn resolve_embedding_config_details(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
) -> Option<EmbeddingConfigDetails> {
    let mut source = TracingFallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
        hits: HashMap::new(),
    };
    let config = resolve_embedding_config_from_source(None, &mut source)?;
    let source_name = source
        .hit_source(embedding_keys::AI_API_BASE)
        .unwrap_or("unknown");
    Some(EmbeddingConfigDetails {
        config,
        namespace: embedding_keys::AI_NAMESPACE,
        source: source_name,
    })
}

pub(crate) fn resolve_embedding_config_from_source(
    project_id: Option<String>,
    source: &mut impl ConfigSource,
) -> Option<super::EmbeddingConfig> {
    let context = AiContext::resolve(project_id, source);
    let binding = context.binding(AiCapability::Embed);
    if !embedding_binding_uses_openai_http(binding) || !embedding_binding_routes_direct(binding) {
        return None;
    }
    gobby_core::config::resolve_embedding_config_from_binding(source, binding)
}

fn embedding_binding_routes_direct(binding: &CapabilityBinding) -> bool {
    match binding.routing {
        AiRouting::Off | AiRouting::Daemon => false,
        AiRouting::Auto | AiRouting::Direct => binding
            .api_base
            .as_deref()
            .map(str::trim)
            .is_some_and(|value| !value.is_empty()),
    }
}

fn embedding_binding_uses_openai_http(binding: &CapabilityBinding) -> bool {
    binding
        .transport
        .as_deref()
        .map(str::trim)
        .is_none_or(|transport| transport.is_empty() || transport == "openai_compatible_http")
}

pub(super) fn resolve_code_vector_settings(
    conn: &mut Client,
    standalone: Option<StandaloneConfig>,
) -> Result<CodeVectorSettings, CodeVectorConfigError> {
    let mut source = FallbackConfigSource {
        postgres: PostgresConfigSource { conn },
        standalone,
    };
    resolve_code_vector_settings_from_source(&mut source)
}

pub(super) fn resolve_code_vector_settings_from_source(
    source: &mut impl ConfigSource,
) -> Result<CodeVectorSettings, CodeVectorConfigError> {
    let vector_dim = resolve_vector_dim(source, embedding_keys::AI_DIM)?;

    Ok(CodeVectorSettings { vector_dim })
}

fn resolve_vector_dim(
    source: &mut impl ConfigSource,
    key: &'static str,
) -> Result<Option<usize>, CodeVectorConfigError> {
    source
        .config_value(key)
        .map(|value| parse_vector_dim(key, value.trim()))
        .transpose()
}

fn parse_vector_dim(source: &'static str, value: &str) -> Result<usize, CodeVectorConfigError> {
    value
        .parse::<usize>()
        .ok()
        .filter(|size| *size > 0)
        .ok_or_else(|| CodeVectorConfigError::InvalidVectorDim {
            source,
            value: value.to_string(),
        })
}
