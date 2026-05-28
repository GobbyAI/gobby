//! Reciprocal Rank Fusion: merges ranked result lists from multiple sources.
//!
//! score(rank) = 1.0 / (K + rank) where K = 60.
//! Ports logic from src/gobby/code_index/searcher.py.

/// Merged result: (symbol_id, combined_score, source_names).
pub type MergedResult = (String, f64, Vec<String>);

/// Merge multiple ranked lists using Reciprocal Rank Fusion.
///
/// Each source is a `(name, ranked_ids)` pair where `ranked_ids` is ordered
/// by relevance (index 0 = most relevant).
///
/// Returns `(id, score, sources)` sorted by score descending.
pub fn merge(sources: Vec<(&str, Vec<String>)>) -> Vec<MergedResult> {
    gobby_core::search::rrf_merge(sources)
        .into_iter()
        .map(|result| (result.id, result.score, result.sources))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

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
        // "a" appears in both sources at rank 0, so it gets two rank-zero contributions.
        let a_result = results.iter().find(|r| r.0 == "a").unwrap();
        let expected = 2.0 * (1.0 / 60.0);
        assert!((a_result.1 - expected).abs() < 1e-10);
        assert_eq!(a_result.2.len(), 2);
        // "a" should be ranked first
        assert_eq!(results[0].0, "a");
    }

    #[test]
    fn test_merge_sorts_sources_deterministically() {
        let results = merge(vec![
            ("semantic", vec!["b".into(), "a".into()]),
            ("fts", vec!["b".into()]),
        ]);

        assert_eq!(results[0].0, "b");
        assert_eq!(
            results[0].2,
            vec!["fts".to_string(), "semantic".to_string()]
        );
        assert_eq!(results[1].0, "a");
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

    #[test]
    fn merge_delegates_to_gobby_core_rrf() {
        let sources = vec![
            (
                "fts",
                vec!["a".to_string(), "a".to_string(), "b".to_string()],
            ),
            ("semantic", vec!["b".to_string()]),
        ];
        let results = merge(sources.clone());
        let expected = gobby_core::search::rrf_merge(sources);

        assert_eq!(results.len(), expected.len());
        for (actual, expected) in results.iter().zip(expected.iter()) {
            assert_eq!(actual.0, expected.id);
            assert!((actual.1 - expected.score).abs() < 1e-10);
            assert_eq!(actual.2, expected.sources);
        }

        let source = include_str!("rrf.rs");
        let delegate = ["gobby_core", "::search::rrf_merge"].concat();
        let local_const = ["const ", "RRF_K"].concat();
        assert!(source.contains(&delegate));
        assert!(!source.contains(&local_const));
    }
}
