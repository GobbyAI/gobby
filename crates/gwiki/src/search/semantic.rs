use std::path::PathBuf;
#[cfg(feature = "embeddings-http")]
use std::time::Duration;

#[cfg(feature = "ai")]
use gobby_core::ai::daemon;
#[cfg(feature = "ai")]
use gobby_core::ai_context::AiContext;
use gobby_core::config::{EmbeddingConfig, QdrantConfig};
use gobby_core::degradation::DegradationKind;
use gobby_core::qdrant::{
    CollectionScope, SearchHit, SearchRequest, collection_name, legacy_collection_name,
    resolve_collection_name,
};
use serde_json::{Map, Value, json};

use crate::search::{
    ChunkProvenance, SearchError, SearchHitKind, SearchProvenance, SearchScope, SearchSource,
    WikiSearchResult,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SemanticSearchRequest {
    pub query: String,
    pub scope: SearchScope,
    pub limit: usize,
}

#[derive(Debug, Clone)]
pub struct SemanticSearchOutcome {
    pub hits: Vec<WikiSearchResult>,
    pub degradation: Option<DegradationKind>,
}

pub trait SemanticSearchBackend {
    fn search_semantic(
        &mut self,
        request: SemanticSearchRequest,
    ) -> Result<SemanticSearchOutcome, SearchError>;
}

pub trait QueryEmbedder {
    fn embed_query(
        &mut self,
        embedding: &SemanticEmbedding,
        query: &str,
    ) -> Result<Vec<f32>, SearchError>;

    fn embed_queries(
        &mut self,
        embedding: &SemanticEmbedding,
        queries: &[String],
    ) -> Result<Vec<Vec<f32>>, SearchError> {
        queries
            .iter()
            .map(|query| self.embed_query(embedding, query))
            .collect()
    }
}

#[derive(Debug, Clone)]
pub enum SemanticEmbedding {
    Direct(EmbeddingConfig),
    #[cfg(feature = "ai")]
    Daemon(Box<AiContext>),
}

pub trait VectorSearchBackend {
    fn resolve_collection(
        &mut self,
        _config: &QdrantConfig,
        scope: &SearchScope,
    ) -> anyhow::Result<String> {
        Ok(collection_for_scope(scope))
    }

    fn search(
        &mut self,
        config: &QdrantConfig,
        collection: &str,
        request: SearchRequest,
    ) -> anyhow::Result<Vec<SearchHit>>;
}

pub fn search_semantic<E, V>(
    request: SemanticSearchRequest,
    embedding: Option<&SemanticEmbedding>,
    qdrant: Option<&QdrantConfig>,
    embedder: &mut E,
    vector_backend: &mut V,
) -> Result<SemanticSearchOutcome, SearchError>
where
    E: QueryEmbedder,
    V: VectorSearchBackend,
{
    if request.query.trim().is_empty() || request.limit == 0 {
        return Ok(SemanticSearchOutcome {
            hits: Vec::new(),
            degradation: None,
        });
    }

    let Some(embedding) = embedding else {
        return Err(required_service_error(
            "embeddings",
            "embedding endpoint is not configured",
        ));
    };
    let Some(qdrant) = qdrant else {
        return Err(required_service_error("qdrant", "Qdrant is not configured"));
    };
    if qdrant.url.as_deref().is_none_or(str::is_empty) {
        return Err(required_service_error(
            "qdrant",
            "Qdrant URL is not configured",
        ));
    }

    let embedding_query = match embedding {
        SemanticEmbedding::Direct(config) => semantic_embedding_query(config, &request.query),
        #[cfg(feature = "ai")]
        SemanticEmbedding::Daemon(_) => request.query.clone(),
    };
    let vector = match embedder.embed_query(embedding, &embedding_query) {
        Ok(vector) if !vector.is_empty() => vector,
        Ok(_) => {
            return Ok(degraded(
                "embeddings",
                gobby_core::degradation::ServiceState::Unreachable {
                    message: "embedding endpoint returned an empty vector".to_string(),
                },
            ));
        }
        Err(error) => {
            return Ok(degraded(
                "embeddings",
                gobby_core::degradation::ServiceState::Unreachable {
                    message: error.to_string(),
                },
            ));
        }
    };

    let preferred_collection = collection_for_scope(&request.scope);
    let collection = match vector_backend.resolve_collection(qdrant, &request.scope) {
        Ok(collection) => collection,
        Err(error) => {
            return Ok(qdrant_degradation(error));
        }
    };
    if collection != preferred_collection {
        log::warn!(
            "gwiki semantic search used deprecated legacy Qdrant collection `{collection}`; reindex to use `{preferred_collection}`"
        );
    }
    let filter = payload_filter(&request.scope);
    let qdrant_request = SearchRequest {
        vector,
        limit: request.limit,
        filter: Some(filter),
    };
    let hits = match search_collection_with_legacy_fallback(
        vector_backend,
        qdrant,
        &request.scope,
        &collection,
        qdrant_request,
    ) {
        Ok(hits) => hits,
        Err(error) => {
            return Ok(qdrant_degradation(error));
        }
    };

    let hits = hits
        .into_iter()
        .filter(|hit| payload_matches_scope(&hit.payload, &request.scope))
        .filter_map(|hit| hit_to_result(hit, &request.scope))
        .take(request.limit)
        .collect();

    Ok(SemanticSearchOutcome {
        hits,
        degradation: None,
    })
}

fn semantic_embedding_query(config: &EmbeddingConfig, query: &str) -> String {
    match config.query_prefix.as_deref() {
        Some(prefix) if !prefix.trim().is_empty() => format!("{prefix}{query}"),
        _ => query.to_string(),
    }
}

pub fn collection_for_scope(scope: &SearchScope) -> String {
    collection_name("gwiki", qdrant_collection_scope(scope))
}

fn search_collection_with_legacy_fallback<V>(
    vector_backend: &mut V,
    qdrant: &QdrantConfig,
    scope: &SearchScope,
    collection: &str,
    request: SearchRequest,
) -> anyhow::Result<Vec<SearchHit>>
where
    V: VectorSearchBackend,
{
    let retry_request = SearchRequest {
        vector: request.vector.clone(),
        limit: request.limit,
        filter: request.filter.clone(),
    };
    match vector_backend.search(qdrant, collection, request) {
        Ok(hits) => Ok(hits),
        Err(error) if is_collection_not_found(&error, collection) => {
            let legacy_collection = legacy_collection_for_scope(scope);
            let hits = vector_backend.search(qdrant, &legacy_collection, retry_request)?;
            log::warn!(
                "gwiki semantic search used deprecated legacy Qdrant collection `{legacy_collection}`; reindex to use `{collection}`"
            );
            Ok(hits)
        }
        Err(error) => Err(error),
    }
}

fn legacy_collection_for_scope(scope: &SearchScope) -> String {
    legacy_collection_name("gwiki", qdrant_collection_scope(scope))
}

fn qdrant_collection_scope(scope: &SearchScope) -> CollectionScope<'_> {
    match scope {
        SearchScope::Project { project_id } => CollectionScope::Project(project_id),
        SearchScope::Topic { topic } => CollectionScope::Topic(topic),
    }
}

fn is_collection_not_found(error: &anyhow::Error, collection: &str) -> bool {
    matches!(
        error.downcast_ref::<gobby_core::qdrant::QdrantError>(),
        Some(gobby_core::qdrant::QdrantError::HttpStatus {
            status,
            collection: Some(error_collection),
            ..
        }) if status.as_u16() == 404 && error_collection == collection
    )
}

pub fn payload_filter(scope: &SearchScope) -> Value {
    let scoped_key = match scope {
        SearchScope::Project { .. } => "project_id",
        SearchScope::Topic { .. } => "topic",
    };

    json!({
        "must": [
            {"key": "namespace", "match": {"value": "gwiki"}},
            {"key": "scope_kind", "match": {"value": scope.scope_kind()}},
            {"key": scoped_key, "match": {"value": scope.scope_value()}}
        ]
    })
}

pub struct GobbySemanticBackend<E, V> {
    embedding: Option<SemanticEmbedding>,
    qdrant: Option<QdrantConfig>,
    embedder: E,
    vector_backend: V,
}

impl<E, V> GobbySemanticBackend<E, V> {
    pub fn new(
        embedding: Option<SemanticEmbedding>,
        qdrant: Option<QdrantConfig>,
        embedder: E,
        vector_backend: V,
    ) -> Self {
        Self {
            embedding,
            qdrant,
            embedder,
            vector_backend,
        }
    }
}

impl<E, V> SemanticSearchBackend for GobbySemanticBackend<E, V>
where
    E: QueryEmbedder,
    V: VectorSearchBackend,
{
    fn search_semantic(
        &mut self,
        request: SemanticSearchRequest,
    ) -> Result<SemanticSearchOutcome, SearchError> {
        search_semantic(
            request,
            self.embedding.as_ref(),
            self.qdrant.as_ref(),
            &mut self.embedder,
            &mut self.vector_backend,
        )
    }
}

#[cfg(feature = "embeddings-http")]
#[derive(Debug, Clone)]
pub struct OpenAiEmbeddingBackend {
    client: reqwest::blocking::Client,
}

#[cfg(feature = "embeddings-http")]
impl Default for OpenAiEmbeddingBackend {
    fn default() -> Self {
        Self {
            client: reqwest::blocking::Client::new(),
        }
    }
}

#[cfg(feature = "embeddings-http")]
impl OpenAiEmbeddingBackend {
    pub fn new() -> Self {
        Self::default()
    }
}

#[cfg(feature = "embeddings-http")]
impl QueryEmbedder for OpenAiEmbeddingBackend {
    fn embed_query(
        &mut self,
        embedding: &SemanticEmbedding,
        query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        match embedding {
            SemanticEmbedding::Direct(config) => {
                let mut embeddings =
                    embed_direct_queries(&self.client, config, &[query.to_string()])?;
                embeddings.pop().ok_or_else(|| {
                    SearchError::Backend("missing data[0].embedding array".to_string())
                })
            }
            #[cfg(feature = "ai")]
            SemanticEmbedding::Daemon(context) => embed_daemon_query(context, query),
        }
    }

    fn embed_queries(
        &mut self,
        embedding: &SemanticEmbedding,
        queries: &[String],
    ) -> Result<Vec<Vec<f32>>, SearchError> {
        match embedding {
            SemanticEmbedding::Direct(config) => {
                embed_direct_queries(&self.client, config, queries)
            }
            #[cfg(feature = "ai")]
            SemanticEmbedding::Daemon(_) => queries
                .iter()
                .map(|query| self.embed_query(embedding, query))
                .collect(),
        }
    }
}

#[cfg(feature = "embeddings-http")]
fn embed_direct_queries(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    queries: &[String],
) -> Result<Vec<Vec<f32>>, SearchError> {
    if queries.is_empty() {
        return Ok(Vec::new());
    }
    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut request = client
        .post(url)
        .timeout(Duration::from_secs(config.timeout_seconds))
        .json(&json!({
            "model": config.model,
            "input": queries,
        }));
    if let Some(api_key) = &config.api_key {
        request = request.bearer_auth(api_key);
    }

    let response = request
        .send()
        .map_err(|error| SearchError::Backend(error.to_string()))?;
    let status = response.status();
    if !status.is_success() {
        let body = response
            .text()
            .unwrap_or_else(|error| format!("<failed to read response body: {error}>"));
        return Err(SearchError::Backend(format!(
            "embedding request failed: HTTP {status}: {body}"
        )));
    }

    let data = response
        .json::<Value>()
        .map_err(|error| SearchError::Backend(error.to_string()))?;
    let embeddings = data
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| SearchError::Backend("embedding API returned empty data array".to_string()))?
        .iter()
        .enumerate()
        .map(|(index, item)| {
            item.get("embedding")
                .and_then(Value::as_array)
                .ok_or_else(|| {
                    SearchError::Backend(format!("missing data[{index}].embedding array"))
                })?
                .iter()
                .map(|value| {
                    value.as_f64().map(|value| value as f32).ok_or_else(|| {
                        SearchError::Backend(format!("data[{index}].embedding has non-number"))
                    })
                })
                .collect()
        })
        .collect::<Result<Vec<Vec<f32>>, SearchError>>()?;
    Ok(embeddings)
}

#[cfg(not(feature = "embeddings-http"))]
#[derive(Debug, Clone, Copy, Default)]
pub struct OpenAiEmbeddingBackend;

#[cfg(not(feature = "embeddings-http"))]
impl OpenAiEmbeddingBackend {
    pub fn new() -> Self {
        Self
    }
}

#[cfg(all(not(feature = "embeddings-http"), feature = "ai"))]
impl QueryEmbedder for OpenAiEmbeddingBackend {
    fn embed_query(
        &mut self,
        embedding: &SemanticEmbedding,
        query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        match embedding {
            SemanticEmbedding::Direct(_) => Err(SearchError::Backend(
                "semantic HTTP backend unavailable; build with the `embeddings-http` feature"
                    .to_string(),
            )),
            SemanticEmbedding::Daemon(context) => embed_daemon_query(context, query),
        }
    }
}

#[cfg(all(not(feature = "embeddings-http"), not(feature = "ai")))]
impl QueryEmbedder for OpenAiEmbeddingBackend {
    fn embed_query(
        &mut self,
        _embedding: &SemanticEmbedding,
        _query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        Err(SearchError::Backend(
            "semantic HTTP backend unavailable; build with the `embeddings-http` feature"
                .to_string(),
        ))
    }
}

#[cfg(feature = "ai")]
fn embed_daemon_query(context: &AiContext, query: &str) -> Result<Vec<f32>, SearchError> {
    let input = vec![query.to_string()];
    daemon::embed_via_daemon(context, &input, true)
        .map_err(|error| SearchError::Backend(error.to_string()))?
        .embeddings
        .into_iter()
        .next()
        .ok_or_else(|| SearchError::Backend("daemon embedding response was empty".to_string()))
}

#[derive(Debug, Default, Clone, Copy)]
pub struct GobbyQdrantBackend;

impl VectorSearchBackend for GobbyQdrantBackend {
    fn resolve_collection(
        &mut self,
        config: &QdrantConfig,
        scope: &SearchScope,
    ) -> anyhow::Result<String> {
        resolve_collection_name(config, "gwiki", qdrant_collection_scope(scope))
    }

    fn search(
        &mut self,
        config: &QdrantConfig,
        collection: &str,
        request: SearchRequest,
    ) -> anyhow::Result<Vec<SearchHit>> {
        gobby_core::qdrant::search(config, collection, request)
    }
}

fn required_service_error(service: &str, detail: &str) -> SearchError {
    SearchError::Backend(format!(
        "semantic search requires {service}; {detail}. Run `gwiki setup --standalone` or attach to Gobby's full datastore stack"
    ))
}

fn payload_matches_scope(payload: &Map<String, Value>, scope: &SearchScope) -> bool {
    payload_string(payload, "namespace").as_deref() == Some("gwiki")
        && payload_string(payload, "scope_kind").as_deref() == Some(scope.scope_kind())
        && match scope {
            SearchScope::Project { project_id } => {
                payload_string(payload, "project_id").as_deref() == Some(project_id.as_str())
            }
            SearchScope::Topic { topic } => {
                payload_string(payload, "topic").as_deref() == Some(topic.as_str())
            }
        }
}

fn hit_to_result(hit: SearchHit, scope: &SearchScope) -> Option<WikiSearchResult> {
    let path = payload_string(&hit.payload, "path")?;
    let source_path = payload_string(&hit.payload, "source_path").unwrap_or_else(|| path.clone());
    let path_buf = PathBuf::from(path);
    let source_path_buf = PathBuf::from(source_path);
    let chunk_index = payload_usize(&hit.payload, "chunk_index");
    let byte_start = payload_usize(&hit.payload, "byte_start");
    let byte_end = payload_usize(&hit.payload, "byte_end");
    let chunk = match (chunk_index, byte_start, byte_end) {
        (Some(chunk_index), Some(byte_start), Some(byte_end)) => Some(ChunkProvenance {
            chunk_index,
            byte_start,
            byte_end,
            heading: payload_string(&hit.payload, "heading"),
        }),
        _ => None,
    };

    Some(WikiSearchResult {
        id: hit.id,
        title: payload_string(&hit.payload, "title"),
        scope: scope.clone(),
        path: path_buf.clone(),
        source_path: source_path_buf.clone(),
        hit_kind: if chunk.is_some() {
            SearchHitKind::Chunk
        } else {
            SearchHitKind::Document
        },
        snippet: payload_string(&hit.payload, "snippet")
            .or_else(|| payload_string(&hit.payload, "content"))
            .unwrap_or_default(),
        score: f64::from(hit.score),
        sources: vec![SearchSource::Semantic],
        explanations: Vec::new(),
        chunk,
        provenance: SearchProvenance {
            document_path: path_buf,
            source_path: source_path_buf,
            source_kind: payload_string(&hit.payload, "source_kind")
                .unwrap_or_else(|| "unknown".to_string()),
            content_hash: payload_string(&hit.payload, "content_hash"),
        },
    })
}

fn payload_string(payload: &Map<String, Value>, key: &str) -> Option<String> {
    payload.get(key).and_then(Value::as_str).map(str::to_string)
}

fn payload_usize(payload: &Map<String, Value>, key: &str) -> Option<usize> {
    payload
        .get(key)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
}

fn degraded(service: &str, state: gobby_core::degradation::ServiceState) -> SemanticSearchOutcome {
    SemanticSearchOutcome {
        hits: Vec::new(),
        degradation: Some(DegradationKind::ServiceUnavailable {
            service: service.to_string(),
            state,
        }),
    }
}

fn qdrant_degradation(error: anyhow::Error) -> SemanticSearchOutcome {
    let message = error.to_string();
    if let Some(gobby_core::qdrant::QdrantError::HttpStatus { status, .. }) =
        error.downcast_ref::<gobby_core::qdrant::QdrantError>()
    {
        return match status.as_u16() {
            404 => degraded(
                "qdrant_collection",
                gobby_core::degradation::ServiceState::NotConfigured,
            ),
            401 | 403 => degraded(
                "qdrant_auth",
                gobby_core::degradation::ServiceState::Unreachable { message },
            ),
            400..=499 => degraded(
                "qdrant_config",
                gobby_core::degradation::ServiceState::Unreachable { message },
            ),
            _ => degraded(
                "qdrant",
                gobby_core::degradation::ServiceState::Unreachable { message },
            ),
        };
    }

    degraded(
        "qdrant",
        gobby_core::degradation::ServiceState::Unreachable { message },
    )
}

#[cfg(test)]
pub struct UnavailableSemanticBackend;

#[cfg(test)]
impl SemanticSearchBackend for UnavailableSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: SemanticSearchRequest,
    ) -> Result<SemanticSearchOutcome, SearchError> {
        Ok(degraded(
            "qdrant",
            gobby_core::degradation::ServiceState::NotConfigured,
        ))
    }
}

#[cfg(test)]
struct FixedEmbedder {
    vector: Vec<f32>,
    queries: Vec<String>,
}

#[cfg(test)]
impl FixedEmbedder {
    fn new(vector: Vec<f32>) -> Self {
        Self {
            vector,
            queries: Vec::new(),
        }
    }
}

#[cfg(test)]
impl QueryEmbedder for FixedEmbedder {
    fn embed_query(
        &mut self,
        _embedding: &SemanticEmbedding,
        query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        self.queries.push(query.to_string());
        Ok(self.vector.clone())
    }
}

#[cfg(test)]
struct RecordingVectorBackend {
    hits: Vec<SearchHit>,
    collection: Option<String>,
    filter: Option<Value>,
}

#[cfg(test)]
impl RecordingVectorBackend {
    fn new(hits: Vec<SearchHit>) -> Self {
        Self {
            hits,
            collection: None,
            filter: None,
        }
    }
}

#[cfg(test)]
impl VectorSearchBackend for RecordingVectorBackend {
    fn search(
        &mut self,
        _config: &QdrantConfig,
        collection: &str,
        request: SearchRequest,
    ) -> anyhow::Result<Vec<SearchHit>> {
        self.collection = Some(collection.to_string());
        self.filter = request.filter;
        Ok(self.hits.clone())
    }
}

#[cfg(test)]
struct FailingEmbedder;

#[cfg(test)]
impl QueryEmbedder for FailingEmbedder {
    fn embed_query(
        &mut self,
        _embedding: &SemanticEmbedding,
        _query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        Err(SearchError::Backend("embedding timeout".to_string()))
    }
}

#[cfg(test)]
struct FailingVectorBackend;

#[cfg(test)]
impl VectorSearchBackend for FailingVectorBackend {
    fn search(
        &mut self,
        _config: &QdrantConfig,
        _collection: &str,
        _request: SearchRequest,
    ) -> anyhow::Result<Vec<SearchHit>> {
        Err(anyhow::anyhow!("qdrant timeout"))
    }
}

#[cfg(test)]
struct QdrantStatusVectorBackend {
    status: gobby_core::qdrant::StatusCode,
}

#[cfg(test)]
impl VectorSearchBackend for QdrantStatusVectorBackend {
    fn search(
        &mut self,
        _config: &QdrantConfig,
        collection: &str,
        _request: SearchRequest,
    ) -> anyhow::Result<Vec<SearchHit>> {
        Err(gobby_core::qdrant::QdrantError::HttpStatus {
            operation: "search",
            status: self.status,
            body: "qdrant failure".to_string(),
            collection: Some(collection.to_string()),
            request: Some(format!("/collections/{collection}/points/search")),
        }
        .into())
    }
}

#[cfg(test)]
#[path = "semantic_tests.rs"]
mod semantic_tests;
