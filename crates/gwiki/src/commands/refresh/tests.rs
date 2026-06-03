use super::*;
use crate::IngestFileOptions;
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceReplay};

fn test_scope(root: &Path) -> ResolvedScope {
    ResolvedScope::topic(
        "refresh-test".to_string(),
        root.to_path_buf(),
        root.join("registry.toml"),
    )
}

fn seed_url(root: &Path, location: &str, fetched_at: &str, body: &[u8]) -> SourceRecord {
    SourceManifest::register(
        root,
        SourceDraft {
            location: location.to_string(),
            kind: SourceKind::Url,
            fetched_at: fetched_at.to_string(),
            content: body.to_vec(),
            title: Some("Example".to_string()),
            citation: Some(location.to_string()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register source")
}

fn seed_legacy_file(root: &Path) -> SourceRecord {
    SourceManifest::register(
        root,
        SourceDraft {
            location: "/tmp/source.txt".to_string(),
            kind: SourceKind::File,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            content: b"file".to_vec(),
            title: Some("File".to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register source")
}

fn seed_local_file(root: &Path, relative_path: &str, body: &[u8]) -> SourceRecord {
    let path = root.join(relative_path);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create source parent");
    }
    fs::write(&path, body).expect("write replay source");
    let kind = if relative_path.ends_with(".md") {
        SourceKind::Markdown
    } else if relative_path.ends_with(".txt") {
        SourceKind::Text
    } else {
        SourceKind::File
    };
    let record = SourceManifest::register(
        root,
        SourceDraft {
            location: relative_path.to_string(),
            kind,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            content: body.to_vec(),
            title: Some("Local file".to_string()),
            citation: Some(relative_path.to_string()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register local source");
    let replay = SourceReplay::local_file(
        path,
        &IngestFileOptions {
            no_ai: true,
            video_frame_interval_seconds: Some(0),
            ..IngestFileOptions::default()
        },
    );
    SourceManifest::update(root, |manifest| {
        manifest
            .entries
            .iter_mut()
            .find(|entry| entry.id == record.id)
            .expect("seeded local source")
            .replay = Some(replay);
        Ok(true)
    })
    .expect("write local replay metadata");
    SourceManifest::read(root)
        .expect("read manifest")
        .entries
        .into_iter()
        .find(|entry| entry.id == record.id)
        .expect("updated local source")
}

fn seed_unsupported_connector(root: &Path) -> SourceRecord {
    SourceManifest::register(
        root,
        SourceDraft {
            location: "stdin:source".to_string(),
            kind: SourceKind::Stdin,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            content: b"stdin".to_vec(),
            title: Some("Stdin".to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register unsupported connector")
}

fn snapshot(url: &str, body: &str) -> UrlSnapshot {
    UrlSnapshot {
        requested_url: url.to_string(),
        final_url: url.to_string(),
        fetched_at: "2026-06-02T00:00:00Z".to_string(),
        body: body.as_bytes().to_vec(),
        content_type: Some("text/html".to_string()),
    }
}

#[test]
fn dry_run_plans_without_fetching_or_writing() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_url(temp.path(), "https://example.test/a", "then", b"old");
    let mut fetched = false;

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![record.id.clone()],
        true,
        |_record, _fetched_at| {
            fetched = true;
            unreachable!("dry-run must not fetch")
        },
    )
    .expect("refresh dry-run");

    assert!(!fetched);
    assert_eq!(outcome.result.payload["status"], "dry_run");
    assert_eq!(outcome.result.payload["planned"][0]["id"], record.id);
    assert_eq!(
        SourceManifest::read(temp.path())
            .expect("read manifest")
            .entries
            .len(),
        1
    );
}

#[test]
fn unchanged_content_does_not_rewrite_or_index() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_url(temp.path(), "https://example.test/a", "then", b"same");

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![record.id.clone()],
        false,
        |record, _fetched_at| Ok(snapshot(&record.location, "same")),
    )
    .expect("refresh unchanged");

    assert_eq!(outcome.result.payload["status"], "unchanged");
    assert_eq!(outcome.result.payload["unchanged"][0]["id"], record.id);
    assert_eq!(outcome.result.payload["index_status"]["status"], "not_run");
    assert_eq!(
        SourceManifest::read(temp.path())
            .expect("read manifest")
            .entries
            .len(),
        1
    );
}

#[test]
fn unchanged_local_file_does_not_replay_or_index() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_local_file(temp.path(), "notes.md", b"# Same\n");

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![record.id.clone()],
        false,
        |_record, _fetched_at| unreachable!("local refresh does not fetch URLs"),
    )
    .expect("refresh local unchanged");

    assert_eq!(outcome.result.payload["status"], "unchanged");
    assert_eq!(outcome.result.payload["unchanged"][0]["id"], record.id);
    assert_eq!(
        outcome.result.payload["unchanged"][0]["replay_kind"],
        "local_file"
    );
    assert_eq!(outcome.result.payload["index_status"]["status"], "not_run");
    assert_eq!(
        SourceManifest::read(temp.path())
            .expect("read manifest")
            .entries
            .len(),
        1
    );
}

#[test]
fn changed_content_replaces_manifest_and_removes_old_raw() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_url(temp.path(), "https://example.test/a", "then", b"old");
    let old_raw = temp
        .path()
        .join(raw_source_path(&record.id).expect("raw path"));
    fs::write(&old_raw, "old raw").expect("write old raw");

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![record.id.clone()],
        false,
        |record, _fetched_at| {
            Ok(snapshot(
                &record.location,
                "<html><title>New</title><body>new</body></html>",
            ))
        },
    )
    .expect("refresh changed");

    assert_eq!(outcome.result.payload["status"], "refreshed");
    let refreshed = &outcome.result.payload["refreshed"][0];
    assert_eq!(refreshed["old_id"], record.id);
    assert_ne!(refreshed["new_id"], refreshed["old_id"]);
    assert!(!old_raw.exists());

    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(
        manifest.entries[0].id,
        refreshed["new_id"].as_str().unwrap()
    );
}

#[test]
fn changed_local_file_replays_and_removes_old_raw_assets() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_local_file(temp.path(), "artifact.bin", b"old");
    let old_raw = temp
        .path()
        .join(raw_source_path(&record.id).expect("raw path"));
    let old_asset = temp
        .path()
        .join("raw")
        .join("assets")
        .join(format!("{}.bin", record.id));
    fs::create_dir_all(old_asset.parent().expect("asset parent")).expect("asset dir");
    fs::write(&old_raw, "old raw").expect("write old raw");
    fs::write(&old_asset, "old asset").expect("write old asset");
    fs::write(temp.path().join("artifact.bin"), b"new").expect("change source");

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![record.id.clone()],
        false,
        |_record, _fetched_at| unreachable!("local refresh does not fetch URLs"),
    )
    .expect("refresh local changed");

    assert_eq!(outcome.result.payload["status"], "refreshed");
    let refreshed = &outcome.result.payload["refreshed"][0];
    assert_eq!(refreshed["old_id"], record.id);
    assert_eq!(refreshed["replay_kind"], "local_file");
    let new_id = refreshed["new_id"].as_str().expect("new source id");
    assert_ne!(new_id, record.id);
    assert!(!old_raw.exists());
    assert!(!old_asset.exists());
    assert!(temp.path().join(format!("raw/{new_id}.md")).is_file());
    assert!(
        temp.path()
            .join(format!("raw/assets/{new_id}.bin"))
            .is_file()
    );
    assert_eq!(outcome.result.payload["index_status"]["status"], "indexed");

    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, new_id);
    assert!(manifest.entries[0].replay.is_some());
}

#[test]
fn explicit_unsupported_and_missing_sources_fail_structurally() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file = seed_legacy_file(temp.path());

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![file.id.clone(), "missing".to_string()],
        false,
        |_record, _fetched_at| unreachable!("unsupported and missing do not fetch"),
    )
    .expect("refresh unsupported");

    assert_eq!(outcome.exit_code, 1);
    assert_eq!(outcome.result.payload["status"], "failed");
    assert_eq!(
        outcome.result.payload["failed"].as_array().unwrap().len(),
        2
    );
    assert_eq!(
        outcome.result.payload["failed"][0]["code"],
        "missing_replay_metadata"
    );
    assert_eq!(outcome.result.payload["failed"][1]["code"], "not_found");
}

#[test]
fn all_source_refresh_skips_unsupported_records() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = seed_url(temp.path(), "https://example.test/a", "then", b"same");
    let file = seed_unsupported_connector(temp.path());

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        Vec::new(),
        false,
        |record, _fetched_at| Ok(snapshot(&record.location, "same")),
    )
    .expect("refresh all");

    assert_eq!(outcome.result.payload["status"], "unchanged");
    assert_eq!(outcome.result.payload["planned"][0]["id"], url.id);
    assert_eq!(outcome.result.payload["skipped"][0]["id"], file.id);
}
