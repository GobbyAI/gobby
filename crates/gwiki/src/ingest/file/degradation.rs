use crate::ingest::video::VideoIngestResult;
use crate::vision::VisionDegradation;

pub(super) fn transcription_degradation_summary(
    degradation: &crate::transcribe::TranscriptionDegradation,
) -> String {
    format!(
        "audio_transcription:{}:{}",
        degradation.reason, degradation.fallback
    )
}

pub(super) fn vision_degradation_summary(degradation: &VisionDegradation) -> String {
    format!("vision:{}:{}", degradation.reason, degradation.fallback)
}

#[cfg(feature = "documents")]
pub(super) fn document_degradation_summary(
    degradation: &crate::document::DocumentDegradation,
) -> String {
    format!("document:{}:{}", degradation.reason(), degradation.fallback)
}

pub(super) fn video_degradation_summaries(result: &VideoIngestResult) -> Vec<String> {
    let mut degradations = result
        .media_degradations
        .iter()
        .map(|degradation| {
            format!(
                "video_{}:{}:{}",
                degradation.kind, degradation.reason, degradation.message
            )
        })
        .collect::<Vec<_>>();
    if let Some(degradation) = &result.transcription_degradation {
        degradations.push(transcription_degradation_summary(degradation));
    }
    degradations
}
