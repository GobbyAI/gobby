use std::fs;
use std::path::{Path, PathBuf};

use crate::ScopeIdentity;
use crate::WikiError;
use crate::document::{DocumentDegradation, DocumentFailureMode, DocumentUnitCount};
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_title, write_asset, write_raw_markdown,
};
use crate::sources::{SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::vision::{VisionEndpoint, disabled_degradation};

use super::markdown::{merge_pdf_pages, render_pdf_markdown};
#[cfg(feature = "documents")]
use super::render::{extract_text_layer_pages, render_pdf_pages};
use super::text::normalize_page_text;
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
        VisionEndpoint::Unavailable(disabled_degradation()),
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
    let previous_manifest = SourceManifest::read(vault_root)?;
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let draft = SourceDraft::new(
        snapshot.location.clone(),
        SourceKind::Pdf,
        snapshot.fetched_at.to_rfc3339(),
        Vec::new(),
    )
    .with_title(title.clone())
    .with_citation(snapshot.location.clone());
    let record = SourceManifest::register_with_content_hash(vault_root, draft, content_hash)?;
    let asset_path = match write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes) {
        Ok(asset_path) => asset_path,
        Err(error) => {
            return rollback_registered_pdf_source(vault_root, &previous_manifest, None, error);
        }
    };
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
    let raw_path = match write_raw_markdown(vault_root, &record, &markdown) {
        Ok(raw_path) => raw_path,
        Err(error) => {
            return rollback_registered_pdf_source(
                vault_root,
                &previous_manifest,
                Some(&asset_path),
                error,
            );
        }
    };

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: Some(asset_path),
    })
}

fn rollback_registered_pdf_source<T>(
    vault_root: &Path,
    previous_manifest: &SourceManifest,
    asset_path: Option<&PathBuf>,
    original_error: WikiError,
) -> Result<T, WikiError> {
    let mut cleanup_errors = Vec::new();
    if let Some(asset_path) = asset_path {
        cleanup_pdf_file(vault_root, asset_path, &mut cleanup_errors);
    }
    if let Err(rollback_error) = previous_manifest.write(vault_root) {
        return Err(WikiError::Config {
            detail: format!(
                "failed to roll back source manifest after PDF ingest failure: {rollback_error}; original error: {original_error}{}",
                pdf_cleanup_detail(&cleanup_errors)
            ),
        });
    }
    if cleanup_errors.is_empty() {
        return Err(original_error);
    }
    Err(WikiError::Config {
        detail: format!("{original_error}{}", pdf_cleanup_detail(&cleanup_errors)),
    })
}

fn cleanup_pdf_file(vault_root: &Path, relative_path: &Path, cleanup_errors: &mut Vec<String>) {
    let path = vault_root.join(relative_path);
    match fs::remove_file(&path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => cleanup_errors.push(format!("{}: {error}", path.display())),
    }
}

fn pdf_cleanup_detail(cleanup_errors: &[String]) -> String {
    if cleanup_errors.is_empty() {
        String::new()
    } else {
        format!("; cleanup failures: {}", cleanup_errors.join("; "))
    }
}
