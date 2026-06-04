use std::path::{Path, PathBuf};

use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiCapability, AiRouting};

#[cfg(feature = "ai")]
use crate::ai::clients::ProductionTranscriptionClient;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::transcribe::{
    TranscriptionDegradation, TranscriptionEndpoint, TranscriptionMarkdownInput,
    TranscriptionRequest, write_audio_transcript_markdown,
};
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub mime_type: Option<String>,
    pub duration_seconds: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioIngestResult {
    pub record: crate::sources::SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub transcript_path: PathBuf,
    pub transcription_degradation: Option<TranscriptionDegradation>,
}

pub fn ingest_audio(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    ai_context: &AiContext,
) -> Result<AudioIngestResult, WikiError> {
    ingest_audio_with_transcription(
        vault_root,
        store,
        scope,
        snapshot,
        production_transcription_endpoint(ai_context, false),
    )
}

pub fn production_transcription_endpoint(
    context: &AiContext,
    translate: bool,
) -> TranscriptionEndpoint<'static> {
    let capability = if translate {
        AiCapability::AudioTranslate
    } else {
        AiCapability::AudioTranscribe
    };
    let route = resolved_transcription_route(context, capability);
    if translate {
        let transcribe_route = resolved_transcription_route(context, AiCapability::AudioTranscribe);
        let text_route = resolved_transcription_route(context, AiCapability::TextGenerate);
        if route_available(route)
            || (route_available(transcribe_route) && route_available(text_route))
        {
            available_production_transcription_endpoint(context, route, translate)
        } else {
            TranscriptionEndpoint::Unavailable(transcription_degradation(route, translate))
        }
    } else if matches!(route, AiRouting::Daemon | AiRouting::Direct) {
        available_production_transcription_endpoint(context, route, translate)
    } else {
        TranscriptionEndpoint::Unavailable(transcription_degradation(route, translate))
    }
}

fn route_available(route: AiRouting) -> bool {
    matches!(route, AiRouting::Daemon | AiRouting::Direct)
}

#[cfg(feature = "ai")]
fn resolved_transcription_route(context: &AiContext, capability: AiCapability) -> AiRouting {
    gobby_core::ai::effective_route(context, capability)
}

#[cfg(not(feature = "ai"))]
fn resolved_transcription_route(context: &AiContext, capability: AiCapability) -> AiRouting {
    context.binding(capability).routing
}

#[cfg(feature = "ai")]
fn available_production_transcription_endpoint(
    context: &AiContext,
    _route: AiRouting,
    translate: bool,
) -> TranscriptionEndpoint<'static> {
    let client = Box::new(ProductionTranscriptionClient::new(context.clone()));
    if translate {
        TranscriptionEndpoint::Translating {
            client,
            target_lang: context
                .binding(AiCapability::AudioTranslate)
                .target_lang
                .clone(),
            language_hint: context
                .binding(AiCapability::AudioTranscribe)
                .language
                .clone(),
        }
    } else {
        TranscriptionEndpoint::Available(client)
    }
}

#[cfg(not(feature = "ai"))]
fn available_production_transcription_endpoint(
    _context: &AiContext,
    route: AiRouting,
    translate: bool,
) -> TranscriptionEndpoint<'static> {
    TranscriptionEndpoint::Unavailable(transcription_degradation(route, translate))
}

pub fn ingest_audio_with_transcription(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    endpoint: TranscriptionEndpoint<'_>,
) -> Result<AudioIngestResult, WikiError> {
    let result =
        ingest_audio_with_transcription_without_index(vault_root, scope, snapshot, endpoint)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_audio_with_transcription_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    endpoint: TranscriptionEndpoint<'_>,
) -> Result<AudioIngestResult, WikiError> {
    let title = markdown_title(&snapshot.file_name);
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let draft = SourceDraft {
        location: snapshot.location.clone(),
        kind: SourceKind::Audio,
        fetched_at: snapshot.fetched_at.clone(),
        content: Vec::new(),
        title: Some(title),
        citation: Some(snapshot.location.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register_with_content_hash(vault_root, draft, content_hash)?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    let raw_markdown = render_raw_audio_markdown(&snapshot, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let request = TranscriptionRequest {
        file_name: &snapshot.file_name,
        mime_type: snapshot.mime_type.as_deref(),
        asset_path: &asset_path,
        bytes: &snapshot.bytes,
    };
    let transcript = write_audio_transcript_markdown(
        vault_root,
        &scope,
        &record,
        request,
        transcribe_for_markdown(&request, endpoint),
    )?;

    Ok(AudioIngestResult {
        record,
        raw_path,
        asset_path,
        transcript_path: transcript.path,
        transcription_degradation: transcript.degradation,
    })
}

pub(crate) fn transcribe_for_markdown(
    request: &TranscriptionRequest<'_>,
    endpoint: TranscriptionEndpoint<'_>,
) -> TranscriptionMarkdownInput {
    match endpoint {
        TranscriptionEndpoint::Available(client) => {
            transcription_result_for_markdown(request, client.as_ref())
        }
        TranscriptionEndpoint::Unavailable(degradation) => {
            TranscriptionMarkdownInput::Degraded(degradation)
        }
        TranscriptionEndpoint::Translating {
            client,
            target_lang,
            language_hint,
        } => translate_for_markdown(
            request,
            client.as_ref(),
            target_lang.as_deref(),
            language_hint.as_deref(),
        ),
    }
}

fn transcription_result_for_markdown(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
) -> TranscriptionMarkdownInput {
    let result = transcribe_available(request, client);
    transcription_result_to_markdown(result, "transcription_error", "Transcription failed")
}

#[cfg(feature = "ai")]
fn transcribe_available(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
) -> Result<crate::transcribe::TranscriptionOutput, WikiError> {
    crate::ai::chunk::transcribe_audio_request(
        request,
        client,
        crate::ai::chunk::ChunkTranscriptionMode::Transcribe,
    )
}

#[cfg(not(feature = "ai"))]
fn transcribe_available(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
) -> Result<crate::transcribe::TranscriptionOutput, WikiError> {
    client.transcribe(request)
}

#[cfg(feature = "ai")]
fn translate_for_markdown(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
    target_lang: Option<&str>,
    language_hint: Option<&str>,
) -> TranscriptionMarkdownInput {
    let result = if crate::ai::chunk::requires_chunking(request.bytes.len()) {
        let target_lang = target_lang.unwrap_or("en");
        let mode = if is_english_target(target_lang) {
            crate::ai::chunk::ChunkTranscriptionMode::TranslateToEnglish { language_hint }
        } else {
            crate::ai::chunk::ChunkTranscriptionMode::TranslateSegments {
                target_lang,
                language_hint,
            }
        };
        crate::ai::chunk::transcribe_audio_request(request, client, mode)
    } else {
        crate::ai::translate::translate_audio(request, client, target_lang, language_hint)
    };
    transcription_result_to_markdown(result, "translation_error", "Translation failed")
}

#[cfg(not(feature = "ai"))]
fn translate_for_markdown(
    _request: &TranscriptionRequest<'_>,
    _client: &dyn crate::transcribe::TranscriptionClient,
    _target_lang: Option<&str>,
    _language_hint: Option<&str>,
) -> TranscriptionMarkdownInput {
    TranscriptionMarkdownInput::Degraded(TranscriptionDegradation {
        reason: "translation_unavailable".to_string(),
        fallback: "Translation requires the ai feature.".to_string(),
    })
}

fn transcription_result_to_markdown(
    result: Result<crate::transcribe::TranscriptionOutput, WikiError>,
    reason: &str,
    prefix: &str,
) -> TranscriptionMarkdownInput {
    match result {
        Ok(output) => TranscriptionMarkdownInput::Transcribed(output),
        Err(error) => TranscriptionMarkdownInput::Degraded(TranscriptionDegradation {
            reason: reason.to_string(),
            fallback: format!(
                "{prefix}: {error}; keep raw audio assets and require supplied transcripts."
            ),
        }),
    }
}

#[cfg(feature = "ai")]
fn is_english_target(target_lang: &str) -> bool {
    target_lang
        .trim()
        .split(['-', '_'])
        .next()
        .unwrap_or("")
        .eq_ignore_ascii_case("en")
}

fn transcription_degradation(routing: AiRouting, translate: bool) -> TranscriptionDegradation {
    let action = if translate {
        "translation"
    } else {
        "transcription"
    };
    let reason = match routing {
        AiRouting::Off => "disabled",
        AiRouting::Auto | AiRouting::Daemon | AiRouting::Direct => "missing_endpoint",
    };
    TranscriptionDegradation {
        reason: reason.to_string(),
        fallback: format!("Keep raw audio assets and skip daemon {action}."),
    }
}

impl From<AudioIngestResult> for IngestResult {
    fn from(result: AudioIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

fn render_raw_audio_markdown(
    snapshot: &AudioSnapshot,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "audio".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];
    if let Some(mime_type) = &snapshot.mime_type {
        fields.push(("audio_mime_type", mime_type.clone()));
    }
    if let Some(duration_seconds) = snapshot.duration_seconds {
        fields.push(("audio_duration_seconds", duration_seconds.to_string()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original audio stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "ai")]
    use std::cell::RefCell;
    #[cfg(feature = "ai")]
    use std::rc::Rc;

    use gobby_core::ai_context::{AiBindings, AiContext, AiLimiter};
    use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{MemoryWikiStore, WikiDocumentKind};
    use crate::transcribe::{
        TranscriptSegment, TranscriptionClient, TranscriptionOutput, TranscriptionRequest,
    };

    fn sample_snapshot() -> AudioSnapshot {
        AudioSnapshot {
            location: "/tmp/interview.wav".to_string(),
            file_name: "interview.wav".to_string(),
            fetched_at: "2026-05-29T21:15:00Z".to_string(),
            bytes: b"RIFF....WAVEaudio-bytes".to_vec(),
            mime_type: Some("audio/wav".to_string()),
            duration_seconds: Some(12),
        }
    }

    #[cfg(feature = "ai")]
    fn long_snapshot() -> AudioSnapshot {
        AudioSnapshot {
            bytes: vec![b'a'; crate::ai::chunk::MAX_AUDIO_UPLOAD_BYTES + 1],
            duration_seconds: Some(1_200),
            ..sample_snapshot()
        }
    }

    struct FakeTranscriptionClient;

    impl TranscriptionClient for FakeTranscriptionClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            Ok(TranscriptionOutput {
                segments: vec![TranscriptSegment {
                    start_ms: 2_000,
                    end_ms: 4_000,
                    text: "Scope searchable hydrophone transcript phrase.".to_string(),
                }],
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
            })
        }
    }

    #[cfg(feature = "ai")]
    struct ScriptedTranscriptionClient {
        transcriptions: RefCell<Vec<Result<TranscriptionOutput, WikiError>>>,
        english: RefCell<Vec<Result<TranscriptionOutput, WikiError>>>,
        translations: RefCell<Vec<Vec<String>>>,
        calls: Rc<RefCell<Vec<&'static str>>>,
    }

    #[cfg(feature = "ai")]
    impl ScriptedTranscriptionClient {
        fn new(transcriptions: Vec<TranscriptionOutput>) -> Self {
            Self {
                transcriptions: RefCell::new(transcriptions.into_iter().map(Ok).collect()),
                english: RefCell::new(Vec::new()),
                translations: RefCell::new(Vec::new()),
                calls: Rc::new(RefCell::new(Vec::new())),
            }
        }

        fn with_english(english: Vec<TranscriptionOutput>) -> Self {
            Self {
                transcriptions: RefCell::new(Vec::new()),
                english: RefCell::new(english.into_iter().map(Ok).collect()),
                translations: RefCell::new(Vec::new()),
                calls: Rc::new(RefCell::new(Vec::new())),
            }
        }

        fn calls(&self) -> Rc<RefCell<Vec<&'static str>>> {
            Rc::clone(&self.calls)
        }
    }

    #[cfg(feature = "ai")]
    impl TranscriptionClient for ScriptedTranscriptionClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.calls.borrow_mut().push("transcribe");
            self.transcriptions.borrow_mut().remove(0)
        }

        fn translate_to_english(
            &self,
            _request: &TranscriptionRequest<'_>,
            _language_hint: Option<&str>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.calls.borrow_mut().push("translate_to_english");
            self.english.borrow_mut().remove(0)
        }

        fn translate_segments(
            &self,
            segments: &[TranscriptSegment],
            _source_lang: &str,
            _target_lang: &str,
        ) -> Result<Vec<String>, WikiError> {
            self.calls.borrow_mut().push("translate_segments");
            let mut translations = self.translations.borrow_mut();
            if translations.is_empty() {
                return Ok(segments
                    .iter()
                    .map(|segment| format!("translated {}", segment.text))
                    .collect());
            }
            Ok(translations.remove(0))
        }
    }

    fn test_context(routing: AiRouting, api_base: Option<String>) -> AiContext {
        let binding = CapabilityBinding {
            routing,
            transport: None,
            api_base,
            api_key: None,
            model: Some("whisper-1".to_string()),
            provider: None,
            task: None,
            language: None,
            target_lang: None,
        };
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
            project_id: None,
        }
    }

    #[cfg(feature = "ai")]
    fn spawn_transcription_server(
        response: &'static str,
    ) -> (String, crate::test_http::RequestHandle) {
        crate::test_http::spawn_json_response(response).expect("spawn test server")
    }

    #[cfg(feature = "ai")]
    fn test_chunk(start_ms: u64, end_ms: u64) -> crate::ai::chunk::AudioChunk {
        crate::ai::chunk::AudioChunk {
            start_ms,
            end_ms,
            file_name: format!("chunk-{start_ms}.wav"),
            path: PathBuf::from(format!("chunk-{start_ms}.wav")),
            bytes: vec![b'w', b'a', b'v'],
        }
    }

    #[cfg(feature = "ai")]
    fn transcript_output(
        source_lang: &str,
        translated: bool,
        task: &str,
        segments: &[(u64, u64, &str)],
    ) -> TranscriptionOutput {
        TranscriptionOutput {
            segments: segments
                .iter()
                .map(|(start_ms, end_ms, text)| TranscriptSegment {
                    start_ms: *start_ms,
                    end_ms: *end_ms,
                    text: (*text).to_string(),
                })
                .collect(),
            language: Some(if translated { "en" } else { source_lang }.to_string()),
            model: Some("fake-stt".to_string()),
            source_language: Some(source_lang.to_string()),
            task: Some(task.to_string()),
            target_language: translated.then(|| "en".to_string()),
            translated,
            translation_degraded: false,
            partial: false,
            completed_ranges: Vec::new(),
            missing_ranges: Vec::new(),
        }
    }

    #[cfg(feature = "ai")]
    #[test]
    fn english_target_uses_primary_language_subtag() {
        assert!(is_english_target("en"));
        assert!(is_english_target("EN-us"));
        assert!(is_english_target("en_US"));
        assert!(!is_english_target("eng"));
        assert!(!is_english_target("fr-en"));
    }

    #[cfg(feature = "ai")]
    #[test]
    fn production_transcription_writes_fields() {
        let response = r#"{"text":"Production routed transcript.","source_language":"es","language":"en","model":"whisper-prod","task":"transcribe","translated":false,"segments":[{"start":0.0,"end":1.25,"text":"Production routed transcript."}]}"#;
        let (api_base, request) = spawn_transcription_server(response);
        let context = test_context(AiRouting::Direct, Some(api_base));
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            sample_snapshot(),
            &context,
        )
        .expect("ingest audio with production transcript");

        let request = request
            .join()
            .expect("transcription test server thread joins");
        let request = request.expect("transcription request was captured");
        assert!(request.starts_with("POST /v1/audio/transcriptions HTTP/1.1"));
        assert!(result.transcription_degradation.is_none());

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.transcript_path)).expect("markdown");
        assert!(markdown.contains("transcription_status: transcribed"));
        assert!(markdown.contains("transcription_language: en"));
        assert!(markdown.contains("transcription_source_language: es"));
        assert!(markdown.contains("transcription_model: whisper-prod"));
        assert!(markdown.contains("transcription_task: transcribe"));
        assert!(markdown.contains("translated: \"false\""));
        assert!(markdown.contains("[00:00:00] Production routed transcript."));
    }

    #[cfg(feature = "ai")]
    #[test]
    fn production_path_applies_translation() {
        let response = r#"{"text":"Translated transcript.","source_language":"es","language":"es","model":"whisper-prod","task":"translate","translated":true,"segments":[{"start":0.0,"end":1.25,"text":"Translated transcript."}]}"#;
        let (api_base, request) = spawn_transcription_server(response);
        let context = test_context(AiRouting::Direct, Some(api_base));
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio_with_transcription(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            sample_snapshot(),
            production_transcription_endpoint(&context, true),
        )
        .expect("ingest translated audio");

        let request = request
            .join()
            .expect("translation test server thread joins");
        let request = request.expect("translation request was captured");
        assert!(request.starts_with("POST /v1/audio/translations HTTP/1.1"));
        assert!(result.transcription_degradation.is_none());

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.transcript_path)).expect("markdown");
        assert!(markdown.contains("transcription_status: transcribed"));
        assert!(markdown.contains("transcription_language: en"));
        assert!(markdown.contains("transcription_source_language: es"));
        assert!(markdown.contains("transcription_target_language: en"));
        assert!(markdown.contains("transcription_task: translate"));
        assert!(markdown.contains("translated: \"true\""));
        assert!(markdown.contains("[00:00:00] Translated transcript."));
    }

    #[cfg(feature = "ai")]
    #[test]
    fn production_path_chunks_long_audio() {
        let _chunks = crate::ai::chunk::install_test_chunks(vec![
            test_chunk(0, 10_000),
            test_chunk(9_000, 19_000),
        ]);
        let client = ScriptedTranscriptionClient::new(vec![
            transcript_output("en", false, "transcribe", &[(0, 1_000, "first chunk")]),
            transcript_output("en", false, "transcribe", &[(0, 1_000, "second chunk")]),
        ]);
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio_with_transcription(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            long_snapshot(),
            TranscriptionEndpoint::Available(Box::new(client)),
        )
        .expect("ingest long audio");

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.transcript_path)).expect("markdown");
        assert!(markdown.contains("transcription_completed_ranges: 0-10000,9000-19000"));
        assert!(markdown.contains("[00:00:00] first chunk"));
        assert!(markdown.contains("[00:00:09] second chunk"));
    }

    #[cfg(feature = "ai")]
    #[test]
    fn long_media_chunks_then_translates() {
        let _chunks = crate::ai::chunk::install_test_chunks(vec![
            test_chunk(0, 10_000),
            test_chunk(9_000, 19_000),
        ]);
        let client = ScriptedTranscriptionClient::new(vec![
            transcript_output("es", false, "transcribe", &[(0, 1_000, "hola")]),
            transcript_output("es", false, "transcribe", &[(0, 1_000, "mundo")]),
        ]);
        client
            .translations
            .borrow_mut()
            .push(vec!["bonjour".to_string(), "monde".to_string()]);
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio_with_transcription(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            long_snapshot(),
            TranscriptionEndpoint::Translating {
                client: Box::new(client),
                target_lang: Some("fr".to_string()),
                language_hint: None,
            },
        )
        .expect("ingest translated long audio");

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.transcript_path)).expect("markdown");
        assert!(markdown.contains("transcription_source_language: es"));
        assert!(markdown.contains("transcription_target_language: fr"));
        assert!(markdown.contains("transcription_task: translate"));
        assert!(markdown.contains("translated: \"true\""));
        assert!(markdown.contains("[00:00:00] bonjour"));
        assert!(markdown.contains("[00:00:09] monde"));
    }

    #[cfg(feature = "ai")]
    #[test]
    fn long_english_translation_per_chunk() {
        let _chunks = crate::ai::chunk::install_test_chunks(vec![
            test_chunk(0, 10_000),
            test_chunk(9_000, 19_000),
        ]);
        let client = ScriptedTranscriptionClient::with_english(vec![
            transcript_output("es", true, "translate", &[(0, 1_000, "hello")]),
            transcript_output("es", true, "translate", &[(0, 1_000, "world")]),
        ]);
        let calls = client.calls();
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio_with_transcription(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            long_snapshot(),
            TranscriptionEndpoint::Translating {
                client: Box::new(client),
                target_lang: Some("en".to_string()),
                language_hint: Some("es".to_string()),
            },
        )
        .expect("ingest English translated long audio");

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.transcript_path)).expect("markdown");
        assert!(markdown.contains("transcription_source_language: es"));
        assert!(markdown.contains("transcription_target_language: en"));
        assert!(markdown.contains("transcription_task: translate"));
        assert!(markdown.contains("translated: \"true\""));
        assert!(markdown.contains("[00:00:00] hello"));
        assert!(markdown.contains("[00:00:09] world"));
        assert_eq!(
            calls.borrow().as_slice(),
            &["translate_to_english", "translate_to_english"]
        );
    }

    #[test]
    fn off_routing_degrades() {
        let context = test_context(AiRouting::Off, None);
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = sample_snapshot();
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            snapshot.clone(),
            &context,
        )
        .expect("ingest degraded audio");

        assert_eq!(
            std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
            snapshot.bytes
        );
        assert_eq!(
            result
                .transcription_degradation
                .as_ref()
                .map(|degradation| degradation.reason.as_str()),
            Some("disabled")
        );
        let markdown =
            std::fs::read_to_string(temp.path().join(&result.transcript_path)).expect("markdown");
        assert!(markdown.contains("transcription_status: unavailable"));
        assert!(markdown.contains("transcription_degradation: disabled"));
        assert!(markdown.contains("Keep raw audio assets"));
    }

    #[test]
    fn stores_original_audio() {
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = sample_snapshot();
        let expected_hash = content_hash(&snapshot.bytes);
        let mut store = MemoryWikiStore::default();
        let context = test_context(AiRouting::Off, None);

        let result = ingest_audio(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            snapshot.clone(),
            &context,
        )
        .expect("ingest audio");

        assert_eq!(
            result.asset_path.parent(),
            Some(PathBuf::from("raw/assets").as_path())
        );
        assert_eq!(
            std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
            snapshot.bytes
        );
        let raw =
            std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown");
        assert!(raw.contains("source_kind: audio"));
        assert!(raw.contains("source_asset: raw/assets/"));
        assert!(raw.contains("audio_mime_type: audio/wav"));
        assert!(raw.contains("audio_duration_seconds: \"12\""));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Audio);
        assert_eq!(manifest.entries[0].content_hash, expected_hash);
    }

    #[test]
    fn transcript_chunks_are_scope_searchable() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio_with_transcription(
            temp.path(),
            &mut store,
            ScopeIdentity::project("project-123"),
            sample_snapshot(),
            TranscriptionEndpoint::Available(Box::new(FakeTranscriptionClient)),
        )
        .expect("ingest audio with transcript");

        let document = store
            .documents
            .get(&result.transcript_path)
            .expect("transcript document indexed");
        assert_eq!(document.kind, WikiDocumentKind::SourceNote);
        assert!(document.body.contains("scope_kind: project"));
        assert!(document.body.contains("scope_id: project-123"));
        assert!(
            document
                .body
                .contains("Scope searchable hydrophone transcript phrase.")
        );
        assert!(store.sources.contains_key(&result.transcript_path));
        let chunks = store
            .chunks
            .get(&result.transcript_path)
            .expect("transcript chunks indexed");
        assert!(chunks.iter().any(|chunk| {
            chunk
                .content
                .contains("Scope searchable hydrophone transcript phrase.")
        }));
    }
}
