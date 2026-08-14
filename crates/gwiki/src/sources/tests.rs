use super::render::{canonicalize_location, existing_index_without_manifest};
use super::*;
use crate::IngestFileOptions;
use std::path::PathBuf;

#[test]
fn dedupes_by_canonical_identity_and_hash() {
    let temp = tempfile::tempdir().expect("tempdir");
    let content = b"Source material about durable wiki provenance.".to_vec();

    let first = SourceManifest::register(
        temp.path(),
        SourceDraft::url(
            "https://Example.com/docs/provenance#chunk-1",
            "2026-05-29T15:00:00Z",
            content.clone(),
        )
        .with_title("Durable provenance")
        .with_citation("Example Docs, Durable provenance")
        .with_license("Apache-2.0"),
    )
    .expect("first source registered");
    let duplicate = SourceManifest::register(
        temp.path(),
        SourceDraft::url(
            "https://example.com/docs/provenance/",
            "2026-05-29T16:00:00Z",
            content,
        )
        .with_title("Duplicate durable provenance")
        .with_citation("Duplicate citation should not replace record")
        .with_license("MIT"),
    )
    .expect("duplicate source reused");

    assert_eq!(duplicate, first);

    let index = std::fs::read_to_string(SourceManifest::index_path(temp.path()))
        .expect("raw index written");
    assert_eq!(index.matches("gwiki-source:").count(), 1);
    assert!(index.contains("https://Example.com/docs/provenance#chunk-1"));
    assert!(index.contains("kind: `url`"));
    assert!(index.contains("fetched_at: `2026-05-29T15:00:00Z`"));
    assert!(index.contains(&first.content_hash));
    assert!(index.contains("citation: Example Docs, Durable provenance"));
    assert!(index.contains("license: Apache-2.0"));
    assert!(index.contains("ingestion_method: `manual`"));
    assert!(index.contains("compile_status: `pending`"));
}

#[test]
fn supersede_location_removes_stale_records_and_their_files() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let location = "/notes/recaptured.md";

    let stale = SourceManifest::register(
        vault,
        SourceDraft::new(
            location,
            SourceKind::Markdown,
            "2026-07-01T00:00:00Z",
            b"first capture".to_vec(),
        ),
    )
    .expect("register stale record");
    let kept = SourceManifest::register(
        vault,
        SourceDraft::new(
            location,
            SourceKind::Markdown,
            "2026-07-02T00:00:00Z",
            b"second capture".to_vec(),
        ),
    )
    .expect("register kept record");
    assert_ne!(stale.id, kept.id);

    let stale_raw = vault.join(format!("raw/{}.md", stale.id));
    let stale_digest = vault.join(format!("knowledge/sources/{}.md", stale.id));
    let stale_asset = vault.join(format!("raw/assets/{}.pdf", stale.id));
    let kept_raw = vault.join(format!("raw/{}.md", kept.id));
    for path in [&stale_raw, &stale_digest, &stale_asset, &kept_raw] {
        std::fs::create_dir_all(path.parent().expect("parent")).expect("dir");
        std::fs::write(path, "body").expect("file");
    }

    let removed = SourceManifest::supersede_location(vault, &kept).expect("supersede stale record");

    assert_eq!(
        removed,
        vec![
            PathBuf::from(format!("raw/{}.md", stale.id)),
            PathBuf::from(format!("knowledge/sources/{}.md", stale.id)),
            PathBuf::from(format!("raw/assets/{}.pdf", stale.id)),
        ]
    );
    assert!(!stale_raw.exists());
    assert!(!stale_digest.exists());
    assert!(!stale_asset.exists());
    assert!(kept_raw.exists());
    let manifest = SourceManifest::read(vault).expect("read manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, kept.id);

    // Idempotent once the location has a single record.
    let removed_again =
        SourceManifest::supersede_location(vault, &kept).expect("supersede idempotent");
    assert_eq!(removed_again, Vec::<PathBuf>::new());
    assert!(kept_raw.exists());
}

#[test]
fn local_file_replay_metadata_round_trips_through_manifest() {
    let temp = tempfile::tempdir().expect("tempdir");
    let record = SourceManifest::register(
        temp.path(),
        SourceDraft {
            location: "notes/source.md".to_string(),
            kind: SourceKind::Markdown,
            fetched_at: "2026-06-02T00:00:00Z".to_string(),
            last_verified_at: "2026-06-02T00:00:00Z".to_string(),
            fetch_provenance: FetchProvenance::Stub,
            content: b"# Source\n".to_vec(),
            title: Some("Source".to_string()),
            citation: Some("notes/source.md".to_string()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register source");
    let options = IngestFileOptions {
        no_ai: true,
        translate: true,
        target_lang: Some("es".to_string()),
        video_frame_interval_seconds: Some(11),
    };
    let replay = SourceReplay::local_file(PathBuf::from("notes/source.md"), &options);

    SourceManifest::update(temp.path(), |manifest| {
        manifest.entries[0].replay = Some(replay);
        Ok(true)
    })
    .expect("write replay metadata");

    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].id, record.id);
    let Some(SourceReplay::LocalFile {
        path,
        options: replay_options,
    }) = &manifest.entries[0].replay
    else {
        panic!("expected local file replay");
    };
    assert_eq!(path, &PathBuf::from("notes/source.md"));
    assert!(replay_options.no_ai);
    assert!(replay_options.translate);
    assert_eq!(replay_options.target_lang.as_deref(), Some("es"));
    assert_eq!(replay_options.video_frame_interval_seconds, Some(11));

    let restored = replay_options
        .to_ingest_file_options()
        .expect("replay options parse");
    assert_eq!(restored, options);
    let index = std::fs::read_to_string(SourceManifest::index_path(temp.path()))
        .expect("raw index written");
    assert!(index.contains("\"replay\""));
    assert!(index.contains("\"kind\":\"local_file\""));
}

#[test]
fn private_tmp_scratchpad_replay_path_is_ephemeral() {
    let path = PathBuf::from("/private/tmp/session-1/scratchpad/source.md");

    assert!(is_ephemeral_scratchpad_replay_path(&path));
    assert!(!is_ephemeral_scratchpad_replay_path(&PathBuf::from(
        "/private/tmp/ordinary/source.md"
    )));
    assert!(!is_ephemeral_scratchpad_replay_path(&PathBuf::from(
        "scratchpad/source.md"
    )));
}

#[test]
fn canonical_location_sorts_query_before_trimming_slash() {
    assert_eq!(
        canonicalize_location("https://Example.com/docs/?b=2&a=1#frag"),
        "https://example.com/docs?a=1&b=2"
    );
}

#[test]
fn existing_index_strips_unmarked_manifest_until_next_heading() {
    let temp = tempfile::tempdir().expect("tempdir");
    let index_path = SourceManifest::index_path(temp.path());
    std::fs::create_dir_all(index_path.parent().expect("raw dir")).expect("raw dir");
    std::fs::write(
        &index_path,
        "# Raw Sources\n\nManual note.\n\n## Source manifest\n\n- generated\n\n## Generated Heading\n\n- stale generated content\n",
    )
    .expect("index");

    let stripped = existing_index_without_manifest(&index_path).expect("strip manifest");

    assert!(stripped.prefix.contains("Manual note."));
    assert!(!stripped.prefix.contains("Generated Heading"));
    assert!(stripped.suffix.contains("Generated Heading"));
    assert!(stripped.suffix.contains("stale generated content"));
}

#[test]
fn existing_index_preserves_content_after_marked_manifest() {
    let temp = tempfile::tempdir().expect("tempdir");
    let index_path = SourceManifest::index_path(temp.path());
    std::fs::create_dir_all(index_path.parent().expect("raw dir")).expect("raw dir");
    std::fs::write(
        &index_path,
        format!(
            "# Raw Sources\n\nManual before.\n\n{GENERATED_SOURCE_MANIFEST_START}\n## Source manifest\n\n- generated\n{GENERATED_SOURCE_MANIFEST_END}\n\nManual after.\n",
        ),
    )
    .expect("index");

    let stripped = existing_index_without_manifest(&index_path).expect("strip manifest");

    assert!(stripped.prefix.contains("Manual before."));
    assert!(!stripped.prefix.contains("generated"));
    assert!(stripped.suffix.contains("Manual after."));
}
