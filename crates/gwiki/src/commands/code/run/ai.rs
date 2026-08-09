use std::collections::BTreeSet;

use gobby_core::ai::AiNoticeKind;
use gobby_core::config::AiRouting;

use crate::commands::code::{
    CodeEngineRuntime, CodewikiAiOptions, CodewikiAiOutcome, CodewikiGraphAvailability,
    SyncTextGenerator, SyncTextVerifier, ToolLoopGenerator, direct_route_candidate_error,
    resolve_text_generator, resolve_text_verifier, resolve_tool_loop_generator,
};

pub(super) struct ResolvedAiRun {
    pub generator: Option<Box<SyncTextGenerator<'static>>>,
    pub verifier: Option<Box<SyncTextVerifier<'static>>>,
    pub tool_loop_generator: Option<Box<ToolLoopGenerator<'static>>>,
    pub ai_outcome: CodewikiAiOutcome,
    pub aggregate_ai_outcome: CodewikiAiOutcome,
    pub ai_enabled: bool,
    pub ai_mode: &'static str,
    pub notices: AiRunNotices,
}

impl ResolvedAiRun {
    pub(super) fn resolve(
        ctx: &CodeEngineRuntime,
        options: &CodewikiAiOptions,
        graph_availability: CodewikiGraphAvailability,
    ) -> anyhow::Result<Self> {
        let resolved_generator = resolve_text_generator(ctx, options);
        let mut notices = AiRunNotices::default();
        notices.warn_once(ctx, resolved_generator.notice_kind());
        let ai_outcome = resolved_generator.ai_outcome();
        let no_generator_reason = resolved_generator.no_generator_reason;
        let generator = resolved_generator.generator;
        let verifier = resolve_text_verifier(ctx, options);
        let resolved_tool_loop = resolve_tool_loop_generator(ctx, options, graph_availability);

        if let Some(error) = direct_route_candidate_error(
            &options.aggregate_candidates,
            resolved_generator.ai_route,
            resolved_tool_loop
                .generator
                .is_some()
                .then_some(resolved_tool_loop.ai_outcome.route),
        ) {
            anyhow::bail!(error);
        }
        let aggregate_ai_outcome = if resolved_tool_loop.generator.is_some() {
            resolved_tool_loop.ai_outcome
        } else {
            ai_outcome
        };
        let ai_enabled = generator.is_some();
        let ai_mode = if ai_outcome.route == AiRouting::Off
            && !ai_outcome.fallback
            && no_generator_reason.is_none()
        {
            "off"
        } else {
            options.depth.mode_label()
        };

        Ok(Self {
            generator,
            verifier,
            tool_loop_generator: resolved_tool_loop.generator,
            ai_outcome,
            aggregate_ai_outcome,
            ai_enabled,
            ai_mode,
            notices,
        })
    }
}

#[derive(Default)]
pub(super) struct AiRunNotices {
    emitted: BTreeSet<AiNoticeKind>,
}

impl AiRunNotices {
    pub(super) fn warn_once(&mut self, ctx: &CodeEngineRuntime, notice: Option<AiNoticeKind>) {
        let Some(notice) = notice else {
            return;
        };
        if ctx.quiet || !self.emitted.insert(notice) {
            return;
        }
        let message = match notice {
            AiNoticeKind::AutoFallbackToDirect => {
                "codewiki: AI auto routing could not use the daemon; falling back to Direct generation"
            }
            AiNoticeKind::AutoFallbackToOff => {
                "codewiki: AI auto routing found no daemon or usable Direct config; writing structural docs"
            }
            AiNoticeKind::NoGenerator => {
                "codewiki: AI generation was requested but no usable generator is configured; writing structural docs"
            }
            AiNoticeKind::GenerationFailed => {
                "codewiki: AI generation failed; affected pages record degraded status"
            }
        };
        eprintln!("{message}");
    }
}
