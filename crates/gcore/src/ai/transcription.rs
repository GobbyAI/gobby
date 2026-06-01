use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TranscriptionResult};
use crate::config::AiCapability;

use std::io::Cursor;

use bytes::Bytes;
use reqwest::blocking::{RequestBuilder, multipart};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TranscriptionTask {
    Transcribe,
    Translate,
}

impl TranscriptionTask {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Transcribe => "transcribe",
            Self::Translate => "translate",
        }
    }

    fn capability(self) -> AiCapability {
        match self {
            Self::Transcribe => AiCapability::AudioTranscribe,
            Self::Translate => AiCapability::AudioTranslate,
        }
    }

    fn endpoint_path(self) -> &'static str {
        match self {
            Self::Transcribe => "/v1/audio/transcriptions",
            Self::Translate => "/v1/audio/translations",
        }
    }
}

pub fn transcribe(
    cfg: &AiContext,
    bytes: Vec<u8>,
    file_name: &str,
    mime: &str,
    task: TranscriptionTask,
    language: Option<&str>,
) -> Result<TranscriptionResult, AiError> {
    let transport = super::AiTransport::new(cfg)?;
    let capability = task.capability();
    let url = endpoint_url(cfg, task)?;
    let bytes = Bytes::from(bytes);
    let file_name = file_name.to_string();
    let mime = mime.to_string();
    let language = language.map(str::to_string);
    let _permit = cfg.limiter.acquire();

    let value = super::retry_with_backoff(
        || {
            let request = build_request(
                &transport,
                capability,
                &url,
                bytes.clone(),
                &file_name,
                &mime,
                language.as_deref(),
            )?;
            super::parse_json_response(request.send().map_err(super::reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    transport.parse_transcription(value)
}

fn endpoint_url(cfg: &AiContext, task: TranscriptionTask) -> Result<String, AiError> {
    let capability = task.capability();
    let binding = cfg.binding(capability);
    let api_base = binding
        .api_base
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            AiError::not_configured(
                Some(capability.as_str().to_string()),
                format!(
                    "{}.api_base is required for direct audio {}",
                    capability.namespace(),
                    task.as_str()
                ),
            )
        })?;

    Ok(format!(
        "{}{}",
        api_base.trim_end_matches('/'),
        task.endpoint_path()
    ))
}

fn build_request(
    transport: &super::AiTransport<'_>,
    capability: AiCapability,
    url: &str,
    bytes: Bytes,
    file_name: &str,
    mime: &str,
    language: Option<&str>,
) -> Result<RequestBuilder, AiError> {
    let binding = transport.context.binding(capability);
    let file_len = u64::try_from(bytes.len()).map_err(|_| {
        AiError::parse_failure("transcription payload is too large to send".to_string())
    })?;
    let file_part = multipart::Part::reader_with_length(Cursor::new(bytes), file_len)
        .file_name(file_name.to_string())
        .mime_str(mime)
        .map_err(|error| {
            AiError::parse_failure(format!("invalid transcription MIME type {mime}: {error}"))
        })?;
    let mut form = multipart::Form::new()
        .part("file", file_part)
        .text("response_format", "verbose_json");

    if let Some(model) = binding.model.as_deref().filter(|value| !value.is_empty()) {
        form = form.text("model", model.to_string());
    }
    if let Some(language) = language
        .or(binding.language.as_deref())
        .filter(|value| !value.is_empty())
    {
        form = form.text("language", language.to_string());
    }

    Ok(super::apply_api_key(
        transport
            .client
            .post(url)
            .timeout(super::timeout_for(capability))
            .multipart(form),
        binding,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_context::{AiBindings, AiLimiter};
    use crate::config::{AiRouting, AiTuning, CapabilityBinding};
    use std::io::{ErrorKind, Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    #[test]
    fn builds_multipart_and_parses_segments() {
        let response = r#"{"text":"hello world","language":"es","model":"whisper-1","task":"translate","segments":[{"start":0.125,"end":1.5,"text":"hello"},{"start":1.5,"end":2.0,"text":" world"}]}"#;
        let (api_base, request) = spawn_server(response);
        let cfg = test_context(&api_base, None);

        let result = transcribe(
            &cfg,
            b"hola mundo".to_vec(),
            "clip.webm",
            "audio/webm",
            TranscriptionTask::Translate,
            Some("es"),
        )
        .unwrap();
        let request = request.join().unwrap();

        assert!(request.starts_with("POST /v1/audio/translations HTTP/1.1"));
        assert!(request.contains("filename=\"clip.webm\""));
        assert!(request.contains("name=\"response_format\"\r\n\r\nverbose_json"));
        assert!(request.contains("name=\"language\"\r\n\r\nes"));
        assert!(request.contains("name=\"model\"\r\n\r\nwhisper-1"));
        assert_eq!(result.language.as_deref(), Some("es"));
        assert_eq!(result.segments[0].start_ms, 125);
        assert_eq!(result.segments[0].end_ms, 1500);
        assert_eq!(result.segments[1].start_ms, 1500);
        assert_eq!(result.segments[1].end_ms, 2000);
    }

    #[test]
    fn wire_multipart_filename_and_auth() {
        let response = r#"{"text":"hello","language":"en","segments":[]}"#;
        let (api_base, request) = spawn_server(response);
        let cfg = test_context(&api_base, Some("test-token"));

        transcribe(
            &cfg,
            b"audio bytes".to_vec(),
            "meeting.m4a",
            "audio/mp4",
            TranscriptionTask::Transcribe,
            None,
        )
        .unwrap();
        let request = request.join().unwrap();

        assert!(request.starts_with("POST /v1/audio/transcriptions HTTP/1.1"));
        assert!(has_header(&request, "authorization", "Bearer test-token"));
        assert!(request.contains("name=\"file\"; filename=\"meeting.m4a\""));
        assert!(request.contains("Content-Type: audio/mp4"));
    }

    fn spawn_server(response: &'static str) -> (String, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let api_base = format!("http://{}", listener.local_addr().unwrap());
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let request = read_http_request(&mut stream);
            write!(
                stream,
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                response.len(),
                response
            )
            .unwrap();
            request
        });
        (api_base, handle)
    }

    fn read_http_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = match stream.read(&mut chunk) {
                Ok(read) => read,
                Err(error)
                    if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) =>
                {
                    break;
                }
                Err(error) => panic!("read request: {error}"),
            };
            if read == 0 {
                break;
            }
            request.extend_from_slice(&chunk[..read]);

            if let Some(header_end) = find_header_end(&request) {
                let header = String::from_utf8_lossy(&request[..header_end]);
                if let Some(content_length) = content_length(&header) {
                    let body_len = request.len().saturating_sub(header_end + 4);
                    if body_len >= content_length {
                        break;
                    }
                }
            }
        }
        String::from_utf8(request).unwrap()
    }

    fn find_header_end(request: &[u8]) -> Option<usize> {
        request.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn content_length(header: &str) -> Option<usize> {
        header.lines().find_map(|line| {
            line.strip_prefix("content-length: ")
                .or_else(|| line.strip_prefix("Content-Length: "))
                .and_then(|value| value.trim().parse().ok())
        })
    }

    fn has_header(request: &str, name: &str, value: &str) -> bool {
        request.lines().any(|line| {
            let Some((header_name, header_value)) = line.split_once(':') else {
                return false;
            };
            header_name.eq_ignore_ascii_case(name) && header_value.trim() == value
        })
    }

    fn test_context(api_base: &str, api_key: Option<&str>) -> AiContext {
        let binding = binding(api_base, api_key);
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

    fn binding(api_base: &str, api_key: Option<&str>) -> CapabilityBinding {
        CapabilityBinding {
            routing: AiRouting::Direct,
            transport: None,
            api_base: Some(api_base.to_string()),
            api_key: api_key.map(str::to_string),
            model: Some("whisper-1".to_string()),
            provider: None,
            task: None,
            language: None,
            target_lang: None,
        }
    }
}
