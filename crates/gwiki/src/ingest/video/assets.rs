use super::*;

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) fn ingest_video_with_asset(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: VideoSnapshotRef<'_>,
    content_hash: String,
    degradations: VideoDegradationContext<'_>,
    write_asset_fn: impl FnOnce(&SourceRecord) -> Result<PathBuf, WikiError>,
) -> Result<VideoIngestResult, WikiError> {
    let result = ingest_video_with_asset_without_index(
        vault_root,
        scope,
        snapshot,
        content_hash,
        degradations,
        write_asset_fn,
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_video_with_asset_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    snapshot: VideoSnapshotRef<'_>,
    content_hash: String,
    degradations: VideoDegradationContext<'_>,
    write_asset_fn: impl FnOnce(&SourceRecord) -> Result<PathBuf, WikiError>,
) -> Result<VideoIngestResult, WikiError> {
    let title = markdown_title(snapshot.file_name);
    let draft = SourceDraft::new(
        snapshot.location.to_string(),
        SourceKind::Video,
        snapshot.fetched_at.to_string(),
        Vec::new(),
    )
    .with_title(title)
    .with_citation(snapshot.location.to_string());
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
    let PersistedVideoFrameAssets {
        samples: frame_samples,
        image_paths: frame_image_paths,
        descriptions: frame_descriptions,
    } = persist_video_frame_assets(
        vault_root,
        &record,
        snapshot.file_name,
        frame_samples,
        snapshot.frame_image_paths,
        snapshot.frame_descriptions,
    )?;
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
            frame_image_paths: &frame_image_paths,
            frame_descriptions: &frame_descriptions,
            transcript_segments: snapshot.transcript_segments,
            transcription: snapshot.transcription,
        },
    )?;

    Ok(VideoIngestResult {
        record,
        raw_path,
        asset_path,
        derived_path,
        frame_samples,
        aligned_segments,
        media_degradations: degradations.media.to_vec(),
        transcription_degradation: degradations.transcription.cloned(),
    })
}

#[derive(Debug)]
pub(crate) struct PersistedVideoFrameAssets {
    samples: Vec<VideoFrameSample>,
    image_paths: Vec<PathBuf>,
    descriptions: Vec<VideoFrameDescription>,
}

/// Persists frame assets by index: `samples[index]`, `frame_image_paths[index]`,
/// and matching descriptions are expected to refer to the same sampled frame.
pub(crate) fn persist_video_frame_assets(
    vault_root: &Path,
    record: &SourceRecord,
    video_file_name: &str,
    mut samples: Vec<VideoFrameSample>,
    frame_image_paths: &[PathBuf],
    frame_descriptions: &[VideoFrameDescription],
) -> Result<PersistedVideoFrameAssets, WikiError> {
    if frame_image_paths.is_empty() {
        return Ok(PersistedVideoFrameAssets {
            samples,
            image_paths: Vec::new(),
            descriptions: frame_descriptions.to_vec(),
        });
    }

    let mut persisted_paths = Vec::with_capacity(frame_image_paths.len());
    let mut descriptions = frame_descriptions.to_vec();
    let mut source_temp_paths = Vec::new();
    let temp_dir = std::env::temp_dir();
    for (index, path) in frame_image_paths.iter().enumerate() {
        let cleanup_source_temp = samples
            .get(index)
            .is_some_and(|sample| sample.source_asset.as_path() == path.as_path())
            && path.starts_with(&temp_dir);
        let bytes = match std::fs::read(path) {
            Ok(bytes) => bytes,
            Err(source) => {
                cleanup_deferred_temp_frame_sources(&source_temp_paths);
                cleanup_sampled_temp_frame_sources(&samples, frame_image_paths);
                return Err(WikiError::Io {
                    action: "read sampled video frame asset",
                    path: Some(path.clone()),
                    source,
                });
            }
        };
        let file_name = format!("{video_file_name}.frame-{index:04}.jpg");
        let persisted_path = match write_asset_with_suffix(
            vault_root,
            record,
            &format!("frame-{index:04}"),
            &file_name,
            &bytes,
        ) {
            Ok(path) => path,
            Err(error) => {
                cleanup_deferred_temp_frame_sources(&source_temp_paths);
                cleanup_sampled_temp_frame_sources(&samples, frame_image_paths);
                return Err(error);
            }
        };
        if cleanup_source_temp && !source_temp_paths.iter().any(|existing| existing == path) {
            source_temp_paths.push(path.clone());
        }
        let reference = path_to_string(&persisted_path);
        if let Some(sample) = samples.get_mut(index) {
            sample.source_asset = persisted_path.clone();
            sample.source_reference = reference.clone();
        }
        if let Some(description) = descriptions.get_mut(index) {
            description.source_reference = reference;
        }
        persisted_paths.push(persisted_path);
    }

    for path in &source_temp_paths {
        if let Err(error) = remove_sampled_temp_frame(path) {
            log::warn!(
                "failed to remove sampled temp video frame {} after persistence: {error}",
                path.display()
            );
        }
    }

    Ok(PersistedVideoFrameAssets {
        samples,
        image_paths: persisted_paths,
        descriptions,
    })
}

fn cleanup_deferred_temp_frame_sources(paths: &[PathBuf]) {
    for path in paths {
        let _ = remove_sampled_temp_frame(path);
    }
}

pub(crate) fn remove_sampled_temp_frame(path: &Path) -> Result<(), WikiError> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(source) => Err(WikiError::Io {
            action: "remove sampled video frame temp file",
            path: Some(path.to_path_buf()),
            source,
        }),
    }
}

pub(crate) fn cleanup_sampled_temp_frame_sources(
    samples: &[VideoFrameSample],
    frame_image_paths: &[PathBuf],
) {
    let temp_dir = std::env::temp_dir();
    for (index, path) in frame_image_paths.iter().enumerate() {
        let should_cleanup = samples
            .get(index)
            .is_some_and(|sample| sample.source_asset.as_path() == path.as_path())
            && path.starts_with(&temp_dir);
        if should_cleanup {
            // Best-effort cleanup after the primary persistence failure has
            // already been selected for reporting.
            let _ = std::fs::remove_file(path);
        }
    }
}
