use super::*;

#[test]
fn stores_original_video() {
    let temp = tempfile::tempdir().expect("tempdir");
    let snapshot = sample_snapshot();
    let expected_hash = content_hash(&snapshot.bytes);
    let mut store = MemoryWikiStore::default();

    let result = ingest_video(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        snapshot.clone(),
    )
    .expect("ingest video");

    assert_eq!(result.asset_path.parent(), Some(Path::new("raw/assets")));
    assert_eq!(
        std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
        snapshot.bytes
    );
    let raw = std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown");
    assert!(raw.contains("source_kind: video"));
    assert!(raw.contains("source_asset: raw/assets/"));
    assert!(raw.contains("video_mime_type: video/mp4"));
    assert!(raw.contains("video_duration_seconds: \"8\""));
    assert!(raw.contains("video_frame_interval_seconds: \"4\""));

    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].kind, SourceKind::Video);
    assert_eq!(manifest.entries[0].content_hash, expected_hash);
}

#[test]
fn stores_file_backed_video() {
    let temp = tempfile::tempdir().expect("tempdir");
    let source_path = temp.path().join("lecture-source.mp4");
    let bytes = b"\0\0\0\x18ftypmp42file-backed-video";
    std::fs::write(&source_path, bytes).expect("write source video");
    let sample = sample_snapshot();
    let expected_hash = file_content_hash(&source_path).expect("hash source video");
    let mut store = MemoryWikiStore::default();

    let result = ingest_video_file(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        VideoFileSnapshot {
            location: sample.location,
            file_name: sample.file_name,
            fetched_at: sample.fetched_at,
            path: source_path,
            mime_type: sample.mime_type,
            duration_seconds: sample.duration_seconds,
            frame_interval_seconds: sample.frame_interval_seconds,
            frame_samples: sample.frame_samples,
            frame_image_paths: sample.frame_image_paths,
            frame_descriptions: sample.frame_descriptions,
            transcript_segments: sample.transcript_segments,
            transcription: sample.transcription,
        },
    )
    .expect("ingest file-backed video");

    assert_eq!(
        std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
        bytes
    );
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries[0].content_hash, expected_hash);
    assert!(store.sources.contains_key(&result.derived_path));
}

#[test]
fn video_derivatives_keep_provenance() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut store = MemoryWikiStore::default();

    let result = ingest_video(
        temp.path(),
        &mut store,
        ScopeIdentity::project("project-123"),
        sample_snapshot(),
    )
    .expect("ingest video");

    let document = store
        .documents
        .get(&result.derived_path)
        .expect("derived video document indexed");
    assert_eq!(document.kind, WikiDocumentKind::SourceNote);
    assert!(document.body.contains("source_kind: video"));
    assert!(document.body.contains("source_asset: raw/assets/"));
    assert!(document.body.contains("source_raw: raw/"));
    assert!(document.body.contains("video_frame_interval_seconds: 4"));
    assert!(document.body.contains("scope_kind: project"));
    assert!(document.body.contains("scope_id: project-123"));
    assert!(document.body.contains("Original video: `raw/assets/"));
    assert!(document.body.contains("Audio reference: `raw/assets/"));
    assert!(
        document
            .body
            .contains("Speaker stands beside a field recorder.")
    );
    assert!(
        document
            .body
            .contains("Each transcript segment lines up with sampled frames.")
    );
    assert!(store.sources.contains_key(&result.derived_path));
}
