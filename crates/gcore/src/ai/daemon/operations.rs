use bytes::Bytes;

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult, TranscriptionResult, VisionResult};
use crate::config::{AiCapability, FeatureCandidate};

use super::request::{
    TextRequestOptions, add_optional_text, audio_capability, embeddings_request_body,
    multipart_form_with_file, text_request_body,
};
use super::response::{parse_daemon_embeddings, parse_daemon_transcription};
use super::transport::{daemon_client, daemon_url, read_local_cli_token, with_grant_presentation};
use super::types::{DaemonEmbeddingResult, DaemonTranscriptionOptions};
use crate::grant::GrantBundle;

const VOICE_TRANSCRIBE_PATH: &str = "/api/voice/transcribe";
const VISION_EXTRACT_PATH: &str = "/api/llm/vision/extract";
pub(super) const TEXT_GENERATE_PATH: &str = "/api/llm/generate";
const FAST_CANDIDATE_TIMEOUT_SECONDS: u64 = 30;
const SPAWN_COLD_CANDIDATE_TIMEOUT_SECONDS: u64 = 60;
const TOTAL_GENERATION_TIMEOUT_SECONDS: u64 = 1200;

/// Per-candidate budget class for daemon text generation (#18288).
///
/// `Interactive` keeps the tight per-candidate budgets that let a failing
/// candidate fail over quickly on short generations (#17710). `LongForm`
/// gives each candidate the whole total budget — codewiki aggregate pages
/// legitimately generate for minutes, and the daemon route caps raised
/// per-candidate budgets at its configured total.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GenerationBudget {
    Interactive,
    LongForm,
}

impl GenerationBudget {
    fn candidate_timeouts(self) -> (u64, u64) {
        match self {
            Self::Interactive => (
                FAST_CANDIDATE_TIMEOUT_SECONDS,
                SPAWN_COLD_CANDIDATE_TIMEOUT_SECONDS,
            ),
            Self::LongForm => (
                TOTAL_GENERATION_TIMEOUT_SECONDS,
                TOTAL_GENERATION_TIMEOUT_SECONDS,
            ),
        }
    }
}
const EMBEDDINGS_PATH: &str = "/api/embeddings";

fn presented_grant(cfg: &AiContext) -> Result<GrantBundle, AiError> {
    if let Some(state) = &cfg.grant {
        return Ok(state.bundle.clone());
    }
    let root = std::env::current_dir()
        .ok()
        .and_then(|cwd| crate::project::find_project_root(&cwd))
        .ok_or_else(|| {
            AiError::not_configured(None, "daemon AI requests require a presented runtime grant")
        })?;
    crate::grant::acquire(root)
        .map(|acquired| acquired.bundle)
        .map_err(|error| AiError::not_configured(None, error.to_string()))
}

pub fn transcribe_via_daemon(
    cfg: &AiContext,
    bytes: Vec<u8>,
    file_name: &str,
    mime: &str,
    options: DaemonTranscriptionOptions<'_>,
) -> Result<TranscriptionResult, AiError> {
    let capability = audio_capability(options.capability)?;
    cfg.require_granted(capability)?;
    let grant = presented_grant(cfg)?;
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
            let request = with_grant_presentation(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .multipart(form),
                &token,
                &grant,
            )?;
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
    cfg.require_granted(capability)?;
    let grant = presented_grant(cfg)?;
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
            let request = with_grant_presentation(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .multipart(form),
                &token,
                &grant,
            )?;
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
    generate_via_daemon_with_max_tokens(
        cfg,
        prompt,
        system,
        None,
        None,
        GenerationBudget::Interactive,
    )
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
    budget: GenerationBudget,
) -> Result<TextResult, AiError> {
    generate_text_via_daemon(cfg, prompt, system, max_tokens, profile, None, budget)
}

/// Pin an explicit provider/model candidate chain for this one call, overriding
/// the binding's profile/provider/model/reasoning. Each [`FeatureCandidate`]
/// carries its own optional `reasoning_effort`. Used by callers that need a
/// specific model (e.g. codewiki's aggregate writer under
/// `--ai-aggregate-candidate`) regardless of the binding's default daemon
/// feature profile.
pub fn generate_via_daemon_with_candidates(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
    candidates: &[FeatureCandidate],
    budget: GenerationBudget,
) -> Result<TextResult, AiError> {
    generate_text_via_daemon(
        cfg,
        prompt,
        system,
        max_tokens,
        None,
        Some(candidates),
        budget,
    )
}

fn generate_text_via_daemon(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
    profile: Option<&str>,
    candidates_override: Option<&[FeatureCandidate]>,
    budget: GenerationBudget,
) -> Result<TextResult, AiError> {
    let capability = AiCapability::TextGenerate;
    cfg.require_granted(capability)?;
    let grant = presented_grant(cfg)?;
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(TEXT_GENERATE_PATH);
    let (candidate_timeout, cli_candidate_timeout) = budget.candidate_timeouts();
    // An explicit daemon candidate chain pins the exact provider/model
    // sequence. Otherwise the daemon selects candidates from the requested
    // feature profile; standalone binding fields never cross this boundary.
    let options = match candidates_override {
        Some(candidates) => TextRequestOptions {
            provider: None,
            model: None,
            project_id: cfg.project_id.as_deref(),
            max_tokens,
            profile: None,
            candidates: Some(candidates),
            reasoning_effort: None,
            candidate_timeout_seconds: Some(candidate_timeout),
            cli_candidate_timeout_seconds: Some(cli_candidate_timeout),
            total_timeout_seconds: Some(TOTAL_GENERATION_TIMEOUT_SECONDS),
        },
        None => TextRequestOptions {
            provider: None,
            model: None,
            project_id: cfg.project_id.as_deref(),
            max_tokens,
            profile,
            candidates: None,
            reasoning_effort: None,
            candidate_timeout_seconds: Some(candidate_timeout),
            cli_candidate_timeout_seconds: Some(cli_candidate_timeout),
            total_timeout_seconds: Some(TOTAL_GENERATION_TIMEOUT_SECONDS),
        },
    };
    let body = text_request_body(prompt, system, options);
    let _permit = cfg.limiter.acquire();

    let value = super::super::retry_with_backoff(
        || {
            let request = with_grant_presentation(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .json(&body),
                &token,
                &grant,
            )?;
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
    cfg.require_granted(capability)?;
    let grant = presented_grant(cfg)?;
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
            let request = with_grant_presentation(
                client
                    .post(&url)
                    .timeout(super::super::timeout_for(capability))
                    .json(&body),
                &token,
                &grant,
            )?;
            super::super::parse_json_response(request.send().map_err(super::super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    parse_daemon_embeddings(value, input.len())
}
