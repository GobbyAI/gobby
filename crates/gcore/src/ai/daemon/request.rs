use bytes::Bytes;
use reqwest::blocking::multipart;
use serde_json::{Map, Value};
use std::io::Cursor;

use crate::ai_types::AiError;
use crate::config::{AiCapability, FeatureCandidate};

const TEXT_GENERATE_DEFAULT_PROFILE: &str = "feature_low";

pub(super) fn audio_capability(capability: AiCapability) -> Result<AiCapability, AiError> {
    match capability {
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => Ok(capability),
        other => Err(AiError::capability_unavailable(
            other.as_str(),
            "daemon voice transcription supports audio_transcribe and audio_translate",
        )),
    }
}

pub(super) fn multipart_form_with_file(
    bytes: Bytes,
    file_name: &str,
    mime: &str,
    capability: AiCapability,
) -> Result<multipart::Form, AiError> {
    let file_len = u64::try_from(bytes.len()).map_err(|_| {
        AiError::parse_failure("daemon multipart payload length exceeds this platform's u64 sizing")
    })?;
    let file_part = multipart::Part::reader_with_length(Cursor::new(bytes), file_len)
        .file_name(file_name.to_string())
        .mime_str(mime)
        .map_err(|error| {
            AiError::parse_failure(format!(
                "invalid {} MIME type {mime}: {error}",
                capability.as_str()
            ))
        })?;

    Ok(multipart::Form::new().part("file", file_part))
}

pub(super) fn add_optional_text(
    form: multipart::Form,
    name: &'static str,
    value: Option<&str>,
) -> multipart::Form {
    match non_empty(value) {
        Some(value) => form.text(name, value.to_string()),
        None => form,
    }
}

pub(super) struct TextRequestOptions<'a> {
    pub provider: Option<&'a str>,
    pub model: Option<&'a str>,
    pub project_id: Option<&'a str>,
    pub max_tokens: Option<usize>,
    pub profile: Option<&'a str>,
    pub candidates: Option<&'a [FeatureCandidate]>,
    pub reasoning_effort: Option<&'a str>,
}

pub(super) fn text_request_body(
    prompt: &str,
    system: Option<&str>,
    options: TextRequestOptions<'_>,
) -> Value {
    let mut body = Map::new();
    let provider = non_empty(options.provider);
    let model = non_empty(options.model);
    let candidates = options
        .candidates
        .filter(|candidates| !candidates.is_empty());
    body.insert("prompt".to_string(), Value::String(prompt.to_string()));
    insert_optional(&mut body, "system_prompt", system);
    insert_optional(&mut body, "provider", provider);
    insert_optional(&mut body, "model", model);
    if provider.is_none() && model.is_none() {
        match (non_empty(options.profile), candidates.is_some()) {
            (Some(profile), _) => {
                body.insert("profile".to_string(), Value::String(profile.to_string()));
            }
            (None, false) => {
                body.insert(
                    "profile".to_string(),
                    Value::String(TEXT_GENERATE_DEFAULT_PROFILE.to_string()),
                );
            }
            (None, true) => {}
        }
    }
    if let Some(candidates) = candidates {
        body.insert(
            "candidates".to_string(),
            serde_json::to_value(candidates).expect("feature candidates serialize"),
        );
    }
    insert_optional(&mut body, "reasoning_effort", options.reasoning_effort);
    insert_optional(&mut body, "project_id", options.project_id);
    if let Some(max_tokens) = options.max_tokens.filter(|value| *value > 0) {
        body.insert("max_tokens".to_string(), Value::from(max_tokens));
    }
    Value::Object(body)
}

pub(super) fn embeddings_request_body(
    input: &[String],
    is_query: bool,
    project_id: Option<&str>,
    provider: Option<&str>,
    model: Option<&str>,
) -> Value {
    let mut body = Map::new();
    body.insert(
        "input".to_string(),
        Value::Array(input.iter().cloned().map(Value::String).collect()),
    );
    body.insert("is_query".to_string(), Value::Bool(is_query));
    insert_optional(&mut body, "project_id", project_id);
    insert_optional(&mut body, "provider", provider);
    insert_optional(&mut body, "model", model);
    Value::Object(body)
}

fn insert_optional(body: &mut Map<String, Value>, name: &str, value: Option<&str>) {
    if let Some(value) = non_empty(value) {
        body.insert(name.to_string(), Value::String(value.to_string()));
    }
}

fn non_empty(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|value| !value.is_empty())
}
