use super::common::*;

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
        tool_loop_limits: ToolLoopLimits::default(),
        project_id: project_id.map(str::to_string),
        grant: None,
    }
}

fn spawn_agentic_response(response: impl Into<String>) -> std::io::Result<(String, RequestHandle)> {
    let response = response.into();
    let mut response: Value = serde_json::from_str(&response).expect("valid response fixture");
    spawn_json_response_from_request(move |raw| {
        let request = request_body_json(raw);
        let investigation = response
            .as_object_mut()
            .expect("response fixture is an object")
            .entry("investigation")
            .or_insert_with(|| json!({}));
        let investigation = investigation
            .as_object_mut()
            .expect("investigation fixture is an object");
        investigation.insert("caller".to_string(), request["caller"].clone());
        investigation.insert("request_id".to_string(), request["request_id"].clone());
        response.to_string()
    })
}

/// Points the daemon dial URL at a stub server and stages a local CLI token
/// under a temp `GOBBY_HOME`, restoring all scoped env vars on drop. Mirrors the
/// daemon test harness so `daemon_agentic_chat` is exercised end to end over the
/// shared stub HTTP server.
struct DaemonEnvGuard {
    _lock: MutexGuard<'static, ()>,
    home: Option<OsString>,
    gobby_home: Option<OsString>,
    daemon_url: Option<OsString>,
    port: Option<OsString>,
    agent_api_token: Option<OsString>,
    agent_run_id: Option<OsString>,
    managed_execution_id: Option<OsString>,
    project_id: Option<OsString>,
    session_id: Option<OsString>,
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
            agent_api_token: std::env::var_os("GOBBY_AGENT_API_TOKEN"),
            agent_run_id: std::env::var_os("GOBBY_AGENT_RUN_ID"),
            managed_execution_id: std::env::var_os("GOBBY_MANAGED_EXECUTION_ID"),
            project_id: std::env::var_os("GOBBY_PROJECT_ID"),
            session_id: std::env::var_os("GOBBY_SESSION_ID"),
        };
        fs::write(home.join("local_cli_token"), format!("{token}\n")).unwrap();
        // SAFETY: env mutation is serialized through TEST_ENV_LOCK, held for the
        // guard's lifetime; Drop restores the originals while still holding it.
        // GOBBY_PORT is cleared so a stray value cannot mask GOBBY_DAEMON_URL.
        unsafe {
            std::env::set_var("HOME", home);
            std::env::set_var("GOBBY_HOME", home);
            std::env::set_var("GOBBY_DAEMON_URL", daemon_url);
            std::env::set_var("GOBBY_AGENT_API_TOKEN", token);
            std::env::remove_var("GOBBY_AGENT_RUN_ID");
            std::env::set_var("GOBBY_MANAGED_EXECUTION_ID", "tool-execution-7");
            std::env::set_var("GOBBY_PROJECT_ID", "project-7");
            std::env::set_var("GOBBY_SESSION_ID", "session-7");
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
                ("GOBBY_AGENT_API_TOKEN", &self.agent_api_token),
                ("GOBBY_AGENT_RUN_ID", &self.agent_run_id),
                ("GOBBY_MANAGED_EXECUTION_ID", &self.managed_execution_id),
                ("GOBBY_PROJECT_ID", &self.project_id),
                ("GOBBY_SESSION_ID", &self.session_id),
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
    let response = r##"{"model":"claude-opus","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"# Architecture\n\nGrounded narrative citing crates/foo/src/lib.rs:12."}}],"investigation":{"tool_use_count":7,"turns":4,"stop_reason":"max_turns","tools":{"Read":5,"Grep":2}},"usage":{"input_tokens":1200,"output_tokens":800,"total_tokens":2000}}"##;
    let (api_base, handle) = spawn_agentic_response(response).expect("spawn test server");
    let home = tempfile::tempdir().expect("temp home");
    let _env = DaemonEnvGuard::set(&api_base, home.path(), "agentic-token");
    let mut context = daemon_agentic_context(Some("project-7"));
    context.tool_loop_limits.max_turns = Some(60);
    let messages = vec![
        ChatMessage::system("page system + daemon directive"),
        ChatMessage::user("Write the architecture page for crates/foo."),
    ];

    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string(), "outline".to_string()],
        allow_mutation: false,
    };
    let result = daemon_agentic_chat(
        &context,
        "test.agentic",
        "feature_high",
        None,
        "/abs/repo",
        &tool_policy,
        &messages,
        &context.tool_loop_limits,
        Some("high"),
    )
    .expect("agentic chat succeeds");

    let raw = handle.join().unwrap().unwrap();
    assert!(raw.starts_with("POST /api/llm/chat/completions HTTP/1.1"));
    assert!(raw.lines().any(|line| {
        line.to_ascii_lowercase().starts_with("authorization:")
            && line.contains("Bearer agentic-token")
    }));
    assert!(raw.lines().any(|line| {
        line.eq_ignore_ascii_case("X-Gobby-Managed-Execution-Id: tool-execution-7")
    }));
    assert!(
        raw.lines()
            .any(|line| line.eq_ignore_ascii_case("X-Gobby-Caller-Project-Id: project-7"))
    );
    assert!(
        raw.lines()
            .any(|line| line.eq_ignore_ascii_case("X-Gobby-Session-Id: session-7"))
    );
    let body = request_body_json(&raw);
    assert_eq!(body["caller"], "test.agentic");
    let request_id = body["request_id"].as_str().expect("request UUID string");
    let request_uuid = uuid::Uuid::parse_str(request_id).expect("request UUID parses");
    assert_eq!(request_uuid.get_version_num(), 4);
    assert_eq!(body["profile"], "feature_high");
    assert!(body.get("project_id").is_none());
    assert_eq!(body["project_path"], "/abs/repo");
    assert_eq!(body["limits"]["max_turns"], 60);
    assert_eq!(body["limits"]["max_tool_calls"], 30);
    assert_eq!(body["limits"]["max_bytes_per_tool_result"], 16_384);
    assert_eq!(body["limits"]["tool_timeout_seconds"], 300);
    assert_eq!(body["limits"]["loop_timeout_seconds"], 1_200);
    assert!(body.get("max_turns").is_none());
    assert_eq!(body["reasoning_effort"], "high");
    assert_eq!(body["messages"][0]["role"], "system");
    assert_eq!(
        body["messages"][1]["content"],
        "Write the architecture page for crates/foo."
    );
    // The caller's read-only policy reaches the daemon, which builds the tools
    // from it (the daemon route REQUIRES tool_policy).
    assert_eq!(body["tool_policy"]["cli"], "gcode");
    assert_eq!(body["tool_policy"]["tools"][0], "search");
    assert_eq!(body["tool_policy"]["tools"][1], "outline");
    assert_eq!(body["tool_policy"]["allow_mutation"], false);
    // Daemon-side agentic: no raw tool-call passthrough, no pinned model — the
    // daemon owns tool-schema construction and provider/model selection.
    assert!(body.get("tools").is_none());
    assert!(body.get("tool_choice").is_none());
    assert!(body.get("model").is_none());

    assert_eq!(
        result.content.as_deref(),
        Some("# Architecture\n\nGrounded narrative citing crates/foo/src/lib.rs:12.")
    );
    assert_eq!(result.model.as_deref(), Some("claude-opus"));
    assert_eq!(result.caller, "test.agentic");
    assert_eq!(result.request_id, request_id);
    assert_eq!(result.tool_use_count, 7);
    assert_eq!(result.turns, Some(4));
    assert_eq!(result.stop_reason.as_deref(), Some("max_turns"));
    assert_eq!(
        result.usage.and_then(|usage| usage.token_count()),
        Some(2000)
    );
}

#[test]
fn daemon_agentic_chat_defaults_missing_optional_investigation_and_omits_unset_fields() {
    let response = r#"{"model":"claude-opus","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"body"}}]}"#;
    let (api_base, handle) = spawn_agentic_response(response).expect("spawn test server");
    let home = tempfile::tempdir().expect("temp home");
    let _env = DaemonEnvGuard::set(&api_base, home.path(), "agentic-token");
    let context = daemon_agentic_context(None);
    let messages = vec![ChatMessage::user("seed")];

    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string()],
        allow_mutation: false,
    };
    let result = daemon_agentic_chat(
        &context,
        "test.agentic",
        "feature_high",
        None,
        "/abs/repo",
        &tool_policy,
        &messages,
        &context.tool_loop_limits,
        None,
    )
    .expect("agentic chat succeeds");

    let raw = handle.join().unwrap().unwrap();
    let body = request_body_json(&raw);
    assert_eq!(body["project_path"], "/abs/repo");
    // The policy is always present even when optional fields are omitted.
    assert_eq!(body["tool_policy"]["cli"], "gcode");
    assert_eq!(body["tool_policy"]["tools"][0], "search");
    assert_eq!(body["tool_policy"]["allow_mutation"], false);
    assert!(body.get("project_id").is_none());
    assert!(body.get("max_turns").is_none());
    assert_eq!(body["limits"]["max_turns"], Value::Null);
    assert!(body.get("reasoning_effort").is_none());

    assert_eq!(result.content.as_deref(), Some("body"));
    assert_eq!(result.tool_use_count, 0);
    assert_eq!(result.turns, None);
    assert_eq!(result.stop_reason, None);
    assert!(result.usage.is_none());
}

#[test]
fn parse_daemon_agentic_preserves_null_investigation_provenance() {
    let result = parse_daemon_agentic(&json!({
        "investigation": {
            "caller": "test.agentic",
            "request_id": "019fc08a-1d63-4b23-bbc8-659d56bc4168",
            "turns": null,
            "stop_reason": null,
            "tool_use_count": null
        }
    }))
    .expect("correlated investigation parses");

    assert_eq!(result.turns, None);
    assert_eq!(result.stop_reason, None);
    assert_eq!(result.tool_use_count, 0);
}

#[test]
fn daemon_agentic_chat_rejects_blank_profile_before_daemon_setup() {
    let context = daemon_agentic_context(Some("project-1"));
    let messages = vec![ChatMessage::user("investigate")];
    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string()],
        allow_mutation: false,
    };

    let error = daemon_agentic_chat(
        &context,
        "test.agentic",
        " \t ",
        None,
        "/repo",
        &tool_policy,
        &messages,
        &context.tool_loop_limits,
        None,
    )
    .expect_err("blank profile rejected");

    assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    assert!(error.to_string().contains("profile is required"), "{error}");
}

#[test]
fn daemon_agentic_chat_rejects_blank_caller_before_daemon_setup() {
    let context = daemon_agentic_context(Some("project-1"));
    let messages = vec![ChatMessage::user("investigate")];
    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string()],
        allow_mutation: false,
    };

    let error = daemon_agentic_chat(
        &context,
        " \t ",
        "feature_high",
        None,
        "/repo",
        &tool_policy,
        &messages,
        &context.tool_loop_limits,
        None,
    )
    .expect_err("blank caller rejected");

    assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    assert!(error.to_string().contains("caller is required"), "{error}");
}

#[test]
fn daemon_agentic_chat_pinned_candidates_supersede_profile() {
    // An explicit candidate chain (--ai-aggregate-candidate) pins the exact
    // provider/model sequence: the body carries `candidates` and omits
    // `profile`, mirroring the one-shot /api/llm/generate precedence.
    let response = r#"{"model":"claude-sonnet","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"pinned narrative"}}]}"#;
    let (api_base, handle) = spawn_agentic_response(response).expect("spawn test server");
    let home = tempfile::tempdir().expect("temp home");
    let _env = DaemonEnvGuard::set(&api_base, home.path(), "agentic-token");
    let context = daemon_agentic_context(Some("project-7"));
    let messages = vec![ChatMessage::user("seed")];
    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string()],
        allow_mutation: false,
    };
    let candidates = vec![
        FeatureCandidate {
            candidate: "claude/sonnet".to_string(),
            reasoning_effort: Some("xhigh".to_string()),
        },
        FeatureCandidate {
            candidate: "codex/gpt-5.6-sol".to_string(),
            reasoning_effort: None,
        },
    ];

    let result = daemon_agentic_chat(
        &context,
        "test.agentic",
        "feature_high",
        Some(&candidates),
        "/abs/repo",
        &tool_policy,
        &messages,
        &context.tool_loop_limits,
        None,
    )
    .expect("agentic chat succeeds");

    let raw = handle.join().unwrap().unwrap();
    let body = request_body_json(&raw);
    assert!(body.get("profile").is_none());
    assert_eq!(
        body["candidates"],
        serde_json::json!([
            {"candidate":"claude/sonnet","reasoning_effort":"xhigh"},
            {"candidate":"codex/gpt-5.6-sol"}
        ])
    );
    assert_eq!(result.content.as_deref(), Some("pinned narrative"));
}

#[test]
fn daemon_agentic_chat_empty_candidates_fall_back_to_profile() {
    // An empty chain is "not pinned": the profile is required and forwarded.
    let response = r#"{"model":"m","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"body"}}]}"#;
    let (api_base, handle) = spawn_agentic_response(response).expect("spawn test server");
    let home = tempfile::tempdir().expect("temp home");
    let _env = DaemonEnvGuard::set(&api_base, home.path(), "agentic-token");
    let context = daemon_agentic_context(None);
    let messages = vec![ChatMessage::user("seed")];
    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string()],
        allow_mutation: false,
    };

    daemon_agentic_chat(
        &context,
        "test.agentic",
        "feature_high",
        Some(&[]),
        "/abs/repo",
        &tool_policy,
        &messages,
        &context.tool_loop_limits,
        None,
    )
    .expect("agentic chat succeeds");

    let raw = handle.join().unwrap().unwrap();
    let body = request_body_json(&raw);
    assert_eq!(body["profile"], "feature_high");
    assert!(body.get("candidates").is_none());
}
