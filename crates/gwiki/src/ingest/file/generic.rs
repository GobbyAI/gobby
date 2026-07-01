use std::path::Path;

use crate::WikiError;
use crate::ingest::{IngestResult, markdown_title, write_asset, write_raw_markdown};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef, SourceKind, SourceManifest};

use super::LocalFileIngestResult;
use super::render::render_file_markdown;
use super::source::{read_source_file, should_store_asset};

pub(super) fn ingest_generic_file_without_index(
    vault_root: &Path,
    kind: &SourceKind,
    file_name: &str,
    location: &str,
    path: &Path,
    fetched_at: &str,
) -> Result<LocalFileIngestResult, WikiError> {
    let bytes = read_source_file(path)?;
    let title = markdown_title(file_name);
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: location.to_string(),
            kind: kind.clone(),
            fetched_at: fetched_at.to_string(),
            content: &bytes,
            title: Some(title.clone()),
            citation: Some(location.to_string()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let asset_path = should_store_asset(kind, bytes.len())
        .then(|| write_asset(vault_root, &record, file_name, &bytes))
        .transpose()?;
    let markdown = render_file_markdown(
        &title,
        location,
        fetched_at,
        &record.content_hash,
        kind,
        &bytes,
        asset_path.as_deref(),
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(LocalFileIngestResult {
        result: IngestResult {
            record,
            raw_path,
            asset_path,
        },
        degradations: Vec::new(),
    })
}
