use std::io::Read;
use std::path::Path;
use std::time::Duration;

use scraper::{ElementRef, Html, Node, Selector};

use crate::WikiError;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, single_line,
    text_from_utf8_lossy, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

const URL_FETCH_TIMEOUT: Duration = Duration::from_secs(30);
const USER_AGENT: &str = "gwiki/0.1";

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
    let html = text_from_utf8_lossy(&snapshot.body);
    let source_hash = gobby_core::indexing::content_hash(&snapshot.body);
    let document = Html::parse_document(&html);
    let title = extract_title(&document).unwrap_or_else(|| snapshot.final_url.clone());
    let draft = SourceDraft {
        location: snapshot.final_url.clone(),
        kind: SourceKind::Url,
        fetched_at: snapshot.fetched_at.clone(),
        content: std::mem::take(&mut snapshot.body),
        title: Some(markdown_title(&title)),
        citation: Some(snapshot.final_url.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
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

pub(crate) fn ingest_urls(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    urls: &[String],
    fetched_at: &str,
) -> Result<UrlBatchIngest, WikiError> {
    let fetcher = BlockingUrlFetcher::default();
    ingest_urls_with_fetcher(vault_root, store, urls, fetched_at, |url, fetched_at| {
        fetcher.fetch(url, fetched_at)
    })
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

#[derive(Debug, Clone)]
struct BlockingUrlFetcher {
    agent: ureq::Agent,
}

impl Default for BlockingUrlFetcher {
    fn default() -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(URL_FETCH_TIMEOUT).build(),
        }
    }
}

impl BlockingUrlFetcher {
    fn fetch(&self, url: &str, fetched_at: &str) -> Result<UrlSnapshot, UrlIngestFailure> {
        validate_fetch_url(url)?;
        let response = match self.agent.get(url).set("User-Agent", USER_AGENT).call() {
            Ok(response) => response,
            Err(ureq::Error::Status(status, response)) => {
                return Err(UrlIngestFailure::http_status(url, status, response));
            }
            Err(ureq::Error::Transport(error)) => {
                return Err(UrlIngestFailure::new(
                    url,
                    "transport_error",
                    error.to_string(),
                ));
            }
        };
        let final_url = response.get_url().to_string();
        let content_type = response.header("content-type").map(ToOwned::to_owned);
        let max_bytes = crate::support::env::max_inbox_item_bytes_from_env();
        if content_length_exceeds_limit(response.header("content-length"), max_bytes) {
            return Err(response_too_large(url, max_bytes));
        }
        let body = read_limited_body(response.into_reader(), max_bytes, url)?;

        Ok(UrlSnapshot {
            requested_url: url.to_string(),
            final_url,
            fetched_at: fetched_at.to_string(),
            body,
            content_type,
        })
    }
}

fn content_length_exceeds_limit(content_length: Option<&str>, max_bytes: u64) -> bool {
    content_length
        .and_then(|value| value.trim().parse::<u64>().ok())
        .is_some_and(|length| length > max_bytes)
}

fn read_limited_body(
    reader: impl Read,
    max_bytes: u64,
    url: &str,
) -> Result<Vec<u8>, UrlIngestFailure> {
    let mut body = Vec::new();
    reader
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut body)
        .map_err(|error| UrlIngestFailure::new(url, "read_error", error.to_string()))?;
    if u64::try_from(body.len()).unwrap_or(u64::MAX) > max_bytes {
        return Err(response_too_large(url, max_bytes));
    }
    Ok(body)
}

fn response_too_large(url: &str, max_bytes: u64) -> UrlIngestFailure {
    UrlIngestFailure::new(
        url,
        "response_too_large",
        format!("response exceeds GWIKI_MAX_INBOX_ITEM_BYTES limit of {max_bytes} bytes"),
    )
}

impl UrlIngestFailure {
    fn new(url: impl Into<String>, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            code: code.into(),
            message: message.into(),
        }
    }

    fn from_wiki_error(url: &str, error: WikiError) -> Self {
        Self::new(url, error.code(), error.to_string())
    }

    fn http_status(url: &str, status: u16, response: ureq::Response) -> Self {
        let body = response.into_string().unwrap_or_default();
        let body = single_line(&body);
        let detail = if body.is_empty() {
            format!("HTTP status {status}")
        } else {
            format!("HTTP status {status}: {}", truncate_message(&body))
        };
        Self::new(url, "http_status", detail)
    }
}

fn validate_fetch_url(raw_url: &str) -> Result<(), UrlIngestFailure> {
    let parsed = url::Url::parse(raw_url)
        .map_err(|error| UrlIngestFailure::new(raw_url, "invalid_url", error.to_string()))?;
    if matches!(parsed.scheme(), "http" | "https") {
        Ok(())
    } else {
        Err(UrlIngestFailure::new(
            raw_url,
            "invalid_url",
            format!("unsupported URL scheme `{}`", parsed.scheme()),
        ))
    }
}

fn truncate_message(message: &str) -> String {
    const MAX_CHARS: usize = 200;
    let mut chars = message.chars();
    let truncated = chars.by_ref().take(MAX_CHARS).collect::<String>();
    if chars.next().is_some() {
        format!("{truncated}...")
    } else {
        truncated
    }
}

fn render_url_markdown(
    snapshot: &UrlSnapshot,
    canonical_url: &str,
    title: &str,
    document: &Html,
    source_hash: &str,
) -> String {
    let mut fields = vec![
        ("source_kind", "url".to_string()),
        ("source_url", snapshot.final_url.clone()),
        ("requested_url", snapshot.requested_url.clone()),
        ("canonical_url", canonical_url.to_string()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
    ];
    if let Some(content_type) = &snapshot.content_type {
        fields.push(("content_type", content_type.clone()));
    }
    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(title));
    markdown.push_str("\n\n");
    markdown.push_str(&html_to_markdownish_text(document));
    markdown.push('\n');
    markdown
}

fn extract_title(document: &Html) -> Option<String> {
    let selector = Selector::parse("title").ok()?;
    let title = document
        .select(&selector)
        .next()?
        .text()
        .collect::<Vec<_>>()
        .join(" ");
    let title = single_line(&title);
    (!title.is_empty()).then_some(title)
}

fn html_to_markdownish_text(document: &Html) -> String {
    let mut parts = Vec::new();
    let root = Selector::parse("body")
        .ok()
        .and_then(|selector| document.select(&selector).next())
        .unwrap_or_else(|| document.root_element());
    collect_visible_text(root, &mut parts);
    let text = parts.join("\n");
    normalize_markdown_text(&text)
}

fn collect_visible_text(element: ElementRef<'_>, parts: &mut Vec<String>) {
    if is_hidden_element(element.value().name()) {
        return;
    }
    if is_text_block(element.value().name()) {
        let mut text = String::new();
        collect_inline_text(element, &mut text);
        if !single_line(&text).is_empty() {
            parts.push(text);
        }
        return;
    }

    let mut inline = String::new();
    for child in element.children() {
        match child.value() {
            Node::Text(text) => inline.push_str(&text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    if is_hidden_element(child_element.value().name()) {
                        continue;
                    }
                    if is_text_block(child_element.value().name()) {
                        push_inline_part(&mut inline, parts);
                        collect_visible_text(child_element, parts);
                    } else {
                        collect_inline_text(child_element, &mut inline);
                    }
                }
            }
            _ => {}
        }
    }
    push_inline_part(&mut inline, parts);
}

fn collect_inline_text(element: ElementRef<'_>, output: &mut String) {
    if is_hidden_element(element.value().name()) {
        return;
    }
    for child in element.children() {
        match child.value() {
            Node::Text(text) => output.push_str(&text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    collect_inline_text(child_element, output);
                }
            }
            _ => {}
        }
    }
}

fn push_inline_part(inline: &mut String, parts: &mut Vec<String>) {
    if !single_line(inline).is_empty() {
        parts.push(std::mem::take(inline));
    } else {
        inline.clear();
    }
}

fn is_hidden_element(name: &str) -> bool {
    matches!(name, "head" | "script" | "style")
}

fn is_text_block(name: &str) -> bool {
    matches!(
        name,
        "address"
            | "blockquote"
            | "dd"
            | "dt"
            | "figcaption"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "li"
            | "p"
            | "pre"
            | "td"
            | "th"
    )
}

fn normalize_markdown_text(text: &str) -> String {
    let mut lines = Vec::new();
    for line in text.lines() {
        let line = single_line(line);
        if !line.is_empty() && lines.last().is_none_or(|last: &String| last != &line) {
            lines.push(line);
        }
    }
    lines.join("\n\n")
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::path::Path;
    use std::path::PathBuf;

    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{
        MemoryWikiStore, StoreError, WikiChunk, WikiDocument, WikiIndexStore, WikiIngestion,
        WikiLink, WikiSource,
    };

    #[test]
    fn url_ingest_writes_raw_and_manifest() {
        let temp = tempfile::tempdir().expect("tempdir");
        let body = br#"<!doctype html>
<html>
<head><title>Durable Wikis</title></head>
<body><main><h1>Durable Wikis</h1><p>Capture source material.</p></main></body>
</html>"#
            .to_vec();
        let expected_hash = content_hash(&body);
        let snapshot = UrlSnapshot {
            requested_url: "https://Example.com/docs/wiki#overview".to_string(),
            final_url: "https://example.com/docs/wiki/".to_string(),
            fetched_at: "2026-05-29T16:00:00Z".to_string(),
            body,
            content_type: Some("text/html".to_string()),
        };
        let mut store = MemoryWikiStore::default();

        let result =
            ingest_snapshot(temp.path(), &mut store, snapshot).expect("ingest url snapshot");

        assert_eq!(result.asset_path, None);
        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("# Durable Wikis"));
        assert!(raw.contains("canonical_url: https://example.com/docs/wiki"));
        assert!(raw.contains("fetched_at: 2026-05-29T16:00:00Z"));
        assert!(raw.contains("content_type: text/html"));
        assert!(raw.contains(&format!("source_hash: {expected_hash}")));
        assert!(raw.contains("Capture source material."));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        let entry = &manifest.entries[0];
        assert_eq!(entry.kind, SourceKind::Url);
        assert_eq!(entry.title.as_deref(), Some("Durable Wikis"));
        assert_eq!(entry.canonical_location, "https://example.com/docs/wiki");
        assert_eq!(entry.content_hash, expected_hash);
        assert_eq!(entry.fetched_at, "2026-05-29T16:00:00Z");
        assert!(store.documents.contains_key(&PathBuf::from("raw/INDEX.md")));
    }

    #[test]
    fn html_parser_extracts_body_text_and_decodes_entities() {
        let html = br#"<!doctype html>
<html>
<head><title>Hidden &amp; Title</title></head>
<body><main><p>Keep <strong>&amp; decode</strong> together.</p><script>drop()</script></main></body>
</html>"#;

        let html = Html::parse_document(&text_from_utf8_lossy(html));

        assert_eq!(extract_title(&html), Some("Hidden & Title".to_string()));
        assert_eq!(html_to_markdownish_text(&html), "Keep & decode together.");
    }

    #[test]
    fn batch_url_ingest_accepts_successes_and_records_failures() {
        let temp = tempfile::tempdir().expect("tempdir");
        let urls = vec![
            "https://example.test/accepted".to_string(),
            "https://example.test/failure".to_string(),
        ];
        let mut store = MemoryWikiStore::default();

        let result = ingest_urls_with_fetcher(
            temp.path(),
            &mut store,
            &urls,
            "2026-06-02T00:00:00Z",
            |url, fetched_at| {
                if url.ends_with("/accepted") {
                    Ok(test_snapshot(url, url, "Accepted URL", fetched_at))
                } else {
                    Err(UrlIngestFailure::new(url, "http_status", "HTTP status 500"))
                }
            },
        )
        .expect("batch ingest");

        assert_eq!(result.status(), "partial");
        assert_eq!(result.exit_code(), 0);
        assert_eq!(result.accepted.len(), 1);
        assert_eq!(result.failed.len(), 1);
        assert_eq!(
            result.accepted[0].requested_url,
            "https://example.test/accepted"
        );
        assert_eq!(result.failed[0].url, "https://example.test/failure");
        assert_eq!(result.failed[0].code, "http_status");
        assert!(store.documents.contains_key(&PathBuf::from("raw/INDEX.md")));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Url);
        assert_eq!(
            manifest.entries[0].canonical_location,
            "https://example.test/accepted"
        );
    }

    #[test]
    fn batch_url_ingest_indexes_once_after_accepted_batch() {
        let temp = tempfile::tempdir().expect("tempdir");
        let urls = vec![
            "https://example.test/one".to_string(),
            "https://example.test/two".to_string(),
        ];
        let mut store = CountingStore::default();

        let result = ingest_urls_with_fetcher(
            temp.path(),
            &mut store,
            &urls,
            "2026-06-02T00:00:00Z",
            |url, fetched_at| Ok(test_snapshot(url, url, url, fetched_at)),
        )
        .expect("batch ingest");

        assert_eq!(result.status(), "ingested");
        assert_eq!(result.accepted.len(), 2);
        assert_eq!(store.indexed_hash_reads, 1);
    }

    #[test]
    fn url_fetch_limits_content_length_and_stream_bytes() {
        assert!(content_length_exceeds_limit(Some("11"), 10));
        assert!(!content_length_exceeds_limit(Some("10"), 10));
        assert!(!content_length_exceeds_limit(Some("invalid"), 10));

        let error = read_limited_body(std::io::Cursor::new(vec![0_u8; 11]), 10, "https://x.test")
            .expect_err("stream exceeding limit should fail");

        assert_eq!(error.code, "response_too_large");
        assert_eq!(
            read_limited_body(std::io::Cursor::new(vec![0_u8; 10]), 10, "https://x.test")
                .expect("stream at limit")
                .len(),
            10
        );
    }

    fn test_snapshot(
        requested_url: &str,
        final_url: &str,
        title: &str,
        fetched_at: &str,
    ) -> UrlSnapshot {
        UrlSnapshot {
            requested_url: requested_url.to_string(),
            final_url: final_url.to_string(),
            fetched_at: fetched_at.to_string(),
            body: format!(
                "<!doctype html><html><head><title>{title}</title></head><body><p>{title} body.</p></body></html>"
            )
            .into_bytes(),
            content_type: Some("text/html".to_string()),
        }
    }

    #[derive(Default)]
    struct CountingStore {
        inner: MemoryWikiStore,
        indexed_hash_reads: usize,
    }

    impl WikiIndexStore for CountingStore {
        fn indexed_hashes(&mut self) -> Result<BTreeMap<PathBuf, String>, StoreError> {
            self.indexed_hash_reads += 1;
            self.inner.indexed_hashes()
        }

        fn upsert_document(&mut self, document: WikiDocument) -> Result<(), StoreError> {
            self.inner.upsert_document(document)
        }

        fn replace_chunks(
            &mut self,
            path: &Path,
            chunks: Vec<WikiChunk>,
        ) -> Result<(), StoreError> {
            self.inner.replace_chunks(path, chunks)
        }

        fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError> {
            self.inner.replace_links(path, links)
        }

        fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError> {
            self.inner.upsert_source(source)
        }

        fn record_ingestion(&mut self, ingestion: WikiIngestion) -> Result<(), StoreError> {
            self.inner.record_ingestion(ingestion)
        }

        fn record_file_hash(
            &mut self,
            path: PathBuf,
            content_hash: String,
        ) -> Result<(), StoreError> {
            self.inner.record_file_hash(path, content_hash)
        }

        fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError> {
            self.inner.delete_derived_rows(path)
        }
    }
}
