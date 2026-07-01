use std::collections::{BTreeMap, BTreeSet};

use gobby_core::ai::effective_route;
use gobby_core::ai::generation::{
    GenerationTier, generate_one_shot, profile_for_tier, resolve_direct_generation_target,
};
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::ai_types::AiError;
use gobby_core::config::{AiCapability, AiRouting};
use serde::{Deserialize, Serialize};

use crate::WikiError;
use crate::provenance::ProvenanceGraph;

use super::{ContradictionFinding, ContradictionSection};

#[derive(Debug, Clone, Serialize)]
pub(super) struct SectionClaimComparison {
    pub(super) section: String,
    pub(super) claims: Vec<SourceClaim>,
}

#[derive(Debug, Clone, Serialize)]
pub(super) struct SourceClaim {
    pub(super) source_id: String,
    pub(super) claim: String,
}

#[derive(Debug, Deserialize)]
struct ContradictionModelOutput {
    findings: Vec<ContradictionFinding>,
}

pub(super) fn contradiction_section(
    provenance: &ProvenanceGraph,
    ai_available: bool,
    detector: &mut impl FnMut(&[SectionClaimComparison]) -> Result<Vec<ContradictionFinding>, WikiError>,
) -> Result<ContradictionSection, WikiError> {
    if !ai_available {
        return Ok(ContradictionSection {
            available: false,
            note: Some(
                "AI-assisted contradiction detection unavailable; no contradictions fabricated."
                    .to_string(),
            ),
            findings: Vec::new(),
        });
    }

    let comparisons = section_claim_comparisons(provenance);
    if comparisons.is_empty() {
        return Ok(ContradictionSection {
            available: true,
            note: Some(
                "No multi-source sections with differing source claims were available for \
                 contradiction detection."
                    .to_string(),
            ),
            findings: Vec::new(),
        });
    }

    let source_ids = comparison_source_ids(&comparisons);
    let findings = sanitize_contradiction_findings(detector(&comparisons)?, &source_ids);
    Ok(ContradictionSection {
        available: true,
        note: None,
        findings,
    })
}

fn section_claim_comparisons(provenance: &ProvenanceGraph) -> Vec<SectionClaimComparison> {
    let mut sections: BTreeMap<String, BTreeMap<(String, String), SourceClaim>> = BTreeMap::new();
    for link in provenance.links() {
        let Some(claim) = link
            .claim
            .as_deref()
            .map(str::trim)
            .filter(|claim| !claim.is_empty())
        else {
            continue;
        };
        let source_id = link.source.source_id.trim();
        if source_id.is_empty() {
            continue;
        }
        let section = format!(
            "{}#{}",
            link.section.page_path.display(),
            link.section.section_id
        );
        sections
            .entry(section)
            .or_default()
            .entry((source_id.to_string(), normalize_claim(claim)))
            .or_insert_with(|| SourceClaim {
                source_id: source_id.to_string(),
                claim: claim.to_string(),
            });
    }

    sections
        .into_iter()
        .filter_map(|(section, claims)| {
            let claims: Vec<_> = claims.into_values().collect();
            let source_count = claims
                .iter()
                .map(|claim| claim.source_id.as_str())
                .collect::<BTreeSet<_>>()
                .len();
            let claim_count = claims
                .iter()
                .map(|claim| normalize_claim(&claim.claim))
                .collect::<BTreeSet<_>>()
                .len();
            (source_count >= 2 && claim_count >= 2)
                .then_some(SectionClaimComparison { section, claims })
        })
        .collect()
}

fn comparison_source_ids(comparisons: &[SectionClaimComparison]) -> BTreeSet<String> {
    comparisons
        .iter()
        .flat_map(|comparison| comparison.claims.iter())
        .map(|claim| claim.source_id.clone())
        .collect()
}

pub(super) fn model_contradiction_findings(
    comparisons: &[SectionClaimComparison],
) -> Result<Vec<ContradictionFinding>, WikiError> {
    let prompt_input =
        serde_json::to_string_pretty(comparisons).map_err(|error| WikiError::Json {
            action: "serialize contradiction detection prompt input",
            path: None,
            source: error,
        })?;
    let prompt = format!(
        "Compare the following wiki section claims across sources. Only report direct factual \
         contradictions between source-backed claims from the same section. Do not report repeated \
         support, paraphrases, omissions, or unrelated differences.\n\n{prompt_input}\n\nReturn JSON \
         only with this schema: {{\"findings\":[{{\"claim\":\"short contradiction summary\",\
         \"source_ids\":[\"source-id-a\",\"source-id-b\"]}}]}}."
    );
    let mut source = crate::support::config::hub_ai_config_source("gwiki citation-quality")?;
    let context = AiContext::resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: None,
        },
    );
    let route = effective_route(&context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return Ok(Vec::new());
    }
    // Contradiction QA is the Module (`feature_mid`) text-generate tier; resolve the
    // Direct-route target from the same config source and route through tier->profile.
    let target = matches!(route, AiRouting::Direct).then(|| {
        resolve_direct_generation_target(
            &mut source,
            &profile_for_tier(GenerationTier::Module, None),
        )
    });
    let result = generate_one_shot(
        &context,
        route,
        GenerationTier::Module,
        None,
        target.as_ref(),
        &prompt,
        Some(CONTRADICTION_SYSTEM),
        None,
    )
    .map_err(ai_error_to_wiki_error)?;
    parse_contradiction_findings(&result.text)
}

const CONTRADICTION_SYSTEM: &str = "You are a citation-quality auditor. Identify only direct, \
    material factual contradictions between source-backed claims in the same wiki section. Return \
    valid JSON only. Do not use markdown.";

pub(super) fn parse_contradiction_findings(
    text: &str,
) -> Result<Vec<ContradictionFinding>, WikiError> {
    let json = extract_json_payload(text);
    let output: ContradictionModelOutput =
        serde_json::from_str(json).map_err(|error| WikiError::Json {
            action: "parse contradiction detection response",
            path: None,
            source: error,
        })?;
    Ok(output.findings)
}

fn extract_json_payload(text: &str) -> &str {
    let trimmed = text.trim();
    if let Some(json) = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```"))
        .and_then(|body| body.strip_suffix("```"))
    {
        json.trim()
    } else {
        trimmed
    }
}

fn sanitize_contradiction_findings(
    findings: Vec<ContradictionFinding>,
    allowed_source_ids: &BTreeSet<String>,
) -> Vec<ContradictionFinding> {
    let mut seen = BTreeSet::new();
    let mut sanitized = Vec::new();
    for finding in findings {
        let claim = finding.claim.trim();
        if claim.is_empty() {
            continue;
        }
        let mut source_ids = finding
            .source_ids
            .into_iter()
            .map(|source_id| source_id.trim().to_string())
            .filter(|source_id| allowed_source_ids.contains(source_id))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        if source_ids.len() < 2 {
            continue;
        }
        let key = (normalize_claim(claim), source_ids.clone());
        if seen.insert(key) {
            sanitized.push(ContradictionFinding {
                claim: claim.to_string(),
                source_ids: std::mem::take(&mut source_ids),
            });
        }
    }
    sanitized
}

fn normalize_claim(claim: &str) -> String {
    claim
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn ai_error_to_wiki_error(error: AiError) -> WikiError {
    WikiError::Daemon {
        endpoint: "gcore::ai/text-generate",
        message: error.to_string(),
    }
}
