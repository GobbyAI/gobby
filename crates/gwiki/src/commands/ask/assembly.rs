use std::collections::BTreeSet;

use crate::commands::ask::evidence::{ASK_PROMPT_TOKEN_BUDGET, EvidencePlan};
use crate::output::{AskOutput, SearchOutput};

pub(super) fn ask_output_from_retrieval(search: SearchOutput, plan: &EvidencePlan) -> AskOutput {
    let sources = unique_sources(&search);
    let degraded_sources = ordered_unique_strings(search.degradations.clone());
    let warnings = ordered_unique_strings(search.degradations);
    let status = if search.results.is_empty() {
        "no_results"
    } else {
        "retrieved"
    };
    let truncated = plan.dropped_hits > 0;
    AskOutput {
        command: "ask",
        scope: search.scope,
        query: search.query,
        status,
        degraded: !degraded_sources.is_empty(),
        degraded_sources,
        hits: search.results,
        sources,
        code_citations: search.code_citations,
        evidence: plan.items.clone(),
        prompt_token_budget: ASK_PROMPT_TOKEN_BUDGET,
        prompt_tokens_estimated: plan.prompt_tokens_estimated,
        truncated,
        truncated_components: if truncated {
            vec!["evidence".to_string()]
        } else {
            Vec::new()
        },
        warnings,
        hint: search.hint,
        ai: None,
        synthesis: None,
    }
}

fn unique_sources(search: &SearchOutput) -> Vec<String> {
    let mut seen = BTreeSet::new();
    for hit in &search.results {
        seen.insert(hit.source_path.display().to_string());
        for source in &hit.sources {
            seen.insert(source.clone());
        }
    }
    seen.into_iter().collect()
}

pub(super) fn ordered_unique_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = BTreeSet::new();
    values
        .into_iter()
        .filter(|value| seen.insert(value.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::ScopeIdentity;
    use crate::commands::ask::evidence::plan_evidence;
    use crate::commands::search::SearchRetrieval;
    use crate::output::{SearchResultOutput, SearchResultType};

    use super::*;

    #[test]
    fn ask_output_keeps_bounded_retrieval_shape() {
        let retrieval = SearchRetrieval {
            output: SearchOutput::new(
                ScopeIdentity::topic("docs"),
                "How do hooks work?",
                10,
                vec![SearchResultOutput {
                    title: Some("Hooks".to_string()),
                    fusion_key: "topic:docs:wiki/hooks.md".to_string(),
                    wiki_page: PathBuf::from("wiki/hooks.md"),
                    source_path: PathBuf::from("raw/hooks.md"),
                    result_type: SearchResultType::Wiki,
                    snippet: "Hooks run at turn boundaries.".to_string(),
                    score: 0.9,
                    sources: vec!["bm25".to_string()],
                    explanations: Vec::new(),
                }],
                vec![
                    "semantic_unavailable".to_string(),
                    "semantic_unavailable".to_string(),
                ],
            ),
            evidence: vec!["Hooks run at turn boundaries and dispatch envelopes.".to_string()],
        };
        let plan = plan_evidence(&retrieval);
        let output = ask_output_from_retrieval(retrieval.output, &plan);

        assert_eq!(output.command, "ask");
        assert_eq!(output.status, "retrieved");
        assert_eq!(output.query, "How do hooks work?");
        assert_eq!(output.hits.len(), 1);
        assert_eq!(
            output.sources,
            vec!["bm25".to_string(), "raw/hooks.md".to_string()]
        );
        assert_eq!(output.evidence.len(), 1);
        assert_eq!(output.evidence[0].wiki_page, PathBuf::from("wiki/hooks.md"));
        assert_eq!(output.prompt_token_budget, ASK_PROMPT_TOKEN_BUDGET);
        assert!(output.prompt_tokens_estimated > 0);
        assert!(!output.truncated);
        assert!(output.truncated_components.is_empty());
        assert_eq!(output.warnings, vec!["semantic_unavailable".to_string()]);
        assert_eq!(
            output.degraded_sources,
            vec!["semantic_unavailable".to_string()]
        );
        assert!(output.ai.is_none());
        assert!(output.synthesis.is_none());
    }
}
