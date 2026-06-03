use std::collections::BTreeMap;

use serde_json::{Value, json};

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult, TokenUsage};
use crate::config::{AiCapability, CapabilityBinding};

pub fn generate_text(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
) -> Result<TextResult, AiError> {
    generate_text_with_max_tokens(cfg, prompt, system, None)
}

pub fn generate_text_with_max_tokens(
    cfg: &AiContext,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
) -> Result<TextResult, AiError> {
    let capability = AiCapability::TextGenerate;
    let transport = super::AiTransport::new(cfg)?;
    let url = super::chat_completions_url(cfg, capability)?;
    let body = request_body(cfg.binding(capability), prompt, system, max_tokens);
    let value = transport.post_json(capability, &url, &body)?;

    Ok(TextResult {
        text: super::chat_completion_content(&value)?,
        model: super::chat_completion_model(&value),
        usage: chat_completion_usage(&value),
        metadata: BTreeMap::new(),
    })
}

fn request_body(
    binding: &CapabilityBinding,
    prompt: &str,
    system: Option<&str>,
    max_tokens: Option<usize>,
) -> Value {
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
    if let Some(max_tokens) = max_tokens.filter(|value| *value > 0) {
        body["max_tokens"] = Value::from(max_tokens);
    }

    body
}

fn chat_completion_usage(value: &Value) -> Option<TokenUsage> {
    let usage = value.get("usage")?;
    Some(TokenUsage {
        input_tokens: usage
            .get("prompt_tokens")
            .or_else(|| usage.get("input_tokens"))
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok()),
        output_tokens: usage
            .get("completion_tokens")
            .or_else(|| usage.get("output_tokens"))
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok()),
        total_tokens: usage
            .get("total_tokens")
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_context::{AiBindings, AiLimiter};
    use crate::config::{AiRouting, AiTuning, CapabilityBinding};
    use crate::test_http::{RequestHandle, spawn_json_response};
    use serde_json::Value;

    #[test]
    fn generates_text() {
        let response = r#"{"model":"gpt-4.1-mini","choices":[{"message":{"content":"Generated answer"}}],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}"#;
        let (api_base, request) = spawn_server(response);
        let cfg = test_context(&api_base, Some("text-token"));

        let result = generate_text(&cfg, "Write a caption", Some("Be direct")).unwrap();
        let request = request.join().unwrap().unwrap();
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
        assert_eq!(
            result.usage.as_ref().and_then(|usage| usage.token_count()),
            Some(18)
        );
    }

    #[test]
    fn forwards_generation_max_tokens() {
        let response =
            r#"{"model":"gpt-4.1-mini","choices":[{"message":{"content":"Generated answer"}}]}"#;
        let (api_base, request) = spawn_server(response);
        let cfg = test_context(&api_base, Some("text-token"));

        generate_text_with_max_tokens(&cfg, "Write a caption", None, Some(42)).unwrap();
        let request = request.join().unwrap().unwrap();
        let body = request_body_json(&request);

        assert_eq!(body["max_tokens"], 42);
    }

    fn spawn_server(response: &'static str) -> (String, RequestHandle) {
        spawn_json_response(response).expect("spawn test server")
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
