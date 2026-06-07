use std::collections::BTreeMap;

use crate::search::{SearchSource, SearchSourceExplanation, WikiSearchResponse, WikiSearchResult};
use gobby_core::degradation::DegradationKind;

pub fn fuse_sources(
    bm25_hits: Vec<WikiSearchResult>,
    semantic_hits: Vec<WikiSearchResult>,
    graph_hits: Vec<WikiSearchResult>,
    degradations: Vec<DegradationKind>,
    limit: usize,
) -> WikiSearchResponse {
    if limit == 0 {
        return WikiSearchResponse {
            results: Vec::new(),
            degradations,
        };
    }

    let bm25_keys = ranked_keys(&bm25_hits);
    let semantic_keys = ranked_keys(&semantic_hits);
    let graph_keys = ranked_keys(&graph_hits);

    let mut by_key = BTreeMap::new();
    for hit in bm25_hits.into_iter().chain(semantic_hits).chain(graph_hits) {
        let key = hit.fusion_key();
        by_key
            .entry(key)
            .and_modify(|existing| merge_hit_metadata(existing, &hit))
            .or_insert(hit);
    }

    let mut sources = Vec::new();
    if !bm25_keys.is_empty() {
        sources.push((SearchSource::Bm25.as_str(), bm25_keys));
    }
    if !graph_keys.is_empty() {
        sources.push((SearchSource::Graph.as_str(), graph_keys));
    }
    if !semantic_keys.is_empty() {
        sources.push((SearchSource::Semantic.as_str(), semantic_keys));
    }

    let mut results = gobby_core::search::rrf_merge(sources)
        .into_iter()
        .filter_map(|fused| {
            let Some(mut result) = by_key.remove(&fused.id) else {
                log::warn!("RRF returned key absent from source hit map: {}", fused.id);
                return None;
            };
            result.score = fused.score;
            result.sources = fused
                .sources
                .iter()
                .filter_map(|source| {
                    SearchSource::from_source_name(source).or_else(|| {
                        log::warn!("RRF returned unknown source name: {source}");
                        None
                    })
                })
                .collect();
            result.explanations = fused
                .explanations
                .iter()
                .filter_map(|explanation| {
                    let source =
                        SearchSource::from_source_name(&explanation.source).or_else(|| {
                            log::warn!(
                                "RRF returned unknown explanation source: {}",
                                explanation.source
                            );
                            None
                        })?;
                    Some(SearchSourceExplanation {
                        source,
                        rank: explanation.rank,
                        score: explanation.score,
                    })
                })
                .collect();
            Some(result)
        })
        .collect::<Vec<_>>();
    results.truncate(limit);

    WikiSearchResponse {
        results,
        degradations,
    }
}

fn ranked_keys(hits: &[WikiSearchResult]) -> Vec<String> {
    hits.iter().map(WikiSearchResult::fusion_key).collect()
}

fn merge_hit_metadata(existing: &mut WikiSearchResult, hit: &WikiSearchResult) {
    if existing.title.is_none() {
        existing.title.clone_from(&hit.title);
    }
    if existing.snippet.is_empty() && !hit.snippet.is_empty() {
        existing.snippet.clone_from(&hit.snippet);
    }
    if existing.chunk.is_none() {
        existing.chunk.clone_from(&hit.chunk);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search::{
        ChunkProvenance, SearchHitKind, SearchProvenance, SearchScope, SearchSource,
        SearchSourceExplanation,
    };

    #[test]
    fn fusion_preserves_sources() {
        let response = fuse_sources(
            vec![
                search_result("document:wiki/topics/rust.md", SearchSource::Bm25),
                search_result("document:wiki/topics/borrow.md", SearchSource::Bm25),
            ],
            vec![search_result(
                "document:wiki/topics/rust.md",
                SearchSource::Semantic,
            )],
            vec![search_result(
                "document:wiki/topics/rust.md",
                SearchSource::Graph,
            )],
            vec![DegradationKind::PartialSearch {
                available: vec!["bm25".to_string(), "graph".to_string()],
                unavailable: vec!["semantic".to_string()],
            }],
            10,
        );

        let fused = response
            .results
            .iter()
            .find(|result| result.id == "document:wiki/topics/rust.md")
            .expect("shared document is fused");

        assert_eq!(
            fused.sources,
            vec![
                SearchSource::Bm25,
                SearchSource::Graph,
                SearchSource::Semantic
            ]
        );
        assert_eq!(
            fused
                .explanations
                .iter()
                .map(|explanation| explanation.source)
                .collect::<Vec<_>>(),
            vec![
                SearchSource::Bm25,
                SearchSource::Graph,
                SearchSource::Semantic
            ]
        );
        assert!(
            response.degradations.iter().any(|degradation| {
                matches!(
                    degradation,
                    DegradationKind::PartialSearch {
                        available,
                        unavailable
                    } if available.as_slice() == ["bm25", "graph"]
                        && unavailable.as_slice() == ["semantic"]
                )
            }),
            "fusion should preserve degradation metadata"
        );
    }

    #[test]
    fn fusion_uses_canonical_page_key() {
        let response = fuse_sources(
            vec![search_result(
                "chunk:wiki/topics/rust.md:0",
                SearchSource::Bm25,
            )],
            vec![search_result("point:semantic-rust", SearchSource::Semantic)],
            vec![search_result(
                "document:wiki/topics/rust.md",
                SearchSource::Graph,
            )],
            Vec::new(),
            10,
        );

        assert_eq!(response.results.len(), 1);
        let fused = &response.results[0];
        assert_eq!(fused.fusion_key(), "project:project-1:wiki/topics/rust.md");
        assert_eq!(
            fused.sources,
            vec![
                SearchSource::Bm25,
                SearchSource::Graph,
                SearchSource::Semantic
            ]
        );
        assert_eq!(
            fused
                .explanations
                .iter()
                .map(|explanation| explanation.source)
                .collect::<Vec<_>>(),
            vec![
                SearchSource::Bm25,
                SearchSource::Graph,
                SearchSource::Semantic
            ]
        );
    }

    fn search_result(id: &str, source: SearchSource) -> WikiSearchResult {
        WikiSearchResult {
            id: id.to_string(),
            title: Some("Rust".to_string()),
            scope: SearchScope::project("project-1"),
            path: "wiki/topics/rust.md".into(),
            source_path: "raw/INDEX.md".into(),
            hit_kind: SearchHitKind::Document,
            snippet: "Ownership and borrowing".to_string(),
            score: 1.0,
            sources: vec![source],
            explanations: Vec::<SearchSourceExplanation>::new(),
            chunk: Some(ChunkProvenance {
                chunk_index: 0,
                byte_start: 0,
                byte_end: 24,
                heading: Some("Rust".to_string()),
            }),
            provenance: SearchProvenance {
                document_path: "wiki/topics/rust.md".into(),
                source_path: "raw/INDEX.md".into(),
                source_kind: "topic".to_string(),
                content_hash: Some("hash".to_string()),
            },
        }
    }
}
