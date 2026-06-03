use std::path::PathBuf;
#[cfg(feature = "embeddings-http")]
use std::time::Duration;

#[cfg(feature = "ai")]
use gobby_core::ai::daemon;
#[cfg(feature = "ai")]
use gobby_core::ai_context::AiContext;
use gobby_core::config::{EmbeddingConfig, QdrantConfig};
use gobby_core::degradation::{DegradationKind, ServiceState};
use gobby_core::qdrant::{CollectionScope, SearchHit, SearchRequest, collection_name};
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
}

#[derive(Debug, Clone)]
pub enum SemanticEmbedding {
    Direct(EmbeddingConfig),
    #[cfg(feature = "ai")]
    Daemon(Box<AiContext>),
}

pub trait VectorSearchBackend {
    fn search(
        &mut self,
        config: &QdrantConfig,
        collection: &str,
        request: SearchRequest,
    ) -> Result<Vec<SearchHit>, SearchError>;
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
        return Ok(degraded("embeddings", ServiceState::NotConfigured));
    };
    let Some(qdrant) = qdrant else {
        return Ok(degraded("qdrant", ServiceState::NotConfigured));
    };
    if qdrant.url.as_deref().is_none_or(str::is_empty) {
        return Ok(degraded("qdrant", ServiceState::NotConfigured));
    }

    let embedding_query = match embedding {
        SemanticEmbedding::Direct(config) => semantic_embedding_query(config, &request.query),
        #[cfg(feature = "ai")]
        SemanticEmbedding::Daemon(_) => request.query.clone(),
    };
    let vector = match embedder.embed_query(embedding, &embedding_query) {
        Ok(vector) if !vector.is_empty() => vector,
        Ok(_) => return Ok(degraded("embeddings", ServiceState::NotConfigured)),
        Err(error) => {
            return Ok(degraded(
                "embeddings",
                ServiceState::Unreachable {
                    message: error.to_string(),
                },
            ));
        }
    };

    let collection = collection_for_scope(&request.scope);
    let filter = payload_filter(&request.scope);
    let qdrant_request = SearchRequest {
        vector,
        limit: request.limit,
        filter: Some(filter),
    };
    let hits = match vector_backend.search(qdrant, &collection, qdrant_request) {
        Ok(hits) => hits,
        Err(error) => {
            return Ok(degraded(
                "qdrant",
                ServiceState::Unreachable {
                    message: error.to_string(),
                },
            ));
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
    match scope {
        SearchScope::Project { project_id } => {
            collection_name("gwiki", CollectionScope::Project(project_id))
        }
        SearchScope::Topic { topic } => collection_name("gwiki", CollectionScope::Topic(topic)),
    }
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
            SemanticEmbedding::Direct(config) => embed_direct_query(&self.client, config, query),
            #[cfg(feature = "ai")]
            SemanticEmbedding::Daemon(context) => embed_daemon_query(context, query),
        }
    }
}

#[cfg(feature = "embeddings-http")]
fn embed_direct_query(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    query: &str,
) -> Result<Vec<f32>, SearchError> {
    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut request = client
        .post(url)
        .timeout(Duration::from_secs(config.timeout_seconds))
        .json(&json!({
            "model": config.model,
            "input": query,
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
    data.get("data")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(|item| item.get("embedding"))
        .and_then(Value::as_array)
        .ok_or_else(|| SearchError::Backend("missing data[0].embedding array".to_string()))?
        .iter()
        .map(|value| {
            value.as_f64().map(|value| value as f32).ok_or_else(|| {
                SearchError::Backend("embedding array contains a non-number".to_string())
            })
        })
        .collect()
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
    fn search(
        &mut self,
        config: &QdrantConfig,
        collection: &str,
        request: SearchRequest,
    ) -> Result<Vec<SearchHit>, SearchError> {
        gobby_core::qdrant::search(config, collection, request)
            .map_err(|error| SearchError::Backend(error.to_string()))
    }
}

fn degraded(service: &str, state: ServiceState) -> SemanticSearchOutcome {
    SemanticSearchOutcome {
        hits: Vec::new(),
        degradation: Some(DegradationKind::ServiceUnavailable {
            service: service.to_string(),
            state,
        }),
    }
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

#[cfg(test)]
pub struct UnavailableSemanticBackend;

#[cfg(test)]
impl SemanticSearchBackend for UnavailableSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: SemanticSearchRequest,
    ) -> Result<SemanticSearchOutcome, SearchError> {
        Ok(degraded("qdrant", ServiceState::NotConfigured))
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
    ) -> Result<Vec<SearchHit>, SearchError> {
        self.collection = Some(collection.to_string());
        self.filter = request.filter;
        Ok(self.hits.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search::{SearchScope, SearchSource};
    use gobby_core::config::{EmbeddingConfig, QdrantConfig};
    use serde_json::json;

    #[test]
    fn semantic_search_is_scope_filtered() {
        let embedding = EmbeddingConfig {
            api_base: "http://embeddings.local/v1".to_string(),
            model: "embed-model".to_string(),
            api_key: None,
            query_prefix: Some("query: ".to_string()),
            timeout_seconds: 10,
        };
        let qdrant = QdrantConfig {
            url: Some("http://qdrant.local".to_string()),
            api_key: None,
        };
        let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
        let mut vector = RecordingVectorBackend::new(vec![
            vector_hit("doc-1", "project", "project-1"),
            vector_hit("doc-2", "topic", "rust"),
        ]);

        let outcome = search_semantic(
            SemanticSearchRequest {
                query: "ownership".to_string(),
                scope: SearchScope::project("project-1"),
                limit: 5,
            },
            Some(&SemanticEmbedding::Direct(embedding)),
            Some(&qdrant),
            &mut embedder,
            &mut vector,
        )
        .expect("semantic search succeeds");

        assert_eq!(outcome.hits.len(), 1);
        assert_eq!(outcome.hits[0].id, "doc-1");
        assert_eq!(outcome.hits[0].sources, vec![SearchSource::Semantic]);
        assert!(outcome.degradation.is_none());
        assert_eq!(embedder.queries, vec!["query: ownership"]);

        assert_eq!(
            vector.collection,
            Some("gwiki:project:project-1".to_string())
        );
        assert_eq!(
            vector.filter,
            Some(json!({
                "must": [
                    {"key": "namespace", "match": {"value": "gwiki"}},
                    {"key": "scope_kind", "match": {"value": "project"}},
                    {"key": "project_id", "match": {"value": "project-1"}}
                ]
            }))
        );
    }

    #[cfg(feature = "ai")]
    #[test]
    fn semantic_search_daemon_embedding_uses_raw_query() {
        let qdrant = QdrantConfig {
            url: Some("http://qdrant.local".to_string()),
            api_key: None,
        };
        let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
        let mut vector =
            RecordingVectorBackend::new(vec![vector_hit("doc-1", "project", "project-1")]);

        let outcome = search_semantic(
            SemanticSearchRequest {
                query: "ownership".to_string(),
                scope: SearchScope::project("project-1"),
                limit: 5,
            },
            Some(&SemanticEmbedding::Daemon(Box::new(test_ai_context()))),
            Some(&qdrant),
            &mut embedder,
            &mut vector,
        )
        .expect("semantic search succeeds");

        assert_eq!(outcome.hits.len(), 1);
        assert_eq!(embedder.queries, vec!["ownership"]);
    }

    #[cfg(feature = "ai")]
    fn test_ai_context() -> AiContext {
        use gobby_core::ai_context::{AiBindings, AiLimiter};
        use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};

        let binding = CapabilityBinding {
            routing: AiRouting::Daemon,
            transport: None,
            api_base: None,
            api_key: None,
            model: None,
            provider: None,
            task: None,
            language: None,
            target_lang: None,
        };

        AiContext {
            bindings: AiBindings {
                embed: binding.clone(),
                audio_transcribe: binding.clone(),
                audio_translate: binding.clone(),
                vision_extract: binding.clone(),
                text_generate: binding,
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: AiLimiter::new(1),
            project_id: Some("project-1".to_string()),
        }
    }

    fn vector_hit(id: &str, scope_kind: &str, scope_value: &str) -> gobby_core::qdrant::SearchHit {
        let mut payload = serde_json::Map::new();
        payload.insert("namespace".to_string(), json!("gwiki"));
        payload.insert("scope_kind".to_string(), json!(scope_kind));
        payload.insert("project_id".to_string(), json!(scope_value));
        payload.insert("topic".to_string(), json!(scope_value));
        payload.insert("path".to_string(), json!("wiki/topics/rust.md"));
        payload.insert("source_path".to_string(), json!("raw/INDEX.md"));
        payload.insert("source_kind".to_string(), json!("topic"));
        payload.insert("content_hash".to_string(), json!("hash"));
        payload.insert("snippet".to_string(), json!("ownership and borrowing"));
        gobby_core::qdrant::SearchHit {
            id: id.to_string(),
            score: 0.8,
            payload,
        }
    }
}
