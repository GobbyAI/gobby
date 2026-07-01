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

    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string(), "outline".to_string()],
        allow_mutation: false,
    };
    let result = daemon_agentic_chat(
        &context,
        "feature_high",
        "/abs/repo",
        &tool_policy,
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

    let tool_policy = ToolPolicy {
        cli: "gcode".to_string(),
        tools: vec!["search".to_string()],
        allow_mutation: false,
    };
    let result = daemon_agentic_chat(
        &context,
        "feature_high",
        "/abs/repo",
        &tool_policy,
        &messages,
        None,
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
    assert!(body.get("reasoning_effort").is_none());

    assert_eq!(result.content.as_deref(), Some("body"));
    assert_eq!(result.tool_use_count, 0);
    assert_eq!(result.turns, 0);
    assert!(result.usage.is_none());
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
        " \t ",
        "/repo",
        &tool_policy,
        &messages,
        None,
        None,
    )
    .expect_err("blank profile rejected");

    assert!(matches!(error, AiError::NotConfigured { .. }), "{error}");
    assert!(error.to_string().contains("profile is required"), "{error}");
}
