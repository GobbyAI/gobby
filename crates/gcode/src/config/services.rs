#![allow(dead_code)]

use gobby_core::ai_context::AiContext;
use gobby_core::config::embedding_keys;
use gobby_core::config::{AiCapability, AiRouting, CapabilityBinding, ConfigSource};
use postgres::Client;

use super::{
    CodeVectorConfigError, CodeVectorSettings, FALKORDB_GRAPH_NAME, FalkorConfig, QdrantConfig,
};
use crate::config::context::{
    FALKORDB_HOST_CONFIG_KEY, FALKORDB_PASSWORD_CONFIG_KEY, FALKORDB_PORT_CONFIG_KEY,
    IndexingSettings, ServiceConfigSelection,
};
use crate::config::layers::{ConfigLayers, HubConfigCapture, ServiceSource};

pub(super) trait ServiceConfigSource {
    fn config_value(&mut self, key: &str) -> anyhow::Result<Option<String>>;
    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String>;

    fn hit_source(&self, _key: &str) -> Option<&'static str> {
        None
    }
}

pub(super) fn service_env_value(key: &str) -> Option<String> {
    let env_key = match key {
        gobby_core::config::INDEXING_RESPECT_GITIGNORE_KEY => "GOBBY_INDEXING_RESPECT_GITIGNORE",
        _ => return None,
    };
    std::env::var(env_key).ok()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EmbeddingConfigDetails {
    pub config: super::EmbeddingConfig,
    pub namespace: &'static str,
    pub source: &'static str,
}

struct ErrorCapturingConfigSource<'a, S> {
    source: &'a mut S,
    first_error: Option<anyhow::Error>,
}

impl<S> ErrorCapturingConfigSource<'_, S> {
    fn finish<T>(self, value: T) -> anyhow::Result<T> {
        match self.first_error {
            Some(error) => Err(error),
            None => Ok(value),
        }
    }
}

impl<S> ConfigSource for ErrorCapturingConfigSource<'_, S>
where
    S: ServiceConfigSource,
{
    fn config_value(&mut self, key: &str) -> Option<String> {
        if self.first_error.is_some() {
            return None;
        }
        match self.source.config_value(key) {
            Ok(value) => value,
            Err(error) => {
                self.first_error =
                    Some(error.context(format!("failed to read config key {key:?}")));
                None
            }
        }
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.source.resolve_value(value)
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
impl<R, S> ServiceConfigSource for ClosureConfigSource<R, S>
where
    R: FnMut(&str) -> Option<String>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    fn config_value(&mut self, key: &str) -> anyhow::Result<Option<String>> {
        if let Some(value) = service_env_value(key) {
            return Ok(Some(value));
        }
        Ok((self.read_config_value)(key)
            .and_then(|raw| gobby_core::config::decode_config_value(&raw)))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        (self.resolve_value)(value)
    }
}

#[cfg(test)]
struct FallibleClosureConfigSource<R, S> {
    read_config_value: R,
    resolve_value: S,
}

#[cfg(test)]
impl<R, S> ServiceConfigSource for FallibleClosureConfigSource<R, S>
where
    R: FnMut(&str) -> anyhow::Result<Option<String>>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    fn config_value(&mut self, key: &str) -> anyhow::Result<Option<String>> {
        if let Some(value) = service_env_value(key) {
            return Ok(Some(value));
        }
        Ok((self.read_config_value)(key)?
            .and_then(|raw| gobby_core::config::decode_config_value(&raw)))
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
    resolve_falkordb_config_from_source(&mut source).expect("test config should resolve")
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
    resolve_qdrant_config_from_source(&mut source).expect("test config should resolve")
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
pub(super) fn resolve_embedding_config_from_fallible_values<R, S>(
    read_config_value: R,
    resolve_value: S,
) -> anyhow::Result<Option<super::EmbeddingConfig>>
where
    R: FnMut(&str) -> anyhow::Result<Option<String>>,
    S: FnMut(&str) -> anyhow::Result<String>,
{
    let mut source = FallibleClosureConfigSource {
        read_config_value,
        resolve_value,
    };
    resolve_embedding_config_from_service_source(None, &mut source)
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
pub(super) fn resolve_falkordb_config_from_source(
    source: &mut impl ServiceConfigSource,
) -> anyhow::Result<Option<FalkorConfig>> {
    let Some(host) = resolve_service_setting(source, FALKORDB_HOST_CONFIG_KEY)? else {
        return Ok(None);
    };
    let port = resolve_service_port(source, FALKORDB_PORT_CONFIG_KEY, 16379)?;
    let password = resolve_service_setting(source, FALKORDB_PASSWORD_CONFIG_KEY)?;

    Ok(Some(FalkorConfig {
        host,
        port,
        password,
        graph_name: FALKORDB_GRAPH_NAME.to_string(),
    }))
}

/// Resolve Qdrant configuration from config_store + env vars.
///
/// `_quiet` is reserved for future verbosity control; config resolution is currently silent.
pub(super) fn resolve_qdrant_config_from_source(
    source: &mut impl ServiceConfigSource,
) -> anyhow::Result<Option<QdrantConfig>> {
    let url = resolve_service_setting(source, "databases.qdrant.url")?;
    if url.is_none() {
        return Ok(None);
    }
    let api_key = resolve_service_setting(source, "databases.qdrant.api_key")?;
    Ok(Some(QdrantConfig { url, api_key }))
}

fn resolve_service_setting(
    source: &mut impl ServiceConfigSource,
    key: &'static str,
) -> anyhow::Result<Option<String>> {
    let Some(value) = source.config_value(key)? else {
        return Ok(None);
    };
    resolve_service_non_empty(source, &value)
}

fn resolve_service_non_empty(
    source: &mut impl ServiceConfigSource,
    value: &str,
) -> anyhow::Result<Option<String>> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    let resolved = source.resolve_value(trimmed)?;
    let resolved = resolved.trim();
    if resolved.is_empty() {
        Ok(None)
    } else {
        Ok(Some(resolved.to_string()))
    }
}

fn resolve_service_port(
    source: &mut impl ServiceConfigSource,
    key: &'static str,
    default: u16,
) -> anyhow::Result<u16> {
    let Some(raw_port) = resolve_service_setting(source, key)? else {
        return Ok(default);
    };
    match raw_port.parse::<u16>() {
        Ok(0) => {
            eprintln!(
                "Warning: invalid service port config {key}={raw_port:?}; using default {default}"
            );
            Ok(default)
        }
        Ok(port) => Ok(port),
        Err(_) => {
            eprintln!(
                "Warning: invalid service port config {key}={raw_port:?}; using default {default}"
            );
            Ok(default)
        }
    }
}

/// Resolve embedding API configuration from config_store + grant-backed config.
///
/// Returns None if no api_base is found (BM25 only).
///
/// `_quiet` is reserved for future verbosity control; config resolution is currently silent.
pub(crate) fn resolve_embedding_config_details(
    conn: &mut Client,
    layers: &ConfigLayers,
) -> anyhow::Result<Option<EmbeddingConfigDetails>> {
    let (mut source, _revision, _capture_status) =
        ServiceSource::new(conn, layers, HubConfigCapture::Required)?;
    resolve_embedding_config_details_from_service_source(&mut source)
}

pub(super) fn resolve_embedding_config_details_from_service_source(
    source: &mut impl ServiceConfigSource,
) -> anyhow::Result<Option<EmbeddingConfigDetails>> {
    let Some(config) = resolve_embedding_config_from_service_source(None, source)? else {
        return Ok(None);
    };
    let source_name = source
        .hit_source(embedding_keys::AI_API_BASE)
        .unwrap_or("unknown");
    Ok(Some(EmbeddingConfigDetails {
        config,
        namespace: embedding_keys::AI_NAMESPACE,
        source: source_name,
    }))
}

pub(super) fn resolve_embedding_config_from_service_source(
    project_id: Option<String>,
    source: &mut impl ServiceConfigSource,
) -> anyhow::Result<Option<super::EmbeddingConfig>> {
    let mut source = ErrorCapturingConfigSource {
        source,
        first_error: None,
    };
    let config = resolve_embedding_config_from_source(project_id, &mut source);
    source.finish(config)
}

pub(crate) fn resolve_embedding_config_from_source(
    project_id: Option<String>,
    source: &mut impl ConfigSource,
) -> Option<super::EmbeddingConfig> {
    let context = AiContext::resolve(project_id, source);
    let binding = context.binding(AiCapability::Embed);
    if binding.routing == AiRouting::Off || !embedding_binding_uses_openai_http(binding) {
        return None;
    }
    gobby_core::config::resolve_embedding_config_from_binding(source, binding)
}

fn embedding_binding_uses_openai_http(binding: &CapabilityBinding) -> bool {
    binding
        .transport
        .as_deref()
        .map(str::trim)
        .is_none_or(|transport| transport.is_empty() || transport == "openai_compatible_http")
}

struct GrantSettingsSource<'a> {
    settings: &'a std::collections::BTreeMap<String, String>,
}

impl ServiceConfigSource for GrantSettingsSource<'_> {
    fn config_value(&mut self, key: &str) -> anyhow::Result<Option<String>> {
        if let Some(value) = service_env_value(key) {
            return Ok(Some(value));
        }
        Ok(self
            .settings
            .get(key)
            .cloned()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw)))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        Ok(value.to_string())
    }
}

pub(super) fn resolve_from_grant_settings(
    settings: &std::collections::BTreeMap<String, String>,
    services: ServiceConfigSelection,
) -> anyhow::Result<(
    Option<super::EmbeddingConfig>,
    IndexingSettings,
    CodeVectorSettings,
)> {
    let mut source = GrantSettingsSource { settings };
    let embedding = if services.embedding {
        resolve_embedding_config_from_service_source(None, &mut source)?
    } else {
        None
    };
    let indexing = resolve_indexing_settings_from_source(&mut source)?;
    let code_vectors = if services.code_vectors {
        resolve_code_vector_settings_from_source(&mut source)?
    } else {
        CodeVectorSettings::default()
    };
    Ok((embedding, indexing, code_vectors))
}

pub(super) fn resolve_indexing_settings_from_source(
    source: &mut impl ServiceConfigSource,
) -> anyhow::Result<IndexingSettings> {
    let mut source = ErrorCapturingConfigSource {
        source,
        first_error: None,
    };
    let settings = gobby_core::config::resolve_indexing_config_from_source(&mut source)?;
    source.finish(settings)
}

pub(super) fn resolve_code_vector_settings_from_source(
    source: &mut impl ServiceConfigSource,
) -> Result<CodeVectorSettings, CodeVectorConfigError> {
    let vector_dim = resolve_vector_dim(source, embedding_keys::AI_DIM)?;

    Ok(CodeVectorSettings::with_vector_dim(vector_dim))
}

fn resolve_vector_dim(
    source: &mut impl ServiceConfigSource,
    key: &'static str,
) -> Result<Option<usize>, CodeVectorConfigError> {
    source
        .config_value(key)
        .map_err(|source| CodeVectorConfigError::Read {
            source: source.to_string(),
        })?
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
