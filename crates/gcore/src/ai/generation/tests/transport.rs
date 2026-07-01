use super::common::*;

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
