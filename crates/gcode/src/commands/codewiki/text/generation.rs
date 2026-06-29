use std::collections::BTreeSet;
use std::time::Duration;

use gobby_core::ai::generation::{
    ChatMessage, ChatTransport, DaemonChatTransport, DirectChatTransport, DirectGenerationTarget,
    GenerationTier, StopReason, ToolLoopLimits, ToolLoopOutcome, generate_one_shot,
    profile_for_tier, resolve_direct_generation_target, run_tool_loop,
};
use gobby_core::ai::{
    AiNoticeKind, daemon::generate_via_daemon_with_max_tokens, effective_route,
    resolve_route_observed, text::generate_text,
};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions, PostgresAiConfigSource};
use gobby_core::ai_types::{AiError, TokenUsage};
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::codewiki::{
    CodewikiAiOptions, CodewikiAiOutcome, CodewikiGraphAvailability, CodewikiToolExecutor,
    DEFAULT_VERIFY_PROFILE, PromptTier, TextGenerator, TextVerifier, prompts,
};
use crate::config::{self, Context};
use crate::{db, secrets};

/// Backoff between generation attempts; the array length bounds the retries.
pub(super) const GENERATION_RETRY_BACKOFF: [Duration; 2] =
    [Duration::from_millis(200), Duration::from_millis(500)];

pub(crate) struct ResolvedTextGenerator {
    pub(crate) generator: Option<Box<TextGenerator<'static>>>,
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
        self.no_generator_reason.or_else(|| {
            (self.ai_fallback && self.ai_route == AiRouting::Direct)
                .then_some(AiNoticeKind::AutoFallbackToDirect)
        })
    }
}

pub(crate) fn resolve_text_generator(
    ctx: &Context,
    ai: &CodewikiAiOptions,
) -> ResolvedTextGenerator {
    let ai_context = match resolve_ai_context(ctx, ai.routing) {
        Ok(ai_context) => ai_context,
        Err(_) => {
            let requested = ai.routing.unwrap_or(AiRouting::Auto);
            let (route, fallback) = match requested {
                AiRouting::Auto => (AiRouting::Off, true),
                route => (route, false),
            };
            return ResolvedTextGenerator::skipped(
                route,
                fallback,
                Some(AiNoticeKind::NoGenerator),
            );
        }
    };
    let observed = resolve_route_observed(&ai_context, AiCapability::TextGenerate);
    let route = observed.route;
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return ResolvedTextGenerator::skipped(route, observed.fallback, observed.reason);
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
    if direct_targets
        .as_ref()
        .is_some_and(|targets| !targets.has_usable_target())
    {
        return ResolvedTextGenerator::skipped(
            route,
            observed.fallback,
            Some(AiNoticeKind::NoGenerator),
        );
    }
    let max_tokens = ai.prose_depth.max_tokens();
    let register = ai.register;
    let mut warned = false;
    let quiet = ctx.quiet;
    let generator: Box<TextGenerator<'static>> = Box::new(move |prompt, system, tier| {
        let gen_tier = generation_tier(tier);
        let target = direct_targets
            .as_ref()
            .map(|targets| targets.for_tier(gen_tier));
        let system = prompts::with_register(system, register);
        let prompt = bound_one_shot_prompt(prompt);
        let result = generate_with_bounded_retry(|| {
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
    });
    ResolvedTextGenerator {
        generator: Some(generator),
        ai_route: route,
        ai_fallback: observed.fallback,
        no_generator_reason: None,
    }
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

    fn has_usable_target(&self) -> bool {
        self.aggregate.api_base().is_some()
            && self.module.api_base().is_some()
            && self.standard.api_base().is_some()
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

/// Why an AI narrative generation attempt failed on an AI-enabled run.
///
/// Distinct from data-source degradation (graph/vector unavailable, see
/// [`GRAPH_UNAVAILABLE`]): a graph backend being down is *evidence* degradation
/// — the page still renders useful narrative — and is never recorded as a
/// `GenerationFailureCause`. Splitting these causes out of the old blanket
/// `model-unavailable` code is the substrate #978 needs to hard-fail a Lane B
/// page with a precise reason instead of silently degrading to a skeleton.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GenerationFailureCause {
    /// The model echoed the prompt back instead of writing fresh prose.
    PromptEcho,
    /// The model refused the request (policy-style "I cannot…" lead).
    Refusal,
    /// The transport/model produced nothing usable: empty output, or the
    /// completion call failed after bounded retry. Genuine unavailability.
    Unavailable,
}

impl GenerationFailureCause {
    /// Every cause, for exhaustive iteration over reason codes.
    pub(crate) const ALL: [Self; 3] = [Self::PromptEcho, Self::Refusal, Self::Unavailable];

    /// Stable degradation reason code recorded into a page's `degraded_sources`,
    /// the rendered degradation notice, and `_meta/codewiki.json`.
    pub(crate) fn reason_code(self) -> &'static str {
        match self {
            Self::PromptEcho => "model-prompt-echo",
            Self::Refusal => "model-refusal",
            Self::Unavailable => "model-unavailable",
        }
    }
}

/// Data-source degradation reason code for an unavailable code-graph backend.
///
/// #979 defines the code shape; #978 wires the concrete graph-tool behavior and
/// its test when the Lane B `ToolExecutor` lands. A graph/vector backend being
/// down is evidence degradation, never an AI-generation failure, so it carries
/// its own code and is excluded from [`is_ai_generation_failure_code`].
pub(crate) const GRAPH_UNAVAILABLE: &str = "graph-unavailable";

/// True when `code` denotes an AI-generation failure (any
/// [`GenerationFailureCause`]), as opposed to a data-source degradation
/// (`graph-unavailable`) or a grounding gap (`grounding-empty`).
///
/// The architecture page derives its `degraded` flag from this: without it,
/// splitting the blanket `model-unavailable` code into distinct causes would
/// silently stop counting refused/echoed pages as degraded.
pub(crate) fn is_ai_generation_failure_code(code: &str) -> bool {
    // `graph-unavailable` is data-source (evidence) degradation, not an AI
    // failure — the page still renders narrative — so it never flips an
    // AI-generation `degraded` flag. #978 records it from the graph tool.
    if code == GRAPH_UNAVAILABLE {
        return false;
    }
    GenerationFailureCause::ALL
        .iter()
        .any(|cause| cause.reason_code() == code)
}

/// Lifecycle status of an attempted (or skipped) generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GenerationStatus {
    /// A generator ran and produced usable narrative.
    Generated,
    /// A generator ran on an AI-enabled run and failed (see the cause).
    Failed,
    /// No generator ran — AI off, or gated by depth. Deterministic structural
    /// output is the intent, not a degradation.
    Skipped,
}

/// Per-attempt observability, uniform across the Lane A one-shot and (in #978)
/// the Lane B tool loop. Lane A reports a single completed turn with no tool
/// calls; #978 maps a `ToolLoopOutcome` (turns, tool calls, stop reason, summed
/// usage) in directly.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct GenerationObservability {
    pub(crate) stop_reason: Option<StopReason>,
    pub(crate) tool_call_count: usize,
    pub(crate) turns: usize,
    pub(crate) usage: Option<TokenUsage>,
}

/// Owned, matchable view of a [`GenerationOutcome`]: the structured replacement
/// for the old `Generation` enum, carrying a distinct cause on `Failed`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum GenerationContent {
    Generated(String),
    Failed(GenerationFailureCause),
    Skipped,
}

/// Structured outcome of a CodeWiki narrative generation attempt. Replaces the
/// lossy `Option<String>`/`Generation` boundary: a failed AI attempt carries a
/// distinct [`GenerationFailureCause`] instead of collapsing every failure to
/// `model-unavailable`, and every attempt carries its [`GenerationObservability`]
/// for `_meta` recording (#978).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct GenerationOutcome {
    status: GenerationStatus,
    content: Option<String>,
    cause: Option<GenerationFailureCause>,
    observability: GenerationObservability,
}

impl GenerationOutcome {
    /// A completed Lane A one-shot: one turn, no tool calls.
    pub(crate) fn generated(content: String) -> Self {
        Self {
            status: GenerationStatus::Generated,
            content: Some(content),
            cause: None,
            observability: GenerationObservability {
                stop_reason: Some(StopReason::Completed),
                tool_call_count: 0,
                turns: 1,
                usage: None,
            },
        }
    }

    /// A Lane A attempt whose completion returned content that failed
    /// classification (an echo or a refusal): one completed turn, no usable
    /// output.
    pub(crate) fn rejected(cause: GenerationFailureCause) -> Self {
        Self {
            status: GenerationStatus::Failed,
            content: None,
            cause: Some(cause),
            observability: GenerationObservability {
                stop_reason: Some(StopReason::Completed),
                tool_call_count: 0,
                turns: 1,
                usage: None,
            },
        }
    }

    /// The transport/model produced nothing usable (empty output or a transport
    /// error after bounded retry).
    pub(crate) fn unavailable() -> Self {
        Self {
            status: GenerationStatus::Failed,
            content: None,
            cause: Some(GenerationFailureCause::Unavailable),
            observability: GenerationObservability::default(),
        }
    }

    /// No generator ran (AI off or depth-gated).
    pub(crate) fn skipped() -> Self {
        Self {
            status: GenerationStatus::Skipped,
            content: None,
            cause: None,
            observability: GenerationObservability::default(),
        }
    }

    /// A completed Lane B tool-loop generation, carrying the loop's observability
    /// (real turn / tool-call counts and summed usage) rather than the Lane A
    /// single-turn defaults.
    fn generated_with_observability(
        content: String,
        observability: GenerationObservability,
    ) -> Self {
        Self {
            status: GenerationStatus::Generated,
            content: Some(content),
            cause: None,
            observability,
        }
    }

    /// A Lane B attempt that ran the loop but produced no usable answer (bad
    /// stop reason, no content, an echo/refusal, or empty output). Carries the
    /// loop observability so the run can report what the loop did before
    /// failing.
    fn rejected_with_observability(
        cause: GenerationFailureCause,
        observability: GenerationObservability,
    ) -> Self {
        Self {
            status: GenerationStatus::Failed,
            content: None,
            cause: Some(cause),
            observability,
        }
    }

    /// Map a gcore [`ToolLoopOutcome`] into a structured `GenerationOutcome`,
    /// classifying the Lane B result with the same vocabulary as the Lane A
    /// one-shot: a non-`Completed` stop reason or missing/echoed/refused/empty
    /// content is a failure with a distinct cause; usable content is
    /// `Generated`. The loop's turn / tool-call counts and summed usage are
    /// preserved on every branch for `_meta` and frontmatter recording.
    pub(crate) fn from_tool_loop(outcome: ToolLoopOutcome, prompt: &str) -> Self {
        let observability = GenerationObservability {
            stop_reason: Some(outcome.stop_reason),
            tool_call_count: outcome.observability.tool_call_count,
            turns: outcome.observability.turns,
            usage: outcome.total_usage,
        };
        if !outcome.stop_reason.is_completed() {
            return Self::rejected_with_observability(
                GenerationFailureCause::Unavailable,
                observability,
            );
        }
        match outcome.content {
            None => Self::rejected_with_observability(
                GenerationFailureCause::Unavailable,
                observability,
            ),
            Some(text) if is_prompt_echo(&text, prompt) => {
                Self::rejected_with_observability(GenerationFailureCause::PromptEcho, observability)
            }
            Some(text) if is_model_refusal(&text) => {
                Self::rejected_with_observability(GenerationFailureCause::Refusal, observability)
            }
            Some(text) => match clean_generated(text) {
                Some(clean) => Self::generated_with_observability(clean, observability),
                None => Self::rejected_with_observability(
                    GenerationFailureCause::Unavailable,
                    observability,
                ),
            },
        }
    }

    /// The per-attempt observability (stop reason, tool-call count, turns,
    /// usage), read by the aggregate builders to record `lane: tool_loop` +
    /// counts into page frontmatter and `_meta/codewiki.json` (#978).
    pub(crate) fn observability(&self) -> &GenerationObservability {
        &self.observability
    }

    /// The distinct failure cause, when the attempt failed.
    pub(crate) fn failure_cause(&self) -> Option<GenerationFailureCause> {
        self.cause
    }

    /// Consume into the matchable content view, dropping observability.
    pub(crate) fn into_content(self) -> GenerationContent {
        match self.status {
            GenerationStatus::Generated => {
                GenerationContent::Generated(self.content.unwrap_or_default())
            }
            GenerationStatus::Failed => {
                GenerationContent::Failed(self.cause.unwrap_or(GenerationFailureCause::Unavailable))
            }
            GenerationStatus::Skipped => GenerationContent::Skipped,
        }
    }

    /// Generated text, or `fallback` while recording the distinct failure
    /// reason code into `degraded_sources` when the generator attempted and
    /// failed. A skipped attempt records nothing — structural output is the
    /// intent, not a degradation.
    pub(crate) fn unwrap_or_record(
        self,
        fallback: String,
        degraded_sources: &mut BTreeSet<String>,
    ) -> String {
        match self.into_content() {
            GenerationContent::Generated(text) => text,
            GenerationContent::Failed(cause) => {
                degraded_sources.insert(cause.reason_code().to_string());
                fallback
            }
            GenerationContent::Skipped => fallback,
        }
    }
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

/// Frontmatter/`_meta` lane label for a page produced by the Lane B tool loop.
pub(crate) const LANE_TOOL_LOOP: &str = "tool_loop";
/// Lane label for a page produced by a Lane A one-shot generation.
pub(crate) const LANE_ONE_SHOT: &str = "one_shot";

/// Result of one Lane B tool-loop generation: the classified outcome plus any
/// data-source (evidence) degradation the executor hit during the loop (e.g.
/// `graph-unavailable`). The degradation is surfaced on the generated page but
/// never hard-fails it — services being down is evidence degradation, not a
/// generation failure.
pub(crate) struct LaneBResult {
    pub(crate) outcome: GenerationOutcome,
    pub(crate) data_source_degraded: Vec<String>,
}

impl LaneBResult {
    /// The transport/loop could not run (transport build error or loop error).
    fn unavailable() -> Self {
        Self {
            outcome: GenerationOutcome::unavailable(),
            data_source_degraded: Vec::new(),
        }
    }

    /// No tool-chat route resolved (off/auto) — defensive; the resolver returns
    /// `None` in that case so this is reached only on a redundant route check.
    fn skipped() -> Self {
        Self {
            outcome: GenerationOutcome::skipped(),
            data_source_degraded: Vec::new(),
        }
    }
}

/// A resolved Lane B generator: a closure that, given `(prompt, system)`, runs
/// the gcode tool loop over the index and returns a [`LaneBResult`]. Mirrors
/// [`TextGenerator`] for Lane A; aggregate-only (leaf pages stay Lane A).
pub(crate) type ToolLoopGenerator<'a> = dyn FnMut(&str, &str) -> LaneBResult + 'a;

/// Run one Lane B tool loop against the gcode index for the given transport,
/// returning the classified outcome plus the executor's data-source
/// degradation. Builds a fresh read-only executor per call.
/// Maximum byte size of the Lane B seed (user) prompt. The tool loop lets the
/// model fetch its own grounding, so the seed only needs to orient it; bounding
/// it keeps the request inside a tool-capable model's effective context (#993).
const LANE_B_SEED_MAX_BYTES: usize = 16 * 1024;

/// Upper bound on a Lane A one-shot prompt. A large module/aggregate prompt
/// (many child summaries + source excerpts) combined with an unbounded output
/// budget can overrun the model's loaded context window and 400 the request
/// ("tokens to keep from the initial prompt is greater than the context
/// length"). The one-shot path has no tools to recover elided detail, so the cap
/// is far more generous than the Lane B seed; it only trims pathological inputs.
const LANE_A_PROMPT_MAX_BYTES: usize = 32 * 1024;

/// Bound a Lane A one-shot prompt to [`LANE_A_PROMPT_MAX_BYTES`], truncating on a
/// char boundary, so an oversized module/aggregate prompt cannot 400 the request
/// and degrade the page (#993). No tool note: Lane A has no investigation tools.
fn bound_one_shot_prompt(prompt: &str) -> String {
    if prompt.len() <= LANE_A_PROMPT_MAX_BYTES {
        return prompt.to_string();
    }
    let mut end = LANE_A_PROMPT_MAX_BYTES;
    while end > 0 && !prompt.is_char_boundary(end) {
        end -= 1;
    }
    format!(
        "{}\n\n[Input truncated to fit the model context.]",
        &prompt[..end]
    )
}

/// Bound the Lane B seed prompt to [`LANE_B_SEED_MAX_BYTES`], truncating on a
/// char boundary and appending a tool-investigation note so the model fetches
/// any details beyond the seed via its tools.
fn bound_seed_prompt(prompt: &str) -> String {
    if prompt.len() <= LANE_B_SEED_MAX_BYTES {
        return prompt.to_string();
    }
    let mut end = LANE_B_SEED_MAX_BYTES;
    while end > 0 && !prompt.is_char_boundary(end) {
        end -= 1;
    }
    format!(
        "{}\n\n[Seed truncated to fit context. Use the provided tools (search_code, \
         outline_file, read_symbol, read_file, grep_repo, find_callers, find_usages, \
         imports) to investigate the codebase for any details beyond this excerpt.]",
        &prompt[..end]
    )
}

/// Lane B operating directive appended to every aggregate page's system prompt.
/// The shared page prompts tell the model to write from "only the supplied
/// input" — correct for the Lane A one-shot, but in Lane B the seed is a
/// bounded orientation and the real grounding comes from the tools. Without
/// this, a weak function-calling model both skips investigation and declines to
/// cite anything it fetched, producing an ungroundable body that hard-fails the
/// no-skeleton contract (#993/#978).
const LANE_B_SYSTEM_DIRECTIVE: &str = "Investigation mode: you have tools \
(search_code, outline_file, read_symbol, read_file, grep_repo, find_callers, \
find_usages, imports). The user message is a bounded seed for orientation, not \
the full evidence. Before answering, call these tools to read the actual source \
so your output is grounded in the real code; treat tool results as authoritative \
repository source, cite the file:line anchors they reveal alongside any in the \
seed, and never invent files, symbols, or line numbers. If the task instructions \
require specific section headings, your final answer must include every one of \
them verbatim and in the given order; when a section's evidence is thin, state \
that briefly under its heading rather than dropping the heading. When you have \
gathered enough, write the complete response in a single final message and \
return only that answer in exactly the format the task instructions require — no \
tool-call narration, no preamble, and no extra commentary or code fences \
wrapping a required JSON object.";

/// Compose the Lane B system prompt: the page-specific system prompt followed by
/// the [`LANE_B_SYSTEM_DIRECTIVE`] so the model investigates via tools before it
/// answers.
fn lane_b_system_prompt(page_system: &str) -> String {
    format!("{page_system}\n\n{LANE_B_SYSTEM_DIRECTIVE}")
}

fn run_lane_b_loop(
    transport: &dyn ChatTransport,
    ctx: &Context,
    graph_availability: CodewikiGraphAvailability,
    system: &str,
    prompt: &str,
    max_tokens: Option<usize>,
) -> LaneBResult {
    let mut executor = match CodewikiToolExecutor::new(ctx, graph_availability) {
        Ok(executor) => executor,
        Err(_) => return LaneBResult::unavailable(),
    };
    // Lane B's premise is that the model gathers its own grounding via tools, so
    // the seed only orients it. An unbounded pre-assembled aggregate seed (full
    // source excerpts + repo-wide summary dumps — ~95KB observed) overruns a
    // tool-capable model's effective context and 400s the request (#993); bound
    // it and let the model fetch the rest via tools.
    let prompt = bound_seed_prompt(prompt);
    let messages = vec![
        ChatMessage::system(lane_b_system_prompt(system)),
        ChatMessage::user(prompt.clone()),
    ];
    // A local standalone model (e.g. gemma) is slower per turn than a frontier
    // model and accumulates context across investigation turns. Keep each tool
    // result lean so the running prompt does not balloon back toward the
    // context limit that 400s the request (#993), give the model room to both
    // investigate and write, and grant a generous wall-clock budget so genuine
    // multi-turn investigation completes instead of being cut short.
    let limits = ToolLoopLimits {
        max_turns: 12,
        max_tool_calls: 24,
        max_bytes_per_tool_result: 8 * 1024,
        timeout: std::time::Duration::from_secs(600),
    };
    let outcome = match run_tool_loop(transport, &mut executor, messages, &limits, max_tokens) {
        Ok(outcome) => GenerationOutcome::from_tool_loop(outcome, &prompt),
        Err(_) => GenerationOutcome::unavailable(),
    };
    LaneBResult {
        outcome,
        data_source_degraded: executor.into_data_source_degraded(),
    }
}

/// Resolve the Direct-route aggregate (`feature_high`) generation target from
/// the AI config source, so a standalone `gcore.yaml` routes Lane B to its own
/// provider/model/api_key. A failed config read leaves the target unset; the
/// resolver then declines to produce a generator rather than silently
/// degrading.
fn resolve_aggregate_direct_target(
    ctx: &Context,
    aggregate_override: Option<&str>,
) -> DirectGenerationTarget {
    let Ok(mut conn) = db::connect_readonly(&ctx.database_url) else {
        return DirectGenerationTarget::default();
    };
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    resolve_direct_generation_target(
        &mut source,
        &profile_for_tier(GenerationTier::Aggregate, aggregate_override),
    )
}

/// Resolve the Lane B aggregate generator for the `tool_chat` capability,
/// mirroring [`resolve_text_generator`] but for the tool-calling chat route.
/// Returns `None` when the route is off/auto or the Direct route has no usable
/// aggregate target; in those cases aggregate pages fall back to the Lane A
/// path (or structural output when AI is off). Tool-chat reuses the
/// `text_generate` config tree (Aggregate → `feature_high`), with daemon-side
/// tool-capability filtering layered on; there is no parallel config tree.
pub(crate) fn resolve_tool_loop_generator(
    ctx: &Context,
    ai: &CodewikiAiOptions,
    graph_availability: CodewikiGraphAvailability,
) -> Option<Box<ToolLoopGenerator<'static>>> {
    let ai_context = resolve_ai_context(ctx, ai.routing).ok()?;
    let observed = resolve_route_observed(&ai_context, AiCapability::ToolChat);
    let route = observed.route;
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return None;
    }
    let aggregate_profile = ai.aggregate_profile.clone();
    let profile = profile_for_tier(GenerationTier::Aggregate, aggregate_profile.as_deref());
    let direct_target = if matches!(route, AiRouting::Direct) {
        let target = resolve_aggregate_direct_target(ctx, aggregate_profile.as_deref());
        // No resolved api_base means a Direct-route Lane B cannot run; decline
        // rather than build a generator that always errors.
        target.api_base()?;
        Some(target)
    } else {
        None
    };
    let max_tokens = ai.prose_depth.max_tokens();
    let register = ai.register;
    let ctx_owned = ctx.clone();
    let generator: Box<ToolLoopGenerator<'static>> = Box::new(move |prompt, system| {
        let system = prompts::with_register(system, register);
        match route {
            AiRouting::Daemon => match DaemonChatTransport::new(&ai_context, profile.clone()) {
                Ok(transport) => run_lane_b_loop(
                    &transport,
                    &ctx_owned,
                    graph_availability,
                    system.as_ref(),
                    prompt,
                    max_tokens,
                ),
                Err(_) => LaneBResult::unavailable(),
            },
            AiRouting::Direct => match direct_target.clone() {
                Some(target) => {
                    match DirectChatTransport::new(&ai_context, target, Some(profile.clone())) {
                        Ok(transport) => run_lane_b_loop(
                            &transport,
                            &ctx_owned,
                            graph_availability,
                            system.as_ref(),
                            prompt,
                            max_tokens,
                        ),
                        Err(_) => LaneBResult::unavailable(),
                    }
                }
                None => LaneBResult::unavailable(),
            },
            AiRouting::Off | AiRouting::Auto => LaneBResult::skipped(),
        }
    });
    Some(generator)
}

/// Run the Lane B generator if present, returning a [`LaneBResult`]. A `None`
/// generator yields a skipped outcome, exactly like [`maybe_generate`] for
/// Lane A — the aggregate site then falls back to Lane A or structural output.
pub(crate) fn maybe_generate_tool_loop(
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    prompt: &str,
    system: &str,
) -> LaneBResult {
    match tool_loop.as_deref_mut() {
        None => LaneBResult {
            outcome: GenerationOutcome::skipped(),
            data_source_degraded: Vec::new(),
        },
        Some(generate) => generate(prompt, system),
    }
}

/// The classified content of an aggregate-page generation plus its lane,
/// observability, and data-source degradation, uniform across Lane B and the
/// Lane A fallback. A Lane B *generation failure* never reaches here — it is
/// returned as an `Err` by [`generate_aggregate`] so the page hard-fails (no
/// skeleton); only Lane A failures arrive as [`GenerationContent::Failed`].
#[derive(Debug)]
pub(crate) struct AggregateGeneration {
    pub(crate) content: GenerationContent,
    pub(crate) observability: GenerationObservability,
    pub(crate) data_source_degraded: Vec<String>,
    pub(crate) lane: &'static str,
}

/// Generate an aggregate page: Lane B (tool loop) when a `tool_loop` generator
/// is present, else the Lane A one-shot. A Lane B generation failure (bad stop
/// reason, no/echoed/refused/empty content) is a hard `Err` with a distinct
/// reason code — the page is not written and there is no skeleton or silent
/// Lane A fallback (#978). A `graph-unavailable` evidence degradation hit during
/// the loop is carried on the `Ok` result, not a failure. Lane A failures stay
/// `Ok(Failed(cause))` so the existing leaf-style structural fallback applies.
pub(crate) fn generate_aggregate(
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    prompt: &str,
    system: &str,
    label: &str,
) -> anyhow::Result<AggregateGeneration> {
    if tool_loop.is_some() {
        let result = maybe_generate_tool_loop(tool_loop, prompt, system);
        let observability = result.outcome.observability().clone();
        let data_source_degraded = result.data_source_degraded;
        // A Lane B generation failure hard-fails the page with its distinct
        // reason code — no skeleton, no silent Lane A fallback (#978).
        if let Some(cause) = result.outcome.failure_cause() {
            return Err(anyhow::anyhow!(
                "Lane B {label} generation failed ({}, stop={:?}, turns={}, tool_calls={}); \
                 page not written (no skeleton, no Lane A fallback)",
                cause.reason_code(),
                observability.stop_reason,
                observability.turns,
                observability.tool_call_count,
            ));
        }
        Ok(AggregateGeneration {
            content: result.outcome.into_content(),
            observability,
            data_source_degraded,
            lane: LANE_TOOL_LOOP,
        })
    } else {
        let outcome = maybe_generate(generate, prompt, system, PromptTier::Aggregate);
        let observability = outcome.observability().clone();
        Ok(AggregateGeneration {
            content: outcome.into_content(),
            observability,
            data_source_degraded: Vec::new(),
            lane: LANE_ONE_SHOT,
        })
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
/// an echo it maps to a [`GenerationFailureCause`], so the page falls back to
/// its structural body and is recorded degraded rather than shipping the apology
/// as documentation (#904). Conservative on purpose — first-person can't/unable
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
    use gobby_core::ai::generation::{
        FEATURE_HIGH, FEATURE_LOW, FEATURE_MID, ToolLoopObservability,
    };

    #[test]
    fn bound_seed_prompt_passes_small_prompts_through() {
        let prompt = "short seed";
        assert_eq!(bound_seed_prompt(prompt), prompt);
    }

    #[test]
    fn lane_b_system_prompt_appends_investigation_directive() {
        let composed = lane_b_system_prompt(prompts::CONCEPT_PAGE_SYSTEM);
        // Page contract stays first and intact; the Lane B directive follows.
        assert!(composed.starts_with(prompts::CONCEPT_PAGE_SYSTEM));
        assert!(composed.contains("Investigation mode"));
        assert!(composed.contains("search_code"));
        assert!(composed.contains("authoritative repository source"));
        // Format-agnostic: must not impose a prose-page shape, so it can also
        // front the JSON curated-navigation pass without corrupting its output.
        assert!(composed.contains("in exactly the format the task"));
        assert!(!composed.contains("write the page"));
        // Completeness clause: every required section heading must be emitted
        // verbatim (state absence rather than drop a heading), so a thin-evidence
        // section can't silently disappear and fail the structural check (#993).
        assert!(composed.contains("verbatim and in the given order"));
    }

    #[test]
    fn bound_one_shot_prompt_passes_small_prompts_through() {
        let prompt = "small module prompt";
        assert_eq!(bound_one_shot_prompt(prompt), prompt);
    }

    #[test]
    fn bound_one_shot_prompt_truncates_oversized_prompt_on_a_char_boundary() {
        let prompt = "é".repeat(LANE_A_PROMPT_MAX_BYTES); // 2 bytes each → over the cap
        let bounded = bound_one_shot_prompt(&prompt);
        assert!(std::str::from_utf8(bounded.as_bytes()).is_ok());
        assert!(bounded.contains("[Input truncated to fit the model context.]"));
        assert!(bounded.len() <= LANE_A_PROMPT_MAX_BYTES + 64);
    }

    #[test]
    fn bound_seed_prompt_truncates_oversized_seed_on_a_char_boundary_with_a_tool_note() {
        // A multi-byte char straddling the cap must not be split mid-codepoint.
        let prompt = "é".repeat(LANE_B_SEED_MAX_BYTES); // 2 bytes each → well over the cap
        let bounded = bound_seed_prompt(&prompt);
        assert!(bounded.is_char_boundary(0) && std::str::from_utf8(bounded.as_bytes()).is_ok());
        assert!(bounded.contains("[Seed truncated to fit context."));
        assert!(bounded.contains("search_code"));
        // The retained prefix is bounded; the note adds a small fixed suffix.
        assert!(bounded.len() < prompt.len());
        assert!(bounded.len() <= LANE_B_SEED_MAX_BYTES + 512);
    }

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

    #[test]
    fn direct_tier_targets_require_api_base_for_every_tier() {
        fn target(api_base: Option<&str>) -> DirectGenerationTarget {
            DirectGenerationTarget {
                api_base: api_base.map(str::to_string),
                ..DirectGenerationTarget::default()
            }
        }

        assert!(
            DirectTierTargets {
                aggregate: target(Some("http://aggregate.test/v1")),
                module: target(Some("http://module.test/v1")),
                standard: target(Some("http://standard.test/v1")),
            }
            .has_usable_target()
        );

        for targets in [
            DirectTierTargets {
                aggregate: target(None),
                module: target(Some("http://module.test/v1")),
                standard: target(Some("http://standard.test/v1")),
            },
            DirectTierTargets {
                aggregate: target(Some("http://aggregate.test/v1")),
                module: target(None),
                standard: target(Some("http://standard.test/v1")),
            },
            DirectTierTargets {
                aggregate: target(Some("http://aggregate.test/v1")),
                module: target(Some("http://module.test/v1")),
                standard: target(None),
            },
        ] {
            assert!(!targets.has_usable_target());
        }
    }

    #[test]
    fn resolved_text_generator_classifies_notice_kinds() {
        let no_generator = ResolvedTextGenerator::skipped(
            AiRouting::Direct,
            false,
            Some(AiNoticeKind::NoGenerator),
        );

        assert_eq!(no_generator.notice_kind(), Some(AiNoticeKind::NoGenerator));
        assert_eq!(no_generator.ai_outcome().route, AiRouting::Direct);
        assert_eq!(
            no_generator.ai_outcome().status,
            crate::commands::codewiki::AiGenerationStatus::Skipped
        );

        let fallback_direct = ResolvedTextGenerator {
            generator: Some(Box::new(|_, _, _| Some("generated".to_string()))),
            ai_route: AiRouting::Direct,
            ai_fallback: true,
            no_generator_reason: None,
        };

        assert_eq!(
            fallback_direct.notice_kind(),
            Some(AiNoticeKind::AutoFallbackToDirect)
        );
        assert_eq!(
            fallback_direct.ai_outcome().status,
            crate::commands::codewiki::AiGenerationStatus::Generated
        );
    }

    #[test]
    fn generation_failure_cause_reason_codes_are_distinct_and_stable() {
        assert_eq!(
            GenerationFailureCause::PromptEcho.reason_code(),
            "model-prompt-echo"
        );
        assert_eq!(
            GenerationFailureCause::Refusal.reason_code(),
            "model-refusal"
        );
        assert_eq!(
            GenerationFailureCause::Unavailable.reason_code(),
            "model-unavailable"
        );
        let codes: std::collections::BTreeSet<_> = GenerationFailureCause::ALL
            .iter()
            .map(|cause| cause.reason_code())
            .collect();
        assert_eq!(
            codes.len(),
            GenerationFailureCause::ALL.len(),
            "every AI failure cause must map to a distinct reason code"
        );
    }

    #[test]
    fn is_ai_generation_failure_code_excludes_data_source_codes() {
        for cause in GenerationFailureCause::ALL {
            assert!(is_ai_generation_failure_code(cause.reason_code()));
        }
        // Data-source / grounding degradation is evidence loss, not an AI
        // failure, so it must never trip the AI-failure classification.
        assert!(!is_ai_generation_failure_code(GRAPH_UNAVAILABLE));
        assert!(!is_ai_generation_failure_code("grounding-empty"));
        assert!(!is_ai_generation_failure_code("not-a-code"));
    }

    #[test]
    fn maybe_generate_classifies_distinct_failure_causes() {
        let long_prompt = "Document the module in full, covering responsibilities, \
                           collaborators, data flow, and failure modes with grounded citations.";
        assert!(long_prompt.chars().count() >= PROMPT_ECHO_PREFIX_CHARS);

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

        // No generator is `--ai off`, not a failure: structural output is the
        // intent, so no degradation cause is recorded.
        let mut generate = None::<&mut TextGenerator<'_>>;
        let outcome = maybe_generate(&mut generate, long_prompt, "system", PromptTier::Aggregate);
        assert_eq!(outcome.failure_cause(), None);
        assert!(matches!(outcome.into_content(), GenerationContent::Skipped));
    }

    #[test]
    fn lane_a_generation_outcome_carries_one_shot_observability() {
        let mut healthy = |_: &str, _: &str, _: PromptTier| Some("Grounded narrative.".to_string());
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut healthy);
        let outcome = maybe_generate(&mut generate, "prompt", "system", PromptTier::Aggregate);
        assert_eq!(outcome.status, GenerationStatus::Generated);
        assert_eq!(
            outcome.observability.stop_reason,
            Some(StopReason::Completed)
        );
        assert_eq!(outcome.observability.tool_call_count, 0);
        assert_eq!(outcome.observability.turns, 1);
        assert_eq!(outcome.observability.usage, None);
        assert!(matches!(
            outcome.into_content(),
            GenerationContent::Generated(text) if text == "Grounded narrative."
        ));

        // A transport failure has no completed turn to report.
        let mut failing = |_: &str, _: &str, _: PromptTier| None;
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut failing);
        let outcome = maybe_generate(&mut generate, "prompt", "system", PromptTier::Aggregate);
        assert_eq!(outcome.observability.stop_reason, None);
        assert_eq!(outcome.observability.turns, 0);
    }

    #[test]
    fn unwrap_or_record_writes_distinct_codes_only_on_failure() {
        let mut codes = std::collections::BTreeSet::new();
        let text = GenerationOutcome::rejected(GenerationFailureCause::Refusal)
            .unwrap_or_record("fallback".to_string(), &mut codes);
        assert_eq!(text, "fallback");
        assert!(codes.contains("model-refusal"));
        assert!(!codes.contains("model-unavailable"));

        let mut codes = std::collections::BTreeSet::new();
        let text =
            GenerationOutcome::skipped().unwrap_or_record("fallback".to_string(), &mut codes);
        assert_eq!(text, "fallback");
        assert!(codes.is_empty(), "a skip is not a degradation");

        let mut codes = std::collections::BTreeSet::new();
        let text = GenerationOutcome::generated("body".to_string())
            .unwrap_or_record("fallback".to_string(), &mut codes);
        assert_eq!(text, "body");
        assert!(codes.is_empty());
    }

    /// Build a stub `ToolLoopOutcome` for Lane B classification tests, without a
    /// live transport or executor.
    fn tool_loop_outcome(
        content: Option<&str>,
        stop_reason: StopReason,
        tool_call_count: usize,
        turns: usize,
    ) -> ToolLoopOutcome {
        ToolLoopOutcome {
            content: content.map(str::to_string),
            stop_reason,
            observability: ToolLoopObservability {
                lane: "tool_loop",
                route: "stub",
                profile: None,
                provider: None,
                model: None,
                tool_names: Vec::new(),
                tool_call_count,
                turns,
                elapsed_ms: 0,
                termination_reason: stop_reason.as_str(),
            },
            total_usage: None,
        }
    }

    #[test]
    fn from_tool_loop_maps_completed_content_to_generated_with_observability() {
        let outcome = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(
                Some("A grounded narrative body."),
                StopReason::Completed,
                5,
                3,
            ),
            "investigate the repo",
        );
        assert_eq!(outcome.observability().tool_call_count, 5);
        assert_eq!(outcome.observability().turns, 3);
        assert_eq!(
            outcome.observability().stop_reason,
            Some(StopReason::Completed)
        );
        assert!(matches!(
            outcome.into_content(),
            GenerationContent::Generated(text) if text == "A grounded narrative body."
        ));
    }

    #[test]
    fn from_tool_loop_hard_fails_on_bad_stop_reason_or_missing_content() {
        // A non-Completed stop reason is a Lane B failure, with the loop's
        // observability preserved.
        let max_turns = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(None, StopReason::MaxTurns, 24, 8),
            "p",
        );
        assert_eq!(
            max_turns.failure_cause(),
            Some(GenerationFailureCause::Unavailable)
        );
        assert_eq!(max_turns.observability().turns, 8);
        // Completed but no content is also a failure.
        let empty = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(None, StopReason::Completed, 1, 1),
            "p",
        );
        assert_eq!(
            empty.failure_cause(),
            Some(GenerationFailureCause::Unavailable)
        );
        let timeout = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(Some("x"), StopReason::Timeout, 2, 2),
            "p",
        );
        assert_eq!(
            timeout.failure_cause(),
            Some(GenerationFailureCause::Unavailable)
        );
    }

    #[test]
    fn from_tool_loop_classifies_echo_and_refusal() {
        // The prompt must exceed the echo-detection floor (80 chars) for an echo
        // to be classified as such rather than treated as fresh prose.
        let prompt = "Summarize the architecture of this codebase in thorough detail for the \
                      reader, covering every subsystem boundary and data flow.";
        let echo = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(Some(prompt), StopReason::Completed, 1, 1),
            prompt,
        );
        assert_eq!(
            echo.failure_cause(),
            Some(GenerationFailureCause::PromptEcho)
        );
        let refusal = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(
                Some("I cannot write this documentation for the codebase."),
                StopReason::Completed,
                1,
                1,
            ),
            prompt,
        );
        assert_eq!(
            refusal.failure_cause(),
            Some(GenerationFailureCause::Refusal)
        );
    }

    #[test]
    fn generate_aggregate_lane_b_success_records_lane_and_observability() {
        let mut tool_loop = |_prompt: &str, _system: &str| LaneBResult {
            outcome: GenerationOutcome::from_tool_loop(
                tool_loop_outcome(
                    Some("Grounded aggregate prose."),
                    StopReason::Completed,
                    4,
                    2,
                ),
                "p",
            ),
            data_source_degraded: Vec::new(),
        };
        let mut tool_loop: Option<&mut ToolLoopGenerator<'_>> = Some(&mut tool_loop);
        let aggregate = generate_aggregate(
            &mut tool_loop,
            &mut None,
            "prompt",
            "system",
            "repo overview",
        )
        .expect("lane B success is not a hard fail");
        assert_eq!(aggregate.lane, LANE_TOOL_LOOP);
        assert_eq!(aggregate.observability.tool_call_count, 4);
        assert_eq!(aggregate.observability.turns, 2);
        assert!(aggregate.data_source_degraded.is_empty());
        assert!(matches!(
            aggregate.content,
            GenerationContent::Generated(text) if text == "Grounded aggregate prose."
        ));
    }

    #[test]
    fn generate_aggregate_lane_b_failure_hard_fails_with_reason_code() {
        let mut tool_loop = |_prompt: &str, _system: &str| LaneBResult {
            outcome: GenerationOutcome::from_tool_loop(
                tool_loop_outcome(None, StopReason::MaxToolCalls, 24, 8),
                "p",
            ),
            data_source_degraded: Vec::new(),
        };
        let mut tool_loop: Option<&mut ToolLoopGenerator<'_>> = Some(&mut tool_loop);
        let error = generate_aggregate(
            &mut tool_loop,
            &mut None,
            "prompt",
            "system",
            "architecture",
        )
        .expect_err("a Lane B failure must hard-fail the page, not degrade");
        let message = error.to_string();
        assert!(message.contains("Lane B architecture"), "{message}");
        assert!(message.contains("model-unavailable"), "{message}");
        assert!(message.contains("no skeleton"), "{message}");
        // The hard-fail surfaces the loop's termination reason so a stalled or
        // budget-exhausted investigation is diagnosable (not just "unavailable").
        assert!(message.contains("MaxToolCalls"), "{message}");
        assert!(message.contains("turns=8"), "{message}");
        assert!(message.contains("tool_calls=24"), "{message}");
    }

    #[test]
    fn generate_aggregate_lane_b_carries_graph_unavailable_evidence_degradation() {
        // graph-unavailable during the loop is evidence degradation: the page
        // still generates and the code is carried, never a hard fail.
        let mut tool_loop = |_prompt: &str, _system: &str| LaneBResult {
            outcome: GenerationOutcome::from_tool_loop(
                tool_loop_outcome(
                    Some("Prose grounded without the graph."),
                    StopReason::Completed,
                    6,
                    3,
                ),
                "p",
            ),
            data_source_degraded: vec![GRAPH_UNAVAILABLE.to_string()],
        };
        let mut tool_loop: Option<&mut ToolLoopGenerator<'_>> = Some(&mut tool_loop);
        let aggregate = generate_aggregate(
            &mut tool_loop,
            &mut None,
            "prompt",
            "system",
            "repo overview",
        )
        .expect("graph-unavailable is evidence degradation, not a hard fail");
        assert_eq!(
            aggregate.data_source_degraded,
            vec![GRAPH_UNAVAILABLE.to_string()]
        );
        assert!(!is_ai_generation_failure_code(GRAPH_UNAVAILABLE));
        assert!(matches!(aggregate.content, GenerationContent::Generated(_)));
    }

    #[test]
    fn generate_aggregate_without_tool_loop_uses_lane_a_one_shot() {
        // No tool-chat route resolved: aggregates fall back to the Lane A
        // one-shot (lane = one_shot), and a Lane A failure degrades rather than
        // hard-failing.
        let mut generate =
            |_prompt: &str, _system: &str, _tier: PromptTier| Some("Lane A prose.".to_string());
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generate);
        let aggregate = generate_aggregate(
            &mut None,
            &mut generate,
            "prompt",
            "system",
            "repo overview",
        )
        .expect("lane A path never hard-fails");
        assert_eq!(aggregate.lane, LANE_ONE_SHOT);
        assert!(matches!(
            aggregate.content,
            GenerationContent::Generated(text) if text == "Lane A prose."
        ));

        let mut failing = |_prompt: &str, _system: &str, _tier: PromptTier| None;
        let mut failing: Option<&mut TextGenerator<'_>> = Some(&mut failing);
        let degraded =
            generate_aggregate(&mut None, &mut failing, "prompt", "system", "repo overview")
                .expect("a Lane A failure degrades, never hard-fails");
        assert!(matches!(
            degraded.content,
            GenerationContent::Failed(GenerationFailureCause::Unavailable)
        ));
    }
}
