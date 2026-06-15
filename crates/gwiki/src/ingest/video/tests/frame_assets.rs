use super::*;

#[test]
fn persisted_frame_sources_are_removed_after_successful_loop() {
    let vault = tempfile::tempdir().expect("vault tempdir");
    let record = SourceManifest::register(
        vault.path(),
        SourceDraft {
            location: "/tmp/video.mp4".to_string(),
            kind: SourceKind::Video,
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            content: Vec::new(),
            title: Some("video.mp4".to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("source record");
    let kept = temp_file_with_bytes(".jpg", b"shared frame").expect("frame temp");
    let kept_path = kept.into_temp_path().keep().expect("keep frame");
    let samples = vec![
        VideoFrameSample {
            timestamp_seconds: 0,
            timestamp: "00:00:00".to_string(),
            source_asset: kept_path.clone(),
            source_reference: path_to_string(&kept_path),
        },
        VideoFrameSample {
            timestamp_seconds: 4,
            timestamp: "00:00:04".to_string(),
            source_asset: kept_path.clone(),
            source_reference: path_to_string(&kept_path),
        },
    ];

    let _persisted = persist_video_frame_assets(
        vault.path(),
        &record,
        "video.mp4",
        samples,
        &[kept_path.clone(), kept_path.clone()],
        &[],
    )
    .expect("persist shared frame source");

    assert!(
        !kept_path.exists(),
        "source temp frame should be removed after all frames are persisted"
    );
}

#[test]
fn persisted_frame_read_failure_drops_remaining_kept_temp_frames() {
    let vault = tempfile::tempdir().expect("vault tempdir");
    let record = SourceManifest::register(
        vault.path(),
        SourceDraft {
            location: "/tmp/video.mp4".to_string(),
            kind: SourceKind::Video,
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            content: Vec::new(),
            title: Some("video.mp4".to_string()),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("source record");
    let missing_path = std::env::temp_dir().join(format!(
        "gwiki-missing-frame-{}-{}.jpg",
        std::process::id(),
        "read-failure"
    ));
    let _ = std::fs::remove_file(&missing_path);
    let kept = temp_file_with_bytes(".jpg", b"kept frame").expect("frame temp");
    let kept_path = kept.into_temp_path().keep().expect("keep frame");
    let samples = vec![
        VideoFrameSample {
            timestamp_seconds: 0,
            timestamp: "00:00:00".to_string(),
            source_asset: missing_path.clone(),
            source_reference: path_to_string(&missing_path),
        },
        VideoFrameSample {
            timestamp_seconds: 4,
            timestamp: "00:00:04".to_string(),
            source_asset: kept_path.clone(),
            source_reference: path_to_string(&kept_path),
        },
    ];

    let error = persist_video_frame_assets(
        vault.path(),
        &record,
        "video.mp4",
        samples,
        &[missing_path, kept_path.clone()],
        &[],
    )
    .expect_err("missing frame read fails");

    assert!(error.to_string().contains("read sampled video frame asset"));
    assert!(
        !kept_path.exists(),
        "remaining kept temp frame should be cleaned"
    );
}
