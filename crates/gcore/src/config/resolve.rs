use super::*;

const FALKORDB_DEFAULT_PORT: u16 = 16379;
pub(crate) const EMBEDDING_DEFAULT_MODEL: &str = "nomic-embed-text";
pub(crate) const EMBEDDING_DEFAULT_TIMEOUT_SECONDS: u64 = 10;
const AI_DEFAULT_MAX_CONCURRENCY: u8 = 1;

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
        "databases.falkordb.requirepass",
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
    }
}

fn resolve_ai_routing_value(source: &mut impl ConfigSource, config_key: &str) -> Option<AiRouting> {
    resolve_ai_config_value(source, config_key).and_then(|value| value.parse().ok())
}

fn resolve_ai_config_value(source: &mut impl ConfigSource, config_key: &str) -> Option<String> {
    let value = source.config_value(config_key)?;
    resolve_ai_non_empty(source, &value)
}

/// Resolve an AI config value and reject empty or still-unexpanded placeholders.
///
/// AI config resolves from `config_store`/gcore.yaml, but stored values may
/// reference secrets or `${VAR}`. Unresolved placeholders must not masquerade as
/// usable endpoints, models, or keys.
fn resolve_ai_non_empty(source: &mut impl ConfigSource, value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    source.resolve_value(trimmed).ok().filter(|resolved| {
        let resolved = resolved.trim();
        !resolved.is_empty() && !contains_unresolved_env_pattern(resolved)
    })
}

fn contains_unresolved_env_pattern(value: &str) -> bool {
    value.contains("${")
}

fn resolve_setting(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_key: &str,
) -> Option<String> {
    let value = env_value(env_key).or_else(|| source.config_value(config_key))?;
    resolve_non_empty(source, &value)
}

fn resolve_port(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_key: &str,
    default: u16,
) -> u16 {
    let Some(raw_port) = env_value(env_key).or_else(|| source.config_value(config_key)) else {
        return default;
    };
    let Some(resolved) = resolve_non_empty(source, &raw_port) else {
        return default;
    };
    resolved.parse::<u16>().unwrap_or(default)
}

fn resolve_non_empty(source: &mut impl ConfigSource, value: &str) -> Option<String> {
    if value.trim().is_empty() {
        return None;
    }
    source
        .resolve_value(value)
        .ok()
        .filter(|resolved| !resolved.trim().is_empty())
}

fn env_value(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .filter(|value| !value.trim().is_empty())
}
