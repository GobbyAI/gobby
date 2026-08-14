use std::collections::BTreeMap;
use std::net::IpAddr;
use std::path::{Path, PathBuf};

use gobby_core::indexing::content_hash;
use scraper::Html;

use super::fetch::{
    content_length_exceeds_limit, is_disallowed_fetch_ip, read_limited_body, resolve_redirect_url,
};
use super::render::{escape_wikilink_delimiters, extract_title, html_to_markdownish_text};
use super::*;
use crate::ingest::text_from_utf8_lossy;
use crate::sources::{FetchProvenance, SourceKind, SourceManifest};
use crate::store::{
    FakeWikiStore, StoreError, WikiChunk, WikiDocument, WikiIndexStore, WikiIngestion, WikiLink,
    WikiSource,
};
use crate::support::test_env::EnvGuard;

#[derive(Default)]
struct RecordingProgress {
    events: Vec<String>,
}

impl crate::progress::ProgressSink for RecordingProgress {
    fn start(&mut self, phase: crate::progress::ProgressPhase, total: usize) {
        self.events.push(format!("{phase:?}:start:{total}"));
    }

    fn advance(&mut self, phase: crate::progress::ProgressPhase, item: &str) {
        self.events.push(format!("{phase:?}:advance:{item}"));
    }

    fn finish(&mut self, phase: crate::progress::ProgressPhase) {
        self.events.push(format!("{phase:?}:finish"));
    }
}

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
    let mut store = FakeWikiStore::default();

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
    let mut store = FakeWikiStore::default();

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
fn url_ingest_escapes_wikilink_payloads_and_keeps_frontmatter_intact() {
    let temp = tempfile::tempdir().expect("tempdir");
    let body = br#"<!doctype html>
<html>
<head><title>Take [[control]] of your vault</title></head>
<body><main>
<p>See [[secret-note]] and the embed ![[vault-page]] for details.</p>
<p>---</p>
<p>trust: high</p>
<p>---</p>
</main></body>
</html>"#
        .to_vec();
    let expected_hash = content_hash(&body);
    let snapshot = UrlSnapshot {
        requested_url: "https://evil.example/page".to_string(),
        final_url: "https://evil.example/page".to_string(),
        fetched_at: "2026-07-05T08:00:00Z".to_string(),
        body,
        content_type: Some("text/html".to_string()),
    };
    let mut store = FakeWikiStore::default();

    let result =
        ingest_snapshot(temp.path(), &mut store, snapshot).expect("ingest hostile snapshot");

    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
    assert!(!raw.contains("[["), "unescaped wikilink opener in: {raw}");
    assert!(!raw.contains("]]"), "unescaped wikilink closer in: {raw}");
    assert!(
        raw.contains("# Take \\[\\[control\\]\\] of your vault"),
        "{raw}"
    );
    assert!(raw.contains("\\[\\[secret-note\\]\\]"), "{raw}");
    assert!(raw.contains("!\\[\\[vault-page\\]\\]"), "{raw}");
    assert!(raw.contains(&format!("source_hash: {expected_hash}")));

    // The fake frontmatter block in the body must stay inert: the real
    // frontmatter closes at its own delimiter and gains nothing from the
    // payload's `---` fences.
    let parsed = crate::frontmatter::parse_frontmatter(&raw).expect("hostile raw digest parses");
    assert_eq!(parsed.metadata.trust, None);
    assert!(parsed.body.contains("trust: high"), "{raw}");
}

#[test]
fn url_ingest_escapes_wikilink_delimiters_in_non_html_titles() {
    // The non-HTML title comes from the URL's last path segment, which the
    // url crate passes through with literal brackets intact.
    let temp = tempfile::tempdir().expect("tempdir");
    let snapshot = UrlSnapshot {
        requested_url: "https://evil.example/files/[[hostile]].txt".to_string(),
        final_url: "https://evil.example/files/[[hostile]].txt".to_string(),
        fetched_at: "2026-07-05T08:00:00Z".to_string(),
        body: b"plain text payload\n".to_vec(),
        content_type: Some("text/plain".to_string()),
    };
    let mut store = FakeWikiStore::default();

    let result = ingest_snapshot(temp.path(), &mut store, snapshot).expect("ingest text snapshot");

    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
    assert!(raw.contains("# \\[\\[hostile\\]\\].txt"), "{raw}");
}

#[test]
fn wikilink_escaping_removes_all_delimiter_adjacency() {
    assert_eq!(escape_wikilink_delimiters("[[x]]"), "\\[\\[x\\]\\]");
    assert_eq!(escape_wikilink_delimiters("![[x]]"), "!\\[\\[x\\]\\]");
    for hostile in [
        "[[[deep]]]",
        "[[[[double]]]]",
        "\\[[pre-escaped]]",
        "a[[b]]c[[d]]",
    ] {
        let escaped = escape_wikilink_delimiters(hostile);
        assert!(!escaped.contains("[["), "{hostile} -> {escaped}");
        assert!(!escaped.contains("]]"), "{hostile} -> {escaped}");
    }
}

#[test]
fn batch_url_ingest_progress_reports_urls_and_index_phase() {
    let temp = tempfile::tempdir().expect("tempdir");
    let urls = vec![
        "https://example.test/one".to_string(),
        "https://example.test/two".to_string(),
    ];
    let mut store = FakeWikiStore::default();
    let mut progress = RecordingProgress::default();

    ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &urls,
        "2026-06-02T00:00:00Z",
        0,
        |url, fetched_at| Ok(test_snapshot(url, url, url, fetched_at)),
        &mut crate::progress::ProgressOptions::with_sink(&mut progress),
    )
    .expect("batch ingest");

    assert_eq!(
        progress.events.first().map(String::as_str),
        Some("IngestUrl:start:2")
    );
    assert!(
        progress
            .events
            .contains(&"IngestUrl:advance:https://example.test/one".to_string())
    );
    assert!(
        progress
            .events
            .contains(&"IngestUrl:advance:https://example.test/two".to_string())
    );
    assert!(progress.events.contains(&"IngestUrl:finish".to_string()));
    assert!(
        progress
            .events
            .iter()
            .any(|event| event.starts_with("VaultIndex:start:"))
    );
    assert!(progress.events.contains(&"VaultIndex:finish".to_string()));
}

#[test]
fn batch_url_ingest_accepts_successes_and_records_failures() {
    let temp = tempfile::tempdir().expect("tempdir");
    let urls = vec![
        "https://example.test/accepted".to_string(),
        "https://example.test/failure".to_string(),
    ];
    let mut store = FakeWikiStore::default();

    let result = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &urls,
        "2026-06-02T00:00:00Z",
        0,
        |url, fetched_at| {
            if url.ends_with("/accepted") {
                Ok(test_snapshot(url, url, "Accepted URL", fetched_at))
            } else {
                Err(UrlIngestFailure::new(url, "http_status", "HTTP status 500"))
            }
        },
        &mut crate::progress::ProgressOptions::default(),
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
fn within_ttl_uses_manifest_cache_without_fetch_or_store() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = "https://example.test/cached";
    let first = ingest_snapshot_without_index(
        temp.path(),
        test_snapshot(url, url, "Cached URL", "unix-ms:1783215000000"),
    )
    .expect("seed fetched URL");
    let mut store = CountingStore::default();
    let mut fetch_calls = 0;

    let result = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &[url.to_string()],
        "unix-ms:1783222200000",
        24,
        |_, _| {
            fetch_calls += 1;
            unreachable!("fresh URL must not be fetched")
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("cached ingest");

    assert!(result.accepted.is_empty());
    assert!(result.failed.is_empty());
    assert_eq!(result.status(), "ingested");
    assert_eq!(result.exit_code(), 0);
    assert_eq!(fetch_calls, 0);
    assert_eq!(
        result.cached,
        vec![CachedUrlIngest {
            requested_url: url.to_string(),
            source_id: first.record.id,
            fetched_at: "unix-ms:1783215000000".to_string(),
            age_hours: 2,
        }]
    );
    assert_eq!(store.indexed_hash_reads, 0);
    assert_eq!(store.indexed_hashes_reads, 0);

    let cached_plus_failed = UrlBatchIngest {
        accepted: Vec::new(),
        cached: result.cached.clone(),
        failed: vec![UrlIngestFailure::new(
            "https://example.test/failed",
            "http_status",
            "HTTP status 500",
        )],
    };
    assert_eq!(cached_plus_failed.status(), "partial");
    assert_eq!(cached_plus_failed.exit_code(), 0);
}

#[test]
fn zero_ttl_forces_refetch_even_at_zero_age() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = "https://example.test/zero-ttl";
    ingest_snapshot_without_index(
        temp.path(),
        test_snapshot(url, url, "Zero TTL", "unix-ms:1783215000000"),
    )
    .expect("seed fetched URL");
    let mut html_store = FakeWikiStore::default();
    let mut fetch_calls = 0;

    let result = ingest_urls_with_fetcher(
        temp.path(),
        &mut html_store,
        &[url.to_string()],
        "unix-ms:1783215000000",
        0,
        |url, fetched_at| {
            fetch_calls += 1;
            Ok(test_snapshot(url, url, "Zero TTL", fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("zero-TTL ingest");

    assert_eq!(fetch_calls, 1);
    assert_eq!(result.accepted.len(), 1);
    assert!(result.cached.is_empty());
}

#[test]
fn expired_unchanged_refetch_touches_freshness_and_preserves_capture() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = "https://example.test/expired";
    let first = ingest_snapshot_without_index(
        temp.path(),
        test_snapshot(url, url, "Expired", "unix-ms:1783215000000"),
    )
    .expect("seed fetched URL");
    let first_raw =
        std::fs::read(temp.path().join(&first.raw_path)).expect("read first raw capture");
    let mut store = FakeWikiStore::default();
    let mut fetch_calls = 0;

    let refreshed = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &[url.to_string()],
        "unix-ms:1783305000000",
        24,
        |url, fetched_at| {
            fetch_calls += 1;
            Ok(test_snapshot(url, url, "Expired", fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("expired unchanged refetch");

    assert_eq!(fetch_calls, 1);
    assert_eq!(refreshed.accepted.len(), 1);
    let refreshed_record = &refreshed.accepted[0].result.record;
    assert_eq!(refreshed_record.id, first.record.id);
    assert_eq!(refreshed_record.fetched_at, "unix-ms:1783215000000");
    assert_eq!(refreshed_record.last_verified_at, "unix-ms:1783305000000");
    assert_eq!(refreshed_record.fetch_provenance, FetchProvenance::Fetched);
    assert_eq!(
        std::fs::read(temp.path().join(&first.raw_path)).expect("read refreshed raw"),
        first_raw
    );

    let cached = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &[url.to_string()],
        "unix-ms:1783308600000",
        24,
        |_, _| unreachable!("freshly verified URL must hit cache"),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("fresh follow-up");
    assert_eq!(cached.cached.len(), 1);
    assert_eq!(cached.cached[0].source_id, first.record.id);
}

#[test]
fn missing_url_artifacts_and_invalid_freshness_refetch_for_self_healing() {
    let html_root = tempfile::tempdir().expect("html tempdir");
    let html_url = "https://example.test/missing-raw";
    let html = ingest_snapshot_without_index(
        html_root.path(),
        test_snapshot(html_url, html_url, "Missing raw", "unix-ms:1783215000000"),
    )
    .expect("seed HTML URL");
    std::fs::remove_file(html_root.path().join(&html.raw_path)).expect("remove raw");
    let mut store = FakeWikiStore::default();
    let mut html_fetches = 0;
    ingest_urls_with_fetcher(
        html_root.path(),
        &mut store,
        &[html_url.to_string()],
        "unix-ms:1783218600000",
        24,
        |url, fetched_at| {
            html_fetches += 1;
            Ok(test_snapshot(url, url, "Missing raw", fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("self-heal missing raw");
    assert_eq!(html_fetches, 1);
    assert!(html_root.path().join(&html.raw_path).is_file());

    let pdf_root = tempfile::tempdir().expect("pdf tempdir");
    let pdf_url = "https://example.test/missing.pdf";
    let pdf = ingest_snapshot_without_index(
        pdf_root.path(),
        pdf_snapshot(pdf_url, "unix-ms:1783215000000"),
    )
    .expect("seed PDF URL");
    let asset_path = pdf.asset_path.clone().expect("PDF asset");
    std::fs::remove_file(pdf_root.path().join(&asset_path)).expect("remove asset");
    let mut pdf_store = FakeWikiStore::default();
    let mut pdf_fetches = 0;
    ingest_urls_with_fetcher(
        pdf_root.path(),
        &mut pdf_store,
        &[pdf_url.to_string()],
        "unix-ms:1783218600000",
        24,
        |url, fetched_at| {
            pdf_fetches += 1;
            Ok(pdf_snapshot(url, fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("self-heal missing asset");
    assert_eq!(pdf_fetches, 1);
    assert!(pdf_root.path().join(asset_path).is_file());

    SourceManifest::update(pdf_root.path(), |manifest| {
        manifest.entries[0].last_verified_at = "unix-ms:1784000000000".to_string();
        Ok(true)
    })
    .expect("set future freshness");
    ingest_urls_with_fetcher(
        pdf_root.path(),
        &mut pdf_store,
        &[pdf_url.to_string()],
        "unix-ms:1783218600000",
        24,
        |url, fetched_at| {
            pdf_fetches += 1;
            Ok(pdf_snapshot(url, fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("future freshness is a miss");
    SourceManifest::update(pdf_root.path(), |manifest| {
        manifest.entries[0].last_verified_at = "corrupt".to_string();
        Ok(true)
    })
    .expect("set corrupt freshness");
    ingest_urls_with_fetcher(
        pdf_root.path(),
        &mut pdf_store,
        &[pdf_url.to_string()],
        "unix-ms:1783218601000",
        24,
        |url, fetched_at| {
            pdf_fetches += 1;
            Ok(pdf_snapshot(url, fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("corrupt freshness is a miss");
    assert_eq!(pdf_fetches, 3);
}

#[test]
fn canonical_batch_duplicates_reuse_success_cache_age_and_failure() {
    let first_url = "https://EXAMPLE.test/duplicate?b=2&a=1";
    let duplicate_url = "https://example.test/duplicate?a=1&b=2";
    let urls = vec![first_url.to_string(), duplicate_url.to_string()];

    let fresh_root = tempfile::tempdir().expect("fresh tempdir");
    let mut store = FakeWikiStore::default();
    let mut fetches = 0;
    let fresh = ingest_urls_with_fetcher(
        fresh_root.path(),
        &mut store,
        &urls,
        "unix-ms:1783215000000",
        0,
        |url, fetched_at| {
            fetches += 1;
            Ok(test_snapshot(url, url, "Duplicate", fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("dedup first fetch");
    assert_eq!(fetches, 1);
    assert_eq!(fresh.accepted.len(), 1);
    assert_eq!(fresh.cached.len(), 1);
    assert_eq!(fresh.cached[0].age_hours, 0);
    assert_eq!(
        fresh.cached[0].source_id,
        fresh.accepted[0].result.record.id
    );

    let cached_root = tempfile::tempdir().expect("cached tempdir");
    let seeded = ingest_snapshot_without_index(
        cached_root.path(),
        test_snapshot(first_url, first_url, "Duplicate", "unix-ms:1783215000000"),
    )
    .expect("seed cached URL");
    let cached = ingest_urls_with_fetcher(
        cached_root.path(),
        &mut store,
        &urls,
        "unix-ms:1783222200000",
        24,
        |_, _| unreachable!("cache-hit duplicates must not fetch"),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("dedup cache hit");
    assert_eq!(cached.cached.len(), 2);
    assert!(cached.cached.iter().all(|entry| entry.age_hours == 2));
    assert!(
        cached
            .cached
            .iter()
            .all(|entry| entry.source_id == seeded.record.id)
    );

    let failed_root = tempfile::tempdir().expect("failure tempdir");
    let mut failed_fetches = 0;
    let failed = ingest_urls_with_fetcher(
        failed_root.path(),
        &mut store,
        &urls,
        "unix-ms:1783215000000",
        24,
        |url, _| {
            failed_fetches += 1;
            Err(UrlIngestFailure::new(url, "http_status", "HTTP status 500"))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("dedup failure");
    assert_eq!(failed_fetches, 1);
    assert_eq!(failed.failed.len(), 2);
}

#[test]
fn pre_field_record_promotes_once_and_collect_redrop_cannot_downgrade() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = "https://example.test/pre-field";
    let seeded = ingest_snapshot_without_index(
        temp.path(),
        test_snapshot(url, url, "Pre-field", "unix-ms:1783215000000"),
    )
    .expect("seed fetched URL");
    remove_freshness_fields_from_manifest(temp.path());
    let legacy = SourceManifest::read(temp.path()).expect("read legacy manifest");
    assert_eq!(legacy.entries[0].fetch_provenance, FetchProvenance::Stub);
    assert_eq!(
        legacy.entries[0].last_verified_at,
        legacy.entries[0].fetched_at
    );

    let mut store = FakeWikiStore::default();
    let mut fetches = 0;
    ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &[url.to_string()],
        "unix-ms:1783218600000",
        24,
        |url, fetched_at| {
            fetches += 1;
            Ok(test_snapshot(url, url, "Pre-field", fetched_at))
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("promote legacy record");
    let promoted = SourceManifest::read(temp.path()).expect("read promoted manifest");
    let fetched_record = promoted
        .entries
        .iter()
        .find(|record| record.id == seeded.record.id)
        .expect("promoted record");
    assert_eq!(fetched_record.fetch_provenance, FetchProvenance::Fetched);

    std::fs::create_dir_all(temp.path().join("inbox")).expect("create inbox");
    std::fs::write(
        temp.path().join("inbox/link.url"),
        format!("[InternetShortcut]\nURL={url}\n"),
    )
    .expect("write collect stub");
    crate::collect::collect_inbox(temp.path(), "unix-ms:1783219000000").expect("collect re-drop");
    let cached = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &[url.to_string()],
        "unix-ms:1783220000000",
        24,
        |_, _| unreachable!("collect stub must not hide fetched record"),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("cache after collect re-drop");
    assert_eq!(fetches, 1);
    assert_eq!(cached.cached[0].source_id, seeded.record.id);
}

#[test]
fn non_html_url_has_cache_parity_with_html() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = "https://example.test/paper.pdf";
    let first =
        ingest_snapshot_without_index(temp.path(), pdf_snapshot(url, "unix-ms:1783215000000"))
            .expect("seed PDF URL");
    assert_eq!(first.record.kind, SourceKind::Pdf);
    let mut store = FakeWikiStore::default();

    let cached = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &[url.to_string()],
        "unix-ms:1783218600000",
        24,
        |_, _| unreachable!("fresh PDF URL must hit cache"),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("cached PDF");

    assert_eq!(cached.cached.len(), 1);
    assert_eq!(cached.cached[0].source_id, first.record.id);
}

#[test]
fn reingesting_changed_url_supersedes_manifest_record() {
    let temp = tempfile::tempdir().expect("tempdir");
    let urls = vec!["https://example.test/changing".to_string()];
    let mut store = FakeWikiStore::default();

    let first = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &urls,
        "2026-07-01T00:00:00Z",
        0,
        |url, fetched_at| Ok(test_snapshot(url, url, "First revision", fetched_at)),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("first ingest");
    let old_record = first.accepted[0].result.record.clone();
    let old_raw = temp.path().join(&first.accepted[0].result.raw_path);
    assert!(old_raw.is_file());

    let second = ingest_urls_with_fetcher(
        temp.path(),
        &mut store,
        &urls,
        "2026-07-02T00:00:00Z",
        0,
        |url, fetched_at| Ok(test_snapshot(url, url, "Second revision", fetched_at)),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("second ingest");
    let new_record = second.accepted[0].result.record.clone();

    assert_ne!(new_record.id, old_record.id);
    assert_eq!(new_record.canonical_location, old_record.canonical_location);
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1, "single record per URL");
    assert_eq!(manifest.entries[0].id, new_record.id);
    assert!(!old_raw.exists(), "stale raw capture removed");
    assert!(
        temp.path()
            .join(&second.accepted[0].result.raw_path)
            .is_file()
    );
}

#[test]
fn unchanged_html_url_reingest_reuses_raw_capture_and_record_timestamp() {
    let temp = tempfile::tempdir().expect("tempdir");
    let first = ingest_snapshot_without_index(
        temp.path(),
        test_snapshot(
            "https://example.test/stable",
            "https://example.test/stable",
            "Stable",
            "2026-07-01T00:00:00Z",
        ),
    )
    .expect("first ingest");
    let first_raw = std::fs::read_to_string(temp.path().join(&first.raw_path)).expect("first raw");

    let second = ingest_snapshot_without_index(
        temp.path(),
        test_snapshot(
            "https://example.test/stable",
            "https://example.test/stable",
            "Stable",
            "2026-07-02T00:00:00Z",
        ),
    )
    .expect("unchanged reingest");
    let second_raw =
        std::fs::read_to_string(temp.path().join(&second.raw_path)).expect("second raw");

    assert_eq!(second.record.id, first.record.id);
    assert_eq!(second.record.fetched_at, first.record.fetched_at);
    assert_eq!(second.record.last_verified_at, "2026-07-02T00:00:00Z");
    assert_eq!(second.record.fetch_provenance, FetchProvenance::Fetched);
    assert_eq!(second.raw_path, first.raw_path);
    assert_eq!(second_raw, first_raw);
    assert!(second_raw.contains("2026-07-01T00:00:00Z"));
    assert!(!second_raw.contains("2026-07-02T00:00:00Z"));
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
}

#[test]
fn unchanged_non_html_url_reingest_reuses_raw_capture_and_asset() {
    let temp = tempfile::tempdir().expect("tempdir");
    let body = b"%PDF stable bytes\n".to_vec();
    let first = ingest_snapshot_without_index(
        temp.path(),
        UrlSnapshot {
            requested_url: "https://example.test/report".to_string(),
            final_url: "https://example.test/report.pdf".to_string(),
            fetched_at: "2026-07-01T00:00:00Z".to_string(),
            body: body.clone(),
            content_type: Some("application/pdf".to_string()),
        },
    )
    .expect("first ingest");
    let first_raw = std::fs::read_to_string(temp.path().join(&first.raw_path)).expect("first raw");
    let first_asset = first.asset_path.clone().expect("first asset path");

    let second = ingest_snapshot_without_index(
        temp.path(),
        UrlSnapshot {
            requested_url: "https://example.test/report".to_string(),
            final_url: "https://example.test/report.pdf".to_string(),
            fetched_at: "2026-07-02T00:00:00Z".to_string(),
            body: body.clone(),
            content_type: Some("application/pdf".to_string()),
        },
    )
    .expect("unchanged reingest");
    let second_raw =
        std::fs::read_to_string(temp.path().join(&second.raw_path)).expect("second raw");

    assert_eq!(second.record.id, first.record.id);
    assert_eq!(second.record.fetched_at, first.record.fetched_at);
    assert_eq!(second.record.last_verified_at, "2026-07-02T00:00:00Z");
    assert_eq!(second.record.fetch_provenance, FetchProvenance::Fetched);
    assert_eq!(second.raw_path, first.raw_path);
    assert_eq!(second.asset_path.as_ref(), Some(&first_asset));
    assert_eq!(
        std::fs::read(temp.path().join(&first_asset)).expect("asset bytes"),
        body
    );
    assert_eq!(second_raw, first_raw);
    assert!(second_raw.contains("2026-07-01T00:00:00Z"));
    assert!(!second_raw.contains("2026-07-02T00:00:00Z"));
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
        0,
        |url, fetched_at| Ok(test_snapshot(url, url, url, fetched_at)),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("batch ingest");

    assert_eq!(result.status(), "ingested");
    assert_eq!(result.accepted.len(), 2);
    assert_eq!(store.indexed_hash_reads, 1);
    assert_eq!(store.indexed_hashes_reads, 0);
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
fn actual_url_fetch_oversized_404_reports_http_status() {
    let _env = EnvGuard::set("GWIKI_ALLOW_LOOPBACK_URL_FETCH_FOR_TESTS", "1")
        .and_set("GWIKI_MAX_INBOX_ITEM_BYTES", "8");
    let (base_url, request) =
        crate::test_http::spawn_response(404, "text/plain", "body larger than limit")
            .expect("spawn test server");

    let error =
        fetch_url_snapshot(&base_url, "2026-06-02T00:00:00Z").expect_err("fetch should fail");

    assert_eq!(error.code, "http_status");
    assert_eq!(error.message, "HTTP status 404");
    let request = request.join().expect("server thread").expect("request");
    assert!(request.starts_with("GET / HTTP/1.1"));
}

#[test]
fn actual_url_fetch_oversized_200_reports_response_too_large() {
    let _env = EnvGuard::set("GWIKI_ALLOW_LOOPBACK_URL_FETCH_FOR_TESTS", "1")
        .and_set("GWIKI_MAX_INBOX_ITEM_BYTES", "8");
    let (base_url, request) =
        crate::test_http::spawn_response(200, "text/plain", "body larger than limit")
            .expect("spawn test server");

    let error =
        fetch_url_snapshot(&base_url, "2026-06-02T00:00:00Z").expect_err("fetch should fail");

    assert_eq!(error.code, "response_too_large");
    assert_eq!(
        error.message,
        "response exceeds GWIKI_MAX_INBOX_ITEM_BYTES limit of 8 bytes"
    );
    let request = request.join().expect("server thread").expect("request");
    assert!(request.starts_with("GET / HTTP/1.1"));
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

fn pdf_snapshot(url: &str, fetched_at: &str) -> UrlSnapshot {
    UrlSnapshot {
        requested_url: url.to_string(),
        final_url: url.to_string(),
        fetched_at: fetched_at.to_string(),
        body: b"%PDF-1.7\ncached payload\n%%EOF\n".to_vec(),
        content_type: Some("application/pdf".to_string()),
    }
}

fn remove_freshness_fields_from_manifest(vault_root: &Path) {
    let path = SourceManifest::index_path(vault_root);
    let index = std::fs::read_to_string(&path).expect("read source index");
    let rewritten = index
        .lines()
        .map(|line| {
            let Some(marker_start) = line.find("<!-- gwiki-source:") else {
                return line.to_string();
            };
            let json_start = marker_start + "<!-- gwiki-source:".len();
            let marker_end = line[json_start..].rfind("-->").expect("marker end");
            let json_end = json_start + marker_end;
            let mut value: serde_json::Value =
                serde_json::from_str(line[json_start..json_end].trim()).expect("parse marker");
            let object = value.as_object_mut().expect("source record object");
            object.remove("last_verified_at");
            object.remove("fetch_provenance");
            format!(
                "{}{} -->",
                &line[..json_start],
                serde_json::to_string(&value).expect("serialize legacy marker")
            )
        })
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(path, format!("{rewritten}\n")).expect("write legacy source index");
}

#[derive(Default)]
struct CountingStore {
    inner: FakeWikiStore,
    indexed_hash_reads: usize,
    indexed_hashes_reads: usize,
}

impl WikiIndexStore for CountingStore {
    fn indexed_hashes(&mut self) -> Result<BTreeMap<PathBuf, String>, StoreError> {
        self.indexed_hashes_reads += 1;
        self.inner.indexed_hashes()
    }

    fn indexed_hash(&mut self, path: &Path) -> Result<Option<String>, StoreError> {
        self.indexed_hash_reads += 1;
        self.inner.indexed_hash(path)
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

    fn delete_derived_rows_and_record_ingestion(
        &mut self,
        ingestion: WikiIngestion,
    ) -> Result<(), StoreError> {
        self.inner
            .delete_derived_rows_and_record_ingestion(ingestion)
    }
}
