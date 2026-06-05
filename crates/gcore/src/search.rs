//! Generic search result and rank-fusion primitives.
//!
//! This module is available with the `search` feature. Domain-specific query
//! behavior stays with the consuming crate.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// RRF constant — matches Python RRF_K in code_index/searcher.py.
const RRF_K: f64 = 60.0;

/// BM25 score function installed by pg_search/PostgresML.
pub const BM25_SCORE_FUNCTION: &str = "pdb.score";

/// Regprocedure signature required by runtime schema validation.
pub const BM25_SCORE_REGPROCEDURE: &str = "pdb.score(anyelement)";

/// Render a BM25 score expression for a table row identifier.
///
/// # Safety
///
/// `row_id` is interpolated into SQL. Callers must pass a trusted row
/// identifier such as a static table alias or schema-qualified column, never
/// user-controlled text.
pub fn bm25_score_expr(row_id: &str) -> String {
    format!("{BM25_SCORE_FUNCTION}({row_id})")
}

/// A search result from any source, with opaque identity and metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    /// Opaque identifier (symbol UUID, doc UUID, chunk ID, etc.).
    pub id: String,
    /// Combined score after fusion.
    pub score: f64,
    /// Which sources contributed this result.
    pub sources: Vec<String>,
    /// Source-level explanations for debugging.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub explanations: Vec<SourceExplanation>,
}

/// Per-source contribution to a fused search result.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceExplanation {
    pub source: String,
    pub rank: usize,
    pub score: f64,
}

/// Metadata for a search that had unavailable sources.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchDegradation {
    pub unavailable_sources: Vec<String>,
    pub available_sources: Vec<String>,
}

/// Merge multiple ranked lists using Reciprocal Rank Fusion.
///
/// Each source is a `(name, ranked_ids)` pair where index 0 = most relevant.
/// Returns results sorted by combined RRF score descending.
pub fn rrf_merge(sources: Vec<(&str, Vec<String>)>) -> Vec<SearchResult> {
    let mut entries: HashMap<String, Vec<SourceExplanation>> = HashMap::new();

    for (source_name, ids) in &sources {
        let mut best_rank: HashMap<&String, usize> = HashMap::new();
        for (rank, id) in ids.iter().enumerate() {
            best_rank
                .entry(id)
                .and_modify(|best| *best = (*best).min(rank))
                .or_insert(rank);
        }

        for (id, rank) in best_rank {
            let score = 1.0 / (RRF_K + rank as f64);
            entries
                .entry(id.clone())
                .or_default()
                .push(SourceExplanation {
                    source: source_name.to_string(),
                    rank,
                    score,
                });
        }
    }

    let mut results: Vec<SearchResult> = entries
        .into_iter()
        .map(|(id, mut explanations)| {
            explanations.sort_by(|a, b| a.source.cmp(&b.source));
            let score = explanations
                .iter()
                .map(|explanation| explanation.score)
                .sum();
            let sources = explanations
                .iter()
                .map(|explanation| explanation.source.clone())
                .collect();

            SearchResult {
                id,
                score,
                sources,
                explanations,
            }
        })
        .collect();

    results.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.id.cmp(&b.id))
    });
    results
}

/// Sanitize user input for pg_search's BM25 query DSL.
pub fn sanitize_pg_search_query(query: &str) -> String {
    let cleaned = query
        .chars()
        .filter_map(|ch| {
            if ch.is_control() {
                ch.is_whitespace().then_some(' ')
            } else {
                Some(ch)
            }
        })
        .collect::<String>();

    cleaned
        .split_whitespace()
        .map(|token| {
            if token.starts_with('-') {
                format!("\\{token}")
            } else {
                token.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rrf_preserves_explanations_and_degradation() {
        let results = rrf_merge(vec![
            ("semantic", vec!["b".to_string(), "a".to_string()]),
            ("fts", vec!["a".to_string()]),
        ]);

        let a = results.iter().find(|result| result.id == "a").unwrap();
        assert_eq!(a.sources, vec!["fts".to_string(), "semantic".to_string()]);
        assert_eq!(a.explanations.len(), 2);
        assert_eq!(a.explanations[0].source, "fts");
        assert_eq!(a.explanations[0].rank, 0);
        assert_eq!(a.explanations[1].source, "semantic");
        assert_eq!(a.explanations[1].rank, 1);

        let degradation = SearchDegradation {
            unavailable_sources: vec!["fallback".to_string()],
            available_sources: vec!["fts".to_string(), "semantic".to_string()],
        };
        assert_eq!(degradation.unavailable_sources, vec!["fallback"]);
        assert_eq!(degradation.available_sources, vec!["fts", "semantic"]);
    }

    #[test]
    fn sanitize_pg_search_query_matches_gobby_rules() {
        assert_eq!(
            sanitize_pg_search_query("foo::bar baz-qux _id + \"drop\""),
            "foo::bar baz-qux _id + \"drop\""
        );
        assert_eq!(sanitize_pg_search_query("-draft stable"), "\\-draft stable");
        assert_eq!(
            sanitize_pg_search_query(r"\-draft -stable"),
            r"\-draft \-stable"
        );
        assert_eq!(
            sanitize_pg_search_query("alpha\tbeta\u{0}gamma"),
            "alpha betagamma"
        );
        assert_eq!(sanitize_pg_search_query(":: + ()"), ":: + ()");
    }

    #[test]
    fn search_result_is_cli_independent() {
        let result = SearchResult {
            id: "symbol-1".to_string(),
            score: 0.25,
            sources: vec!["fts".to_string()],
            explanations: vec![SourceExplanation {
                source: "fts".to_string(),
                rank: 0,
                score: 1.0 / 60.0,
            }],
        };

        let json = serde_json::to_string(&result).unwrap();
        assert!(json.contains("\"id\":\"symbol-1\""));

        let round_trip: SearchResult = serde_json::from_str(&json).unwrap();
        assert_eq!(round_trip.id, result.id);
        assert_eq!(round_trip.sources, result.sources);
        assert_eq!(round_trip.explanations[0].source, "fts");
    }

    #[test]
    fn bm25_score_expression_uses_pdb_score() {
        assert_eq!(bm25_score_expr("row.id"), "pdb.score(row.id)");
    }

    #[test]
    fn bm25_score_regprocedure_matches_runtime_schema_contract() {
        assert_eq!(BM25_SCORE_REGPROCEDURE, "pdb.score(anyelement)");
    }

    #[test]
    fn search_core_has_no_domain_queries() {
        let source = include_str!("search.rs");
        for forbidden in forbidden_domain_fragments() {
            assert!(
                !source.contains(&forbidden),
                "search core should not contain domain-specific query fragment {forbidden:?}"
            );
        }
    }

    fn forbidden_domain_fragments() -> Vec<String> {
        [
            ["SEL", "ECT "],
            ["FR", "OM "],
            ["WHE", "RE "],
            ["qd", "rant"],
            ["pay", "load"],
            ["CA", "LLS"],
            ["gra", "ph"],
            ["Fal", "kor"],
            ["Gra", "ph"],
            ["code", "_symbols"],
            ["code", "_content_chunks"],
            ["gwiki", "_documents"],
            ["gwiki", "_chunks"],
            ["JOIN", " "],
        ]
        .into_iter()
        .map(|parts| parts.concat())
        .collect()
    }

    #[test]
    fn rrf_deduplicates_within_source() {
        let results = rrf_merge(vec![("fts", vec!["a".to_string(), "a".to_string()])]);

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, "a");
        assert_eq!(results[0].sources, vec!["fts".to_string()]);
        assert_eq!(results[0].explanations.len(), 1);
        assert_eq!(results[0].explanations[0].rank, 0);
        assert!((results[0].score - (1.0 / 60.0)).abs() < 1e-10);
    }

    #[test]
    fn rrf_sorts_sources_deterministically() {
        let results = rrf_merge(vec![
            ("semantic", vec!["b".to_string()]),
            ("fts", vec!["b".to_string()]),
        ]);

        assert_eq!(results[0].id, "b");
        assert_eq!(
            results[0].sources,
            vec!["fts".to_string(), "semantic".to_string()]
        );
        assert_eq!(results[0].explanations[0].source, "fts");
        assert_eq!(results[0].explanations[1].source, "semantic");
    }
}
