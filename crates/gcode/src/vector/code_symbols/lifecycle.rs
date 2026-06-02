use reqwest::StatusCode;
use serde_json::{Map, Value, json};

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorSettings, QdrantConfig};
use crate::models::Symbol;
use gobby_core::degradation::ServiceState;
use gobby_core::qdrant::UpsertRequest;

use super::embedding::{
    EmbeddingBackend, EmbeddingSource, dimension_probe_text, vector_text_for_symbol,
};
use super::qdrant::{
    VECTOR_DISTANCE_COSINE, collection_name, collection_path, delete_vectors_for_filter,
    delete_vectors_for_filter_excluding_ids, parse_collection_schema, qdrant_http_error,
    qdrant_request_for_config,
};
use super::types::{
    CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleOutput,
    CodeSymbolVectorLifecycleStatus, CodeSymbolVectorPayload, ExistingVectorCollectionSchema,
    VectorCollectionSchema, VectorLifecycleError,
};

#[derive(Debug)]
pub struct CodeSymbolVectorLifecycle {
    project_id: String,
    collection: String,
    qdrant: QdrantConfig,
    embedding: EmbeddingBackend,
    settings: CodeVectorSettings,
    probed_vector_size: Option<usize>,
    client: reqwest::blocking::Client,
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

impl CodeSymbolVectorLifecycle {
    pub fn new(
        project_id: String,
        qdrant: QdrantConfig,
        embedding: impl Into<EmbeddingSource>,
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

        let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &project_id);
        let embedding = EmbeddingBackend::new(embedding.into())?;
        let client = reqwest::blocking::Client::new();
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
        let schema = self.ensure_collection()?;
        let points = self.points_for_symbols(symbols, schema.size)?;
        let point_ids = point_ids(&points);
        let vectors_upserted = self.upsert_points(points)?;
        let delete_operations_issued = self.delete_stale_vectors(Some(file_path), &point_ids)?;

        Ok(self.output(
            CodeSymbolVectorLifecycleAction::SyncFile,
            Some(file_path.to_string()),
            symbols.len(),
            vectors_upserted,
            delete_operations_issued,
        ))
    }

    pub fn clear_project_vectors(
        &mut self,
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        self.require_qdrant_boundary()?;
        let deleted = match self.get_collection_schema()? {
            Some(found) => {
                if let Some(size) = self.settings.vector_dim {
                    self.ensure_compatible_schema(
                        VectorCollectionSchema {
                            size,
                            distance: VECTOR_DISTANCE_COSINE.to_string(),
                        },
                        found,
                    )?;
                }
                self.delete_vectors(None)?
            }
            None => 0,
        };

        Ok(self.output(CodeSymbolVectorLifecycleAction::Clear, None, 0, 0, deleted))
    }

    pub fn rebuild_symbols(
        &mut self,
        symbols: &[Symbol],
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        let schema = self.ensure_collection()?;
        let points = self.points_for_symbols(symbols, schema.size)?;
        let point_ids = point_ids(&points);
        let vectors_upserted = self.upsert_points(points)?;
        let delete_operations_issued = self.delete_stale_vectors(None, &point_ids)?;

        Ok(self.output(
            CodeSymbolVectorLifecycleAction::Rebuild,
            None,
            symbols.len(),
            vectors_upserted,
            delete_operations_issued,
        ))
    }

    fn output(
        &self,
        action: CodeSymbolVectorLifecycleAction,
        file_path: Option<String>,
        symbols: usize,
        vectors_upserted: usize,
        delete_operations_issued: usize,
    ) -> CodeSymbolVectorLifecycleOutput {
        CodeSymbolVectorLifecycleOutput {
            project_id: self.project_id.clone(),
            collection: self.collection.clone(),
            action,
            file_path,
            symbols,
            vectors_upserted,
            delete_operations_issued,
            summary: format!(
                "{vectors_upserted} vector(s) upserted, {delete_operations_issued} delete operation(s) issued"
            ),
        }
    }

    fn expected_schema(&mut self) -> Result<VectorCollectionSchema, VectorLifecycleError> {
        let size = match self.settings.vector_dim {
            Some(size) => size,
            None => match self.probed_vector_size {
                Some(size) => size,
                None => {
                    let size = self.embedding.embed_text(dimension_probe_text())?.len();
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
            .qdrant_request(reqwest::Method::GET, &collection_path(&self.collection))?
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
            .qdrant_request(reqwest::Method::PUT, &collection_path(&self.collection))?
            .json(&body)
            .send()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        if !resp.status().is_success() {
            return Err(qdrant_http_error("create collection", resp.status(), resp));
        }
        Ok(())
    }

    fn delete_vectors(&self, file_path: Option<&str>) -> Result<usize, VectorLifecycleError> {
        delete_vectors_for_filter(
            &self.client,
            &self.qdrant,
            &self.collection,
            &self.project_id,
            file_path,
        )
    }

    fn delete_stale_vectors(
        &self,
        file_path: Option<&str>,
        keep_point_ids: &[String],
    ) -> Result<usize, VectorLifecycleError> {
        delete_vectors_for_filter_excluding_ids(
            &self.client,
            &self.qdrant,
            &self.collection,
            &self.project_id,
            file_path,
            keep_point_ids,
        )
    }

    fn upsert_points(&self, points: Vec<UpsertRequest>) -> Result<usize, VectorLifecycleError> {
        if points.is_empty() {
            return Ok(0);
        }
        let requested = points.len();
        let ((), state) = gobby_core::qdrant::with_qdrant(Some(&self.qdrant), (), |config| {
            gobby_core::qdrant::upsert(config, &self.collection, points)
        })
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        match state {
            ServiceState::Available => Ok(requested),
            ServiceState::NotConfigured => Err(VectorLifecycleError::MissingQdrantConfig),
            other => Err(VectorLifecycleError::QdrantOperation(format!(
                "unexpected Qdrant service state: {other:?}"
            ))),
        }
    }

    fn points_for_symbols(
        &self,
        symbols: &[Symbol],
        expected_vector_size: usize,
    ) -> Result<Vec<UpsertRequest>, VectorLifecycleError> {
        if symbols.is_empty() {
            return Ok(Vec::new());
        }

        let texts = symbols
            .iter()
            .map(vector_text_for_symbol)
            .collect::<Vec<_>>();
        let vectors = self.embedding.embed_text_batch(&texts)?;
        if vectors.len() != symbols.len() {
            return Err(VectorLifecycleError::EmbeddingResponse(format!(
                "embedding batch returned {} vector(s) for {} symbol(s)",
                vectors.len(),
                symbols.len()
            )));
        }
        symbols
            .iter()
            .zip(vectors)
            .map(|(symbol, vector)| {
                if vector.len() != expected_vector_size {
                    return Err(VectorLifecycleError::EmbeddingResponse(format!(
                        "embedding for symbol {} returned {} dimension(s), expected {}",
                        symbol.id,
                        vector.len(),
                        expected_vector_size
                    )));
                }
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
        qdrant_request_for_config(&self.client, &self.qdrant, method, path)
    }
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

fn point_ids(points: &[UpsertRequest]) -> Vec<String> {
    points.iter().map(|point| point.id.clone()).collect()
}
