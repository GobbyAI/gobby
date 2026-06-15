use super::*;

#[test]
fn video_produces_transcript_and_frames() {
    let temp = tempfile::tempdir().expect("tempdir");
    let source_path = temp.path().join("lecture-source.mp4");
    std::fs::write(&source_path, b"video bytes").expect("write source video");
    let mut store = MemoryWikiStore::default();
    let media = FakeVideoMediaExtractor {
        audio_bytes: b"extracted audio".to_vec(),
        frames: vec![(0, b"frame-zero".to_vec()), (4_000, b"frame-four".to_vec())],
        fail_audio: None,
        fail_frames: None,
    };
    let vision = FakeVisionClient;

    let result = ingest_video_file_with_processing(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        VideoFileSnapshot {
            location: "/tmp/lecture.mp4".to_string(),
            file_name: "lecture.mp4".to_string(),
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            path: source_path,
            mime_type: Some("video/mp4".to_string()),
            duration_seconds: Some(8),
            frame_interval_seconds: Some(4),
            frame_samples: Vec::new(),
            frame_descriptions: Vec::new(),
            frame_image_paths: Vec::new(),
            transcript_segments: Vec::new(),
            transcription: None,
        },
        TranscriptionEndpoint::Available(Box::new(FakeTranscriptionClient)),
        VisionEndpoint::Available(&vision),
        &media,
    )
    .expect("ingest processed video");

    assert_eq!(result.frame_samples.len(), 2);
    assert_eq!(result.aligned_segments.len(), 2);
    assert!(
        result
            .frame_samples
            .iter()
            .all(|sample| sample.source_reference.starts_with("raw/assets/"))
    );
    assert!(
        result
            .aligned_segments
            .iter()
            .flat_map(|segment| &segment.frame_descriptions)
            .all(|description| description.source_reference.starts_with("raw/assets/"))
    );
    for sample in &result.frame_samples {
        assert!(temp.path().join(&sample.source_asset).exists());
    }
    let document = store
        .documents
        .get(&result.derived_path)
        .expect("derived video document indexed");
    assert!(document.body.contains("video_frame_image_count: 2"));
    assert!(document.body.contains("video_frame_description_count: 2"));
    assert!(document.body.contains("video_transcript_segment_count: 1"));
    assert!(
        document
            .body
            .contains("Audio-first transcript from extracted video audio.")
    );
    assert!(
        document
            .body
            .contains("frame lecture.mp4.frame-0000.jpg has 10 bytes")
    );
}

#[test]
fn frame_interval_zero_disables_frames() {
    let temp = tempfile::tempdir().expect("tempdir");
    let source_path = temp.path().join("lecture-source.mp4");
    std::fs::write(&source_path, b"video bytes").expect("write source video");
    let mut store = MemoryWikiStore::default();
    let media = FakeVideoMediaExtractor {
        audio_bytes: b"extracted audio".to_vec(),
        frames: vec![(0, b"should-not-be-sampled".to_vec())],
        fail_audio: None,
        fail_frames: None,
    };
    let vision = FakeVisionClient;

    let result = ingest_video_file_with_processing(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        VideoFileSnapshot {
            location: "/tmp/lecture.mp4".to_string(),
            file_name: "lecture.mp4".to_string(),
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            path: source_path,
            mime_type: Some("video/mp4".to_string()),
            duration_seconds: Some(8),
            frame_interval_seconds: Some(0),
            frame_samples: Vec::new(),
            frame_descriptions: Vec::new(),
            frame_image_paths: Vec::new(),
            transcript_segments: Vec::new(),
            transcription: None,
        },
        TranscriptionEndpoint::Available(Box::new(FakeTranscriptionClient)),
        VisionEndpoint::Available(&vision),
        &media,
    )
    .expect("ingest audio-only video");

    assert!(result.frame_samples.is_empty());
    let document = store
        .documents
        .get(&result.derived_path)
        .expect("derived video document indexed");
    assert!(document.body.contains("video_frame_sample_count: 0"));
    assert!(document.body.contains("video_frame_image_count: 0"));
    assert!(document.body.contains("video_frame_description_count: 0"));
    assert!(document.body.contains("video_transcript_segment_count: 1"));
    assert!(document.body.contains("No frame samples recorded."));
}
