use crate::commands::search::{SearchRetrieval, query_window};
use crate::output::AskEvidenceOutput;

/// Hard cap on the estimated token count of the single synthesis prompt.
/// Evidence assembly stops before the prompt would exceed this budget.
pub(super) const ASK_PROMPT_TOKEN_BUDGET: usize = 12_000;

/// Per-hit evidence excerpt bounds: a query-token-centered character window.
/// Chunk-sized evidence only — a whole document body never enters the prompt.
const EVIDENCE_BEFORE_CHARS: usize = 800;
const EVIDENCE_AFTER_CHARS: usize = 3_200;

/// Conservative ~4 chars/token estimate, rounded up.
pub(super) fn estimate_tokens(chars: usize) -> usize {
    chars.div_ceil(4)
}

/// Bounded evidence selected for the single synthesis completion, plus the
/// fully assembled prompt. Excerpts align with `items`.
pub(super) struct EvidencePlan {
    pub(super) items: Vec<AskEvidenceOutput>,
    pub(super) excerpts: Vec<String>,
    pub(super) prompt: String,
    pub(super) prompt_tokens_estimated: usize,
    pub(super) dropped_hits: usize,
}

/// Assemble top-k evidence in rank order until the next excerpt would push
/// the prompt past [`ASK_PROMPT_TOKEN_BUDGET`]; remaining hits are dropped
/// and surfaced via `truncated`/`truncated_components` on the output.
pub(super) fn plan_evidence(retrieval: &SearchRetrieval) -> EvidencePlan {
    let query = &retrieval.output.query;
    let mut prompt = format!("Question: {query}\n\nEvidence:\n");
    let mut items = Vec::new();
    let mut excerpts = Vec::new();
    let mut dropped_hits = 0usize;

    if retrieval.output.results.is_empty() {
        prompt.push_str("No wiki evidence was found.\n");
    }

    for (index, hit) in retrieval.output.results.iter().enumerate() {
        let materialized = retrieval.evidence.iter().find(|evidence| {
            evidence.fusion_key == hit.fusion_key
                && evidence.wiki_page == hit.wiki_page
                && evidence.source_path == hit.source_path
        });
        let (raw, content_hash) = match materialized {
            Some(evidence) => (evidence.body.as_str(), evidence.content_hash.clone()),
            None => (
                hit.snippet.as_str(),
                crate::page_version::content_hash(&hit.snippet),
            ),
        };
        let excerpt = query_window(raw, query, EVIDENCE_BEFORE_CHARS, EVIDENCE_AFTER_CHARS)
            .trim()
            .to_string();
        if excerpt.is_empty() {
            continue;
        }
        let entry = format!(
            "{}. {} (wiki: {}, source: {})\n{}\n\n",
            items.len() + 1,
            hit.title.as_deref().unwrap_or("Untitled"),
            hit.wiki_page.display(),
            hit.source_path.display(),
            excerpt,
        );
        let projected_chars = prompt.chars().count() + entry.chars().count();
        if estimate_tokens(projected_chars) > ASK_PROMPT_TOKEN_BUDGET {
            // Rank order is meaningful: stop at the first overflow instead of
            // back-filling with lower-ranked hits.
            dropped_hits = retrieval.output.results.len() - index;
            break;
        }
        prompt.push_str(&entry);
        items.push(AskEvidenceOutput {
            wiki_page: hit.wiki_page.clone(),
            source_path: hit.source_path.clone(),
            content_hash,
            excerpt_chars: excerpt.chars().count(),
        });
        excerpts.push(excerpt);
    }

    let prompt_tokens_estimated = estimate_tokens(prompt.chars().count());
    EvidencePlan {
        items,
        excerpts,
        prompt,
        prompt_tokens_estimated,
        dropped_hits,
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::ScopeIdentity;
    use crate::commands::search::{SearchEvidence, SearchRetrieval};
    use crate::output::{SearchOutput, SearchResultOutput, SearchResultType};

    use super::*;

    fn retrieval_with_bodies(bodies: Vec<String>) -> SearchRetrieval {
        let result_count = bodies.len();
        let results = bodies
            .iter()
            .enumerate()
            .map(|(index, _)| SearchResultOutput {
                title: Some(format!("Hit {index}")),
                fusion_key: format!("topic:docs:wiki/hit-{index}.md"),
                wiki_page: PathBuf::from(format!("wiki/hit-{index}.md")),
                source_path: PathBuf::from(format!("raw/hit-{index}.md")),
                result_type: SearchResultType::Wiki,
                snippet: "bounded display snippet".to_string(),
                score: 1.0 - index as f64 * 0.01,
                sources: vec!["bm25".to_string()],
                explanations: Vec::new(),
            })
            .collect();
        let evidence = bodies
            .into_iter()
            .enumerate()
            .map(|(index, body)| SearchEvidence {
                fusion_key: format!("topic:docs:wiki/hit-{index}.md"),
                wiki_page: PathBuf::from(format!("wiki/hit-{index}.md")),
                source_path: PathBuf::from(format!("raw/hit-{index}.md")),
                content_hash: crate::page_version::content_hash(&body),
                body,
            })
            .collect();
        SearchRetrieval {
            output: SearchOutput::new(
                ScopeIdentity::topic("docs"),
                "enqueue failure handling",
                result_count,
                results,
                Vec::new(),
            ),
            evidence,
        }
    }

    #[test]
    fn prompt_never_exceeds_token_budget() {
        // 40 hits with maximal evidence windows would blow far past the
        // budget if unbounded.
        let bodies = vec!["enqueue failure ".repeat(20_000); 40];
        let plan = plan_evidence(&retrieval_with_bodies(bodies));

        assert!(plan.prompt_tokens_estimated <= ASK_PROMPT_TOKEN_BUDGET);
        assert!(plan.dropped_hits > 0);
        assert_eq!(plan.items.len() + plan.dropped_hits, 40);
    }

    #[test]
    fn evidence_excerpts_are_chunk_sized_not_full_bodies() {
        let body = format!(
            "{}the inbox enqueue failure path retries{}",
            "lead ".repeat(10_000),
            " tail".repeat(10_000)
        );
        let body_chars = body.chars().count();
        let plan = plan_evidence(&retrieval_with_bodies(vec![body]));

        assert_eq!(plan.items.len(), 1);
        assert!(plan.items[0].excerpt_chars <= EVIDENCE_BEFORE_CHARS + EVIDENCE_AFTER_CHARS);
        assert!(plan.items[0].excerpt_chars < body_chars);
        assert!(plan.excerpts[0].contains("enqueue failure"));
    }

    #[test]
    fn empty_excerpts_are_skipped_without_consuming_budget() {
        let plan = plan_evidence(&retrieval_with_bodies(vec![
            String::new(),
            "Later document evidence.".to_string(),
        ]));

        assert_eq!(plan.items.len(), 1);
        assert_eq!(plan.items[0].wiki_page, PathBuf::from("wiki/hit-1.md"));
        assert_eq!(plan.excerpts, vec!["Later document evidence."]);
        assert!(plan.prompt.contains("1. Hit 1"));
        assert!(!plan.prompt.contains("Hit 0"));
        assert_eq!(plan.dropped_hits, 0);
    }

    #[test]
    fn empty_graph_hit_followed_by_document_keeps_document_provenance() {
        let mut retrieval = retrieval_with_bodies(vec![
            String::new(),
            "Document evidence survives.".to_string(),
        ]);
        retrieval.output.results[0].wiki_page = PathBuf::from("graph/entity");
        retrieval.output.results[0].source_path = PathBuf::from("graph/entity");
        retrieval.output.results[1].wiki_page = PathBuf::from("wiki/document.md");
        retrieval.output.results[1].source_path = PathBuf::from("raw/document.md");
        retrieval.evidence[0].wiki_page = PathBuf::from("graph/entity");
        retrieval.evidence[0].source_path = PathBuf::from("graph/entity");
        retrieval.evidence[1].wiki_page = PathBuf::from("wiki/document.md");
        retrieval.evidence[1].source_path = PathBuf::from("raw/document.md");
        retrieval.evidence.swap(0, 1);

        let plan = plan_evidence(&retrieval);

        assert_eq!(plan.items.len(), 1);
        assert_eq!(plan.items[0].wiki_page, PathBuf::from("wiki/document.md"));
        assert_eq!(plan.items[0].source_path, PathBuf::from("raw/document.md"));
        assert_eq!(plan.excerpts, vec!["Document evidence survives."]);
    }

    #[test]
    fn empty_results_state_missing_evidence() {
        let plan = plan_evidence(&retrieval_with_bodies(Vec::new()));

        assert!(plan.prompt.contains("No wiki evidence was found."));
        assert_eq!(plan.dropped_hits, 0);
        assert!(plan.items.is_empty());
    }
}
