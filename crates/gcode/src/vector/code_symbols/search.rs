use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};

use super::embedding::{embed_query_with_source, embedding_source_from_context};
use super::qdrant::{collection_name, vector_search};
use super::types::{CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest};

pub fn search_code_symbols(
    ctx: &Context,
    request: &CodeSymbolVectorSearchRequest,
) -> Vec<CodeSymbolVectorSearchHit> {
    let qdrant_config = match &ctx.qdrant {
        Some(c) => c,
        None => {
            eprintln!("gcode: semantic vector search skipped: Qdrant config is missing");
            return vec![];
        }
    };

    let embedding_source = match embedding_source_from_context(ctx) {
        Some(source) => source,
        None => {
            eprintln!("gcode: semantic vector search skipped: embedding config is missing");
            return vec![];
        }
    };

    let embedding = match embed_query_with_source(&embedding_source, &request.query) {
        Some(e) => e,
        None => {
            eprintln!("gcode: semantic vector search skipped: query embedding failed");
            return vec![];
        }
    };

    let collection = collection_name(&request.collection_prefix, &request.project_id);
    match vector_search(qdrant_config, &collection, &embedding, request.limit) {
        Ok(hits) => hits
            .into_iter()
            .map(|(symbol_id, score)| CodeSymbolVectorSearchHit { symbol_id, score })
            .collect(),
        Err(error) => {
            eprintln!("gcode: semantic vector search failed: {error}");
            Vec::new()
        }
    }
}

/// Semantic search is an optional ranking signal. Returning an empty result on
/// transport/config errors lets hybrid search degrade to lexical/graph sources
/// instead of failing the whole user query.
pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
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
