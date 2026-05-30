//! Compatibility wrapper for Qdrant vector search.
//!
//! Reusable vector projection behavior lives in `crate::vector::code_symbols`.

pub use crate::vector::code_symbols::{embed_query, vector_search};

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::visibility;
use gobby_core::qdrant::{CollectionScope, SearchRequest};

pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    let Some(embedding_config) = ctx.embedding.as_ref() else {
        return vec![];
    };
    let Some(query_vector) = embed_query(embedding_config, query) else {
        return vec![];
    };

    let mut results = Vec::new();
    for project_id in visibility::visible_project_ids(ctx) {
        let collection = gobby_core::qdrant::collection_name(
            "gcode",
            CollectionScope::Custom(&format!("{CODE_SYMBOL_COLLECTION_PREFIX}{project_id}")),
        );
        let request = SearchRequest {
            vector: query_vector.clone(),
            limit,
            filter: None,
        };

        let Ok((hits, _state)) =
            gobby_core::qdrant::with_qdrant(ctx.qdrant.as_ref(), Vec::new(), |config| {
                gobby_core::qdrant::search(config, &collection, request)
            })
        else {
            continue;
        };

        results.extend(hits.into_iter().map(|hit| (hit.id, f64::from(hit.score))));
    }
    results.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    results.truncate(limit);
    results
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::QdrantConfig;
    use std::path::PathBuf;

    fn make_ctx_no_qdrant() -> Context {
        Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "test".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    #[test]
    fn test_semantic_search_no_qdrant() {
        let ctx = make_ctx_no_qdrant();
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }

    #[test]
    fn test_semantic_search_no_embedding_config() {
        let ctx = Context {
            qdrant: Some(QdrantConfig {
                url: Some("http://localhost:6333".to_string()),
                api_key: None,
            }),
            ..make_ctx_no_qdrant()
        };
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }
}
