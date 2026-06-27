use std::time::Duration;

use gobby_core::ai::{
    daemon::{
        CodeWikiWriterOptions, generate_via_daemon_with_max_tokens, write_codewiki_via_daemon,
    },
    effective_route,
    text::{generate_text, generate_text_with_max_tokens},
};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions, PostgresAiConfigSource};
use gobby_core::ai_types::AiError;
use gobby_core::config::{AiCapability, AiRouting, FeatureCandidate};

use crate::commands::codewiki::{
    CodewikiAiOptions, DEFAULT_VERIFY_PROFILE, PromptTier, TextGenerator, TextVerifier, prompts,
};
use crate::config::{self, Context};
use crate::{db, secrets};

/// Backoff between generation attempts; the array length bounds the retries.
pub(super) const GENERATION_RETRY_BACKOFF: [Duration; 2] =
    [Duration::from_millis(200), Duration::from_millis(500)];

/// Default aggregate-writer candidate chain for the dedicated CodeWiki daemon
/// writer. Keep this Codex-only until another provider has an equivalent
/// noninteractive read-only execution contract.
fn writer_candidate_chain() -> Vec<FeatureCandidate> {
    vec![FeatureCandidate {
        candidate: "codex/gpt-5.5".to_string(),
        reasoning_effort: Some("xhigh".to_string()),
    }]
}

/// Daemon feature profile for [`PromptTier::Module`] writing (#904): module docs
/// and file-body narratives are mid-level per-unit synthesis, written by sonnet
/// — cheaper than the opus-first top-level writer, richer than the per-symbol
/// default tier.
const MODULE_WRITER_PROFILE: &str = "feature_mid";
const CODEWIKI_WRITER_TIMEOUT_SECONDS: f64 = 240.0;

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
    // the high-value writing surface. Route daemon aggregate writing through the
    // dedicated read-only CodeWiki writer endpoint. An explicit
    // `--ai-aggregate-profile` overrides the default Codex candidate chain with a
    // named daemon feature profile. Per-file Standard summaries stay on the
    // binding's lighter default tier.
    let aggregate_profile = ai.aggregate_profile.clone();
    let writer_candidates = writer_candidate_chain();
    let max_tokens = ai.prose_depth.max_tokens();
    let register = ai.register;
    let project_root = ctx.project_root.to_string_lossy().to_string();
    let mut warned = false;
    let quiet = ctx.quiet;
    Some(Box::new(move |prompt, system, tier| {
        // Top-level aggregate writing uses the dedicated writer endpoint; module
        // and file-body synthesis routes to sonnet; per-symbol Standard prose
        // stays light.
        let use_codewiki_writer = matches!(tier, PromptTier::Aggregate);
        let profile = match tier {
            PromptTier::Aggregate => aggregate_profile.as_deref(),
            PromptTier::Module => Some(MODULE_WRITER_PROFILE),
            PromptTier::Standard => None,
        };
        let system = prompts::with_register(system, register);
        let result = generate_with_bounded_retry(|| match route {
            AiRouting::Daemon => {
                if use_codewiki_writer {
                    if !quiet {
                        eprintln!(
                            "codewiki: AI writer active ({})",
                            writer_route_label(profile, &writer_candidates)
                        );
                    }
                    write_codewiki_via_daemon(
                        &ai_context,
                        prompt,
                        Some(system.as_ref()),
                        CodeWikiWriterOptions {
                            cwd: Some(project_root.as_str()),
                            max_tokens,
                            profile,
                            candidates: if profile.is_none() {
                                Some(writer_candidates.as_slice())
                            } else {
                                None
                            },
                            timeout_seconds: Some(CODEWIKI_WRITER_TIMEOUT_SECONDS),
                            reasoning_effort: None,
                            page_kind: Some("aggregate"),
                        },
                    )
                    .map(|result| result.into_text_result())
                } else {
                    generate_via_daemon_with_max_tokens(
                        &ai_context,
                        prompt,
                        Some(system.as_ref()),
                        max_tokens,
                        profile,
                    )
                }
            }
            AiRouting::Direct => generate_text_with_max_tokens(
                &ai_context,
                prompt,
                Some(system.as_ref()),
                max_tokens,
            ),
            AiRouting::Off | AiRouting::Auto => {
                unreachable!("non-generating routes returned above")
            }
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

fn writer_route_label(profile: Option<&str>, candidates: &[FeatureCandidate]) -> String {
    match profile {
        Some(profile) => format!("daemon codewiki writer, profile={profile}"),
        None => {
            let labels = candidates
                .iter()
                .map(|candidate| candidate.candidate.as_str())
                .collect::<Vec<_>>()
                .join(",");
            format!("daemon codewiki writer, candidates={labels}")
        }
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

    #[test]
    fn aggregate_writer_default_candidates_are_codex_only() {
        let candidates = writer_candidate_chain();

        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].candidate, "codex/gpt-5.5");
        assert_eq!(candidates[0].reasoning_effort.as_deref(), Some("xhigh"));
        assert_eq!(
            writer_route_label(None, &candidates),
            "daemon codewiki writer, candidates=codex/gpt-5.5"
        );
    }
}
