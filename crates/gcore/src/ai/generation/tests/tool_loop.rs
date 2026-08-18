use super::common::*;
use std::sync::atomic::{AtomicBool, Ordering};

struct MapConfigSource {
    values: BTreeMap<String, String>,
}

impl ConfigSource for MapConfigSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        Ok(value.to_string())
    }
}

fn config_source(values: &[(&str, &str)]) -> MapConfigSource {
    MapConfigSource {
        values: values
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect(),
    }
}

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
fn tool_loop_executes_tool_then_completes_with_observability() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({"text":"hi"})),
        content_completion("final answer"),
    ]);
    let executor = Arc::new(EchoExecutor::new("ECHO:hi"));
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        executor.clone(),
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
    let calls = executor.calls.lock().expect("calls lock");
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].name, "echo");
    assert_eq!(calls[0].arguments["text"], "hi");

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
fn tool_loop_forces_tool_use_on_first_turn_then_auto() {
    // Turn 0 forces a tool call (`required`) so a weak function-calling model
    // cannot one-shot an ungrounded answer and skip investigation entirely;
    // every later turn lets the model finalize freely (`auto`).
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({"text": "hi"})),
        content_completion("final answer"),
    ]);
    let executor = Arc::new(EchoExecutor::new("ECHO:hi"));
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        executor,
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
fn tool_loop_reprompts_a_model_that_ignores_required_tool_choice() {
    // A runtime may treat `tool_choice = "required"` as best-effort: the model
    // answers turn 0 with no tool call. The loop must not accept that
    // uninvestigated reply — it appends a correction and keeps forcing until a
    // tool call lands, so the tool loop genuinely investigates before answering.
    let transport = StubTransport::new(vec![
        content_completion("premature one-shot answer"),
        tool_call_completion("echo", "call_echo", json!({"text": "hi"})),
        content_completion("grounded final answer"),
    ]);
    let executor = Arc::new(EchoExecutor::new("ECHO:hi"));
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        executor,
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
fn tool_loop_hard_fails_when_the_model_never_investigates_after_corrections() {
    // A model that never calls a tool, even after repeated corrections, must
    // not spin forever and must not ship a silent uninvestigated one-shot:
    // the forcing retries are bounded, after which the loop fails (a non-
    // Completed stop reason → callers hard-fail the page, no skeleton).
    let stubborn = (0..=MAX_FORCED_INVESTIGATION_RETRIES)
        .map(|_| content_completion("one-shot answer"))
        .collect();
    let transport = StubTransport::new(stubborn);
    let executor = Arc::new(EchoExecutor::new("unused"));
    let messages = vec![
        ChatMessage::system("system prompt"),
        ChatMessage::user("write docs"),
    ];

    let outcome = run_tool_loop(
        &transport,
        executor,
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
fn tool_loop_relays_tool_error_to_model() {
    struct FailingExecutor;
    impl ToolExecutor for FailingExecutor {
        fn schemas(&self) -> Vec<ToolSchema> {
            vec![ToolSchema {
                name: "boom".to_string(),
                description: "always fails".to_string(),
                parameters: json!({"type":"object"}),
            }]
        }
        fn execute(&self, _call: &ToolCall) -> Result<String, ToolError> {
            Err(ToolError::new("not found"))
        }
    }

    let transport = StubTransport::new(vec![
        tool_call_completion("boom", "call_boom", json!({})),
        content_completion("recovered"),
    ]);
    let executor = Arc::new(FailingExecutor);
    let outcome = run_tool_loop(
        &transport,
        executor,
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
fn tool_loop_aggregates_token_usage_across_turns() {
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
    let executor = Arc::new(EchoExecutor::new("r"));
    let outcome = run_tool_loop(
        &transport,
        executor,
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
fn tool_loop_reports_no_usage_when_unreported() {
    // Neither the investigation turn nor the final turn reports usage.
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({"text": "r"})),
        content_completion("done"),
    ]);
    let executor = Arc::new(EchoExecutor::new("r"));
    let outcome = run_tool_loop(
        &transport,
        executor,
        vec![ChatMessage::user("go")],
        &ToolLoopLimits::default(),
        None,
    )
    .expect("loop runs");
    assert!(outcome.total_usage.is_none());
}

#[test]
fn tool_loop_stops_at_max_turns() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "a", json!({})),
        tool_call_completion("echo", "b", json!({})),
    ]);
    let executor = Arc::new(EchoExecutor::new("r"));
    let limits = ToolLoopLimits {
        max_turns: Some(2),
        max_tool_calls: 100,
        ..ToolLoopLimits::default()
    };
    let outcome = run_tool_loop(
        &transport,
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::MaxTurns);
    assert!(outcome.content.is_none());
    assert_eq!(outcome.observability.turns, 2);
    assert_eq!(outcome.observability.tool_call_count, 1);
}

#[test]
fn tool_loop_allows_unlimited_turns() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "a", json!({})),
        tool_call_completion("echo", "b", json!({})),
        tool_call_completion("echo", "c", json!({})),
        content_completion("done"),
    ]);
    let executor = Arc::new(EchoExecutor::new("r"));
    let limits = ToolLoopLimits {
        max_turns: None,
        max_tool_calls: 3,
        ..ToolLoopLimits::default()
    };

    let outcome = run_tool_loop(
        &transport,
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    assert_eq!(outcome.content.as_deref(), Some("done"));
    assert_eq!(outcome.observability.turns, 4);
    assert_eq!(outcome.observability.tool_call_count, 3);
}

#[test]
fn tool_loop_stops_at_max_tool_calls() {
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
    let executor = Arc::new(EchoExecutor::new("r"));
    let limits = ToolLoopLimits {
        max_turns: Some(100),
        max_tool_calls: 3,
        ..ToolLoopLimits::default()
    };
    let outcome = run_tool_loop(
        &transport,
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::MaxToolCalls);
    assert_eq!(outcome.observability.tool_call_count, 3);
    assert_eq!(outcome.observability.turns, 2);
}

const OVERSIZE_HEAD: &str = "UNIQUE_BODY_PREFIX_7f3a9c_";
const OVERSIZE_TAIL: &str = "_UNIQUE_BODY_SUFFIX_7f3a9c";

fn oversized_tool_body() -> String {
    format!("{OVERSIZE_HEAD}{}{OVERSIZE_TAIL}", "x".repeat(20_000))
}

fn tool_message_content(transport: &StubTransport) -> String {
    let requests = transport.requests.borrow();
    requests
        .iter()
        .flat_map(|turn| turn.iter())
        .find(|message| message.role == ChatRole::Tool)
        .and_then(|message| message.content.clone())
        .expect("tool result present")
}

fn run_echo_loop(
    result: &str,
    max_bytes: usize,
    artifact_dir: Option<&std::path::Path>,
) -> (StubTransport, String) {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "call_echo", json!({})),
        content_completion("done"),
    ]);
    let executor = Arc::new(EchoExecutor::new(result));
    let limits = ToolLoopLimits {
        max_bytes_per_tool_result: max_bytes,
        ..ToolLoopLimits::default()
    };
    let context = ToolLoopRunContext {
        artifact_dir: artifact_dir.map(std::path::Path::to_path_buf),
    };
    let outcome = run_tool_loop_with_context(
        &transport,
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        &context,
    )
    .expect("loop runs");
    assert_eq!(outcome.stop_reason, StopReason::Completed);
    let content = tool_message_content(&transport);
    (transport, content)
}

#[test]
fn tool_loop_under_cap_result_is_unchanged_and_writes_no_sidecar() {
    let dir = tempfile::tempdir().expect("artifact dir");
    let result = "hello-under-cap";
    let (_transport, content) = run_echo_loop(result, 64, Some(dir.path()));
    assert_eq!(content, result);
    let entries: Vec<_> = fs::read_dir(dir.path())
        .expect("read artifact dir")
        .collect();
    assert!(
        entries.is_empty(),
        "under-cap results must not write a sidecar"
    );
}

#[test]
fn tool_loop_over_cap_with_artifact_dir_writes_full_sidecar_and_pointer_only_message() {
    let dir = tempfile::tempdir().expect("artifact dir");
    let result = oversized_tool_body();
    let prefix = result[..64].to_string();
    let (_transport, content) = run_echo_loop(&result, 64, Some(dir.path()));

    assert!(
        !content.contains(OVERSIZE_HEAD),
        "tool message must not contain the body prefix: {content}"
    );
    assert!(
        !content.contains(OVERSIZE_TAIL),
        "tool message must not contain the body suffix: {content}"
    );
    assert!(
        !content.contains(&prefix),
        "tool message must not contain a prefix of the body: {content}"
    );
    assert!(
        content.contains(&result.len().to_string()),
        "pointer must include byte count: {content}"
    );
    assert!(
        content.contains("sidecar") && content.contains("re-query"),
        "pointer must tell the model to read the sidecar or re-query: {content}"
    );

    let entries: Vec<_> = fs::read_dir(dir.path())
        .expect("read artifact dir")
        .map(|entry| entry.expect("sidecar entry").path())
        .collect();
    assert_eq!(entries.len(), 1, "expected exactly one sidecar");
    let sidecar = &entries[0];
    let stored = fs::read(sidecar).expect("read sidecar");
    assert_eq!(stored, result.as_bytes());
    assert!(
        content.contains(&sidecar.display().to_string()),
        "pointer must include the sidecar path: {content}"
    );
}

#[test]
fn tool_loop_over_cap_without_artifact_dir_is_pointer_text_only() {
    let result = oversized_tool_body();
    let prefix = result[..64].to_string();
    let (_transport, content) = run_echo_loop(&result, 64, None);

    assert!(
        !content.contains(OVERSIZE_HEAD),
        "tool message must not contain the body prefix: {content}"
    );
    assert!(
        !content.contains(&prefix),
        "tool message must not contain a prefix of the body: {content}"
    );
    assert!(
        content.contains(&result.len().to_string()),
        "pointer must include byte count: {content}"
    );
    assert!(
        content.contains("re-query"),
        "pointer must tell the model to re-query: {content}"
    );
    assert!(
        !content.contains("written to"),
        "without artifact_dir the pointer must not claim a sidecar path: {content}"
    );
}

#[test]
fn tool_loop_stops_at_timeout() {
    let transport = StubTransport::new(vec![
        tool_call_completion("echo", "a", json!({})),
        tool_call_completion("echo", "b", json!({})),
    ]);
    let executor = Arc::new(EchoExecutor::new("r"));
    let limits = ToolLoopLimits {
        loop_timeout_seconds: 1,
        max_turns: Some(100),
        max_tool_calls: 100,
        ..ToolLoopLimits::default()
    };

    // Scripted clock: proceed once (0ms), then exceed the one-second budget.
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_secs(2),
        Duration::from_secs(2),
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
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert_eq!(outcome.observability.turns, 1);
    assert_eq!(outcome.observability.elapsed_ms, 2_000);
}

#[test]
fn tool_loop_content_completion_times_out_after_transport() {
    let transport = StubTransport::new(vec![content_completion("late answer")]);
    let executor = Arc::new(EchoExecutor::new("unused"));
    let limits = ToolLoopLimits {
        loop_timeout_seconds: 1,
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_secs(2),
        Duration::from_secs(2),
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
        executor.clone(),
        vec![ChatMessage::user("go")],
        &limits,
        None,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert!(outcome.content.is_none());
    assert_eq!(outcome.observability.turns, 1);
    assert!(executor.calls.lock().expect("calls lock").is_empty());
}

#[test]
fn tool_loop_tool_call_completion_times_out_after_transport_without_executing_tools() {
    let transport = StubTransport::new(vec![tool_call_completion("echo", "a", json!({}))]);
    let executor = Arc::new(EchoExecutor::new("unused"));
    let limits = ToolLoopLimits {
        loop_timeout_seconds: 1,
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_secs(2),
        Duration::from_secs(2),
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
        executor.clone(),
        vec![ChatMessage::user("go")],
        &limits,
        None,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert!(executor.calls.lock().expect("calls lock").is_empty());
    assert_eq!(outcome.observability.tool_call_count, 0);
}

#[test]
fn tool_loop_times_out_after_first_tool_result_before_next_tool_call() {
    let transport = StubTransport::new(vec![two_tool_call_completion()]);
    let executor = Arc::new(EchoExecutor::new("r"));
    let limits = ToolLoopLimits {
        loop_timeout_seconds: 1,
        max_tool_calls: 100,
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::from_millis(0),
        Duration::from_millis(0),
        Duration::from_millis(0),
        Duration::from_secs(2),
        Duration::from_secs(2),
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
        executor.clone(),
        vec![ChatMessage::user("go")],
        &limits,
        None,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    let calls = executor.calls.lock().expect("calls lock");
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].id, "call_a");
    assert_eq!(outcome.observability.tool_call_count, 1);
}

struct SlowExecutor {
    completed: Arc<AtomicBool>,
    sleep_for: Duration,
}

impl ToolExecutor for SlowExecutor {
    fn schemas(&self) -> Vec<ToolSchema> {
        vec![ToolSchema {
            name: "slow".to_string(),
            description: "sleeps".to_string(),
            parameters: json!({"type":"object"}),
        }]
    }

    fn execute(&self, _call: &ToolCall) -> Result<String, ToolError> {
        std::thread::sleep(self.sleep_for);
        self.completed.store(true, Ordering::SeqCst);
        Ok("late result".to_string())
    }
}

#[test]
fn tool_timeout_is_recoverable_and_worker_drains_after_loop_continues() {
    let transport = StubTransport::new(vec![
        tool_call_completion("slow", "slow-call", json!({})),
        content_completion("recovered"),
    ]);
    let completed = Arc::new(AtomicBool::new(false));
    let executor = Arc::new(SlowExecutor {
        completed: Arc::clone(&completed),
        sleep_for: Duration::from_millis(2_000),
    });
    let limits = ToolLoopLimits {
        max_turns: Some(3),
        tool_timeout_seconds: 1,
        loop_timeout_seconds: 8,
        ..ToolLoopLimits::default()
    };

    let outcome = run_tool_loop(
        &transport,
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    assert_eq!(outcome.content.as_deref(), Some("recovered"));
    assert_eq!(outcome.observability.tool_call_count, 1);
    assert!(!completed.load(Ordering::SeqCst));
    let requests = transport.requests.borrow();
    let timeout_result = requests[1]
        .iter()
        .find(|message| message.role == ChatRole::Tool)
        .and_then(|message| message.content.as_deref())
        .expect("timeout result is relayed to the model");
    assert!(timeout_result.contains("timed out after 1 seconds"));
    drop(requests);

    for _ in 0..150 {
        if completed.load(Ordering::SeqCst) {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(completed.load(Ordering::SeqCst));
}

#[test]
fn remaining_loop_budget_precedes_longer_tool_timeout() {
    let transport = StubTransport::new(vec![tool_call_completion("slow", "slow-call", json!({}))]);
    let completed = Arc::new(AtomicBool::new(false));
    let executor = Arc::new(SlowExecutor {
        completed: Arc::clone(&completed),
        sleep_for: Duration::from_millis(50),
    });
    let limits = ToolLoopLimits {
        tool_timeout_seconds: 300,
        loop_timeout_seconds: 1,
        ..ToolLoopLimits::default()
    };
    let elapsed = [
        Duration::ZERO,
        Duration::ZERO,
        Duration::from_millis(990),
        Duration::from_secs(1),
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
        executor,
        vec![ChatMessage::user("go")],
        &limits,
        None,
        None,
        clock,
    )
    .expect("loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Timeout);
    assert_eq!(outcome.observability.tool_call_count, 0);
    assert!(!completed.load(Ordering::SeqCst));
}

#[test]
fn tool_loop_contract_defaults_and_complete_overrides_resolve() {
    let defaults = ToolLoopLimits::resolve(&mut config_source(&[])).expect("defaults resolve");
    assert_eq!(defaults.max_turns, None);
    assert_eq!(defaults.max_tool_calls, 30);
    assert_eq!(defaults.max_bytes_per_tool_result, 16_384);
    assert_eq!(defaults.tool_timeout_seconds, 300);
    assert_eq!(defaults.loop_timeout_seconds, 1_200);

    let overrides = ToolLoopLimits::resolve(&mut config_source(&[
        ("ai.generation.tool_loop.max_turns", "7"),
        ("ai.generation.tool_loop.max_tool_calls", "8"),
        ("ai.generation.tool_loop.max_bytes_per_tool_result", "9"),
        ("ai.generation.tool_loop.tool_timeout_seconds", "10"),
        ("ai.generation.tool_loop.loop_timeout_seconds", "11"),
    ]))
    .expect("complete overrides resolve");
    assert_eq!(
        overrides,
        ToolLoopLimits {
            max_turns: Some(7),
            max_tool_calls: 8,
            max_bytes_per_tool_result: 9,
            tool_timeout_seconds: 10,
            loop_timeout_seconds: 11,
        }
    );
}

#[test]
fn tool_loop_contract_rejects_every_non_null_zero_limit() {
    for suffix in [
        "max_turns",
        "max_tool_calls",
        "max_bytes_per_tool_result",
        "tool_timeout_seconds",
        "loop_timeout_seconds",
    ] {
        let key = format!("ai.generation.tool_loop.{suffix}");
        let mut source = config_source(&[(&key, "0")]);
        let error = ToolLoopLimits::resolve(&mut source).expect_err("zero is invalid");
        assert!(
            error.to_string().contains(&key),
            "error should identify {key}: {error}"
        );
    }
}

// ----- Direct OpenAI-compatible transport ------------------------------------
