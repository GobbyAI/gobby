use super::*;
use crate::IngestFileOptions;
use crate::sources::{
    CompileStatus, FetchProvenance, IngestionMethod, SourceDraft, SourceKind, SourceManifest,
    SourceReplay,
};
use std::fs;
use std::path::PathBuf;

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
            last_verified_at: fetched_at.to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
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

fn seed_file_without_replay(root: &Path) -> SourceRecord {
    SourceManifest::register(
        root,
        SourceDraft {
            location: "/tmp/source.txt".to_string(),
            kind: SourceKind::File,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            last_verified_at: "2026-06-02T00:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
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
            last_verified_at: "2026-06-02T00:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
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

fn seed_scratchpad_replay(root: &Path) -> SourceRecord {
    let record = SourceManifest::register(
        root,
        SourceDraft {
            location: "scratchpad export".to_string(),
            kind: SourceKind::File,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            last_verified_at: "2026-06-02T00:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content: b"scratchpad".to_vec(),
            title: Some("Scratchpad".to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register scratchpad source");
    let path = std::env::temp_dir()
        .join("gobby-agent-scratchpad-stale-replay")
        .join("source.txt");
    let replay = SourceReplay::local_file(path, &IngestFileOptions::default());
    SourceManifest::update(root, |manifest| {
        manifest
            .entries
            .iter_mut()
            .find(|entry| entry.id == record.id)
            .expect("seeded scratchpad source")
            .replay = Some(replay);
        Ok(true)
    })
    .expect("write scratchpad replay metadata");
    SourceManifest::read(root)
        .expect("read manifest")
        .entries
        .into_iter()
        .find(|entry| entry.id == record.id)
        .expect("updated scratchpad source")
}

fn seed_unsupported_connector(root: &Path) -> SourceRecord {
    SourceManifest::register(
        root,
        SourceDraft {
            location: "stdin:source".to_string(),
            kind: SourceKind::Stdin,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            last_verified_at: "2026-06-02T00:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
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
fn unchanged_url_refresh_promotes_and_advances_cache_freshness() {
    let temp = tempfile::tempdir().expect("tempdir");
    let project = temp.path().join("project");
    fs::create_dir_all(project.join(".gobby")).expect("create project metadata");
    fs::write(
        project.join(".gobby/gcode.json"),
        "{\n  \"id\": \"refresh-cache-test\"\n}\n",
    )
    .expect("write project identity");
    let vault = project.join("wiki");
    fs::create_dir_all(vault.join("_gwiki")).expect("create vault state");
    fs::write(vault.join("_gwiki/scope.json"), "{}\n").expect("write vault identity");
    let url = "https://example.test/refresh-cache";
    let record = seed_url(&vault, url, "unix-ms:1", b"same");
    let raw_path = crate::paths::raw_source_path(&record.id).expect("raw source path");
    fs::create_dir_all(vault.join("raw")).expect("create raw directory");
    fs::write(vault.join(&raw_path), "raw source").expect("write raw source");

    execute_with_fetcher(
        ScopeSelection::project(&project),
        vec![record.id.clone()],
        false,
        |record, fetched_at| {
            Ok(UrlSnapshot {
                requested_url: record.location.clone(),
                final_url: record.location.clone(),
                fetched_at: fetched_at.to_string(),
                body: b"same".to_vec(),
                content_type: Some("text/html".to_string()),
            })
        },
    )
    .expect("refresh unchanged URL");

    let refreshed = SourceManifest::read(&vault)
        .expect("read refreshed manifest")
        .entries
        .into_iter()
        .find(|entry| entry.id == record.id)
        .expect("refreshed record");
    assert_eq!(refreshed.fetch_provenance, FetchProvenance::Fetched);
    assert_ne!(refreshed.last_verified_at, "unix-ms:1");
    assert!(crate::support::time::parse_unix_ms(&refreshed.last_verified_at).is_some());

    let mut store = crate::store::FakeWikiStore::default();
    let cached = crate::ingest::url::ingest_urls_with_fetcher(
        &vault,
        &mut store,
        &[url.to_string()],
        &refreshed.last_verified_at,
        24,
        |_, _| unreachable!("successful refresh must make URL cacheable"),
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("ingest after refresh");
    assert_eq!(cached.cached.len(), 1);
    assert_eq!(cached.cached[0].source_id, record.id);
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
    let old_digest = temp
        .path()
        .join("knowledge/sources")
        .join(format!("{}.md", record.id));
    fs::create_dir_all(old_digest.parent().expect("digest parent")).expect("digest dir");
    fs::write(&old_digest, "old digest").expect("write old digest");

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
    assert!(
        !old_digest.exists(),
        "superseded record's derived digest must not linger as an orphan (#17651)"
    );
    let new_id = refreshed["new_id"].as_str().expect("new source id");
    assert!(temp.path().join(format!("raw/{new_id}.md")).is_file());

    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, new_id);
    // A source that was Pending stays Pending: carry-forward only preserves an
    // already-Compiled status, it never gratuitously promotes (#17708).
    assert_eq!(manifest.entries[0].compile_status, CompileStatus::Pending);
}

#[test]
fn changed_content_carries_compiled_status_forward() {
    let temp = tempfile::tempdir().expect("tempdir");
    // Seed a URL source and mark it Compiled, as a topic compile would.
    let record = seed_url(temp.path(), "https://example.test/a", "then", b"old");
    SourceManifest::update(temp.path(), |manifest| {
        let entry = manifest
            .entries
            .iter_mut()
            .find(|entry| entry.id == record.id)
            .expect("seeded record present");
        entry.compile_status = CompileStatus::Compiled;
        Ok(true)
    })
    .expect("mark compiled");

    // A re-fetch rotates the content hash (volatile chrome) but the canonical
    // location is unchanged, so the replacement record must inherit the
    // Compiled status instead of being re-pended (#17708).
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

    let refreshed = &outcome.result.payload["refreshed"][0];
    let new_id = refreshed["new_id"].as_str().expect("new source id");
    assert_ne!(
        new_id, record.id,
        "content-hash rotation mints a new record id"
    );

    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, new_id);
    assert_eq!(
        manifest.entries[0].compile_status,
        CompileStatus::Compiled,
        "an already-compiled source must not be re-pended by content-hash rotation (#17708)"
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
    let old_digest = temp
        .path()
        .join("knowledge/sources")
        .join(format!("{}.md", record.id));
    fs::create_dir_all(old_digest.parent().expect("digest parent")).expect("digest dir");
    fs::write(&old_digest, "old digest").expect("write old digest");
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
    assert!(
        !old_digest.exists(),
        "superseded record's derived digest must not linger as an orphan (#17651)"
    );
    assert!(temp.path().join(format!("raw/{new_id}.md")).is_file());
    assert!(
        temp.path()
            .join(format!("raw/assets/{new_id}.bin"))
            .is_file()
    );
    let index_status = outcome.result.payload["index_status"]["status"]
        .as_str()
        .expect("index status");
    assert!(
        matches!(index_status, "indexed" | "degraded"),
        "refresh should attempt indexing after local replay, got {index_status:?}"
    );
    if index_status == "degraded" {
        let degradations = outcome.result.payload["degradations"]
            .as_array()
            .expect("degradations");
        assert!(
            degradations.iter().any(|degradation| {
                degradation
                    .as_str()
                    .is_some_and(|message| message.starts_with("index_failed:"))
            }),
            "degraded refresh should report index failure, got {degradations:?}"
        );
    }

    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, new_id);
    assert!(manifest.entries[0].replay.is_some());
}

#[test]
fn changed_local_file_carries_compiled_status_forward() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_local_file(temp.path(), "artifact.txt", b"old");
    SourceManifest::update(temp.path(), |manifest| {
        let entry = manifest
            .entries
            .iter_mut()
            .find(|entry| entry.id == record.id)
            .expect("seeded record present");
        entry.compile_status = CompileStatus::Compiled;
        Ok(true)
    })
    .expect("mark compiled");
    fs::write(temp.path().join("artifact.txt"), b"new").expect("change source");

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        vec![record.id.clone()],
        false,
        |_record, _fetched_at| unreachable!("local refresh does not fetch URLs"),
    )
    .expect("refresh local changed");

    assert_eq!(outcome.result.payload["status"], "refreshed");
    let new_id = outcome.result.payload["refreshed"][0]["new_id"]
        .as_str()
        .expect("new source id");
    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, new_id);
    assert_eq!(manifest.entries[0].compile_status, CompileStatus::Compiled);
    assert!(manifest.entries[0].replay.is_some());
}

#[test]
fn explicit_unsupported_and_missing_sources_fail_structurally() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file = seed_file_without_replay(temp.path());

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
fn explicit_selection_reports_malformed_raw_source_ids() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut record = seed_url(temp.path(), "https://example.test/bad", "then", b"same");
    record.id = "../bad".to_string();
    let source_ids = vec![record.id.clone()];
    let entries = vec![record];

    let selection = select_sources(&entries, &source_ids);

    assert!(selection.planned.is_empty());
    assert_eq!(selection.failed.len(), 1);
    assert_eq!(selection.failed[0].code, "invalid_source_id");
    assert!(
        selection.failed[0].message.contains("unsafe source id"),
        "unexpected failure message: {}",
        selection.failed[0].message
    );
}

#[test]
fn raw_source_path_trims_source_ids() {
    assert_eq!(
        raw_source_path("  src-abc  ").expect("raw path"),
        PathBuf::from("raw/src-abc.md")
    );
}

#[test]
fn source_asset_paths_for_id_accepts_only_single_extension_assets() {
    let temp = tempfile::tempdir().expect("tempdir");
    let asset_dir = temp.path().join("raw/assets");
    fs::create_dir_all(&asset_dir).expect("asset dir");
    fs::write(asset_dir.join("source-1.png"), "asset").expect("single extension");
    fs::write(asset_dir.join("source-1.pdf.png"), "derived").expect("derived extension");
    fs::write(asset_dir.join("source-10.png"), "other source").expect("other source");
    fs::write(asset_dir.join("source-1."), "empty extension").expect("empty extension");

    let mut paths =
        crate::paths::source_asset_paths_for_id(temp.path(), "source-1").expect("asset paths");
    paths.sort();

    assert_eq!(paths, vec![PathBuf::from("raw/assets/source-1.png")]);
}

#[test]
fn remove_relative_file_rejects_unsafe_paths_before_join() {
    let temp = tempfile::tempdir().expect("tempdir");
    for relative in [
        Path::new(""),
        Path::new("."),
        Path::new("../raw/source.md"),
        Path::new("/raw/source.md"),
    ] {
        assert!(
            matches!(
                remove_relative_file(temp.path(), relative),
                Err(WikiError::InvalidInput { .. })
            ),
            "expected invalid path for {}",
            relative.display()
        );
    }
}

#[test]
fn remove_relative_file_normalizes_current_dir_components() {
    let temp = tempfile::tempdir().expect("tempdir");
    let raw_dir = temp.path().join("raw");
    fs::create_dir_all(&raw_dir).expect("raw dir");
    fs::write(raw_dir.join("source.md"), "raw").expect("raw file");

    assert!(
        remove_relative_file(temp.path(), Path::new("raw/./source.md"))
            .expect("remove normalized file")
    );
    assert!(!raw_dir.join("source.md").exists());
}

#[test]
fn refresh_url_accepts_case_insensitive_http_scheme() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut record = seed_url(
        temp.path(),
        "HTTPS://Example.test/Page",
        "unix-ms:1",
        b"body",
    );
    record.canonical_location = "https://canonical.example/page".to_string();

    assert_eq!(refresh_url(&record), "HTTPS://Example.test/Page");
}

#[test]
fn invalid_http_like_locations_are_not_url_sources() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_url(temp.path(), "https://", "unix-ms:1", b"body");

    assert_eq!(
        replay_kind(&record),
        Err(SelectionFailure::UnsupportedSourceKind)
    );
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

#[test]
fn all_source_refresh_skips_missing_replay_metadata_records() {
    let temp = tempfile::tempdir().expect("tempdir");
    let url = seed_url(temp.path(), "https://example.test/a", "then", b"same");
    let file = seed_file_without_replay(temp.path());

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        Vec::new(),
        false,
        |record, _fetched_at| Ok(snapshot(&record.location, "same")),
    )
    .expect("refresh all");

    assert_eq!(outcome.exit_code, 0);
    assert_eq!(outcome.result.payload["status"], "unchanged");
    assert_eq!(outcome.result.payload["planned"][0]["id"], url.id);
    assert!(
        outcome.result.payload["failed"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(outcome.result.payload["skipped"][0]["id"], file.id);
    assert_eq!(
        outcome.result.payload["skipped"][0]["code"],
        "missing_replay_metadata"
    );
}

#[test]
fn all_source_refresh_repairs_stale_scratchpad_replay_metadata() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_scratchpad_replay(temp.path());

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        Vec::new(),
        false,
        |_record, _fetched_at| unreachable!("scratchpad replay should be stripped before planning"),
    )
    .expect("refresh all");

    assert_eq!(outcome.exit_code, 0);
    assert_eq!(outcome.result.payload["skipped"][0]["id"], record.id);
    assert_eq!(
        outcome.result.payload["skipped"][0]["code"],
        "missing_replay_metadata"
    );
    assert!(
        !outcome.result.payload["failed"]
            .as_array()
            .unwrap()
            .iter()
            .any(|failure| failure["code"] == "missing_local_file")
    );
    let repaired = SourceManifest::read(temp.path()).expect("read repaired manifest");
    let entry = repaired
        .entries
        .iter()
        .find(|entry| entry.id == record.id)
        .expect("repaired entry");
    assert!(entry.replay.is_none());
}

#[test]
fn dry_run_classifies_stale_scratchpad_replay_without_persisting_repair() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = seed_scratchpad_replay(temp.path());

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        Vec::new(),
        true,
        |_record, _fetched_at| unreachable!("dry-run should not fetch"),
    )
    .expect("refresh dry-run");

    assert_eq!(outcome.result.payload["status"], "dry_run");
    assert_eq!(outcome.result.payload["skipped"][0]["id"], record.id);
    assert_eq!(
        outcome.result.payload["skipped"][0]["code"],
        "missing_replay_metadata"
    );
    let manifest = SourceManifest::read(temp.path()).expect("read manifest");
    let entry = manifest
        .entries
        .iter()
        .find(|entry| entry.id == record.id)
        .expect("manifest entry");
    assert!(entry.replay.is_some());
}

#[test]
fn refresh_forwards_url_failure_codes_and_messages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let status = seed_url(temp.path(), "https://example.test/status", "then", b"old");
    let too_large = seed_url(temp.path(), "https://example.test/large", "then", b"old");

    let outcome = execute_resolved_with_fetcher(
        test_scope(temp.path()),
        Vec::new(),
        false,
        |record, _fetched_at| {
            if record.id == status.id {
                Err(UrlIngestFailure {
                    url: record.location.clone(),
                    code: "http_status".to_string(),
                    message: "HTTP status 404".to_string(),
                })
            } else {
                Err(UrlIngestFailure {
                    url: record.location.clone(),
                    code: "response_too_large".to_string(),
                    message: "response exceeds GWIKI_MAX_INBOX_ITEM_BYTES limit of 8 bytes"
                        .to_string(),
                })
            }
        },
    )
    .expect("refresh all");

    let failures = outcome.result.payload["failed"].as_array().unwrap();
    assert!(failures.iter().any(|failure| {
        failure["id"] == status.id
            && failure["code"] == "http_status"
            && failure["message"] == "HTTP status 404"
    }));
    assert!(failures.iter().any(|failure| {
        failure["id"] == too_large.id
            && failure["code"] == "response_too_large"
            && failure["message"] == "response exceeds GWIKI_MAX_INBOX_ITEM_BYTES limit of 8 bytes"
    }));
}
