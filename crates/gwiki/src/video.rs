mod alignment;
mod markdown;
mod sampling;
#[cfg(test)]
mod tests;
mod timestamps;
mod types;
mod write;

pub use markdown::write_video_derived_markdown;
pub use sampling::sample_frames;
pub use types::{
    AlignedVideoSegment, FrameSamplingPlan, VideoFrameDescription, VideoFrameSample,
    VideoMarkdownRequest, VideoMarkdownResult, VideoMediaDegradation, VideoMediaMetadata,
};
