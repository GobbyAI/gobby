use std::path::Path;

use crate::ScopeIdentity;
use crate::WikiError;
use crate::document::{DocumentDegradation, DocumentFailureMode, DocumentUnitCount};
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_title, write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::vision::VisionEndpoint;

use super::markdown::{merge_pdf_pages, render_pdf_markdown};
use super::render::normalize_page_text;
#[cfg(feature = "documents")]
use super::render::{extract_text_layer_pages, render_pdf_pages};
#[cfg(feature = "documents")]
use super::types::{PdfFileSnapshot, PdfIngestOptions};
use super::types::{PdfRenderedPage, PdfSnapshot};

pub fn ingest_pages(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
) -> Result<IngestResult, WikiError> {
    ingest_pages_with_vision(
        vault_root,
        store,
        scope,
        snapshot,
        Vec::new(),
        VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
            reason: "disabled".to_string(),
            fallback: "Keep PDF text layer only.".to_string(),
        }),
    )
}

#[cfg(feature = "documents")]
pub fn ingest_pdf_file(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    snapshot: PdfFileSnapshot,
    endpoint: VisionEndpoint<'_>,
    options: PdfIngestOptions,
) -> Result<IngestResult, WikiError> {
    let result = ingest_pdf_file_without_index(vault_root, scope, snapshot, endpoint, options)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

#[cfg(feature = "documents")]
pub(crate) fn ingest_pdf_file_without_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    snapshot: PdfFileSnapshot,
    endpoint: VisionEndpoint<'_>,
    options: PdfIngestOptions,
) -> Result<IngestResult, WikiError> {
    let mut degradations = Vec::new();
    let pages = match extract_text_layer_pages(&snapshot.bytes) {
        Ok(pages) => pages,
        Err(error) => {
            degradations.push(DocumentDegradation::new(
                DocumentFailureMode::PdfTextLayerError,
                DocumentUnitCount::pages(0),
                format!("PDF text-layer extraction failed: {error}; original asset is preserved."),
            ));
            Vec::new()
        }
    };
    let rendered_pages = match endpoint {
        VisionEndpoint::Available(_) => match render_pdf_pages(&snapshot, options.render_dpi) {
            Ok(outcome) => {
                if let Some(degradation) = outcome.degradation {
                    degradations.push(degradation);
                }
                outcome.pages
            }
            Err(error) => {
                degradations.push(DocumentDegradation::new(
                    DocumentFailureMode::PdfRenderError,
                    DocumentUnitCount::pages(pages.len()),
                    format!("PDF page rendering failed: {error}; original asset is preserved."),
                ));
                Vec::new()
            }
        },
        VisionEndpoint::Unavailable(_) => Vec::new(),
    };

    ingest_pages_with_vision_inner(
        vault_root,
        scope,
        PdfSnapshot {
            location: snapshot.location,
            file_name: snapshot.file_name,
            fetched_at: snapshot.fetched_at,
            bytes: snapshot.bytes,
            pages,
        },
        rendered_pages,
        endpoint,
        degradations,
    )
}

pub fn ingest_pages_with_vision(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
) -> Result<IngestResult, WikiError> {
    let result = ingest_pages_with_vision_without_index(
        vault_root,
        scope,
        snapshot,
        rendered_pages,
        endpoint,
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_pages_with_vision_without_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
) -> Result<IngestResult, WikiError> {
    ingest_pages_with_vision_inner(
        vault_root,
        scope,
        snapshot,
        rendered_pages,
        endpoint,
        Vec::new(),
    )
}

fn ingest_pages_with_vision_inner(
    vault_root: &Path,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
    mut degradations: Vec<DocumentDegradation>,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.file_name);
    let draft = SourceDraft {
        location: snapshot.location.clone(),
        kind: SourceKind::Pdf,
        fetched_at: snapshot.fetched_at.to_rfc3339(),
        content: snapshot.bytes.clone(),
        title: Some(title.clone()),
        citation: Some(snapshot.location.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    if snapshot
        .pages
        .iter()
        .all(|page| normalize_page_text(&page.text).is_empty())
        && rendered_pages.is_empty()
    {
        degradations.push(DocumentDegradation::new(
            DocumentFailureMode::PdfNoExtractableContent,
            DocumentUnitCount::pages(snapshot.pages.len()),
            "PDF contained no extractable text and no usable rendered page vision; original asset is preserved.",
        ));
    }
    let (pages, summary) = merge_pdf_pages(
        &snapshot,
        &asset_path,
        rendered_pages,
        endpoint,
        degradations,
    );
    let markdown = render_pdf_markdown(
        scope,
        &snapshot,
        &title,
        &record.content_hash,
        &asset_path,
        &pages,
        &summary,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: Some(asset_path),
    })
}
