use bytes::Bytes;

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult, TranscriptionResult, VisionResult};
use crate::config::AiCapability;

use super::request::{
    add_optional_text, audio_capability, embeddings_request_body, multipart_form_with_file,
    text_request_body,
};
use super::response::{parse_daemon_embeddings, parse_daemon_transcription};
use super::transport::{daemon_client, daemon_url, read_local_cli_token, with_local_token};
use super::types::{DaemonEmbeddingResult, DaemonTranscriptionOptions};

const VOICE_TRANSCRIBE_PATH: &str = "/api/voice/transcribe";
const VISION_EXTRACT_PATH: &str = "/api/llm/vision/extract";
pub(super) const TEXT_GENERATE_PATH: &str = "/api/llm/generate";
const EMBEDDINGS_PATH: &str = "/api/embeddings";

pub fn transcribe_via_daemon(
    cfg: &AiContext,
    bytes: Vec<u8>,
    file_name: &str,
    mime: &str,
    options: DaemonTranscriptionOptions<'_>,
) -> Result<TranscriptionResult, AiError> {
    let capability = audio_capability(options.capability)?;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(VOICE_TRANSCRIBE_PATH);
    let file_name = file_name.to_string();
    let mime = mime.to_string();
    let language = options
        .language
        .or(binding.language.as_deref())
        .map(str::to_string);
    let target_lang = options
        .target_lang
        .or(binding.target_lang.as_deref())
        .map(str::to_string);
    let prompt = options.prompt.map(str::to_string);
    let provider = binding.provider.clone();
    let model = binding.model.clone();
    let project_id = cfg.project_id.clone();
    let bytes = Bytes::from(bytes);
    let _permit = cfg.limiter.acquire();

    let value = super::super::retry_with_backoff(
        || {
            let form = multipart_form_with_file(bytes.clone(), &file_name, &mime, capability)?
                .text("capability", capability.as_str().to_string());
            let form = add_optional_text(form, "provider", provider.as_deref());
            let form = add_optional_text(form, "model", model.as_deref());
            let form = add_optional_text(form, "language", language.as_deref());
            let form = add_optional_text(form, "target_lang", target_lang.as_deref());
            let form = add_optional_text(form, "prompt", prompt.as_deref());
            let form = add_optional_text(form, "project_id", project_id.as_deref());
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .multipart(form),
                &token,
            );
            super::super::parse_json_response(request.send().map_err(super::super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    parse_daemon_transcription(value)
}

pub fn describe_image_via_daemon(
    cfg: &AiContext,
    bytes: Vec<u8>,
    file_name: &str,
    mime: &str,
) -> Result<VisionResult, AiError> {
    let capability = AiCapability::VisionExtract;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(VISION_EXTRACT_PATH);
    let file_name = file_name.to_string();
    let mime = mime.to_string();
    let provider = binding.provider.clone();
    let model = binding.model.clone();
    let project_id = cfg.project_id.clone();
    let bytes = Bytes::from(bytes);
    let _permit = cfg.limiter.acquire();

    let value = super::super::retry_with_backoff(
        || {
            let form = multipart_form_with_file(bytes.clone(), &file_name, &mime, capability)?;
            let form = add_optional_text(form, "provider", provider.as_deref());
            let form = add_optional_text(form, "model", model.as_deref());
            let form = add_optional_text(form, "project_id", project_id.as_deref());
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .multipart(form),
                &token,
            );
            super::super::parse_json_response(request.send().map_err(super::super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    VisionResult::from_wire_json(value)
}

pub fn generate_via_daemon(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
) -> Result<TextResult, AiError> {
    generate_via_daemon_with_max_tokens(cfg, prompt, system, None, None)
}

/// `profile` overrides the binding's configured daemon feature profile for
/// this call; both are sent only when provider/model are unset (explicit
/// provider/model > profile > daemon feature_low default).
pub fn generate_via_daemon_with_max_tokens(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
    profile: Option<&str>,
) -> Result<TextResult, AiError> {
    let capability = AiCapability::TextGenerate;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(TEXT_GENERATE_PATH);
    let body = text_request_body(
        prompt,
        system,
        binding.provider.as_deref(),
        binding.model.as_deref(),
        cfg.project_id.as_deref(),
        max_tokens,
        profile.or(binding.profile.as_deref()),
    );
    let _permit = cfg.limiter.acquire();

    let value = super::super::retry_with_backoff(
        || {
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .json(&body),
                &token,
            );
            super::super::parse_json_response(request.send().map_err(super::super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    TextResult::from_wire_json(value)
}

pub fn embed_via_daemon(
    cfg: &AiContext,
    input: &[String],
    is_query: bool,
) -> Result<DaemonEmbeddingResult, AiError> {
    let capability = AiCapability::Embed;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(EMBEDDINGS_PATH);
    let body = embeddings_request_body(
        input,
        is_query,
        cfg.project_id.as_deref(),
        binding.provider.as_deref(),
        binding.model.as_deref(),
    );
    let _permit = cfg.limiter.acquire();

    let value = super::super::retry_with_backoff(
        || {
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .json(&body),
                &token,
            );
            super::super::parse_json_response(request.send().map_err(super::super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    parse_daemon_embeddings(value, input.len())
}
