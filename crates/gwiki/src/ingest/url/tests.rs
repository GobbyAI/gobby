use std::collections::BTreeMap;
use std::net::IpAddr;
use std::path::{Path, PathBuf};

use gobby_core::indexing::content_hash;
use scraper::Html;

use super::fetch::{
    content_length_exceeds_limit, is_disallowed_fetch_ip, read_limited_body, resolve_redirect_url,
};
use super::render::{extract_title, html_to_markdownish_text};
use super::*;
use crate::ingest::text_from_utf8_lossy;
use crate::sources::{SourceKind, SourceManifest};
use crate::store::{
    MemoryWikiStore, StoreError, WikiChunk, WikiDocument, WikiIndexStore, WikiIngestion, WikiLink,
    WikiSource,
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

    let result = ingest_snapshot(temp.path(), &mut store, snapshot).expect("ingest url snapshot");

    assert_eq!(result.asset_path, None);
    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
    assert!(raw.contains("# Durable Wikis"));
    assert!(raw.contains("canonical_url: \"https://example.com/docs/wiki\""));
    assert!(raw.contains("fetched_at: \"2026-05-29T16:00:00Z\""));
    assert!(raw.contains("content_type: \"text/html\""));
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
fn url_ingest_preserves_non_html_as_typed_asset() {
    let temp = tempfile::tempdir().expect("tempdir");
    let body = b"%PDF-1.7\nbinary-ish\n%%EOF\n".to_vec();
    let snapshot = UrlSnapshot {
        requested_url: "https://example.com/report".to_string(),
        final_url: "https://example.com/files/report.pdf".to_string(),
        fetched_at: "2026-05-29T16:00:00Z".to_string(),
        body: body.clone(),
        content_type: Some("Application/PDF; charset=binary".to_string()),
    };
    let mut store = MemoryWikiStore::default();

    let result =
        ingest_snapshot(temp.path(), &mut store, snapshot).expect("ingest pdf url snapshot");

    let asset_path = result.asset_path.expect("non-html asset path");
    assert_eq!(
        std::fs::read(temp.path().join(&asset_path)).expect("asset bytes"),
        body
    );
    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
    assert!(raw.contains("source_kind: pdf"));
    assert!(raw.contains("source_asset: "));
    assert!(raw.contains("media_degradation: url_non_html_asset"));
    assert!(raw.contains("Non-HTML URL response preserved as a source asset."));
    assert!(!raw.contains("binary-ish"));

    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries[0].kind, SourceKind::Pdf);
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

#[test]
fn url_fetch_rejects_private_and_local_addresses() {
    for address in [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ] {
        let ip = address.parse::<IpAddr>().expect("test IP parses");
        assert!(is_disallowed_fetch_ip(ip), "{address} should be rejected");
    }

    assert!(!is_disallowed_fetch_ip(
        "93.184.216.34".parse().expect("public IP parses")
    ));
}

#[test]
fn redirect_url_resolution_handles_relative_locations() {
    assert_eq!(
        resolve_redirect_url("https://example.com/a/b", "../next").expect("redirect"),
        "https://example.com/next"
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

    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) -> Result<(), StoreError> {
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

    fn record_file_hash(&mut self, path: PathBuf, content_hash: String) -> Result<(), StoreError> {
        self.inner.record_file_hash(path, content_hash)
    }

    fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError> {
        self.inner.delete_derived_rows(path)
    }
}
