mod degradation;
mod dispatch;
mod generic;
mod render;
mod replay;
mod source;

#[cfg(test)]
mod tests;

use std::path::Path;

use gobby_core::ai_context::AiContext;

use crate::api::IngestFileOptions;
use crate::ingest::{IngestResult, index_after_ingest, markdown_title, write_raw_markdown};
use crate::sources::{SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::{ScopeIdentity, WikiError};

pub(crate) use dispatch::ingest_path_without_index;

/// TEXT_INLINE_LIMIT_BYTES keeps ordinary text inline while pushing unusually
/// large text into raw assets. The 256 KB threshold is above typical note and
/// config sizes but below the point where markdown rendering, chunking, memory
/// use, search indexing, and API transfer costs start to dominate an ingest.
/// Lower it in production if inline text drives slow indexing or high network
/// transfer; raise it only when metrics show asset indirection is costing more
/// than the extra memory and I/O.
const TEXT_INLINE_LIMIT_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub struct StdinSnapshot {
    pub label: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LocalFileIngestResult {
    pub result: IngestResult,
    pub degradations: Vec<String>,
}

pub fn ingest_path(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    ai_context: &AiContext,
    options: &IngestFileOptions,
    path: &Path,
    fetched_at: &str,
) -> Result<IngestResult, WikiError> {
    let result =
        ingest_path_without_index(vault_root, scope, ai_context, options, path, fetched_at)?;
    index_after_ingest(vault_root, store)?;
    Ok(result.result)
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_stdin(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: StdinSnapshot,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.label);
    let location = format!("stdin:{}", snapshot.label);
    let draft = SourceDraft::new(
        location.clone(),
        SourceKind::Stdin,
        snapshot.fetched_at.clone(),
        snapshot.bytes.clone(),
    )
    .with_title(title.clone());
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render::render_file_markdown(
        &title,
        &location,
        &snapshot.fetched_at,
        &record.content_hash,
        &SourceKind::Stdin,
        &snapshot.bytes,
        None,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}
