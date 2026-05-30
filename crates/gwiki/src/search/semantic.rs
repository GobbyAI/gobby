use std::path::PathBuf;

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
        config: &EmbeddingConfig,
        query: &str,
    ) -> Result<Vec<f32>, SearchError>;
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
    embedding: Option<&EmbeddingConfig>,
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

    let vector = match embedder.embed_query(embedding, &request.query) {
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
    embedding: Option<EmbeddingConfig>,
    qdrant: Option<QdrantConfig>,
    embedder: E,
    vector_backend: V,
}

impl<E, V> GobbySemanticBackend<E, V> {
    pub fn new(
        embedding: Option<EmbeddingConfig>,
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

#[derive(Debug, Clone)]
pub struct OpenAiEmbeddingBackend {
    client: reqwest::blocking::Client,
}

impl Default for OpenAiEmbeddingBackend {
    fn default() -> Self {
        Self {
            client: reqwest::blocking::Client::new(),
        }
    }
}

impl QueryEmbedder for OpenAiEmbeddingBackend {
    fn embed_query(
        &mut self,
        config: &EmbeddingConfig,
        query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
        let mut request = self.client.post(url).json(&json!({
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
        path: PathBuf::from(&path),
        source_path: PathBuf::from(&source_path),
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
            document_path: PathBuf::from(path),
            source_path: PathBuf::from(source_path),
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
struct FixedEmbedder(Vec<f32>);

#[cfg(test)]
impl QueryEmbedder for FixedEmbedder {
    fn embed_query(
        &mut self,
        _config: &EmbeddingConfig,
        _query: &str,
    ) -> Result<Vec<f32>, SearchError> {
        Ok(self.0.clone())
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
        };
        let qdrant = QdrantConfig {
            url: Some("http://qdrant.local".to_string()),
            api_key: None,
        };
        let mut embedder = FixedEmbedder(vec![0.1, 0.2, 0.3]);
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
            Some(&embedding),
            Some(&qdrant),
            &mut embedder,
            &mut vector,
        )
        .expect("semantic search succeeds");

        assert_eq!(outcome.hits.len(), 1);
        assert_eq!(outcome.hits[0].id, "doc-1");
        assert_eq!(outcome.hits[0].sources, vec![SearchSource::Semantic]);
        assert!(outcome.degradation.is_none());

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
