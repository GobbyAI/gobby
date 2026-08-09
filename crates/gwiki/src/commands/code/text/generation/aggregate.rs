use crate::commands::code::{PromptTier, TextGenerator};

use super::one_shot::maybe_generate;
use super::outcome::{GenerationContent, GenerationObservability};
use super::tool_loop::{ToolLoopGenerator, maybe_generate_tool_loop};

pub(crate) const LANE_TOOL_LOOP: &str = "tool_loop";
pub(crate) const LANE_ONE_SHOT: &str = "one_shot";

#[derive(Debug)]
pub(crate) struct AggregateGeneration {
    pub(crate) content: GenerationContent,
    pub(crate) observability: GenerationObservability,
    pub(crate) data_source_degraded: Vec<String>,
    pub(crate) lane: &'static str,
}

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
        if let Some(cause) = result.outcome.failure_cause() {
            return Err(anyhow::anyhow!(
                "Tool-loop {label} generation failed ({}, stop={:?}, turns={:?}, tool_calls={}); \
                 page not written (no skeleton, no one-shot fallback)",
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

#[cfg(test)]
mod tests {
    use super::super::outcome::{
        GRAPH_UNAVAILABLE, GenerationFailureCause, GenerationOutcome, is_ai_generation_failure_code,
    };
    use super::super::tool_loop::ToolLoopResult;
    use super::*;
    use gobby_core::ai::generation::{StopReason, ToolLoopObservability, ToolLoopOutcome};

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
    fn generate_aggregate_tool_loop_success_records_route_and_observability() {
        let mut tool_loop = |_prompt: &str, _system: &str| ToolLoopResult {
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
        .expect("tool-loop success is not a hard fail");
        assert_eq!(aggregate.lane, LANE_TOOL_LOOP);
        assert_eq!(aggregate.observability.tool_call_count, 4);
        assert_eq!(aggregate.observability.turns, Some(2));
        assert!(aggregate.data_source_degraded.is_empty());
        assert!(matches!(
            aggregate.content,
            GenerationContent::Generated(text) if text == "Grounded aggregate prose."
        ));
    }

    #[test]
    fn generate_aggregate_tool_loop_failure_hard_fails_with_reason_code() {
        let mut tool_loop = |_prompt: &str, _system: &str| ToolLoopResult {
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
        .expect_err("a tool-loop failure must hard-fail the page");
        let message = error.to_string();
        assert!(message.contains("Tool-loop architecture"), "{message}");
        assert!(message.contains("model-unavailable"), "{message}");
        assert!(message.contains("no skeleton"), "{message}");
        assert!(message.contains("MaxToolCalls"), "{message}");
        assert!(message.contains("turns=Some(8)"), "{message}");
        assert!(message.contains("tool_calls=24"), "{message}");
    }

    #[test]
    fn generate_aggregate_tool_loop_carries_graph_unavailable_evidence_degradation() {
        let mut tool_loop = |_prompt: &str, _system: &str| ToolLoopResult {
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
    fn generate_aggregate_without_tool_loop_uses_one_shot() {
        let mut generate =
            |_prompt: &str, _system: &str, _tier: PromptTier| Some("One-shot prose.".to_string());
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generate);
        let aggregate = generate_aggregate(
            &mut None,
            &mut generate,
            "prompt",
            "system",
            "repo overview",
        )
        .expect("one-shot path never hard-fails");
        assert_eq!(aggregate.lane, LANE_ONE_SHOT);
        assert!(matches!(
            aggregate.content,
            GenerationContent::Generated(text) if text == "One-shot prose."
        ));

        let mut failing = |_prompt: &str, _system: &str, _tier: PromptTier| None;
        let mut failing: Option<&mut TextGenerator<'_>> = Some(&mut failing);
        let degraded =
            generate_aggregate(&mut None, &mut failing, "prompt", "system", "repo overview")
                .expect("a one-shot failure degrades");
        assert!(matches!(
            degraded.content,
            GenerationContent::Failed(GenerationFailureCause::Unavailable)
        ));
    }
}
