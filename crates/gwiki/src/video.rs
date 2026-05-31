use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::ingest::{markdown_metadata, markdown_title, single_line};
use crate::sources::SourceRecord;
use crate::transcribe::TranscriptSegment;
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
    let source_reference = format!("{}#audio", path_to_string(asset_path));
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
            let timestamp_seconds = timestamp_seconds_or_zero(&segment.timestamp, "transcript");
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
        let timestamp_seconds = timestamp_seconds_or_zero(&segment.timestamp, "transcript");
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
    pub frame_interval_seconds: u32,
    pub frame_samples: &'a [VideoFrameSample],
    pub frame_descriptions: &'a [VideoFrameDescription],
    pub transcript_segments: &'a [TranscriptSegment],
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
    std::fs::write(&path, markdown).map_err(|error| WikiError::Io {
        action: "write video derived markdown",
        path: Some(path),
        source: error,
    })?;

    Ok(VideoMarkdownResult {
        path: relative_path,
        aligned_segments,
    })
}

fn frame_sample(asset_path: &Path, timestamp_seconds: u32) -> VideoFrameSample {
    let timestamp = format_timestamp(timestamp_seconds);
    let source_asset = asset_path.to_path_buf();
    let source_reference = format!("{}#t={timestamp}", path_to_string(asset_path));

    VideoFrameSample {
        timestamp_seconds,
        timestamp,
        source_asset,
        source_reference,
    }
}

fn derived_markdown_path(record: &SourceRecord) -> PathBuf {
    PathBuf::from("wiki")
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
    let asset_path = path_to_string(request.asset_path);
    let raw_path = path_to_string(request.raw_path);
    let audio_reference = audio_reference_for_video(request.asset_path);
    let audio_source_reference = audio_reference.source_reference;
    let mut fields = vec![
        ("title".to_string(), title.clone()),
        ("source_kind".to_string(), "video".to_string()),
        ("source_location".to_string(), record.location.clone()),
        ("source_hash".to_string(), record.content_hash.clone()),
        ("source_asset".to_string(), asset_path.clone()),
        ("source_raw".to_string(), raw_path.clone()),
        ("fetched_at".to_string(), record.fetched_at.clone()),
        ("scope_kind".to_string(), scope.kind.as_str().to_string()),
        ("scope_id".to_string(), scope.id.clone()),
        (
            "video_frame_interval_seconds".to_string(),
            request.frame_interval_seconds.to_string(),
        ),
        (
            "video_frame_sample_count".to_string(),
            request.frame_samples.len().to_string(),
        ),
        (
            "video_frame_description_count".to_string(),
            request.frame_descriptions.len().to_string(),
        ),
        (
            "video_transcript_segment_count".to_string(),
            request.transcript_segments.len().to_string(),
        ),
        (
            "audio_reference".to_string(),
            audio_source_reference.clone(),
        ),
    ];
    if let Some(mime_type) = request.mime_type {
        fields.push(("video_mime_type".to_string(), mime_type.to_string()));
    }
    if let Some(duration_seconds) = request.duration_seconds {
        fields.push((
            "video_duration_seconds".to_string(),
            duration_seconds.to_string(),
        ));
    }

    let mut markdown = {
        let field_refs = fields
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect::<Vec<_>>();
        markdown_metadata(&field_refs)
    };
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
                    markdown.push_str(&single_line(&transcript.timestamp));
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

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transcribe::TranscriptSegment;

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
                timestamp: "00:00:02".to_string(),
                text: "The speaker introduces the data collection setup.".to_string(),
            },
            TranscriptSegment {
                timestamp: "00:00:06".to_string(),
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
}
