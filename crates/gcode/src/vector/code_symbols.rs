mod embedding;
mod lifecycle;
mod qdrant;
mod repository;
mod search;
mod types;
mod vector_text;

pub use embedding::{EmbeddingSource, embedding_source_from_context, probe_embedding_dim};
pub use lifecycle::{CodeSymbolVectorLifecycle, RebuiltSymbol};
pub(crate) use qdrant::count_symbol_vectors;
pub use qdrant::{PointIdentity, collection_name, delete_symbol_vectors, scroll_point_identities};
#[cfg(test)]
pub use qdrant::{
    delete_code_symbol_collections_with_prefix, delete_project_collection,
    list_code_symbol_collections,
};
pub use repository::{
    fetch_symbols_for_file, fetch_symbols_for_file_version, fetch_symbols_for_project,
};
pub use search::semantic_search;
#[cfg(test)]
pub(crate) use search::semantic_search_with_source;
pub(crate) use search::{audited_semantic_search, has_pending_pinned_projection};
pub use types::{
    CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleOutput, CodeSymbolVectorPayload,
    CodeSymbolVectorSearchHit, CodeSymbolVectorSearchRequest, VectorLifecycleError,
};

#[cfg(all(test, feature = "ai"))]
use embedding::{embed_text, embed_text_batch, embedding_client};
#[cfg(all(test, feature = "ai"))]
pub(crate) use qdrant::VECTOR_DISTANCE_COSINE;
#[cfg(test)]
use search::{SearchError, search_code_symbols};
pub(crate) use vector_text::vector_input_fingerprint;
#[cfg(test)]
pub(crate) use vector_text::vector_text_for_symbol;

#[cfg(test)]
pub(crate) mod tests;
