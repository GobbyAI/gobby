//! Reciprocal Rank Fusion: merges ranked result lists from multiple sources.
//!
//! score(rank) = 1.0 / (K + rank) where K = 60.
//! Ports logic from src/gobby/code_index/searcher.py.

use std::collections::HashMap;

/// RRF constant — matches Python RRF_K in code_index/searcher.py and memory/manager.py.
const RRF_K: f64 = 60.0;

/// Compute RRF score for a given rank (0-indexed).
fn rrf_score(rank: usize) -> f64 {
    1.0 / (RRF_K + rank as f64)
}

/// Merged result: (symbol_id, combined_score, source_names).
pub type MergedResult = (String, f64, Vec<String>);

/// Merge multiple ranked lists using Reciprocal Rank Fusion.
///
/// Each source is a `(name, ranked_ids)` pair where `ranked_ids` is ordered
/// by relevance (index 0 = most relevant).
///
/// Returns `(id, score, sources)` sorted by score descending.
pub fn merge(sources: Vec<(&str, Vec<String>)>) -> Vec<MergedResult> {
    let mut entries: HashMap<String, HashMap<String, usize>> = HashMap::new();

    for (source_name, ids) in &sources {
        for (rank, id) in ids.iter().enumerate() {
            entries
                .entry(id.clone())
                .or_default()
                .insert(source_name.to_string(), rank);
        }
    }

    let mut results: Vec<MergedResult> = entries
        .into_iter()
        .map(|(id, source_ranks)| {
            let score: f64 = source_ranks.values().map(|&rank| rrf_score(rank)).sum();
            let source_names: Vec<String> = source_ranks.into_keys().collect();
            (id, score, source_names)
        })
        .collect();

    results.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    results
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rrf_score_rank_zero() {
        let score = rrf_score(0);
        assert!((score - 1.0 / 60.0).abs() < 1e-10);
    }

    #[test]
    fn test_rrf_score_rank_ten() {
        let score = rrf_score(10);
        assert!((score - 1.0 / 70.0).abs() < 1e-10);
    }

    #[test]
    fn test_merge_single_source() {
        let results = merge(vec![("fts", vec!["a".into(), "b".into(), "c".into()])]);
        assert_eq!(results.len(), 3);
        // First result should have highest score
        assert_eq!(results[0].0, "a");
        assert!(results[0].1 > results[1].1);
        assert!(results[1].1 > results[2].1);
    }

    #[test]
    fn test_merge_two_sources_same_ids() {
        let results = merge(vec![
            ("fts", vec!["a".into(), "b".into()]),
            ("graph", vec!["a".into(), "c".into()]),
        ]);
        // "a" appears in both sources at rank 0, so it gets 2 * rrf_score(0)
        let a_result = results.iter().find(|r| r.0 == "a").unwrap();
        let expected = 2.0 * rrf_score(0);
        assert!((a_result.1 - expected).abs() < 1e-10);
        assert_eq!(a_result.2.len(), 2);
        // "a" should be ranked first
        assert_eq!(results[0].0, "a");
    }

    #[test]
    fn test_merge_two_sources_disjoint() {
        let results = merge(vec![("fts", vec!["a".into()]), ("graph", vec!["b".into()])]);
        assert_eq!(results.len(), 2);
        // Both have same score (rank 0 in their respective source)
        assert!((results[0].1 - results[1].1).abs() < 1e-10);
        // Each should have exactly 1 source
        assert_eq!(results[0].2.len(), 1);
        assert_eq!(results[1].2.len(), 1);
    }

    #[test]
    fn test_merge_empty_sources() {
        let results = merge(vec![]);
        assert!(results.is_empty());
    }

    #[test]
    fn test_merge_empty_id_lists() {
        let results = merge(vec![("fts", vec![]), ("graph", vec![])]);
        assert!(results.is_empty());
    }
}
