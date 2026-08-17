use std::time::Duration;

use gobby_core::ai::generation::{GenerationTier, generate_one_shot, generate_one_shot_pinned};
use gobby_core::ai::{
    AiNoticeKind,
    daemon::{GenerationBudget, generate_via_daemon_with_max_tokens},
    effective_route, resolve_route_observed,
};
use gobby_core::ai_types::AiError;
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::code::CodeEngineRuntime;
use crate::commands::code::{
    CodewikiAiOptions, CodewikiAiOutcome, DEFAULT_VERIFY_PROFILE, PromptTier, SyncTextGenerator,
    SyncTextVerifier, TextGenerator, prompts,
};

use super::outcome::{
    GenerationFailureCause, GenerationOutcome, clean_generated, is_model_refusal, is_prompt_echo,
};
use super::routing::{generation_tier, resolve_ai_context, resolve_direct_tier_targets};

/// Backoff between generation attempts; the array length bounds the retries.
pub(super) const GENERATION_RETRY_BACKOFF: [Duration; 2] =
    [Duration::from_millis(200), Duration::from_millis(500)];

pub(crate) struct ResolvedTextGenerator {
    /// Thread-safe so the bounded file-page worker pool (#17532) can share it;
    /// serial call sites adapt it to the `FnMut` [`TextGenerator`] surface.
    pub(crate) generator: Option<Box<SyncTextGenerator<'static>>>,
    pub(crate) ai_route: AiRouting,
    pub(crate) ai_fallback: bool,
    pub(crate) no_generator_reason: Option<AiNoticeKind>,
}

impl ResolvedTextGenerator {
    fn skipped(
        ai_route: AiRouting,
        ai_fallback: bool,
        no_generator_reason: Option<AiNoticeKind>,
    ) -> Self {
        Self {
            generator: None,
            ai_route,
            ai_fallback,
            no_generator_reason,
        }
    }

    pub(crate) fn ai_outcome(&self) -> CodewikiAiOutcome {
        if self.generator.is_some() {
            CodewikiAiOutcome::generated(self.ai_route, self.ai_fallback)
        } else {
            CodewikiAiOutcome::skipped(self.ai_route, self.ai_fallback)
        }
    }

    pub(crate) fn notice_kind(&self) -> Option<AiNoticeKind> {
        self.no_generator_reason
    }
}

pub(crate) fn resolve_text_generator(
    ctx: &CodeEngineRuntime,
    ai: &CodewikiAiOptions,
) -> ResolvedTextGenerator {
    let ai_context = resolve_ai_context(ctx, ai.routing);
    let observed = resolve_route_observed(&ai_context, AiCapability::TextGenerate);
    let route = observed.route;
    if matches!(route, AiRouting::Off) {
        return ResolvedTextGenerator::skipped(route, observed.fallback, observed.reason);
    }

    let aggregate_profile = ai.aggregate_profile.clone();
    let aggregate_candidates = ai.aggregate_candidates.clone();
    let direct_targets = matches!(route, AiRouting::Daemon)
        .then(|| resolve_direct_tier_targets(ctx, aggregate_profile.as_deref()));
    if direct_targets
        .as_ref()
        .is_some_and(|targets| !targets.all_tiers_usable())
    {
        return ResolvedTextGenerator::skipped(
            route,
            observed.fallback,
            Some(AiNoticeKind::NoGenerator),
        );
    }
    let max_tokens = ai.prose_depth.max_tokens();
    let register = ai.register;
    let warned = std::sync::atomic::AtomicBool::new(false);
    let quiet = ctx.quiet;
    let generator: Box<SyncTextGenerator<'static>> = Box::new(move |prompt, system, tier| {
        let gen_tier = generation_tier(tier);
        let target = direct_targets
            .as_ref()
            .map(|targets| targets.for_tier(gen_tier));
        let system = prompts::with_register(system, register);
        let prompt = bound_one_shot_prompt(prompt);
        let pinned = gen_tier == GenerationTier::Aggregate && !aggregate_candidates.is_empty();
        let result = generate_with_bounded_retry(|| {
            if pinned {
                generate_one_shot_pinned(
                    &ai_context,
                    route,
                    gen_tier,
                    &aggregate_candidates,
                    prompt.as_str(),
                    Some(system.as_ref()),
                    max_tokens,
                )
            } else {
                generate_one_shot(
                    &ai_context,
                    route,
                    gen_tier,
                    aggregate_profile.as_deref(),
                    target,
                    prompt.as_str(),
                    Some(system.as_ref()),
                    max_tokens,
                )
            }
        });
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned.swap(true, std::sync::atomic::Ordering::Relaxed) {
                    eprintln!(
                        "text generation failed; affected codewiki docs fall back to AST-only \
                         content and record degraded: true: {error}"
                    );
                }
                None
            }
        }
    });
    ResolvedTextGenerator {
        generator: Some(generator),
        ai_route: route,
        ai_fallback: observed.fallback,
        no_generator_reason: None,
    }
}

pub(crate) fn resolve_text_verifier(
    ctx: &CodeEngineRuntime,
    ai: &CodewikiAiOptions,
) -> Option<Box<SyncTextVerifier<'static>>> {
    let mut ai_context = resolve_ai_context(ctx, ai.routing);
    let route = effective_route(&ai_context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off) {
        return None;
    }

    let binding = ai_context.binding(AiCapability::TextGenerate);
    let verify_profile = ai
        .verify_profile
        .clone()
        .or_else(|| binding.verify_profile.clone())
        .unwrap_or_else(|| DEFAULT_VERIFY_PROFILE.to_string());
    let verify_model = ai
        .verify_model
        .clone()
        .or_else(|| binding.verify_model.clone());
    let verify_api_key = ai
        .verify_api_key
        .clone()
        .or_else(|| binding.verify_api_key.clone());

    if matches!(route, AiRouting::Daemon) {
        let text_generate = &mut ai_context.bindings.text_generate;
        if let Some(model) = verify_model {
            text_generate.model = Some(model);
        }
        if let Some(api_key) = verify_api_key {
            text_generate.api_key = Some(api_key);
        }
    }

    let quiet = ctx.quiet;
    let warned = std::sync::atomic::AtomicBool::new(false);
    Some(Box::new(move |prompt: &str, system: &str| {
        let result = generate_with_bounded_retry(|| match route {
            AiRouting::Daemon => generate_via_daemon_with_max_tokens(
                &ai_context,
                prompt,
                Some(system),
                None,
                Some(verify_profile.as_str()),
                GenerationBudget::Interactive,
            ),
            AiRouting::Off => {
                unreachable!("non-generating routes returned above")
            }
        });
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned.swap(true, std::sync::atomic::Ordering::Relaxed) {
                    eprintln!(
                        "codewiki verification unavailable; generated narratives ship \
                         unverified (degraded: false): {error}"
                    );
                }
                None
            }
        }
    }))
}

pub(crate) fn generate_with_bounded_retry<T>(
    mut call: impl FnMut() -> Result<T, AiError>,
) -> Result<T, AiError> {
    generate_with_bounded_retry_and_sleep(&mut call, std::thread::sleep)
}

fn generate_with_bounded_retry_and_sleep<T>(
    mut call: impl FnMut() -> Result<T, AiError>,
    mut sleep: impl FnMut(Duration),
) -> Result<T, AiError> {
    let mut result = call();
    for backoff in GENERATION_RETRY_BACKOFF {
        match &result {
            Err(error) if retryable_generation_error(error) => {
                sleep(error.retry_after().unwrap_or(backoff));
                result = call();
            }
            _ => break,
        }
    }
    result
}

fn retryable_generation_error(error: &AiError) -> bool {
    if error.is_timeout() {
        return false;
    }
    match error {
        AiError::TransportFailure { .. } | AiError::RateLimited { .. } => true,
        AiError::HttpStatus { status, .. } => *status >= 500,
        AiError::CapabilityUnavailable { .. }
        | AiError::NotConfigured { .. }
        | AiError::ParseFailure { .. }
        | AiError::Grant { .. } => false,
    }
}

const ONE_SHOT_PROMPT_MAX_BYTES: usize = 32 * 1024;

fn bound_one_shot_prompt(prompt: &str) -> String {
    if prompt.len() <= ONE_SHOT_PROMPT_MAX_BYTES {
        return prompt.to_string();
    }
    let mut end = ONE_SHOT_PROMPT_MAX_BYTES;
    while end > 0 && !prompt.is_char_boundary(end) {
        end -= 1;
    }
    format!(
        "{}\n\n[Input truncated to fit the model context.]",
        &prompt[..end]
    )
}

pub(crate) fn maybe_generate(
    generate: &mut Option<&mut TextGenerator<'_>>,
    prompt: &str,
    system: &str,
    tier: PromptTier,
) -> GenerationOutcome {
    match generate.as_deref_mut() {
        None => GenerationOutcome::skipped(),
        Some(generate) => match generate(prompt, system, tier) {
            Some(text) if is_prompt_echo(&text, prompt) => {
                GenerationOutcome::rejected(GenerationFailureCause::PromptEcho)
            }
            Some(text) if is_model_refusal(&text) => {
                GenerationOutcome::rejected(GenerationFailureCause::Refusal)
            }
            Some(text) => GenerationOutcome::generated(text),
            None => GenerationOutcome::unavailable(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::super::outcome::GenerationContent;
    use super::*;
    use crate::commands::code::{AiGenerationStatus, prompts};

    #[test]
    fn bound_one_shot_prompt_passes_small_prompts_through() {
        let prompt = "small module prompt";
        assert_eq!(bound_one_shot_prompt(prompt), prompt);
    }

    #[test]
    fn bound_one_shot_prompt_truncates_oversized_prompt_on_a_char_boundary() {
        let prompt = "é".repeat(ONE_SHOT_PROMPT_MAX_BYTES);
        let bounded = bound_one_shot_prompt(&prompt);
        assert!(std::str::from_utf8(bounded.as_bytes()).is_ok());
        assert!(bounded.contains("[Input truncated to fit the model context.]"));
        assert!(bounded.len() <= ONE_SHOT_PROMPT_MAX_BYTES + 64);
    }

    #[test]
    fn resolved_text_generator_classifies_notice_kinds() {
        let no_generator = ResolvedTextGenerator::skipped(
            AiRouting::Daemon,
            false,
            Some(AiNoticeKind::NoGenerator),
        );

        assert_eq!(no_generator.notice_kind(), Some(AiNoticeKind::NoGenerator));
        assert_eq!(no_generator.ai_outcome().route, AiRouting::Daemon);
        assert_eq!(
            no_generator.ai_outcome().status,
            AiGenerationStatus::Skipped
        );

        let daemon_with_stale_fallback_flag = ResolvedTextGenerator {
            generator: Some(Box::new(|_, _, _| Some("generated".to_string()))),
            ai_route: AiRouting::Daemon,
            ai_fallback: true,
            no_generator_reason: None,
        };

        assert_eq!(daemon_with_stale_fallback_flag.notice_kind(), None);
        assert_eq!(
            daemon_with_stale_fallback_flag.ai_outcome().status,
            AiGenerationStatus::Generated
        );
        assert_eq!(
            daemon_with_stale_fallback_flag.ai_outcome().route,
            AiRouting::Daemon
        );
    }

    #[test]
    fn maybe_generate_classifies_distinct_failure_causes() {
        let long_prompt = "Document the module in full, covering responsibilities, \
                           collaborators, data flow, and failure modes with grounded citations.";

        let mut echoing = |prompt: &str, _: &str, _: PromptTier| Some(prompt.to_string());
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut echoing);
        let outcome = maybe_generate(&mut generate, long_prompt, "system", PromptTier::Aggregate);
        assert_eq!(
            outcome.failure_cause(),
            Some(GenerationFailureCause::PromptEcho)
        );

        let mut refusing =
            |_: &str, _: &str, _: PromptTier| Some("I cannot write this page.".to_string());
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut refusing);
        let outcome = maybe_generate(&mut generate, long_prompt, "system", PromptTier::Aggregate);
        assert_eq!(
            outcome.failure_cause(),
            Some(GenerationFailureCause::Refusal)
        );

        let mut failing = |_: &str, _: &str, _: PromptTier| None;
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut failing);
        let outcome = maybe_generate(&mut generate, long_prompt, "system", PromptTier::Aggregate);
        assert_eq!(
            outcome.failure_cause(),
            Some(GenerationFailureCause::Unavailable)
        );

        let mut generate = None::<&mut TextGenerator<'_>>;
        let outcome = maybe_generate(&mut generate, long_prompt, "system", PromptTier::Aggregate);
        assert_eq!(outcome.failure_cause(), None);
        assert!(matches!(outcome.into_content(), GenerationContent::Skipped));
    }

    #[test]
    fn prompt_echo_is_rejected_as_failed_generation() {
        let prompt = prompts::module_prompt(
            "crates/gcode",
            &[prompts::ChildSummary {
                name: "crates/gcode/Cargo.toml".to_string(),
                summary: "Manifest for the gcode binary.".to_string(),
            }],
            &[],
            &[],
            &[],
            &crate::commands::code::RelationshipFacts::default(),
        );

        let mut echoing = |prompt: &str, _system: &str, _tier: PromptTier| Some(prompt.to_string());
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut echoing);
        let generation = maybe_generate(
            &mut generate,
            &prompt,
            prompts::MODULE_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(matches!(
            generation.into_content(),
            GenerationContent::Failed(_)
        ));

        let mut healthy = |_prompt: &str, _system: &str, _tier: PromptTier| {
            Some("`crates/gcode` indexes source and serves search.".to_string())
        };
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut healthy);
        let generation = maybe_generate(
            &mut generate,
            &prompt,
            prompts::MODULE_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(matches!(
            generation.into_content(),
            GenerationContent::Generated(_)
        ));
    }

    #[test]
    fn refusal_body_makes_maybe_generate_fail_and_fall_back() {
        let mut refusing = |_prompt: &str, _system: &str, _tier: PromptTier| {
            Some("I am unable to write this page.".to_string())
        };
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut refusing);
        let generation = maybe_generate(
            &mut generate,
            "Write the repository overview.",
            prompts::REPO_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(matches!(
            generation.into_content(),
            GenerationContent::Failed(_)
        ));
    }

    fn transport_failure() -> AiError {
        AiError::TransportFailure {
            status: None,
            body: None,
            source: "connection reset".to_string(),
            timeout: false,
        }
    }

    #[test]
    fn bounded_retry_recovers_from_transient_transport_failure() {
        let mut calls = 0_usize;
        let result = generate_with_bounded_retry(|| {
            calls += 1;
            if calls == 1 {
                Err(transport_failure())
            } else {
                Ok("generated".to_string())
            }
        });

        assert_eq!(result.expect("retry recovers"), "generated");
        assert_eq!(calls, 2);
    }

    #[test]
    fn bounded_retry_prefers_server_retry_after_hint() {
        let mut calls = 0;
        let mut delays = Vec::new();
        let result = generate_with_bounded_retry_and_sleep(
            || {
                calls += 1;
                if calls == 1 {
                    Err(AiError::rate_limited(Some(Duration::from_millis(17)), None))
                } else {
                    Ok("generated")
                }
            },
            |delay| delays.push(delay),
        )
        .expect("retry succeeds");

        assert_eq!(result, "generated");
        assert_eq!(delays, [Duration::from_millis(17)]);
    }

    #[test]
    fn bounded_retry_gives_up_after_bounded_attempts() {
        let mut calls = 0_usize;
        let result: Result<String, AiError> = generate_with_bounded_retry(|| {
            calls += 1;
            Err(transport_failure())
        });

        assert!(result.is_err());
        assert_eq!(calls, 1 + GENERATION_RETRY_BACKOFF.len());
    }

    #[test]
    fn bounded_retry_fails_fast_on_non_transient_errors() {
        for error in [
            AiError::NotConfigured {
                capability: None,
                message: "no provider".to_string(),
            },
            AiError::Grant {
                source: gobby_core::grant::GrantError::DaemonRequired,
            },
        ] {
            let mut calls = 0_usize;
            let result: Result<String, AiError> = generate_with_bounded_retry(|| {
                calls += 1;
                Err(error.clone())
            });

            assert!(result.is_err(), "{error}");
            assert_eq!(calls, 1, "{error}");
        }
    }
}
