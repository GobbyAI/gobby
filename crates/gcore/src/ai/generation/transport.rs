//! Daemon-only tool-loop chat generation.
//!
//! [`daemon_agentic_chat`] POSTs to the daemon's agentic
//! `/api/llm/chat/completions` endpoint, which runs its own server-side
//! investigation loop under the caller's [`ToolPolicy`] and returns the
//! finished narrative — never `tool_calls` for the caller to execute.

use std::time::Instant;

use serde_json::{Map, Value, json};

use crate::ai::daemon::{daemon_client, daemon_url, read_local_cli_token, with_local_token};
use crate::ai::{
    chat_completion_model, chat_completion_usage, parse_json_response, reqwest_error,
    retry_with_backoff_until,
};
use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TokenUsage};
use crate::config::{AiCapability, FeatureCandidate};
use crate::local_token::AGENT_API_TOKEN_ENV;

use super::profile::DirectGenerationTarget;
use super::tool_loop::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ToolCall, ToolLoopLimits,
    ToolSchema,
};

/// Daemon tool-passthrough chat-completion path (#17393).
const DAEMON_CHAT_COMPLETIONS_PATH: &str = "/api/llm/chat/completions";

/// Final result of a one-shot daemon-side agentic narrative generation call
/// (the tool-loop daemon route for codewiki and gwiki).
///
/// The daemon runs its own Claude Agent SDK investigation loop (executing only
/// the policy-whitelisted tools over the project) server-side and returns the
/// finished narrative plus investigation provenance. The CLI runs no local
/// tool loop for this route — the agentic endpoint never returns `tool_calls`,
/// so a single-turn passthrough transport would re-prompt forever.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DaemonAgenticResult {
    /// Stable caller label echoed by the daemon.
    pub caller: String,
    /// UUID v4 generated for this request and echoed by the daemon.
    pub request_id: String,
    /// Final assistant narrative, if the daemon produced any.
    pub content: Option<String>,
    /// Model the daemon selected for its agent.
    pub model: Option<String>,
    /// Tool invocations the daemon's agent made during its investigation.
    pub tool_use_count: usize,
    /// Provider-native investigation turns, when reported.
    pub turns: Option<usize>,
    /// Canonical termination reason reported by the daemon, when available.
    pub stop_reason: Option<String>,
    /// Token usage, when reported.
    pub usage: Option<TokenUsage>,
}

/// Caller-declared description of the agent's investigation surface on the
/// daemon `tool_chat` route. gcore stays generic over *what* the agent does:
/// the caller (codewiki, gwiki) names the executable family and the exact
/// subcommands it may run. `cli` selects the family (`"gcode"`/`"gwiki"`),
/// `tools` lists the exposed subcommands, and `allow_mutation` gates mutating
/// subcommands — a read-only caller leaves it `false`. The daemon validates
/// these against its own whitelist before executing any tool.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolPolicy {
    /// Executable family the daemon runs (`"gcode"` or `"gwiki"`).
    pub cli: String,
    /// Whitelisted subcommands the agent may invoke.
    pub tools: Vec<String>,
    /// Whether mutating subcommands are permitted (read-only callers: `false`).
    pub allow_mutation: bool,
}

/// One-shot daemon-side agentic narrative generation. POSTs the system+user
/// `messages` plus the feature `profile` (or an explicit `candidates` chain
/// that supersedes it), absolute `project_path`, and the
/// caller's `tool_policy` to the daemon's `/api/llm/chat/completions` endpoint;
/// the daemon runs its own agent loop over the repo (executing only the
/// whitelisted tools) and returns the finished narrative. A single POST — no
/// `tools`/`tool_choice`/`model` are sent and the response is never re-prompted
/// (it carries the final answer and `investigation` provenance, not `tool_calls`
/// to execute locally). The request runs under the local CLI token with retry
/// and the `ToolChat` capability timeout.
#[allow(clippy::too_many_arguments)]
pub fn daemon_agentic_chat(
    context: &AiContext,
    caller: &str,
    profile: &str,
    candidates: Option<&[FeatureCandidate]>,
    project_path: &str,
    tool_policy: &ToolPolicy,
    messages: &[ChatMessage],
    limits: &ToolLoopLimits,
    reasoning_effort: Option<&str>,
) -> Result<DaemonAgenticResult, AiError> {
    context.require_granted(AiCapability::ToolChat)?;
    let caller = caller.trim();
    if caller.is_empty() {
        return Err(AiError::not_configured(
            Some(AiCapability::ToolChat.as_str().to_string()),
            "daemon agentic chat caller is required",
        ));
    }
    let profile = profile.trim();
    let candidates = candidates.filter(|candidates| !candidates.is_empty());
    if profile.is_empty() && candidates.is_none() {
        return Err(AiError::not_configured(
            Some(AiCapability::ToolChat.as_str().to_string()),
            "daemon agentic chat profile is required",
        ));
    }
    let url = daemon_url(DAEMON_CHAT_COMPLETIONS_PATH);
    let client = daemon_client()?;
    let token = read_local_cli_token()?;
    let request_id = uuid::Uuid::new_v4().to_string();
    let body = build_daemon_agentic_body(
        caller,
        &request_id,
        profile,
        candidates,
        project_path,
        tool_policy,
        messages,
        limits,
        reasoning_effort,
    );
    log::debug!(
        "daemon agentic request started caller={caller} request_id={request_id} profile={profile} candidates={}",
        candidates.map_or(0, |items| items.len())
    );

    let _permit = context.limiter.acquire();
    let deadline = Instant::now() + limits.loop_timeout();
    let value = retry_with_backoff_until(
        deadline,
        |remaining| {
            let http = with_managed_identity_headers(with_local_token(
                client.post(&url).timeout(remaining).json(&body),
                &token,
            ))?;
            parse_json_response(http.send().map_err(reqwest_error)?)
        },
        std::thread::sleep,
    )?;

    let result = parse_daemon_agentic(&value)?;
    if result.caller != caller || result.request_id != request_id {
        return Err(AiError::parse_failure(
            "daemon agentic response correlation does not match request",
        ));
    }
    log::debug!(
        "daemon agentic request completed caller={} request_id={} model={} stop_reason={} turns={} tool_use_count={}",
        result.caller,
        result.request_id,
        result.model.as_deref().unwrap_or("<unknown>"),
        result.stop_reason.as_deref().unwrap_or("<unknown>"),
        result
            .turns
            .map_or_else(|| "<unknown>".to_string(), |turns| turns.to_string()),
        result.tool_use_count,
    );
    Ok(result)
}

/// Build the daemon agentic-chat body: the system+user `messages`, the feature
/// `profile` (or an explicit `candidates` chain that supersedes it, mirroring
/// the one-shot `/api/llm/generate` precedence), the absolute `project_path`
/// the daemon investigates, the caller's `tool_policy`
/// (the daemon builds the executable tools from it and enforces its own
/// whitelist), and optional `max_turns`/`reasoning_effort`. No
/// `tools`/`tool_choice`/`model` — the daemon owns its provider/model selection
/// and builds the tools from the policy.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_daemon_agentic_body(
    caller: &str,
    request_id: &str,
    profile: &str,
    candidates: Option<&[FeatureCandidate]>,
    project_path: &str,
    tool_policy: &ToolPolicy,
    messages: &[ChatMessage],
    limits: &ToolLoopLimits,
    reasoning_effort: Option<&str>,
) -> Value {
    let mut body = Map::new();
    body.insert("caller".to_string(), Value::String(caller.to_string()));
    body.insert(
        "request_id".to_string(),
        Value::String(request_id.to_string()),
    );
    let messages: Vec<Value> = messages.iter().map(message_to_json).collect();
    body.insert("messages".to_string(), Value::Array(messages));
    match candidates {
        // An explicit candidate chain pins the exact provider/model sequence
        // for this call; the profile is omitted so the daemon routes to the
        // requested candidates only (each carries its own reasoning pin).
        Some(candidates) => {
            body.insert(
                "candidates".to_string(),
                serde_json::to_value(candidates).expect("feature candidates serialize"),
            );
        }
        None => insert_trimmed(&mut body, "profile", Some(profile)),
    }
    insert_trimmed(&mut body, "project_path", Some(project_path));
    body.insert("tool_policy".to_string(), tool_policy_to_json(tool_policy));
    body.insert(
        "limits".to_string(),
        serde_json::to_value(limits).expect("validated tool-loop limits serialize"),
    );
    insert_trimmed(&mut body, "reasoning_effort", reasoning_effort);
    Value::Object(body)
}

fn with_managed_identity_headers(
    request: reqwest::blocking::RequestBuilder,
) -> Result<reqwest::blocking::RequestBuilder, AiError> {
    fn env_value(name: &str) -> Option<String> {
        std::env::var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    }

    if env_value(AGENT_API_TOKEN_ENV).is_none() {
        return Ok(request);
    }
    let (owner_header, execution_id) = match (
        env_value("GOBBY_AGENT_RUN_ID"),
        env_value("GOBBY_MANAGED_EXECUTION_ID"),
    ) {
        (Some(execution_id), None) => ("X-Gobby-Agent-Run-Id", execution_id),
        (None, Some(execution_id)) => ("X-Gobby-Managed-Execution-Id", execution_id),
        _ => {
            return Err(AiError::not_configured(
                Some(AiCapability::ToolChat.as_str().to_string()),
                "managed daemon capability owner is incomplete or ambiguous",
            ));
        }
    };
    let project_id = env_value("GOBBY_PROJECT_ID").ok_or_else(|| {
        AiError::not_configured(
            Some(AiCapability::ToolChat.as_str().to_string()),
            "managed daemon capability project identity is missing",
        )
    })?;
    let session_id = env_value("GOBBY_SESSION_ID").ok_or_else(|| {
        AiError::not_configured(
            Some(AiCapability::ToolChat.as_str().to_string()),
            "managed daemon capability session identity is missing",
        )
    })?;

    Ok(request
        .header(owner_header, execution_id)
        .header("X-Gobby-Caller-Project-Id", project_id)
        .header("X-Gobby-Session-Id", session_id))
}

/// Serialize a [`ToolPolicy`] into the daemon's `{cli, tools, allow_mutation}`
/// shape. The daemon rejects an empty `tools` list and validates each subcommand
/// against its read-only whitelist before executing it.
fn tool_policy_to_json(policy: &ToolPolicy) -> Value {
    let tools: Vec<Value> = policy
        .tools
        .iter()
        .map(|tool| Value::String(tool.clone()))
        .collect();
    let mut object = Map::new();
    object.insert("cli".to_string(), Value::String(policy.cli.clone()));
    object.insert("tools".to_string(), Value::Array(tools));
    object.insert(
        "allow_mutation".to_string(),
        Value::Bool(policy.allow_mutation),
    );
    Value::Object(object)
}

/// Parse the daemon agentic-chat response into a [`DaemonAgenticResult`]:
/// `choices[0].message.content` is the narrative, `model` the agent's model, and
/// `investigation.tool_use_count`/`investigation.turns`/`investigation.stop_reason`
/// the provenance — `tool_use_count` defaults to 0 when absent, while `turns` and
/// `stop_reason` stay `None` so absence is never reported as a count or a verdict.
/// Token usage reuses the shared chat-completion usage parser.
pub(crate) fn parse_daemon_agentic(value: &Value) -> Result<DaemonAgenticResult, AiError> {
    let content = value
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("message"))
        .and_then(|message| message.get("content"))
        .and_then(Value::as_str)
        .filter(|content| !content.is_empty())
        .map(str::to_string);
    let investigation = value
        .get("investigation")
        .and_then(Value::as_object)
        .ok_or_else(|| AiError::parse_failure("daemon agentic response missing investigation"))?;
    let required_text = |key: &str| {
        investigation
            .get(key)
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(str::to_string)
            .ok_or_else(|| {
                AiError::parse_failure(format!(
                    "daemon agentic response missing investigation.{key}"
                ))
            })
    };
    let count = |key: &str| {
        investigation
            .get(key)
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok())
    };
    Ok(DaemonAgenticResult {
        caller: required_text("caller")?,
        request_id: required_text("request_id")?,
        content,
        model: chat_completion_model(value),
        tool_use_count: count("tool_use_count").unwrap_or(0),
        turns: count("turns"),
        stop_reason: investigation
            .get("stop_reason")
            .and_then(Value::as_str)
            .map(str::to_string),
        usage: chat_completion_usage(value),
    })
}

/// Build the direct-route OpenAI-compatible request body for one completion
/// turn. Threads the target's optional `reasoning_effort` through so direct
/// Tool-loop and one-shot routes keep their profile reasoning pins.
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

/// Insert the OpenAI-shaped `messages` array, and `tools`/`tool_choice` when the
/// request advertises any tools (one-shot passes none, suppressing tool calls).
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
#[allow(dead_code)]
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

#[allow(dead_code)]
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
