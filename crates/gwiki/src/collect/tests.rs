use std::fs;
use std::path::{Path, PathBuf};

use super::*;
use crate::sources::{SourceKind, SourceManifest};
use crate::store::{FakeWikiStore, WikiDocumentKind, WikiIngestionEvent};

fn write_file(root: &Path, relative: &str, contents: &[u8]) {
    let path = root.join(relative);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent");
    }
    fs::write(path, contents).expect("write file");
}

#[test]
fn collect_routes_known_items() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(
        temp.path(),
        "inbox/link.url",
        b"[InternetShortcut]\nURL=https://example.com/research\n",
    );
    write_file(temp.path(), "inbox/paper.pdf", b"%PDF-1.7\nsource\n%%EOF\n");
    write_file(
        temp.path(),
        "inbox/notes.md",
        b"# Notes\n\nMarkdown source.\n",
    );
    write_file(temp.path(), "inbox/plain.txt", b"plain source text\n");
    write_file(temp.path(), "inbox/interview.wav", b"RIFF....WAVEaudio");
    write_file(temp.path(), "inbox/data.csv", b"name,value\nalpha,1\n");

    let report = collect_inbox(temp.path(), "2026-05-29T18:00:00Z").expect("collect inbox items");

    assert_eq!(report.accepted.len(), 6);
    assert!(report.skipped.is_empty());
    for name in [
        "link.url",
        "paper.pdf",
        "notes.md",
        "plain.txt",
        "interview.wav",
        "data.csv",
    ] {
        assert!(
            !temp.path().join("inbox").join(name).exists(),
            "accepted inbox item should move out: {name}"
        );
    }

    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    let kinds = manifest
        .entries
        .iter()
        .map(|entry| entry.kind.clone())
        .collect::<Vec<_>>();
    for kind in [
        SourceKind::Url,
        SourceKind::Pdf,
        SourceKind::Markdown,
        SourceKind::Text,
        SourceKind::Audio,
        SourceKind::File,
    ] {
        assert!(kinds.contains(&kind), "manifest contains {kind}");
    }
    for entry in manifest.entries {
        assert!(
            temp.path()
                .join("raw")
                .join(format!("{}.md", entry.id))
                .is_file(),
            "raw markdown exists for {}",
            entry.location
        );
    }
}

#[test]
fn collect_unchanged_url_redrop_dedups_to_existing_record() {
    let temp = tempfile::tempdir().expect("tempdir");
    let stub = b"[InternetShortcut]\nURL=https://example.com/research\n";
    write_file(temp.path(), "inbox/link.url", stub);
    let first = collect_inbox(temp.path(), "2026-05-29T18:00:00Z").expect("first collect");
    assert_eq!(first.accepted.len(), 1);

    write_file(temp.path(), "inbox/link.url", stub);
    let second = collect_inbox(temp.path(), "2026-06-01T09:00:00Z")
        .expect("unchanged re-drop dedups instead of failing");

    assert_eq!(second.accepted.len(), 1);
    assert!(second.skipped.is_empty());
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
    let entry = &manifest.entries[0];
    assert_eq!(entry.fetched_at, "2026-05-29T18:00:00Z");
    let raw = std::fs::read_to_string(temp.path().join("raw").join(format!("{}.md", entry.id)))
        .expect("raw capture survives re-drop");
    assert!(raw.contains("2026-05-29T18:00:00Z"));
    assert!(!raw.contains("2026-06-01T09:00:00Z"));
    assert!(!temp.path().join("inbox/link.url").exists());
}

#[test]
fn collect_unchanged_file_redrop_dedups_to_existing_record() {
    let temp = tempfile::tempdir().expect("tempdir");
    let notes = b"# Notes\n\nMarkdown source.\n";
    write_file(temp.path(), "inbox/notes.md", notes);
    let first = collect_inbox(temp.path(), "2026-05-29T18:00:00Z").expect("first collect");
    assert_eq!(first.accepted.len(), 1);

    write_file(temp.path(), "inbox/notes.md", notes);
    let second = collect_inbox(temp.path(), "2026-06-01T09:00:00Z")
        .expect("unchanged re-drop dedups instead of failing");

    assert_eq!(second.accepted.len(), 1);
    assert!(second.skipped.is_empty());
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
    let entry = &manifest.entries[0];
    assert_eq!(entry.fetched_at, "2026-05-29T18:00:00Z");
    let raw = std::fs::read_to_string(temp.path().join("raw").join(format!("{}.md", entry.id)))
        .expect("raw capture survives re-drop");
    assert!(raw.contains("2026-05-29T18:00:00Z"));
    assert!(!raw.contains("2026-06-01T09:00:00Z"));
    assert!(!temp.path().join("inbox/notes.md").exists());
}

#[test]
fn collect_asset_write_failure_does_not_delete_preexisting_asset() {
    let temp = tempfile::tempdir().expect("tempdir");
    let bytes = b"%PDF-1.7\nfresh\n%%EOF\n".to_vec();
    write_file(temp.path(), "inbox/paper.pdf", &bytes);
    let relative = "inbox/paper.pdf";
    let record = SourceManifest::register(
        temp.path(),
        SourceDraft::new(
            relative.to_string(),
            SourceKind::Pdf,
            "2026-05-29T18:00:00Z".to_string(),
            bytes,
        )
        .with_title(markdown_title("paper.pdf"))
        .with_citation(relative.to_string()),
    )
    .expect("pre-register source");
    let asset_path = source_asset_path(&record, "paper.pdf");
    write_file(
        temp.path(),
        &path_to_string(&asset_path),
        b"different existing asset",
    );

    let error =
        collect_inbox(temp.path(), "2026-05-29T18:00:00Z").expect_err("asset mismatch fails");

    assert_eq!(error.code(), "invalid_input");
    assert_eq!(
        std::fs::read(temp.path().join(asset_path)).expect("preexisting asset survives"),
        b"different existing asset"
    );
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
}

#[test]
fn collect_asset_write_failure_rolls_back_new_manifest_entry() {
    let temp = tempfile::tempdir().expect("tempdir");
    let bytes = b"%PDF-1.7\nfresh\n%%EOF\n".to_vec();
    write_file(temp.path(), "inbox/paper.pdf", &bytes);
    let relative = "inbox/paper.pdf";
    let record = SourceManifest::register(
        temp.path(),
        SourceDraft::new(
            relative.to_string(),
            SourceKind::Pdf,
            "2026-05-29T18:00:00Z".to_string(),
            bytes,
        )
        .with_title(markdown_title("paper.pdf"))
        .with_citation(relative.to_string()),
    )
    .expect("pre-register source");
    let asset_path = source_asset_path(&record, "paper.pdf");
    SourceManifest {
        entries: Vec::new(),
    }
    .write(temp.path())
    .expect("reset manifest");
    let asset_parent = asset_path.parent().expect("asset parent");
    fs::create_dir_all(
        temp.path()
            .join(asset_parent.parent().expect("asset grandparent")),
    )
    .expect("asset grandparent dir");
    fs::write(temp.path().join(asset_parent), b"not a directory")
        .expect("asset parent obstruction");

    let error = collect_inbox(temp.path(), "2026-05-29T18:00:00Z").expect_err("asset write fails");

    assert_eq!(error.code(), "io_error");
    assert!(!temp.path().join(&asset_path).exists());
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert!(manifest.entries.is_empty());
}

#[test]
fn collect_indexes_accepted_sources() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "inbox/note.txt", b"accepted source text\n");
    let mut store = FakeWikiStore::default();

    let report = collect_inbox_and_index(temp.path(), &mut store, "2026-05-29T18:03:00Z")
        .expect("collect and index inbox items");

    assert_eq!(report.accepted.len(), 1);
    let raw_path = PathBuf::from(
        report.accepted[0]
            .raw_path
            .as_deref()
            .expect("accepted raw path"),
    );
    assert!(temp.path().join(&raw_path).is_file());

    let catalog_path = PathBuf::from("raw/INDEX.md");
    let catalog = store
        .documents
        .get(&catalog_path)
        .expect("raw source catalog indexed");
    assert_eq!(catalog.kind, WikiDocumentKind::SourceCatalog);
    assert!(catalog.body.contains("inbox/note.txt"));
    assert!(catalog.body.contains("kind: `text`"));
    assert!(store.sources.contains_key(&catalog_path));
    assert!(store.ingestions.iter().any(|ingestion| {
        ingestion.path == catalog_path && ingestion.event == WikiIngestionEvent::Added
    }));
}

#[test]
fn embedded_url_parser_returns_all_urls_in_order() {
    assert_eq!(
        urls_from_embedded_text("Sources: https://example.test/one, then http://example.test/two."),
        vec![
            "https://example.test/one,".to_string(),
            "http://example.test/two.".to_string()
        ]
    );
}

#[test]
fn embedded_url_parser_preserves_valid_punctuation_before_trimming() {
    assert_eq!(
        urls_from_embedded_text("See https://example.test/path_(v1) for details"),
        vec!["https://example.test/path_(v1)".to_string()]
    );
}

#[test]
fn embedded_url_parser_returns_trimmed_candidate_when_trimmed_parse_succeeds() {
    assert_eq!(
        urls_from_embedded_text("See [https://example.test/research]."),
        vec!["https://example.test/research".to_string()]
    );
}

#[test]
fn ambiguous_items_remain_in_inbox() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "inbox/untitled", b"ambiguous dropped text\n");

    let report = collect_inbox(temp.path(), "2026-05-29T18:05:00Z").expect("collect inbox items");

    assert!(report.accepted.is_empty());
    assert_eq!(report.skipped.len(), 1);
    assert!(temp.path().join("inbox/untitled").is_file());
    let status_path = temp.path().join("inbox/untitled.status.json");
    let status = fs::read_to_string(status_path).expect("status sidecar");
    assert!(status.contains("\"status\""));
    assert!(status.contains("\"skipped\""));
    assert!(status.contains("extensionless inbox item is ambiguous"));
}

#[test]
fn collect_logs_actions() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "inbox/note.txt", b"accepted text\n");
    write_file(temp.path(), "inbox/mystery", b"ambiguous text\n");

    collect_inbox(temp.path(), "2026-05-29T18:10:00Z").expect("collect inbox items");

    let log = fs::read_to_string(temp.path().join("log.md")).expect("read log");
    assert!(log.contains("(unix-ms:1780078200000) collect accepted"));
    assert!(log.contains("inbox/note.txt"));
    assert!(log.contains("(unix-ms:1780078200000) collect skipped"));
    assert!(log.contains("inbox/mystery"));
}

#[test]
fn oversized_items_are_skipped_before_reading() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_file(temp.path(), "inbox/large.txt", b"too large");

    let report = collect_inbox_with_limit(temp.path(), "2026-05-29T18:12:00Z", 3)
        .expect("collect inbox items");

    assert!(report.accepted.is_empty());
    assert_eq!(report.skipped.len(), 1);
    assert_eq!(
        report.skipped[0].reason.as_deref(),
        Some("inbox item exceeds 3 byte limit")
    );
    assert!(temp.path().join("inbox/large.txt").is_file());
}

#[test]
fn collect_cleanup_context_preserves_config_error_variant() {
    let error = collect_error_with_cleanup::<()>(
        WikiError::Config {
            detail: "write failed".to_string(),
        },
        vec!["remove raw failed".to_string()],
    )
    .expect_err("cleanup error must be returned");

    match error {
        WikiError::Config { detail } => {
            assert!(detail.contains("write failed"));
            assert!(detail.contains("cleanup failures: remove raw failed"));
        }
        other => panic!("expected config error, got {other:?}"),
    }
}

#[test]
fn collect_cleanup_context_preserves_io_error_variant() {
    let error = collect_error_with_cleanup::<()>(
        WikiError::Io {
            action: "write raw source",
            path: None,
            source: std::io::Error::new(std::io::ErrorKind::PermissionDenied, "denied"),
        },
        vec!["remove raw failed".to_string()],
    )
    .expect_err("cleanup error must be returned");

    match error {
        WikiError::Io { source, .. } => {
            assert_eq!(source.kind(), std::io::ErrorKind::PermissionDenied);
            assert!(source.to_string().contains("denied"));
            assert!(
                source
                    .to_string()
                    .contains("cleanup failures: remove raw failed")
            );
        }
        other => panic!("expected io error, got {other:?}"),
    }
}
