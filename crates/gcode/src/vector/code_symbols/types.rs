use serde::{Deserialize, Serialize};
use std::fmt;

use crate::models::{ProjectionMetadata, ProjectionProvenance, Symbol};

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
pub(super) struct ExistingVectorCollectionSchema {
    pub(super) size: Option<usize>,
    pub(super) distance: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbolVectorLifecycleOutput {
    pub project_id: String,
    pub collection: String,
    pub action: CodeSymbolVectorLifecycleAction,
    #[serde(skip_serializing_if = "Option::is_none")]
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
