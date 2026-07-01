use super::*;

#[test]
fn production_ingest_applies_degradation_matrix() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vision = FakeVisionClient;

    let no_ffmpeg = ingest_with_media(
        temp.path(),
        FakeVideoMediaExtractor {
            audio_bytes: Vec::new(),
            frames: Vec::new(),
            fail_audio: Some("ffmpeg is unavailable"),
            fail_frames: Some("ffmpeg is unavailable"),
        },
        TranscriptionEndpoint::Available(Box::new(FakeTranscriptionClient)),
        VisionEndpoint::Available(&vision),
        "no-ffmpeg.mp4",
    )
    .expect("no ffmpeg degrades");
    assert_asset_preserved(temp.path(), &no_ffmpeg, b"video bytes");
    let no_ffmpeg_doc = read_derived(temp.path(), &no_ffmpeg);
    assert!(no_ffmpeg_doc.contains("file_size_bytes: 11"));
    assert!(
        no_ffmpeg_doc.contains("audio:ffmpeg_unavailable")
            || no_ffmpeg_doc.contains("frames:ffmpeg_unavailable")
    );

    let frames_fail = ingest_with_media(
        temp.path(),
        FakeVideoMediaExtractor {
            audio_bytes: b"extracted audio".to_vec(),
            frames: Vec::new(),
            fail_audio: None,
            fail_frames: Some("frame extraction failed"),
        },
        TranscriptionEndpoint::Available(Box::new(FakeTranscriptionClient)),
        VisionEndpoint::Available(&vision),
        "frames-fail.mp4",
    )
    .expect("frame extraction degrades");
    let frames_fail_doc = read_derived(temp.path(), &frames_fail);
    assert!(frames_fail_doc.contains("media_degradation: \"frames:extraction_failed\""));
    assert!(frames_fail_doc.contains("Audio-first transcript from extracted video audio."));
    assert!(frames_fail_doc.contains("No frame samples recorded."));

    let vision_unavailable = ingest_with_media(
        temp.path(),
        FakeVideoMediaExtractor {
            audio_bytes: b"extracted audio".to_vec(),
            frames: vec![(0, b"frame-zero".to_vec())],
            fail_audio: None,
            fail_frames: None,
        },
        TranscriptionEndpoint::Available(Box::new(FakeTranscriptionClient)),
        VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
            reason: gobby_core::degradation::ModalityDegradationReason::Disabled,
            fallback: "skip frames".to_string(),
        }),
        "vision-unavailable.mp4",
    )
    .expect("vision unavailable degrades");
    let vision_unavailable_doc = read_derived(temp.path(), &vision_unavailable);
    assert!(vision_unavailable_doc.contains("media_degradation: \"frames:vision_unavailable\""));
    assert!(vision_unavailable_doc.contains("No frame samples recorded."));

    let transcription_unavailable = ingest_with_media(
        temp.path(),
        FakeVideoMediaExtractor {
            audio_bytes: b"extracted audio".to_vec(),
            frames: vec![(0, b"frame-zero".to_vec())],
            fail_audio: None,
            fail_frames: None,
        },
        TranscriptionEndpoint::Unavailable(crate::transcribe::TranscriptionDegradation {
            reason: gobby_core::degradation::ModalityDegradationReason::Disabled,
            fallback: "skip audio".to_string(),
        }),
        VisionEndpoint::Available(&vision),
        "transcription-unavailable.mp4",
    )
    .expect("transcription unavailable degrades");
    let transcription_unavailable_doc = read_derived(temp.path(), &transcription_unavailable);
    assert!(transcription_unavailable_doc.contains("transcription_status: degraded"));
    assert!(transcription_unavailable_doc.contains("transcription_degradation: unavailable"));
    assert!(transcription_unavailable_doc.contains("disabled: skip audio"));

    let stt_fail = ingest_with_media(
        temp.path(),
        FakeVideoMediaExtractor {
            audio_bytes: b"extracted audio".to_vec(),
            frames: vec![(0, b"frame-zero".to_vec())],
            fail_audio: None,
            fail_frames: None,
        },
        TranscriptionEndpoint::Available(Box::new(FailingTranscriptionClient)),
        VisionEndpoint::Available(&vision),
        "stt-fail.mp4",
    )
    .expect("stt degrades");
    let stt_fail_doc = read_derived(temp.path(), &stt_fail);
    assert!(stt_fail_doc.contains("transcription_status: degraded"));
    assert!(stt_fail_doc.contains("transcription_degradation: transcription_error"));
    assert!(stt_fail_doc.contains("frame stt-fail.mp4.frame-0000.jpg has 10 bytes"));

    #[cfg(feature = "ai")]
    {
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
        let partial = ingest_with_media(
            temp.path(),
            FakeVideoMediaExtractor {
                audio_bytes: vec![b'a'; crate::ai::chunk::MAX_AUDIO_UPLOAD_BYTES + 1],
                frames: Vec::new(),
                fail_audio: None,
                fail_frames: None,
            },
            TranscriptionEndpoint::Available(Box::new(ScriptedChunkTranscriptionClient {
                outputs: RefCell::new(vec![
                    Ok(transcript_output(
                        "en",
                        false,
                        "transcribe",
                        &[(0, 1_000, "completed chunk")],
                    )),
                    Err(WikiError::Config {
                        detail: "provider failed mid chunk".to_string(),
                    }),
                ]),
            })),
            VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
                reason: gobby_core::degradation::ModalityDegradationReason::Disabled,
                fallback: "skip frames".to_string(),
            }),
            "partial-chunk.mp4",
        )
        .expect("partial chunk aggregate degrades");
        let partial_doc = read_derived(temp.path(), &partial);
        assert!(partial_doc.contains("transcription_partial: \"true\""));
        assert!(partial_doc.contains("transcription_missing_ranges: 9000-19000"));
        assert!(partial_doc.contains("[00:00:00] completed chunk"));
    }
}

#[test]
fn video_media_degradation_classifies_only_unavailable_ffmpeg_errors() {
    let unavailable = video_media_degradation(
        "audio",
        "extraction_failed",
        WikiError::Config {
            detail: "ffmpeg executable not found on PATH".to_string(),
        },
    );
    assert_eq!(unavailable.reason, "ffmpeg_unavailable");

    let failed = video_media_degradation(
        "frames",
        "extraction_failed",
        WikiError::Config {
            detail: "ffmpeg frame extraction failed".to_string(),
        },
    );
    assert_eq!(failed.reason, "extraction_failed");
}

#[test]
fn frame_vision_failure_keeps_sample_without_description() {
    let frame = temp_file_with_bytes(".jpg", b"frame-zero").expect("frame temp");

    let described = describe_frame_images(
        "lecture.mp4",
        vec![(0, frame)],
        VisionEndpoint::Available(&FailingVisionClient),
    )
    .expect("vision failure should keep frame sample");

    assert_eq!(described.samples.len(), 1);
    assert_eq!(described.paths.len(), 1);
    assert!(described.descriptions.is_empty());
    assert!(described.paths[0].exists(), "frame should be persisted");
    cleanup_kept_temp_frames(&described.paths);
}
