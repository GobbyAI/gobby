use super::*;
use std::path::{Path, PathBuf};

use super::alignment::align_transcript_and_frames;
use crate::ScopeIdentity;
use crate::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest, SourceRecord,
};
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
        reason: gobby_core::degradation::ModalityDegradationReason::TranscriptionError,
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
    assert!(frame_timeline_doc.contains("transcription_degradation: transcription_error"));
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
