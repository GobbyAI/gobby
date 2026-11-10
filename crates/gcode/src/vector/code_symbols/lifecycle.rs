use std::path::PathBuf;
use std::time::Duration;

use reqwest::StatusCode;
use serde_json::{Map, Value, json};

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorSettings, QdrantConfig};
use crate::models::Symbol;
use gobby_core::degradation::ServiceState;
use gobby_core::qdrant::UpsertRequest;

use super::embedding::{EmbeddingBackend, EmbeddingSource, dimension_probe_text};
use super::qdrant::{
    VECTOR_DISTANCE_COSINE, collection_name, collection_path, delete_project_collection,
    delete_vectors_for_filter, delete_vectors_for_filter_excluding_ids, parse_collection_schema,
    qdrant_http_error, qdrant_request_for_config,
};
use super::types::{
    CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleOutput, CodeSymbolVectorPayload,
    ExistingVectorCollectionSchema, VectorCollectionSchema, VectorLifecycleError,
};
use super::vector_text::vector_text_for_symbol_in;

const QDRANT_LIFECYCLE_TIMEOUT: Duration = Duration::from_secs(10);
const VECTOR_SYNC_BATCH_SIZE: usize = gobby_core::qdrant::DEFAULT_UPSERT_BATCH_SIZE;

/// What a rebuild did with one symbol, reported once its round is upserted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RebuiltSymbol {
    /// Its vector is in the collection.
    Upserted,
    /// Its source no longer matches the index, so it has no vector.
    Skipped,
}

#[derive(Debug)]
pub struct CodeSymbolVectorLifecycle {
    project_id: String,
    collection: String,
    qdrant: QdrantConfig,
    embedding: EmbeddingBackend,
    settings: CodeVectorSettings,
    /// Symbol paths resolve against this root when embedding source bodies.
    project_root: PathBuf,
    probed_vector_size: Option<usize>,
    client: reqwest::blocking::Client,
}

impl CodeSymbolVectorLifecycle {
    pub fn new(
        project_id: String,
        qdrant: QdrantConfig,
        embedding: impl Into<EmbeddingSource>,
        settings: CodeVectorSettings,
    ) -> Result<Self, VectorLifecycleError> {
        Self::require_qdrant_boundary_config(&qdrant)?;

        let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &project_id)?;
        let embedding = EmbeddingBackend::new(embedding.into())?;
        let client =
            gobby_core::loopback::dial_localhost_as_loopback(reqwest::blocking::Client::builder())
                .timeout(QDRANT_LIFECYCLE_TIMEOUT)
                .build()
                .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        Ok(Self {
            project_id,
            collection,
            qdrant,
            embedding,
            settings,
            project_root: PathBuf::new(),
            probed_vector_size: None,
            client,
        })
    }

    /// Resolve symbol file paths under `project_root` when embedding bodies.
    pub fn with_project_root(mut self, project_root: PathBuf) -> Self {
        self.project_root = project_root;
        self
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

    /// Whether the project's collection already exists in Qdrant.
    pub fn collection_exists(&mut self) -> Result<bool, VectorLifecycleError> {
        self.require_qdrant_boundary()?;
        Ok(self.get_collection_schema()?.is_some())
    }

    pub fn sync_file_symbols(
        &mut self,
        file_path: &str,
        symbols: &[Symbol],
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        let schema = self.ensure_collection()?;
        let points = self.points_for_symbols(symbols, schema.size, &mut |_| {})?;
        let vectors_upserted = self.upsert_points(points)?;

        Ok(self.output(
            CodeSymbolVectorLifecycleAction::SyncFile,
            Some(file_path.to_string()),
            symbols.len(),
            vectors_upserted,
            0,
        ))
    }

    /// Deletes this project's vector points for `file_path` whose ids are not in `keep_ids`.
    pub fn prune_file_points(
        &self,
        file_path: &str,
        keep_ids: &[String],
    ) -> Result<usize, VectorLifecycleError> {
        self.require_qdrant_boundary()?;
        delete_vectors_for_filter_excluding_ids(
            &self.client,
            &self.qdrant,
            &self.collection,
            &self.project_id,
            Some(file_path),
            keep_ids,
        )
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

    /// Delete the project's whole Qdrant collection.
    ///
    /// For purging an indexed project: no schema probe, so a collection left by an
    /// older embedding dimension still goes away, and a missing collection is a
    /// no-op rather than an error.
    pub fn drop_project_collection(
        &self,
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        self.require_qdrant_boundary()?;
        let deleted = delete_project_collection(&self.qdrant, &self.project_id)?;
        Ok(self.output(CodeSymbolVectorLifecycleAction::Clear, None, 0, 0, deleted))
    }

    /// Rebuild vectors for `symbols`, calling `on_done` once per symbol, with
    /// its outcome, after its round is upserted.
    ///
    /// Each round embeds as many batches in parallel as the embedding backend
    /// allows (`ai.max_concurrency` on the daemon route) and
    /// upserts them before the next round, so an interrupted rebuild keeps
    /// the vectors it finished. A symbol whose file no longer matches the index is
    /// skipped and counted; the next index refresh re-syncs that file.
    pub fn rebuild_symbols(
        &mut self,
        symbols: &[Symbol],
        on_done: &mut dyn FnMut(&Symbol, RebuiltSymbol),
    ) -> Result<CodeSymbolVectorLifecycleOutput, VectorLifecycleError> {
        let schema = self.ensure_collection()?;
        let mut vectors_upserted = 0;
        let mut skipped = 0;
        let round_size = VECTOR_SYNC_BATCH_SIZE * self.embedding.max_concurrency().max(1);
        for round in symbols.chunks(round_size) {
            let mut batches: Vec<(Vec<&Symbol>, Vec<String>)> = Vec::new();
            let mut round_skipped: Vec<&Symbol> = Vec::new();
            for batch in round.chunks(VECTOR_SYNC_BATCH_SIZE) {
                let mut kept = Vec::with_capacity(batch.len());
                let mut texts = Vec::with_capacity(batch.len());
                for symbol in batch {
                    match vector_text_for_symbol_in(&self.project_root, symbol) {
                        Ok(text) => {
                            kept.push(symbol);
                            texts.push(text);
                        }
                        Err(_) => round_skipped.push(symbol),
                    }
                }
                if !kept.is_empty() {
                    batches.push((kept, texts));
                }
            }
            let embedding = &self.embedding;
            let results = std::thread::scope(|scope| {
                let workers: Vec<_> = batches
                    .iter()
                    .map(|(_, texts)| scope.spawn(move || embedding.embed_text_batch(texts)))
                    .collect();
                workers
                    .into_iter()
                    .map(|worker| {
                        worker.join().unwrap_or_else(|_| {
                            Err(VectorLifecycleError::EmbeddingResponse(
                                "embedding worker panicked".to_string(),
                            ))
                        })
                    })
                    .collect::<Vec<_>>()
            });
            let mut points = Vec::with_capacity(round.len());
            for ((kept, _), vectors) in batches.iter().zip(results) {
                points.extend(points_from_vectors(kept, vectors?, schema.size)?);
            }
            vectors_upserted += self.upsert_points(points)?;
            for (kept, _) in &batches {
                kept.iter()
                    .for_each(|symbol| on_done(symbol, RebuiltSymbol::Upserted));
            }
            skipped += round_skipped.len();
            round_skipped
                .into_iter()
                .for_each(|symbol| on_done(symbol, RebuiltSymbol::Skipped));
        }

        let mut output = self.output(
            CodeSymbolVectorLifecycleAction::Rebuild,
            None,
            symbols.len(),
            vectors_upserted,
            0,
        );
        if skipped > 0 {
            output.summary.push_str(&format!(
                ", {skipped} symbol(s) skipped because their source no longer matches the index"
            ));
        }
        Ok(output)
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
        Self::require_qdrant_boundary_config(&self.qdrant)
    }

    fn require_qdrant_boundary_config(qdrant: &QdrantConfig) -> Result<(), VectorLifecycleError> {
        let ((), state) = gobby_core::qdrant::with_qdrant(Some(qdrant), (), |_| Ok(()))
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
        let mut retries = 0;
        let resp = loop {
            let resp = self
                .qdrant_request(reqwest::Method::GET, &collection_path(&self.collection))?
                .send()
                .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
            if !resp.status().is_server_error() || retries == 3 {
                break resp;
            }
            // Concurrent creation can expose a collection before its replica is readable.
            drop(resp);
            std::thread::sleep(Duration::from_millis(100 << retries));
            retries += 1;
        };
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
        let status = resp.status();
        if status == StatusCode::CONFLICT {
            let conflict = qdrant_http_error("create collection", status, resp);
            // Another sync process may have created the collection after our GET.
            // Reuse it only after verifying the installed schema.
            return match self.get_collection_schema()? {
                Some(found) => self
                    .ensure_compatible_schema(schema.clone(), found)
                    .map(|_| ()),
                None => Err(conflict),
            };
        }
        if !status.is_success() {
            return Err(qdrant_http_error("create collection", status, resp));
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

    fn upsert_points(&self, points: Vec<UpsertRequest>) -> Result<usize, VectorLifecycleError> {
        if points.is_empty() {
            return Ok(0);
        }
        let requested = points.len();
        let (_result, state) =
            gobby_core::qdrant::with_qdrant(Some(&self.qdrant), None, |config| {
                gobby_core::qdrant::upsert_batched(config, &self.collection, points).map(Some)
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
        on_embedded: &mut dyn FnMut(&Symbol),
    ) -> Result<Vec<UpsertRequest>, VectorLifecycleError> {
        if symbols.is_empty() {
            return Ok(Vec::new());
        }

        let mut points = Vec::with_capacity(symbols.len());
        for batch in symbols.chunks(VECTOR_SYNC_BATCH_SIZE) {
            let texts = batch
                .iter()
                .map(|symbol| {
                    vector_text_for_symbol_in(&self.project_root, symbol).map_err(|err| {
                        match err.downcast::<VectorLifecycleError>() {
                            Ok(typed) => typed,
                            Err(err) => VectorLifecycleError::EmbeddingInput(format!(
                                "symbol {}: {err:#}",
                                symbol.id
                            )),
                        }
                    })
                })
                .collect::<Result<Vec<_>, _>>()?;
            let vectors = self.embedding.embed_text_batch(&texts)?;
            let batch: Vec<&Symbol> = batch.iter().collect();
            points.extend(points_from_vectors(&batch, vectors, expected_vector_size)?);
            batch.iter().for_each(|symbol| on_embedded(symbol));
        }

        Ok(points)
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

/// Pairs one embedding batch with its symbols, checking count and dimension.
fn points_from_vectors(
    symbols: &[&Symbol],
    vectors: Vec<Vec<f32>>,
    expected_vector_size: usize,
) -> Result<Vec<UpsertRequest>, VectorLifecycleError> {
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
            Ok(UpsertRequest {
                id: symbol.id.clone(),
                vector,
                payload: payload_map(CodeSymbolVectorPayload::from_symbol(symbol))?,
            })
        })
        .collect()
}
