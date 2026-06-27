use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::collections::VecDeque;
use std::time::Duration;

use serde_json::{Value, json};

use super::tool_loop::run_tool_loop_with_clock;
use super::transport::{build_request_body, parse_completion};
use super::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatRole, ChatTransport,
    DirectChatTransport, DirectGenerationTarget, FEATURE_HIGH, FEATURE_LOW, FEATURE_MID,
    GenerationTier, StopReason, ToolCall, ToolError, ToolExecutor, ToolLoopLimits, ToolSchema,
    profile_for_tier, resolve_direct_generation_target, run_tool_loop,
};
use crate::ai_context::{AiBindings, AiContext, AiLimiter};
use crate::ai_types::{AiError, TokenUsage};
use crate::config::{AiRouting, AiTuning, CapabilityBinding, ConfigSource, ai_keys};
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
}

impl StubTransport {
    fn new(completions: Vec<ChatCompletion>) -> Self {
        Self {
            completions: RefCell::new(completions.into()),
            requests: RefCell::new(Vec::new()),
        }
    }
}

impl ChatTransport for StubTransport {
    fn complete(&self, request: ChatCompletionRequest<'_>) -> Result<ChatCompletion, AiError> {
        self.requests.borrow_mut().push(request.messages.to_vec());
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
    let transport = StubTransport::new(vec![content_completion("done")]);
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
fn parse_completion_reads_plain_content_and_usage() {
    let value: Value = serde_json::from_str(
        r#"{"model":"m","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":3,"completion_tokens":5,"total_tokens":8}}"#,
    )
    .unwrap();
    let completion = parse_completion(&value);
    assert_eq!(completion.content.as_deref(), Some("hello"));
    assert!(completion.tool_calls.is_empty());
    assert_eq!(completion.finish_reason.as_deref(), Some("stop"));
    assert_eq!(
        completion.usage.and_then(|usage| usage.token_count()),
        Some(8)
    );
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
    };
    let body = build_request_body(&target, &request);
    assert!(body.get("tools").is_none());
    assert!(body.get("tool_choice").is_none());
    assert_eq!(body["model"], "m");
    assert_eq!(body["messages"][0]["role"], "user");
    assert_eq!(body["messages"][0]["content"], "hi");
}
