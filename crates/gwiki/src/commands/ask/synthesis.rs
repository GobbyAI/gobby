use gobby_core::ai::generation::{
    DirectGenerationTarget, GenerationTier, generate_one_shot, profile_for_tier,
    resolve_direct_generation_target,
};
use gobby_core::ai::{AiNoticeKind, resolve_route_observed};
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};

use crate::WikiError;
use crate::commands::ask::citation::citation_check;
use crate::commands::ask::evidence::EvidencePlan;
use crate::commands::ask::narration::strip_leading_model_narration;
use crate::output::{AskAiOutput, AskOutput, AskSynthesisOutput};

/// Ask answers are a single bounded synthesis over retrieved evidence, so they
/// generate on the module tier. Tier -> feature profile is owned by gcore's
/// `profile_for_tier` (Module -> feature_mid); provider/model resolution stays
/// in config and is never pinned here. The Daemon route forwards the resolved
/// profile name; the Direct route resolves it to a concrete target so a
/// standalone gcore.yaml routes ask to its own provider/model/api_key.
const ASK_TIER: GenerationTier = GenerationTier::Module;

/// Run the single bounded-prompt completion over the planned evidence.
/// Transport is the daemon route or the direct OpenAI-compatible endpoint
/// (`ai.text_generate.api_base`/`api_key`, including LM Studio), resolved by
/// the same gcore routing every other gwiki capability uses.
pub(super) fn synthesize(
    output: &mut AskOutput,
    plan: &EvidencePlan,
    requested_mode: AiRouting,
    require_ai: bool,
) -> Result<(), WikiError> {
    let mut source = crate::support::config::hub_ai_config_source("gwiki ask")?;
    let context = AiContext::resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: Some(requested_mode),
        },
    );
    let observed = resolve_route_observed(&context, AiCapability::TextGenerate);
    let route = observed.route;
    if let Some(notice) = observed.reason.or_else(|| {
        (observed.fallback && route == AiRouting::Direct)
            .then_some(AiNoticeKind::AutoFallbackToDirect)
    }) {
        push_ai_notice_warning(output, notice);
    }
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode: routing_label(requested_mode),
        route: routing_label(route),
        status: "unavailable",
        model: None,
        error: None,
    });

    match route {
        AiRouting::Direct => {
            // Resolve the per-tier target from the same config source (hub
            // config_store plus any standalone gcore.yaml) the context was
            // built from; the Daemon route forwards the profile name instead.
            let target =
                resolve_direct_generation_target(&mut source, &profile_for_tier(ASK_TIER, None));
            if target.api_base().is_none() {
                push_ai_notice_warning(output, AiNoticeKind::NoGenerator);
                return mark_ai_unavailable(
                    output,
                    require_ai,
                    Some("direct AI synthesis requires ai.text_generate api_base".to_string()),
                );
            }
            generate_synthesis(output, plan, &context, route, Some(&target), require_ai)
        }
        AiRouting::Daemon => generate_synthesis(output, plan, &context, route, None, require_ai),
        AiRouting::Auto | AiRouting::Off => mark_ai_unavailable(output, require_ai, None),
    }
}

fn generate_synthesis(
    output: &mut AskOutput,
    plan: &EvidencePlan,
    context: &AiContext,
    route: AiRouting,
    target: Option<&DirectGenerationTarget>,
    require_ai: bool,
) -> Result<(), WikiError> {
    match generate_one_shot(
        context,
        route,
        ASK_TIER,
        None,
        target,
        &plan.prompt,
        Some(synthesis_system()),
        None,
    ) {
        Ok(result) => {
            record_synthesis(
                output,
                &plan.excerpts,
                routing_label(route),
                result.text,
                result.model,
            );
            Ok(())
        }
        Err(error) => {
            push_ai_notice_warning(output, AiNoticeKind::GenerationFailed);
            mark_ai_unavailable(output, require_ai, Some(error.to_string()))
        }
    }
}

pub(super) fn record_synthesis(
    output: &mut AskOutput,
    evidence_excerpts: &[String],
    route: &'static str,
    answer: String,
    model: Option<String>,
) {
    let answer = strip_leading_model_narration(&answer);
    output.status = "answered";
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode: output
            .ai
            .as_ref()
            .map(|ai| ai.requested_mode)
            .unwrap_or(route),
        route,
        status: "available",
        model: model.clone(),
        error: None,
    });
    let citation_check = citation_check(&answer, output, evidence_excerpts);
    for claim in &citation_check.unsupported_claims {
        let warning =
            format!("synthesis claim lacks citation support in retrieved evidence: {claim}");
        if !output.warnings.contains(&warning) {
            output.warnings.push(warning);
        }
    }
    output.synthesis = Some(AskSynthesisOutput {
        answer,
        model,
        citation_check,
    });
}

fn mark_ai_unavailable(
    output: &mut AskOutput,
    require_ai: bool,
    error: Option<String>,
) -> Result<(), WikiError> {
    if require_ai {
        return Err(WikiError::Config {
            detail: error.unwrap_or_else(|| "AI synthesis is unavailable".to_string()),
        });
    }
    output.status = "partial";
    output.degraded = true;
    if !output
        .degraded_sources
        .iter()
        .any(|source| source == "model_provider_unavailable")
    {
        output
            .degraded_sources
            .push("model_provider_unavailable".to_string());
    }
    if !output
        .warnings
        .iter()
        .any(|warning| warning == "ai_unavailable")
    {
        output.warnings.push("ai_unavailable".to_string());
    }
    if let Some(ai) = &mut output.ai {
        ai.error = error;
    }
    Ok(())
}

fn push_ai_notice_warning(output: &mut AskOutput, notice: AiNoticeKind) {
    let warning = ai_notice_label(notice).to_string();
    if !output.warnings.contains(&warning) {
        output.warnings.push(warning);
    }
}

fn ai_notice_label(notice: AiNoticeKind) -> &'static str {
    match notice {
        AiNoticeKind::AutoFallbackToDirect => "ai_auto_fallback_to_direct",
        AiNoticeKind::AutoFallbackToOff => "ai_auto_fallback_to_off",
        AiNoticeKind::NoGenerator => "ai_no_generator",
        AiNoticeKind::GenerationFailed => "ai_generation_failed",
    }
}

fn synthesis_system() -> &'static str {
    "Answer the user's question using only the provided evidence excerpts and code citations. Write only the final answer; do not describe your process, plan, search, or retrieval steps. Be concise. Say when the evidence is insufficient."
}

fn routing_label(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Auto => "auto",
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Off => "off",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ScopeIdentity;
    use crate::commands::ask::assembly::ask_output_from_retrieval;
    use crate::commands::ask::evidence::plan_evidence;
    use crate::commands::ask::test_support::retrieval_with_hooks_hit;
    use crate::commands::search::SearchRetrieval;
    use crate::output::SearchOutput;

    #[test]
    fn ask_generates_on_the_module_feature_profile() {
        use gobby_core::ai::generation::FEATURE_MID;
        assert_eq!(ASK_TIER, GenerationTier::Module);
        assert_eq!(profile_for_tier(ASK_TIER, None), FEATURE_MID);
    }

    #[test]
    fn synthesis_with_ungrounded_claim_is_flagged_in_json() {
        let retrieval = retrieval_with_hooks_hit();
        let plan = plan_evidence(&retrieval);
        let mut output = ask_output_from_retrieval(retrieval.output, &plan);

        record_synthesis(
            &mut output,
            &plan.excerpts,
            "direct",
            "Hooks run at turn boundaries and dispatch envelopes to the daemon. \
                 Kubernetes pods restart the scheduler cluster nightly."
                .to_string(),
            Some("test-model".to_string()),
        );

        let synthesis = output.synthesis.as_ref().expect("synthesis recorded");
        assert_eq!(synthesis.citation_check.status, "unsupported_claims");
        assert_eq!(synthesis.citation_check.checked_claims, 2);
        assert_eq!(
            synthesis.citation_check.unsupported_claims,
            vec!["Kubernetes pods restart the scheduler cluster nightly".to_string()]
        );
        assert_eq!(
            output.warnings,
            vec![
                "synthesis claim lacks citation support in retrieved evidence: \
                     Kubernetes pods restart the scheduler cluster nightly"
                    .to_string()
            ]
        );

        let json = serde_json::to_value(&output).expect("serialize ask output");
        assert_eq!(
            json["synthesis"]["citation_check"]["status"],
            "unsupported_claims"
        );
        assert_eq!(
            json["synthesis"]["citation_check"]["unsupported_claims"][0],
            "Kubernetes pods restart the scheduler cluster nightly"
        );
        assert!(
            json["warnings"][0]
                .as_str()
                .expect("warning is a string")
                .contains("lacks citation support")
        );
    }

    #[test]
    fn synthesis_strips_leading_model_narration_before_recording() {
        let retrieval = retrieval_with_hooks_hit();
        let plan = plan_evidence(&retrieval);
        let mut output = ask_output_from_retrieval(retrieval.output, &plan);

        record_synthesis(
                &mut output,
                &plan.excerpts,
                "daemon",
                "I'm checking the codewiki docs just enough to answer which page types it emits, \
                 then I'll summarize the set precisely.I've got the documented page categories already. \
                 Hooks run at turn boundaries and dispatch envelopes to the daemon."
                    .to_string(),
                Some("test-model".to_string()),
            );

        let synthesis = output.synthesis.as_ref().expect("synthesis recorded");
        assert_eq!(
            synthesis.answer,
            "Hooks run at turn boundaries and dispatch envelopes to the daemon."
        );
        assert_eq!(synthesis.citation_check.status, "supported");
        assert_eq!(synthesis.citation_check.checked_claims, 1);
        assert!(synthesis.citation_check.unsupported_claims.is_empty());
        assert!(output.warnings.is_empty());
    }

    #[test]
    fn synthesis_system_requests_answer_only_output() {
        let system = synthesis_system();

        assert!(system.contains("Write only the final answer"));
        assert!(system.contains("do not describe your process"));
        assert!(system.contains("retrieval steps"));
    }

    #[test]
    fn synthesis_grounded_in_hits_passes_citation_check() {
        let retrieval = retrieval_with_hooks_hit();
        let plan = plan_evidence(&retrieval);
        let mut output = ask_output_from_retrieval(retrieval.output, &plan);

        record_synthesis(
            &mut output,
            &plan.excerpts,
            "daemon",
            "Hooks dispatch envelopes to the daemon at turn boundaries. \
                 The evidence is thin."
                .to_string(),
            None,
        );

        let synthesis = output.synthesis.as_ref().expect("synthesis recorded");
        assert_eq!(synthesis.citation_check.status, "supported");
        assert_eq!(synthesis.citation_check.checked_claims, 1);
        assert!(synthesis.citation_check.unsupported_claims.is_empty());
        assert!(output.warnings.is_empty());
    }

    #[test]
    fn ask_model_unavailable_marks_degraded() {
        let retrieval = SearchRetrieval {
            output: SearchOutput::new(
                ScopeIdentity::project("project-1"),
                "Can this synthesize?",
                10,
                Vec::new(),
                Vec::new(),
            ),
            evidence: Vec::new(),
        };
        let plan = plan_evidence(&retrieval);
        let mut output = ask_output_from_retrieval(retrieval.output, &plan);

        mark_ai_unavailable(&mut output, false, Some("no model".to_string()))
            .expect("model unavailable should degrade without require_ai");

        assert!(output.degraded);
        assert_eq!(
            output.degraded_sources,
            vec!["model_provider_unavailable".to_string()]
        );
        assert_eq!(output.warnings, vec!["ai_unavailable".to_string()]);
    }
}
