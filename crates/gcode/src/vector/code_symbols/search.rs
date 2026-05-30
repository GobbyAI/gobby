use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};

use super::embedding::embed_query;
use super::qdrant::{collection_name, vector_search};
use super::types::{CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest};

pub fn search_code_symbols(
    ctx: &Context,
    request: &CodeSymbolVectorSearchRequest,
) -> Vec<CodeSymbolVectorSearchHit> {
    let qdrant_config = match &ctx.qdrant {
        Some(c) => c,
        None => return vec![],
    };

    let embedding_config = match &ctx.embedding {
        Some(c) => c,
        None => return vec![],
    };

    let embedding = match embed_query(embedding_config, &request.query) {
        Some(e) => e,
        None => return vec![],
    };

    let collection = collection_name(&request.collection_prefix, &request.project_id);
    vector_search(qdrant_config, &collection, &embedding, request.limit)
        .unwrap_or_default()
        .into_iter()
        .map(|(symbol_id, score)| CodeSymbolVectorSearchHit { symbol_id, score })
        .collect()
}

pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    if ctx.qdrant.is_none() {
        return vec![];
    }

    let request = CodeSymbolVectorSearchRequest {
        project_id: ctx.project_id.clone(),
        query: query.to_string(),
        limit,
        collection_prefix: CODE_SYMBOL_COLLECTION_PREFIX.to_string(),
    };

    search_code_symbols(ctx, &request)
        .into_iter()
        .map(|hit| (hit.symbol_id, hit.score))
        .collect()
}
