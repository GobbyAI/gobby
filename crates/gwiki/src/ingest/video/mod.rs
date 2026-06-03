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
    write_asset, write_asset_from_path, write_asset_with_suffix, write_raw_markdown,
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
    pub media_degradations: Vec<VideoMediaDegradation>,
    pub transcription_degradation: Option<TranscriptionDegradation>,
}

pub fn ingest_video(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoSnapshot,
) -> Result<VideoIngestResult, WikiError> {
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let metadata = VideoSnapshotRef::from_snapshot(&snapshot);
    let result = ingest_video_with_asset_without_index(
        vault_root,
        scope,
        metadata,
        content_hash,
        VideoDegradationContext::default(),
        |record| write_asset(vault_root, record, &snapshot.file_name, &snapshot.bytes),
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
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
    let result = ingest_video_file_with_degradations_without_index(
        vault_root,
        scope,
        snapshot,
        media_degradations,
        transcription_degradation,
        suppress_frame_sampling,
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

fn ingest_video_file_with_degradations_without_index(
    vault_root: &Path,
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
    ingest_video_with_asset_without_index(
        vault_root,
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
    snapshot: VideoFileSnapshot,
    translate: bool,
) -> Result<VideoIngestResult, WikiError> {
    let result = ingest_video_file_with_production_processing_without_index(
        vault_root, scope, ai_context, snapshot, translate,
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_video_file_with_production_processing_without_index(
    vault_root: &Path,
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
            return ingest_video_file_with_processing_without_index(
                vault_root,
                scope,
                snapshot,
                transcription_endpoint,
                VisionEndpoint::Available(&vision_client),
                &media,
            );
        }
        ingest_video_file_with_processing_without_index(
            vault_root,
            scope,
            snapshot,
            transcription_endpoint,
            VisionEndpoint::Unavailable(vision_degradation(route)),
            &media,
        )
    }

    #[cfg(not(feature = "ai"))]
    {
        ingest_video_file_with_processing_without_index(
            vault_root,
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

mod assets;
mod metadata;
mod processing;

pub(crate) use assets::*;
pub(crate) use metadata::*;
pub(crate) use processing::*;

#[cfg(test)]
mod tests;
