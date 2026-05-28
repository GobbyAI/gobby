use postgres::GenericClient;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::fmt;
use std::time::Duration;

use crate::config::{
    CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorSettings, Context, EmbeddingConfig, QdrantConfig,
};
use crate::db;
use crate::models::{ProjectionMetadata, ProjectionProvenance, Symbol};
use gobby_core::degradation::ServiceState;
use gobby_core::qdrant::{CollectionScope, SearchRequest, UpsertRequest};

// Keep code-symbol collections compatible with the Python daemon's Qdrant schema.
pub const VECTOR_DISTANCE_COSINE: &str = "Cosine";
const DIMENSION_PROBE_TEXT: &str = "dimension_probe";
const HTTP_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbolVectorSearchRequest {
    pub project_id: String,
    pub query: String,
    pub limit: usize,
    pub collection_prefix: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CodeSymbolVectorSearchHit {
    pub symbol_id: String,
    pub score: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CodeSymbolVectorPayload {
    pub project_id: String,
    pub file_path: String,
    pub symbol_id: String,
    pub name: String,
    pub kind: String,
    pub language: String,
    pub line_start: usize,
    pub line_end: usize,
    pub byte_start: usize,
    pub byte_end: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub docstring: Option<String>,
    pub provenance: ProjectionProvenance,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    pub source_system: String,
    pub source_file_path: String,
    pub source_line: usize,
    pub source_line_start: usize,
    pub source_line_end: usize,
    pub source_byte_start: usize,
    pub source_byte_end: usize,
    pub source_symbol_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
}

impl CodeSymbolVectorPayload {
    pub fn from_symbol(symbol: &Symbol) -> Self {
        let metadata = ProjectionMetadata::gcode_extracted()
            .with_source_file_path(&symbol.file_path)
            .with_source_line(symbol.line_start)
            .with_source_symbol_id(&symbol.id);

        Self {
            project_id: symbol.project_id.clone(),
            file_path: symbol.file_path.clone(),
            symbol_id: symbol.id.clone(),
            name: symbol.name.clone(),
            kind: symbol.kind.clone(),
            language: symbol.language.clone(),
            line_start: symbol.line_start,
            line_end: symbol.line_end,
            byte_start: symbol.byte_start,
            byte_end: symbol.byte_end,
            signature: symbol.signature.clone(),
            docstring: symbol.docstring.clone(),
            provenance: metadata.provenance,
            confidence: metadata.confidence,
            source_system: metadata.source_system,
            source_file_path: metadata
                .source_file_path
                .unwrap_or_else(|| symbol.file_path.clone()),
            source_line: metadata.source_line.unwrap_or(symbol.line_start),
            source_line_start: symbol.line_start,
            source_line_end: symbol.line_end,
            source_byte_start: symbol.byte_start,
            source_byte_end: symbol.byte_end,
            source_symbol_id: metadata
                .source_symbol_id
                .unwrap_or_else(|| symbol.id.clone()),
            summary: symbol.summary.clone(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeSymbolVectorLifecycleAction {
    Ensure,
    SyncFile,
    Clear,
    Rebuild,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbolVectorLifecycleStatus {
    pub project_id: String,
    pub collection: String,
    pub action: CodeSymbolVectorLifecycleAction,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VectorCollectionSchema {
    pub size: usize,
    pub distance: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ExistingVectorCollectionSchema {
    size: Option<usize>,
    distance: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbolVectorLifecycleOutput {
    pub project_id: String,
    pub collection: String,
    pub action: CodeSymbolVectorLifecycleAction,
    pub file_path: Option<String>,
    pub symbols: usize,
    pub vectors_upserted: usize,
    pub vectors_deleted: usize,
    pub summary: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VectorLifecycleError {
    MissingQdrantConfig,
    MissingEmbeddingConfig,
    EmbeddingHttp {
        status: u16,
        body: String,
    },
    EmbeddingResponse(String),
    QdrantHttp {
        operation: &'static str,
        status: u16,
        body: String,
    },
    QdrantOperation(String),
    DimensionMismatch {
        collection: String,
        expected_size: usize,
        found_size: Option<usize>,
        expected_distance: &'static str,
        found_distance: Option<String>,
    },
}

impl fmt::Display for VectorLifecycleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingQdrantConfig => {
                write!(f, "Qdrant config is required for vector lifecycle commands")
            }
            Self::MissingEmbeddingConfig => write!(
                f,
                "embedding config is required for vector lifecycle commands"
            ),
            Self::EmbeddingHttp { status, body } => {
                write!(f, "embedding request failed: HTTP {status}: {body}")
            }
            Self::EmbeddingResponse(reason) => {
                write!(f, "embedding response was invalid: {reason}")
            }
            Self::QdrantHttp {
                operation,
                status,
                body,
            } => write!(f, "Qdrant {operation} failed: HTTP {status}: {body}"),
            Self::QdrantOperation(reason) => write!(f, "Qdrant operation failed: {reason}"),
            Self::DimensionMismatch {
                collection,
                expected_size,
                found_size,
                expected_distance,
                found_distance,
            } => write!(
                f,
                "Qdrant collection `{collection}` has incompatible vector schema: expected size {expected_size} distance {expected_distance}, found size {} distance {}. Refusing to migrate, drop, or recreate the collection.",
                found_size
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "unknown".to_string()),
                found_distance.as_deref().unwrap_or("unknown")
            ),
        }
    }
}

impl std::error::Error for VectorLifecycleError {}

#[derive(Debug)]
pub struct CodeSymbolVectorLifecycle {
    project_id: String,
    collection: String,
    qdrant: QdrantConfig,
    embedding: EmbeddingConfig,
    settings: CodeVectorSettings,
    probed_vector_size: Option<usize>,
    client: reqwest::blocking::Client,
}

pub fn collection_name(collection_prefix: &str, project_id: &str) -> String {
    let collection = format!("{collection_prefix}{project_id}");
    gobby_core::qdrant::collection_name("gcode", CollectionScope::Custom(&collection))
}

pub fn resolve_lifecycle_qdrant_config(
    source: &mut impl gobby_core::config::ConfigSource,
) -> Option<QdrantConfig> {
    gobby_core::config::resolve_qdrant_config(source)
}

pub fn lifecycle_status(
    project_id: impl Into<String>,
    collection_prefix: &str,
    action: CodeSymbolVectorLifecycleAction,
) -> CodeSymbolVectorLifecycleStatus {
    let project_id = project_id.into();
    CodeSymbolVectorLifecycleStatus {
        collection: collection_name(collection_prefix, &project_id),
        project_id,
        action,
    }
}

pub fn embed_text(config: &EmbeddingConfig, text: &str) -> Result<Vec<f32>, VectorLifecycleError> {
    let client = reqwest::blocking::Client::builder()
        .timeout(HTTP_TIMEOUT)
        .build()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;

    let body = json!({
        "model": config.model,
        "input": text,
    });

    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut req = client.post(&url).json(&body);

    if let Some(key) = &config.api_key {
        req = req.header("Authorization", format!("Bearer {key}"));
    }

    let resp = req
        .send()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let body = resp.text().unwrap_or_default();
        return Err(VectorLifecycleError::EmbeddingHttp { status, body });
    }

    let data: Value = resp
        .json()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    let embedding: Vec<f32> = data
        .get("data")
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .and_then(|value| value.get("embedding"))
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("missing data[0].embedding array".to_string())
        })?
        .iter()
        .map(|value| {
            value.as_f64().map(|f| f as f32).ok_or_else(|| {
                VectorLifecycleError::EmbeddingResponse(
                    "embedding array contains a non-number".to_string(),
                )
            })
        })
        .collect::<Result<Vec<_>, _>>()?;

    if embedding.is_empty() {
        Err(VectorLifecycleError::EmbeddingResponse(
            "embedding vector was empty".to_string(),
        ))
    } else {
        Ok(embedding)
    }
}

pub fn embed_query(config: &EmbeddingConfig, text: &str) -> Option<Vec<f32>> {
    embed_text(config, &format!("search_query: {text}")).ok()
}

pub fn vector_text_for_symbol(symbol: &Symbol) -> String {
    let mut lines = vec![
        format!("name: {}", symbol.name),
        format!("qualified_name: {}", symbol.qualified_name),
        format!("kind: {}", symbol.kind),
        format!("language: {}", symbol.language),
        format!("file_path: {}", symbol.file_path),
        format!("range: {}-{}", symbol.line_start, symbol.line_end),
    ];
    if let Some(signature) = symbol
        .signature
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("signature: {signature}"));
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("docstring: {docstring}"));
    }
    if let Some(summary) = symbol
        .summary
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("summary: {summary}"));
    }
    lines.join("\n")
}

pub fn vector_search(
    config: &QdrantConfig,
    collection: &str,
    query_vector: &[f32],
    limit: usize,
) -> anyhow::Result<Vec<(String, f64)>> {
    let request = SearchRequest {
        vector: query_vector.to_vec(),
        limit,
        filter: None,
    };
    let (hits, _) = gobby_core::qdrant::with_qdrant(Some(config), Vec::new(), |config| {
        gobby_core::qdrant::search(config, collection, request)
    })?;
    Ok(hits
        .into_iter()
        .map(|hit| (hit.id, f64::from(hit.score)))
        .collect())
}

impl CodeSymbolVectorLifecycle {
    pub fn new(
        project_id: String,
        qdrant: QdrantConfig,
        embedding: EmbeddingConfig,
        settings: CodeVectorSettings,
    ) -> Result<Self, VectorLifecycleError> {
        if qdrant
            .url
            .as_deref()
            .filter(|url| !url.trim().is_empty())
            .is_none()
        {
            return Err(VectorLifecycleError::MissingQdrantConfig);
        }
        if embedding.api_base.trim().is_empty() {
            return Err(VectorLifecycleError::MissingEmbeddingConfig);
        }

        let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &project_id);
        let client = reqwest::blocking::Client::builder()
            .timeout(HTTP_TIMEOUT)
            .build()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        Ok(Self {
            project_id,
            collection,
            qdrant,
            embedding,
            settings,
            probed_vector_size: None,
            client,
        })
    }

    pub fn collection(&self) -> &str {
        &self.collection
    }

    pub fn ensure_collection(&mut self) -> Result<VectorCollectionSchema, VectorLifecycleError> {
        let expected = self.expected_schema()?;
        self.require_qdrant_boundary()?;
        match self.get_collection_schema()? {
            Some(found) => self.ensure_compatible_schema(expected, found),
            None => {
                self.create_collection(&expected)?;
                Ok(expected)
            }
        }
    }

    pub fn sync_file_symbols(
        &mut self,
        file_path: &str,
        symbols: &[Symbol],
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        self.ensure_collection()?;
        let points = self.points_for_symbols(symbols)?;
        self.delete_vectors(Some(file_path))?;
        self.upsert_points(points)?;

        Ok(self.output(
            CodeSymbolVectorLifecycleAction::SyncFile,
            Some(file_path.to_string()),
            symbols.len(),
            symbols.len(),
            1,
        ))
    }

    pub fn clear_project_vectors(
        &mut self,
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        let expected = self.expected_schema()?;
        self.require_qdrant_boundary()?;
        let deleted = match self.get_collection_schema()? {
            Some(found) => {
                self.ensure_compatible_schema(expected, found)?;
                self.delete_vectors(None)?;
                1
            }
            None => 0,
        };

        Ok(self.output(CodeSymbolVectorLifecycleAction::Clear, None, 0, 0, deleted))
    }

    pub fn rebuild_symbols(
        &mut self,
        symbols: &[Symbol],
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        self.ensure_collection()?;
        let points = self.points_for_symbols(symbols)?;
        self.delete_vectors(None)?;
        self.upsert_points(points)?;

        Ok(self.output(
            CodeSymbolVectorLifecycleAction::Rebuild,
            None,
            symbols.len(),
            symbols.len(),
            1,
        ))
    }

    fn output(
        &self,
        action: CodeSymbolVectorLifecycleAction,
        file_path: Option<String>,
        symbols: usize,
        vectors_upserted: usize,
        vectors_deleted: usize,
    ) -> CodeSymbolVectorLifecycleOutput {
        CodeSymbolVectorLifecycleOutput {
            project_id: self.project_id.clone(),
            collection: self.collection.clone(),
            action,
            file_path,
            symbols,
            vectors_upserted,
            vectors_deleted,
            summary: format!(
                "{vectors_upserted} vector(s) upserted, {vectors_deleted} delete operation(s) issued"
            ),
        }
    }

    fn expected_schema(&mut self) -> Result<VectorCollectionSchema, VectorLifecycleError> {
        let size = match self.settings.vector_dim {
            Some(size) => size,
            None => match self.probed_vector_size {
                Some(size) => size,
                None => {
                    let size = embed_text(&self.embedding, DIMENSION_PROBE_TEXT)?.len();
                    self.probed_vector_size = Some(size);
                    size
                }
            },
        };

        Ok(VectorCollectionSchema {
            size,
            distance: VECTOR_DISTANCE_COSINE.to_string(),
        })
    }

    fn require_qdrant_boundary(&self) -> Result<(), VectorLifecycleError> {
        let ((), state) = gobby_core::qdrant::with_qdrant(Some(&self.qdrant), (), |_| Ok(()))
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        match state {
            ServiceState::Available => Ok(()),
            ServiceState::NotConfigured => Err(VectorLifecycleError::MissingQdrantConfig),
            other => Err(VectorLifecycleError::QdrantOperation(format!(
                "unexpected Qdrant service state: {other:?}"
            ))),
        }
    }

    fn ensure_compatible_schema(
        &self,
        expected: VectorCollectionSchema,
        found: ExistingVectorCollectionSchema,
    ) -> Result<VectorCollectionSchema, VectorLifecycleError> {
        if found.size == Some(expected.size)
            && found.distance.as_deref() == Some(&expected.distance)
        {
            return Ok(VectorCollectionSchema {
                size: expected.size,
                distance: expected.distance,
            });
        }

        Err(VectorLifecycleError::DimensionMismatch {
            collection: self.collection.clone(),
            expected_size: expected.size,
            found_size: found.size,
            expected_distance: VECTOR_DISTANCE_COSINE,
            found_distance: found.distance,
        })
    }

    fn get_collection_schema(
        &self,
    ) -> Result<Option<ExistingVectorCollectionSchema>, VectorLifecycleError> {
        let resp = self
            .qdrant_request(
                reqwest::Method::GET,
                &format!("/collections/{}", self.collection),
            )?
            .send()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        let status = resp.status();
        if status == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        if !status.is_success() {
            return Err(qdrant_http_error("get collection", status, resp));
        }

        let data: Value = resp
            .json()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        Ok(parse_collection_schema(&data))
    }

    fn create_collection(
        &self,
        schema: &VectorCollectionSchema,
    ) -> Result<(), VectorLifecycleError> {
        let body = json!({
            "vectors": {
                "size": schema.size,
                "distance": schema.distance,
            },
        });
        let resp = self
            .qdrant_request(
                reqwest::Method::PUT,
                &format!("/collections/{}", self.collection),
            )?
            .json(&body)
            .send()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        if !resp.status().is_success() {
            return Err(qdrant_http_error("create collection", resp.status(), resp));
        }
        Ok(())
    }

    fn delete_vectors(&self, file_path: Option<&str>) -> Result<(), VectorLifecycleError> {
        let mut must = vec![json!({
            "key": "project_id",
            "match": {"value": self.project_id},
        })];
        if let Some(file_path) = file_path {
            must.push(json!({
                "key": "file_path",
                "match": {"value": file_path},
            }));
        }
        let body = json!({
            "filter": {
                "must": must,
            },
        });
        let resp = self
            .qdrant_request(
                reqwest::Method::POST,
                &format!("/collections/{}/points/delete", self.collection),
            )?
            .json(&body)
            .send()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        if !resp.status().is_success() {
            return Err(qdrant_http_error("delete points", resp.status(), resp));
        }
        Ok(())
    }

    fn upsert_points(&self, points: Vec<UpsertRequest>) -> Result<(), VectorLifecycleError> {
        if points.is_empty() {
            return Ok(());
        }
        let ((), state) = gobby_core::qdrant::with_qdrant(Some(&self.qdrant), (), |config| {
            gobby_core::qdrant::upsert(config, &self.collection, points)
        })
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        match state {
            ServiceState::Available => Ok(()),
            ServiceState::NotConfigured => Err(VectorLifecycleError::MissingQdrantConfig),
            other => Err(VectorLifecycleError::QdrantOperation(format!(
                "unexpected Qdrant service state: {other:?}"
            ))),
        }
    }

    fn points_for_symbols(
        &self,
        symbols: &[Symbol],
    ) -> Result<Vec<UpsertRequest>, VectorLifecycleError> {
        symbols
            .iter()
            .map(|symbol| {
                let vector = embed_text(&self.embedding, &vector_text_for_symbol(symbol))?;
                let payload = payload_map(CodeSymbolVectorPayload::from_symbol(symbol))?;
                Ok(UpsertRequest {
                    id: symbol.id.clone(),
                    vector,
                    payload,
                })
            })
            .collect()
    }

    fn qdrant_request(
        &self,
        method: reqwest::Method,
        path: &str,
    ) -> Result<reqwest::blocking::RequestBuilder, VectorLifecycleError> {
        let base = self
            .qdrant
            .url
            .as_deref()
            .ok_or(VectorLifecycleError::MissingQdrantConfig)?
            .trim_end_matches('/');
        let url = format!("{base}{path}");
        let mut req = self.client.request(method, url);
        if let Some(key) = &self.qdrant.api_key {
            req = req.header("api-key", key);
        }
        Ok(req)
    }
}

pub fn fetch_symbols_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE project_id = $1 AND file_path = $2
             ORDER BY file_path, byte_start, id"
        ),
        &[&project_id, &file_path],
    )?
    .into_iter()
    .map(|row| Symbol::from_row(&row))
    .collect()
}

pub fn fetch_symbols_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE project_id = $1
             ORDER BY file_path, byte_start, id"
        ),
        &[&project_id],
    )?
    .into_iter()
    .map(|row| Symbol::from_row(&row))
    .collect()
}

fn payload_map(
    payload: CodeSymbolVectorPayload,
) -> Result<Map<String, Value>, VectorLifecycleError> {
    match serde_json::to_value(payload)
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?
    {
        Value::Object(map) => Ok(map),
        _ => Err(VectorLifecycleError::QdrantOperation(
            "vector payload did not serialize to an object".to_string(),
        )),
    }
}

fn parse_collection_schema(data: &Value) -> Option<ExistingVectorCollectionSchema> {
    let vectors = data.pointer("/result/config/params/vectors")?;
    let size = vectors
        .get("size")
        .and_then(Value::as_u64)
        .map(|size| size as usize);
    let distance = vectors
        .get("distance")
        .and_then(Value::as_str)
        .map(str::to_string);
    Some(ExistingVectorCollectionSchema { size, distance })
}

fn qdrant_http_error(
    operation: &'static str,
    status: StatusCode,
    resp: reqwest::blocking::Response,
) -> VectorLifecycleError {
    VectorLifecycleError::QdrantHttp {
        operation,
        status: status.as_u16(),
        body: resp.text().unwrap_or_default(),
    }
}

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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CodeVectorSettings, QdrantConfig};
    use crate::models::{SOURCE_SYSTEM_GCODE, Symbol};
    use serde_json::{Value, json};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    fn test_symbol(summary: Option<String>) -> Symbol {
        Symbol {
            id: "symbol-1".to_string(),
            project_id: "project-1".to_string(),
            file_path: "src/lib.rs".to_string(),
            name: "run".to_string(),
            qualified_name: "crate::run".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 10,
            byte_end: 40,
            line_start: 3,
            line_end: 5,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            content_hash: "hash".to_string(),
            summary,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn payloads_carry_provenance_metadata() {
        let payload = CodeSymbolVectorPayload::from_symbol(&test_symbol(Some("does work".into())));

        assert_eq!(payload.provenance, ProjectionProvenance::Extracted);
        assert_eq!(payload.confidence, Some(1.0));
        assert_eq!(payload.source_system, SOURCE_SYSTEM_GCODE);
        assert_eq!(payload.source_file_path, "src/lib.rs");
        assert_eq!(payload.source_line_start, 3);
        assert_eq!(payload.source_line_end, 5);
        assert_eq!(payload.source_byte_start, 10);
        assert_eq!(payload.source_byte_end, 40);
        assert_eq!(payload.source_line, 3);
        assert_eq!(payload.source_symbol_id, "symbol-1");
        assert_eq!(payload.summary.as_deref(), Some("does work"));
        assert_eq!(payload.signature, None);
        assert_eq!(payload.docstring, None);

        let value = serde_json::to_value(payload).expect("payload serializes");
        assert_eq!(value["provenance"], "EXTRACTED");
        assert_eq!(value["confidence"], 1.0);
        assert_eq!(value["source_system"], SOURCE_SYSTEM_GCODE);
        assert_eq!(value["source_file_path"], "src/lib.rs");
        assert_eq!(value["source_line_start"], 3);
        assert_eq!(value["source_line_end"], 5);
        assert_eq!(value["source_byte_start"], 10);
        assert_eq!(value["source_byte_end"], 40);
        assert_eq!(value["source_symbol_id"], "symbol-1");
    }

    #[test]
    fn summaries_are_optional_enrichment() {
        let symbol = test_symbol(None);
        let payload = CodeSymbolVectorPayload::from_symbol(&symbol);
        let vector_text = vector_text_for_symbol(&symbol);
        let value = serde_json::to_value(payload).expect("payload serializes");

        assert!(value.get("summary").is_none());
        assert!(vector_text.contains("name: run"));
        assert!(!vector_text.contains("summary:"));
    }

    #[test]
    fn collection_name_compatibility() {
        assert_eq!(
            collection_name(CODE_SYMBOL_COLLECTION_PREFIX, "project-1"),
            "code_symbols_project-1"
        );
    }

    #[test]
    fn embedding_request_response() {
        let (base_url, handle) = spawn_http_responses(vec![(
            200,
            json!({"data": [{"embedding": [0.25, 0.5, 0.75]}]}),
        )]);
        let config = EmbeddingConfig {
            api_base: format!("{base_url}/v1"),
            model: "embed-small".to_string(),
            api_key: Some("embedding-key".to_string()),
        };

        let embedding = embed_text(&config, "dimension_probe").expect("embedding response");
        let requests = handle.join().expect("server thread");

        assert_eq!(embedding, vec![0.25, 0.5, 0.75]);
        assert_eq!(requests.len(), 1);
        assert!(requests[0].contains("POST /v1/embeddings HTTP/1.1"));
        assert!(requests[0].contains("authorization: Bearer embedding-key"));
        assert!(requests[0].contains(r#""model":"embed-small""#));
        assert!(requests[0].contains(r#""input":"dimension_probe""#));
    }

    #[test]
    fn ensure_collection_resolves_vector_size_and_distance() {
        let (embedding_url, embedding_handle) = spawn_http_responses(vec![(
            200,
            json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
        )]);
        let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
            (404, json!({"status": "not found"})),
            (200, json!({"result": true})),
            (
                200,
                json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
            ),
        ]);
        let mut lifecycle = CodeSymbolVectorLifecycle::new(
            "project-1".to_string(),
            QdrantConfig {
                url: Some(qdrant_url),
                api_key: None,
            },
            EmbeddingConfig {
                api_base: format!("{embedding_url}/v1"),
                model: "embed-small".to_string(),
                api_key: None,
            },
            CodeVectorSettings { vector_dim: None },
        )
        .expect("lifecycle");

        let created = lifecycle.ensure_collection().expect("create collection");
        let reused = lifecycle.ensure_collection().expect("reuse collection");
        let embedding_requests = embedding_handle.join().expect("embedding requests");
        let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

        assert_eq!(created.size, 3);
        assert_eq!(created.distance, VECTOR_DISTANCE_COSINE);
        assert_eq!(reused.size, 3);
        assert_eq!(embedding_requests.len(), 1, "dimension probe is cached");
        assert!(qdrant_requests[1].contains("PUT /collections/code_symbols_project-1 HTTP/1.1"));
        assert!(qdrant_requests[1].contains(r#""size":3"#));
        assert!(qdrant_requests[1].contains(r#""distance":"Cosine""#));

        let (explicit_qdrant_url, explicit_handle) = spawn_http_responses(vec![
            (404, json!({"status": "not found"})),
            (200, json!({"result": true})),
        ]);
        let mut explicit = CodeSymbolVectorLifecycle::new(
            "project-1".to_string(),
            QdrantConfig {
                url: Some(explicit_qdrant_url),
                api_key: None,
            },
            EmbeddingConfig {
                api_base: "http://127.0.0.1:9/v1".to_string(),
                model: "unused".to_string(),
                api_key: None,
            },
            CodeVectorSettings {
                vector_dim: Some(1536),
            },
        )
        .expect("lifecycle with explicit size");

        let schema = explicit.ensure_collection().expect("explicit size create");
        let explicit_requests = explicit_handle.join().expect("explicit qdrant requests");
        assert_eq!(schema.size, 1536);
        assert!(explicit_requests[1].contains(r#""size":1536"#));
    }

    #[test]
    fn lifecycle_http_scoped_to_module() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let src_dir = manifest_dir.join("src");
        let mut offenders = Vec::new();

        fn visit(path: &std::path::Path, offenders: &mut Vec<std::path::PathBuf>) {
            for entry in std::fs::read_dir(path).expect("read source directory") {
                let entry = entry.expect("source entry");
                let path = entry.path();
                if path.is_dir() {
                    visit(&path, offenders);
                    continue;
                }
                if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                    continue;
                }
                let source = std::fs::read_to_string(&path).expect("read source file");
                let lifecycle_rest = [
                    "/points/delete",
                    "points/delete",
                    "collections/{collection}",
                    "/collections/{collection}",
                ];
                if lifecycle_rest.iter().any(|needle| source.contains(needle))
                    && !path.ends_with("vector/code_symbols.rs")
                {
                    offenders.push(path);
                }
            }
        }

        visit(&src_dir, &mut offenders);
        assert!(
            offenders.is_empty(),
            "Qdrant lifecycle REST must stay scoped to vector/code_symbols.rs: {offenders:?}"
        );
    }

    #[test]
    fn routes_through_gobby_core_qdrant() {
        let source = include_str!("code_symbols.rs");
        assert!(source.contains("gobby_core::config::resolve_qdrant_config"));
        assert!(source.contains("gobby_core::qdrant::with_qdrant"));
        assert!(source.contains("gobby_core::qdrant::collection_name"));
        assert!(source.contains("CollectionScope::Custom"));
        assert!(source.contains("gobby_core::qdrant::search"));
        assert!(source.contains("gobby_core::qdrant::upsert"));
    }

    fn spawn_http_responses(
        responses: Vec<(u16, Value)>,
    ) -> (String, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let addr = listener.local_addr().expect("local addr");
        let handle = thread::spawn(move || {
            let mut requests = Vec::new();
            for (status, body) in responses {
                let (mut stream, _) = listener.accept().expect("accept request");
                requests.push(read_http_request(&mut stream));

                let body = body.to_string();
                write!(
                    stream,
                    "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                )
                .expect("write response");
            }
            requests
        });

        (format!("http://{addr}"), handle)
    }

    fn read_http_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut buffer = [0; 4096];
        let mut expected_len = None;

        loop {
            let n = stream.read(&mut buffer).expect("read request");
            if n == 0 {
                break;
            }
            request.extend_from_slice(&buffer[..n]);

            if expected_len.is_none()
                && let Some(header_end) =
                    request.windows(4).position(|window| window == b"\r\n\r\n")
            {
                let headers = String::from_utf8_lossy(&request[..header_end]);
                let content_len = headers
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length: ")
                            .and_then(|value| value.parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                expected_len = Some(header_end + 4 + content_len);
            }

            if let Some(expected_len) = expected_len
                && request.len() >= expected_len
            {
                break;
            }
        }

        String::from_utf8_lossy(&request).into_owned()
    }
}
