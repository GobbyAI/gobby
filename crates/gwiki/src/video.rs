use std::collections::BTreeMap;
#[cfg(unix)]
use std::fs::File;
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};

use crate::ingest::{MetadataValue, markdown_metadata_values, markdown_title, single_line};
use crate::sources::SourceRecord;
use crate::support::text::display_path;
use crate::transcribe::{
    TranscriptSegment, TranscriptionDegradation, TranscriptionOutput, TranscriptionRange,
};
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrameSamplingPlan {
    pub duration_seconds: Option<u32>,
    pub interval_seconds: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoFrameSample {
    pub timestamp_seconds: u32,
    pub timestamp: String,
    pub source_asset: PathBuf,
    pub source_reference: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoFrameDescription {
    pub timestamp: String,
    pub source_reference: String,
    pub description: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlignedVideoSegment {
    pub timestamp: String,
    pub frame_descriptions: Vec<VideoFrameDescription>,
    pub transcript_segments: Vec<TranscriptSegment>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoAudioReference {
    pub source_asset: PathBuf,
    pub source_reference: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoMarkdownResult {
    pub path: PathBuf,
    pub aligned_segments: Vec<AlignedVideoSegment>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoMediaMetadata {
    pub file_size_bytes: u64,
    pub duration_seconds: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VideoMediaDegradation {
    pub kind: String,
    pub reason: String,
    pub message: String,
}

pub fn sample_frames(asset_path: &Path, plan: FrameSamplingPlan) -> Vec<VideoFrameSample> {
    if plan.interval_seconds == 0 {
        return Vec::new();
    }
    let interval_seconds = plan.interval_seconds;
    let mut samples = Vec::new();
    let Some(duration_seconds) = plan.duration_seconds else {
        samples.push(frame_sample(asset_path, 0));
        return samples;
    };

    let mut timestamp_seconds = 0;
    loop {
        samples.push(frame_sample(asset_path, timestamp_seconds));
        let Some(next_timestamp) = timestamp_seconds.checked_add(interval_seconds) else {
            break;
        };
        if next_timestamp > duration_seconds {
            break;
        }
        timestamp_seconds = next_timestamp;
    }

    samples
}

pub fn audio_reference_for_video(asset_path: &Path) -> VideoAudioReference {
    let source_asset = asset_path.to_path_buf();
    let source_reference = format!("{}#audio", display_path(asset_path));
    VideoAudioReference {
        source_asset,
        source_reference,
    }
}

pub fn align_transcript_and_frames(
    transcript_segments: &[TranscriptSegment],
    frame_descriptions: &[VideoFrameDescription],
) -> Vec<AlignedVideoSegment> {
    let mut aligned = BTreeMap::<u32, AlignedVideoSegment>::new();

    for frame in frame_descriptions {
        let timestamp_seconds = timestamp_seconds_or_zero(&frame.timestamp, "video frame");
        aligned
            .entry(timestamp_seconds)
            .or_insert_with(|| AlignedVideoSegment {
                timestamp: format_timestamp(timestamp_seconds),
                frame_descriptions: Vec::new(),
                transcript_segments: Vec::new(),
            })
            .frame_descriptions
            .push(frame.clone());
    }

    if aligned.is_empty() {
        for segment in transcript_segments {
            let timestamp_seconds = transcript_start_seconds(segment);
            aligned
                .entry(timestamp_seconds)
                .or_insert_with(|| AlignedVideoSegment {
                    timestamp: format_timestamp(timestamp_seconds),
                    frame_descriptions: Vec::new(),
                    transcript_segments: Vec::new(),
                })
                .transcript_segments
                .push(segment.clone());
        }
        return aligned.into_values().collect();
    }

    // Non-empty: the early return above handles the no-frame case.
    let frame_timestamps = aligned.keys().copied().collect::<Vec<_>>();
    for segment in transcript_segments {
        let timestamp_seconds = transcript_start_seconds(segment);
        let aligned_timestamp = frame_timestamps
            .iter()
            .copied()
            .take_while(|frame_timestamp| *frame_timestamp <= timestamp_seconds)
            .last()
            .unwrap_or(frame_timestamps[0]);

        aligned
            .entry(aligned_timestamp)
            .or_insert_with(|| AlignedVideoSegment {
                timestamp: format_timestamp(aligned_timestamp),
                frame_descriptions: Vec::new(),
                transcript_segments: Vec::new(),
            })
            .transcript_segments
            .push(segment.clone());
    }

    aligned.into_values().collect()
}

fn transcript_start_seconds(segment: &TranscriptSegment) -> u32 {
    let seconds = segment.start_ms / 1_000;
    seconds.min(u64::from(u32::MAX)) as u32
}

fn timestamp_seconds_or_zero(value: &str, label: &str) -> u32 {
    match parse_timestamp_seconds(value) {
        Some(seconds) => seconds,
        None => {
            log::debug!("invalid {label} timestamp {value:?}; aligning at 00:00:00");
            0
        }
    }
}

pub struct VideoMarkdownRequest<'a> {
    pub file_name: &'a str,
    pub mime_type: Option<&'a str>,
    pub asset_path: &'a Path,
    pub raw_path: &'a Path,
    pub duration_seconds: Option<u32>,
    pub media_metadata: Option<VideoMediaMetadata>,
    pub media_degradations: &'a [VideoMediaDegradation],
    pub transcription_degradation: Option<&'a TranscriptionDegradation>,
    pub frame_interval_seconds: u32,
    pub frame_samples: &'a [VideoFrameSample],
    pub frame_image_paths: &'a [PathBuf],
    pub frame_descriptions: &'a [VideoFrameDescription],
    pub transcript_segments: &'a [TranscriptSegment],
    pub transcription: Option<&'a TranscriptionOutput>,
}

pub fn write_video_derived_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: VideoMarkdownRequest<'_>,
) -> Result<VideoMarkdownResult, WikiError> {
    let aligned_segments =
        align_transcript_and_frames(request.transcript_segments, request.frame_descriptions);
    let relative_path = derived_markdown_path(record);
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

fn write_video_markdown_atomic(path: &Path, bytes: &[u8]) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp_path)
        .map_err(|error| WikiError::Io {
            action: "create video derived markdown temp file",
            path: Some(temp_path.clone()),
            source: error,
        })?;
    if let Err(error) = file.write_all(bytes) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write video derived markdown temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync video derived markdown temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = fs::rename(&temp_path, path) {
        if error.kind() == ErrorKind::AlreadyExists {
            if let Err(remove_error) = fs::remove_file(path) {
                let _ = fs::remove_file(&temp_path);
                return Err(WikiError::Io {
                    action: "replace existing video derived markdown",
                    path: Some(path.to_path_buf()),
                    source: remove_error,
                });
            }
            if let Err(rename_error) = fs::rename(&temp_path, path) {
                let _ = fs::remove_file(&temp_path);
                return Err(WikiError::Io {
                    action: "write video derived markdown",
                    path: Some(path.to_path_buf()),
                    source: rename_error,
                });
            }
            return sync_parent_dir(path);
        }
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write video derived markdown",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("video.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}

fn sync_parent_dir(path: &Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(())
    }
    #[cfg(unix)]
    {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync video derived markdown directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}

fn frame_sample(asset_path: &Path, timestamp_seconds: u32) -> VideoFrameSample {
    let timestamp = format_timestamp(timestamp_seconds);
    let source_asset = asset_path.to_path_buf();
    let source_reference = format!("{}#t={timestamp}", display_path(asset_path));

    VideoFrameSample {
        timestamp_seconds,
        timestamp,
        source_asset,
        source_reference,
    }
}

fn derived_markdown_path(record: &SourceRecord) -> PathBuf {
    PathBuf::from("knowledge")
        .join("sources")
        .join(format!("{}.md", record.id))
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
            MetadataValue::string(degradation.reason.clone()),
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
            markdown.push_str(&single_line(&degradation.reason));
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

fn parse_timestamp_seconds(value: &str) -> Option<u32> {
    let parts = value
        .split(':')
        .map(parse_timestamp_part)
        .collect::<Option<Vec<_>>>()?;

    match parts.as_slice() {
        [seconds] => Some(*seconds),
        [minutes, seconds] => minutes.checked_mul(60)?.checked_add(*seconds),
        [hours, minutes, seconds] => hours
            .checked_mul(3600)?
            .checked_add(minutes.checked_mul(60)?)?
            .checked_add(*seconds),
        _ => None,
    }
}

fn parse_timestamp_part(value: &str) -> Option<u32> {
    value
        .split_once('.')
        .map_or(value, |(seconds, _)| seconds)
        .trim()
        .parse()
        .ok()
}

fn format_timestamp(seconds: u32) -> String {
    let hours = seconds / 3600;
    let minutes = (seconds % 3600) / 60;
    let seconds = seconds % 60;
    format!("{hours:02}:{minutes:02}:{seconds:02}")
}

fn format_ranges_ms(ranges: &[TranscriptionRange]) -> String {
    ranges
        .iter()
        .map(|range| format!("{}-{}", range.start_ms, range.end_ms))
        .collect::<Vec<_>>()
        .join(",")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
    use crate::transcribe::{TranscriptSegment, TranscriptionDegradation, TranscriptionOutput};

    #[test]
    fn frame_sampling_records_timestamps() {
        let asset_path = PathBuf::from("raw/assets/lecture.mp4");

        let samples = sample_frames(
            &asset_path,
            FrameSamplingPlan {
                duration_seconds: Some(7),
                interval_seconds: 3,
            },
        );

        assert_eq!(
            samples
                .iter()
                .map(|sample| sample.timestamp.as_str())
                .collect::<Vec<_>>(),
            vec!["00:00:00", "00:00:03", "00:00:06"]
        );
        assert!(
            samples
                .iter()
                .all(|sample| sample.source_asset == asset_path)
        );
        assert_eq!(
            samples[1].source_reference,
            "raw/assets/lecture.mp4#t=00:00:03"
        );
    }

    #[test]
    fn zero_frame_interval_disables_sampling() {
        let samples = sample_frames(
            &PathBuf::from("raw/assets/lecture.mp4"),
            FrameSamplingPlan {
                duration_seconds: Some(7),
                interval_seconds: 0,
            },
        );

        assert!(samples.is_empty());
    }

    #[test]
    fn aligns_transcript_and_frames() {
        let frame_descriptions = vec![
            VideoFrameDescription {
                timestamp: "00:00:00".to_string(),
                source_reference: "raw/assets/lecture.mp4#t=00:00:00".to_string(),
                description: "Wide shot of the speaker at the podium.".to_string(),
            },
            VideoFrameDescription {
                timestamp: "00:00:05".to_string(),
                source_reference: "raw/assets/lecture.mp4#t=00:00:05".to_string(),
                description: "Slide with the alignment diagram.".to_string(),
            },
        ];
        let transcript_segments = vec![
            TranscriptSegment {
                start_ms: 2_000,
                end_ms: 3_500,
                text: "The speaker introduces the data collection setup.".to_string(),
            },
            TranscriptSegment {
                start_ms: 6_000,
                end_ms: 7_500,
                text: "The diagram shows transcript and frame alignment.".to_string(),
            },
        ];

        let aligned = align_transcript_and_frames(&transcript_segments, &frame_descriptions);

        assert_eq!(aligned.len(), 2);
        assert_eq!(aligned[0].timestamp, "00:00:00");
        assert_eq!(aligned[0].frame_descriptions[0], frame_descriptions[0]);
        assert_eq!(aligned[0].transcript_segments[0], transcript_segments[0]);
        assert_eq!(aligned[1].timestamp, "00:00:05");
        assert_eq!(aligned[1].frame_descriptions[0], frame_descriptions[1]);
        assert_eq!(aligned[1].transcript_segments[0], transcript_segments[1]);
    }

    #[test]
    fn aligns_on_numeric_start_ms() {
        let frame_descriptions = vec![
            VideoFrameDescription {
                timestamp: "00:00:00".to_string(),
                source_reference: "raw/assets/lecture.mp4#t=00:00:00".to_string(),
                description: "Wide shot of the speaker at the podium.".to_string(),
            },
            VideoFrameDescription {
                timestamp: "00:00:05".to_string(),
                source_reference: "raw/assets/lecture.mp4#t=00:00:05".to_string(),
                description: "Slide with the alignment diagram.".to_string(),
            },
        ];
        let transcript_segments = vec![
            TranscriptSegment {
                start_ms: 2_450,
                end_ms: 4_950,
                text: "The speaker introduces the data collection setup.".to_string(),
            },
            TranscriptSegment {
                start_ms: 5_000,
                end_ms: 7_250,
                text: "The diagram shows transcript and frame alignment.".to_string(),
            },
        ];

        let aligned = align_transcript_and_frames(&transcript_segments, &frame_descriptions);

        assert_eq!(aligned.len(), 2);
        assert_eq!(aligned[0].timestamp, "00:00:00");
        assert_eq!(aligned[0].frame_descriptions[0], frame_descriptions[0]);
        assert_eq!(aligned[0].transcript_segments[0], transcript_segments[0]);
        assert_eq!(aligned[1].timestamp, "00:00:05");
        assert_eq!(aligned[1].frame_descriptions[0], frame_descriptions[1]);
        assert_eq!(aligned[1].transcript_segments[0], transcript_segments[1]);
    }

    #[test]
    fn partial_failure_matrix() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let raw_path = PathBuf::from("raw/source-note.md");
        let asset_path = PathBuf::from("raw/assets/lecture.mp4");
        let transcript_segments = vec![TranscriptSegment {
            start_ms: 1_000,
            end_ms: 2_000,
            text: "Transcript survives frame extraction failure.".to_string(),
        }];
        let frame_descriptions = vec![VideoFrameDescription {
            timestamp: "00:00:04".to_string(),
            source_reference: "raw/assets/lecture.mp4#t=00:00:04".to_string(),
            description: "Frame survives STT failure.".to_string(),
        }];
        let no_frames = [VideoMediaDegradation {
            kind: "frames".to_string(),
            reason: "extraction_failed".to_string(),
            message: "ffmpeg frame sampling failed".to_string(),
        }];
        let transcription_degradation = TranscriptionDegradation {
            reason: "provider_failed".to_string(),
            fallback: "STT provider failed after frames were extracted.".to_string(),
        };

        let transcript_only = write_video_derived_markdown(
            temp.path(),
            &ScopeIdentity::topic("field-work"),
            &record,
            VideoMarkdownRequest {
                file_name: "lecture.mp4",
                mime_type: Some("video/mp4"),
                asset_path: &asset_path,
                raw_path: &raw_path,
                duration_seconds: Some(8),
                media_metadata: Some(VideoMediaMetadata {
                    file_size_bytes: 12,
                    duration_seconds: Some(8),
                }),
                media_degradations: &no_frames,
                transcription_degradation: None,
                frame_interval_seconds: 4,
                frame_samples: &[],
                frame_image_paths: &[],
                frame_descriptions: &[],
                transcript_segments: &transcript_segments,
                transcription: Some(&transcription_output(&transcript_segments)),
            },
        )
        .expect("write transcript-only degradation doc");

        let transcript_only_doc =
            std::fs::read_to_string(temp.path().join(transcript_only.path)).expect("read doc");
        assert!(transcript_only_doc.contains("media_degradation: \"frames:extraction_failed\""));
        assert!(transcript_only_doc.contains("Transcript survives frame extraction failure."));
        assert!(transcript_only_doc.contains("No frame samples recorded."));

        let frame_timeline = write_video_derived_markdown(
            temp.path(),
            &ScopeIdentity::topic("field-work"),
            &record,
            VideoMarkdownRequest {
                file_name: "lecture.mp4",
                mime_type: Some("video/mp4"),
                asset_path: &asset_path,
                raw_path: &raw_path,
                duration_seconds: Some(8),
                media_metadata: Some(VideoMediaMetadata {
                    file_size_bytes: 12,
                    duration_seconds: Some(8),
                }),
                media_degradations: &[],
                transcription_degradation: Some(&transcription_degradation),
                frame_interval_seconds: 4,
                frame_samples: &[VideoFrameSample {
                    timestamp_seconds: 4,
                    timestamp: "00:00:04".to_string(),
                    source_asset: asset_path.clone(),
                    source_reference: "raw/assets/lecture.mp4#t=00:00:04".to_string(),
                }],
                frame_image_paths: &[],
                frame_descriptions: &frame_descriptions,
                transcript_segments: &[],
                transcription: None,
            },
        )
        .expect("write frame-only degradation doc");

        let frame_timeline_doc =
            std::fs::read_to_string(temp.path().join(frame_timeline.path)).expect("read doc");
        assert!(frame_timeline_doc.contains("transcription_degradation: provider_failed"));
        assert!(frame_timeline_doc.contains("STT provider failed after frames were extracted."));
        assert!(frame_timeline_doc.contains("Frame survives STT failure."));
    }

    #[test]
    fn degradation_metadata_has_size_and_duration() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let raw_path = PathBuf::from("raw/source-note.md");
        let asset_path = PathBuf::from("raw/assets/lecture.mp4");

        let result = write_video_derived_markdown(
            temp.path(),
            &ScopeIdentity::topic("field-work"),
            &record,
            VideoMarkdownRequest {
                file_name: "lecture.mp4",
                mime_type: Some("video/mp4"),
                asset_path: &asset_path,
                raw_path: &raw_path,
                duration_seconds: Some(13),
                media_metadata: Some(VideoMediaMetadata {
                    file_size_bytes: 42,
                    duration_seconds: Some(13),
                }),
                media_degradations: &[VideoMediaDegradation {
                    kind: "media".to_string(),
                    reason: "ffmpeg_unavailable".to_string(),
                    message: "ffmpeg was not found".to_string(),
                }],
                transcription_degradation: None,
                frame_interval_seconds: 5,
                frame_samples: &[],
                frame_image_paths: &[],
                frame_descriptions: &[],
                transcript_segments: &[],
                transcription: None,
            },
        )
        .expect("write degradation metadata doc");

        let document = std::fs::read_to_string(temp.path().join(result.path)).expect("read doc");
        assert!(document.contains("video_duration_seconds: 13"));
        assert!(document.contains("file_size_bytes: 42"));
        assert!(document.contains("duration_seconds: 13"));
        assert!(document.contains("media_degradation: \"media:ffmpeg_unavailable\""));
        assert!(document.contains("ffmpeg was not found"));
    }

    fn record_for(temp: &Path) -> SourceRecord {
        SourceManifest::register(
            temp,
            SourceDraft {
                location: "/tmp/lecture.mp4".to_string(),
                kind: SourceKind::Video,
                fetched_at: "2026-05-29T21:30:00Z".to_string(),
                content: b"video-bytes".to_vec(),
                title: Some("lecture.mp4".to_string()),
                citation: Some("/tmp/lecture.mp4".to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register video source")
    }

    fn transcription_output(segments: &[TranscriptSegment]) -> TranscriptionOutput {
        TranscriptionOutput {
            segments: segments.to_vec(),
            language: Some("en".to_string()),
            model: Some("fake-stt".to_string()),
            source_language: Some("en".to_string()),
            task: Some("transcribe".to_string()),
            target_language: None,
            translated: false,
            translation_degraded: false,
            partial: false,
            completed_ranges: Vec::new(),
            missing_ranges: Vec::new(),
        }
    }
}
