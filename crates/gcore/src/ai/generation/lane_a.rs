//! Lane A: one-shot text generation (tools suppressed), routed by writing tier.
//!
//! Lane A is the existing single-prompt path used for verification, summaries,
//! and per-symbol/per-file prose. This module adds the tier -> feature-profile
//! routing on top of it so both the Daemon and Direct routes honor the same
//! mapping:
//!
//! * **Daemon** forwards the resolved profile name to `/api/llm/generate`; the
//!   daemon owns provider/model selection for that profile.
//! * **Direct** resolves the profile to a concrete
//!   [`DirectGenerationTarget`] (provider/model/api_base/api_key) and posts an
//!   OpenAI-compatible chat completion with no tools.

use std::collections::BTreeMap;

use reqwest::blocking::Client;
use reqwest::header::AUTHORIZATION;

use crate::ai::daemon::generate_via_daemon_with_max_tokens;
use crate::ai::text::chat_completion_usage;
use crate::ai::{
    chat_api_root, chat_completion_content, chat_completion_model, parse_json_response,
    reqwest_error, retry_with_backoff, timeout_for,
};
use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult};
use crate::config::{AiCapability, AiRouting};

use super::profile::DirectGenerationTarget;
use super::tier::{GenerationTier, profile_for_tier};
use super::tool_loop::{ChatCompletionRequest, ChatMessage};
use super::transport::build_request_body;

/// One-shot generation for a writing tier on an already-resolved route.
///
/// `route` must be [`AiRouting::Daemon`] or [`AiRouting::Direct`]; `Off`/`Auto`
/// return [`AiError::NotConfigured`]. For the Direct route, `direct_target` must
/// carry the profile target the caller resolved with
/// [`super::profile::resolve_direct_generation_target`].
#[allow(clippy::too_many_arguments)]
pub fn generate_one_shot(
    context: &AiContext,
    route: AiRouting,
    tier: GenerationTier,
    aggregate_override: Option<&str>,
    direct_target: Option<&DirectGenerationTarget>,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
) -> Result<TextResult, AiError> {
    let profile = profile_for_tier(tier, aggregate_override);
    match route {
        AiRouting::Daemon => generate_via_daemon_with_max_tokens(
            context,
            prompt,
            system,
            max_tokens,
            Some(profile.as_str()),
        ),
        AiRouting::Direct => {
            let target = direct_target.ok_or_else(|| {
                AiError::not_configured(
                    Some(AiCapability::TextGenerate.as_str().to_string()),
                    "direct one-shot generation requires a resolved profile target",
                )
            })?;
            generate_text_with_target(context, target, prompt, system, max_tokens)
        }
        AiRouting::Off | AiRouting::Auto => Err(AiError::not_configured(
            Some(AiCapability::TextGenerate.as_str().to_string()),
            "text generation route is off or unresolved (Auto); resolve to Daemon or Direct first",
        )),
    }
}

/// Direct-route one-shot completion honoring an explicit profile target.
///
/// Unlike [`crate::ai::text::generate_text_with_max_tokens`], which reads the
/// base `text_generate` binding off the context, this honors a profile-resolved
/// `target` so `feature_low/mid/high` route to their own provider/model/api_key.
pub fn generate_text_with_target(
    context: &AiContext,
    target: &DirectGenerationTarget,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
) -> Result<TextResult, AiError> {
    let capability = AiCapability::TextGenerate;
    let api_base = target.api_base().ok_or_else(|| {
        AiError::not_configured(
            Some(capability.as_str().to_string()),
            "ai.text_generate profile api_base is required for direct chat completions",
        )
    })?;
    let url = format!("{}/v1/chat/completions", chat_api_root(api_base));

    let mut messages = Vec::new();
    if let Some(system) = system.map(str::trim).filter(|value| !value.is_empty()) {
        messages.push(ChatMessage::system(system));
    }
    messages.push(ChatMessage::user(prompt));
    let request = ChatCompletionRequest {
        messages: &messages,
        tools: &[],
        max_tokens,
    };
    let body = build_request_body(target, &request);

    let api_key = target
        .api_key
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let client = Client::builder().build().map_err(reqwest_error)?;
    let _permit = context.limiter.acquire();
    let value = retry_with_backoff(
        || {
            let mut http = client
                .post(&url)
                .timeout(timeout_for(capability))
                .json(&body);
            if let Some(api_key) = api_key.as_deref() {
                http = http.header(AUTHORIZATION, format!("Bearer {api_key}"));
            }
            parse_json_response(http.send().map_err(reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    Ok(TextResult {
        text: chat_completion_content(&value)?,
        model: chat_completion_model(&value),
        applied_reasoning_effort: None,
        usage: chat_completion_usage(&value),
        metadata: BTreeMap::new(),
    })
}
