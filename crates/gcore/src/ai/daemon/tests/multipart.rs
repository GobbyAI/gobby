use super::*;
use crate::config::AiCapability;

#[test]
fn sends_local_token_and_multipart() {
    let (port, request) = spawn_server(r#"{"description":"diagram","ocr_text":null}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "local-secret");
    let cfg = test_context(None);

    describe_image_via_daemon(&cfg, b"png bytes".to_vec(), "figure.png", "image/png").unwrap();
    let request = request.join().unwrap().unwrap();

    assert!(request.starts_with("POST /api/llm/vision/extract HTTP/1.1"));
    assert!(has_header(&request, "x-gobby-local-token", "local-secret"));
    assert!(request.contains("multipart/form-data"));
    assert!(request.contains("name=\"file\"; filename=\"figure.png\""));
    assert!(request.contains("Content-Type: image/png"));

    let (port, request) =
        spawn_server(r#"{"text":"hello","segments":[{"start":0.0,"end":1.0,"text":"hello"}]}"#);
    write_daemon_files(home.path(), port, "local-secret");

    transcribe_via_daemon(
        &cfg,
        b"audio bytes".to_vec(),
        "meeting.m4a",
        "audio/mp4",
        DaemonTranscriptionOptions::default(),
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();

    assert!(request.starts_with("POST /api/voice/transcribe HTTP/1.1"));
    assert!(has_header(&request, "x-gobby-local-token", "local-secret"));
    assert!(request.contains("multipart/form-data"));
    assert!(request.contains("name=\"file\"; filename=\"meeting.m4a\""));
    assert!(request.contains("Content-Type: audio/mp4"));
}

#[test]
fn voice_multipart_carries_capability_fields() {
    let (port, request) = spawn_server(r#"{"text":"hello","segments":[]}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "voice-token");
    let cfg = test_context(None);

    transcribe_via_daemon(
        &cfg,
        b"audio bytes".to_vec(),
        "meeting.wav",
        "audio/wav",
        DaemonTranscriptionOptions {
            capability: AiCapability::AudioTranslate,
            language: Some("es"),
            target_lang: Some("en"),
            prompt: Some("names: Gobby"),
        },
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();

    assert!(multipart_has_field(
        &request,
        "capability",
        "audio_translate"
    ));
    assert!(multipart_has_field(&request, "provider", "daemon-provider"));
    assert!(multipart_has_field(&request, "model", "daemon-model"));
    assert!(multipart_has_field(&request, "language", "es"));
    assert!(multipart_has_field(&request, "target_lang", "en"));
    assert!(multipart_has_field(&request, "prompt", "names: Gobby"));
    assert!(!multipart_has_field(&request, "capability", "translate"));

    let (port, request) = spawn_server(r#"{"text":"hello","segments":[]}"#);
    write_daemon_files(home.path(), port, "voice-token");

    transcribe_via_daemon(
        &cfg,
        b"audio bytes".to_vec(),
        "meeting.wav",
        "audio/wav",
        DaemonTranscriptionOptions::default(),
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();

    assert!(multipart_has_field(
        &request,
        "capability",
        "audio_transcribe"
    ));
    assert!(!multipart_has_field(&request, "capability", "transcribe"));
}
