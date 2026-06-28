//! [`ChatTransport`] implementations over OpenAI-compatible chat completions
//! with tool calling, for both Lane B routes.
//!
//! * [`DirectChatTransport`] targets OpenAI-compatible local servers (LM Studio,
//!   vLLM, llama.cpp, and similar) that expose function/tool calling through the
//!   standard chat schema. It is provider-neutral: the model/api_base/api_key
//!   come from a resolved [`DirectGenerationTarget`], never a pinned vendor.
//! * [`DaemonChatTransport`] forwards the same OpenAI-shaped request to the
//!   daemon's `/api/llm/chat/completions` passthrough, sending a feature
//!   `profile` (the daemon owns tool-capable provider/model selection) under
//!   the local CLI token. No server-side loop or state.
//!
//! Both share the request serialization ([`message_to_json`]/[`tool_to_json`])
//! and the response parser ([`parse_completion`]), so the tool loop is blind to
//! which route produced a completion.

use reqwest::blocking::Client;
use reqwest::header::AUTHORIZATION;
use serde_json::{Map, Value, json};

use crate::ai::daemon::{daemon_client, daemon_url, read_local_cli_token, with_local_token};
use crate::ai::text::chat_completion_usage;
use crate::ai::{
    chat_api_root, chat_completion_model, parse_json_response, reqwest_error, retry_with_backoff,
    timeout_for,
};
use crate::ai_context::AiContext;
use crate::ai_types::AiError;
use crate::config::AiCapability;

use super::profile::DirectGenerationTarget;
use super::tool_loop::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport, ToolCall,
    ToolSchema,
};

/// Daemon tool-passthrough chat-completion path (#17393).
const DAEMON_CHAT_COMPLETIONS_PATH: &str = "/api/llm/chat/completions";

/// Direct OpenAI-compatible chat-completion transport with tool calling.
pub struct DirectChatTransport<'a> {
    context: &'a AiContext,
    target: DirectGenerationTarget,
    profile: Option<String>,
    client: Client,
}

impl<'a> DirectChatTransport<'a> {
    /// Build a transport for a resolved profile target. `profile` is the feature
    /// profile name the target was resolved from, recorded for observability.
    pub fn new(
        context: &'a AiContext,
        target: DirectGenerationTarget,
        profile: Option<String>,
    ) -> Result<Self, AiError> {
        let client = Client::builder().build().map_err(reqwest_error)?;
        Ok(Self {
            context,
            target,
            profile,
            client,
        })
    }

    fn url(&self) -> Result<String, AiError> {
        let api_base = self.target.api_base().ok_or_else(|| {
            AiError::not_configured(
                Some(AiCapability::TextGenerate.as_str().to_string()),
                "ai.text_generate profile api_base is required for direct tool-calling completions",
            )
        })?;
        Ok(format!("{}/v1/chat/completions", chat_api_root(api_base)))
    }
}

impl ChatTransport for DirectChatTransport<'_> {
    fn complete(&self, request: ChatCompletionRequest<'_>) -> Result<ChatCompletion, AiError> {
        let url = self.url()?;
        let body = build_request_body(&self.target, &request);
        let api_key = self
            .target
            .api_key
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string);

        let _permit = self.context.limiter.acquire();
        let value = retry_with_backoff(
            || {
                let mut http = self
                    .client
                    .post(&url)
                    .timeout(timeout_for(AiCapability::TextGenerate))
                    .json(&body);
                if let Some(api_key) = api_key.as_deref() {
                    http = http.header(AUTHORIZATION, format!("Bearer {api_key}"));
                }
                parse_json_response(http.send().map_err(reqwest_error)?)
            },
            std::thread::sleep,
        )?;

        parse_completion(&value)
    }

    fn route(&self) -> &'static str {
        "direct"
    }

    fn profile(&self) -> Option<&str> {
        self.profile.as_deref()
    }

    fn provider(&self) -> Option<&str> {
        self.target.provider.as_deref()
    }

    fn model(&self) -> Option<&str> {
        self.target.model.as_deref()
    }
}

/// Daemon-route [`ChatTransport`]: forwards an OpenAI-shaped chat completion to
/// the daemon's `/api/llm/chat/completions` passthrough (#17393).
///
/// The daemon owns tool-capable provider/model selection for the forwarded
/// feature `profile` (Aggregate-tier Lane B forwards `feature_high`); this
/// transport adds no server-side loop or state. Routing for the
/// [`AiCapability::ToolChat`] capability reuses the `ai.text_generate` binding,
/// so `project_id` and `reasoning_effort` come from that resolved binding.
pub struct DaemonChatTransport<'a> {
    context: &'a AiContext,
    profile: String,
    client: Client,
    token: String,
}

impl<'a> DaemonChatTransport<'a> {
    /// Build a daemon chat transport for a resolved feature `profile`. The local
    /// CLI token is read now so a missing token fails fast before the loop runs.
    pub fn new(context: &'a AiContext, profile: impl Into<String>) -> Result<Self, AiError> {
        let client = daemon_client()?;
        let token = read_local_cli_token()?;
        Ok(Self {
            context,
            profile: profile.into(),
            client,
            token,
        })
    }
}

impl ChatTransport for DaemonChatTransport<'_> {
    fn complete(&self, request: ChatCompletionRequest<'_>) -> Result<ChatCompletion, AiError> {
        let url = daemon_url(DAEMON_CHAT_COMPLETIONS_PATH);
        let binding = self.context.binding(AiCapability::ToolChat);
        let body = build_daemon_chat_body(
            &self.profile,
            self.context.project_id.as_deref(),
            binding.reasoning_effort.as_deref(),
            &request,
        );

        let _permit = self.context.limiter.acquire();
        let value = retry_with_backoff(
            || {
                let http = with_local_token(
                    self.client
                        .post(&url)
                        .timeout(timeout_for(AiCapability::ToolChat))
                        .json(&body),
                    &self.token,
                );
                parse_json_response(http.send().map_err(reqwest_error)?)
            },
            std::thread::sleep,
        )?;

        parse_completion(&value)
    }

    fn route(&self) -> &'static str {
        "daemon"
    }

    fn profile(&self) -> Option<&str> {
        Some(&self.profile)
    }
}

/// Build the direct-route OpenAI-compatible request body for one completion
/// turn. Threads the target's optional `reasoning_effort` through so direct
/// Lane B (and Lane A) keep their profile reasoning pins.
pub(crate) fn build_request_body(
    target: &DirectGenerationTarget,
    request: &ChatCompletionRequest<'_>,
) -> Value {
    let mut body = Map::new();
    push_messages_and_tools(&mut body, request);
    insert_trimmed(&mut body, "model", target.model.as_deref());
    if let Some(max_tokens) = request.max_tokens.filter(|value| *value > 0) {
        body.insert("max_tokens".to_string(), Value::from(max_tokens));
    }
    insert_trimmed(
        &mut body,
        "reasoning_effort",
        target.reasoning_effort.as_deref(),
    );
    Value::Object(body)
}

/// Build the daemon-route chat-completion body: the same OpenAI-shaped messages
/// and tools, plus the feature `profile` the daemon resolves to a tool-capable
/// provider/model, the active `project_id`, and an optional `reasoning_effort`
/// pin. The daemon owns provider/model selection, so neither is sent here.
pub(crate) fn build_daemon_chat_body(
    profile: &str,
    project_id: Option<&str>,
    reasoning_effort: Option<&str>,
    request: &ChatCompletionRequest<'_>,
) -> Value {
    let mut body = Map::new();
    push_messages_and_tools(&mut body, request);
    insert_trimmed(&mut body, "profile", Some(profile));
    insert_trimmed(&mut body, "project_id", project_id);
    if let Some(max_tokens) = request.max_tokens.filter(|value| *value > 0) {
        body.insert("max_tokens".to_string(), Value::from(max_tokens));
    }
    insert_trimmed(&mut body, "reasoning_effort", reasoning_effort);
    Value::Object(body)
}

/// Insert the OpenAI-shaped `messages` array, and `tools`/`tool_choice` when the
/// request advertises any tools (Lane A passes none, suppressing tool calls).
fn push_messages_and_tools(body: &mut Map<String, Value>, request: &ChatCompletionRequest<'_>) {
    let messages: Vec<Value> = request.messages.iter().map(message_to_json).collect();
    body.insert("messages".to_string(), Value::Array(messages));
    if !request.tools.is_empty() {
        let tools: Vec<Value> = request.tools.iter().map(tool_to_json).collect();
        body.insert("tools".to_string(), Value::Array(tools));
        body.insert(
            "tool_choice".to_string(),
            Value::String(request.tool_choice.as_str().to_string()),
        );
    }
}

/// Insert a string field only when it is present and non-blank.
fn insert_trimmed(body: &mut Map<String, Value>, name: &str, value: Option<&str>) {
    if let Some(value) = value.map(str::trim).filter(|value| !value.is_empty()) {
        body.insert(name.to_string(), Value::String(value.to_string()));
    }
}

fn message_to_json(message: &ChatMessage) -> Value {
    let mut object = Map::new();
    object.insert(
        "role".to_string(),
        Value::String(message.role.as_str().to_string()),
    );
    object.insert(
        "content".to_string(),
        match &message.content {
            Some(content) => Value::String(content.clone()),
            None => Value::Null,
        },
    );
    if let Some(tool_call_id) = &message.tool_call_id {
        object.insert(
            "tool_call_id".to_string(),
            Value::String(tool_call_id.clone()),
        );
    }
    if message.role == ChatRole::Assistant && !message.tool_calls.is_empty() {
        let calls: Vec<Value> = message.tool_calls.iter().map(tool_call_to_json).collect();
        object.insert("tool_calls".to_string(), Value::Array(calls));
    }
    Value::Object(object)
}

fn tool_call_to_json(call: &ToolCall) -> Value {
    let arguments = serde_json::to_string(&call.arguments).unwrap_or_else(|_| "{}".to_string());
    json!({
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": arguments,
        }
    })
}

fn tool_to_json(tool: &ToolSchema) -> Value {
    json!({
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
    })
}

/// Parse an OpenAI-compatible chat-completion response into a [`ChatCompletion`].
pub(crate) fn parse_completion(value: &Value) -> Result<ChatCompletion, AiError> {
    let choice = value
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first());
    let message = choice.and_then(|choice| choice.get("message"));

    let content = message
        .and_then(|message| message.get("content"))
        .and_then(Value::as_str)
        .filter(|content| !content.is_empty())
        .map(str::to_string);

    let tool_calls: Vec<ToolCall> = message
        .and_then(|message| message.get("tool_calls"))
        .and_then(Value::as_array)
        .map(|calls| calls.iter().filter_map(parse_tool_call).collect())
        .unwrap_or_default();

    if content.is_none() && tool_calls.is_empty() {
        return Err(AiError::parse_failure(
            "chat completion response missing assistant content or tool calls",
        ));
    }

    let finish_reason = choice
        .and_then(|choice| choice.get("finish_reason"))
        .and_then(Value::as_str)
        .map(str::to_string);

    Ok(ChatCompletion {
        content,
        tool_calls,
        finish_reason,
        model: chat_completion_model(value),
        usage: chat_completion_usage(value),
    })
}

fn parse_tool_call(value: &Value) -> Option<ToolCall> {
    let function = value.get("function")?;
    let name = function.get("name").and_then(Value::as_str)?.to_string();
    let id = value
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("call_{name}"));
    let arguments = match function.get("arguments") {
        Some(Value::String(raw)) => serde_json::from_str::<Value>(raw).unwrap_or(Value::Null),
        Some(other) => other.clone(),
        None => Value::Null,
    };
    Some(ToolCall {
        id,
        name,
        arguments,
    })
}
