//! Compatibility wrapper for Qdrant vector search.
//!
//! Reusable vector projection behavior lives in `crate::vector::code_symbols`.

pub use crate::vector::code_symbols::{embed_query, vector_search};

use crate::config::Context;

pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    crate::vector::code_symbols::semantic_search(ctx, query, limit)
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
            daemon_url: None,
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
                collection_prefix: "code_symbols_".to_string(),
            }),
            ..make_ctx_no_qdrant()
        };
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }
}
