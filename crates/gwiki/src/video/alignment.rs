use std::collections::BTreeMap;

use crate::transcribe::TranscriptSegment;

use super::timestamps::{format_timestamp, parse_timestamp_seconds, transcript_start_seconds};
use super::types::{AlignedVideoSegment, VideoFrameDescription};

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

fn timestamp_seconds_or_zero(value: &str, label: &str) -> u32 {
    match parse_timestamp_seconds(value) {
        Some(seconds) => seconds,
        None => {
            log::debug!("invalid {label} timestamp {value:?}; aligning at 00:00:00");
            0
        }
    }
}
