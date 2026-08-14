#[cfg(feature = "ai")]
use std::cell::RefCell;
#[cfg(feature = "ai")]
use std::rc::Rc;

use gobby_core::ai_context::{AiBindings, AiContext, AiLimiter};
use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};
use gobby_core::indexing::content_hash;

use super::*;
use crate::sources::{SourceKind, SourceManifest};
use crate::store::{FakeWikiStore, WikiDocumentKind};
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
        profile: None,
        candidates: None,
        reasoning_effort: None,
        verify_profile: None,
        verify_model: None,
        verify_api_key: None,
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
        grant: None,
        tool_loop_limits: gobby_core::ai::generation::ToolLoopLimits::default(),
    }
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
fn off_routing_skips_production_transcription() {
    let context = test_context(AiRouting::Off, None);
    let temp = tempfile::tempdir().expect("tempdir");
    let mut store = FakeWikiStore::default();

    let result = ingest_audio(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        sample_snapshot(),
        &context,
    )
    .expect("ingest audio with AI off");

    assert!(result.transcription_degradation.is_some());
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
    let mut store = FakeWikiStore::default();

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
    let mut store = FakeWikiStore::default();

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
    let mut store = FakeWikiStore::default();

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
    let mut store = FakeWikiStore::default();

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
    let mut store = FakeWikiStore::default();
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
    let raw = std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown");
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
fn raw_markdown_failure_removes_written_audio_asset() {
    let temp = tempfile::tempdir().expect("tempdir");
    let snapshot = sample_snapshot();
    let content_hash = content_hash(&snapshot.bytes);
    let record = SourceManifest::register_with_content_hash(
        temp.path(),
        SourceDraft::new(
            snapshot.location.clone(),
            SourceKind::Audio,
            snapshot.fetched_at.clone(),
            Vec::new(),
        )
        .with_title(markdown_title(&snapshot.file_name))
        .with_citation(snapshot.location.clone()),
        content_hash,
    )
    .expect("pre-register source");
    let raw_path =
        super::super::raw_markdown_relative_path(&record).expect("valid raw source path");
    std::fs::create_dir_all(temp.path().join(&raw_path)).expect("raw path blocker");
    let expected_asset_path = PathBuf::from("raw/assets").join(format!("{}.wav", record.id));
    let mut store = FakeWikiStore::default();
    let context = test_context(AiRouting::Off, None);

    let error = ingest_audio(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        snapshot,
        &context,
    )
    .expect_err("raw markdown blocker fails ingest");

    assert_eq!(error.code(), "io_error");
    assert!(
        !temp.path().join(expected_asset_path).exists(),
        "asset written before raw failure should be removed"
    );
}

#[test]
fn unchanged_audio_reingest_reuses_immutable_raw_capture() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut store = FakeWikiStore::default();
    let context = test_context(AiRouting::Off, None);

    let first = ingest_audio(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        sample_snapshot(),
        &context,
    )
    .expect("first ingest");

    let mut reingest = sample_snapshot();
    reingest.fetched_at = "2026-05-30T09:00:00Z".to_string();
    let second = ingest_audio(
        temp.path(),
        &mut store,
        ScopeIdentity::topic("field-work"),
        reingest,
        &context,
    )
    .expect("unchanged re-ingest");

    assert_eq!(second.record.id, first.record.id);
    assert_eq!(second.raw_path, first.raw_path);
    let raw = std::fs::read_to_string(temp.path().join(&second.raw_path)).expect("raw markdown");
    assert!(
        raw.contains("2026-05-29T21:15:00Z"),
        "first capture time kept"
    );
    assert!(
        !raw.contains("2026-05-30T09:00:00Z"),
        "re-ingest time not written"
    );
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
}

#[test]
fn transcript_chunks_are_scope_searchable() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut store = FakeWikiStore::default();

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
