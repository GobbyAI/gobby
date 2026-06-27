use super::*;

pub(crate) const FALKORDB_DEFAULT_PORT: u16 = 16379;
pub(crate) const EMBEDDING_DEFAULT_MODEL: &str = "nomic-embed-text";
pub(crate) const EMBEDDING_DEFAULT_TIMEOUT_SECONDS: u64 = 10;
const AI_DEFAULT_MAX_CONCURRENCY: u8 = 1;
pub const INDEXING_RESPECT_GITIGNORE_KEY: &str = "indexing.respect_gitignore";
const INDEXING_RESPECT_GITIGNORE_ENV: &str = "GOBBY_INDEXING_RESPECT_GITIGNORE";

/// Decode a config_store value from its stored representation.
pub fn decode_config_value(raw: &str) -> Option<String> {
    match serde_json::from_str::<serde_json::Value>(raw) {
        Ok(serde_json::Value::String(value)) => Some(value),
        Ok(value @ (serde_json::Value::Array(_) | serde_json::Value::Object(_))) => {
            Some(serde_json::to_string(&value).unwrap_or_else(|_| raw.to_string()))
        }
        Ok(serde_json::Value::Null) => None,
        Ok(value) => Some(value.to_string()),
        Err(_) => Some(raw.to_string()),
    }
}

/// Resolve `${VAR}` and `${VAR:-default}` environment variable patterns.
pub fn resolve_env_pattern(value: &str) -> anyhow::Result<Option<String>> {
    if !value.contains("${") {
        return Ok(Some(value.to_string()));
    }

    let mut output = String::with_capacity(value.len());
    let mut rest = value;
    let mut unresolved = false;

    while let Some(start) = rest.find("${") {
        output.push_str(&rest[..start]);
        let pattern = &rest[start + 2..];
        let Some(end) = pattern.find('}') else {
            anyhow::bail!("unterminated environment pattern in `{value}`");
        };

        let expression = &pattern[..end];
        if expression.is_empty() {
            anyhow::bail!("empty environment pattern in `{value}`");
        }

        let (name, default) = match expression.split_once(":-") {
            Some((name, default)) => (name, Some(default)),
            None => (expression, None),
        };
        if name.is_empty() {
            anyhow::bail!("empty environment variable name in `{value}`");
        }

        match std::env::var(name) {
            Ok(current) if !(current.is_empty() && default.is_some()) => {
                output.push_str(&current);
            }
            Ok(_) | Err(std::env::VarError::NotPresent) => match default {
                Some(default) => output.push_str(default),
                None => unresolved = true,
            },
            Err(std::env::VarError::NotUnicode(_)) => {
                anyhow::bail!("environment variable `{name}` is not valid unicode");
            }
        }

        rest = &pattern[end + 1..];
    }

    output.push_str(rest);
    if unresolved {
        Ok(None)
    } else {
        Ok(Some(output))
    }
}

/// Source for config values and interpolation.
pub trait ConfigSource {
    /// Read a decoded config value by key.
    fn config_value(&mut self, key: &str) -> Option<String>;

    /// Resolve interpolation patterns in a config value.
    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String>;
}

/// Generic primary/fallback source for non-env layered configuration.
pub struct LayeredConfigSource<P, F> {
    primary: Option<P>,
    fallback: Option<F>,
}

impl<P, F> LayeredConfigSource<P, F> {
    pub fn new(primary: Option<P>, fallback: Option<F>) -> Self {
        Self { primary, fallback }
    }
}

impl<P, F> ConfigSource for LayeredConfigSource<P, F>
where
    P: ConfigSource,
    F: ConfigSource,
{
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.primary
            .as_mut()
            .and_then(|source| source.config_value(key))
            .or_else(|| {
                self.fallback
                    .as_mut()
                    .and_then(|source| source.config_value(key))
            })
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        match self.primary.as_mut() {
            Some(source) => source.resolve_value(value),
            None => self
                .fallback
                .as_mut()
                .map(|source| source.resolve_value(value))
                .unwrap_or_else(|| {
                    resolve_env_pattern(value)?
                        .ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
                }),
        }
    }
}

/// Environment-only source for consumers without database access.
pub struct EnvOnlySource;

impl ConfigSource for EnvOnlySource {
    fn config_value(&mut self, _key: &str) -> Option<String> {
        None
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if value.contains("$secret:") {
            anyhow::bail!("secret resolution requires a datastore-backed config source");
        }
        resolve_env_pattern(value)?.ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
    }
}

/// Resolve FalkorDB config from env, config_store, then defaults.
pub fn resolve_falkordb_config(source: &mut impl ConfigSource) -> Option<FalkorConfig> {
    let host = resolve_setting(source, "GOBBY_FALKORDB_HOST", "databases.falkordb.host")?;
    let port = resolve_port(
        source,
        "GOBBY_FALKORDB_PORT",
        "databases.falkordb.port",
        FALKORDB_DEFAULT_PORT,
    );
    let password = resolve_setting(
        source,
        "GOBBY_FALKORDB_PASSWORD",
        "databases.falkordb.password",
    );

    Some(FalkorConfig {
        host,
        port,
        password,
    })
}

/// Resolve Qdrant config from env and config_store.
pub fn resolve_qdrant_config(source: &mut impl ConfigSource) -> Option<QdrantConfig> {
    let url = resolve_setting(source, "GOBBY_QDRANT_URL", "databases.qdrant.url");
    url.as_ref()?;
    let api_key = resolve_setting(source, "GOBBY_QDRANT_API_KEY", "databases.qdrant.api_key");

    Some(QdrantConfig { url, api_key })
}

/// Resolve embedding API config from config_store/gcore.yaml.
pub fn resolve_embedding_config(source: &mut impl ConfigSource) -> Option<EmbeddingConfig> {
    resolve_embedding_config_resolution(source).map(|resolution| resolution.config)
}

/// Resolve indexing config from env/config_store/gcore.yaml/defaults.
pub fn resolve_indexing_config(source: &mut impl ConfigSource) -> anyhow::Result<IndexingConfig> {
    let respect_gitignore = match env_value(INDEXING_RESPECT_GITIGNORE_ENV) {
        Some(value) => parse_config_bool_or_default(INDEXING_RESPECT_GITIGNORE_ENV, &value, true),
        None => resolve_config_bool(source, INDEXING_RESPECT_GITIGNORE_KEY, true),
    };

    Ok(IndexingConfig { respect_gitignore })
}

/// Resolve embedding API config and report which namespace supplied api_base.
pub fn resolve_embedding_config_resolution(
    source: &mut impl ConfigSource,
) -> Option<EmbeddingConfigResolution> {
    let binding = resolve_capability_binding(source, AiCapability::Embed);
    let config = resolve_embedding_config_from_binding(source, &binding)?;

    Some(EmbeddingConfigResolution {
        config,
        namespace: embedding_keys::AI_NAMESPACE,
    })
}

/// Build OpenAI-compatible embedding client config from the shared embed binding.
pub fn resolve_embedding_config_from_binding(
    source: &mut impl ConfigSource,
    binding: &CapabilityBinding,
) -> Option<EmbeddingConfig> {
    let api_base = binding
        .api_base
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())?
        .to_string();
    let model = binding
        .model
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .unwrap_or_else(|| EMBEDDING_DEFAULT_MODEL.to_string());
    let api_key = binding
        .api_key
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string);
    let query_prefix = resolve_embedding_setting(source, embedding_keys::AI_QUERY_PREFIX);
    let timeout_seconds = resolve_embedding_setting(source, embedding_keys::AI_TIMEOUT_SECONDS)
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(EMBEDDING_DEFAULT_TIMEOUT_SECONDS);

    Some(EmbeddingConfig {
        api_base,
        model,
        api_key,
        query_prefix,
        timeout_seconds,
    })
}

fn resolve_embedding_setting(source: &mut impl ConfigSource, config_key: &str) -> Option<String> {
    resolve_ai_config_value(source, config_key)
}

/// Resolve a capability's desired routing from config only.
pub fn resolve_capability_routing(
    source: &mut impl ConfigSource,
    capability: AiCapability,
) -> AiRouting {
    resolve_ai_routing_value(source, capability.routing_key())
        .or_else(|| resolve_ai_routing_value(source, ai_keys::ROUTING))
        .unwrap_or_default()
}

/// Resolve a single AI config setting by key, applying the shared
/// secret/env-pattern resolution rules. Returns `None` when the key is absent,
/// resolves to empty, or still contains an unresolved `${...}` pattern.
///
/// This is the public entry point used by profile-aware generation resolvers
/// (e.g. `ai.text_generate.profiles.<profile>.<field>`) so dotted-key lookups
/// reuse the same resolution path as binding resolution.
pub fn resolve_ai_setting(source: &mut impl ConfigSource, key: &str) -> Option<String> {
    resolve_ai_config_value(source, key)
}

/// Resolve a capability binding from config only.
pub fn resolve_capability_binding(
    source: &mut impl ConfigSource,
    capability: AiCapability,
) -> CapabilityBinding {
    match capability {
        AiCapability::AudioTranslate => resolve_audio_translate_binding(source),
        capability => resolve_base_capability_binding(source, capability),
    }
}

/// Resolve shared AI tuning from config only.
pub fn resolve_ai_tuning(source: &mut impl ConfigSource) -> AiTuning {
    let max_concurrency = resolve_ai_config_value(source, ai_keys::MAX_CONCURRENCY)
        .and_then(|value| value.trim().parse::<u8>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(AI_DEFAULT_MAX_CONCURRENCY);
    let keep_alive = resolve_ai_config_value(source, ai_keys::KEEP_ALIVE);

    AiTuning {
        max_concurrency,
        keep_alive,
    }
}

fn resolve_base_capability_binding(
    source: &mut impl ConfigSource,
    capability: AiCapability,
) -> CapabilityBinding {
    CapabilityBinding {
        routing: resolve_capability_routing(source, capability),
        transport: resolve_ai_config_value(source, capability.transport_key()),
        api_base: resolve_ai_config_value(source, capability.api_base_key()),
        api_key: resolve_ai_config_value(source, capability.api_key_key()),
        model: resolve_ai_config_value(source, capability.model_key()),
        provider: resolve_ai_config_value(source, capability.provider_key()),
        task: match capability {
            AiCapability::AudioTranscribe => {
                resolve_ai_config_value(source, ai_keys::AUDIO_TRANSCRIBE_TASK)
            }
            _ => None,
        },
        language: match capability {
            AiCapability::AudioTranscribe => {
                resolve_ai_config_value(source, ai_keys::AUDIO_TRANSCRIBE_LANGUAGE)
            }
            _ => None,
        },
        target_lang: match capability {
            AiCapability::AudioTranslate => {
                resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_TARGET_LANG)
            }
            _ => None,
        },
        profile: match capability {
            AiCapability::TextGenerate => {
                resolve_ai_config_value(source, ai_keys::TEXT_GENERATE_PROFILE)
            }
            _ => None,
        },
        candidates: match capability {
            AiCapability::TextGenerate => {
                resolve_feature_candidates(source, ai_keys::TEXT_GENERATE_CANDIDATES)
            }
            _ => None,
        },
        reasoning_effort: match capability {
            AiCapability::TextGenerate => {
                resolve_ai_config_value(source, ai_keys::TEXT_GENERATE_REASONING_EFFORT)
            }
            _ => None,
        },
        verify_profile: match capability {
            AiCapability::TextGenerate => {
                resolve_ai_config_value(source, ai_keys::TEXT_GENERATE_VERIFY_PROFILE)
            }
            _ => None,
        },
        verify_model: match capability {
            AiCapability::TextGenerate => {
                resolve_ai_config_value(source, ai_keys::TEXT_GENERATE_VERIFY_MODEL)
            }
            _ => None,
        },
        verify_api_key: match capability {
            AiCapability::TextGenerate => {
                resolve_ai_config_value(source, ai_keys::TEXT_GENERATE_VERIFY_API_KEY)
            }
            _ => None,
        },
    }
}

fn resolve_audio_translate_binding(source: &mut impl ConfigSource) -> CapabilityBinding {
    let routing = resolve_ai_routing_value(source, ai_keys::AUDIO_TRANSLATE_ROUTING);
    let transport = resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_TRANSPORT);
    let api_base = resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_API_BASE);
    let api_key = resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_API_KEY);
    let model = resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_MODEL);
    let provider = resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_PROVIDER);
    let target_lang = resolve_ai_config_value(source, ai_keys::AUDIO_TRANSLATE_TARGET_LANG);
    let inherited = resolve_base_capability_binding(source, AiCapability::AudioTranscribe);

    CapabilityBinding {
        routing: routing.unwrap_or(inherited.routing),
        transport: transport.or(inherited.transport),
        api_base: api_base.or(inherited.api_base),
        api_key: api_key.or(inherited.api_key),
        model: model.or(inherited.model),
        provider: provider.or(inherited.provider),
        task: None,
        language: None,
        target_lang,
        profile: None,
        candidates: None,
        reasoning_effort: None,
        verify_profile: None,
        verify_model: None,
        verify_api_key: None,
    }
}

fn resolve_ai_routing_value(source: &mut impl ConfigSource, config_key: &str) -> Option<AiRouting> {
    resolve_ai_config_value(source, config_key).and_then(|value| value.parse().ok())
}

fn resolve_ai_config_value(source: &mut impl ConfigSource, config_key: &str) -> Option<String> {
    let value = source.config_value(config_key)?;
    resolve_ai_non_empty(source, config_key, &value)
}

fn resolve_feature_candidates(
    source: &mut impl ConfigSource,
    config_key: &str,
) -> Option<Vec<FeatureCandidate>> {
    let raw = resolve_ai_config_value(source, config_key)?;
    match serde_json::from_str::<Vec<FeatureCandidate>>(&raw) {
        Ok(candidates) if candidates.is_empty() => None,
        Ok(candidates) => Some(candidates),
        Err(error) => {
            log::warn!("invalid feature candidates for config key {config_key:?}: {error}");
            None
        }
    }
}

fn resolve_config_bool(
    source: &mut impl ConfigSource,
    config_key: &'static str,
    default: bool,
) -> bool {
    let Some(value) = source.config_value(config_key) else {
        return default;
    };
    let Some(resolved) = resolve_non_empty(source, config_key, &value) else {
        return default;
    };
    parse_config_bool_or_default(config_key, &resolved, default)
}

fn parse_config_bool_or_default(source_key: &str, value: &str, default: bool) -> bool {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => true,
        "false" | "0" | "no" | "off" => false,
        _ => {
            log::warn!("invalid boolean for config key {source_key:?}; using default {default}");
            default
        }
    }
}

/// Resolve an AI config value and reject empty or still-unexpanded placeholders.
///
/// AI config resolves from `config_store`/gcore.yaml, but stored values may
/// reference secrets or `${VAR}`. Unresolved placeholders must not masquerade as
/// usable endpoints, models, or keys.
fn resolve_ai_non_empty(
    source: &mut impl ConfigSource,
    source_key: &str,
    value: &str,
) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    let resolved = match source.resolve_value(trimmed) {
        Ok(resolved) => resolved,
        Err(error) => {
            log::warn!("failed to resolve config key {source_key:?}: {error}");
            return None;
        }
    };
    let resolved_trimmed = resolved.trim();
    if resolved_trimmed.is_empty() || contains_unresolved_env_pattern(resolved_trimmed) {
        None
    } else {
        Some(resolved)
    }
}

fn contains_unresolved_env_pattern(value: &str) -> bool {
    value.contains("${")
}

fn resolve_setting(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_key: &str,
) -> Option<String> {
    resolve_setting_from_keys(source, env_key, &[config_key])
}

fn resolve_setting_from_keys(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_keys: &[&str],
) -> Option<String> {
    if let Some(value) = env_value(env_key) {
        return resolve_non_empty(source, env_key, &value);
    }
    for config_key in config_keys {
        let Some(value) = source.config_value(config_key) else {
            continue;
        };
        if let Some(resolved) = resolve_non_empty(source, config_key, &value) {
            return Some(resolved);
        }
    }
    None
}

fn resolve_port(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_key: &str,
    default: u16,
) -> u16 {
    let (source_key, raw_port) = if let Some(raw_port) = env_value(env_key) {
        (env_key, raw_port)
    } else {
        let Some(raw_port) = source.config_value(config_key) else {
            return default;
        };
        (config_key, raw_port)
    };
    let Some(resolved) = resolve_non_empty(source, source_key, &raw_port) else {
        return default;
    };
    match resolved.parse::<u16>() {
        Ok(port) => port,
        Err(error) => {
            log::warn!(
                "invalid port for config key {source_key:?}: {error}; using default {default}"
            );
            default
        }
    }
}

fn resolve_non_empty(
    source: &mut impl ConfigSource,
    source_key: &str,
    value: &str,
) -> Option<String> {
    if value.trim().is_empty() {
        return None;
    }
    let resolved = match source.resolve_value(value) {
        Ok(resolved) => resolved,
        Err(error) => {
            log::warn!("failed to resolve config key {source_key:?}: {error}");
            return None;
        }
    };
    if resolved.trim().is_empty() {
        None
    } else {
        Some(resolved)
    }
}

fn env_value(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .filter(|value| !value.trim().is_empty())
}
