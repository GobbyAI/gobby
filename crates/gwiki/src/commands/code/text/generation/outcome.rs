use std::collections::BTreeSet;

#[cfg(test)]
use gobby_core::ai::generation::ToolLoopOutcome;
use gobby_core::ai::generation::{DaemonAgenticResult, StopReason};
use gobby_core::ai_types::TokenUsage;

/// Why an AI narrative generation attempt failed on an AI-enabled run.
///
/// Distinct from data-source degradation (graph/vector unavailable, see
/// [`GRAPH_UNAVAILABLE`]): a graph backend being down is *evidence* degradation
/// — the page still renders useful narrative — and is never recorded as a
/// `GenerationFailureCause`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GenerationFailureCause {
    PromptEcho,
    Refusal,
    Unavailable,
}

impl GenerationFailureCause {
    pub(crate) const ALL: [Self; 3] = [Self::PromptEcho, Self::Refusal, Self::Unavailable];

    pub(crate) fn reason_code(self) -> &'static str {
        match self {
            Self::PromptEcho => "model-prompt-echo",
            Self::Refusal => "model-refusal",
            Self::Unavailable => "model-unavailable",
        }
    }
}

/// Data-source degradation reason code for an unavailable code-graph backend.
pub(crate) const GRAPH_UNAVAILABLE: &str = "graph-unavailable";

pub(crate) fn is_ai_generation_failure_code(code: &str) -> bool {
    if code == GRAPH_UNAVAILABLE {
        return false;
    }
    GenerationFailureCause::ALL
        .iter()
        .any(|cause| cause.reason_code() == code)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GenerationStatus {
    Generated,
    Failed,
    Skipped,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct GenerationObservability {
    pub(crate) stop_reason: Option<StopReason>,
    pub(crate) tool_call_count: usize,
    pub(crate) turns: Option<usize>,
    pub(crate) usage: Option<TokenUsage>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum GenerationContent {
    Generated(String),
    Failed(GenerationFailureCause),
    Skipped,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct GenerationOutcome {
    status: GenerationStatus,
    content: Option<String>,
    cause: Option<GenerationFailureCause>,
    observability: GenerationObservability,
}

impl GenerationOutcome {
    pub(crate) fn generated(content: String) -> Self {
        Self {
            status: GenerationStatus::Generated,
            content: Some(content),
            cause: None,
            observability: GenerationObservability {
                stop_reason: Some(StopReason::Completed),
                tool_call_count: 0,
                turns: Some(1),
                usage: None,
            },
        }
    }

    pub(crate) fn rejected(cause: GenerationFailureCause) -> Self {
        Self {
            status: GenerationStatus::Failed,
            content: None,
            cause: Some(cause),
            observability: GenerationObservability {
                stop_reason: Some(StopReason::Completed),
                tool_call_count: 0,
                turns: Some(1),
                usage: None,
            },
        }
    }

    pub(crate) fn unavailable() -> Self {
        Self {
            status: GenerationStatus::Failed,
            content: None,
            cause: Some(GenerationFailureCause::Unavailable),
            observability: GenerationObservability::default(),
        }
    }

    pub(crate) fn skipped() -> Self {
        Self {
            status: GenerationStatus::Skipped,
            content: None,
            cause: None,
            observability: GenerationObservability::default(),
        }
    }

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

    fn classify_content(
        content: Option<String>,
        prompt: &str,
        observability: GenerationObservability,
    ) -> Self {
        match content {
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

    #[cfg(test)]
    pub(crate) fn from_tool_loop(outcome: ToolLoopOutcome, prompt: &str) -> Self {
        let observability = GenerationObservability {
            stop_reason: Some(outcome.stop_reason),
            tool_call_count: outcome.observability.tool_call_count,
            turns: Some(outcome.observability.turns),
            usage: outcome.total_usage,
        };
        if !outcome.stop_reason.is_completed() {
            return Self::rejected_with_observability(
                GenerationFailureCause::Unavailable,
                observability,
            );
        }
        Self::classify_content(outcome.content, prompt, observability)
    }

    pub(crate) fn from_daemon_agentic(result: DaemonAgenticResult, prompt: &str) -> Self {
        let stop_reason = match result.stop_reason.as_deref() {
            Some("completed") => Some(StopReason::Completed),
            Some("max_turns") => Some(StopReason::MaxTurns),
            Some("max_tool_calls") => Some(StopReason::MaxToolCalls),
            Some("timeout") => Some(StopReason::Timeout),
            Some(_) | None => None,
        };
        let observability = GenerationObservability {
            stop_reason,
            tool_call_count: result.tool_use_count,
            turns: result.turns,
            usage: result.usage,
        };
        if stop_reason.is_some_and(|reason| !reason.is_completed()) {
            return Self::rejected_with_observability(
                GenerationFailureCause::Unavailable,
                observability,
            );
        }
        Self::classify_content(result.content, prompt, observability)
    }

    pub(crate) fn observability(&self) -> &GenerationObservability {
        &self.observability
    }

    pub(crate) fn failure_cause(&self) -> Option<GenerationFailureCause> {
        self.cause
    }

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

const PROMPT_ECHO_PREFIX_CHARS: usize = 80;

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

const REFUSAL_SCAN_CHARS: usize = 600;

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

pub(super) fn clean_generated(text: String) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::ai::generation::ToolLoopObservability;

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
        let codes: BTreeSet<_> = GenerationFailureCause::ALL
            .iter()
            .map(|cause| cause.reason_code())
            .collect();
        assert_eq!(codes.len(), GenerationFailureCause::ALL.len());
    }

    #[test]
    fn is_ai_generation_failure_code_excludes_data_source_codes() {
        for cause in GenerationFailureCause::ALL {
            assert!(is_ai_generation_failure_code(cause.reason_code()));
        }
        assert!(!is_ai_generation_failure_code(GRAPH_UNAVAILABLE));
        assert!(!is_ai_generation_failure_code("grounding-empty"));
        assert!(!is_ai_generation_failure_code("not-a-code"));
    }

    #[test]
    fn one_shot_generation_outcome_carries_observability() {
        let outcome = GenerationOutcome::generated("Grounded narrative.".to_string());
        assert_eq!(outcome.status, GenerationStatus::Generated);
        assert_eq!(
            outcome.observability.stop_reason,
            Some(StopReason::Completed)
        );
        assert_eq!(outcome.observability.tool_call_count, 0);
        assert_eq!(outcome.observability.turns, Some(1));
        assert_eq!(outcome.observability.usage, None);
        assert!(matches!(
            outcome.into_content(),
            GenerationContent::Generated(text) if text == "Grounded narrative."
        ));

        let outcome = GenerationOutcome::unavailable();
        assert_eq!(outcome.observability.stop_reason, None);
        assert_eq!(outcome.observability.turns, None);
    }

    #[test]
    fn unwrap_or_record_writes_distinct_codes_only_on_failure() {
        let mut codes = BTreeSet::new();
        let text = GenerationOutcome::rejected(GenerationFailureCause::Refusal)
            .unwrap_or_record("fallback".to_string(), &mut codes);
        assert_eq!(text, "fallback");
        assert!(codes.contains("model-refusal"));
        assert!(!codes.contains("model-unavailable"));

        let mut codes = BTreeSet::new();
        let text =
            GenerationOutcome::skipped().unwrap_or_record("fallback".to_string(), &mut codes);
        assert_eq!(text, "fallback");
        assert!(codes.is_empty());

        let mut codes = BTreeSet::new();
        let text = GenerationOutcome::generated("body".to_string())
            .unwrap_or_record("fallback".to_string(), &mut codes);
        assert_eq!(text, "body");
        assert!(codes.is_empty());
    }

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
        assert_eq!(outcome.observability().turns, Some(3));
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
        let max_turns = GenerationOutcome::from_tool_loop(
            tool_loop_outcome(None, StopReason::MaxTurns, 24, 8),
            "p",
        );
        assert_eq!(
            max_turns.failure_cause(),
            Some(GenerationFailureCause::Unavailable)
        );
        assert_eq!(max_turns.observability().turns, Some(8));

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

    fn daemon_agentic_result(
        content: Option<&str>,
        tool_use_count: usize,
        turns: Option<usize>,
        stop_reason: Option<&str>,
    ) -> DaemonAgenticResult {
        DaemonAgenticResult {
            caller: "gwiki.code".to_string(),
            request_id: "019fc08a-1d63-4b23-bbc8-659d56bc4168".to_string(),
            content: content.map(str::to_string),
            model: Some("claude-opus".to_string()),
            tool_use_count,
            turns,
            stop_reason: stop_reason.map(str::to_string),
            usage: Some(TokenUsage {
                input_tokens: Some(100),
                output_tokens: Some(50),
                total_tokens: Some(150),
            }),
        }
    }

    #[test]
    fn from_daemon_agentic_maps_completed_content_with_provenance() {
        let outcome = GenerationOutcome::from_daemon_agentic(
            daemon_agentic_result(
                Some("A grounded narrative body."),
                7,
                Some(4),
                Some("completed"),
            ),
            "investigate the repo",
        );
        assert_eq!(outcome.observability().tool_call_count, 7);
        assert_eq!(outcome.observability().turns, Some(4));
        assert_eq!(
            outcome.observability().stop_reason,
            Some(StopReason::Completed)
        );
        assert_eq!(
            outcome
                .observability()
                .usage
                .as_ref()
                .and_then(TokenUsage::token_count),
            Some(150)
        );
        assert!(matches!(
            outcome.into_content(),
            GenerationContent::Generated(text) if text == "A grounded narrative body."
        ));
    }

    #[test]
    fn from_daemon_agentic_classifies_missing_echo_and_refusal() {
        let empty = GenerationOutcome::from_daemon_agentic(
            daemon_agentic_result(None, 3, Some(2), Some("completed")),
            "p",
        );
        assert_eq!(
            empty.failure_cause(),
            Some(GenerationFailureCause::Unavailable)
        );
        assert_eq!(empty.observability().turns, Some(2));

        let prompt = "Summarize the architecture of this codebase in thorough detail for the \
                      reader, covering every subsystem boundary and data flow.";
        let echo = GenerationOutcome::from_daemon_agentic(
            daemon_agentic_result(Some(prompt), 1, Some(1), Some("completed")),
            prompt,
        );
        assert_eq!(
            echo.failure_cause(),
            Some(GenerationFailureCause::PromptEcho)
        );

        let refusal = GenerationOutcome::from_daemon_agentic(
            daemon_agentic_result(
                Some("I cannot write this documentation for the codebase."),
                1,
                Some(1),
                Some("completed"),
            ),
            prompt,
        );
        assert_eq!(
            refusal.failure_cause(),
            Some(GenerationFailureCause::Refusal)
        );
    }

    #[test]
    fn from_daemon_agentic_maps_limit_exits_and_optional_provenance() {
        for (reported, expected) in [
            ("max_turns", StopReason::MaxTurns),
            ("max_tool_calls", StopReason::MaxToolCalls),
            ("timeout", StopReason::Timeout),
        ] {
            let outcome = GenerationOutcome::from_daemon_agentic(
                daemon_agentic_result(Some("partial"), 5, Some(8), Some(reported)),
                "prompt",
            );
            assert_eq!(outcome.observability().stop_reason, Some(expected));
            assert_eq!(outcome.observability().turns, Some(8));
            assert_eq!(
                outcome.failure_cause(),
                Some(GenerationFailureCause::Unavailable)
            );
        }

        for reported in [None, Some("provider_specific")] {
            let outcome = GenerationOutcome::from_daemon_agentic(
                daemon_agentic_result(Some("grounded"), 2, None, reported),
                "prompt",
            );
            assert_eq!(outcome.observability().stop_reason, None);
            assert_eq!(outcome.observability().turns, None);
            assert!(matches!(
                outcome.into_content(),
                GenerationContent::Generated(text) if text == "grounded"
            ));
        }
    }

    #[test]
    fn short_prompts_never_trigger_echo_rejection() {
        let prompt = "Short prompt.";
        assert!(!is_prompt_echo("Short prompt.", prompt));
    }

    #[test]
    fn model_refusal_is_detected_but_real_prose_is_not() {
        let refusal = "# Welcome to Gcode\n\nI cannot write this chapter as specified. \
             The supplied evidence is insufficient to create a guided-tour chapter.";
        assert!(is_model_refusal(refusal));
        let prose = "The indexing pipeline parses each file with tree-sitter and writes \
             symbols to PostgreSQL. It cannot index binary files, which are skipped.";
        assert!(!is_model_refusal(prose));
    }

    #[test]
    fn refusal_marker_after_the_lead_is_ignored() {
        let body = format!(
            "{}\n\nA contributor once joked they i cannot write tests fast enough.",
            "Real grounded prose about the parser. ".repeat(30)
        );
        assert!(!is_model_refusal(&body));
    }
}
