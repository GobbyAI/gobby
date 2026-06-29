#![allow(unused_imports)]

pub(super) use std::cell::{Cell, RefCell};
pub(super) use std::collections::BTreeMap;
pub(super) use std::collections::VecDeque;
pub(super) use std::ffi::OsString;
pub(super) use std::fs;
pub(super) use std::path::Path;
pub(super) use std::sync::MutexGuard;
pub(super) use std::time::Duration;

pub(super) use serde_json::{Value, json};

pub(super) use super::super::tool_loop::{
    MAX_FORCED_INVESTIGATION_RETRIES, run_tool_loop_with_clock,
};
pub(super) use super::super::transport::{
    ToolPolicy, build_daemon_chat_body, build_request_body, daemon_agentic_chat, parse_completion,
};
pub(super) use super::super::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport,
    DirectChatTransport, DirectGenerationTarget, FEATURE_HIGH, FEATURE_LOW, FEATURE_MID,
    GenerationTier, StopReason, ToolCall, ToolChoice, ToolError, ToolExecutor, ToolLoopLimits,
    ToolSchema, profile_for_tier, resolve_direct_generation_target, run_tool_loop,
};
pub(super) use crate::ai_context::{AiBindings, AiContext, AiLimiter};
pub(super) use crate::ai_types::{AiError, TokenUsage};
pub(super) use crate::config::{
    AiRouting, AiTuning, CapabilityBinding, ConfigSource, TEST_ENV_LOCK, ai_keys,
};
pub(super) use crate::test_http::spawn_json_response;

pub(super) fn request_body_json(raw: &str) -> Value {
    let body = raw.split("\r\n\r\n").nth(1).expect("request has a body");
    serde_json::from_str(body).expect("request body is JSON")
}

pub(super) fn blank_binding() -> CapabilityBinding {
    CapabilityBinding {
        routing: AiRouting::Direct,
        transport: None,
        api_base: None,
        api_key: None,
        model: None,
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
    }
}

pub(super) struct EchoExecutor {
    pub(super) calls: Vec<ToolCall>,
    result: String,
}

impl EchoExecutor {
    pub(super) fn new(result: impl Into<String>) -> Self {
        Self {
            calls: Vec::new(),
            result: result.into(),
        }
    }
}

impl ToolExecutor for EchoExecutor {
    fn schemas(&self) -> Vec<ToolSchema> {
        vec![ToolSchema {
            name: "echo".to_string(),
            description: "echoes its input".to_string(),
            parameters: json!({"type":"object","properties":{"text":{"type":"string"}}}),
        }]
    }

    fn execute(&mut self, call: &ToolCall) -> Result<String, ToolError> {
        self.calls.push(call.clone());
        Ok(self.result.clone())
    }
}
