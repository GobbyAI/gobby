mod embedding;
mod lifecycle;
mod qdrant;
mod repository;
mod search;
mod types;

pub use embedding::{EmbeddingSource, embedding_source_from_context, probe_embedding_dim};
pub use lifecycle::CodeSymbolVectorLifecycle;
pub use qdrant::{
    VectorOrphanCleanup, cleanup_orphan_file_vectors, collection_name, delete_symbol_vectors,
};
#[cfg(test)]
pub use qdrant::{
    delete_code_symbol_collections_with_prefix, delete_project_collection,
    list_code_symbol_collections,
};
pub use repository::{fetch_symbols_for_file, fetch_symbols_for_project};
pub use search::semantic_search;
pub use types::{
    CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleOutput, CodeSymbolVectorPayload,
    CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest, VectorLifecycleError,
};

#[cfg(test)]
use embedding::{embed_text, embed_text_batch, embedding_client, vector_text_for_symbol};
#[cfg(test)]
pub(crate) use qdrant::VECTOR_DISTANCE_COSINE;
#[cfg(test)]
use qdrant::delete_file_vectors;
#[cfg(test)]
use search::{SearchError, search_code_symbols};

#[cfg(test)]
mod tests;
