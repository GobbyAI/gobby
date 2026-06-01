use std::collections::BTreeMap;

use base64::Engine as _;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, VisionResult};
use crate::config::{AiCapability, CapabilityBinding};

const VISION_PROMPT: &str = "Describe the image and extract legible text. Return compact JSON with keys description and ocr_text. Use null for ocr_text when no text is visible.";

#[derive(Clone, Copy)]
enum Section {
    Description,
    Ocr,
}

pub fn describe_image(
    cfg: &AiContext,
    bytes: Vec<u8>,
    mime: &str,
) -> Result<VisionResult, AiError> {
    let capability = AiCapability::VisionExtract;
    let transport = super::AiTransport::new(cfg)?;
    let url = super::chat_completions_url(cfg, capability)?;
    let body = request_body(cfg.binding(capability), bytes, mime);
    let value = transport.post_json(capability, &url, &body)?;
    let content = super::chat_completion_content(&value)?;

    Ok(parse_content(
        &content,
        super::chat_completion_model(&value),
    ))
}

fn request_body(binding: &CapabilityBinding, bytes: Vec<u8>, mime: &str) -> Value {
    let encoded = base64::engine::general_purpose::STANDARD.encode(bytes);
    let data_uri = format!("data:{mime};base64,{encoded}");
    let mut body = json!({
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": VISION_PROMPT,
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": data_uri,
                    },
                },
            ],
        }],
    });

    if let Some(model) = binding.model.as_deref().filter(|value| !value.is_empty()) {
        body["model"] = Value::String(model.to_string());
    }

    body
}

fn parse_content(content: &str, model: Option<String>) -> VisionResult {
    if let Some((description, ocr_text)) = parse_json_content(content) {
        return VisionResult {
            description,
            ocr_text,
            model,
            metadata: BTreeMap::new(),
        };
    }

    if let Some((description, ocr_text)) = parse_delimited_content(content) {
        return VisionResult {
            description,
            ocr_text,
            model,
            metadata: BTreeMap::new(),
        };
    }

    VisionResult {
        description: content.trim().to_string(),
        ocr_text: None,
        model,
        metadata: BTreeMap::new(),
    }
}

fn parse_json_content(content: &str) -> Option<(String, Option<String>)> {
    #[derive(Deserialize)]
    struct VisionContent {
        description: Option<String>,
        #[serde(default, alias = "ocr", alias = "ocrText")]
        ocr_text: Option<String>,
    }

    let parsed = serde_json::from_str::<VisionContent>(strip_json_fence(content)).ok()?;
    let description = parsed.description?.trim().to_string();
    if description.is_empty() {
        return None;
    }

    Some((description, clean_optional_text(parsed.ocr_text)))
}

fn strip_json_fence(content: &str) -> &str {
    let trimmed = content.trim();
    let Some(without_opening) = trimmed.strip_prefix("```") else {
        return trimmed;
    };
    let without_language = without_opening
        .strip_prefix("json")
        .or_else(|| without_opening.strip_prefix("JSON"))
        .unwrap_or(without_opening)
        .trim_start_matches(['\r', '\n']);

    without_language
        .strip_suffix("```")
        .map(str::trim)
        .unwrap_or(trimmed)
}

fn parse_delimited_content(content: &str) -> Option<(String, Option<String>)> {
    let mut section = None;
    let mut description = Vec::new();
    let mut ocr_text = Vec::new();

    for line in content.lines() {
        if let Some((next_section, remainder)) = parse_section_label(line) {
            section = Some(next_section);
            if !remainder.trim().is_empty() {
                match next_section {
                    Section::Description => description.push(remainder.trim()),
                    Section::Ocr => ocr_text.push(remainder.trim()),
                }
            }
            continue;
        }

        match section {
            Some(Section::Description) => description.push(line),
            Some(Section::Ocr) => ocr_text.push(line),
            None => {}
        }
    }

    let description = description.join("\n").trim().to_string();
    if description.is_empty() {
        return None;
    }

    Some((
        description,
        clean_optional_text(Some(ocr_text.join("\n").trim().to_string())),
    ))
}

fn parse_section_label(line: &str) -> Option<(Section, &str)> {
    let trimmed = line.trim_start();
    let (label, remainder) = trimmed.split_once(':')?;
    let normalized = label
        .trim()
        .trim_start_matches('#')
        .trim()
        .to_ascii_lowercase()
        .replace(' ', "_");

    match normalized.as_str() {
        "description" => Some((Section::Description, remainder)),
        "ocr" | "ocr_text" => Some((Section::Ocr, remainder)),
        _ => None,
    }
}

fn clean_optional_text(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty() && !value.eq_ignore_ascii_case("null"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_context::{AiBindings, AiLimiter};
    use crate::config::{AiRouting, AiTuning, CapabilityBinding};
    use serde_json::Value;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    #[test]
    fn sends_image_url_and_parses() {
        let response = r#"{"model":"gpt-4.1-mini","choices":[{"message":{"content":"{\"description\":\"A receipt on a desk\",\"ocr_text\":\"TOTAL $12\"}"}}]}"#;
        let (api_base, request) = spawn_server(response);
        let cfg = test_context(&api_base, Some("vision-token"));

        let result = describe_image(&cfg, b"image".to_vec(), "image/png").unwrap();
        let request = request.join().unwrap();
        let body = request_body_json(&request);
        let content = body["messages"][0]["content"].as_array().unwrap();
        let image_url = content
            .iter()
            .find(|part| part["type"] == "image_url")
            .unwrap();

        assert!(request.starts_with("POST /v1/chat/completions HTTP/1.1"));
        assert!(has_header(&request, "authorization", "Bearer vision-token"));
        assert_eq!(body["model"], "gpt-4.1-mini");
        assert_eq!(
            image_url["image_url"]["url"],
            "data:image/png;base64,aW1hZ2U="
        );
        assert_eq!(result.description, "A receipt on a desk");
        assert_eq!(result.ocr_text.as_deref(), Some("TOTAL $12"));
        assert_eq!(result.model.as_deref(), Some("gpt-4.1-mini"));

        let fallback = parse_content(
            "A plain prose description.",
            Some("fallback-model".to_string()),
        );
        assert_eq!(fallback.description, "A plain prose description.");
        assert_eq!(fallback.ocr_text, None);
        assert_eq!(fallback.model.as_deref(), Some("fallback-model"));
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
            let read = stream.read(&mut chunk).unwrap();
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
