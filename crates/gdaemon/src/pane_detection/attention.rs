//! Attention classification, the classify half of `gobby.agents.attention_tracker` (#24382,
//! plan 2.13.4). The attention store, lifecycle waits and metadata writes stay with the hub.

use super::RunDetectState;
use super::prompt::{
    DetectedPrompt, PromptDetector, PromptKind, classification_payload, pane_fingerprint,
};
use super::stall::{StallClassifier, StallStatus};

#[derive(Debug, Clone, PartialEq)]
pub struct AttentionInput<'a> {
    pub pane: &'a str,
    pub error: Option<&'a str>,
    pub now: f64,
    pub auto_enter: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttentionTransition {
    pub reason: &'static str,
    pub kind: &'static str,
    pub fingerprint: String,
    pub payload: DetectedPrompt,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttentionClassification {
    pub transition: Option<AttentionTransition>,
    pub dismissed: bool,
}

/// Classifies one pane snapshot into the attention episode it opens, as `sync` decides before it
/// writes. `dismissed` reports a prompt the run already handled, which the hub records as
/// "needs attention".
pub fn classify_attention(
    prompt: &PromptDetector<'_>,
    stall: &StallClassifier<'_>,
    state: &mut RunDetectState,
    input: &AttentionInput<'_>,
) -> AttentionClassification {
    let detected = prompt.detect_prompt(input.pane);
    let detected_kind = detected.as_ref().map(|found| found.kind);
    let approval_dismissed = detected_kind == Some(PromptKind::Approval)
        && state.approval_fingerprint.as_deref() == Some(pane_fingerprint(input.pane).as_str());
    let trust_dismissed = detected_kind == Some(PromptKind::Trust) && state.trust_dismissed;
    let (reason, kind, label) = if detected_kind == Some(PromptKind::Approval)
        && (!input.auto_enter || approval_dismissed)
    {
        (Some(PromptKind::Approval), "actionable", None)
    } else if detected_kind == Some(PromptKind::Trust) && trust_dismissed {
        (Some(PromptKind::Trust), "actionable", None)
    } else if detected_kind == Some(PromptKind::Question) {
        (Some(PromptKind::Question), "actionable", None)
    } else {
        let classification =
            stall.classify(&mut state.stall, input.now, Some(input.pane), input.error);
        if classification.status == StallStatus::ProviderStall {
            (
                Some(PromptKind::Stall),
                "non_actionable",
                classification.reason,
            )
        } else {
            (None, "", None)
        }
    };
    let dismissed = approval_dismissed || trust_dismissed;
    let Some(reason) = reason else {
        return AttentionClassification {
            transition: None,
            dismissed,
        };
    };
    let payload = match detected {
        Some(found) if found.kind == reason => found,
        _ => classification_payload(reason, label.as_deref().unwrap_or(reason.as_str())),
    };
    AttentionClassification {
        transition: Some(AttentionTransition {
            reason: reason.as_str(),
            kind,
            fingerprint: payload.fingerprint.clone(),
            payload,
        }),
        dismissed,
    }
}
