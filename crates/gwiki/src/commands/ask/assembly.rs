use std::collections::BTreeSet;

use crate::commands::ask::dedup::ordered_unique_strings;
use crate::output::{AskCodeCitationOutput, AskOutput, SearchOutput, SearchResultOutput};

pub(super) fn ask_output_from_search(search: SearchOutput) -> AskOutput {
    let related_pages = search
        .results
        .iter()
        .map(|hit| crate::output::AskRelatedPageOutput {
            title: hit.title.clone(),
            path: hit.wiki_page.clone(),
            score: hit.score,
        })
        .collect::<Vec<_>>();
    let sources = unique_sources(&search);
    let code_citations = code_citations_from_results(&search.results);
    let degraded_sources = ordered_unique_strings(search.degradations.clone());
    let warnings = ordered_unique_strings(search.degradations);
    let status = if search.results.is_empty() {
        "no_results"
    } else {
        "retrieved"
    };
    AskOutput {
        command: "ask",
        scope: search.scope,
        query: search.query,
        status,
        degraded: !degraded_sources.is_empty(),
        degraded_sources,
        hits: search.results,
        related_pages,
        sources,
        code_edges: Vec::new(),
        code_citations,
        truncated: false,
        truncated_components: Vec::new(),
        gaps: Vec::new(),
        stale_candidates: Vec::new(),
        suggested_questions: Vec::new(),
        warnings,
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

fn code_citations_from_results(results: &[SearchResultOutput]) -> Vec<AskCodeCitationOutput> {
    let mut seen = BTreeSet::new();
    let mut citations = Vec::new();
    for hit in results {
        if !is_code_result(hit) {
            continue;
        }
        let file = hit.source_path.display().to_string();
        let symbol = hit.title.clone();
        if seen.insert((file.clone(), symbol.clone())) {
            citations.push(AskCodeCitationOutput {
                file,
                line: None,
                symbol,
            });
        }
    }
    citations
}

fn is_code_result(hit: &SearchResultOutput) -> bool {
    hit.result_type.is_code()
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::ScopeIdentity;
    use crate::output::{SearchResultOutput, SearchResultType};

    use super::*;

    #[test]
    fn ask_output_keeps_full_retrieval_shape() {
        let output = ask_output_from_search(SearchOutput::new(
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
        ));

        assert_eq!(output.command, "ask");
        assert_eq!(output.status, "retrieved");
        assert_eq!(output.query, "How do hooks work?");
        assert_eq!(output.hits.len(), 1);
        assert_eq!(output.related_pages[0].path, PathBuf::from("wiki/hooks.md"));
        assert_eq!(
            output.sources,
            vec!["bm25".to_string(), "raw/hooks.md".to_string()]
        );
        assert!(output.code_edges.is_empty());
        assert_eq!(output.warnings, vec!["semantic_unavailable".to_string()]);
        assert_eq!(
            output.degraded_sources,
            vec!["semantic_unavailable".to_string()]
        );
        assert!(output.gaps.is_empty());
        assert!(output.stale_candidates.is_empty());
        assert!(output.suggested_questions.is_empty());
        assert!(output.ai.is_none());
        assert!(output.synthesis.is_none());
    }
}
