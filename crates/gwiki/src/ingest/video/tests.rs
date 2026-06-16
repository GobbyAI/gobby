#[cfg(feature = "ai")]
use std::cell::RefCell;
use std::io::Write;
#[cfg(feature = "ai")]
use std::rc::Rc;
use std::time::Duration;

use gobby_core::indexing::{content_hash, file_content_hash};

use super::*;
use crate::sources::{CompileStatus, IngestionMethod, SourceKind, SourceManifest};
use crate::store::{MemoryWikiStore, WikiDocumentKind};
use crate::transcribe::{
    TranscriptionClient, TranscriptionEndpoint, TranscriptionOutput, TranscriptionRequest,
};
use crate::vision::{VisionClient, VisionEndpoint, VisionExtraction, VisionRequest};

mod degradation;
mod frame_assets;
mod processing;
mod storage;
#[cfg(feature = "ai")]
mod translation;

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

#[cfg(feature = "ai")]
struct ScriptedTranscriptionClient {
    english: RefCell<Vec<Result<TranscriptionOutput, WikiError>>>,
    calls: Rc<RefCell<Vec<&'static str>>>,
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
