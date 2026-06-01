use std::collections::BTreeMap;

use serde_json::{Value, json};

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult};
use crate::config::{AiCapability, CapabilityBinding};

pub fn generate_text(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
) -> Result<TextResult, AiError> {
    let capability = AiCapability::TextGenerate;
    let transport = super::AiTransport::new(cfg)?;
    let url = super::chat_completions_url(cfg, capability)?;
    let body = request_body(cfg.binding(capability), prompt, system);
    let value = transport.post_json(capability, &url, &body)?;

    Ok(TextResult {
        text: super::chat_completion_content(&value)?,
        model: super::chat_completion_model(&value),
        metadata: BTreeMap::new(),
    })
}

fn request_body(binding: &CapabilityBinding, prompt: &str, system: Option<&str>) -> Value {
    let mut messages = Vec::new();
    if let Some(system) = system.map(str::trim).filter(|value| !value.is_empty()) {
        messages.push(json!({
            "role": "system",
            "content": system,
        }));
    }
    messages.push(json!({
        "role": "user",
        "content": prompt,
    }));

    let mut body = json!({
        "messages": messages,
    });

    if let Some(model) = binding.model.as_deref().filter(|value| !value.is_empty()) {
        body["model"] = Value::String(model.to_string());
    }

    body
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_context::{AiBindings, AiLimiter};
    use crate::config::{AiRouting, AiTuning, CapabilityBinding};
    use serde_json::Value;
    use std::io::{ErrorKind, Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    #[test]
    fn generates_text() {
        let response =
            r#"{"model":"gpt-4.1-mini","choices":[{"message":{"content":"Generated answer"}}]}"#;
        let (api_base, request) = spawn_server(response);
        let cfg = test_context(&api_base, Some("text-token"));

        let result = generate_text(&cfg, "Write a caption", Some("Be direct")).unwrap();
        let request = request.join().unwrap();
        let body = request_body_json(&request);

        assert!(request.starts_with("POST /v1/chat/completions HTTP/1.1"));
        assert!(has_header(&request, "authorization", "Bearer text-token"));
        assert_eq!(body["model"], "gpt-4.1-mini");
        assert_eq!(body["messages"][0]["role"], "system");
        assert_eq!(body["messages"][0]["content"], "Be direct");
        assert_eq!(body["messages"][1]["role"], "user");
        assert_eq!(body["messages"][1]["content"], "Write a caption");
        assert_eq!(result.text, "Generated answer");
        assert_eq!(result.model.as_deref(), Some("gpt-4.1-mini"));
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

    fn request_body_json(request: &str) -> Value {
        let body = request.split("\r\n\r\n").nth(1).unwrap();
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
            model: Some("gpt-4.1-mini".to_string()),
            provider: None,
            task: None,
            language: None,
            target_lang: None,
        }
    }
}
