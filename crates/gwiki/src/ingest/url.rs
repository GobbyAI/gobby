mod fetch;
mod render;

#[cfg(test)]
mod tests;

use std::path::Path;

use scraper::Html;

use crate::WikiError;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_title, text_from_utf8_lossy, write_asset,
    write_raw_markdown,
};
use crate::sources::{SourceDraft, SourceManifest};
use crate::store::WikiIndexStore;

use self::render::{
    extract_title, file_name_for_url_response, render_non_html_url_markdown, render_url_markdown,
    snapshot_is_html, source_kind_for_url_response,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UrlSnapshot {
    pub requested_url: String,
    pub final_url: String,
    pub fetched_at: String,
    pub body: Vec<u8>,
    pub content_type: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptedUrlIngest {
    pub requested_url: String,
    pub final_url: String,
    pub result: IngestResult,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UrlIngestFailure {
    pub url: String,
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UrlBatchIngest {
    pub accepted: Vec<AcceptedUrlIngest>,
    pub failed: Vec<UrlIngestFailure>,
}

impl UrlBatchIngest {
    pub fn status(&self) -> &'static str {
        match (self.accepted.is_empty(), self.failed.is_empty()) {
            (false, true) => "ingested",
            (false, false) => "partial",
            (true, _) => "failed",
        }
    }

    pub fn exit_code(&self) -> u8 {
        u8::from(self.accepted.is_empty())
    }
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_snapshot(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: UrlSnapshot,
) -> Result<IngestResult, WikiError> {
    let result = ingest_snapshot_without_index(vault_root, snapshot)?;
    index_after_ingest(
        vault_root,
        store,
        &mut crate::progress::ProgressOptions::default(),
    )?;

    Ok(result)
}

pub(crate) fn ingest_snapshot_without_index(
    vault_root: &Path,
    mut snapshot: UrlSnapshot,
) -> Result<IngestResult, WikiError> {
    if !snapshot_is_html(&snapshot) {
        return ingest_non_html_snapshot_without_index(vault_root, snapshot);
    }

    let html = text_from_utf8_lossy(&snapshot.body);
    let source_hash = gobby_core::indexing::content_hash(&snapshot.body);
    let document = Html::parse_document(&html);
    let title = extract_title(&document).unwrap_or_else(|| snapshot.final_url.clone());
    let draft = SourceDraft::url(
        snapshot.final_url.clone(),
        snapshot.fetched_at.clone(),
        std::mem::take(&mut snapshot.body),
    )
    .with_title(markdown_title(&title))
    .with_citation(snapshot.final_url.clone());
    let record = SourceManifest::register(vault_root, draft)?;
    // Render with the record's stored capture time: an unchanged re-ingest
    // dedups to the existing record, and reproducing its original bytes keeps
    // the immutable raw write idempotent instead of failing on a timestamp
    // drift (#17644).
    snapshot.fetched_at = record.fetched_at.clone();
    let markdown = render_url_markdown(
        &snapshot,
        &record.canonical_location,
        &title,
        &document,
        &source_hash,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

fn ingest_non_html_snapshot_without_index(
    vault_root: &Path,
    mut snapshot: UrlSnapshot,
) -> Result<IngestResult, WikiError> {
    let source_hash = gobby_core::indexing::content_hash(&snapshot.body);
    let kind = source_kind_for_url_response(snapshot.content_type.as_deref());
    let title = markdown_title(&file_name_for_url_response(&snapshot, &kind));
    let body = std::mem::take(&mut snapshot.body);
    let draft = SourceDraft::new(
        snapshot.final_url.clone(),
        kind.clone(),
        snapshot.fetched_at.clone(),
        body.clone(),
    )
    .with_title(title.clone())
    .with_citation(snapshot.final_url.clone());
    let record = SourceManifest::register(vault_root, draft)?;
    let asset_path = write_asset(vault_root, &record, &title, &body)?;
    // Reproduce the record's original bytes on unchanged re-ingest (see the
    // HTML path above).
    snapshot.fetched_at = record.fetched_at.clone();
    let markdown = render_non_html_url_markdown(
        &snapshot,
        &record.canonical_location,
        &title,
        &kind,
        &source_hash,
        &asset_path,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: Some(asset_path),
    })
}

pub(crate) fn ingest_urls(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    urls: &[String],
    fetched_at: &str,
    progress: &mut crate::progress::ProgressOptions<'_>,
) -> Result<UrlBatchIngest, WikiError> {
    ingest_urls_with_fetcher(
        vault_root,
        store,
        urls,
        fetched_at,
        fetch::fetch_url_snapshot,
        progress,
    )
}

pub(crate) fn fetch_url_snapshot(
    url: &str,
    fetched_at: &str,
) -> Result<UrlSnapshot, UrlIngestFailure> {
    fetch::fetch_url_snapshot(url, fetched_at)
}

pub(crate) fn ingest_urls_with_fetcher(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    urls: &[String],
    fetched_at: &str,
    mut fetch: impl FnMut(&str, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
    progress: &mut crate::progress::ProgressOptions<'_>,
) -> Result<UrlBatchIngest, WikiError> {
    if urls.is_empty() {
        return Err(WikiError::InvalidInput {
            field: "ingest-url",
            message: "at least one URL is required".to_string(),
        });
    }

    let mut accepted = Vec::new();
    let mut failed = Vec::new();
    let mut ingest_progress = crate::progress::ActiveProgress::new(
        progress,
        crate::progress::ProgressPhase::IngestUrl,
        urls.len(),
    );
    for url in urls {
        match fetch(url, fetched_at) {
            Ok(snapshot) => {
                let requested_url = snapshot.requested_url.clone();
                let final_url = snapshot.final_url.clone();
                // Re-ingesting a URL whose content changed supersedes the
                // stale manifest record, matching local-file re-capture
                // semantics (#17644). Refresh performs its own supersede and
                // calls `ingest_snapshot_without_index` directly.
                match ingest_snapshot_without_index(vault_root, snapshot).and_then(|result| {
                    SourceManifest::supersede_location(vault_root, &result.record)?;
                    Ok(result)
                }) {
                    Ok(result) => accepted.push(AcceptedUrlIngest {
                        requested_url,
                        final_url,
                        result,
                    }),
                    Err(error) => failed.push(UrlIngestFailure::from_wiki_error(url, error)),
                }
            }
            Err(error) => failed.push(error),
        }
        ingest_progress.advance(url);
    }
    drop(ingest_progress);

    if !accepted.is_empty() {
        index_after_ingest(vault_root, store, progress)?;
    }

    Ok(UrlBatchIngest { accepted, failed })
}
