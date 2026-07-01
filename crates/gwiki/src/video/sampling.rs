use std::path::Path;

use crate::support::text::display_path;

use super::timestamps::format_timestamp;
use super::types::{FrameSamplingPlan, VideoAudioReference, VideoFrameSample};

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
