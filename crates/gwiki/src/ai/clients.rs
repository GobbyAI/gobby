use gobby_core::ai::daemon::{
    DaemonTranscriptionOptions, describe_image_via_daemon, transcribe_via_daemon,
};
use gobby_core::ai::effective_route;
use gobby_core::ai::transcription::{TranscriptionTask, transcribe};
use gobby_core::ai::vision::describe_image;
use gobby_core::ai_context::AiContext;
use gobby_core::ai_types::{AiError, TranscriptionResult as CoreTranscriptionResult, VisionResult};
use gobby_core::config::{AiCapability, AiRouting};

use crate::WikiError;
use crate::transcribe::{
    TranscriptSegment, TranscriptionClient, TranscriptionOutput, TranscriptionRequest,
};
use crate::vision::{VisionClient, VisionExtraction, VisionRequest};

pub(crate) struct ProductionTranscriptionClient {
    context: AiContext,
}

impl ProductionTranscriptionClient {
    pub(crate) fn new(context: AiContext) -> Self {
        Self { context }
    }
}

impl TranscriptionClient for ProductionTranscriptionClient {
    fn transcribe(
        &self,
        request: &TranscriptionRequest<'_>,
    ) -> Result<TranscriptionOutput, WikiError> {
        let capability = AiCapability::AudioTranscribe;
        let route = effective_route(&self.context, capability);
        let mime = request.mime_type.unwrap_or("application/octet-stream");
        let result = match route {
            AiRouting::Daemon => transcribe_via_daemon(
                &self.context,
                request.bytes.to_vec(),
                request.file_name,
                mime,
                DaemonTranscriptionOptions {
                    capability,
                    language: None,
                    prompt: None,
                },
            ),
            AiRouting::Direct => transcribe(
                &self.context,
                request.bytes.to_vec(),
                request.file_name,
                mime,
                TranscriptionTask::Transcribe,
                None,
            ),
            AiRouting::Off | AiRouting::Auto => Err(route_unavailable(capability, route)),
        };

        result
            .map(transcription_output_from_core)
            .map_err(ai_error_to_wiki_error)
    }
}

pub(crate) struct ProductionVisionClient {
    context: AiContext,
}

impl ProductionVisionClient {
    pub(crate) fn new(context: AiContext) -> Self {
        Self { context }
    }
}

impl VisionClient for ProductionVisionClient {
    fn extract(&self, request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
        let capability = AiCapability::VisionExtract;
        let route = effective_route(&self.context, capability);
        let mime = request.mime_type.unwrap_or("application/octet-stream");
        let result = match route {
            AiRouting::Daemon => describe_image_via_daemon(
                &self.context,
                request.bytes.to_vec(),
                request.file_name,
                mime,
            ),
            AiRouting::Direct => describe_image(&self.context, request.bytes.to_vec(), mime),
            AiRouting::Off | AiRouting::Auto => Err(route_unavailable(capability, route)),
        };

        result
            .map(vision_extraction_from_core)
            .map_err(ai_error_to_wiki_error)
    }
}

fn route_unavailable(capability: AiCapability, route: AiRouting) -> AiError {
    AiError::capability_unavailable(
        capability.as_str(),
        format!(
            "{} unavailable after shared effective routing resolved {}",
            capability.as_str(),
            route_name(route)
        ),
    )
}

fn route_name(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Auto => "auto",
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Off => "off",
    }
}

fn ai_error_to_wiki_error(error: AiError) -> WikiError {
    WikiError::Daemon {
        endpoint: "gcore::ai",
        message: error.to_string(),
    }
}

fn transcription_output_from_core(result: CoreTranscriptionResult) -> TranscriptionOutput {
    TranscriptionOutput {
        segments: result
            .segments
            .into_iter()
            .map(|segment| TranscriptSegment {
                start_ms: segment.start_ms,
                end_ms: segment.end_ms,
                text: segment.text,
            })
            .collect(),
        language: result.language.or(result.source_language),
        model: result.model,
    }
}

fn vision_extraction_from_core(result: VisionResult) -> VisionExtraction {
    let mut metadata = result.metadata.into_iter().collect::<Vec<_>>();
    if let Some(model) = result.model
        && !metadata.iter().any(|(key, _)| key == "model")
    {
        metadata.push(("model".to_string(), model));
    }

    VisionExtraction {
        description: result.description,
        ocr_text: result.ocr_text,
        metadata,
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use gobby_core::ai_context::{AiBindings, AiContext, AiLimiter};
    use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};

    use super::*;

    #[test]
    fn clients_consume_effective_route() {
        let audio_client = ProductionTranscriptionClient::new(test_context(direct_binding(None)));
        let audio_path = PathBuf::from("raw/audio.wav");
        let audio_error = audio_client
            .transcribe(&TranscriptionRequest {
                file_name: "audio.wav",
                mime_type: Some("audio/wav"),
                asset_path: &audio_path,
                bytes: b"audio",
            })
            .expect_err("direct routing without api_base resolves off");

        assert!(
            audio_error.to_string().contains(
                "audio_transcribe unavailable after shared effective routing resolved off"
            )
        );

        let vision_client = ProductionVisionClient::new(test_context(direct_binding(None)));
        let image_path = PathBuf::from("raw/image.png");
        let vision_error = vision_client
            .extract(&VisionRequest {
                file_name: "image.png",
                mime_type: Some("image/png"),
                asset_path: &image_path,
                bytes: b"image",
                width: None,
                height: None,
            })
            .expect_err("direct routing without api_base resolves off");

        assert!(
            vision_error
                .to_string()
                .contains("vision_extract unavailable after shared effective routing resolved off")
        );
    }

    fn test_context(binding: CapabilityBinding) -> AiContext {
        AiContext {
            bindings: AiBindings {
                embed: binding.clone(),
                audio_transcribe: binding.clone(),
                audio_translate: binding.clone(),
                vision_extract: binding.clone(),
                text_generate: binding,
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: AiLimiter::new(1),
            project_id: Some("project-123".to_string()),
        }
    }

    fn direct_binding(api_base: Option<&str>) -> CapabilityBinding {
        CapabilityBinding {
            routing: AiRouting::Direct,
            transport: None,
            api_base: api_base.map(str::to_string),
            api_key: None,
            model: Some("test-model".to_string()),
            provider: Some("test-provider".to_string()),
            task: None,
            language: None,
            target_lang: None,
        }
    }
}
