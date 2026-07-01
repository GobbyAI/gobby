use super::*;

#[cfg(feature = "ai")]
#[test]
fn video_long_english_translation_reuses_chunk_branch() {
    let _chunks = crate::ai::chunk::install_test_chunks(vec![
        crate::ai::chunk::AudioChunk {
            start_ms: 0,
            end_ms: 10_000,
            file_name: "chunk-0.wav".to_string(),
            path: PathBuf::from("chunk-0.wav"),
            bytes: vec![b'w', b'a', b'v'],
        },
        crate::ai::chunk::AudioChunk {
            start_ms: 9_000,
            end_ms: 19_000,
            file_name: "chunk-1.wav".to_string(),
            path: PathBuf::from("chunk-1.wav"),
            bytes: vec![b'w', b'a', b'v'],
        },
    ]);
    let temp = tempfile::tempdir().expect("tempdir");
    let source_path = temp.path().join("lecture-source.mp4");
    std::fs::write(&source_path, b"video bytes").expect("write source video");
    let mut store = MemoryWikiStore::default();
    let media = FakeVideoMediaExtractor {
        audio_bytes: vec![b'a'; crate::ai::chunk::MAX_AUDIO_UPLOAD_BYTES + 1],
        frames: Vec::new(),
        fail_audio: None,
        fail_frames: None,
    };
    let calls = Rc::new(RefCell::new(Vec::new()));
    let client = ScriptedTranscriptionClient {
        english: RefCell::new(vec![
            Ok(transcript_output(
                "es",
                true,
                "translate",
                &[(0, 1_000, "hello")],
            )),
            Err(WikiError::Config {
                detail: "chunk failed".to_string(),
            }),
        ]),
        calls: Rc::clone(&calls),
    };

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
            duration_seconds: Some(20),
            frame_interval_seconds: Some(0),
            frame_samples: Vec::new(),
            frame_descriptions: Vec::new(),
            frame_image_paths: Vec::new(),
            transcript_segments: Vec::new(),
            transcription: None,
        },
        TranscriptionEndpoint::Translating {
            client: Box::new(client),
            target_lang: Some("en".to_string()),
            language_hint: Some("es".to_string()),
        },
        VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
            reason: gobby_core::degradation::ModalityDegradationReason::Disabled,
            fallback: "skip frames".to_string(),
        }),
        &media,
    )
    .expect("ingest long translated video");

    let document = store
        .documents
        .get(&result.derived_path)
        .expect("derived video document indexed");
    assert!(document.body.contains("transcription_source_language: es"));
    assert!(document.body.contains("transcription_target_language: en"));
    assert!(document.body.contains("translated: \"true\""));
    assert!(document.body.contains("transcription_partial: \"true\""));
    assert!(
        document
            .body
            .contains("transcription_missing_ranges: 9000-19000")
    );
    assert!(document.body.contains("[00:00:00] hello"));
    assert_eq!(
        calls.borrow().as_slice(),
        ["translate_to_english", "translate_to_english"]
    );
}
