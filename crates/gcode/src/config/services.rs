use gobby_core::config::ConfigSource;
use gobby_core::provisioning::{GCORE_CONFIG_FILENAME, StandaloneConfig};
use postgres::Client;

use super::{
    CodeVectorConfigError, CodeVectorSettings, EMBEDDING_VECTOR_DIM_CONFIG_KEY,
    FALKORDB_GRAPH_NAME, FalkorConfig, GOBBY_EMBEDDING_VECTOR_DIM_ENV, QdrantConfig,
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

pub(super) fn read_standalone_config() -> Option<StandaloneConfig> {
    let home = db::gobby_home().ok()?;
    let path = home.join(GCORE_CONFIG_FILENAME);
    match StandaloneConfig::read_at(&path) {
        Ok(config) => config,
        Err(error) => {
            log::warn!(
                "failed to read standalone gcode config at {}: {error}",
                path.display()
            );
            None
        }
    }
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
    gobby_core::config::resolve_embedding_config(&mut source)
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

/// Resolve embedding API configuration from config_store + env vars.
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
    gobby_core::config::resolve_embedding_config(&mut source)
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
    let vector_dim = match std::env::var(GOBBY_EMBEDDING_VECTOR_DIM_ENV)
        .ok()
        .filter(|value| !value.trim().is_empty())
    {
        Some(value) => Some(parse_vector_dim(
            GOBBY_EMBEDDING_VECTOR_DIM_ENV,
            value.trim(),
        )?),
        None => source
            .config_value(EMBEDDING_VECTOR_DIM_CONFIG_KEY)
            .map(|value| parse_vector_dim(EMBEDDING_VECTOR_DIM_CONFIG_KEY, value.trim()))
            .transpose()?,
    };

    Ok(CodeVectorSettings { vector_dim })
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
