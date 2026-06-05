use std::path::{Path, PathBuf};
use std::time::Duration;

use gobby_core::config::AiRouting;
use tempfile::NamedTempFile;

use crate::ingest::{index_after_ingest, path_to_string};
use crate::store::WikiIndexStore;
use crate::transcribe::{TranscriptionEndpoint, TranscriptionMarkdownInput, TranscriptionRequest};
use crate::vision::{VisionDegradation, VisionEndpoint, VisionRequest};
use crate::{ScopeIdentity, WikiError};

use super::{
    DEFAULT_FRAME_INTERVAL_SECONDS, VideoFileSnapshot, VideoIngestResult, format_timestamp,
    ingest_video_file_with_degradations_without_index, transcribe_for_markdown,
};
use crate::video::{VideoFrameDescription, VideoFrameSample, VideoMediaDegradation};

pub(crate) trait VideoMediaExtractor {
    fn extract_audio(&self, video: &Path) -> Result<NamedTempFile, WikiError>;

    fn sample_frame_images(
        &self,
        video: &Path,
        interval: Duration,
    ) -> Result<Vec<(u64, NamedTempFile)>, WikiError>;
}

pub(crate) struct ProductionVideoMediaExtractor;

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

pub(crate) fn ingest_video_file_with_processing(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoFileSnapshot,
    transcription_endpoint: TranscriptionEndpoint<'_>,
    vision_endpoint: VisionEndpoint<'_>,
    media: &dyn VideoMediaExtractor,
) -> Result<VideoIngestResult, WikiError> {
    let result = ingest_video_file_with_processing_without_index(
        vault_root,
        scope,
        snapshot,
        transcription_endpoint,
        vision_endpoint,
        media,
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_video_file_with_processing_without_index(
    vault_root: &Path,
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

    match transcription_endpoint {
        TranscriptionEndpoint::Unavailable(reason) => {
            transcription_degradation = Some(crate::transcribe::TranscriptionDegradation {
                reason: "unavailable".to_string(),
                fallback: format!("{}: {}", reason.reason, reason.fallback),
            });
        }
        endpoint => match media.extract_audio(&snapshot.path) {
            Ok(audio) => match std::fs::read(audio.path()) {
                Ok(audio_bytes) => {
                    let request = TranscriptionRequest {
                        file_name: &snapshot.file_name,
                        mime_type: Some("audio/wav"),
                        asset_path: audio.path(),
                        bytes: &audio_bytes,
                    };
                    match transcribe_for_markdown(&request, endpoint) {
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
        },
    }

    if frame_interval_seconds != 0 {
        match &vision_endpoint {
            VisionEndpoint::Available(_) => {
                match media.sample_frame_images(
                    &snapshot.path,
                    Duration::from_secs(u64::from(frame_interval_seconds)),
                ) {
                    Ok(frames) => {
                        match describe_frame_images(&snapshot.file_name, frames, vision_endpoint) {
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
                        }
                    }
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
            VisionEndpoint::Unavailable(degradation) => {
                suppress_frame_sampling = true;
                media_degradations.push(VideoMediaDegradation {
                    kind: "frames".to_string(),
                    reason: "vision_unavailable".to_string(),
                    message: format!("{}: {}", degradation.reason, degradation.fallback),
                });
            }
        }
    }

    ingest_video_file_with_degradations_without_index(
        vault_root,
        scope,
        snapshot,
        &media_degradations,
        transcription_degradation.as_ref(),
        suppress_frame_sampling,
    )
}

pub(crate) fn video_media_degradation(
    kind: impl Into<String>,
    fallback_reason: &str,
    error: WikiError,
) -> VideoMediaDegradation {
    let message = error.to_string();
    let reason = if error.is_ffmpeg_unavailable() || message_is_ffmpeg_unavailable(&message) {
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

fn message_is_ffmpeg_unavailable(message: &str) -> bool {
    let message = message.to_ascii_lowercase();
    [
        "ffmpeg is unavailable",
        "ffmpeg unavailable",
        "ffmpeg executable not found",
        "ffmpeg was not found",
    ]
    .iter()
    .any(|needle| message.contains(needle))
}

#[derive(Debug)]
pub(crate) struct DescribedFrameImages {
    pub(crate) samples: Vec<VideoFrameSample>,
    pub(crate) paths: Vec<PathBuf>,
    pub(crate) descriptions: Vec<VideoFrameDescription>,
}

pub(crate) struct PendingFrameImage {
    timestamp_seconds: u32,
    timestamp: String,
    frame: NamedTempFile,
    description: Option<String>,
}

pub(crate) fn describe_frame_images(
    video_file_name: &str,
    frames: Vec<(u64, NamedTempFile)>,
    endpoint: VisionEndpoint<'_>,
) -> Result<DescribedFrameImages, WikiError> {
    let mut pending = Vec::with_capacity(frames.len());
    let client = match endpoint {
        VisionEndpoint::Available(client) => Some(client),
        VisionEndpoint::Unavailable(_) => None,
    };

    for (index, (timestamp_ms, frame)) in frames.into_iter().enumerate() {
        let timestamp_seconds = (timestamp_ms / 1_000).min(u64::from(u32::MAX)) as u32;
        let timestamp = format_timestamp(timestamp_seconds);
        let path = frame.path().to_path_buf();
        let description = if let Some(client) = client {
            match std::fs::read(&path) {
                Ok(bytes) => {
                    let file_name = format!("{video_file_name}.frame-{index:04}.jpg");
                    match client.extract(&VisionRequest {
                        file_name: &file_name,
                        mime_type: Some("image/jpeg"),
                        asset_path: &path,
                        bytes: &bytes,
                        width: None,
                        height: None,
                    }) {
                        Ok(extraction) => Some(extraction.description),
                        Err(error) => {
                            log::warn!(
                                "video frame vision failed for {} at {}: {error}",
                                video_file_name,
                                timestamp
                            );
                            None
                        }
                    }
                }
                Err(source) => {
                    log::warn!(
                        "failed to read sampled video frame {} for {} at {}: {source}",
                        path.display(),
                        video_file_name,
                        timestamp
                    );
                    None
                }
            }
        } else {
            None
        };

        pending.push(PendingFrameImage {
            timestamp_seconds,
            timestamp,
            frame,
            description,
        });
    }

    let mut samples = Vec::new();
    let mut paths = Vec::new();
    let mut descriptions = Vec::new();
    let mut kept_paths = Vec::new();

    for pending_frame in pending {
        let PendingFrameImage {
            timestamp_seconds,
            timestamp,
            frame,
            description,
        } = pending_frame;
        let kept_path = frame.into_temp_path().keep().map_err(|error| {
            cleanup_kept_temp_frames(&kept_paths);
            WikiError::Io {
                action: "persist sampled video frame",
                path: Some(error.path.to_path_buf()),
                source: error.error,
            }
        })?;
        let source_reference = path_to_string(&kept_path);
        samples.push(VideoFrameSample {
            timestamp_seconds,
            timestamp: timestamp.clone(),
            source_asset: kept_path.clone(),
            source_reference: source_reference.clone(),
        });
        paths.push(kept_path.clone());
        kept_paths.push(kept_path);

        if let Some(description) = description {
            descriptions.push(VideoFrameDescription {
                timestamp,
                source_reference,
                description,
            });
        }
    }

    Ok(DescribedFrameImages {
        samples,
        paths,
        descriptions,
    })
}

pub(crate) fn cleanup_kept_temp_frames(paths: &[PathBuf]) {
    for path in paths {
        let _ = std::fs::remove_file(path);
    }
}

pub(crate) fn vision_degradation(routing: AiRouting) -> VisionDegradation {
    let reason = match routing {
        AiRouting::Off => "disabled",
        AiRouting::Auto | AiRouting::Daemon | AiRouting::Direct => "missing_endpoint",
    };
    VisionDegradation {
        reason: reason.to_string(),
        fallback: "Keep raw video assets and skip frame vision.".to_string(),
    }
}
