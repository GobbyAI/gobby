use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::collections::VecDeque;
use std::ffi::OsString;
use std::fs;
use std::path::Path;
use std::sync::MutexGuard;
use std::time::Duration;

use serde_json::{Value, json};

use super::tool_loop::{MAX_FORCED_INVESTIGATION_RETRIES, run_tool_loop_with_clock};
use super::transport::{
    build_daemon_chat_body, build_request_body, daemon_agentic_chat, parse_completion,
};
use super::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport,
    DirectChatTransport, DirectGenerationTarget, FEATURE_HIGH, FEATURE_LOW, FEATURE_MID,
    GenerationTier, StopReason, ToolCall, ToolChoice, ToolError, ToolExecutor, ToolLoopLimits,
    ToolSchema, profile_for_tier, resolve_direct_generation_target, run_tool_loop,
};
use crate::ai_context::{AiBindings, AiContext, AiLimiter};
use crate::ai_types::{AiError, TokenUsage};
use crate::config::{AiRouting, AiTuning, CapabilityBinding, ConfigSource, TEST_ENV_LOCK, ai_keys};
use crate::test_http::spawn_json_response;

// ----- tier -> profile mapping -----------------------------------------------

#[test]
fn tier_profile_mapping_is_fixed_with_aggregate_override() {
    assert_eq!(
        profile_for_tier(GenerationTier::Standard, None).as_str(),
        FEATURE_LOW
    );
    assert_eq!(
        profile_for_tier(GenerationTier::Module, None).as_str(),
        FEATURE_MID
    );
    assert_eq!(
        profile_for_tier(GenerationTier::Aggregate, None).as_str(),
        FEATURE_HIGH
    );

    // Override applies to Aggregate only.
    assert_eq!(
        profile_for_tier(GenerationTier::Aggregate, Some("feature_custom")).as_str(),
        "feature_custom"
    );
    assert_eq!(
        profile_for_tier(GenerationTier::Module, Some("feature_custom")).as_str(),
        FEATURE_MID
    );

    // Blank override falls back to the default high tier.
    assert_eq!(
        profile_for_tier(GenerationTier::Aggregate, Some("   ")).as_str(),
        FEATURE_HIGH
    );
}

// ----- standalone Direct profile resolution ----------------------------------

struct MapSource {
    values: BTreeMap<String, String>,
}

impl MapSource {
    fn new() -> Self {
        Self {
            values: BTreeMap::new(),
        }
    }

    fn with(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.values.insert(key.into(), value.into());
        self
    }
}

impl ConfigSource for MapSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        // Standalone behavior: expand ${ENV} patterns, pass plaintext through.
        crate::config::resolve_env_pattern(value)?
            .ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
    }
}

#[test]
fn profile_field_prefers_profile_key_then_base_fallback() {
    let mut source = MapSource::new()
        .with(
            ai_keys::text_generate_profile_key("feature_high", ai_keys::PROFILE_MODEL),
            "high-model",
        )
        .with(ai_keys::TEXT_GENERATE_MODEL, "base-model")
        .with(ai_keys::TEXT_GENERATE_API_BASE, "http://base:1234/v1");

    let target = resolve_direct_generation_target(&mut source, "feature_high");
    // Profile-specific model wins.
    assert_eq!(target.model.as_deref(), Some("high-model"));
    // api_base falls back to the base text_generate binding.
    assert_eq!(target.api_base.as_deref(), Some("http://base:1234/v1"));
    assert_eq!(target.api_base(), Some("http://base:1234/v1"));
}

#[test]
fn profile_resolves_plaintext_api_key_and_env_default() {
    let mut source = MapSource::new()
        .with(
            ai_keys::TEXT_GENERATE_API_BASE,
            "${GCORE_TEST_UNSET_HOST:-http://default-host:1234/v1}",
        )
        .with(
            ai_keys::text_generate_profile_key("feature_low", ai_keys::PROFILE_API_KEY),
            "sk-plaintext-123",
        );

    let target = resolve_direct_generation_target(&mut source, "feature_low");
    // ${...:-default} expands without any env var set.
    assert_eq!(
        target.api_base.as_deref(),
        Some("http://default-host:1234/v1")
    );
    // Plaintext api_key is accepted in standalone YAML.
    assert_eq!(target.api_key.as_deref(), Some("sk-plaintext-123"));
}

#[test]
fn profile_unresolved_env_without_default_is_none() {
    let mut source = MapSource::new().with(
        ai_keys::TEXT_GENERATE_MODEL,
        "${GCORE_TEST_UNSET_NO_DEFAULT}",
    );
    let target = resolve_direct_generation_target(&mut source, "feature_mid");
    assert!(target.model.is_none());
    assert!(target.api_base.is_none());
}

// ----- Lane B: stub transport + trivial executor harness ---------------------

struct StubTransport {
    completions: RefCell<VecDeque<ChatCompletion>>,
    requests: RefCell<Vec<Vec<ChatMessage>>>,
    tool_choices: RefCell<Vec<ToolChoice>>,
}

impl StubTransport {
    fn new(completions: Vec<ChatCompletion>) -> Self {
        Self {
            completions: RefCell::new(completions.into()),
            requests: RefCell::new(Vec::new()),
            tool_choices: RefCell::new(Vec::new()),
        }
    }
}

impl ChatTransport for StubTransport {
    fn complete(&self, request: ChatCompletionRequest<'_>) -> Result<ChatCompletion, AiError> {
        self.requests.borrow_mut().push(request.messages.to_vec());
        self.tool_choices.borrow_mut().push(request.tool_choice);
        Ok(self
            .completions
            .borrow_mut()
            .pop_front()
            .expect("stub has a scripted completion"))
    }

    fn route(&self) -> &'static str {
        "stub"
    }

    fn model(&self) -> Option<&str> {
        Some("stub-model")
    }
}

struct EchoExecutor {
    calls: Vec<ToolCall>,
    result: String,
}

impl EchoExecutor {
    fn new(result: impl Into<String>) -> Self {
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

fn tool_call_completion(name: &str, id: &str, arguments: Value) -> ChatCompletion {
    ChatCompletion {
        content: None,
        tool_calls: vec![ToolCall {
            id: id.to_string(),
            name: name.to_string(),
            arguments,
        }],
        finish_reason: Some("tool_calls".to_string()),
        model: Some("stub-model".to_string()),
        usage: None,
    }
}

fn content_completion(text: &str) -> ChatCompletion {
    ChatCompletion {
        content: Some(text.to_string()),
        tool_calls: Vec::new(),
        finish_reason: Some("stop".to_string()),
        model: Some("stub-model".to_string()),
        usage: None,
    }
}

fn two_tool_call_completion() -> ChatCompletion {
    ChatCompletion {
        content: None,
        tool_calls: vec![
            ToolCall {
                id: "call_a".to_string(),
                name: "echo".to_string(),
                arguments: json!({"text":"a"}),
            },
            ToolCall {
                id: "call_b".to_string(),
                name: "echo".to_string(),
                arguments: json!({"text":"b"}),
            },
        ],
        finish_reason: Some("tool_calls".to_string()),
        model: Some("stub-model".to_string()),
        usage: None,
    }
}

#[test]
fn lane_b_executes_tool_then_completes_with_observability() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({"text":"hi"})),
        content_completion("final answer"),
    ]);
    let mut executor = EchoExecutor::new("ECHO:hi");
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        messages,
        &ToolLoopLimits::default(),
        Some(256),
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    assert!(outcome.stop_reason.is_completed());
    assert_eq!(outcome.content.as_deref(), Some("final answer"));

    let obs = &outcome.observability;
    assert_eq!(obs.lane, "tool_loop");
    assert_eq!(obs.route, "stub");
    assert_eq!(obs.model.as_deref(), Some("stub-model"));
    assert_eq!(obs.tool_names, vec!["echo".to_string()]);
    assert_eq!(obs.tool_call_count, 1);
    assert_eq!(obs.turns, 2);
    assert_eq!(obs.termination_reason, "completed");

    // Executor saw exactly the echo call.
    assert_eq!(executor.calls.len(), 1);
    assert_eq!(executor.calls[0].name, "echo");
    assert_eq!(executor.calls[0].arguments["text"], "hi");

    // The second turn fed the tool result back to the model.
    let requests = transport.requests.borrow();
    assert_eq!(requests.len(), 2);
    let second = &requests[1];
    assert!(
        second
            .iter()
            .any(|message| message.role == ChatRole::Assistant && !message.tool_calls.is_empty())
    );
    let tool_message = second
        .iter()
        .find(|message| message.role == ChatRole::Tool)
        .expect("tool result message present");
    assert_eq!(tool_message.content.as_deref(), Some("ECHO:hi"));
    assert_eq!(tool_message.tool_call_id.as_deref(), Some("call_echo"));
}

#[test]
fn lane_b_forces_tool_use_on_first_turn_then_auto() {
    // Turn 0 forces a tool call (`required`) so a weak function-calling model
    // cannot one-shot an ungrounded answer and skip investigation entirely;
    // every later turn lets the model finalize freely (`auto`).
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({"text": "hi"})),
        content_completion("final answer"),
    ]);
    let mut executor = EchoExecutor::new("ECHO:hi");
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        messages,
        &ToolLoopLimits::default(),
        Some(256),
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    assert_eq!(
        *transport.tool_choices.borrow(),
        vec![ToolChoice::Required, ToolChoice::Auto]
    );
}

#[test]
fn lane_b_reprompts_a_model_that_ignores_required_tool_choice() {
    // A runtime may treat `tool_choice = "required"` as best-effort: the model
    // answers turn 0 with no tool call. The loop must not accept that
    // uninvestigated reply — it appends a correction and keeps forcing until a
    // tool call lands, so Lane B genuinely investigates before answering.
    let transport = StubTransport::new(vec![
        content_completion("premature one-shot answer"),
        tool_call_completion("echo", "call_echo", json!({"text": "hi"})),
        content_completion("grounded final answer"),
    ]);
    let mut executor = EchoExecutor::new("ECHO:hi");
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        messages,
        &ToolLoopLimits::default(),
        Some(256),
    )
    .expect("loop runs");

    // The premature answer was rejected; the grounded one (after a tool call) wins.
    assert_eq!(outcome.stop_reason, StopReason::Completed);
    assert_eq!(outcome.content.as_deref(), Some("grounded final answer"));
    assert_eq!(outcome.observability.tool_call_count, 1);

    // Forcing stayed on across the retry (Required, Required) until the tool
    // call landed, then switched to Auto for the final turn.
    assert_eq!(
        *transport.tool_choices.borrow(),
        vec![ToolChoice::Required, ToolChoice::Required, ToolChoice::Auto]
    );

    // The retry surfaced the rejected draft as an assistant turn and appended an
    // explicit correction, rather than re-issuing blindly.
    let requests = transport.requests.borrow();
    assert_eq!(requests[1].len(), requests[0].len() + 2);
    let draft = &requests[1][requests[1].len() - 2];
    assert_eq!(draft.role, ChatRole::Assistant);
    assert_eq!(draft.content.as_deref(), Some("premature one-shot answer"));
    let correction = requests[1].last().expect("retry request has messages");
    assert_eq!(correction.role, ChatRole::User);
    assert!(
        correction
            .content
            .as_deref()
            .is_some_and(|text| text.contains("without calling any tool"))
    );
}

#[test]
fn lane_b_hard_fails_when_the_model_never_investigates_after_corrections() {
    // A model that never calls a tool, even after repeated corrections, must
    // not spin forever and must not ship a silent uninvestigated one-shot:
    // the forcing retries are bounded, after which the loop fails (a non-
    // Completed stop reason → callers hard-fail the page, no skeleton).
    let stubborn = (0..=MAX_FORCED_INVESTIGATION_RETRIES)
        .map(|_| content_completion("one-shot answer"))
        .collect();
    let transport = StubTransport::new(stubborn);
    let mut executor = EchoExecutor::new("unused");
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        messages,
        &ToolLoopLimits::default(),
        Some(256),
    )
    .expect("loop runs");

    assert!(!outcome.stop_reason.is_completed());
    assert_eq!(outcome.stop_reason, StopReason::MaxTurns);
    assert_eq!(outcome.content, None);
    assert_eq!(outcome.observability.tool_call_count, 0);
    // One initial forcing turn plus the bounded retries, all `required`.
    assert_eq!(
        transport.tool_choices.borrow().len(),
        MAX_FORCED_INVESTIGATION_RETRIES + 1
    );
    assert!(
        transport
            .tool_choices
            .borrow()
            .iter()
            .all(|choice| *choice == ToolChoice::Required)
    );
}

#[test]
fn lane_b_relays_tool_error_to_model() {
    struct FailingExecutor;
    impl ToolExecutor for FailingExecutor {
        fn schemas(&self) -> Vec<ToolSchema> {
            vec![ToolSchema {
                name: "boom".to_string(),
                description: "always fails".to_string(),
                parameters: json!({"type":"object"}),
            }]
        }
        fn execute(&mut self, _call: &ToolCall) -> Result<String, ToolError> {
            Err(ToolError::new("not found"))
        }
    }

    let transport = StubTransport::new(vec![
        tool_call_completion("boom", "call_boom", json!({})),
        content_completion("recovered"),
    ]);
    let mut executor = FailingExecutor;
    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &ToolLoopLimits::default(),
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    let requests = transport.requests.borrow();
    let tool_message = requests[1]
        .iter()
        .find(|message| message.role == ChatRole::Tool)
        .expect("tool error relayed");
    assert_eq!(
        tool_message.content.as_deref(),
        Some("tool error: not found")
    );
}

#[test]
fn lane_b_aggregates_token_usage_across_turns() {
    let mut turn1 = tool_call_completion("echo", "call_echo", json!({}));
    turn1.usage = Some(TokenUsage {
        input_tokens: Some(10),
        output_tokens: Some(4),
        total_tokens: Some(14),
    });
    let mut turn2 = content_completion("done");
    turn2.usage = Some(TokenUsage {
        input_tokens: Some(20),
        output_tokens: Some(6),
        total_tokens: Some(26),
    });

    let transport = StubTransport::new(vec![turn1, turn2]);
    let mut executor = EchoExecutor::new("r");
    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &ToolLoopLimits::default(),
        None,
    )
    .expect("loop runs");

    let usage = outcome
        .total_usage
        .expect("token usage aggregated across turns");
    assert_eq!(usage.input_tokens, Some(30));
    assert_eq!(usage.output_tokens, Some(10));
    assert_eq!(usage.total_tokens, Some(40));
    assert_eq!(usage.token_count(), Some(40));
}

#[test]
fn lane_b_reports_no_usage_when_unreported() {
    // Neither the investigation turn nor the final turn reports usage.
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({"text": "r"})),
        content_completion("done"),
    ]);
    let mut executor = EchoExecutor::new("r");
    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &ToolLoopLimits::default(),
        None,
    )
    .expect("loop runs");
    assert!(outcome.total_usage.is_none());
}

#[test]
fn lane_b_stops_at_max_turns() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "a", json!({})),
        tool_call_completion("echo", "b", json!({})),
    ]);
    let mut executor = EchoExecutor::new("r");
    let limits = ToolLoopLimits {
        max_turns: 2,
        max_tool_calls: 100,
        ..ToolLoopLimits::default()
    };
    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::MaxTurns);
    assert!(outcome.content.is_none());
    assert_eq!(outcome.observability.turns, 2);
    assert_eq!(outcome.observability.tool_call_count, 2);
}

#[test]
fn lane_b_stops_at_max_tool_calls() {
    fn two_calls() -> ChatCompletion {
        ChatCompletion {
            content: None,
            tool_calls: vec![
                ToolCall {
                    id: "a".to_string(),
                    name: "echo".to_string(),
                    arguments: json!({}),
                },
                ToolCall {
                    id: "b".to_string(),
                    name: "echo".to_string(),
                    arguments: json!({}),
                },
            ],
            finish_reason: Some("tool_calls".to_string()),
            model: Some("stub-model".to_string()),
            usage: None,
        }
    }

    let transport = StubTransport::new(vec![two_calls(), two_calls()]);
    let mut executor = EchoExecutor::new("r");
    let limits = ToolLoopLimits {
        max_turns: 100,
        max_tool_calls: 3,
        ..ToolLoopLimits::default()
    };
    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::MaxToolCalls);
    assert_eq!(outcome.observability.tool_call_count, 3);
    assert_eq!(outcome.observability.turns, 2);
}

#[test]
fn lane_b_truncates_tool_result_on_utf8_boundary() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({})),
        content_completion("done"),
    ]);
    // "ééé" is 6 bytes; a 5-byte cap keeps 2 chars (4 bytes) without splitting.
    let mut executor = EchoExecutor::new("ééé");
    let limits = ToolLoopLimits {
        max_bytes_per_tool_result: 5,
        ..ToolLoopLimits::default()
    };
    let outcome = run_tool_loop(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    let requests = transport.requests.borrow();
    let tool_message = requests[1]
        .iter()
        .find(|message| message.role == ChatRole::Tool)
        .expect("tool result present");
    let content = tool_message.content.as_deref().unwrap();
    assert!(content.len() <= 5);
    assert_eq!(content, "éé");
}

#[test]
fn lane_b_stops_at_timeout() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "a", json!({})),
        tool_call_completion("echo", "b", json!({})),
    ]);
    let mut executor = EchoExecutor::new("r");
    let limits = ToolLoopLimits {
        timeout: Duration::from_millis(50),
        max_turns: 100,
        max_tool_calls: 100,
        ..ToolLoopLimits::default()
    };

    // Scripted clock: proceed once (0ms), then exceed the 50ms budget.
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_millis(100),
        Duration::from_millis(100),
    ];
    let index = Cell::new(0usize);
    let clock = || {
        let current = index.get();
        let value = elapsed[current.min(elapsed.len() - 1)];
        index.set(current + 1);
        value
    };

    let outcome = run_tool_loop_with_clock(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert_eq!(outcome.observability.turns, 1);
    assert_eq!(outcome.observability.elapsed_ms, 100);
}

#[test]
fn lane_b_content_completion_times_out_after_transport() {
    let transport = StubTransport::new(vec![content_completion("late answer")]);
    let mut executor = EchoExecutor::new("unused");
    let limits = ToolLoopLimits {
        timeout: Duration::from_millis(50),
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_millis(100),
        Duration::from_millis(100),
    ];
    let index = Cell::new(0usize);
    let clock = || {
        let current = index.get();
        let value = elapsed[current.min(elapsed.len() - 1)];
        index.set(current + 1);
        value
    };

    let outcome = run_tool_loop_with_clock(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert!(outcome.content.is_none());
    assert_eq!(outcome.observability.turns, 1);
    assert!(executor.calls.is_empty());
}

#[test]
fn lane_b_tool_call_completion_times_out_after_transport_without_executing_tools() {
    let transport = StubTransport::new(vec![tool_call_completion("echo", "a", json!({}))]);
    let mut executor = EchoExecutor::new("unused");
    let limits = ToolLoopLimits {
        timeout: Duration::from_millis(50),
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_millis(100),
        Duration::from_millis(100),
    ];
    let index = Cell::new(0usize);
    let clock = || {
        let current = index.get();
        let value = elapsed[current.min(elapsed.len() - 1)];
        index.set(current + 1);
        value
    };

    let outcome = run_tool_loop_with_clock(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert!(executor.calls.is_empty());
    assert_eq!(outcome.observability.tool_call_count, 0);
}

#[test]
fn lane_b_times_out_after_first_tool_result_before_next_tool_call() {
    let transport = StubTransport::new(vec![two_tool_call_completion()]);
    let mut executor = EchoExecutor::new("r");
    let limits = ToolLoopLimits {
        timeout: Duration::from_millis(50),
        max_tool_calls: 100,
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_millis(0),
        Duration::from_millis(100),
        Duration::from_millis(100),
    ];
    let index = Cell::new(0usize);
    let clock = || {
        let current = index.get();
        let value = elapsed[current.min(elapsed.len() - 1)];
        index.set(current + 1);
        value
    };

    let outcome = run_tool_loop_with_clock(
        &transport,
        &mut executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert_eq!(executor.calls.len(), 1);
    assert_eq!(executor.calls[0].id, "call_a");
    assert_eq!(outcome.observability.tool_call_count, 1);
}

// ----- Direct OpenAI-compatible transport ------------------------------------

fn blank_binding() -> CapabilityBinding {
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

fn direct_context() -> AiContext {
    AiContext {
        bindings: AiBindings {
            embed: blank_binding(),
            audio_transcribe: blank_binding(),
            audio_translate: blank_binding(),
            vision_extract: blank_binding(),
            text_generate: blank_binding(),
        },
        tuning: AiTuning {
            max_concurrency: 2,
            keep_alive: None,
        },
        limiter: AiLimiter::new(2),
        project_id: None,
    }
}

fn request_body_json(raw: &str) -> Value {
    let body = raw.split("\r\n\r\n").nth(1).expect("request has a body");
    serde_json::from_str(body).expect("request body is JSON")
}

#[test]
fn direct_transport_sends_tools_and_parses_tool_calls() {
    let response = r#"{"model":"local-model","choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"search","arguments":"{\"query\":\"foo\"}"}}]}}]}"#;
    let (api_base, handle) = spawn_json_response(response).expect("spawn test server");
    let context = direct_context();
    let target = DirectGenerationTarget {
        api_base: Some(api_base),
        api_key: Some("sk-test".to_string()),
        model: Some("local-model".to_string()),
        provider: Some("lm-studio".to_string()),
        reasoning_effort: None,
    };
    let transport =
        DirectChatTransport::new(&context, target, Some("feature_high".to_string())).unwrap();
    let tools = vec![ToolSchema {
        name: "search".to_string(),
        description: "search repo".to_string(),
        parameters: json!({"type":"object"}),
    }];
    let messages = vec![ChatMessage::system("system"), ChatMessage::user("find foo")];
    let request = ChatCompletionRequest {
        messages: &messages,
        tools: &tools,
        max_tokens: Some(128),
        tool_choice: ToolChoice::Auto,
    };

    let completion = transport.complete(request).expect("completion");

    let raw = handle.join().unwrap().unwrap();
    let body = request_body_json(&raw);
    assert_eq!(body["model"], "local-model");
    assert_eq!(body["max_tokens"], 128);
    assert_eq!(body["tools"][0]["function"]["name"], "search");
    assert_eq!(body["tool_choice"], "auto");
    assert_eq!(body["messages"][0]["role"], "system");
    assert_eq!(body["messages"][1]["content"], "find foo");
    assert!(raw.lines().any(|line| {
        line.to_ascii_lowercase().starts_with("authorization:") && line.contains("Bearer sk-test")
    }));

    assert!(completion.content.is_none());
    assert_eq!(completion.tool_calls.len(), 1);
    assert_eq!(completion.tool_calls[0].name, "search");
    assert_eq!(completion.tool_calls[0].id, "call_1");
    assert_eq!(completion.tool_calls[0].arguments["query"], "foo");
    assert_eq!(completion.finish_reason.as_deref(), Some("tool_calls"));
    assert_eq!(completion.model.as_deref(), Some("local-model"));

    assert_eq!(transport.route(), "direct");
    assert_eq!(transport.profile(), Some("feature_high"));
    assert_eq!(transport.provider(), Some("lm-studio"));
    assert_eq!(transport.model(), Some("local-model"));
}

#[test]
fn direct_transport_rejects_malformed_chat_completion_through_tool_loop() {
    for response in [
        r#"{"model":"local-model","choices":[{"finish_reason":"stop"}]}"#,
        r#"{"model":"local-model","choices":[{"finish_reason":"stop","message":"bad"}]}"#,
    ] {
        let (api_base, handle) = spawn_json_response(response).expect("spawn test server");
        let context = direct_context();
        let target = DirectGenerationTarget {
            api_base: Some(api_base),
            api_key: Some("sk-test".to_string()),
            model: Some("local-model".to_string()),
            provider: Some("lm-studio".to_string()),
            reasoning_effort: None,
        };
        let transport =
            DirectChatTransport::new(&context, target, Some("feature_high".to_string())).unwrap();
        let mut executor = EchoExecutor::new("unused");

        let error = run_tool_loop(
            &transport,
            &mut executor,
            vec![ChatMessage::user("go")],
            &ToolLoopLimits::default(),
            None,
        )
        .expect_err("malformed response fails");

        assert!(matches!(error, AiError::ParseFailure { .. }));
        handle.join().unwrap().unwrap();
        assert!(executor.calls.is_empty());
    }
}

#[test]
fn parse_completion_reads_plain_content_and_usage() {
    let value: Value = serde_json::from_str(
        r#"{"model":"m","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":3,"completion_tokens":5,"total_tokens":8}}"#,
    )
    .unwrap();
    let completion = parse_completion(&value).expect("completion parses");
    assert_eq!(completion.content.as_deref(), Some("hello"));
    assert!(completion.tool_calls.is_empty());
    assert_eq!(completion.finish_reason.as_deref(), Some("stop"));
    assert_eq!(
        completion.usage.and_then(|usage| usage.token_count()),
        Some(8)
    );
}

#[test]
fn parse_completion_defaults_malformed_tool_arguments_to_null() {
    let value = json!({
        "model": "m",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": "{bad json",
                    },
                }],
            },
        }],
    });

    let completion = parse_completion(&value).expect("valid tool call parses");

    assert_eq!(completion.tool_calls.len(), 1);
    assert_eq!(completion.tool_calls[0].arguments, Value::Null);
}

#[test]
fn build_request_body_suppresses_tools_for_lane_a() {
    let target = DirectGenerationTarget {
        model: Some("m".to_string()),
        ..DirectGenerationTarget::default()
    };
    let messages = vec![ChatMessage::user("hi")];
    let request = ChatCompletionRequest {
        messages: &messages,
        tools: &[],
        max_tokens: None,
        tool_choice: ToolChoice::Auto,
    };
    let body = build_request_body(&target, &request);
    assert!(body.get("tools").is_none());
    assert!(body.get("tool_choice").is_none());
    assert_eq!(body["model"], "m");
    assert_eq!(body["messages"][0]["role"], "user");
    assert_eq!(body["messages"][0]["content"], "hi");
}

#[test]
fn build_request_body_serializes_required_tool_choice() {
    let target = DirectGenerationTarget {
        model: Some("m".to_string()),
        ..DirectGenerationTarget::default()
    };
    let tools = vec![ToolSchema {
        name: "outline_file".to_string(),
        description: "outline a file".to_string(),
        parameters: json!({"type":"object"}),
    }];
    let messages = vec![ChatMessage::user("map the crate")];
    let request = ChatCompletionRequest {
        messages: &messages,
        tools: &tools,
        max_tokens: None,
        tool_choice: ToolChoice::Required,
    };
    let body = build_request_body(&target, &request);
    assert_eq!(body["tool_choice"], "required");
}

#[test]
fn build_request_body_threads_reasoning_effort() {
    let messages = vec![ChatMessage::user("hi")];
    let request = ChatCompletionRequest {
        messages: &messages,
        tools: &[],
        max_tokens: None,
        tool_choice: ToolChoice::Auto,
    };

    // Present -> forwarded so direct Lane A/B keep their profile reasoning pin.
    let target = DirectGenerationTarget {
        model: Some("m".to_string()),
        reasoning_effort: Some("high".to_string()),
        ..DirectGenerationTarget::default()
    };
    let body = build_request_body(&target, &request);
    assert_eq!(body["reasoning_effort"], "high");

    // Absent / blank -> key omitted entirely.
    let unset = DirectGenerationTarget {
        model: Some("m".to_string()),
        reasoning_effort: Some("   ".to_string()),
        ..DirectGenerationTarget::default()
    };
    assert!(
        build_request_body(&unset, &request)
            .get("reasoning_effort")
            .is_none()
    );
    let none = DirectGenerationTarget {
        model: Some("m".to_string()),
        ..DirectGenerationTarget::default()
    };
    assert!(
        build_request_body(&none, &request)
            .get("reasoning_effort")
            .is_none()
    );
}

#[test]
fn build_daemon_chat_body_forwards_profile_project_and_tools() {
    let tools = vec![ToolSchema {
        name: "outline_file".to_string(),
        description: "outline a file".to_string(),
        parameters: json!({"type":"object"}),
    }];
    let messages = vec![ChatMessage::user("map the crate")];
    let request = ChatCompletionRequest {
        messages: &messages,
        tools: &tools,
        max_tokens: Some(512),
        tool_choice: ToolChoice::Auto,
    };

    let body = build_daemon_chat_body("feature_high", Some("project-9"), Some("high"), &request);
    assert_eq!(body["profile"], "feature_high");
    assert_eq!(body["project_id"], "project-9");
    assert_eq!(body["reasoning_effort"], "high");
    assert_eq!(body["max_tokens"], 512);
    assert_eq!(body["tool_choice"], "auto");
    assert_eq!(body["tools"][0]["function"]["name"], "outline_file");
    // The daemon resolves the profile to a provider/model; none is pinned here.
    assert!(body.get("model").is_none());
    assert!(body.get("provider").is_none());

    // Unset project_id / reasoning_effort are omitted, and Lane A style empty
    // tools forward neither tools nor tool_choice.
    let lane_a = ChatCompletionRequest {
        messages: &messages,
        tools: &[],
        max_tokens: None,
        tool_choice: ToolChoice::Auto,
    };
    let body = build_daemon_chat_body("feature_high", None, None, &lane_a);
    assert_eq!(body["profile"], "feature_high");
    assert!(body.get("project_id").is_none());
    assert!(body.get("reasoning_effort").is_none());
    assert!(body.get("tools").is_none());
    assert!(body.get("tool_choice").is_none());
}

// ----- daemon-side agentic narrative generation ------------------------------

fn daemon_agentic_context(project_id: Option<&str>) -> AiContext {
    AiContext {
        bindings: AiBindings {
            embed: blank_binding(),
            audio_transcribe: blank_binding(),
            audio_translate: blank_binding(),
            vision_extract: blank_binding(),
            text_generate: blank_binding(),
        },
        tuning: AiTuning {
            max_concurrency: 1,
            keep_alive: None,
        },
        limiter: AiLimiter::new(1),
        project_id: project_id.map(str::to_string),
    }
}

/// Points the daemon dial URL at a stub server and stages a local CLI token
/// under a temp `GOBBY_HOME`, restoring all four env vars on drop. Mirrors the
/// daemon test harness so `daemon_agentic_chat` is exercised end to end over the
/// shared stub HTTP server.
struct DaemonEnvGuard {
    _lock: MutexGuard<'static, ()>,
    home: Option<OsString>,
    gobby_home: Option<OsString>,
    daemon_url: Option<OsString>,
    port: Option<OsString>,
}

impl DaemonEnvGuard {
    fn set(daemon_url: &str, home: &Path, token: &str) -> Self {
        let guard = Self {
            _lock: TEST_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
            home: std::env::var_os("HOME"),
            gobby_home: std::env::var_os("GOBBY_HOME"),
            daemon_url: std::env::var_os("GOBBY_DAEMON_URL"),
            port: std::env::var_os("GOBBY_PORT"),
        };
        fs::write(home.join("local_cli_token"), format!("{token}\n")).unwrap();
        // SAFETY: env mutation is serialized through TEST_ENV_LOCK, held for the
        // guard's lifetime; Drop restores the originals while still holding it.
        // GOBBY_PORT is cleared so a stray value cannot mask GOBBY_DAEMON_URL.
        unsafe {
            std::env::set_var("HOME", home);
            std::env::set_var("GOBBY_HOME", home);
            std::env::set_var("GOBBY_DAEMON_URL", daemon_url);
            std::env::remove_var("GOBBY_PORT");
        }
        guard
    }
}

impl Drop for DaemonEnvGuard {
    fn drop(&mut self) {
        // SAFETY: see DaemonEnvGuard::set — restoration holds TEST_ENV_LOCK.
        unsafe {
            for (name, value) in [
                ("HOME", &self.home),
                ("GOBBY_HOME", &self.gobby_home),
                ("GOBBY_DAEMON_URL", &self.daemon_url),
                ("GOBBY_PORT", &self.port),
            ] {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

#[test]
fn daemon_agentic_chat_posts_once_and_parses_narrative_and_investigation() {
    // The daemon runs its own agent loop and returns the FINAL narrative plus
    // investigation provenance: a single POST under the local CLI token, no
    // tools/tool_choice/model passthrough, and no per-turn tool-call response.
    let response = r##"{"model":"claude-opus","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"# Architecture\n\nGrounded narrative citing crates/foo/src/lib.rs:12."}}],"investigation":{"tool_use_count":7,"turns":4,"tools":{"Read":5,"Grep":2}},"usage":{"input_tokens":1200,"output_tokens":800,"total_tokens":2000}}"##;
    let (api_base, handle) = spawn_json_response(response).expect("spawn test server");
    let home = tempfile::tempdir().expect("temp home");
    let _env = DaemonEnvGuard::set(&api_base, home.path(), "agentic-token");
    let context = daemon_agentic_context(Some("project-7"));
    let messages = vec![
        ChatMessage::system("page system + daemon directive"),
        ChatMessage::user("Write the architecture page for crates/foo."),
    ];

    let result = daemon_agentic_chat(
        &context,
        "feature_high",
        "/abs/repo",
        &messages,
        Some(60),
        Some("high"),
    )
    .expect("agentic chat succeeds");

    let raw = handle.join().unwrap().unwrap();
    assert!(raw.starts_with("POST /api/llm/chat/completions HTTP/1.1"));
    assert!(raw.lines().any(|line| {
        line.to_ascii_lowercase()
            .starts_with("x-gobby-local-token:")
            && line.contains("agentic-token")
    }));
    let body = request_body_json(&raw);
    assert_eq!(body["profile"], "feature_high");
    assert_eq!(body["project_id"], "project-7");
    assert_eq!(body["project_path"], "/abs/repo");
    assert_eq!(body["max_turns"], 60);
    assert_eq!(body["reasoning_effort"], "high");
    assert_eq!(body["messages"][0]["role"], "system");
    assert_eq!(
        body["messages"][1]["content"],
        "Write the architecture page for crates/foo."
    );
    // Daemon-side agentic: no tool-call passthrough, no pinned model.
    assert!(body.get("tools").is_none());
    assert!(body.get("tool_choice").is_none());
    assert!(body.get("model").is_none());

    assert_eq!(
        result.content.as_deref(),
        Some("# Architecture\n\nGrounded narrative citing crates/foo/src/lib.rs:12.")
    );
    assert_eq!(result.model.as_deref(), Some("claude-opus"));
    assert_eq!(result.tool_use_count, 7);
    assert_eq!(result.turns, 4);
    assert_eq!(
        result.usage.and_then(|usage| usage.token_count()),
        Some(2000)
    );
}

#[test]
fn daemon_agentic_chat_defaults_missing_investigation_and_omits_unset_fields() {
    let response = r#"{"model":"claude-opus","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"body"}}]}"#;
    let (api_base, handle) = spawn_json_response(response).expect("spawn test server");
    let home = tempfile::tempdir().expect("temp home");
    let _env = DaemonEnvGuard::set(&api_base, home.path(), "agentic-token");
    let context = daemon_agentic_context(None);
    let messages = vec![ChatMessage::user("seed")];

    let result = daemon_agentic_chat(&context, "feature_high", "/abs/repo", &messages, None, None)
        .expect("agentic chat succeeds");

    let raw = handle.join().unwrap().unwrap();
    let body = request_body_json(&raw);
    assert_eq!(body["project_path"], "/abs/repo");
    assert!(body.get("project_id").is_none());
    assert!(body.get("max_turns").is_none());
    assert!(body.get("reasoning_effort").is_none());

    assert_eq!(result.content.as_deref(), Some("body"));
    assert_eq!(result.tool_use_count, 0);
    assert_eq!(result.turns, 0);
    assert!(result.usage.is_none());
}
