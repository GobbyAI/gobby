use std::path::{Path, PathBuf};

use crate::document::DocumentDegradation;
use crate::ingest::{
    IngestResult, index_after_ingest, lowercase_extension, markdown_title, write_asset,
    write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::{ScopeIdentity, WikiError};

mod html;
mod office;
mod render;

use html::*;
use office::*;
use render::*;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub kind: SourceKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentIngestResult {
    pub record: crate::sources::SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub derived_path: PathBuf,
    pub document_degradation: Option<DocumentDegradation>,
}

impl From<DocumentIngestResult> for IngestResult {
    fn from(result: DocumentIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct DocumentRequest<'a> {
    pub file_name: &'a str,
    pub kind: &'a SourceKind,
    pub bytes: &'a [u8],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentExtraction {
    pub title: Option<String>,
    pub markdown: String,
    pub units_label: &'static str,
    pub units_count: usize,
    pub degradation: Option<DocumentDegradation>,
}

pub trait DocumentExtractor {
    fn extract(&self, request: &DocumentRequest<'_>) -> Result<DocumentExtraction, WikiError>;
}

pub enum DocumentEndpoint<'a> {
    Available(&'a dyn DocumentExtractor),
    Unavailable(DocumentDegradation),
}

struct LocalDocumentExtractor;

pub fn ingest_document(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: DocumentSnapshot,
) -> Result<DocumentIngestResult, WikiError> {
    let result = ingest_document_without_index(vault_root, scope, snapshot)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_document_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    snapshot: DocumentSnapshot,
) -> Result<DocumentIngestResult, WikiError> {
    static EXTRACTOR: LocalDocumentExtractor = LocalDocumentExtractor;
    ingest_document_with_endpoint_without_index(
        vault_root,
        scope,
        snapshot,
        DocumentEndpoint::Available(&EXTRACTOR),
    )
}

pub fn ingest_document_with_endpoint(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: DocumentSnapshot,
    endpoint: DocumentEndpoint<'_>,
) -> Result<DocumentIngestResult, WikiError> {
    let result =
        ingest_document_with_endpoint_without_index(vault_root, scope, snapshot, endpoint)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_document_with_endpoint_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    snapshot: DocumentSnapshot,
    endpoint: DocumentEndpoint<'_>,
) -> Result<DocumentIngestResult, WikiError> {
    let request = DocumentRequest {
        file_name: &snapshot.file_name,
        kind: &snapshot.kind,
        bytes: &snapshot.bytes,
    };
    let (extraction, degradation) = match endpoint {
        DocumentEndpoint::Available(extractor) => match extractor.extract(&request) {
            Ok(extraction) => {
                let degradation = extraction.degradation.clone();
                (Some(extraction), degradation)
            }
            Err(error) => (
                None,
                Some(document_degradation_for_error(&request, error.to_string())),
            ),
        },
        DocumentEndpoint::Unavailable(degradation) => (None, Some(degradation)),
    };
    let title = extraction
        .as_ref()
        .and_then(|value| value.title.clone())
        .unwrap_or_else(|| markdown_title(&snapshot.file_name));
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: snapshot.location.clone(),
            kind: snapshot.kind.clone(),
            fetched_at: snapshot.fetched_at.clone(),
            content: &snapshot.bytes,
            title: Some(title.clone()),
            citation: Some(snapshot.location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    let raw_markdown = render_raw_document_markdown(&snapshot, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let derived_path = write_document_derived_markdown(
        vault_root,
        &scope,
        &record,
        &snapshot,
        &title,
        &asset_path,
        extraction.as_ref(),
        degradation.as_ref(),
    )?;

    Ok(DocumentIngestResult {
        record,
        raw_path,
        asset_path,
        derived_path,
        document_degradation: degradation,
    })
}

impl DocumentExtractor for LocalDocumentExtractor {
    fn extract(&self, request: &DocumentRequest<'_>) -> Result<DocumentExtraction, WikiError> {
        match request.kind {
            SourceKind::Html => extract_html_document(request.bytes),
            SourceKind::Office => extract_office_document(request.file_name, request.bytes),
            _ => Err(document_error(format!(
                "unsupported document kind: {:?}",
                request.kind
            ))),
        }
    }
}

fn extension(file_name: &str) -> Option<String> {
    lowercase_extension(file_name)
}

fn decode_xml_entities(text: &str) -> String {
    html_escape::decode_html_entities(text).into_owned()
}

fn document_error(message: impl Into<String>) -> WikiError {
    WikiError::InvalidInput {
        field: "document",
        message: message.into(),
    }
}

#[cfg(test)]
mod tests;
