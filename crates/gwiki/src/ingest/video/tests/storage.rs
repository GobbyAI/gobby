use super::*;

#[test]
fn stores_original_video() {
    let temp = tempfile::tempdir().expect("tempdir");
    let snapshot = sample_snapshot();
    let expected_hash = content_hash(&snapshot.bytes);
    let mut store = FakeWikiStore::default();

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
fn unchanged_video_reingest_reuses_immutable_raw_capture() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut store = FakeWikiStore::default();

    let first = ingest_video(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        sample_snapshot(),
    )
    .expect("first ingest");

    // Run-varying capture state: a later run may transcribe differently, so
    // the raw markdown bytes cannot be reproduced; the first capture wins.
    let mut reingest = sample_snapshot();
    reingest.fetched_at = "2026-05-30T09:00:00Z".to_string();
    reingest.transcript_segments.clear();
    let second = ingest_video(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        reingest,
    )
    .expect("unchanged re-ingest");

    assert_eq!(second.record.id, first.record.id);
    assert_eq!(second.raw_path, first.raw_path);
    let raw = std::fs::read_to_string(temp.path().join(&second.raw_path)).expect("raw markdown");
    assert!(
        raw.contains("2026-05-29T21:30:00Z"),
        "first capture time kept"
    );
    assert!(
        !raw.contains("2026-05-30T09:00:00Z"),
        "re-ingest time not written"
    );
    assert!(
        raw.contains("video_transcript_segment_count: \"2\""),
        "first capture transcript count kept"
    );
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
}

#[test]
fn stores_file_backed_video() {
    let temp = tempfile::tempdir().expect("tempdir");
    let source_path = temp.path().join("lecture-source.mp4");
    let bytes = b"\0\0\0\x18ftypmp42file-backed-video";
    std::fs::write(&source_path, bytes).expect("write source video");
    let sample = sample_snapshot();
    let expected_hash = file_content_hash(&source_path).expect("hash source video");
    let mut store = FakeWikiStore::default();

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
    let mut store = FakeWikiStore::default();

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
