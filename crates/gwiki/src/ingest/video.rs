use std::path::{Path, PathBuf};
use std::time::Duration;

#[cfg(feature = "ai")]
use crate::ai::clients::ProductionVisionClient;
#[cfg(feature = "ai")]
use gobby_core::ai::effective_route;
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiCapability, AiRouting};
use tempfile::NamedTempFile;

use crate::ingest::audio::{production_transcription_endpoint, transcribe_for_markdown};
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    write_asset, write_asset_from_path, write_raw_markdown,
};
use crate::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest, SourceRecord,
};
use crate::store::WikiIndexStore;
use crate::transcribe::{
    TranscriptSegment, TranscriptionDegradation, TranscriptionEndpoint, TranscriptionMarkdownInput,
    TranscriptionOutput, TranscriptionRequest,
};
use crate::video::{
    AlignedVideoSegment, FrameSamplingPlan, VideoFrameDescription, VideoFrameSample,
    VideoMarkdownRequest, VideoMarkdownResult, VideoMediaDegradation, VideoMediaMetadata,
    write_video_derived_markdown,
};
use crate::vision::{VisionDegradation, VisionEndpoint, VisionRequest};
use crate::{ScopeIdentity, WikiError};

pub const DEFAULT_FRAME_INTERVAL_SECONDS: u32 = 5;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub mime_type: Option<String>,
    pub duration_seconds: Option<u32>,
    pub frame_interval_seconds: Option<u32>,
    pub frame_samples: Vec<VideoFrameSample>,
    pub frame_image_paths: Vec<PathBuf>,
    pub frame_descriptions: Vec<VideoFrameDescription>,
    pub transcript_segments: Vec<TranscriptSegment>,
    pub transcription: Option<TranscriptionOutput>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoFileSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub path: PathBuf,
    pub mime_type: Option<String>,
    pub duration_seconds: Option<u32>,
    pub frame_interval_seconds: Option<u32>,
    pub frame_samples: Vec<VideoFrameSample>,
    pub frame_image_paths: Vec<PathBuf>,
    pub frame_descriptions: Vec<VideoFrameDescription>,
    pub transcript_segments: Vec<TranscriptSegment>,
    pub transcription: Option<TranscriptionOutput>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoIngestResult {
    pub record: SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub derived_path: PathBuf,
    pub frame_samples: Vec<VideoFrameSample>,
    pub aligned_segments: Vec<AlignedVideoSegment>,
}

pub fn ingest_video(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoSnapshot,
) -> Result<VideoIngestResult, WikiError> {
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let metadata = VideoSnapshotRef::from_snapshot(&snapshot);
    ingest_video_with_asset(
        vault_root,
        store,
        scope,
        metadata,
        content_hash,
        VideoDegradationContext::default(),
        |record| write_asset(vault_root, record, &snapshot.file_name, &snapshot.bytes),
    )
}

pub fn ingest_video_file(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoFileSnapshot,
) -> Result<VideoIngestResult, WikiError> {
    ingest_video_file_with_degradations(vault_root, store, scope, snapshot, &[], None, false)
}

fn ingest_video_file_with_degradations(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoFileSnapshot,
    media_degradations: &[VideoMediaDegradation],
    transcription_degradation: Option<&TranscriptionDegradation>,
    suppress_frame_sampling: bool,
) -> Result<VideoIngestResult, WikiError> {
    let content_hash =
        gobby_core::indexing::file_content_hash(&snapshot.path).map_err(|error| WikiError::Io {
            action: "hash video source",
            path: Some(snapshot.path.clone()),
            source: error,
        })?;
    let metadata = VideoSnapshotRef::from_file_snapshot(&snapshot);
    ingest_video_with_asset(
        vault_root,
        store,
        scope,
        metadata,
        content_hash,
        VideoDegradationContext {
            media: media_degradations,
            transcription: transcription_degradation,
            suppress_frame_sampling,
        },
        |record| {
            write_asset_from_path(
                vault_root,
                record,
                &snapshot.file_name,
                &snapshot.path,
                &record.content_hash,
            )
        },
    )
}

pub fn ingest_video_file_with_production_processing(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    ai_context: &AiContext,
    mut snapshot: VideoFileSnapshot,
    translate: bool,
) -> Result<VideoIngestResult, WikiError> {
    if snapshot.duration_seconds.is_none() {
        snapshot.duration_seconds = crate::media::probe_duration(&snapshot.path);
    }
    let media = ProductionVideoMediaExtractor;
    let transcription_endpoint = production_transcription_endpoint(ai_context, translate);

    #[cfg(feature = "ai")]
    {
        let route = effective_route(ai_context, AiCapability::VisionExtract);
        if matches!(route, AiRouting::Daemon | AiRouting::Direct) {
            let vision_client = ProductionVisionClient::new(ai_context.clone());
            return ingest_video_file_with_processing(
                vault_root,
                store,
                scope,
                snapshot,
                transcription_endpoint,
                VisionEndpoint::Available(&vision_client),
                &media,
            );
        }
        ingest_video_file_with_processing(
            vault_root,
            store,
            scope,
            snapshot,
            transcription_endpoint,
            VisionEndpoint::Unavailable(vision_degradation(route)),
            &media,
        )
    }

    #[cfg(not(feature = "ai"))]
    {
        ingest_video_file_with_processing(
            vault_root,
            store,
            scope,
            snapshot,
            transcription_endpoint,
            VisionEndpoint::Unavailable(vision_degradation(
                ai_context.binding(AiCapability::VisionExtract).routing,
            )),
            &media,
        )
    }
}

trait VideoMediaExtractor {
    fn extract_audio(&self, video: &Path) -> Result<NamedTempFile, WikiError>;

    fn sample_frame_images(
        &self,
        video: &Path,
        interval: Duration,
    ) -> Result<Vec<(u64, NamedTempFile)>, WikiError>;
}

struct ProductionVideoMediaExtractor;

impl VideoMediaExtractor for ProductionVideoMediaExtractor {
    fn extract_audio(&self, video: &Path) -> Result<NamedTempFile, WikiError> {
        crate::media::extract_audio_file(video)
    }

    fn sample_frame_images(
        &self,
        video: &Path,
        interval: Duration,
    ) -> Result<Vec<(u64, NamedTempFile)>, WikiError> {
        crate::media::sample_frame_images(video, interval)
    }
}

fn ingest_video_file_with_processing(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    mut snapshot: VideoFileSnapshot,
    transcription_endpoint: TranscriptionEndpoint<'_>,
    vision_endpoint: VisionEndpoint<'_>,
    media: &dyn VideoMediaExtractor,
) -> Result<VideoIngestResult, WikiError> {
    let frame_interval_seconds = snapshot
        .frame_interval_seconds
        .unwrap_or(DEFAULT_FRAME_INTERVAL_SECONDS);
    let mut media_degradations = Vec::new();
    let mut transcription_degradation = None;
    let mut suppress_frame_sampling = false;

    if !matches!(
        &transcription_endpoint,
        TranscriptionEndpoint::Unavailable(_)
    ) {
        match media.extract_audio(&snapshot.path) {
            Ok(audio) => match std::fs::read(audio.path()) {
                Ok(audio_bytes) => {
                    let request = TranscriptionRequest {
                        file_name: &snapshot.file_name,
                        mime_type: Some("audio/wav"),
                        asset_path: audio.path(),
                        bytes: &audio_bytes,
                    };
                    match transcribe_for_markdown(&request, transcription_endpoint) {
                        TranscriptionMarkdownInput::Transcribed(output) => {
                            snapshot.transcript_segments = output.segments.clone();
                            snapshot.transcription = Some(output);
                        }
                        TranscriptionMarkdownInput::Degraded(degradation) => {
                            transcription_degradation = Some(degradation);
                        }
                    }
                }
                Err(source) => media_degradations.push(VideoMediaDegradation {
                    kind: "audio".to_string(),
                    reason: "read_failed".to_string(),
                    message: WikiError::Io {
                        action: "read extracted video audio",
                        path: Some(audio.path().to_path_buf()),
                        source,
                    }
                    .to_string(),
                }),
            },
            Err(error) => media_degradations.push(video_media_degradation(
                "audio",
                "extraction_failed",
                error,
            )),
        }
    }

    if frame_interval_seconds != 0 && matches!(&vision_endpoint, VisionEndpoint::Available(_)) {
        match media.sample_frame_images(
            &snapshot.path,
            Duration::from_secs(u64::from(frame_interval_seconds)),
        ) {
            Ok(frames) => match describe_frame_images(&snapshot.file_name, frames, vision_endpoint)
            {
                Ok(described_frames) => {
                    snapshot.frame_samples = described_frames.samples;
                    snapshot.frame_image_paths = described_frames.paths;
                    snapshot.frame_descriptions = described_frames.descriptions;
                }
                Err(error) => {
                    suppress_frame_sampling = true;
                    media_degradations.push(video_media_degradation(
                        "frames",
                        "vision_failed",
                        error,
                    ));
                }
            },
            Err(error) => {
                suppress_frame_sampling = true;
                media_degradations.push(video_media_degradation(
                    "frames",
                    "extraction_failed",
                    error,
                ));
            }
        }
    }

    ingest_video_file_with_degradations(
        vault_root,
        store,
        scope,
        snapshot,
        &media_degradations,
        transcription_degradation.as_ref(),
        suppress_frame_sampling,
    )
}

fn video_media_degradation(
    kind: impl Into<String>,
    fallback_reason: &str,
    error: WikiError,
) -> VideoMediaDegradation {
    let message = error.to_string();
    let reason = if message.contains("ffmpeg") {
        "ffmpeg_unavailable"
    } else {
        fallback_reason
    };
    VideoMediaDegradation {
        kind: kind.into(),
        reason: reason.to_string(),
        message,
    }
}

struct DescribedFrameImages {
    samples: Vec<VideoFrameSample>,
    paths: Vec<PathBuf>,
    descriptions: Vec<VideoFrameDescription>,
}

fn describe_frame_images(
    video_file_name: &str,
    frames: Vec<(u64, NamedTempFile)>,
    endpoint: VisionEndpoint<'_>,
) -> Result<DescribedFrameImages, WikiError> {
    let mut samples = Vec::new();
    let mut paths = Vec::new();
    let mut descriptions = Vec::new();
    let client = match endpoint {
        VisionEndpoint::Available(client) => Some(client),
        VisionEndpoint::Unavailable(_) => None,
    };

    for (index, (timestamp_ms, frame)) in frames.into_iter().enumerate() {
        let timestamp_seconds = (timestamp_ms / 1_000).min(u64::from(u32::MAX)) as u32;
        let timestamp = format_timestamp(timestamp_seconds);
        let path = frame
            .into_temp_path()
            .keep()
            .map_err(|error| WikiError::Io {
                action: "persist sampled video frame",
                path: Some(error.path.to_path_buf()),
                source: error.error,
            })?;
        let source_reference = path_to_string(&path);
        samples.push(VideoFrameSample {
            timestamp_seconds,
            timestamp: timestamp.clone(),
            source_asset: path.clone(),
            source_reference: source_reference.clone(),
        });
        paths.push(path.clone());

        if let Some(client) = client {
            let bytes = std::fs::read(&path).map_err(|source| WikiError::Io {
                action: "read sampled video frame",
                path: Some(path.clone()),
                source,
            })?;
            let file_name = format!("{video_file_name}.frame-{index:04}.jpg");
            let extraction = client.extract(&VisionRequest {
                file_name: &file_name,
                mime_type: Some("image/jpeg"),
                asset_path: &path,
                bytes: &bytes,
                width: None,
                height: None,
            })?;
            descriptions.push(VideoFrameDescription {
                timestamp,
                source_reference,
                description: extraction.description,
            });
        }
    }

    Ok(DescribedFrameImages {
        samples,
        paths,
        descriptions,
    })
}

fn vision_degradation(routing: AiRouting) -> VisionDegradation {
    let reason = match routing {
        AiRouting::Off => "disabled",
        AiRouting::Auto | AiRouting::Daemon | AiRouting::Direct => "missing_endpoint",
    };
    VisionDegradation {
        reason: reason.to_string(),
        fallback: "Keep raw video assets and skip frame vision.".to_string(),
    }
}

fn ingest_video_with_asset(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoSnapshotRef<'_>,
    content_hash: String,
    degradations: VideoDegradationContext<'_>,
    write_asset_fn: impl FnOnce(&SourceRecord) -> Result<PathBuf, WikiError>,
) -> Result<VideoIngestResult, WikiError> {
    let title = markdown_title(snapshot.file_name);
    let draft = SourceDraft {
        location: snapshot.location.to_string(),
        kind: SourceKind::Video,
        fetched_at: snapshot.fetched_at.to_string(),
        content: Vec::new(),
        title: Some(title),
        citation: Some(snapshot.location.to_string()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register_with_content_hash(vault_root, draft, content_hash)?;
    let asset_path = write_asset_fn(&record)?;
    let media_metadata = video_media_metadata(vault_root, &asset_path, snapshot.duration_seconds)?;
    let frame_interval_seconds = snapshot
        .frame_interval_seconds
        .unwrap_or(DEFAULT_FRAME_INTERVAL_SECONDS);
    let raw_markdown = render_raw_video_markdown(
        &snapshot,
        &record.content_hash,
        &asset_path,
        frame_interval_seconds,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let frame_samples = if frame_interval_seconds == 0 || degradations.suppress_frame_sampling {
        Vec::new()
    } else if !snapshot.frame_samples.is_empty() {
        snapshot.frame_samples.to_vec()
    } else {
        crate::video::sample_frames(
            &asset_path,
            FrameSamplingPlan {
                duration_seconds: snapshot.duration_seconds,
                interval_seconds: frame_interval_seconds,
            },
        )
    };
    let VideoMarkdownResult {
        path: derived_path,
        aligned_segments,
    } = write_video_derived_markdown(
        vault_root,
        &scope,
        &record,
        VideoMarkdownRequest {
            file_name: snapshot.file_name,
            mime_type: snapshot.mime_type,
            asset_path: &asset_path,
            raw_path: &raw_path,
            duration_seconds: snapshot.duration_seconds,
            media_metadata: Some(media_metadata),
            media_degradations: degradations.media,
            transcription_degradation: degradations.transcription,
            frame_interval_seconds,
            frame_samples: &frame_samples,
            frame_image_paths: snapshot.frame_image_paths,
            frame_descriptions: snapshot.frame_descriptions,
            transcript_segments: snapshot.transcript_segments,
            transcription: snapshot.transcription,
        },
    )?;
    index_after_ingest(vault_root, store)?;

    Ok(VideoIngestResult {
        record,
        raw_path,
        asset_path,
        derived_path,
        frame_samples,
        aligned_segments,
    })
}

#[derive(Clone, Copy, Default)]
struct VideoDegradationContext<'a> {
    media: &'a [VideoMediaDegradation],
    transcription: Option<&'a TranscriptionDegradation>,
    suppress_frame_sampling: bool,
}

fn video_media_metadata(
    vault_root: &Path,
    asset_path: &Path,
    duration_seconds: Option<u32>,
) -> Result<VideoMediaMetadata, WikiError> {
    let absolute_asset_path = vault_root.join(asset_path);
    let metadata = std::fs::metadata(&absolute_asset_path).map_err(|source| WikiError::Io {
        action: "stat video asset",
        path: Some(absolute_asset_path),
        source,
    })?;
    Ok(VideoMediaMetadata {
        file_size_bytes: metadata.len(),
        duration_seconds,
    })
}

struct VideoSnapshotRef<'a> {
    location: &'a str,
    file_name: &'a str,
    fetched_at: &'a str,
    mime_type: Option<&'a str>,
    duration_seconds: Option<u32>,
    frame_interval_seconds: Option<u32>,
    frame_samples: &'a [VideoFrameSample],
    frame_image_paths: &'a [PathBuf],
    frame_descriptions: &'a [VideoFrameDescription],
    transcript_segments: &'a [TranscriptSegment],
    transcription: Option<&'a TranscriptionOutput>,
}

impl<'a> VideoSnapshotRef<'a> {
    fn from_snapshot(snapshot: &'a VideoSnapshot) -> Self {
        Self {
            location: &snapshot.location,
            file_name: &snapshot.file_name,
            fetched_at: &snapshot.fetched_at,
            mime_type: snapshot.mime_type.as_deref(),
            duration_seconds: snapshot.duration_seconds,
            frame_interval_seconds: snapshot.frame_interval_seconds,
            frame_samples: &snapshot.frame_samples,
            frame_image_paths: &snapshot.frame_image_paths,
            frame_descriptions: &snapshot.frame_descriptions,
            transcript_segments: &snapshot.transcript_segments,
            transcription: snapshot.transcription.as_ref(),
        }
    }

    fn from_file_snapshot(snapshot: &'a VideoFileSnapshot) -> Self {
        Self {
            location: &snapshot.location,
            file_name: &snapshot.file_name,
            fetched_at: &snapshot.fetched_at,
            mime_type: snapshot.mime_type.as_deref(),
            duration_seconds: snapshot.duration_seconds,
            frame_interval_seconds: snapshot.frame_interval_seconds,
            frame_samples: &snapshot.frame_samples,
            frame_image_paths: &snapshot.frame_image_paths,
            frame_descriptions: &snapshot.frame_descriptions,
            transcript_segments: &snapshot.transcript_segments,
            transcription: snapshot.transcription.as_ref(),
        }
    }
}

impl From<VideoIngestResult> for IngestResult {
    fn from(result: VideoIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

fn render_raw_video_markdown(
    snapshot: &VideoSnapshotRef<'_>,
    source_hash: &str,
    asset_path: &Path,
    frame_interval_seconds: u32,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "video".to_string()),
        ("source_location", snapshot.location.to_string()),
        ("fetched_at", snapshot.fetched_at.to_string()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];
    if let Some(mime_type) = snapshot.mime_type {
        fields.push(("video_mime_type", mime_type.to_string()));
    }
    if let Some(duration_seconds) = snapshot.duration_seconds {
        fields.push(("video_duration_seconds", duration_seconds.to_string()));
    }
    fields.push((
        "video_frame_interval_seconds",
        frame_interval_seconds.to_string(),
    ));
    fields.push((
        "video_frame_description_count",
        snapshot.frame_descriptions.len().to_string(),
    ));
    fields.push((
        "video_transcript_segment_count",
        snapshot.transcript_segments.len().to_string(),
    ));

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original video stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

fn format_timestamp(seconds: u32) -> String {
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let seconds = seconds % 60;
    format!("{hours:02}:{minutes:02}:{seconds:02}")
}

#[cfg(test)]
mod tests {
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

    fn temp_file_with_bytes(
        suffix: &str,
        bytes: &[u8],
    ) -> Result<tempfile::NamedTempFile, WikiError> {
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
        let raw =
            std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown");
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
}
