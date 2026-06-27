use std::collections::BTreeMap;

use serde::Deserialize;

use crate::ai_types::{AiError, TextResult, TokenUsage};
use crate::config::{AiCapability, FeatureCandidate};

#[derive(Debug, Clone, Copy)]
pub struct DaemonTranscriptionOptions<'a> {
    pub capability: AiCapability,
    pub language: Option<&'a str>,
    pub target_lang: Option<&'a str>,
    pub prompt: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DaemonEmbeddingResult {
    pub embeddings: Vec<Vec<f32>>,
    pub model: String,
    pub dim: usize,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct CodeWikiWriterOptions<'a> {
    pub cwd: Option<&'a str>,
    pub max_tokens: Option<usize>,
    pub profile: Option<&'a str>,
    pub candidates: Option<&'a [FeatureCandidate]>,
    pub timeout_seconds: Option<f64>,
    pub reasoning_effort: Option<&'a str>,
    pub page_kind: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct CodeWikiWriterResult {
    pub text: String,
    pub provider: String,
    pub model: String,
    #[serde(default)]
    pub usage: Option<TokenUsage>,
    pub elapsed_ms: u64,
    #[serde(default)]
    pub diagnostics: BTreeMap<String, serde_json::Value>,
}

impl CodeWikiWriterResult {
    pub fn from_wire_json(value: serde_json::Value) -> Result<Self, AiError> {
        serde_json::from_value(value).map_err(|error| AiError::parse_failure(error.to_string()))
    }

    pub fn into_text_result(self) -> TextResult {
        let mut metadata = BTreeMap::new();
        metadata.insert("provider".to_string(), self.provider);
        metadata.insert("elapsed_ms".to_string(), self.elapsed_ms.to_string());
        for (key, value) in self.diagnostics {
            if let Some(value) = value.as_str() {
                metadata.insert(format!("diagnostic.{key}"), value.to_string());
            } else if value.is_boolean() || value.is_number() {
                metadata.insert(format!("diagnostic.{key}"), value.to_string());
            }
        }
        TextResult {
            text: self.text,
            model: Some(self.model),
            applied_reasoning_effort: None,
            usage: self.usage,
            metadata,
        }
    }
}

impl Default for DaemonTranscriptionOptions<'_> {
    fn default() -> Self {
        Self {
            capability: AiCapability::AudioTranscribe,
            language: None,
            target_lang: None,
            prompt: None,
        }
    }
}
