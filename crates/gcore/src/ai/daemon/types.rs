use crate::config::AiCapability;

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
