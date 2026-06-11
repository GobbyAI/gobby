use super::*;

#[derive(Clone, Copy, Default)]
pub(crate) struct VideoDegradationContext<'a> {
    pub(crate) media: &'a [VideoMediaDegradation],
    pub(crate) transcription: Option<&'a TranscriptionDegradation>,
    pub(crate) suppress_frame_sampling: bool,
}

pub(crate) fn video_media_metadata(
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

pub(crate) struct VideoSnapshotRef<'a> {
    pub(crate) location: &'a str,
    pub(crate) file_name: &'a str,
    pub(crate) fetched_at: &'a str,
    pub(crate) mime_type: Option<&'a str>,
    pub(crate) duration_seconds: Option<u32>,
    pub(crate) frame_interval_seconds: Option<u32>,
    pub(crate) frame_samples: &'a [VideoFrameSample],
    pub(crate) frame_image_paths: &'a [PathBuf],
    pub(crate) frame_descriptions: &'a [VideoFrameDescription],
    pub(crate) transcript_segments: &'a [TranscriptSegment],
    pub(crate) transcription: Option<&'a TranscriptionOutput>,
}

impl<'a> VideoSnapshotRef<'a> {
    #[allow(dead_code, reason = "reserved gwiki CLI/API split")]
    pub(crate) fn from_snapshot(snapshot: &'a VideoSnapshot) -> Self {
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

    pub(crate) fn from_file_snapshot(snapshot: &'a VideoFileSnapshot) -> Self {
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

pub(crate) fn render_raw_video_markdown(
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

pub(crate) fn format_timestamp(seconds: u32) -> String {
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let seconds = seconds % 60;
    format!("{hours:02}:{minutes:02}:{seconds:02}")
}
