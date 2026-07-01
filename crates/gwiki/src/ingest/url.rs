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
    index_after_ingest(vault_root, store)?;

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
) -> Result<UrlBatchIngest, WikiError> {
    ingest_urls_with_fetcher(
        vault_root,
        store,
        urls,
        fetched_at,
        fetch::fetch_url_snapshot,
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
) -> Result<UrlBatchIngest, WikiError> {
    if urls.is_empty() {
        return Err(WikiError::InvalidInput {
            field: "ingest-url",
            message: "at least one URL is required".to_string(),
        });
    }

    let mut accepted = Vec::new();
    let mut failed = Vec::new();
    for url in urls {
        match fetch(url, fetched_at) {
            Ok(snapshot) => {
                let requested_url = snapshot.requested_url.clone();
                let final_url = snapshot.final_url.clone();
                match ingest_snapshot_without_index(vault_root, snapshot) {
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
    }

    if !accepted.is_empty() {
        index_after_ingest(vault_root, store)?;
    }

    Ok(UrlBatchIngest { accepted, failed })
}
