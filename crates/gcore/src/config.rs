//! Shared configuration-resolution boundary.
//!
//! This module is the public home for lightweight configuration contracts that
//! are shared across Gobby Rust crates. Concrete service resolution is added in
//! focused follow-up modules so this baseline crate remains small.

/// FalkorDB connection configuration.
///
/// Graph name selection is consumer-owned.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FalkorConfig {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
}

/// Qdrant connection configuration.
///
/// Collection naming is consumer-owned.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QdrantConfig {
    pub url: Option<String>,
    pub api_key: Option<String>,
}

/// Embedding API configuration for an OpenAI-compatible endpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingConfig {
    pub api_base: String,
    pub model: String,
    pub api_key: Option<String>,
    pub query_prefix: Option<String>,
    pub timeout_seconds: u64,
}

/// AI routing preference for a capability.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum AiRouting {
    #[default]
    Auto,
    Daemon,
    Direct,
    Off,
}

impl std::str::FromStr for AiRouting {
    type Err = ParseAiRoutingError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim() {
            "auto" => Ok(Self::Auto),
            "daemon" => Ok(Self::Daemon),
            "direct" => Ok(Self::Direct),
            "off" => Ok(Self::Off),
            value => Err(ParseAiRoutingError {
                value: value.to_string(),
            }),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseAiRoutingError {
    value: String,
}

impl std::fmt::Display for ParseAiRoutingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid AI routing `{}`", self.value)
    }
}

impl std::error::Error for ParseAiRoutingError {}

/// AI capability names shared with the daemon registry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AiCapability {
    Embed,
    AudioTranscribe,
    AudioTranslate,
    VisionExtract,
    TextGenerate,
}

impl AiCapability {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Embed => "embed",
            Self::AudioTranscribe => "audio_transcribe",
            Self::AudioTranslate => "audio_translate",
            Self::VisionExtract => "vision_extract",
            Self::TextGenerate => "text_generate",
        }
    }

    pub fn namespace(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_NAMESPACE,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_NAMESPACE,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_NAMESPACE,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_NAMESPACE,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_NAMESPACE,
        }
    }

    fn routing_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_ROUTING,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_ROUTING,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_ROUTING,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_ROUTING,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_ROUTING,
        }
    }

    fn transport_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_TRANSPORT,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_TRANSPORT,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_TRANSPORT,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_TRANSPORT,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_TRANSPORT,
        }
    }

    fn api_base_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_API_BASE,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_API_BASE,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_API_BASE,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_API_BASE,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_API_BASE,
        }
    }

    fn api_key_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_API_KEY,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_API_KEY,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_API_KEY,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_API_KEY,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_API_KEY,
        }
    }

    fn model_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_MODEL,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_MODEL,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_MODEL,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_MODEL,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_MODEL,
        }
    }

    fn provider_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_PROVIDER,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_PROVIDER,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_PROVIDER,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_PROVIDER,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_PROVIDER,
        }
    }
}

impl std::str::FromStr for AiCapability {
    type Err = ParseAiCapabilityError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim() {
            "embed" | "embeddings" => Ok(Self::Embed),
            "audio_transcribe" => Ok(Self::AudioTranscribe),
            "audio_translate" => Ok(Self::AudioTranslate),
            "vision_extract" => Ok(Self::VisionExtract),
            "text_generate" => Ok(Self::TextGenerate),
            value => Err(ParseAiCapabilityError {
                value: value.to_string(),
            }),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseAiCapabilityError {
    value: String,
}

impl std::fmt::Display for ParseAiCapabilityError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid AI capability `{}`", self.value)
    }
}

impl std::error::Error for ParseAiCapabilityError {}

/// Per-capability AI endpoint binding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapabilityBinding {
    pub routing: AiRouting,
    pub transport: Option<String>,
    pub api_base: Option<String>,
    pub api_key: Option<String>,
    pub model: Option<String>,
    pub provider: Option<String>,
    pub task: Option<String>,
    pub language: Option<String>,
    pub target_lang: Option<String>,
}

/// Shared AI tuning values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AiTuning {
    pub max_concurrency: u8,
    pub keep_alive: Option<String>,
}

/// Canonical home for embedding config keys during namespace migration.
pub mod embedding_keys {
    pub const AI_NAMESPACE: &str = "ai.embeddings";

    pub const AI_PROVIDER: &str = "ai.embeddings.provider";
    pub const AI_API_BASE: &str = "ai.embeddings.api_base";
    pub const AI_MODEL: &str = "ai.embeddings.model";
    pub const AI_API_KEY: &str = "ai.embeddings.api_key";
    pub const AI_QUERY_PREFIX: &str = "ai.embeddings.query_prefix";
    pub const AI_DIM: &str = "ai.embeddings.dim";
    pub const AI_TIMEOUT_SECONDS: &str = "ai.embeddings.timeout_seconds";

    const LEGACY_NAMESPACE: &str = "embeddings";
    const LEGACY_KEY_SUFFIXES: &[&str] = &[
        "provider",
        "api_base",
        "model",
        "api_key",
        "api_key_env",
        "query_prefix",
        "timeout_seconds",
        "vector_dim",
    ];

    pub fn legacy_keys() -> Vec<String> {
        LEGACY_KEY_SUFFIXES
            .iter()
            .map(|suffix| format!("{LEGACY_NAMESPACE}.{suffix}"))
            .collect()
    }
}

/// Canonical home for AI capability config keys.
pub mod ai_keys {
    pub const ROUTING: &str = "ai.routing";
    pub const MAX_CONCURRENCY: &str = "ai.max_concurrency";
    pub const KEEP_ALIVE: &str = "ai.keep_alive";

    pub const EMBEDDINGS_NAMESPACE: &str = super::embedding_keys::AI_NAMESPACE;
    pub const EMBEDDINGS_ROUTING: &str = "ai.embeddings.routing";
    pub const EMBEDDINGS_TRANSPORT: &str = "ai.embeddings.transport";
    pub const EMBEDDINGS_PROVIDER: &str = super::embedding_keys::AI_PROVIDER;
    pub const EMBEDDINGS_API_BASE: &str = super::embedding_keys::AI_API_BASE;
    pub const EMBEDDINGS_MODEL: &str = super::embedding_keys::AI_MODEL;
    pub const EMBEDDINGS_API_KEY: &str = super::embedding_keys::AI_API_KEY;

    pub const AUDIO_TRANSCRIBE_NAMESPACE: &str = "ai.audio_transcribe";
    pub const AUDIO_TRANSCRIBE_ROUTING: &str = "ai.audio_transcribe.routing";
    pub const AUDIO_TRANSCRIBE_TRANSPORT: &str = "ai.audio_transcribe.transport";
    pub const AUDIO_TRANSCRIBE_API_BASE: &str = "ai.audio_transcribe.api_base";
    pub const AUDIO_TRANSCRIBE_API_KEY: &str = "ai.audio_transcribe.api_key";
    pub const AUDIO_TRANSCRIBE_MODEL: &str = "ai.audio_transcribe.model";
    pub const AUDIO_TRANSCRIBE_PROVIDER: &str = "ai.audio_transcribe.provider";
    pub const AUDIO_TRANSCRIBE_TASK: &str = "ai.audio_transcribe.task";
    pub const AUDIO_TRANSCRIBE_LANGUAGE: &str = "ai.audio_transcribe.language";

    pub const AUDIO_TRANSLATE_NAMESPACE: &str = "ai.audio_translate";
    pub const AUDIO_TRANSLATE_ROUTING: &str = "ai.audio_translate.routing";
    pub const AUDIO_TRANSLATE_TRANSPORT: &str = "ai.audio_translate.transport";
    pub const AUDIO_TRANSLATE_API_BASE: &str = "ai.audio_translate.api_base";
    pub const AUDIO_TRANSLATE_API_KEY: &str = "ai.audio_translate.api_key";
    pub const AUDIO_TRANSLATE_MODEL: &str = "ai.audio_translate.model";
    pub const AUDIO_TRANSLATE_PROVIDER: &str = "ai.audio_translate.provider";
    pub const AUDIO_TRANSLATE_TARGET_LANG: &str = "ai.audio_translate.target_lang";

    pub const VISION_EXTRACT_NAMESPACE: &str = "ai.vision_extract";
    pub const VISION_EXTRACT_ROUTING: &str = "ai.vision_extract.routing";
    pub const VISION_EXTRACT_TRANSPORT: &str = "ai.vision_extract.transport";
    pub const VISION_EXTRACT_API_BASE: &str = "ai.vision_extract.api_base";
    pub const VISION_EXTRACT_API_KEY: &str = "ai.vision_extract.api_key";
    pub const VISION_EXTRACT_MODEL: &str = "ai.vision_extract.model";
    pub const VISION_EXTRACT_PROVIDER: &str = "ai.vision_extract.provider";

    pub const TEXT_GENERATE_NAMESPACE: &str = "ai.text_generate";
    pub const TEXT_GENERATE_ROUTING: &str = "ai.text_generate.routing";
    pub const TEXT_GENERATE_TRANSPORT: &str = "ai.text_generate.transport";
    pub const TEXT_GENERATE_API_BASE: &str = "ai.text_generate.api_base";
    pub const TEXT_GENERATE_API_KEY: &str = "ai.text_generate.api_key";
    pub const TEXT_GENERATE_MODEL: &str = "ai.text_generate.model";
    pub const TEXT_GENERATE_PROVIDER: &str = "ai.text_generate.provider";

    const ALL_KEYS: &[&str] = &[
        ROUTING,
        MAX_CONCURRENCY,
        KEEP_ALIVE,
        EMBEDDINGS_ROUTING,
        EMBEDDINGS_TRANSPORT,
        EMBEDDINGS_PROVIDER,
        EMBEDDINGS_API_BASE,
        EMBEDDINGS_MODEL,
        EMBEDDINGS_API_KEY,
        AUDIO_TRANSCRIBE_ROUTING,
        AUDIO_TRANSCRIBE_TRANSPORT,
        AUDIO_TRANSCRIBE_API_BASE,
        AUDIO_TRANSCRIBE_API_KEY,
        AUDIO_TRANSCRIBE_MODEL,
        AUDIO_TRANSCRIBE_PROVIDER,
        AUDIO_TRANSCRIBE_TASK,
        AUDIO_TRANSCRIBE_LANGUAGE,
        AUDIO_TRANSLATE_ROUTING,
        AUDIO_TRANSLATE_TRANSPORT,
        AUDIO_TRANSLATE_API_BASE,
        AUDIO_TRANSLATE_API_KEY,
        AUDIO_TRANSLATE_MODEL,
        AUDIO_TRANSLATE_PROVIDER,
        AUDIO_TRANSLATE_TARGET_LANG,
        VISION_EXTRACT_ROUTING,
        VISION_EXTRACT_TRANSPORT,
        VISION_EXTRACT_API_BASE,
        VISION_EXTRACT_API_KEY,
        VISION_EXTRACT_MODEL,
        VISION_EXTRACT_PROVIDER,
        TEXT_GENERATE_ROUTING,
        TEXT_GENERATE_TRANSPORT,
        TEXT_GENERATE_API_BASE,
        TEXT_GENERATE_API_KEY,
        TEXT_GENERATE_MODEL,
        TEXT_GENERATE_PROVIDER,
    ];

    pub fn all() -> &'static [&'static str] {
        ALL_KEYS
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingConfigResolution {
    pub config: EmbeddingConfig,
    pub namespace: &'static str,
}

const FALKORDB_DEFAULT_PORT: u16 = 16379;
const EMBEDDING_DEFAULT_MODEL: &str = "nomic-embed-text";
const EMBEDDING_DEFAULT_TIMEOUT_SECONDS: u64 = 10;
const AI_DEFAULT_MAX_CONCURRENCY: u8 = 1;

#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

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

fn resolve_ai_non_empty(source: &mut impl ConfigSource, value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    source
        .resolve_value(trimmed)
        .ok()
        .filter(|resolved| !resolved.trim().is_empty())
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::MutexGuard;

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
    }

    impl EnvGuard {
        fn new() -> Self {
            let guard = Self {
                _lock: TEST_ENV_LOCK
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()),
            };
            guard.clear();
            guard
        }

        fn clear(&self) {
            for key in [
                "GOBBY_FALKORDB_HOST",
                "GOBBY_FALKORDB_PORT",
                "GOBBY_FALKORDB_PASSWORD",
                "GOBBY_QDRANT_URL",
                "GOBBY_QDRANT_API_KEY",
                "GOBBY_EMBEDDING_URL",
                "GOBBY_EMBEDDING_MODEL",
                "GOBBY_EMBEDDING_API_KEY",
                "GOBBY_EMBEDDING_QUERY_PREFIX",
                "GOBBY_EMBEDDING_TIMEOUT_SECONDS",
                "GOBBY_AI_TEXT_GENERATE_API_BASE",
                "GOBBY_TEST_PRESENT",
                "GOBBY_TEST_MISSING",
            ] {
                // SAFETY: TEST_ENV_LOCK serializes all test environment mutation
                // here, and the loop only touches the fixed key list above.
                unsafe { std::env::remove_var(key) };
            }
        }

        fn set(&self, key: &str, value: &str) {
            unsafe { std::env::set_var(key, value) };
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            self.clear();
        }
    }

    #[derive(Default)]
    struct TestSource {
        values: HashMap<&'static str, String>,
        resolved_values: Vec<String>,
    }

    impl TestSource {
        fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values
                    .into_iter()
                    .map(|(key, value)| (key, value.to_string()))
                    .collect(),
                resolved_values: Vec::new(),
            }
        }

        fn with_raw_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values
                    .into_iter()
                    .filter_map(|(key, value)| decode_config_value(value).map(|v| (key, v)))
                    .collect(),
                resolved_values: Vec::new(),
            }
        }
    }

    impl ConfigSource for TestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            self.resolved_values.push(value.to_string());
            if let Some(secret_name) = value.strip_prefix("$secret:") {
                return Ok(format!("resolved-{secret_name}"));
            }
            Ok(resolve_env_pattern(value)?.unwrap_or_else(|| value.to_string()))
        }
    }

    #[derive(Default)]
    struct LayeredTestSource {
        store: TestSource,
        yaml: TestSource,
    }

    impl LayeredTestSource {
        fn with_layers(
            store_values: impl IntoIterator<Item = (&'static str, &'static str)>,
            yaml_values: impl IntoIterator<Item = (&'static str, &'static str)>,
        ) -> Self {
            Self {
                store: TestSource::with_values(store_values),
                yaml: TestSource::with_values(yaml_values),
            }
        }
    }

    impl ConfigSource for LayeredTestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.store
                .config_value(key)
                .or_else(|| self.yaml.config_value(key))
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            self.store.resolve_value(value)
        }
    }

    #[test]
    fn decode_config_value_handles_json_and_plain() {
        assert_eq!(
            decode_config_value("\"http://host:7474\""),
            Some("http://host:7474".to_string())
        );
        assert_eq!(
            decode_config_value(r#"["alpha",1,true]"#),
            Some(r#"["alpha",1,true]"#.to_string())
        );
        assert_eq!(
            decode_config_value(r#"{"host":"falkor.local","port":16379}"#),
            Some(r#"{"host":"falkor.local","port":16379}"#.to_string())
        );
        assert_eq!(decode_config_value("42"), Some("42".to_string()));
        assert_eq!(decode_config_value("true"), Some("true".to_string()));
        assert_eq!(
            decode_config_value("http://plain:7474"),
            Some("http://plain:7474".to_string())
        );
        assert_eq!(decode_config_value("null"), None);
    }

    #[test]
    fn resolve_env_pattern_with_defaults() {
        let env = EnvGuard::new();
        env.set("GOBBY_TEST_PRESENT", "present-value");

        assert_eq!(
            resolve_env_pattern("${GOBBY_TEST_PRESENT}").unwrap(),
            Some("present-value".to_string())
        );
        assert_eq!(
            resolve_env_pattern("prefix-${GOBBY_TEST_PRESENT}-suffix").unwrap(),
            Some("prefix-present-value-suffix".to_string())
        );
        assert_eq!(
            resolve_env_pattern("${GOBBY_TEST_MISSING:-fallback}").unwrap(),
            Some("fallback".to_string())
        );
        assert_eq!(resolve_env_pattern("${GOBBY_TEST_MISSING}").unwrap(), None);
        assert_eq!(
            resolve_env_pattern("plain-value").unwrap(),
            Some("plain-value".to_string())
        );
    }

    #[test]
    fn env_overrides_config_store() {
        let env = EnvGuard::new();
        env.set("GOBBY_FALKORDB_HOST", "env-falkor.local");
        env.set("GOBBY_FALKORDB_PORT", "17000");
        env.set("GOBBY_FALKORDB_PASSWORD", "env-pass");
        env.set("GOBBY_QDRANT_URL", "http://env-qdrant:6333");
        env.set("GOBBY_QDRANT_API_KEY", "env-qdrant-key");

        let mut source = TestSource::with_values([
            ("databases.falkordb.host", "stored-falkor.local"),
            ("databases.falkordb.port", "16000"),
            ("databases.falkordb.requirepass", "stored-pass"),
            ("databases.qdrant.url", "http://stored-qdrant:6333"),
            ("databases.qdrant.api_key", "stored-qdrant-key"),
        ]);

        let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");
        let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");

        assert_eq!(falkordb.host, "env-falkor.local");
        assert_eq!(falkordb.port, 17000);
        assert_eq!(falkordb.password.as_deref(), Some("env-pass"));
        assert_eq!(qdrant.url.as_deref(), Some("http://env-qdrant:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some("env-qdrant-key"));
    }

    #[test]
    fn config_source_handles_secrets() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_values([
            ("databases.falkordb.host", "falkor.local"),
            ("databases.falkordb.requirepass", "$secret:FALKOR_PASS"),
        ]);

        let config = resolve_falkordb_config(&mut source).expect("falkordb config");

        assert_eq!(config.password.as_deref(), Some("resolved-FALKOR_PASS"));
        assert!(
            source
                .resolved_values
                .iter()
                .any(|value| value == "$secret:FALKOR_PASS")
        );
    }

    #[test]
    fn env_only_source_rejects_secret_patterns() {
        let _env = EnvGuard::new();
        let mut source = EnvOnlySource;

        let error = source
            .resolve_value("$secret:FALKOR_PASS")
            .expect_err("secret resolution should require a datastore-backed source");

        assert!(error.to_string().contains("secret resolution"));
    }

    #[test]
    fn ai_routing_per_capability_precedence() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_values([
            (ai_keys::ROUTING, "daemon"),
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "direct"),
        ]);

        assert_eq!(
            resolve_capability_routing(&mut source, AiCapability::AudioTranscribe),
            AiRouting::Direct
        );

        let mut source = TestSource::with_values([(ai_keys::ROUTING, "off")]);
        assert_eq!(
            resolve_capability_routing(&mut source, AiCapability::VisionExtract),
            AiRouting::Off
        );

        let mut source = TestSource::default();
        assert_eq!(
            resolve_capability_routing(&mut source, AiCapability::TextGenerate),
            AiRouting::Auto
        );

        let mut source = TestSource::with_values([
            (ai_keys::TEXT_GENERATE_ROUTING, "unknown"),
            (ai_keys::ROUTING, "direct"),
        ]);
        assert_eq!(
            resolve_capability_routing(&mut source, AiCapability::TextGenerate),
            AiRouting::Direct
        );

        assert_eq!("daemon".parse::<AiRouting>().ok(), Some(AiRouting::Daemon));
        assert!("unknown".parse::<AiRouting>().is_err());
    }

    #[test]
    fn ai_config_resolves_store_then_yaml_no_env() {
        let env = EnvGuard::new();
        env.set("GOBBY_EMBEDDING_URL", "http://env-embedding:11434/v1");
        env.set(
            "GOBBY_AI_TEXT_GENERATE_API_BASE",
            "http://env-text:11434/v1",
        );
        env.set("GOBBY_TEST_PRESENT", "interpolated-text-model");

        let mut source = LayeredTestSource::with_layers(
            [
                (
                    ai_keys::TEXT_GENERATE_API_BASE,
                    "http://store-text:11434/v1",
                ),
                (ai_keys::TEXT_GENERATE_API_KEY, "$secret:TEXT_KEY"),
                (ai_keys::MAX_CONCURRENCY, "3"),
            ],
            [
                (ai_keys::TEXT_GENERATE_API_BASE, "http://yaml-text:11434/v1"),
                (ai_keys::TEXT_GENERATE_MODEL, "${GOBBY_TEST_PRESENT}"),
                (ai_keys::TEXT_GENERATE_API_KEY, "yaml-local-key"),
                (ai_keys::KEEP_ALIVE, "30s"),
            ],
        );

        let binding = resolve_capability_binding(&mut source, AiCapability::TextGenerate);
        let tuning = resolve_ai_tuning(&mut source);

        assert_eq!(
            binding.api_base.as_deref(),
            Some("http://store-text:11434/v1")
        );
        assert_eq!(binding.model.as_deref(), Some("interpolated-text-model"));
        assert_eq!(binding.api_key.as_deref(), Some("resolved-TEXT_KEY"));
        assert_eq!(tuning.max_concurrency, 3);
        assert_eq!(tuning.keep_alive.as_deref(), Some("30s"));

        let mut standalone_source = LayeredTestSource::with_layers(
            [],
            [
                (
                    ai_keys::EMBEDDINGS_API_BASE,
                    "http://yaml-embedding:11434/v1",
                ),
                (ai_keys::EMBEDDINGS_API_KEY, "plaintext-local-key"),
            ],
        );
        let binding = resolve_capability_binding(&mut standalone_source, AiCapability::Embed);
        let embedding = resolve_embedding_config(&mut standalone_source).expect("embedding config");

        assert_eq!(
            binding.api_base.as_deref(),
            Some("http://yaml-embedding:11434/v1")
        );
        assert_eq!(binding.api_key.as_deref(), Some("plaintext-local-key"));
        assert_eq!(embedding.api_base, "http://yaml-embedding:11434/v1");

        let tuning = resolve_ai_tuning(&mut TestSource::default());
        assert_eq!(tuning.max_concurrency, 1);
        assert!(tuning.keep_alive.is_none());
    }

    #[test]
    fn provider_and_translation_fields_resolve() {
        let _env = EnvGuard::new();
        let mut source = LayeredTestSource::with_layers(
            [
                (
                    ai_keys::AUDIO_TRANSLATE_PROVIDER,
                    "store-translate-provider",
                ),
                (ai_keys::AUDIO_TRANSLATE_TARGET_LANG, "fr"),
            ],
            [
                (ai_keys::AUDIO_TRANSLATE_PROVIDER, "yaml-translate-provider"),
                (ai_keys::AUDIO_TRANSLATE_TARGET_LANG, "de"),
                (ai_keys::AUDIO_TRANSCRIBE_LANGUAGE, "en"),
            ],
        );

        let translate = resolve_capability_binding(&mut source, AiCapability::AudioTranslate);
        let transcribe = resolve_capability_binding(&mut source, AiCapability::AudioTranscribe);

        assert_eq!(
            translate.provider.as_deref(),
            Some("store-translate-provider")
        );
        assert_eq!(translate.target_lang.as_deref(), Some("fr"));
        assert!(translate.task.is_none());
        assert!(translate.language.is_none());

        assert_eq!(transcribe.language.as_deref(), Some("en"));
        assert!(transcribe.target_lang.is_none());
        assert!(
            ai_keys::all()
                .iter()
                .all(|key| !key.starts_with("ai.video."))
        );
    }

    #[test]
    fn audio_translate_inherits_transcribe_binding() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_values([
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "direct"),
            (ai_keys::AUDIO_TRANSCRIBE_TRANSPORT, "daemon_native"),
            (ai_keys::AUDIO_TRANSCRIBE_API_BASE, "http://stt:8080/v1"),
            (ai_keys::AUDIO_TRANSCRIBE_API_KEY, "$secret:STT_KEY"),
            (ai_keys::AUDIO_TRANSCRIBE_MODEL, "whisper-large-v3"),
            (ai_keys::AUDIO_TRANSCRIBE_PROVIDER, "faster-whisper"),
            (ai_keys::AUDIO_TRANSCRIBE_TASK, "transcribe"),
            (ai_keys::AUDIO_TRANSCRIBE_LANGUAGE, "en"),
            (ai_keys::AUDIO_TRANSLATE_MODEL, "translate-override"),
            (ai_keys::AUDIO_TRANSLATE_PROVIDER, "translate-provider"),
            (ai_keys::AUDIO_TRANSLATE_TARGET_LANG, "es"),
        ]);

        let translate = resolve_capability_binding(&mut source, AiCapability::AudioTranslate);
        let transcribe = resolve_capability_binding(&mut source, AiCapability::AudioTranscribe);

        assert_eq!(translate.routing, AiRouting::Direct);
        assert_eq!(translate.transport.as_deref(), Some("daemon_native"));
        assert_eq!(translate.api_base.as_deref(), Some("http://stt:8080/v1"));
        assert_eq!(translate.api_key.as_deref(), Some("resolved-STT_KEY"));
        assert_eq!(translate.model.as_deref(), Some("translate-override"));
        assert_eq!(translate.provider.as_deref(), Some("translate-provider"));
        assert_eq!(translate.target_lang.as_deref(), Some("es"));
        assert!(translate.task.is_none());
        assert!(translate.language.is_none());

        assert_eq!(transcribe.task.as_deref(), Some("transcribe"));
        assert_eq!(transcribe.language.as_deref(), Some("en"));
        assert!(transcribe.target_lang.is_none());

        let mut source = TestSource::with_values([
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
            (ai_keys::AUDIO_TRANSLATE_ROUTING, "off"),
        ]);
        let translate = resolve_capability_binding(&mut source, AiCapability::AudioTranslate);
        assert_eq!(translate.routing, AiRouting::Off);
    }

    #[test]
    fn env_does_not_override_ai_embedding_keys() {
        let env = EnvGuard::new();
        env.set("GOBBY_EMBEDDING_URL", "http://env-embedding:11434");
        env.set("GOBBY_EMBEDDING_MODEL", "env-model");
        env.set("GOBBY_EMBEDDING_API_KEY", "env-key");
        env.set("GOBBY_EMBEDDING_QUERY_PREFIX", "env-prefix:");
        env.set("GOBBY_EMBEDDING_TIMEOUT_SECONDS", "7");

        let mut source = TestSource::with_values([
            (embedding_keys::AI_API_BASE, "http://new-embedding:11434"),
            (embedding_keys::AI_MODEL, "new-model"),
            (embedding_keys::AI_API_KEY, "$secret:AI_KEY"),
            (embedding_keys::AI_QUERY_PREFIX, "new-query:"),
            (embedding_keys::AI_TIMEOUT_SECONDS, "12"),
        ]);

        let resolution =
            resolve_embedding_config_resolution(&mut source).expect("embedding config");
        let config = resolution.config;

        assert_eq!(resolution.namespace, embedding_keys::AI_NAMESPACE);
        assert_eq!(config.api_base, "http://new-embedding:11434");
        assert_eq!(config.model, "new-model");
        assert_eq!(config.api_key.as_deref(), Some("resolved-AI_KEY"));
        assert_eq!(config.query_prefix.as_deref(), Some("new-query:"));
        assert_eq!(config.timeout_seconds, 12);
    }

    #[test]
    fn legacy_keys_not_honored() {
        let _env = EnvGuard::new();
        let legacy_keys = embedding_keys::legacy_keys();
        let mut source = TestSource::with_values([
            (
                leak_for_test(legacy_keys[1].clone()),
                "http://legacy-embedding:11434",
            ),
            (leak_for_test(legacy_keys[2].clone()), "legacy-model"),
            (leak_for_test(legacy_keys[3].clone()), "$secret:LEGACY_KEY"),
            (leak_for_test(legacy_keys[5].clone()), "legacy-query:"),
        ]);

        assert!(resolve_embedding_config_resolution(&mut source).is_none());
    }

    fn leak_for_test(value: String) -> &'static str {
        // Test fixtures need stable borrowed keys; leaking these tiny strings is intentional.
        Box::leak(value.into_boxed_str())
    }

    #[test]
    fn postgres_config_source_resolves_secrets() {
        let _env = EnvGuard::new();

        struct ConnectionLike {
            values: HashMap<&'static str, String>,
            secret_reads: usize,
        }

        struct PostgresConfigSource<'a> {
            conn: &'a mut ConnectionLike,
        }

        impl ConfigSource for PostgresConfigSource<'_> {
            fn config_value(&mut self, key: &str) -> Option<String> {
                self.conn.values.get(key).cloned()
            }

            fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
                self.conn.secret_reads += 1;
                Ok(format!("secret::{value}"))
            }
        }

        let mut conn = ConnectionLike {
            values: HashMap::from([
                (
                    embedding_keys::AI_API_BASE,
                    "http://stored-embedding:11434".to_string(),
                ),
                (
                    embedding_keys::AI_API_KEY,
                    "$secret:OPENAI_API_KEY".to_string(),
                ),
            ]),
            secret_reads: 0,
        };
        let config = {
            let mut source = PostgresConfigSource { conn: &mut conn };
            resolve_embedding_config(&mut source).expect("embedding config")
        };

        assert_eq!(
            config.api_key.as_deref(),
            Some("secret::$secret:OPENAI_API_KEY")
        );
        assert_eq!(conn.secret_reads, 2);
    }

    #[test]
    fn resolve_config_handles_json_encoded_store_values() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_raw_values([
            ("databases.falkordb.host", r#""json-falkor.local""#),
            ("databases.falkordb.port", r#""17001""#),
            ("databases.falkordb.requirepass", r#""$secret:FALKOR_PASS""#),
            ("databases.qdrant.url", r#""http://json-qdrant:6333""#),
            ("databases.qdrant.api_key", r#"["alpha",1]"#),
            (
                embedding_keys::AI_API_BASE,
                r#""http://json-embedding:11434""#,
            ),
            (embedding_keys::AI_MODEL, r#"["model",1]"#),
        ]);

        let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");
        let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");
        let embedding = resolve_embedding_config(&mut source).expect("embedding config");

        assert_eq!(falkordb.host, "json-falkor.local");
        assert_eq!(falkordb.port, 17001);
        assert_eq!(falkordb.password.as_deref(), Some("resolved-FALKOR_PASS"));
        assert_eq!(qdrant.url.as_deref(), Some("http://json-qdrant:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some(r#"["alpha",1]"#));
        assert_eq!(embedding.api_base, "http://json-embedding:11434");
        assert_eq!(embedding.model, r#"["model",1]"#);
    }

    #[test]
    fn qdrant_and_embedding_resolution_order() {
        {
            let env = EnvGuard::new();
            env.set("GOBBY_QDRANT_API_KEY", "env-qdrant-key");
            let mut source = TestSource::with_values([
                ("databases.qdrant.url", "http://stored-qdrant:6333"),
                ("databases.qdrant.api_key", "stored-qdrant-key"),
                (
                    embedding_keys::AI_API_BASE,
                    "http://stored-embedding:11434/v1",
                ),
                (embedding_keys::AI_MODEL, "stored-embedding-model"),
                (embedding_keys::AI_API_KEY, "$secret:EMBEDDING_KEY"),
                (embedding_keys::AI_QUERY_PREFIX, "stored-query-prefix:"),
            ]);

            let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");
            let embedding = resolve_embedding_config(&mut source).expect("embedding config");

            assert_eq!(qdrant.url.as_deref(), Some("http://stored-qdrant:6333"));
            assert_eq!(qdrant.api_key.as_deref(), Some("env-qdrant-key"));
            assert_eq!(embedding.api_base, "http://stored-embedding:11434/v1");
            assert_eq!(embedding.model, "stored-embedding-model");
            assert_eq!(embedding.api_key.as_deref(), Some("resolved-EMBEDDING_KEY"));
            assert_eq!(
                embedding.query_prefix.as_deref(),
                Some("stored-query-prefix:")
            );
            assert_eq!(embedding.timeout_seconds, EMBEDDING_DEFAULT_TIMEOUT_SECONDS);
        }

        let _env = EnvGuard::new();
        let mut default_source = TestSource::with_values([(
            embedding_keys::AI_API_BASE,
            "http://stored-embedding:11434/v1",
        )]);
        let default_embedding =
            resolve_embedding_config(&mut default_source).expect("embedding config");

        assert_eq!(default_embedding.model, EMBEDDING_DEFAULT_MODEL);
        assert!(default_embedding.query_prefix.is_none());
        assert_eq!(
            default_embedding.timeout_seconds,
            EMBEDDING_DEFAULT_TIMEOUT_SECONDS
        );
        assert!(resolve_qdrant_config(&mut TestSource::default()).is_none());
    }

    #[test]
    fn invalid_embedding_timeout_uses_default() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_values([
            (
                embedding_keys::AI_API_BASE,
                "http://stored-embedding:11434/v1",
            ),
            (embedding_keys::AI_TIMEOUT_SECONDS, "not-a-number"),
        ]);

        let embedding = resolve_embedding_config(&mut source).expect("embedding config");

        assert_eq!(embedding.timeout_seconds, EMBEDDING_DEFAULT_TIMEOUT_SECONDS);
    }

    #[test]
    fn falkordb_config_has_no_domain_graph_name() {
        let config = FalkorConfig {
            host: "falkor.local".to_string(),
            port: 16379,
            password: None,
        };

        assert!(!format!("{config:?}").contains("graph"));
        let forbidden = ["gobby", "_", "code"].concat();
        assert!(!include_str!("config.rs").contains(&forbidden));
    }

    #[test]
    fn qdrant_config_has_no_domain_collection_prefix() {
        let config = QdrantConfig {
            url: Some("http://qdrant:6333".to_string()),
            api_key: None,
        };

        assert!(!format!("{config:?}").contains("collection"));
    }

    #[test]
    fn embedding_keys_centralized() {
        let workspace = workspace_root();
        let offenders = embedding_key_literal_offenders(&workspace.join("crates"));

        assert!(
            offenders.is_empty(),
            "embedding config key literals must stay in gobby_core::config::embedding_keys: {offenders:?}"
        );
    }

    #[test]
    fn ci_guard_rejects_stray_literal() {
        let dir = tempfile::tempdir().expect("tempdir");
        let src = dir.path().join("src");
        std::fs::create_dir_all(&src).expect("create src");
        std::fs::write(
            src.join("bad.rs"),
            format!(r#"const BAD: &str = "{}";"#, embedding_keys::AI_API_BASE),
        )
        .expect("write bad source");

        let offenders = embedding_key_literal_offenders(dir.path());

        assert_eq!(offenders.len(), 1);
        assert!(offenders[0].ends_with("bad.rs"));
    }

    #[test]
    fn ci_guard_rejects_legacy_namespace() {
        let dir = tempfile::tempdir().expect("tempdir");
        let src = dir.path().join("src");
        std::fs::create_dir_all(&src).expect("create src");
        std::fs::write(
            src.join("bad.rs"),
            format!(
                r#"const BAD: &str = "{}";"#,
                embedding_keys::legacy_keys()[1]
            ),
        )
        .expect("write bad source");

        let offenders = embedding_key_literal_offenders(dir.path());

        assert_eq!(offenders.len(), 1);
        assert!(offenders[0].ends_with("bad.rs"));
    }

    fn workspace_root() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(std::path::Path::parent)
            .expect("workspace root")
            .to_path_buf()
    }

    fn embedding_key_literal_offenders(root: &std::path::Path) -> Vec<std::path::PathBuf> {
        let mut offenders = Vec::new();
        visit_embedding_key_literal_sources(root, &mut offenders);
        offenders
    }

    fn visit_embedding_key_literal_sources(
        path: &std::path::Path,
        offenders: &mut Vec<std::path::PathBuf>,
    ) {
        let entries = match std::fs::read_dir(path) {
            Ok(entries) => entries,
            Err(_) => return,
        };
        for entry in entries {
            let entry = entry.expect("directory entry");
            let path = entry.path();
            if path.is_dir() {
                visit_embedding_key_literal_sources(&path, offenders);
                continue;
            }
            if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                continue;
            }
            if embedding_key_literal_allowed_path(&path) {
                continue;
            }
            let source = std::fs::read_to_string(&path).expect("read source file");
            if guarded_embedding_keys()
                .iter()
                .any(|key| source.contains(key.as_str()))
            {
                offenders.push(path);
            }
        }
    }

    fn guarded_embedding_keys() -> Vec<String> {
        let mut keys = vec![
            embedding_keys::AI_PROVIDER,
            embedding_keys::AI_API_BASE,
            embedding_keys::AI_MODEL,
            embedding_keys::AI_API_KEY,
            embedding_keys::AI_QUERY_PREFIX,
            embedding_keys::AI_DIM,
            embedding_keys::AI_TIMEOUT_SECONDS,
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
        keys.extend(embedding_keys::legacy_keys());
        keys
    }

    fn embedding_key_literal_allowed_path(path: &std::path::Path) -> bool {
        let path = path.to_string_lossy();
        path.ends_with("crates/gcore/src/config.rs")
            || path.ends_with("tests.rs")
            || path.contains("/tests/")
    }
}
