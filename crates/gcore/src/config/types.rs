use serde::{Deserialize, Deserializer, Serialize};

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

/// Shared indexing behavior for gcode/gwiki consumers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexingConfig {
    pub respect_gitignore: bool,
}

impl Default for IndexingConfig {
    fn default() -> Self {
        Self {
            respect_gitignore: true,
        }
    }
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

    pub(crate) fn routing_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_ROUTING,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_ROUTING,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_ROUTING,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_ROUTING,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_ROUTING,
        }
    }

    pub(crate) fn transport_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_TRANSPORT,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_TRANSPORT,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_TRANSPORT,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_TRANSPORT,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_TRANSPORT,
        }
    }

    pub(crate) fn api_base_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_API_BASE,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_API_BASE,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_API_BASE,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_API_BASE,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_API_BASE,
        }
    }

    pub(crate) fn api_key_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_API_KEY,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_API_KEY,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_API_KEY,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_API_KEY,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_API_KEY,
        }
    }

    pub(crate) fn model_key(self) -> &'static str {
        match self {
            Self::Embed => ai_keys::EMBEDDINGS_MODEL,
            Self::AudioTranscribe => ai_keys::AUDIO_TRANSCRIBE_MODEL,
            Self::AudioTranslate => ai_keys::AUDIO_TRANSLATE_MODEL,
            Self::VisionExtract => ai_keys::VISION_EXTRACT_MODEL,
            Self::TextGenerate => ai_keys::TEXT_GENERATE_MODEL,
        }
    }

    pub(crate) fn provider_key(self) -> &'static str {
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

/// One text-generation provider/model candidate plus an optional reasoning pin.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FeatureCandidate {
    pub candidate: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
}

impl<'de> Deserialize<'de> for FeatureCandidate {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum WireFeatureCandidate {
            Label(String),
            Structured {
                candidate: String,
                #[serde(default)]
                reasoning_effort: Option<String>,
            },
        }

        match WireFeatureCandidate::deserialize(deserializer)? {
            WireFeatureCandidate::Label(candidate) => Ok(Self {
                candidate,
                reasoning_effort: None,
            }),
            WireFeatureCandidate::Structured {
                candidate,
                reasoning_effort,
            } => Ok(Self {
                candidate,
                reasoning_effort,
            }),
        }
    }
}

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
    /// Daemon feature profile (text_generate only); ignored when an explicit
    /// provider/model pair routes the call.
    pub profile: Option<String>,
    /// Ordered daemon feature candidates (text_generate only). This is parsed
    /// from the JSON string stored at `ai.text_generate.candidates`.
    pub candidates: Option<Vec<FeatureCandidate>>,
    /// Top-level reasoning effort hint for text-generation requests.
    pub reasoning_effort: Option<String>,
    /// Daemon feature profile for the grounded verification pass (text_generate
    /// only). `None` lets the consumer pick its default verify profile.
    pub verify_profile: Option<String>,
    /// Direct-mode model for the verification pass (text_generate only).
    /// Overrides [`model`](Self::model) for verify calls; `None` reuses `model`.
    pub verify_model: Option<String>,
    /// Direct-mode API key for the verification pass (text_generate only).
    /// `None` falls back to [`api_key`](Self::api_key); provider, api_base, and
    /// transport always inherit from the generate binding.
    pub verify_api_key: Option<String>,
}

/// Shared AI tuning values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AiTuning {
    pub max_concurrency: u8,
    pub keep_alive: Option<String>,
}

/// Canonical embedding config keys shared by gcore and consumer crates.
pub mod embedding_keys {
    pub const AI_NAMESPACE: &str = "ai.embeddings";

    pub const AI_PROVIDER: &str = "ai.embeddings.provider";
    pub const AI_API_BASE: &str = "ai.embeddings.api_base";
    pub const AI_MODEL: &str = "ai.embeddings.model";
    pub const AI_API_KEY: &str = "ai.embeddings.api_key";
    pub const AI_QUERY_PREFIX: &str = "ai.embeddings.query_prefix";
    pub const AI_DIM: &str = "ai.embeddings.dim";
    pub const AI_TIMEOUT_SECONDS: &str = "ai.embeddings.timeout_seconds";
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
    pub const EMBEDDINGS_QUERY_PREFIX: &str = super::embedding_keys::AI_QUERY_PREFIX;
    pub const EMBEDDINGS_DIM: &str = super::embedding_keys::AI_DIM;
    pub const EMBEDDINGS_TIMEOUT_SECONDS: &str = super::embedding_keys::AI_TIMEOUT_SECONDS;

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
    pub const TEXT_GENERATE_PROFILE: &str = "ai.text_generate.profile";
    pub const TEXT_GENERATE_CANDIDATES: &str = "ai.text_generate.candidates";
    pub const TEXT_GENERATE_REASONING_EFFORT: &str = "ai.text_generate.reasoning_effort";
    pub const TEXT_GENERATE_VERIFY_PROFILE: &str = "ai.text_generate.verify_profile";
    pub const TEXT_GENERATE_VERIFY_MODEL: &str = "ai.text_generate.verify_model";
    pub const TEXT_GENERATE_VERIFY_API_KEY: &str = "ai.text_generate.verify_api_key";

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
        EMBEDDINGS_QUERY_PREFIX,
        EMBEDDINGS_DIM,
        EMBEDDINGS_TIMEOUT_SECONDS,
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
        TEXT_GENERATE_PROFILE,
        TEXT_GENERATE_CANDIDATES,
        TEXT_GENERATE_REASONING_EFFORT,
        TEXT_GENERATE_VERIFY_PROFILE,
        TEXT_GENERATE_VERIFY_MODEL,
        TEXT_GENERATE_VERIFY_API_KEY,
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
