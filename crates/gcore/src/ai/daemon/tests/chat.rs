use super::*;
use crate::ai::generation::{
    ChatCompletionRequest, ChatMessage, ChatTransport, DaemonChatTransport, ToolChoice, ToolSchema,
};
use serde_json::json;

/// One tool-call round-trip on the daemon route: the transport POSTs the
/// OpenAI-shaped messages/tools to `/api/llm/chat/completions` under the local
/// CLI token with the resolved `profile`/`project_id`/`reasoning_effort`, and
/// parses the daemon's OpenAI-shaped tool-call response.
#[test]
fn daemon_chat_transport_round_trips_a_tool_call() {
    let response = r#"{"model":"claude-opus","choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"search_code","arguments":"{\"query\":\"hooks\"}"}}]}}],"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}"#;
    let (port, request) = spawn_server(response);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "chat-token");

    let mut cfg = test_context(Some("project-123"));
    // ToolChat reuses the text_generate binding, so the reasoning pin flows
    // from there.
    cfg.bindings.text_generate.reasoning_effort = Some("high".to_string());

    let transport = DaemonChatTransport::new(&cfg, "feature_high").expect("transport builds");
    let tools = vec![ToolSchema {
        name: "search_code".to_string(),
        description: "search the code index".to_string(),
        parameters: json!({"type":"object"}),
    }];
    let messages = vec![
        ChatMessage::system("system"),
        ChatMessage::user("investigate hooks"),
    ];
    let req = ChatCompletionRequest {
        messages: &messages,
        tools: &tools,
        max_tokens: Some(256),
        tool_choice: ToolChoice::Auto,
    };

    let completion = transport.complete(req).expect("completion");

    let raw = request.join().unwrap().unwrap();
    let body = request_body_json(&raw);

    assert!(raw.starts_with("POST /api/llm/chat/completions HTTP/1.1"));
    assert!(has_header(&raw, "X-Gobby-Local-Token", "chat-token"));
    assert_eq!(body["profile"], "feature_high");
    assert_eq!(body["project_id"], "project-123");
    assert_eq!(body["max_tokens"], 256);
    assert_eq!(body["reasoning_effort"], "high");
    assert_eq!(body["tool_choice"], "auto");
    assert_eq!(body["tools"][0]["function"]["name"], "search_code");
    assert_eq!(body["messages"][0]["role"], "system");
    assert_eq!(body["messages"][1]["content"], "investigate hooks");
    // The daemon owns tool-capable provider/model selection for the profile.
    assert!(body.get("model").is_none());
    assert!(body.get("provider").is_none());

    assert_eq!(transport.route(), "daemon");
    assert_eq!(transport.profile(), Some("feature_high"));
    assert!(completion.content.is_none());
    assert_eq!(completion.tool_calls.len(), 1);
    assert_eq!(completion.tool_calls[0].name, "search_code");
    assert_eq!(completion.tool_calls[0].arguments["query"], "hooks");
    assert_eq!(completion.finish_reason.as_deref(), Some("tool_calls"));
    assert_eq!(completion.model.as_deref(), Some("claude-opus"));
    assert_eq!(
        completion.usage.and_then(|usage| usage.token_count()),
        Some(15)
    );
}

/// A missing `project_id` and `reasoning_effort` are simply omitted, and Lane A
/// style empty-tools requests forward neither `tools` nor `tool_choice`.
#[test]
fn daemon_chat_transport_omits_unset_fields() {
    let response = r#"{"model":"m","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"done"}}]}"#;
    let (port, request) = spawn_server(response);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "chat-token");

    let cfg = test_context(None);
    let transport = DaemonChatTransport::new(&cfg, "feature_high").expect("transport builds");
    let messages = vec![ChatMessage::user("hello")];
    let req = ChatCompletionRequest {
        messages: &messages,
        tools: &[],
        max_tokens: None,
        tool_choice: ToolChoice::Auto,
    };

    let completion = transport.complete(req).expect("completion");

    let raw = request.join().unwrap().unwrap();
    let body = request_body_json(&raw);
    assert_eq!(body["profile"], "feature_high");
    assert!(body.get("project_id").is_none());
    assert!(body.get("reasoning_effort").is_none());
    assert!(body.get("max_tokens").is_none());
    assert!(body.get("tools").is_none());
    assert!(body.get("tool_choice").is_none());
    assert_eq!(completion.content.as_deref(), Some("done"));
}
