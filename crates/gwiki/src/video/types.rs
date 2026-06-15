use std::path::{Path, PathBuf};

use crate::transcribe::{TranscriptSegment, TranscriptionDegradation, TranscriptionOutput};

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
