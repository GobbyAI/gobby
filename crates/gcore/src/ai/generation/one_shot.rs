//! One-shot text generation (tools suppressed), routed by writing tier.
//!
//! The one-shot route is the existing single-prompt path used for verification, summaries,
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
use std::sync::OnceLock;

use reqwest::blocking::Client;
use reqwest::header::AUTHORIZATION;

use crate::ai::daemon::{
    GenerationBudget, generate_via_daemon_with_candidates, generate_via_daemon_with_max_tokens,
};
use crate::ai::{
    chat_api_root, chat_completion_content, chat_completion_model, chat_completion_usage,
    parse_json_response, reqwest_error, retry_with_backoff, timeout_for,
};
use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult};
use crate::config::{AiCapability, AiRouting, FeatureCandidate};

use super::profile::DirectGenerationTarget;
use super::tier::{GenerationTier, profile_for_tier};
use super::tool_loop::{ChatCompletionRequest, ChatMessage, ToolChoice};
use super::transport::build_request_body;

static DIRECT_HTTP_CLIENT: OnceLock<Client> = OnceLock::new();

/// Aggregate prose runs for minutes per page, so its daemon candidates get the
/// whole total budget; per-file and module generations keep tight budgets for
/// fast candidate failover (#18288, #17710).
fn budget_for_tier(tier: GenerationTier) -> GenerationBudget {
    match tier {
        GenerationTier::Aggregate => GenerationBudget::LongForm,
        GenerationTier::Standard | GenerationTier::Module => GenerationBudget::Interactive,
    }
}

fn direct_http_client() -> Result<Client, AiError> {
    if let Some(client) = DIRECT_HTTP_CLIENT.get() {
        return Ok(client.clone());
    }
    let client = Client::builder().build().map_err(reqwest_error)?;
    if DIRECT_HTTP_CLIENT.set(client.clone()).is_ok() {
        return Ok(client);
    }
    Ok(DIRECT_HTTP_CLIENT.get().cloned().unwrap_or(client))
}

/// One-shot generation for a writing tier on an already-resolved route.
///
/// `route` must be [`AiRouting::Daemon`]; [`AiRouting::Off`] returns
/// [`AiError::NotConfigured`]. `direct_target` is ignored: generation is
/// daemon-only.
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
    let _ = direct_target;
    let profile = profile_for_tier(tier, aggregate_override);
    dispatch_one_shot(route, || {
        generate_via_daemon_with_max_tokens(
            context,
            prompt,
            system,
            max_tokens,
            Some(profile.as_str()),
            budget_for_tier(tier),
        )
    })
}

fn dispatch_one_shot<T>(
    route: AiRouting,
    daemon: impl FnOnce() -> Result<T, AiError>,
) -> Result<T, AiError> {
    match route {
        AiRouting::Daemon => daemon(),
        AiRouting::Off => Err(AiError::not_configured(
            Some(AiCapability::TextGenerate.as_str().to_string()),
            "text generation route is off",
        )),
    }
}

/// One-shot generation with an explicit provider/model candidate chain pinned
/// for this call (codewiki's `--ai-aggregate-candidate`). The Daemon route
/// forwards the chain via the request's `candidates` field, superseding the
/// binding's profile/provider/model; the Direct route resolves exactly one
/// profile target and cannot honor a candidate chain, so explicit candidates
/// are rejected with a clear error instead of silently generating with an
/// unpinned model.
pub fn generate_one_shot_pinned(
    context: &AiContext,
    route: AiRouting,
    tier: GenerationTier,
    candidates: &[FeatureCandidate],
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
) -> Result<TextResult, AiError> {
    if candidates.is_empty() {
        return Err(AiError::not_configured(
            Some(AiCapability::TextGenerate.as_str().to_string()),
            "pinned one-shot generation requires at least one candidate",
        ));
    }
    match route {
        AiRouting::Daemon => generate_via_daemon_with_candidates(
            context,
            prompt,
            system,
            max_tokens,
            candidates,
            budget_for_tier(tier),
        ),
        AiRouting::Off => Err(AiError::not_configured(
            Some(AiCapability::TextGenerate.as_str().to_string()),
            "text generation route is off",
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
        tool_choice: ToolChoice::Auto,
        timeout: std::time::Duration::MAX,
    };
    let body = build_request_body(target, &request);

    let api_key = target
        .api_key
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let client = direct_http_client()?;
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

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::*;

    #[test]
    fn daemon_failure_does_not_fallback() {
        let daemon_calls = Cell::new(0);

        let result = dispatch_one_shot::<()>(AiRouting::Daemon, || {
            daemon_calls.set(daemon_calls.get() + 1);
            Err(AiError::not_configured(None, "daemon failed"))
        });

        assert!(result.is_err());
        assert_eq!(daemon_calls.get(), 1);
    }

    #[test]
    fn off_route_does_not_invoke_daemon() {
        let daemon_calls = Cell::new(0);

        let result = dispatch_one_shot::<()>(AiRouting::Off, || {
            daemon_calls.set(daemon_calls.get() + 1);
            Ok(())
        });

        assert!(result.is_err());
        assert_eq!(daemon_calls.get(), 0);
    }
}
