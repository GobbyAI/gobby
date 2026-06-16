use std::path::Path;

use gobby_core::ai_context::AiContext;
use gobby_core::config::EnvOnlySource;
use gobby_core::indexing::content_hash;

use super::source::{detect_source_kind, source_location};
use super::*;
use crate::api::IngestFileOptions;
use crate::sources::{SourceKind, SourceManifest};
use crate::store::MemoryWikiStore;

fn no_ai_context() -> AiContext {
    let mut source = EnvOnlySource;
    let mut context = AiContext::resolve(None, &mut source);
    IngestFileOptions {
        no_ai: true,
        ..IngestFileOptions::default()
    }
    .apply_to_ai_context(&mut context);
    context
}

fn ingest_options() -> IngestFileOptions {
    IngestFileOptions {
        no_ai: true,
        video_frame_interval_seconds: Some(0),
        ..IngestFileOptions::default()
    }
}

#[test]
fn source_location_preserves_external_canonical_path() {
    let vault = tempfile::tempdir().expect("vault tempdir");
    let outside = tempfile::tempdir().expect("outside tempdir");
    let source = outside.path().join("source.md");
    std::fs::write(&source, "# Source\n").expect("write outside source");

    let location = source_location(vault.path(), &source);

    assert_eq!(
        location,
        source
            .canonicalize()
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/")
    );
}

#[test]
fn file_and_stdin_ingest_hash_sources() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_bytes = b"# Field Notes\n\nLocal markdown source.\n";
    let file_path = temp.path().join("field-notes.md");
    std::fs::write(&file_path, file_bytes).expect("write local file");
    let stdin_bytes = b"stdin source text\n".to_vec();
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let file_result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-29T16:45:00Z",
    )
    .expect("ingest local file");
    let stdin_result = ingest_stdin(
        temp.path(),
        &mut store,
        StdinSnapshot {
            label: "gwiki-stdin".to_string(),
            fetched_at: "2026-05-29T16:46:00Z".to_string(),
            bytes: stdin_bytes.clone(),
        },
    )
    .expect("ingest stdin");

    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 2);
    let markdown_entry = manifest
        .entries
        .iter()
        .find(|entry| entry.kind == SourceKind::Markdown)
        .expect("markdown source kind");
    let stdin_entry = manifest
        .entries
        .iter()
        .find(|entry| entry.kind == SourceKind::Stdin)
        .expect("stdin source kind");
    assert_eq!(markdown_entry.content_hash, content_hash(file_bytes));
    assert_eq!(stdin_entry.content_hash, content_hash(&stdin_bytes));

    let raw_file =
        std::fs::read_to_string(temp.path().join(file_result.raw_path)).expect("file raw markdown");
    assert!(raw_file.contains("# Field Notes"));
    let raw_stdin = std::fs::read_to_string(temp.path().join(stdin_result.raw_path))
        .expect("stdin raw markdown");
    assert!(raw_stdin.contains("stdin source text"));
}

#[test]
fn common_audio_extensions_ingest_as_audio_assets() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("interview.mp3");
    std::fs::write(&file_path, b"audio bytes").expect("write local file");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-29T16:47:00Z",
    )
    .expect("ingest audio file");

    assert_eq!(result.record.kind, SourceKind::Audio);
    assert!(result.asset_path.is_some());
}

#[test]
fn detects_audio_and_image_extensions() {
    for extension in ["mp3", "wav", "m4a", "flac", "ogg", "aac", "opus"] {
        assert_eq!(
            detect_source_kind(Path::new(&format!("sample.{extension}"))),
            SourceKind::Audio
        );
    }

    for extension in ["png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff"] {
        assert_eq!(
            detect_source_kind(Path::new(&format!("sample.{extension}"))),
            SourceKind::Image
        );
    }
}

#[test]
fn dispatches_media_to_orchestrators() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    for (name, kind, expected_fragment) in [
        (
            "interview.mp3",
            SourceKind::Audio,
            "## Transcription Unavailable",
        ),
        ("diagram.png", SourceKind::Image, "## Vision Unavailable"),
        ("walkthrough.mp4", SourceKind::Video, "## Frame Samples"),
    ] {
        let path = temp.path().join(name);
        std::fs::write(&path, format!("{name} bytes")).expect("write media file");
        let mut store = MemoryWikiStore::default();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &path,
            "2026-05-29T16:48:00Z",
        )
        .expect("ingest media file");

        assert_eq!(result.record.kind, kind);
        assert!(result.asset_path.is_some());
        assert!(
            store
                .documents
                .values()
                .any(|document| document.body.contains(expected_fragment)),
            "{name} should be handled by its media orchestrator"
        );
    }
}

#[test]
fn no_ai_dispatch_degrades() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("diagram.png");
    std::fs::write(&file_path, b"image bytes").expect("write local file");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-29T16:49:00Z",
    )
    .expect("ingest image file");

    assert!(result.asset_path.is_some());
    assert!(
        store
            .documents
            .values()
            .any(|document| document.body.contains("## Vision Unavailable"))
    );
}

#[test]
fn media_dispatch_registers_once() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("interview.mp3");
    std::fs::write(&file_path, b"audio bytes").expect("write local file");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-29T16:50:00Z",
    )
    .expect("ingest audio file");

    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].kind, SourceKind::Audio);
}

#[test]
fn detects_documents_and_inlines_structured_text() {
    assert_eq!(detect_source_kind(Path::new("report.pdf")), SourceKind::Pdf);
    for extension in ["docx", "xlsx", "xls", "ods", "pptx"] {
        assert_eq!(
            detect_source_kind(Path::new(&format!("office.{extension}"))),
            SourceKind::Office
        );
    }
    for extension in ["html", "htm"] {
        assert_eq!(
            detect_source_kind(Path::new(&format!("page.{extension}"))),
            SourceKind::Html
        );
    }
    for extension in [
        "csv",
        "json",
        "xml",
        "yaml",
        "yml",
        "toml",
        "log",
        "ini",
        "env",
        "properties",
        "conf",
        "sql",
        "sh",
        "bash",
    ] {
        assert_eq!(
            detect_source_kind(Path::new(&format!("data.{extension}"))),
            SourceKind::Text
        );
    }
    assert_eq!(
        detect_source_kind(Path::new("session.jsonl")),
        SourceKind::Session
    );

    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();
    let mut store = MemoryWikiStore::default();

    let small_csv = temp.path().join("data.csv");
    std::fs::write(&small_csv, b"city,count\nDuluth,3\n").expect("write csv");
    let small_result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &small_csv,
        "2026-05-29T16:51:00Z",
    )
    .expect("ingest small csv");
    assert_eq!(small_result.record.kind, SourceKind::Text);
    assert!(small_result.asset_path.is_none());

    let large_json = temp.path().join("large.json");
    std::fs::write(&large_json, vec![b'a'; 262_145]).expect("write large json");
    let large_result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &large_json,
        "2026-05-29T16:52:00Z",
    )
    .expect("ingest large json");
    assert_eq!(large_result.record.kind, SourceKind::Text);
    assert!(large_result.asset_path.is_some());
}

#[test]
fn jsonl_session_archive_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("session.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"session","timestamp":"2026-06-16T20:00:00Z","payload":{"title":"Fixture session","messages":[{"role":"user","content":"Please summarize the trace."},{"role":"assistant","content":"The trace has two calls."}]}}"#,
    )
    .expect("write session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T20:01:00Z",
    )
    .expect("ingest session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("source_kind: session"));
    assert!(raw_markdown.contains("session_type: session"));
    assert!(raw_markdown.contains("# Fixture session"));
    assert!(raw_markdown.contains("### user"));
    assert!(raw_markdown.contains("Please summarize the trace."));
    assert!(raw_markdown.contains("### assistant"));
    assert!(raw_markdown.contains("The trace has two calls."));
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
    assert_eq!(manifest.entries[0].kind, SourceKind::Session);
}

#[test]
fn raw_claude_jsonl_session_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("claude.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"ai-title","timestamp":"2026-06-16T20:00:00Z","sessionId":"session-1","aiTitle":"Claude Import"}
{"type":"user","timestamp":"2026-06-16T20:00:01Z","sessionId":"session-1","message":{"role":"user","content":"Inspect the trace."}}
{"type":"assistant","timestamp":"2026-06-16T20:00:02Z","sessionId":"session-1","message":{"role":"assistant","content":[{"type":"text","text":"The trace has one call."},{"type":"tool_use","name":"Read","input":{"file_path":"trace.log"}}]}}
{"type":"user","timestamp":"2026-06-16T20:00:03Z","sessionId":"session-1","toolUseResult":{"file":"trace.log"},"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"call trace","is_error":false}]}}"#,
    )
    .expect("write Claude session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T20:04:00Z",
    )
    .expect("ingest raw Claude session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("session_type: claude-code"));
    assert!(raw_markdown.contains("# Claude Import"));
    assert!(raw_markdown.contains("### user"));
    assert!(raw_markdown.contains("Inspect the trace."));
    assert!(raw_markdown.contains("### assistant"));
    assert!(raw_markdown.contains("Tool use: Read"));
    assert!(raw_markdown.contains("### tool result"));
    assert!(raw_markdown.contains("call trace"));
}

#[test]
fn codex_jsonl_session_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("codex.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"session_meta","timestamp":"2026-06-16T20:00:00Z","payload":{"originator":"codex-tui","timestamp":"2026-06-16T20:00:00Z"}}
{"type":"event_msg","timestamp":"2026-06-16T20:00:01Z","payload":{"type":"user_message","message":"Trace the startup path."}}
{"type":"response_item","timestamp":"2026-06-16T20:00:02Z","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"I will trace it."}]}}
{"type":"response_item","timestamp":"2026-06-16T20:00:03Z","payload":{"type":"function_call","name":"exec_command","call_id":"call_1","arguments":"{\"cmd\":\"pwd\"}"}}
{"type":"response_item","timestamp":"2026-06-16T20:00:04Z","payload":{"type":"function_call_output","call_id":"call_1","output":"Output:\n/workspace\n"}}"#,
    )
    .expect("write Codex session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T20:05:00Z",
    )
    .expect("ingest Codex session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("session_type: codex-tui"));
    assert!(raw_markdown.contains("# Codex session"));
    assert!(raw_markdown.contains("Trace the startup path."));
    assert!(raw_markdown.contains("### assistant (commentary)"));
    assert!(raw_markdown.contains("I will trace it."));
    assert!(raw_markdown.contains("### tool call"));
    assert!(raw_markdown.contains("Function call: exec_command"));
    assert!(raw_markdown.contains("### tool result"));
    assert!(raw_markdown.contains("/workspace"));
}

#[test]
fn gemini_jsonl_session_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("gemini.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"init","timestamp":"2026-06-16T20:00:00Z","session_id":"session-1","model":"gemini-3.5-flash"}
{"type":"message","timestamp":"2026-06-16T20:00:01Z","role":"user","content":"Reply with exactly OK."}
{"type":"message","timestamp":"2026-06-16T20:00:02Z","role":"assistant","content":"OK","delta":true}
{"type":"result","timestamp":"2026-06-16T20:00:03Z","status":"success","stats":{"tool_calls":0}}"#,
    )
    .expect("write Gemini session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T20:06:00Z",
    )
    .expect("ingest Gemini session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("session_type: gemini-cli"));
    assert!(raw_markdown.contains("# Gemini CLI session"));
    assert!(raw_markdown.contains("Reply with exactly OK."));
    assert!(raw_markdown.contains("### assistant"));
    assert!(raw_markdown.contains("OK"));
}

#[test]
fn grok_jsonl_session_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("grok.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"user","content":[{"type":"text","text":"Run the command."}]}
{"type":"assistant","content":"","model_id":"grok-build","tool_calls":[{"id":"call_1","name":"run_terminal_command","arguments":{"command":"echo hello"}}]}
{"type":"tool_result","tool_call_id":"call_1","content":"exit: 0\nhello\n"}
{"type":"assistant","content":"exit: 0\nhello","model_id":"grok-build"}"#,
    )
    .expect("write Grok session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T20:07:00Z",
    )
    .expect("ingest Grok session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("session_type: grok-cli"));
    assert!(raw_markdown.contains("# Grok session"));
    assert!(raw_markdown.contains("Run the command."));
    assert!(raw_markdown.contains("### tool call"));
    assert!(raw_markdown.contains("run_terminal_command"));
    assert!(raw_markdown.contains("### tool result"));
    assert!(raw_markdown.contains("hello"));
}

#[test]
fn droid_jsonl_session_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("droid.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"session_start","id":"session-1","title":"Droid Fixture","sessionTitle":"Droid Fixture","owner":"josh","version":2,"cwd":"/workspace","hostId":"host-1"}
{"type":"message","id":"context-1","timestamp":"2026-06-16T20:08:00Z","visibility":"llm_only","message":{"role":"user","content":[{"type":"text","text":"<system-reminder>hidden</system-reminder>"}]}}
{"type":"message","id":"user-1","timestamp":"2026-06-16T20:08:01Z","message":{"role":"user","content":[{"type":"text","text":"Reply with exactly OK."}]},"parentId":"context-1"}
{"type":"message","id":"assistant-1","timestamp":"2026-06-16T20:08:02Z","message":{"role":"assistant","content":[{"type":"text","text":"OK"}]},"parentId":"user-1"}"#,
    )
    .expect("write Droid session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T20:08:00Z",
    )
    .expect("ingest Droid session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("session_type: droid-cli"));
    assert!(raw_markdown.contains("# Droid Fixture"));
    assert!(raw_markdown.contains("Reply with exactly OK."));
    assert!(raw_markdown.contains("### assistant"));
    assert!(raw_markdown.contains("OK"));
    assert!(!raw_markdown.contains("system-reminder"));
}

#[test]
fn qwen_jsonl_session_routes_to_session_orchestrator() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("qwen.jsonl");
    std::fs::write(
        &file_path,
        r#"{"type":"user","uuid":"user-1","parentUuid":null,"sessionId":"session-1","timestamp":"2026-06-16T21:10:00Z","cwd":"/workspace","version":"0.18.0","message":{"role":"user","parts":[{"text":"Create cap_probe.txt."}]}}
{"type":"system","subtype":"ui_telemetry","uuid":"system-1","parentUuid":"user-1","sessionId":"session-1","timestamp":"2026-06-16T21:10:01Z","cwd":"/workspace","version":"0.18.0","systemPayload":{"hidden":"telemetry"}}
{"type":"assistant","uuid":"assistant-1","parentUuid":"system-1","sessionId":"session-1","timestamp":"2026-06-16T21:10:02Z","cwd":"/workspace","version":"0.18.0","model":"qwen/qwen3","message":{"role":"model","parts":[{"text":"private chain of thought","thought":true},{"text":"I will write the file."},{"functionCall":{"id":"call_1","name":"write_file","args":{"file_path":"cap_probe.txt","content":"hello"}}}]}}
{"type":"tool_result","uuid":"tool-result-1","parentUuid":"assistant-1","sessionId":"session-1","timestamp":"2026-06-16T21:10:03Z","cwd":"/workspace","version":"0.18.0","toolCallResult":{"callId":"call_1","status":"cancelled"},"message":{"role":"user","parts":[{"functionResponse":{"id":"call_1","name":"write_file","response":{"error":"[Operation Cancelled]"}}}]}}"#,
    )
    .expect("write Qwen session jsonl");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-06-16T21:10:00Z",
    )
    .expect("ingest Qwen session archive");

    assert_eq!(result.record.kind, SourceKind::Session);
    assert!(result.asset_path.is_none());
    let raw_markdown =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("session markdown");
    assert!(raw_markdown.contains("session_type: qwen-code"));
    assert!(raw_markdown.contains("# Qwen session"));
    assert!(raw_markdown.contains("Create cap_probe.txt."));
    assert!(raw_markdown.contains("Tool use: write_file: call_1"));
    assert!(raw_markdown.contains("Tool result: write_file: call_1"));
    assert!(!raw_markdown.contains("private chain of thought"));
    assert!(!raw_markdown.contains("telemetry"));
}

#[cfg(feature = "documents")]
#[test]
fn dispatches_office_html_to_document() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("page.html");
    std::fs::write(
        &file_path,
        b"<!doctype html><html><head><title>Dispatch Doc</title></head><body><main><p>Document dispatch body.</p></main></body></html>",
    )
    .expect("write html");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-31T20:01:00Z",
    )
    .expect("ingest html");

    assert_eq!(result.record.kind, SourceKind::Html);
    assert!(result.asset_path.is_some());
    assert!(
        store
            .documents
            .values()
            .any(|document| document.body.contains("Document dispatch body."))
    );
    let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
    assert_eq!(manifest.entries.len(), 1);
}

#[cfg(feature = "documents")]
#[test]
fn dispatches_pdf_to_combined_path() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("scan.pdf");
    std::fs::write(&file_path, b"%PDF-1.7\nsource bytes\n%%EOF\n").expect("write pdf");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-31T20:03:00Z",
    )
    .expect("ingest pdf");

    assert_eq!(result.record.kind, SourceKind::Pdf);
    assert!(result.asset_path.is_some());
    let raw = std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw source");
    assert!(raw.contains("source_kind: pdf"));
    assert!(raw.contains("page_count: "));
    assert!(raw.contains("vision_used: \"false\""));
}

#[cfg(not(feature = "documents"))]
#[test]
fn office_html_store_as_asset_without_documents_feature() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("page.html");
    std::fs::write(&file_path, b"<html><body>stored only</body></html>").expect("write html");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-31T20:02:00Z",
    )
    .expect("ingest html without documents");

    assert_eq!(result.record.kind, SourceKind::Html);
    assert!(result.asset_path.is_some());
    let raw = std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw source");
    assert!(raw.contains("Original artifact stored under"));
}

#[cfg(not(feature = "documents"))]
#[test]
fn pdf_store_as_asset_without_documents_feature() {
    let temp = tempfile::tempdir().expect("tempdir");
    let file_path = temp.path().join("scan.pdf");
    std::fs::write(&file_path, b"%PDF-1.7\nsource bytes\n%%EOF\n").expect("write pdf");
    let mut store = MemoryWikiStore::default();
    let scope = ScopeIdentity::global();
    let ai_context = no_ai_context();
    let options = ingest_options();

    let result = ingest_path(
        temp.path(),
        &mut store,
        &scope,
        &ai_context,
        &options,
        &file_path,
        "2026-05-31T20:04:00Z",
    )
    .expect("ingest pdf without documents");

    assert_eq!(result.record.kind, SourceKind::Pdf);
    assert!(result.asset_path.is_some());
    let raw = std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw source");
    assert!(raw.contains("Original artifact stored under"));
}
