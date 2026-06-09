use std::fmt;

use gobby_core::config::QdrantConfig;
use gobby_core::qdrant::{UpsertRequest, VectorCollectionSchema};
use serde_json::{Map, Value, json};

use crate::search::SearchScope;
use crate::search::semantic::{OpenAiEmbeddingBackend, QueryEmbedder, SemanticEmbedding};

const VECTOR_DISTANCE_COSINE: &str = "Cosine";
const WIKI_VECTOR_UUID_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
    0x67, 0x77, 0x69, 0x6b, 0x69, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01,
]);

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct WikiVectorChunk {
    pub id: String,
    pub path: String,
    pub title: Option<String>,
    pub heading: Option<String>,
    pub chunk_index: usize,
    pub byte_start: usize,
    pub byte_end: usize,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct WikiVectorPoint {
    pub id: String,
    pub vector: Vec<f32>,
    pub payload: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct WikiVectorSyncOutcome {
    pub chunks: usize,
    pub upserted: usize,
    pub deleted_stale_paths: usize,
}

#[derive(Debug)]
pub(crate) enum WikiVectorError {
    Store(String),
    Embedding(String),
    Qdrant(String),
    InvalidData(String),
}

impl fmt::Display for WikiVectorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(message) => write!(f, "wiki vector store error: {message}"),
            Self::Embedding(message) => write!(f, "wiki vector embedding error: {message}"),
            Self::Qdrant(message) => write!(f, "wiki vector qdrant error: {message}"),
            Self::InvalidData(message) => write!(f, "wiki vector invalid data: {message}"),
        }
    }
}

impl std::error::Error for WikiVectorError {}

impl From<postgres::Error> for WikiVectorError {
    fn from(error: postgres::Error) -> Self {
        Self::Store(error.to_string())
    }
}

pub(crate) trait WikiVectorChunkSource {
    fn chunks(&mut self, scope: &SearchScope) -> Result<Vec<WikiVectorChunk>, WikiVectorError>;

    fn stale_paths(&mut self, scope: &SearchScope) -> Result<Vec<String>, WikiVectorError>;
}

pub(crate) trait WikiVectorEmbedder {
    fn embed_texts(&mut self, texts: &[String]) -> Result<Vec<Vec<f32>>, WikiVectorError>;
}

pub(crate) trait WikiVectorStore {
    fn resolve_collection(&mut self, scope: &SearchScope) -> Result<String, WikiVectorError> {
        Ok(collection_for_scope(scope))
    }

    fn ensure_collection(
        &mut self,
        collection: &str,
        schema: VectorCollectionSchema,
    ) -> Result<(), WikiVectorError>;

    fn delete_points(&mut self, collection: &str, filter: Value) -> Result<(), WikiVectorError>;

    fn upsert_points(
        &mut self,
        collection: &str,
        points: Vec<WikiVectorPoint>,
    ) -> Result<(), WikiVectorError>;
}

pub(crate) fn sync_scope_vectors<S, E, V>(
    scope: &SearchScope,
    source: &mut S,
    embedder: &mut E,
    store: &mut V,
) -> Result<WikiVectorSyncOutcome, WikiVectorError>
where
    S: WikiVectorChunkSource,
    E: WikiVectorEmbedder,
    V: WikiVectorStore,
{
    let collection = store.resolve_collection(scope)?;
    let chunks = source.chunks(scope)?;
    let stale_paths = source.stale_paths(scope)?;
    for path in &stale_paths {
        store.delete_points(&collection, delete_filter_for_path(scope, path))?;
    }

    if chunks.is_empty() {
        return Ok(WikiVectorSyncOutcome {
            chunks: 0,
            upserted: 0,
            deleted_stale_paths: stale_paths.len(),
        });
    }

    let texts = chunks
        .iter()
        .map(|chunk| chunk.content.clone())
        .collect::<Vec<_>>();
    let vectors = embedder.embed_texts(&texts)?;
    if vectors.len() != chunks.len() {
        return Err(WikiVectorError::Embedding(format!(
            "embedding batch returned {} vector(s) for {} chunk(s)",
            vectors.len(),
            chunks.len()
        )));
    }

    let vector_size = vectors
        .first()
        .map(Vec::len)
        .filter(|size| *size > 0)
        .ok_or_else(|| WikiVectorError::Embedding("embedding vector was empty".to_string()))?;
    store.ensure_collection(
        &collection,
        VectorCollectionSchema {
            size: vector_size,
            distance: VECTOR_DISTANCE_COSINE.to_string(),
        },
    )?;

    let points = chunks
        .iter()
        .zip(vectors)
        .map(|(chunk, vector)| {
            if vector.len() != vector_size {
                return Err(WikiVectorError::Embedding(format!(
                    "embedding for chunk {} returned {} dimension(s), expected {}",
                    chunk.id,
                    vector.len(),
                    vector_size
                )));
            }
            Ok(WikiVectorPoint {
                id: point_id_for_chunk(chunk),
                vector,
                payload: payload_for_chunk(scope, chunk),
            })
        })
        .collect::<Result<Vec<_>, WikiVectorError>>()?;
    let upserted = points.len();
    store.upsert_points(&collection, points)?;

    Ok(WikiVectorSyncOutcome {
        chunks: chunks.len(),
        upserted,
        deleted_stale_paths: stale_paths.len(),
    })
}

pub(crate) fn collection_for_scope(scope: &SearchScope) -> String {
    crate::search::semantic::collection_for_scope(scope)
}

pub(crate) fn delete_filter_for_path(scope: &SearchScope, path: &str) -> Value {
    let mut filter = crate::search::semantic::payload_filter(scope);
    if let Some(must) = filter.get_mut("must").and_then(Value::as_array_mut) {
        must.push(json!({"key": "path", "match": {"value": path}}));
    }
    filter
}

fn payload_for_chunk(scope: &SearchScope, chunk: &WikiVectorChunk) -> Map<String, Value> {
    let mut payload = Map::new();
    payload.insert("namespace".to_string(), Value::String("gwiki".to_string()));
    payload.insert("chunk_id".to_string(), Value::String(chunk.id.clone()));
    payload.insert(
        "scope_kind".to_string(),
        Value::String(scope.scope_kind().to_string()),
    );
    payload.insert(
        "scope_id".to_string(),
        Value::String(scope.scope_value().to_string()),
    );
    match scope {
        SearchScope::Project { project_id } => {
            payload.insert("project_id".to_string(), Value::String(project_id.clone()));
        }
        SearchScope::Topic { topic } => {
            payload.insert("topic".to_string(), Value::String(topic.clone()));
        }
    }
    payload.insert("path".to_string(), Value::String(chunk.path.clone()));
    payload.insert("source_path".to_string(), Value::String(chunk.path.clone()));
    if let Some(title) = &chunk.title {
        payload.insert("title".to_string(), Value::String(title.clone()));
    }
    if let Some(heading) = &chunk.heading {
        payload.insert("heading".to_string(), Value::String(heading.clone()));
    }
    payload.insert("chunk_index".to_string(), json!(chunk.chunk_index));
    payload.insert("byte_start".to_string(), json!(chunk.byte_start));
    payload.insert("byte_end".to_string(), json!(chunk.byte_end));
    payload.insert("content".to_string(), Value::String(chunk.content.clone()));
    payload.insert(
        "snippet".to_string(),
        Value::String(snippet(&chunk.content)),
    );
    payload
}

fn point_id_for_chunk(chunk: &WikiVectorChunk) -> String {
    uuid::Uuid::new_v5(&WIKI_VECTOR_UUID_NAMESPACE, chunk.id.as_bytes()).to_string()
}

fn snippet(content: &str) -> String {
    crate::support::text::snippet_from_text(content)
}

pub(crate) struct PostgresWikiVectorChunkSource<'a> {
    conn: &'a mut postgres::Client,
}

impl<'a> PostgresWikiVectorChunkSource<'a> {
    pub(crate) fn new(conn: &'a mut postgres::Client) -> Self {
        Self { conn }
    }
}

impl WikiVectorChunkSource for PostgresWikiVectorChunkSource<'_> {
    fn chunks(&mut self, scope: &SearchScope) -> Result<Vec<WikiVectorChunk>, WikiVectorError> {
        let rows = self.conn.query(
            "SELECT c.id, c.path, d.title, c.heading_path, c.chunk_index,
                    c.provenance->>'byte_start' AS byte_start,
                    c.provenance->>'byte_end' AS byte_end,
                    c.content
             FROM gwiki_chunks c
             JOIN gwiki_documents d
               ON d.scope_kind = c.scope_kind
              AND d.scope_id = c.scope_id
              AND d.path = c.path
             WHERE c.scope_kind = $1 AND c.scope_id = $2
             ORDER BY c.path, c.chunk_index",
            &[&scope.scope_kind(), &scope.scope_value()],
        )?;
        rows.into_iter().map(row_to_vector_chunk).collect()
    }

    fn stale_paths(&mut self, scope: &SearchScope) -> Result<Vec<String>, WikiVectorError> {
        let rows = self.conn.query(
            "SELECT path
             FROM (
                SELECT DISTINCT ON (path) path, status, ingested_at
                FROM gwiki_ingestions
                WHERE scope_kind = $1 AND scope_id = $2
                ORDER BY path, ingested_at DESC
             ) latest
             WHERE status = 'deleted'
             ORDER BY path",
            &[&scope.scope_kind(), &scope.scope_value()],
        )?;
        Ok(rows.into_iter().map(|row| row.get("path")).collect())
    }
}

fn row_to_vector_chunk(row: postgres::Row) -> Result<WikiVectorChunk, WikiVectorError> {
    let chunk_index: i32 = row.get("chunk_index");
    let byte_start = required_row_usize(&row, "byte_start")?;
    let byte_end = required_row_usize(&row, "byte_end")?;
    let heading_path: Vec<String> = row.get("heading_path");
    let heading = if heading_path.is_empty() {
        None
    } else {
        Some(heading_path.join(" / "))
    };
    Ok(WikiVectorChunk {
        id: row.get("id"),
        path: row.get("path"),
        title: row.get("title"),
        heading,
        chunk_index: usize::try_from(chunk_index).map_err(|_| {
            WikiVectorError::InvalidData(format!("chunk_index {chunk_index} is negative"))
        })?,
        byte_start,
        byte_end,
        content: row.get("content"),
    })
}

fn required_row_usize(row: &postgres::Row, column: &'static str) -> Result<usize, WikiVectorError> {
    let raw = row.try_get::<_, Option<String>>(column).map_err(|error| {
        WikiVectorError::InvalidData(format!("{column} is unavailable: {error}"))
    })?;
    parse_required_usize(raw, column)
}

fn parse_required_usize(
    raw: Option<String>,
    column: &'static str,
) -> Result<usize, WikiVectorError> {
    let raw = raw.ok_or_else(|| WikiVectorError::InvalidData(format!("{column} is missing")))?;
    raw.parse::<usize>().map_err(|error| {
        WikiVectorError::InvalidData(format!("{column} value {raw:?} is invalid: {error}"))
    })
}

pub(crate) struct GwikiEmbeddingBackend {
    embedding: SemanticEmbedding,
    query_backend: OpenAiEmbeddingBackend,
}

impl GwikiEmbeddingBackend {
    pub(crate) fn new(embedding: SemanticEmbedding) -> Self {
        Self {
            embedding,
            query_backend: OpenAiEmbeddingBackend::new(),
        }
    }
}

impl WikiVectorEmbedder for GwikiEmbeddingBackend {
    fn embed_texts(&mut self, texts: &[String]) -> Result<Vec<Vec<f32>>, WikiVectorError> {
        match &self.embedding {
            #[cfg(feature = "ai")]
            SemanticEmbedding::Daemon(context) => {
                gobby_core::ai::daemon::embed_via_daemon(context, texts, false)
                    .map(|result| result.embeddings)
                    .map_err(|error| WikiVectorError::Embedding(error.to_string()))
            }
            SemanticEmbedding::Direct(_) => self
                .query_backend
                .embed_queries(&self.embedding, texts)
                .map_err(|error| WikiVectorError::Embedding(error.to_string())),
        }
    }
}

pub(crate) struct GwikiQdrantVectorStore {
    config: QdrantConfig,
}

impl GwikiQdrantVectorStore {
    pub(crate) fn new(config: QdrantConfig) -> Self {
        Self { config }
    }
}

impl WikiVectorStore for GwikiQdrantVectorStore {
    fn ensure_collection(
        &mut self,
        collection: &str,
        schema: VectorCollectionSchema,
    ) -> Result<(), WikiVectorError> {
        gobby_core::qdrant::ensure_collection(&self.config, collection, schema)
            .map(|_| ())
            .map_err(|error| WikiVectorError::Qdrant(error.to_string()))
    }

    fn delete_points(&mut self, collection: &str, filter: Value) -> Result<(), WikiVectorError> {
        gobby_core::qdrant::delete_points_by_filter(&self.config, collection, filter)
            .map_err(|error| WikiVectorError::Qdrant(error.to_string()))
    }

    fn upsert_points(
        &mut self,
        collection: &str,
        points: Vec<WikiVectorPoint>,
    ) -> Result<(), WikiVectorError> {
        let points = points
            .into_iter()
            .map(|point| UpsertRequest {
                id: point.id,
                vector: point.vector,
                payload: point.payload,
            })
            .collect();
        gobby_core::qdrant::upsert(&self.config, collection, points)
            .map(|_| ())
            .map_err(|error| WikiVectorError::Qdrant(error.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::search::SearchScope;
    use serde_json::Value;

    #[test]
    fn vector_collection_and_path_filter_match_scope_contract() {
        let project = SearchScope::project("project-1");
        assert_eq!(collection_for_scope(&project), "gwiki_project_project-1");
        assert_eq!(
            filter_value(
                &delete_filter_for_path(&project, "notes/page.md"),
                "project_id"
            ),
            Some("project-1".to_string())
        );

        let topic = SearchScope::topic("rust-async");
        assert_eq!(collection_for_scope(&topic), "gwiki_topic_rust-async");
        assert_eq!(
            filter_value(&delete_filter_for_path(&topic, "notes/page.md"), "topic"),
            Some("rust-async".to_string())
        );
        assert_eq!(
            filter_value(&delete_filter_for_path(&topic, "notes/page.md"), "path"),
            Some("notes/page.md".to_string())
        );
    }

    #[test]
    fn vector_sync_embeds_upserts_and_deletes_stale_vectors() {
        let scope = SearchScope::project("project-1");
        let chunk = WikiVectorChunk {
            id: "chunk:project:project-1:notes/page.md:0".to_string(),
            path: "notes/page.md".to_string(),
            title: Some("Page".to_string()),
            heading: Some("Overview".to_string()),
            chunk_index: 0,
            byte_start: 12,
            byte_end: 64,
            content: "Durable notes about Rust ownership.".to_string(),
        };
        let mut source = MockChunkSource {
            chunks: vec![chunk],
            stale_paths: vec!["notes/stale.md".to_string()],
        };
        let mut embedder = MockEmbedder {
            vectors: vec![vec![0.1, 0.2, 0.3]],
            inputs: Vec::new(),
        };
        let mut store = RecordingVectorStore::default();

        let outcome = sync_scope_vectors(&scope, &mut source, &mut embedder, &mut store)
            .expect("vector sync succeeds");

        assert_eq!(outcome.chunks, 1);
        assert_eq!(outcome.upserted, 1);
        assert_eq!(outcome.deleted_stale_paths, 1);
        assert_eq!(embedder.inputs, vec!["Durable notes about Rust ownership."]);
        assert_eq!(
            store.ensured,
            vec![(
                "gwiki_project_project-1".to_string(),
                3,
                "Cosine".to_string()
            )]
        );
        assert_eq!(store.deleted.len(), 1);
        assert_eq!(
            filter_value(&store.deleted[0], "path"),
            Some("notes/stale.md".to_string())
        );
        assert_eq!(store.upserts.len(), 1);
        let upsert = &store.upserts[0];
        assert_eq!(upsert.collection, "gwiki_project_project-1");
        assert_eq!(upsert.points[0].id, point_id_for_chunk(&source.chunks[0]));
        assert_eq!(
            upsert.points[0]
                .payload
                .get("chunk_id")
                .and_then(Value::as_str),
            Some("chunk:project:project-1:notes/page.md:0")
        );
        assert_eq!(
            upsert.points[0]
                .payload
                .get("title")
                .and_then(Value::as_str),
            Some("Page")
        );
        assert_eq!(
            upsert.points[0]
                .payload
                .get("scope_kind")
                .and_then(Value::as_str),
            Some("project")
        );
    }

    #[cfg(feature = "embeddings-http")]
    #[test]
    fn direct_embedding_backend_batches_texts() {
        let (api_base, request_handle) = crate::test_http::spawn_json_response(
            serde_json::json!({
                "data": [
                    {"embedding": [0.1, 0.2]},
                    {"embedding": [0.3, 0.4]}
                ]
            })
            .to_string(),
        )
        .expect("spawn test server");
        let embedding = SemanticEmbedding::Direct(gobby_core::config::EmbeddingConfig {
            api_base,
            model: "embed-model".to_string(),
            api_key: Some("test-key".to_string()),
            query_prefix: None,
            timeout_seconds: 5,
        });
        let mut backend = GwikiEmbeddingBackend::new(embedding);
        let texts = vec!["first chunk".to_string(), "second chunk".to_string()];

        let vectors = backend.embed_texts(&texts).expect("embeddings resolve");
        let request = request_handle.join().expect("request thread").unwrap();
        let body = request
            .split_once("\r\n\r\n")
            .map(|(_, body)| body)
            .expect("request body");
        let payload: Value = serde_json::from_str(body).expect("request json");

        assert_eq!(vectors, vec![vec![0.1, 0.2], vec![0.3, 0.4]]);
        assert!(request.starts_with("POST /embeddings HTTP/1.1"));
        assert!(request.contains("authorization: Bearer test-key"));
        assert_eq!(payload["model"], "embed-model");
        assert_eq!(payload["input"], serde_json::json!(texts));
    }

    #[test]
    fn vector_required_offset_parser_rejects_missing_and_malformed_values() {
        assert!(matches!(
            parse_required_usize(None, "byte_start"),
            Err(WikiVectorError::InvalidData(message)) if message.contains("byte_start is missing")
        ));
        assert!(matches!(
            parse_required_usize(Some("abc".to_string()), "byte_end"),
            Err(WikiVectorError::InvalidData(message))
                if message.contains("byte_end value \"abc\" is invalid")
        ));
    }

    struct MockChunkSource {
        chunks: Vec<WikiVectorChunk>,
        stale_paths: Vec<String>,
    }

    impl WikiVectorChunkSource for MockChunkSource {
        fn chunks(
            &mut self,
            _scope: &SearchScope,
        ) -> Result<Vec<WikiVectorChunk>, WikiVectorError> {
            Ok(self.chunks.clone())
        }

        fn stale_paths(&mut self, _scope: &SearchScope) -> Result<Vec<String>, WikiVectorError> {
            Ok(self.stale_paths.clone())
        }
    }

    struct MockEmbedder {
        vectors: Vec<Vec<f32>>,
        inputs: Vec<String>,
    }

    impl WikiVectorEmbedder for MockEmbedder {
        fn embed_texts(&mut self, texts: &[String]) -> Result<Vec<Vec<f32>>, WikiVectorError> {
            self.inputs.extend(texts.iter().cloned());
            Ok(self.vectors.clone())
        }
    }

    #[derive(Default)]
    struct RecordingVectorStore {
        ensured: Vec<(String, usize, String)>,
        deleted: Vec<Value>,
        upserts: Vec<RecordedUpsert>,
    }

    struct RecordedUpsert {
        collection: String,
        points: Vec<WikiVectorPoint>,
    }

    impl WikiVectorStore for RecordingVectorStore {
        fn ensure_collection(
            &mut self,
            collection: &str,
            schema: gobby_core::qdrant::VectorCollectionSchema,
        ) -> Result<(), WikiVectorError> {
            self.ensured
                .push((collection.to_string(), schema.size, schema.distance));
            Ok(())
        }

        fn delete_points(
            &mut self,
            _collection: &str,
            filter: Value,
        ) -> Result<(), WikiVectorError> {
            self.deleted.push(filter);
            Ok(())
        }

        fn upsert_points(
            &mut self,
            collection: &str,
            points: Vec<WikiVectorPoint>,
        ) -> Result<(), WikiVectorError> {
            self.upserts.push(RecordedUpsert {
                collection: collection.to_string(),
                points,
            });
            Ok(())
        }
    }

    fn filter_value(filter: &Value, key: &str) -> Option<String> {
        filter
            .get("must")
            .and_then(Value::as_array)?
            .iter()
            .find(|condition| condition.get("key").and_then(Value::as_str) == Some(key))?
            .pointer("/match/value")
            .and_then(Value::as_str)
            .map(str::to_string)
    }
}
