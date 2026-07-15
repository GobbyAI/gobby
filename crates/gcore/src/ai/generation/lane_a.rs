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

use crate::ai::daemon::{
    GenerationBudget, generate_via_daemon_with_candidates, generate_via_daemon_with_max_tokens,
};
use crate::ai::text::chat_completion_usage;
use crate::ai::{
    chat_api_root, chat_completion_content, chat_completion_model, parse_json_response,
    reqwest_error, retry_with_backoff, timeout_for,
};
use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult};
use crate::config::{AiCapability, AiRouting, FeatureCandidate};

use super::profile::DirectGenerationTarget;
use super::tier::{GenerationTier, profile_for_tier};

/// Aggregate prose runs for minutes per page, so its daemon candidates get the
/// whole total budget; per-file and module generations keep tight budgets for
/// fast candidate failover (#18288, #17710).
fn budget_for_tier(tier: GenerationTier) -> GenerationBudget {
    match tier {
        GenerationTier::Aggregate => GenerationBudget::LongForm,
        GenerationTier::Standard | GenerationTier::Module => GenerationBudget::Interactive,
    }
}
use super::tool_loop::{ChatCompletionRequest, ChatMessage, ToolChoice};
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
    dispatch_one_shot(
        route,
        || {
            generate_via_daemon_with_max_tokens(
                context,
                prompt,
                system,
                max_tokens,
                Some(profile.as_str()),
                budget_for_tier(tier),
            )
        },
        || {
            let target = direct_target.ok_or_else(|| {
                AiError::not_configured(
                    Some(AiCapability::TextGenerate.as_str().to_string()),
                    "direct one-shot generation requires a resolved profile target",
                )
            })?;
            generate_text_with_target(context, target, prompt, system, max_tokens)
        },
    )
}

fn dispatch_one_shot<T>(
    route: AiRouting,
    daemon: impl FnOnce() -> Result<T, AiError>,
    direct: impl FnOnce() -> Result<T, AiError>,
) -> Result<T, AiError> {
    match route {
        AiRouting::Daemon => daemon(),
        AiRouting::Direct => direct(),
        AiRouting::Off | AiRouting::Auto => Err(AiError::not_configured(
            Some(AiCapability::TextGenerate.as_str().to_string()),
            "text generation route is off or unresolved (Auto); resolve to Daemon or Direct first",
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
        AiRouting::Direct => Err(AiError::not_configured(
            Some(AiCapability::TextGenerate.as_str().to_string()),
            "explicit generation candidates are unsupported on the Direct route (it resolves a \
             single profile target); use the daemon route, or pin a profile via an aggregate \
             profile override instead",
        )),
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
        tool_choice: ToolChoice::Auto,
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

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::*;

    #[test]
    fn daemon_failure_never_invokes_direct_lane() {
        let daemon_calls = Cell::new(0);
        let direct_calls = Cell::new(0);

        let result = dispatch_one_shot::<()>(
            AiRouting::Daemon,
            || {
                daemon_calls.set(daemon_calls.get() + 1);
                Err(AiError::not_configured(None, "daemon failed"))
            },
            || {
                direct_calls.set(direct_calls.get() + 1);
                Ok(())
            },
        );

        assert!(result.is_err());
        assert_eq!(daemon_calls.get(), 1);
        assert_eq!(direct_calls.get(), 0);
    }

    #[test]
    fn direct_failure_never_invokes_daemon_lane() {
        let daemon_calls = Cell::new(0);
        let direct_calls = Cell::new(0);

        let result = dispatch_one_shot::<()>(
            AiRouting::Direct,
            || {
                daemon_calls.set(daemon_calls.get() + 1);
                Ok(())
            },
            || {
                direct_calls.set(direct_calls.get() + 1);
                Err(AiError::not_configured(None, "direct failed"))
            },
        );

        assert!(result.is_err());
        assert_eq!(daemon_calls.get(), 0);
        assert_eq!(direct_calls.get(), 1);
    }
}
