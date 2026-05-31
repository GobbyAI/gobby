mod embedding;
mod lifecycle;
mod qdrant;
mod repository;
mod search;
mod types;

pub use embedding::{
    embed_query, embed_text, embed_text_batch, embedding_client, vector_text_for_symbol,
};
pub use lifecycle::{CodeSymbolVectorLifecycle, lifecycle_status, resolve_lifecycle_qdrant_config};
pub use qdrant::{
    VECTOR_DISTANCE_COSINE, collection_name, delete_code_symbol_collections_with_prefix,
    delete_file_vectors, delete_project_collection, vector_search,
};
pub use repository::{fetch_symbols_for_file, fetch_symbols_for_project};
pub use search::{search_code_symbols, semantic_search};
pub use types::{
    CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleOutput,
    CodeSymbolVectorLifecycleStatus, CodeSymbolVectorPayload, CodeSymbolVectorSearchHit,
    CodeSymbolVectorSearchRequest, VectorCollectionSchema, VectorLifecycleError,
};

#[cfg(test)]
mod tests;
