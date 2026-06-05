use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};

use super::embedding::{embed_query_with_source, embedding_source_from_context};
use super::qdrant::{collection_name, vector_search};
use super::types::{CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SearchError {
    MissingQdrantConfig,
    MissingEmbeddingConfig,
    QueryEmbeddingFailed,
    VectorSearch(String),
}

impl std::fmt::Display for SearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MissingQdrantConfig => write!(f, "Qdrant config is missing"),
            Self::MissingEmbeddingConfig => write!(f, "embedding config is missing"),
            Self::QueryEmbeddingFailed => write!(f, "query embedding failed"),
            Self::VectorSearch(error) => write!(f, "semantic vector search failed: {error}"),
        }
    }
}

impl std::error::Error for SearchError {}

pub fn search_code_symbols(
    ctx: &Context,
    request: &CodeSymbolVectorSearchRequest,
) -> Result<Vec<CodeSymbolVectorSearchHit>, SearchError> {
    let qdrant_config = match &ctx.qdrant {
        Some(config) => config,
        None => return Err(SearchError::MissingQdrantConfig),
    };

    let embedding_source = match embedding_source_from_context(ctx) {
        Some(source) => source,
        None => return Err(SearchError::MissingEmbeddingConfig),
    };

    let embedding = match embed_query_with_source(&embedding_source, &request.query) {
        Some(embedding) => embedding,
        None => return Err(SearchError::QueryEmbeddingFailed),
    };

    let collection = collection_name(&request.collection_prefix, &request.project_id);
    match vector_search(qdrant_config, &collection, &embedding, request.limit) {
        Ok(hits) => Ok(hits
            .into_iter()
            .map(|(symbol_id, score)| CodeSymbolVectorSearchHit { symbol_id, score })
            .collect()),
        Err(error) => Err(SearchError::VectorSearch(error.to_string())),
    }
}

/// Semantic search is a full-stack ranking signal. Returning an empty result on
/// transport/config errors lets degraded hybrid-search callers keep lexical and
/// graph sources instead of failing the whole user query.
pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    let request = CodeSymbolVectorSearchRequest {
        project_id: ctx.project_id.clone(),
        query: query.to_string(),
        limit,
        collection_prefix: CODE_SYMBOL_COLLECTION_PREFIX.to_string(),
    };

    match search_code_symbols(ctx, &request) {
        Ok(hits) => hits
            .into_iter()
            .map(|hit| (hit.symbol_id, hit.score))
            .collect(),
        Err(error) => {
            log::warn!("semantic vector search skipped: {error}");
            Vec::new()
        }
    }
}
