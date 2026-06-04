#[cfg(feature = "ai")]
use std::cell::RefCell;
use std::io::Write;
use std::time::Duration;

use gobby_core::indexing::{content_hash, file_content_hash};

use super::*;
use crate::sources::{SourceKind, SourceManifest};
use crate::store::{MemoryWikiStore, WikiDocumentKind};
use crate::transcribe::{
    TranscriptionClient, TranscriptionEndpoint, TranscriptionOutput, TranscriptionRequest,
};
use crate::vision::{VisionClient, VisionEndpoint, VisionExtraction, VisionRequest};

fn sample_snapshot() -> VideoSnapshot {
    VideoSnapshot {
        location: "/tmp/lecture.mp4".to_string(),
        file_name: "lecture.mp4".to_string(),
        fetched_at: "2026-05-29T21:30:00Z".to_string(),
        bytes: b"\0\0\0\x18ftypmp42video-bytes".to_vec(),
        mime_type: Some("video/mp4".to_string()),
        duration_seconds: Some(8),
        frame_interval_seconds: Some(4),
        frame_samples: Vec::new(),
        frame_image_paths: Vec::new(),
        frame_descriptions: vec![
            VideoFrameDescription {
                timestamp: "00:00:00".to_string(),
                source_reference: "raw/assets/lecture.mp4#t=00:00:00".to_string(),
                description: "Speaker stands beside a field recorder.".to_string(),
            },
            VideoFrameDescription {
                timestamp: "00:00:04".to_string(),
                source_reference: "raw/assets/lecture.mp4#t=00:00:04".to_string(),
                description: "Slide shows synchronized audio and frames.".to_string(),
            },
        ],
        transcript_segments: vec![
            TranscriptSegment {
                start_ms: 1_000,
                end_ms: 3_000,
                text: "We start by recording the scene.".to_string(),
            },
            TranscriptSegment {
                start_ms: 5_000,
                end_ms: 7_000,
                text: "Each transcript segment lines up with sampled frames.".to_string(),
            },
        ],
        transcription: None,
    }
}

struct FakeVideoMediaExtractor {
    audio_bytes: Vec<u8>,
    frames: Vec<(u64, Vec<u8>)>,
    fail_audio: Option<&'static str>,
    fail_frames: Option<&'static str>,
}

impl VideoMediaExtractor for FakeVideoMediaExtractor {
    fn extract_audio(&self, _video: &Path) -> Result<tempfile::NamedTempFile, WikiError> {
        if let Some(detail) = self.fail_audio {
            return Err(WikiError::Config {
                detail: detail.to_string(),
            });
        }
        temp_file_with_bytes(".wav", &self.audio_bytes)
    }

    fn sample_frame_images(
        &self,
        _video: &Path,
        _interval: Duration,
    ) -> Result<Vec<(u64, tempfile::NamedTempFile)>, WikiError> {
        if let Some(detail) = self.fail_frames {
            return Err(WikiError::Config {
                detail: detail.to_string(),
            });
        }
        self.frames
            .iter()
            .map(|(start_ms, bytes)| Ok((*start_ms, temp_file_with_bytes(".jpg", bytes)?)))
            .collect()
    }
}

fn temp_file_with_bytes(suffix: &str, bytes: &[u8]) -> Result<tempfile::NamedTempFile, WikiError> {
    let mut file = tempfile::Builder::new()
        .suffix(suffix)
        .tempfile()
        .map_err(|source| WikiError::Io {
            action: "create test media file",
            path: None,
            source,
        })?;
    file.write_all(bytes).map_err(|source| WikiError::Io {
        action: "write test media file",
        path: Some(file.path().to_path_buf()),
        source,
    })?;
    file.flush().map_err(|source| WikiError::Io {
        action: "flush test media file",
        path: Some(file.path().to_path_buf()),
        source,
    })?;
    Ok(file)
}

struct FakeTranscriptionClient;

impl TranscriptionClient for FakeTranscriptionClient {
    fn transcribe(
        &self,
        _request: &TranscriptionRequest<'_>,
    ) -> Result<TranscriptionOutput, WikiError> {
        Ok(transcript_output(
            "en",
            false,
            "transcribe",
            &[(
                1_000,
                2_000,
                "Audio-first transcript from extracted video audio.",
            )],
        ))
    }
}

struct FailingTranscriptionClient;

impl TranscriptionClient for FailingTranscriptionClient {
    fn transcribe(
        &self,
        _request: &TranscriptionRequest<'_>,
    ) -> Result<TranscriptionOutput, WikiError> {
        Err(WikiError::Config {
            detail: "stt provider failed".to_string(),
        })
    }
}

struct FakeVisionClient;

impl VisionClient for FakeVisionClient {
    fn extract(&self, request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
        Ok(VisionExtraction {
            description: format!(
                "frame {} has {} bytes",
                request.file_name,
                request.bytes.len()
            ),
            ocr_text: None,
            metadata: Vec::new(),
        })
    }
}

struct FailingVisionClient;

impl VisionClient for FailingVisionClient {
    fn extract(&self, _request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
        Err(WikiError::Config {
            detail: "vision provider failed".to_string(),
        })
    }
}

fn transcript_output(
    source_lang: &str,
    translated: bool,
    task: &str,
    segments: &[(u64, u64, &str)],
) -> TranscriptionOutput {
    TranscriptionOutput {
        segments: segments
            .iter()
            .map(|(start_ms, end_ms, text)| TranscriptSegment {
                start_ms: *start_ms,
                end_ms: *end_ms,
                text: (*text).to_string(),
            })
            .collect(),
        language: Some(if translated { "en" } else { source_lang }.to_string()),
        model: Some("fake-stt".to_string()),
        source_language: Some(source_lang.to_string()),
        task: Some(task.to_string()),
        target_language: translated.then(|| "en".to_string()),
        translated,
        translation_degraded: false,
        partial: false,
        completed_ranges: Vec::new(),
        missing_ranges: Vec::new(),
    }
}

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

#[cfg(feature = "ai")]
struct ScriptedTranscriptionClient {
    english: RefCell<Vec<Result<TranscriptionOutput, WikiError>>>,
    calls: RefCell<Vec<&'static str>>,
}

#[cfg(feature = "ai")]
impl TranscriptionClient for ScriptedTranscriptionClient {
    fn transcribe(
        &self,
        _request: &TranscriptionRequest<'_>,
    ) -> Result<TranscriptionOutput, WikiError> {
        Err(WikiError::Config {
            detail: "unexpected transcribe fallback".to_string(),
        })
    }

    fn translate_to_english(
        &self,
        _request: &TranscriptionRequest<'_>,
        _language_hint: Option<&str>,
    ) -> Result<TranscriptionOutput, WikiError> {
        self.calls.borrow_mut().push("translate_to_english");
        self.english.borrow_mut().remove(0)
    }
}

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
        calls: RefCell::new(Vec::new()),
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
            reason: "disabled".to_string(),
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
    assert!(document.body.contains("translated: true"));
    assert!(document.body.contains("transcription_partial: true"));
    assert!(
        document
            .body
            .contains("transcription_missing_ranges: 9000-19000")
    );
    assert!(document.body.contains("[00:00:00] hello"));
}

#[cfg(feature = "ai")]
struct ScriptedChunkTranscriptionClient {
    outputs: RefCell<Vec<Result<TranscriptionOutput, WikiError>>>,
}

#[cfg(feature = "ai")]
impl TranscriptionClient for ScriptedChunkTranscriptionClient {
    fn transcribe(
        &self,
        _request: &TranscriptionRequest<'_>,
    ) -> Result<TranscriptionOutput, WikiError> {
        self.outputs.borrow_mut().remove(0)
    }
}

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
        no_ffmpeg_doc.contains("media_degradation: audio:ffmpeg_unavailable")
            || no_ffmpeg_doc.contains("media_degradation: frames:ffmpeg_unavailable")
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
    assert!(frames_fail_doc.contains("media_degradation: frames:extraction_failed"));
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
            reason: "disabled".to_string(),
            fallback: "skip frames".to_string(),
        }),
        "vision-unavailable.mp4",
    )
    .expect("vision unavailable degrades");
    let vision_unavailable_doc = read_derived(temp.path(), &vision_unavailable);
    assert!(vision_unavailable_doc.contains("media_degradation: frames:vision_unavailable"));
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
            reason: "disabled".to_string(),
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
                reason: "disabled".to_string(),
                fallback: "skip frames".to_string(),
            }),
            "partial-chunk.mp4",
        )
        .expect("partial chunk aggregate degrades");
        let partial_doc = read_derived(temp.path(), &partial);
        assert!(partial_doc.contains("transcription_partial: true"));
        assert!(partial_doc.contains("transcription_missing_ranges: 9000-19000"));
        assert!(partial_doc.contains("[00:00:00] completed chunk"));
    }
}

#[test]
fn frame_vision_failure_drops_sampled_temp_frames_before_keep() {
    let frame = temp_file_with_bytes(".jpg", b"frame-zero").expect("frame temp");
    let frame_path = frame.path().to_path_buf();

    let err = describe_frame_images(
        "lecture.mp4",
        vec![(0, frame)],
        VisionEndpoint::Available(&FailingVisionClient),
    )
    .expect_err("vision failure should degrade caller");

    assert!(err.to_string().contains("vision provider failed"));
    assert!(!frame_path.exists(), "temp frame should be cleaned up");
}

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

fn ingest_with_media(
    vault_root: &Path,
    media: FakeVideoMediaExtractor,
    transcription_endpoint: TranscriptionEndpoint<'_>,
    vision_endpoint: VisionEndpoint<'_>,
    file_name: &str,
) -> Result<VideoIngestResult, WikiError> {
    let source_path = vault_root.join(format!("{file_name}.source"));
    std::fs::write(&source_path, b"video bytes").expect("write source video");
    let mut store = MemoryWikiStore::default();
    ingest_video_file_with_processing(
        vault_root,
        &mut store,
        ScopeIdentity::topic("field-work"),
        VideoFileSnapshot {
            location: format!("/tmp/{file_name}"),
            file_name: file_name.to_string(),
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            path: source_path,
            mime_type: Some("video/mp4".to_string()),
            duration_seconds: Some(20),
            frame_interval_seconds: Some(4),
            frame_samples: Vec::new(),
            frame_descriptions: Vec::new(),
            frame_image_paths: Vec::new(),
            transcript_segments: Vec::new(),
            transcription: None,
        },
        transcription_endpoint,
        vision_endpoint,
        &media,
    )
}

fn read_derived(vault_root: &Path, result: &VideoIngestResult) -> String {
    std::fs::read_to_string(vault_root.join(&result.derived_path)).expect("read derived video")
}

fn assert_asset_preserved(vault_root: &Path, result: &VideoIngestResult, expected: &[u8]) {
    assert_eq!(
        std::fs::read(vault_root.join(&result.asset_path)).expect("read video asset"),
        expected
    );
}

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
    assert!(raw.contains("video_duration_seconds: 8"));
    assert!(raw.contains("video_frame_interval_seconds: 4"));

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
