use std::path::Path;

use crate::ingest::{MetadataValue, markdown_metadata_values, markdown_title, single_line};
use crate::paths::derived_markdown_path;
use crate::sources::SourceRecord;
use crate::support::text::display_path;
use crate::{ScopeIdentity, WikiError};

use super::alignment::align_transcript_and_frames;
use super::sampling::audio_reference_for_video;
use super::timestamps::{format_ranges_ms, format_timestamp, transcript_start_seconds};
use super::types::{AlignedVideoSegment, VideoMarkdownRequest, VideoMarkdownResult};
use super::write::write_video_markdown_atomic;

pub fn write_video_derived_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: VideoMarkdownRequest<'_>,
) -> Result<VideoMarkdownResult, WikiError> {
    let aligned_segments =
        align_transcript_and_frames(request.transcript_segments, request.frame_descriptions);
    let relative_path = derived_markdown_path(record)?;
    let path = vault_root.join(&relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create video derived markdown directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let markdown = render_video_derived_markdown(scope, record, &request, &aligned_segments);
    write_video_markdown_atomic(&path, markdown.as_bytes())?;

    Ok(VideoMarkdownResult {
        path: relative_path,
        aligned_segments,
    })
}

fn render_video_derived_markdown(
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: &VideoMarkdownRequest<'_>,
    aligned_segments: &[AlignedVideoSegment],
) -> String {
    let title = markdown_title(request.file_name);
    let asset_path = display_path(request.asset_path);
    let raw_path = display_path(request.raw_path);
    let audio_reference = audio_reference_for_video(request.asset_path);
    let audio_source_reference = audio_reference.source_reference;
    let mut fields = vec![
        ("title", MetadataValue::string(title.clone())),
        ("source_kind", MetadataValue::string("video")),
        (
            "source_location",
            MetadataValue::string(record.location.clone()),
        ),
        (
            "source_hash",
            MetadataValue::string(record.content_hash.clone()),
        ),
        ("source_asset", MetadataValue::string(asset_path.clone())),
        ("source_raw", MetadataValue::string(raw_path.clone())),
        (
            "fetched_at",
            MetadataValue::string(record.fetched_at.clone()),
        ),
        ("scope_kind", MetadataValue::string(scope.kind.as_str())),
        ("scope_id", MetadataValue::string(scope.id.clone())),
        (
            "video_frame_interval_seconds",
            MetadataValue::number(request.frame_interval_seconds),
        ),
        (
            "video_frame_sample_count",
            MetadataValue::number(request.frame_samples.len()),
        ),
        (
            "video_frame_image_count",
            MetadataValue::number(request.frame_image_paths.len()),
        ),
        (
            "video_frame_description_count",
            MetadataValue::number(request.frame_descriptions.len()),
        ),
        (
            "video_transcript_segment_count",
            MetadataValue::number(request.transcript_segments.len()),
        ),
        (
            "audio_reference",
            MetadataValue::string(audio_source_reference.clone()),
        ),
        (
            "transcription_status",
            MetadataValue::string(if request.transcription.is_some() {
                "transcribed"
            } else if request.transcription_degradation.is_some() {
                "degraded"
            } else {
                "unavailable"
            }),
        ),
    ];
    if let Some(mime_type) = request.mime_type {
        fields.push(("video_mime_type", MetadataValue::string(mime_type)));
    }
    if let Some(duration_seconds) = request.duration_seconds {
        fields.push((
            "video_duration_seconds",
            MetadataValue::number(duration_seconds),
        ));
    }
    if let Some(metadata) = &request.media_metadata {
        fields.push((
            "file_size_bytes",
            MetadataValue::number(metadata.file_size_bytes),
        ));
        if let Some(duration_seconds) = metadata.duration_seconds {
            fields.push(("duration_seconds", MetadataValue::number(duration_seconds)));
        }
    }
    if !request.media_degradations.is_empty() {
        fields.push((
            "media_degradation",
            MetadataValue::string(
                request
                    .media_degradations
                    .iter()
                    .map(|degradation| format!("{}:{}", degradation.kind, degradation.reason))
                    .collect::<Vec<_>>()
                    .join(","),
            ),
        ));
    }
    if let Some(degradation) = request.transcription_degradation {
        fields.push((
            "transcription_degradation",
            MetadataValue::string(degradation.reason.to_string()),
        ));
    }
    if let Some(output) = request.transcription {
        if let Some(language) = &output.language {
            fields.push((
                "transcription_language",
                MetadataValue::string(language.clone()),
            ));
        }
        if let Some(model) = &output.model {
            fields.push(("transcription_model", MetadataValue::string(model.clone())));
        }
        if let Some(source_language) = &output.source_language {
            fields.push((
                "transcription_source_language",
                MetadataValue::string(source_language.clone()),
            ));
        }
        if let Some(task) = &output.task {
            fields.push(("transcription_task", MetadataValue::string(task.clone())));
        }
        if let Some(target_language) = &output.target_language {
            fields.push((
                "transcription_target_language",
                MetadataValue::string(target_language.clone()),
            ));
        }
        fields.push((
            "translated",
            MetadataValue::string(output.translated.to_string()),
        ));
        if !output.completed_ranges.is_empty() {
            fields.push((
                "transcription_completed_ranges",
                MetadataValue::string(format_ranges_ms(&output.completed_ranges)),
            ));
        }
        if output.partial {
            fields.push(("transcription_partial", MetadataValue::string("true")));
            if !output.missing_ranges.is_empty() {
                fields.push((
                    "transcription_missing_ranges",
                    MetadataValue::string(format_ranges_ms(&output.missing_ranges)),
                ));
            }
        }
    }

    let mut markdown = markdown_metadata_values(&fields);
    markdown.push_str("# ");
    markdown.push_str(&title);
    markdown.push_str("\n\n");
    markdown.push_str("Original video: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n\n");
    markdown.push_str("Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n\n");
    markdown.push_str("Audio reference: `");
    markdown.push_str(&audio_source_reference);
    markdown.push_str("`\n\n");

    if !request.media_degradations.is_empty() || request.transcription_degradation.is_some() {
        markdown.push_str("## Degradations\n\n");
        for degradation in request.media_degradations {
            markdown.push_str("- ");
            markdown.push_str(&single_line(&degradation.kind));
            markdown.push_str(": ");
            markdown.push_str(&single_line(&degradation.reason));
            markdown.push_str(" - ");
            markdown.push_str(&single_line(&degradation.message));
            markdown.push('\n');
        }
        if let Some(degradation) = request.transcription_degradation {
            markdown.push_str("- transcription: ");
            markdown.push_str(&single_line(degradation.reason.as_str()));
            markdown.push_str(" - ");
            markdown.push_str(&single_line(&degradation.fallback));
            markdown.push('\n');
        }
        markdown.push('\n');
    }

    markdown.push_str("## Frame Samples\n\n");
    if request.frame_samples.is_empty() {
        markdown.push_str("No frame samples recorded.\n\n");
    } else {
        for sample in request.frame_samples {
            markdown.push_str("- [");
            markdown.push_str(&sample.timestamp);
            markdown.push_str("] `");
            markdown.push_str(&sample.source_reference);
            markdown.push_str("`\n");
        }
        markdown.push('\n');
    }

    if !request.frame_image_paths.is_empty() {
        markdown.push_str("## Frame Images\n\n");
        for path in request.frame_image_paths {
            markdown.push_str("- `");
            markdown.push_str(&display_path(path));
            markdown.push_str("`\n");
        }
        markdown.push('\n');
    }

    markdown.push_str("## Aligned Transcript and Frames\n\n");
    if aligned_segments.is_empty() {
        markdown.push_str("No transcript or frame descriptions available.\n\n");
    } else {
        for segment in aligned_segments {
            markdown.push_str("### ");
            markdown.push_str(&segment.timestamp);
            markdown.push_str("\n\n");

            if !segment.frame_descriptions.is_empty() {
                markdown.push_str("Frame descriptions:\n\n");
                for frame in &segment.frame_descriptions {
                    markdown.push_str("- `");
                    markdown.push_str(&single_line(&frame.source_reference));
                    markdown.push_str("`: ");
                    markdown.push_str(&single_line(&frame.description));
                    markdown.push('\n');
                }
                markdown.push('\n');
            }

            if !segment.transcript_segments.is_empty() {
                markdown.push_str("Transcript:\n\n");
                for transcript in &segment.transcript_segments {
                    markdown.push_str("- [");
                    markdown.push_str(&format_timestamp(transcript_start_seconds(transcript)));
                    markdown.push_str("] ");
                    markdown.push_str(&single_line(&transcript.text));
                    markdown.push('\n');
                }
                markdown.push('\n');
            }
        }
    }

    markdown.push_str("## Source References\n\n");
    markdown.push_str("- Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n");
    markdown.push_str("- Original video: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n");
    markdown.push_str("- Audio reference: `");
    markdown.push_str(&audio_source_reference);
    markdown.push_str("`\n");
    if let Some(citation) = &record.citation {
        markdown.push_str("- Citation: ");
        markdown.push_str(&single_line(citation));
        markdown.push('\n');
    }
    markdown
}
