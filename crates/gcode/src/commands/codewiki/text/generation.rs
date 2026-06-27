use std::time::Duration;

use gobby_core::ai::generation::{
    DirectGenerationTarget, GenerationTier, generate_one_shot, profile_for_tier,
    resolve_direct_generation_target,
};
use gobby_core::ai::{
    daemon::generate_via_daemon_with_max_tokens, effective_route, text::generate_text,
};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions, PostgresAiConfigSource};
use gobby_core::ai_types::AiError;
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::codewiki::{
    CodewikiAiOptions, DEFAULT_VERIFY_PROFILE, PromptTier, TextGenerator, TextVerifier, prompts,
};
use crate::config::{self, Context};
use crate::{db, secrets};

/// Backoff between generation attempts; the array length bounds the retries.
pub(super) const GENERATION_RETRY_BACKOFF: [Duration; 2] =
    [Duration::from_millis(200), Duration::from_millis(500)];

pub(crate) fn resolve_text_generator(
    ctx: &Context,
    ai: &CodewikiAiOptions,
) -> Option<Box<TextGenerator<'static>>> {
    let ai_context = resolve_ai_context(ctx, ai.routing).ok()?;
    let route = effective_route(&ai_context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return None;
    }

    // Aggregate prose (curated narrative/concepts, repo/architecture/modules) is
    // the high-value writing surface. Tier -> feature profile is owned by gcore's
    // `profile_for_tier`: Aggregate -> feature_high (or an explicit
    // `--ai-aggregate-profile` override), Module -> feature_mid, Standard ->
    // feature_low. Provider/model resolution stays in config, never pinned here.
    // The Daemon route forwards the resolved profile name; the Direct route
    // resolves each profile to a concrete target so a standalone gcore.yaml
    // routes the tiers to their own provider/model/api_key.
    let aggregate_profile = ai.aggregate_profile.clone();
    let direct_targets = matches!(route, AiRouting::Direct)
        .then(|| resolve_direct_tier_targets(ctx, aggregate_profile.as_deref()));
    let max_tokens = ai.prose_depth.max_tokens();
    let register = ai.register;
    let mut warned = false;
    let quiet = ctx.quiet;
    Some(Box::new(move |prompt, system, tier| {
        let gen_tier = generation_tier(tier);
        let target = direct_targets
            .as_ref()
            .map(|targets| targets.for_tier(gen_tier));
        let system = prompts::with_register(system, register);
        let result = generate_with_bounded_retry(|| {
            generate_one_shot(
                &ai_context,
                route,
                gen_tier,
                aggregate_profile.as_deref(),
                target,
                prompt,
                Some(system.as_ref()),
                max_tokens,
            )
        });
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned {
                    eprintln!(
                        "text generation failed; affected codewiki docs fall back to AST-only \
                         content and record degraded: true: {error}"
                    );
                    warned = true;
                }
                None
            }
        }
    }))
}

/// Maps the codewiki prompt tier onto the shared, provider-neutral generation
/// tier owned by gcore. The three tiers line up one-to-one.
fn generation_tier(tier: PromptTier) -> GenerationTier {
    match tier {
        PromptTier::Aggregate => GenerationTier::Aggregate,
        PromptTier::Module => GenerationTier::Module,
        PromptTier::Standard => GenerationTier::Standard,
    }
}

/// Direct-route generation targets resolved once per tier, so a standalone
/// `gcore.yaml` can route `feature_low/mid/high` to their own
/// provider/model/api_key. Only built when the resolved route is Direct.
struct DirectTierTargets {
    aggregate: DirectGenerationTarget,
    module: DirectGenerationTarget,
    standard: DirectGenerationTarget,
}

impl DirectTierTargets {
    fn for_tier(&self, tier: GenerationTier) -> &DirectGenerationTarget {
        match tier {
            GenerationTier::Aggregate => &self.aggregate,
            GenerationTier::Module => &self.module,
            GenerationTier::Standard => &self.standard,
        }
    }
}

/// Resolve a Direct-route target per tier from the AI config source. A failed
/// config read leaves every field unset; generation then surfaces a clear
/// "profile api_base required" error rather than silently degrading to skeleton.
fn resolve_direct_tier_targets(
    ctx: &Context,
    aggregate_override: Option<&str>,
) -> DirectTierTargets {
    let Ok(mut conn) = db::connect_readonly(&ctx.database_url) else {
        return DirectTierTargets {
            aggregate: DirectGenerationTarget::default(),
            module: DirectGenerationTarget::default(),
            standard: DirectGenerationTarget::default(),
        };
    };
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    DirectTierTargets {
        aggregate: resolve_direct_generation_target(
            &mut source,
            &profile_for_tier(GenerationTier::Aggregate, aggregate_override),
        ),
        module: resolve_direct_generation_target(
            &mut source,
            &profile_for_tier(GenerationTier::Module, None),
        ),
        standard: resolve_direct_generation_target(
            &mut source,
            &profile_for_tier(GenerationTier::Standard, None),
        ),
    }
}

/// Resolve the grounded-verification call that pairs with
/// [`resolve_text_generator`]. Returns `None` when text generation is routed
/// off (no generation, so nothing to verify). Profile/model precedence:
/// explicit [`CodewikiAiOptions`] override -> resolved `ai.text_generate.verify_*`
/// config -> the generate model/api_key and [`DEFAULT_VERIFY_PROFILE`]. Daemon
/// mode selects the verify model server-side via the verify profile; direct mode
/// swaps in `verify_model`/`verify_api_key` while inheriting
/// provider/api_base/transport from the generate binding.
pub(crate) fn resolve_text_verifier(
    ctx: &Context,
    ai: &CodewikiAiOptions,
) -> Option<Box<TextVerifier<'static>>> {
    let mut ai_context = resolve_ai_context(ctx, ai.routing).ok()?;
    let route = effective_route(&ai_context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
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

    if matches!(route, AiRouting::Direct) {
        let text_generate = &mut ai_context.bindings.text_generate;
        if let Some(model) = verify_model {
            text_generate.model = Some(model);
        }
        if let Some(api_key) = verify_api_key {
            text_generate.api_key = Some(api_key);
        }
    }

    let quiet = ctx.quiet;
    let mut warned = false;
    Some(Box::new(move |prompt: &str, system: &str| {
        let result = generate_with_bounded_retry(|| match route {
            AiRouting::Daemon => generate_via_daemon_with_max_tokens(
                &ai_context,
                prompt,
                Some(system),
                None,
                Some(verify_profile.as_str()),
            ),
            AiRouting::Direct => generate_text(&ai_context, prompt, Some(system)),
            AiRouting::Off | AiRouting::Auto => {
                unreachable!("non-generating routes returned above")
            }
        });
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned {
                    eprintln!(
                        "codewiki verification unavailable; generated narratives ship \
                         unverified (degraded: false): {error}"
                    );
                    warned = true;
                }
                None
            }
        }
    }))
}

/// Retries transient generation failures with a short backoff so one dropped
/// connection does not cost a doc its prose for the whole run. Non-transient
/// errors (bad config, parse failures, 4xx) fail immediately.
pub(crate) fn generate_with_bounded_retry<T>(
    mut call: impl FnMut() -> Result<T, AiError>,
) -> Result<T, AiError> {
    let mut result = call();
    for backoff in GENERATION_RETRY_BACKOFF {
        match &result {
            Err(error) if retryable_generation_error(error) => {
                std::thread::sleep(backoff);
                result = call();
            }
            _ => break,
        }
    }
    result
}

fn retryable_generation_error(error: &AiError) -> bool {
    match error {
        AiError::TransportFailure { .. } | AiError::RateLimited { .. } => true,
        AiError::HttpStatus { status, .. } => *status >= 500,
        AiError::CapabilityUnavailable { .. }
        | AiError::NotConfigured { .. }
        | AiError::ParseFailure { .. } => false,
    }
}

fn resolve_ai_context(ctx: &Context, ai: Option<AiRouting>) -> anyhow::Result<AiContext> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    Ok(AiContext::resolve_with_options(
        Some(ctx.project_id.clone()),
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: ai,
        },
    ))
}

/// Outcome of one optional generation attempt. `Failed` means a generator ran
/// and produced no usable text — the doc is degraded relative to what the run
/// intended. `Skipped` means no generator ran (AI off, or gated by depth), so
/// structural output is the intent, not a degradation.
#[derive(Debug)]
pub(crate) enum Generation {
    Generated(String),
    Failed,
    Skipped,
}

impl Generation {
    pub(crate) fn failed(&self) -> bool {
        matches!(self, Generation::Failed)
    }

    /// Returns generated text, or `fallback` while flagging `degraded` when
    /// the generator attempted and failed.
    pub(crate) fn unwrap_or_record(self, fallback: String, degraded: &mut bool) -> String {
        match self {
            Generation::Generated(text) => text,
            Generation::Failed => {
                *degraded = true;
                fallback
            }
            Generation::Skipped => fallback,
        }
    }
}

pub(crate) fn maybe_generate(
    generate: &mut Option<&mut TextGenerator<'_>>,
    prompt: &str,
    system: &str,
    tier: PromptTier,
) -> Generation {
    match generate.as_deref_mut() {
        None => Generation::Skipped,
        Some(generate) => match generate(prompt, system, tier) {
            Some(text) if is_prompt_echo(&text, prompt) || is_model_refusal(&text) => {
                Generation::Failed
            }
            Some(text) => Generation::Generated(text),
            None => Generation::Failed,
        },
    }
}

/// Echo detection floor: prompts shorter than this never trigger rejection,
/// and only this much of the prompt head has to reappear to count as an echo.
const PROMPT_ECHO_PREFIX_CHARS: usize = 80;

/// True when the generated text starts by repeating the prompt itself — a
/// failure mode of overloaded models on huge prompts that previously poisoned
/// pages and recorded summaries as healthy output (#698).
pub(super) fn is_prompt_echo(text: &str, prompt: &str) -> bool {
    let prefix = prompt
        .trim_start()
        .chars()
        .take(PROMPT_ECHO_PREFIX_CHARS)
        .collect::<String>();
    if prefix.chars().count() < PROMPT_ECHO_PREFIX_CHARS {
        return false;
    }
    text.trim_start().starts_with(&prefix)
}

/// Refusal-detection floor: only the opening of the body is scanned, since a
/// model refusal leads with the apology rather than burying it in real prose.
const REFUSAL_SCAN_CHARS: usize = 600;

/// True when the generated body reads as the model *refusing* to write the page
/// ("I cannot write this chapter ...", "I am unable to ...") instead of
/// producing prose. Such a refusal is a generation failure, not content: like
/// an echo it maps to [`Generation::Failed`], so the page falls back to its
/// structural body and is recorded degraded rather than shipping the apology as
/// documentation (#904). Conservative on purpose — first-person can't/unable
/// phrasing scanned only in the lead — so legitimate prose that merely
/// discusses a limitation is not misflagged.
pub(super) fn is_model_refusal(text: &str) -> bool {
    let head_end = text
        .char_indices()
        .nth(REFUSAL_SCAN_CHARS)
        .map_or(text.len(), |(idx, _)| idx);
    let head = text[..head_end].to_ascii_lowercase();
    const REFUSAL_MARKERS: [&str; 8] = [
        "i cannot write",
        "i can't write",
        "i cannot create",
        "i can't create",
        "i cannot generate",
        "i can't generate",
        "i am unable to",
        "i'm unable to",
    ];
    REFUSAL_MARKERS.iter().any(|marker| head.contains(marker))
}

fn clean_generated(text: String) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::ai::generation::{FEATURE_HIGH, FEATURE_LOW, FEATURE_MID};

    #[test]
    fn prompt_tier_maps_to_feature_profiles() {
        assert_eq!(
            generation_tier(PromptTier::Aggregate),
            GenerationTier::Aggregate
        );
        assert_eq!(generation_tier(PromptTier::Module), GenerationTier::Module);
        assert_eq!(
            generation_tier(PromptTier::Standard),
            GenerationTier::Standard
        );

        // Aggregate defaults to the high tier; an explicit override wins.
        assert_eq!(
            profile_for_tier(generation_tier(PromptTier::Aggregate), None),
            FEATURE_HIGH
        );
        assert_eq!(
            profile_for_tier(
                generation_tier(PromptTier::Aggregate),
                Some("custom-writer")
            ),
            "custom-writer"
        );
        assert_eq!(
            profile_for_tier(generation_tier(PromptTier::Module), None),
            FEATURE_MID
        );
        assert_eq!(
            profile_for_tier(generation_tier(PromptTier::Standard), None),
            FEATURE_LOW
        );
    }
}
