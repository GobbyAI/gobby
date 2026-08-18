#![allow(unused_imports)]

pub(super) use std::cell::{Cell, RefCell};
pub(super) use std::collections::BTreeMap;
pub(super) use std::collections::VecDeque;
pub(super) use std::ffi::OsString;
pub(super) use std::fs;
pub(super) use std::path::Path;
pub(super) use std::sync::{Arc, Mutex, MutexGuard};
pub(super) use std::time::Duration;

pub(super) use serde_json::{Value, json};

pub(super) use super::super::tool_loop::{
    MAX_FORCED_INVESTIGATION_RETRIES, run_tool_loop_with_clock,
};
pub(super) use super::super::transport::{
    ToolPolicy, build_request_body, daemon_agentic_chat, parse_daemon_agentic,
};
pub(super) use super::super::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport,
    DirectGenerationTarget, FEATURE_HIGH, FEATURE_LOW, FEATURE_MID, GenerationTier, StopReason,
    ToolCall, ToolChoice, ToolError, ToolExecutor, ToolLoopLimits, ToolLoopRunContext, ToolSchema,
    generate_one_shot_pinned, profile_for_tier, resolve_direct_generation_target, run_tool_loop,
    run_tool_loop_with_context,
};
pub(super) use crate::ai_context::{AiBindings, AiContext, AiLimiter, GrantAiState};
pub(super) use crate::ai_types::{AiError, TokenUsage};
pub(super) use crate::config::{
    AiRouting, AiTuning, CapabilityBinding, ConfigSource, FeatureCandidate, TEST_ENV_LOCK, ai_keys,
};
pub(super) use crate::test_http::{
    RequestHandle, spawn_json_response, spawn_json_response_from_request,
};

pub(super) fn request_body_json(raw: &str) -> Value {
    let body = raw.split("\r\n\r\n").nth(1).expect("request has a body");
    serde_json::from_str(body).expect("request body is JSON")
}

pub(super) fn blank_binding() -> CapabilityBinding {
    CapabilityBinding {
        routing: AiRouting::Daemon,
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
    pub(super) calls: Mutex<Vec<ToolCall>>,
    result: String,
}

impl EchoExecutor {
    pub(super) fn new(result: impl Into<String>) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
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

    fn execute(&self, call: &ToolCall) -> Result<String, ToolError> {
        self.calls.lock().expect("calls lock").push(call.clone());
        Ok(self.result.clone())
    }
}

pub(super) fn fixture_grant_state() -> GrantAiState {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/runtime_grants/golden/direct_datastores.json");
    let raw = fs::read(path).expect("golden grant");
    let mut grant: crate::grant::GrantBundle =
        serde_json::from_slice(raw.trim_ascii()).expect("parse golden grant");
    grant.capabilities.vision_extract = crate::grant::AiCapability::Daemon {};
    grant.capabilities.audio_transcribe = crate::grant::AiCapability::Daemon {};
    GrantAiState {
        capabilities: grant.capabilities.clone(),
        daemon_reachable: true,
        bundle: grant,
    }
}
