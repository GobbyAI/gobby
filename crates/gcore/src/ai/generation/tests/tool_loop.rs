use super::common::*;

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
