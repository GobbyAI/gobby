use std::path::{Path, PathBuf};

use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    write_asset, write_raw_markdown,
};
use crate::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest, SourceRecord,
};
use crate::store::WikiIndexStore;
use crate::transcribe::TranscriptSegment;
use crate::video::{
    AlignedVideoSegment, FrameSamplingPlan, VideoFrameDescription, VideoFrameSample,
    VideoMarkdownRequest, VideoMarkdownResult, write_video_derived_markdown,
};
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
    pub frame_descriptions: Vec<VideoFrameDescription>,
    pub transcript_segments: Vec<TranscriptSegment>,
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
    let title = markdown_title(&snapshot.file_name);
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let draft = SourceDraft {
        location: snapshot.location.clone(),
        kind: SourceKind::Video,
        fetched_at: snapshot.fetched_at.clone(),
        content: Vec::new(),
        title: Some(title),
        citation: Some(snapshot.location.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register_with_content_hash(vault_root, draft, content_hash)?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    let raw_markdown = render_raw_video_markdown(&snapshot, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let frame_samples = crate::video::sample_frames(
        &asset_path,
        FrameSamplingPlan {
            duration_seconds: snapshot.duration_seconds,
            interval_seconds: snapshot
                .frame_interval_seconds
                .unwrap_or(DEFAULT_FRAME_INTERVAL_SECONDS),
        },
    );
    let VideoMarkdownResult {
        path: derived_path,
        aligned_segments,
    } = write_video_derived_markdown(
        vault_root,
        &scope,
        &record,
        VideoMarkdownRequest {
            file_name: &snapshot.file_name,
            mime_type: snapshot.mime_type.as_deref(),
            asset_path: &asset_path,
            raw_path: &raw_path,
            duration_seconds: snapshot.duration_seconds,
            frame_samples: &frame_samples,
            frame_descriptions: &snapshot.frame_descriptions,
            transcript_segments: &snapshot.transcript_segments,
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
    snapshot: &VideoSnapshot,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "video".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];
    if let Some(mime_type) = &snapshot.mime_type {
        fields.push(("video_mime_type", mime_type.clone()));
    }
    if let Some(duration_seconds) = snapshot.duration_seconds {
        fields.push(("video_duration_seconds", duration_seconds.to_string()));
    }
    if let Some(frame_interval_seconds) = snapshot.frame_interval_seconds {
        fields.push((
            "video_frame_interval_seconds",
            frame_interval_seconds.to_string(),
        ));
    }
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
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original video stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[cfg(test)]
mod tests {
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{MemoryWikiStore, WikiDocumentKind};

    fn sample_snapshot() -> VideoSnapshot {
        VideoSnapshot {
            location: "/tmp/lecture.mp4".to_string(),
            file_name: "lecture.mp4".to_string(),
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            bytes: b"\0\0\0\x18ftypmp42video-bytes".to_vec(),
            mime_type: Some("video/mp4".to_string()),
            duration_seconds: Some(8),
            frame_interval_seconds: Some(4),
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
                    timestamp: "00:00:01".to_string(),
                    text: "We start by recording the scene.".to_string(),
                },
                TranscriptSegment {
                    timestamp: "00:00:05".to_string(),
                    text: "Each transcript segment lines up with sampled frames.".to_string(),
                },
            ],
        }
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

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Video);
        assert_eq!(manifest.entries[0].content_hash, expected_hash);
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
