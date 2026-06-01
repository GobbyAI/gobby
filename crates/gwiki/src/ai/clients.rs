use gobby_core::ai::daemon::{
    DaemonTranscriptionOptions, describe_image_via_daemon, generate_via_daemon,
    transcribe_via_daemon,
};
use gobby_core::ai::effective_route;
use gobby_core::ai::text::generate_text;
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

#[derive(serde::Deserialize)]
struct IndexedTranslation {
    i: usize,
    text: String,
}

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

    fn translate_to_english(
        &self,
        request: &TranscriptionRequest<'_>,
        language_hint: Option<&str>,
    ) -> Result<TranscriptionOutput, WikiError> {
        let capability = AiCapability::AudioTranslate;
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
                    language: language_hint,
                    prompt: None,
                },
            ),
            AiRouting::Direct => transcribe(
                &self.context,
                request.bytes.to_vec(),
                request.file_name,
                mime,
                TranscriptionTask::Translate,
                language_hint,
            ),
            AiRouting::Off | AiRouting::Auto => Err(route_unavailable(capability, route)),
        };

        result
            .map(transcription_output_from_core)
            .map_err(ai_error_to_wiki_error)
    }

    fn translate_segments(
        &self,
        segments: &[TranscriptSegment],
        source_lang: &str,
        target_lang: &str,
    ) -> Result<Vec<String>, WikiError> {
        let first = self.translate_segment_batch(segments, source_lang, target_lang);
        if let Ok(texts) = &first
            && texts.len() == segments.len()
        {
            return first;
        }
        warn_translation_batch_mismatch("first", segments.len(), &first);

        let second = self.translate_segment_batch(segments, source_lang, target_lang);
        if let Ok(texts) = &second
            && texts.len() == segments.len()
        {
            return second;
        }
        warn_translation_batch_mismatch("second", segments.len(), &second);

        segments
            .iter()
            .map(|segment| {
                self.translate_segment_batch(
                    std::slice::from_ref(segment),
                    source_lang,
                    target_lang,
                )
                .and_then(|texts| {
                    texts.into_iter().next().ok_or_else(|| WikiError::Config {
                        detail: "text translation returned no segment".to_string(),
                    })
                })
            })
            .collect()
    }
}

impl ProductionTranscriptionClient {
    fn translate_segment_batch(
        &self,
        segments: &[TranscriptSegment],
        source_lang: &str,
        target_lang: &str,
    ) -> Result<Vec<String>, WikiError> {
        let capability = AiCapability::TextGenerate;
        let route = effective_route(&self.context, capability);
        let prompt = segment_translation_prompt(segments, source_lang, target_lang)?;
        let system = "Return only valid JSON. Preserve array length and segment indexes.";
        let result = match route {
            AiRouting::Daemon => generate_via_daemon(&self.context, &prompt, Some(system)),
            AiRouting::Direct => generate_text(&self.context, &prompt, Some(system)),
            AiRouting::Off | AiRouting::Auto => Err(route_unavailable(capability, route)),
        }
        .map_err(ai_error_to_wiki_error)?;

        parse_indexed_translation(&result.text, segments.len())
    }
}

fn segment_translation_prompt(
    segments: &[TranscriptSegment],
    source_lang: &str,
    target_lang: &str,
) -> Result<String, WikiError> {
    let indexed_segments = segments
        .iter()
        .enumerate()
        .map(|(i, segment)| serde_json::json!({ "i": i, "text": segment.text }))
        .collect::<Vec<_>>();
    let payload = serde_json::to_string(&indexed_segments).map_err(|source| WikiError::Json {
        action: "serialize translation prompt",
        path: None,
        source,
    })?;
    Ok(format!(
        "Translate each transcript segment from {source_lang} to {target_lang}. Return only a JSON array of objects shaped as {{\"i\": number, \"text\": string}} using the same indexes and order.\nSegments: {payload}"
    ))
}

fn parse_indexed_translation(text: &str, expected_len: usize) -> Result<Vec<String>, WikiError> {
    let items: Vec<IndexedTranslation> =
        serde_json::from_str(text.trim()).map_err(|source| WikiError::Json {
            action: "parse translation response",
            path: None,
            source,
        })?;
    let mut translated = vec![None; expected_len];
    for item in items {
        if item.i >= expected_len {
            return Err(WikiError::Config {
                detail: format!(
                    "translation response index {} is out of range for {expected_len} transcript segment(s)",
                    item.i
                ),
            });
        }
        if translated[item.i].is_some() {
            return Err(WikiError::Config {
                detail: format!(
                    "translation response duplicated transcript segment index {}",
                    item.i
                ),
            });
        }
        translated[item.i] = Some(item.text);
    }
    translated
        .into_iter()
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| WikiError::Config {
            detail: "translation response omitted transcript segments".to_string(),
        })
}

fn warn_translation_batch_mismatch(
    attempt: &str,
    expected_len: usize,
    result: &Result<Vec<String>, WikiError>,
) {
    if let Ok(texts) = result {
        eprintln!(
            "Warning: {attempt} translation batch returned {} text(s) for {expected_len} segment(s); retrying with smaller batches",
            texts.len()
        );
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
    let source_language = result.source_language;
    // `language` is the output transcript language for translated results;
    // `source_language` is only the detection fallback for plain transcription.
    let language = result.language.or_else(|| source_language.clone());
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
        language,
        model: result.model,
        source_language,
        task: result.task,
        target_language: result.target_language,
        translated: result.translated,
        partial: false,
        completed_ranges: Vec::new(),
        missing_ranges: Vec::new(),
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
    fn clients_consume_effective_off_and_direct_routes() {
        let audio_client =
            ProductionTranscriptionClient::new(test_context(binding(AiRouting::Off, None)));
        let audio_path = PathBuf::from("raw/audio.wav");
        let audio_error = audio_client
            .transcribe(&TranscriptionRequest {
                file_name: "audio.wav",
                mime_type: Some("audio/wav"),
                asset_path: &audio_path,
                bytes: b"audio",
            })
            .expect_err("off routing resolves off");

        assert!(
            audio_error.to_string().contains(
                "audio_transcribe unavailable after shared effective routing resolved off"
            )
        );

        let direct_audio_client =
            ProductionTranscriptionClient::new(test_context(binding(AiRouting::Direct, None)));
        let direct_audio_error = direct_audio_client
            .transcribe(&TranscriptionRequest {
                file_name: "audio.wav",
                mime_type: Some("audio/wav"),
                asset_path: &audio_path,
                bytes: b"audio",
            })
            .expect_err("explicit direct routing is forced to the direct transport");

        assert!(
            direct_audio_error
                .to_string()
                .contains("ai.audio_transcribe.api_base is required for direct audio transcribe")
        );

        let vision_client =
            ProductionVisionClient::new(test_context(binding(AiRouting::Off, None)));
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
            .expect_err("off routing resolves off");

        assert!(
            vision_error
                .to_string()
                .contains("vision_extract unavailable after shared effective routing resolved off")
        );
    }

    #[test]
    fn indexed_translation_errors_name_bad_index_shape() {
        let duplicate =
            parse_indexed_translation(r#"[{"i":0,"text":"first"},{"i":0,"text":"again"}]"#, 2)
                .expect_err("duplicate rejected");
        assert!(duplicate.to_string().contains("duplicated"));

        let out_of_range = parse_indexed_translation(r#"[{"i":2,"text":"bad"}]"#, 1)
            .expect_err("out of range rejected");
        assert!(out_of_range.to_string().contains("out of range"));
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

    fn binding(routing: AiRouting, api_base: Option<&str>) -> CapabilityBinding {
        CapabilityBinding {
            routing,
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
