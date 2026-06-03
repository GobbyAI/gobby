use bytes::Bytes;
use reqwest::blocking::{Client, RequestBuilder, multipart};
use serde_json::{Map, Value};
use std::io::Cursor;

use crate::ai_context::AiContext;
use crate::ai_types::{
    AiError, TextResult, TranscriptionResult, TranscriptionSegment, VisionResult,
};
use crate::config::AiCapability;

const LOCAL_CLI_TOKEN_FILENAME: &str = "local_cli_token";
const LOCAL_TOKEN_HEADER: &str = "X-Gobby-Local-Token";
const VOICE_TRANSCRIBE_PATH: &str = "/api/voice/transcribe";
const VISION_EXTRACT_PATH: &str = "/api/llm/vision/extract";
const TEXT_GENERATE_PATH: &str = "/api/llm/generate";
const EMBEDDINGS_PATH: &str = "/api/embeddings";

#[derive(Debug, Clone, Copy)]
pub struct DaemonTranscriptionOptions<'a> {
    pub capability: AiCapability,
    pub language: Option<&'a str>,
    pub prompt: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DaemonEmbeddingResult {
    pub embeddings: Vec<Vec<f32>>,
    pub model: String,
    pub dim: usize,
}

impl Default for DaemonTranscriptionOptions<'_> {
    fn default() -> Self {
        Self {
            capability: AiCapability::AudioTranscribe,
            language: None,
            prompt: None,
        }
    }
}

pub fn transcribe_via_daemon(
    cfg: &AiContext,
    bytes: Vec<u8>,
    file_name: &str,
    mime: &str,
    options: DaemonTranscriptionOptions<'_>,
) -> Result<TranscriptionResult, AiError> {
    let capability = audio_capability(options.capability)?;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(VOICE_TRANSCRIBE_PATH)?;
    let file_name = file_name.to_string();
    let mime = mime.to_string();
    let language = options
        .language
        .or(binding.language.as_deref())
        .map(str::to_string);
    let prompt = options.prompt.map(str::to_string);
    let provider = binding.provider.clone();
    let model = binding.model.clone();
    let project_id = cfg.project_id.clone();
    let bytes = Bytes::from(bytes);
    let _permit = cfg.limiter.acquire();

    let value = super::retry_with_backoff(
        || {
            let form = multipart_form_with_file(bytes.clone(), &file_name, &mime, capability)?
                .text("capability", capability.as_str().to_string());
            let form = add_optional_text(form, "provider", provider.as_deref());
            let form = add_optional_text(form, "model", model.as_deref());
            let form = add_optional_text(form, "language", language.as_deref());
            let form = add_optional_text(form, "prompt", prompt.as_deref());
            let form = add_optional_text(form, "project_id", project_id.as_deref());
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::timeout_for(capability))
                    .multipart(form),
                &token,
            );
            super::parse_json_response(request.send().map_err(super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    parse_daemon_transcription(value)
}

pub fn describe_image_via_daemon(
    cfg: &AiContext,
    bytes: Vec<u8>,
    file_name: &str,
    mime: &str,
) -> Result<VisionResult, AiError> {
    let capability = AiCapability::VisionExtract;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(VISION_EXTRACT_PATH)?;
    let file_name = file_name.to_string();
    let mime = mime.to_string();
    let provider = binding.provider.clone();
    let model = binding.model.clone();
    let project_id = cfg.project_id.clone();
    let bytes = Bytes::from(bytes);
    let _permit = cfg.limiter.acquire();

    let value = super::retry_with_backoff(
        || {
            let form = multipart_form_with_file(bytes.clone(), &file_name, &mime, capability)?;
            let form = add_optional_text(form, "provider", provider.as_deref());
            let form = add_optional_text(form, "model", model.as_deref());
            let form = add_optional_text(form, "project_id", project_id.as_deref());
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::timeout_for(capability))
                    .multipart(form),
                &token,
            );
            super::parse_json_response(request.send().map_err(super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    VisionResult::from_wire_json(value)
}

pub fn generate_via_daemon(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
) -> Result<TextResult, AiError> {
    let capability = AiCapability::TextGenerate;
    let binding = cfg.binding(capability);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(TEXT_GENERATE_PATH)?;
    let body = text_request_body(
        prompt,
        system,
        binding.provider.as_deref(),
        binding.model.as_deref(),
        cfg.project_id.as_deref(),
    );
    let _permit = cfg.limiter.acquire();

    let value = super::retry_with_backoff(
        || {
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::timeout_for(capability))
                    .json(&body),
                &token,
            );
            super::parse_json_response(request.send().map_err(super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    TextResult::from_wire_json(value)
}

pub fn embed_via_daemon(
    cfg: &AiContext,
    input: &[String],
    is_query: bool,
) -> Result<DaemonEmbeddingResult, AiError> {
    let capability = AiCapability::Embed;
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let url = daemon_url(EMBEDDINGS_PATH)?;
    let body = embeddings_request_body(input, is_query);
    let _permit = cfg.limiter.acquire();

    let value = super::retry_with_backoff(
        || {
            let request = with_local_token(
                client
                    .post(&url)
                    .timeout(super::timeout_for(capability))
                    .json(&body),
                &token,
            );
            super::parse_json_response(request.send().map_err(super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    parse_daemon_embeddings(value, input.len())
}

fn audio_capability(capability: AiCapability) -> Result<AiCapability, AiError> {
    match capability {
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => Ok(capability),
        other => Err(AiError::capability_unavailable(
            other.as_str(),
            "daemon voice transcription supports audio_transcribe and audio_translate",
        )),
    }
}

fn daemon_client() -> Result<Client, AiError> {
    Client::builder().build().map_err(super::reqwest_error)
}

fn daemon_url(path: &str) -> Result<String, AiError> {
    let bootstrap_path = gobby_home()?.join("bootstrap.yaml");
    Ok(format!(
        "{}{}",
        crate::daemon_url::daemon_url_at(&bootstrap_path).trim_end_matches('/'),
        path
    ))
}

fn read_local_cli_token() -> Result<String, AiError> {
    let path = gobby_home()?.join(LOCAL_CLI_TOKEN_FILENAME);
    let token = std::fs::read_to_string(&path).map_err(|error| {
        AiError::not_configured(
            None,
            format!("missing local CLI token at {}: {}", path.display(), error),
        )
    })?;
    let token = token.trim().to_string();
    if token.is_empty() {
        return Err(AiError::not_configured(
            None,
            format!("local CLI token at {} is empty", path.display()),
        ));
    }
    Ok(token)
}

fn gobby_home() -> Result<std::path::PathBuf, AiError> {
    crate::gobby_home().map_err(|error| AiError::not_configured(None, error.to_string()))
}

fn with_local_token(request: RequestBuilder, token: &str) -> RequestBuilder {
    request.header(LOCAL_TOKEN_HEADER, token)
}

fn multipart_form_with_file(
    bytes: Bytes,
    file_name: &str,
    mime: &str,
    capability: AiCapability,
) -> Result<multipart::Form, AiError> {
    let file_len = u64::try_from(bytes.len()).map_err(|_| {
        AiError::parse_failure("daemon multipart payload length exceeds this platform's u64 sizing")
    })?;
    let file_part = multipart::Part::reader_with_length(Cursor::new(bytes), file_len)
        .file_name(file_name.to_string())
        .mime_str(mime)
        .map_err(|error| {
            AiError::parse_failure(format!(
                "invalid {} MIME type {mime}: {error}",
                capability.as_str()
            ))
        })?;

    Ok(multipart::Form::new().part("file", file_part))
}

fn add_optional_text(
    form: multipart::Form,
    name: &'static str,
    value: Option<&str>,
) -> multipart::Form {
    match non_empty(value) {
        Some(value) => form.text(name, value.to_string()),
        None => form,
    }
}

fn text_request_body(
    prompt: &str,
    system: Option<&str>,
    provider: Option<&str>,
    model: Option<&str>,
    project_id: Option<&str>,
) -> Value {
    let mut body = Map::new();
    body.insert("prompt".to_string(), Value::String(prompt.to_string()));
    insert_optional(&mut body, "system", system);
    insert_optional(&mut body, "provider", provider);
    insert_optional(&mut body, "model", model);
    insert_optional(&mut body, "project_id", project_id);
    Value::Object(body)
}

fn embeddings_request_body(input: &[String], is_query: bool) -> Value {
    let mut body = Map::new();
    body.insert(
        "input".to_string(),
        Value::Array(input.iter().cloned().map(Value::String).collect()),
    );
    body.insert("is_query".to_string(), Value::Bool(is_query));
    Value::Object(body)
}

fn insert_optional(body: &mut Map<String, Value>, name: &str, value: Option<&str>) {
    if let Some(value) = non_empty(value) {
        body.insert(name.to_string(), Value::String(value.to_string()));
    }
}

fn non_empty(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|value| !value.is_empty())
}

fn parse_daemon_transcription(value: Value) -> Result<TranscriptionResult, AiError> {
    if let Some(text) = legacy_text_only(&value) {
        return Ok(TranscriptionResult {
            text: text.clone(),
            segments: vec![TranscriptionSegment {
                start_ms: 0,
                end_ms: 0,
                text,
            }],
            source_language: None,
            language: None,
            model: None,
            task: None,
            target_language: None,
            translated: false,
        });
    }

    TranscriptionResult::from_wire_json(value)
}

fn legacy_text_only(value: &Value) -> Option<String> {
    let object = value.as_object()?;
    if object.contains_key("segments") {
        return None;
    }
    object
        .get("text")
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn parse_daemon_embeddings(
    value: Value,
    expected_len: usize,
) -> Result<DaemonEmbeddingResult, AiError> {
    let model = value
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| AiError::parse_failure("daemon embedding response missing model"))?
        .to_string();
    let dim = value
        .get("dim")
        .and_then(Value::as_u64)
        .and_then(|dim| usize::try_from(dim).ok())
        .ok_or_else(|| AiError::parse_failure("daemon embedding response missing dim"))?;
    let embeddings = value
        .get("embeddings")
        .and_then(Value::as_array)
        .ok_or_else(|| AiError::parse_failure("daemon embedding response missing embeddings"))?;
    if embeddings.len() != expected_len {
        return Err(AiError::parse_failure(format!(
            "daemon embedding response returned {} vector(s) for {} input(s)",
            embeddings.len(),
            expected_len
        )));
    }

    let embeddings = embeddings
        .iter()
        .map(|embedding| parse_daemon_embedding(embedding, dim))
        .collect::<Result<Vec<_>, _>>()?;

    Ok(DaemonEmbeddingResult {
        embeddings,
        model,
        dim,
    })
}

fn parse_daemon_embedding(value: &Value, dim: usize) -> Result<Vec<f32>, AiError> {
    let vector = value
        .as_array()
        .ok_or_else(|| AiError::parse_failure("daemon embedding item is not an array"))?
        .iter()
        .map(|value| {
            value.as_f64().map(|value| value as f32).ok_or_else(|| {
                AiError::parse_failure("daemon embedding array contains a non-number")
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    if vector.len() != dim {
        return Err(AiError::parse_failure(format!(
            "daemon embedding returned {} dimension(s), expected {}",
            vector.len(),
            dim
        )));
    }
    Ok(vector)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_context::{AiBindings, AiLimiter};
    use crate::config::{AiRouting, AiTuning, CapabilityBinding, TEST_ENV_LOCK};
    use crate::test_http::{RequestHandle, spawn_json_response};
    use std::ffi::OsString;
    use std::fs;
    use std::path::Path;
    use std::sync::MutexGuard;

    #[test]
    fn legacy_text_maps_to_single_segment() {
        let response = r#"{"text":"legacy transcript"}"#;
        let (port, request) = spawn_server(response);
        let home = temp_home();
        let _env = EnvGuard::set_home(home.path());
        write_daemon_files(home.path(), port, "voice-token");
        let cfg = test_context(None);

        let result = transcribe_via_daemon(
            &cfg,
            b"audio".to_vec(),
            "clip.wav",
            "audio/wav",
            DaemonTranscriptionOptions::default(),
        )
        .unwrap();
        let _request = request.join().unwrap().unwrap();

        assert_eq!(result.text, "legacy transcript");
        assert_eq!(result.segments.len(), 1);
        assert_eq!(result.segments[0].start_ms, 0);
        assert_eq!(result.segments[0].end_ms, 0);
        assert_eq!(result.segments[0].text, "legacy transcript");
        assert!(result.source_language.is_none());
        assert!(result.model.is_none());
        assert!(result.task.is_none());
    }

    #[test]
    fn forwards_provider_model_and_optional_project_id() {
        let (port, request) = spawn_server(r#"{"text":"ok","model":"daemon-model"}"#);
        let home = temp_home();
        let _env = EnvGuard::set_home(home.path());
        write_daemon_files(home.path(), port, "text-token");
        let cfg = test_context(Some("project-123"));

        let result = generate_via_daemon(&cfg, "Write a title", Some("Be brief")).unwrap();
        let request = request.join().unwrap().unwrap();
        let body = request_body_json(&request);

        assert!(request.starts_with("POST /api/llm/generate HTTP/1.1"));
        assert_eq!(body["provider"], "daemon-provider");
        assert_eq!(body["model"], "daemon-model");
        assert_eq!(body["project_id"], "project-123");
        assert_eq!(body["prompt"], "Write a title");
        assert_eq!(body["system"], "Be brief");
        assert_eq!(result.text, "ok");

        let (port, request) = spawn_server(r#"{"text":"ok"}"#);
        write_daemon_files(home.path(), port, "text-token");
        let cfg = test_context(None);

        generate_via_daemon(&cfg, "No project", None).unwrap();
        let request = request.join().unwrap().unwrap();
        let body = request_body_json(&request);

        assert_eq!(body["provider"], "daemon-provider");
        assert_eq!(body["model"], "daemon-model");
        assert!(body.get("project_id").is_none());
    }

    #[test]
    fn embeddings_post_preserves_batch_and_local_token() {
        let (port, request) =
            spawn_server(r#"{"embeddings":[[0.1,0.2],[0.3,0.4]],"model":"embed-model","dim":2}"#);
        let home = temp_home();
        let _env = EnvGuard::set_home(home.path());
        write_daemon_files(home.path(), port, "embed-token");
        let cfg = test_context(Some("project-123"));
        let input = vec!["same".to_string(), "same".to_string()];

        let result = embed_via_daemon(&cfg, &input, true).unwrap();
        let request = request.join().unwrap().unwrap();
        let body = request_body_json(&request);

        assert!(request.starts_with("POST /api/embeddings HTTP/1.1"));
        assert!(has_header(&request, LOCAL_TOKEN_HEADER, "embed-token"));
        assert_eq!(body["input"], serde_json::json!(["same", "same"]));
        assert_eq!(body["is_query"], true);
        assert_eq!(result.model, "embed-model");
        assert_eq!(result.dim, 2);
        assert_eq!(result.embeddings, vec![vec![0.1, 0.2], vec![0.3, 0.4]]);
    }

    #[test]
    fn embedding_response_validates_count_and_dimension() {
        let wrong_count = parse_daemon_embeddings(
            serde_json::json!({
                "embeddings": [[0.1, 0.2]],
                "model": "embed-model",
                "dim": 2
            }),
            2,
        )
        .expect_err("count mismatch rejected");
        assert!(
            wrong_count
                .to_string()
                .contains("returned 1 vector(s) for 2 input(s)")
        );

        let wrong_dim = parse_daemon_embeddings(
            serde_json::json!({
                "embeddings": [[0.1]],
                "model": "embed-model",
                "dim": 2
            }),
            1,
        )
        .expect_err("dimension mismatch rejected");
        assert!(
            wrong_dim
                .to_string()
                .contains("returned 1 dimension(s), expected 2")
        );
    }

    #[test]
    fn empty_embedding_batch_parses_daemon_model_and_dim() {
        let (port, request) = spawn_server(r#"{"embeddings":[],"model":"embed-model","dim":768}"#);
        let home = temp_home();
        let _env = EnvGuard::set_home(home.path());
        write_daemon_files(home.path(), port, "embed-token");
        let cfg = test_context(Some("project-123"));

        let result = embed_via_daemon(&cfg, &[], false).unwrap();
        let request = request.join().unwrap().unwrap();
        let body = request_body_json(&request);

        assert_eq!(body["input"], serde_json::json!([]));
        assert_eq!(result.model, "embed-model");
        assert_eq!(result.dim, 768);
        assert!(result.embeddings.is_empty());
    }

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

    fn spawn_server(response: &'static str) -> (u16, RequestHandle) {
        let (api_base, handle) = spawn_json_response(response).expect("spawn test server");
        let port = api_base
            .rsplit(':')
            .next()
            .expect("server port")
            .parse()
            .expect("numeric server port");
        (port, handle)
    }

    fn request_body_json(request: &str) -> serde_json::Value {
        let (_, body) = request.split_once("\r\n\r\n").unwrap();
        serde_json::from_str(body).unwrap()
    }

    fn has_header(request: &str, name: &str, value: &str) -> bool {
        request.lines().any(|line| {
            let Some((header_name, header_value)) = line.split_once(':') else {
                return false;
            };
            header_name.eq_ignore_ascii_case(name) && header_value.trim() == value
        })
    }

    fn multipart_has_field(request: &str, name: &str, value: &str) -> bool {
        request.contains(&format!("name=\"{name}\"\r\n\r\n{value}"))
    }

    fn temp_home() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    fn write_daemon_files(home: &Path, port: u16, token: &str) {
        let gobby_home = home.join(".gobby");
        fs::create_dir_all(&gobby_home).unwrap();
        fs::write(
            gobby_home.join("bootstrap.yaml"),
            format!("daemon_port: {port}\nbind_host: 127.0.0.1\n"),
        )
        .unwrap();
        fs::write(gobby_home.join("local_cli_token"), format!("{token}\n")).unwrap();
    }

    fn test_context(project_id: Option<&str>) -> AiContext {
        let binding = binding();
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
            project_id: project_id.map(str::to_string),
        }
    }

    fn binding() -> CapabilityBinding {
        CapabilityBinding {
            routing: AiRouting::Daemon,
            transport: None,
            api_base: None,
            api_key: None,
            model: Some("daemon-model".to_string()),
            provider: Some("daemon-provider".to_string()),
            task: None,
            language: None,
            target_lang: None,
        }
    }

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
        home: Option<OsString>,
        gobby_home: Option<OsString>,
    }

    impl EnvGuard {
        fn set_home(home: &Path) -> Self {
            let guard = Self {
                _lock: TEST_ENV_LOCK
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()),
                home: std::env::var_os("HOME"),
                gobby_home: std::env::var_os("GOBBY_HOME"),
            };
            // SAFETY: these tests serialize HOME/GOBBY_HOME mutation through
            // TEST_ENV_LOCK, and EnvGuard restores the original values while
            // still holding that lock.
            unsafe {
                std::env::set_var("HOME", home);
                std::env::set_var("GOBBY_HOME", home.join(".gobby"));
            }
            guard
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            // SAFETY: EnvGuard owns the TEST_ENV_LOCK guard for the lifetime of
            // the temporary HOME/GOBBY_HOME override, so restoration cannot race
            // with another test using this helper.
            unsafe {
                match &self.home {
                    Some(value) => std::env::set_var("HOME", value),
                    None => std::env::remove_var("HOME"),
                }
                match &self.gobby_home {
                    Some(value) => std::env::set_var("GOBBY_HOME", value),
                    None => std::env::remove_var("GOBBY_HOME"),
                }
            }
        }
    }
}
