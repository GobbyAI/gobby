use super::*;
use crate::config::FeatureCandidate;

#[test]
fn daemon_profile_request_omits_standalone_binding_fields() {
    let (port, request) = spawn_server(
        r#"{"text":"ok","model":"qwen/qwen3.6-35b-a3b","usage":{"input_tokens":3,"output_tokens":4,"total_tokens":7}}"#,
    );
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(Some("project-123"));
    cfg.bindings.text_generate.provider = Some("local:lm-studio".to_string());
    cfg.bindings.text_generate.model = Some("qwen/qwen3.6-35b-a3b".to_string());

    let result = generate_via_daemon_with_max_tokens(
        &cfg,
        "Write a title",
        Some("Be brief"),
        Some(64),
        None,
        GenerationBudget::Interactive,
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(request.starts_with("POST /api/llm/generate HTTP/1.1"));
    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert_eq!(body["project_id"], "project-123");
    assert_eq!(body["prompt"], "Write a title");
    assert_eq!(body["system_prompt"], "Be brief");
    assert!(body.get("system").is_none());
    assert_eq!(body["profile"], "feature_low");
    assert_eq!(body["max_tokens"], 64);
    assert_eq!(body["candidate_timeout_seconds"], 30);
    assert_eq!(body["cli_candidate_timeout_seconds"], 60);
    assert_eq!(body["total_timeout_seconds"], 1200);
    assert_eq!(result.text, "ok");
    assert_eq!(
        result.usage.as_ref().and_then(|usage| usage.token_count()),
        Some(7)
    );

    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(None);
    cfg.bindings.text_generate.provider = Some("local:lm-studio".to_string());
    cfg.bindings.text_generate.model = Some("qwen/qwen3.6-35b-a3b".to_string());

    generate_via_daemon(&cfg, "No project", None).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert!(body.get("project_id").is_none());
    assert_eq!(body["profile"], "feature_low");
}

#[test]
fn text_generation_defaults_to_feature_low_without_provider_model() {
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(Some("project-123"));
    cfg.bindings.text_generate.provider = None;
    cfg.bindings.text_generate.model = None;

    generate_via_daemon(&cfg, "No provider", Some("Be brief")).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["prompt"], "No provider");
    assert_eq!(body["system_prompt"], "Be brief");
    assert_eq!(body["profile"], "feature_low");
    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert_eq!(body["project_id"], "project-123");
}

#[test]
fn configured_binding_profile_does_not_override_daemon_feature_profile() {
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(None);
    cfg.bindings.text_generate.provider = None;
    cfg.bindings.text_generate.model = None;
    cfg.bindings.text_generate.profile = Some("feature_high".to_string());

    generate_via_daemon(&cfg, "Configured profile", None).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["profile"], "feature_low");
    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
}

#[test]
fn lopsided_standalone_binding_is_ignored_by_daemon_routing() {
    // A direct-route binding (api_base + model, no provider) forced onto the
    // daemon route must not be forwarded as explicit routing: the daemon 400s
    // a lone model ("provider and model must be supplied together"), which
    // degraded whole codewiki runs to AST-only pages (#17778). The partial
    // pair is dropped and profile routing wins.
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(Some("project-123"));
    cfg.bindings.text_generate.provider = None;
    cfg.bindings.text_generate.model = Some("qwen2.5-vl-7b-instruct".to_string());

    generate_via_daemon(&cfg, "Model without provider", None).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert_eq!(body["profile"], "feature_low");

    // Provider-only is the mirror image; incidental binding profile data is
    // also isolated from the daemon request.
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(None);
    cfg.bindings.text_generate.provider = Some("local:lm-studio".to_string());
    cfg.bindings.text_generate.model = None;
    cfg.bindings.text_generate.profile = Some("feature_mid".to_string());

    generate_via_daemon(&cfg, "Provider without model", None).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert_eq!(body["profile"], "feature_low");
}

#[test]
fn pinned_one_shot_forwards_explicit_candidates_and_omits_profile() {
    use crate::ai::generation::{GenerationTier, generate_one_shot_pinned};
    use crate::config::AiRouting;

    let (port, request) = spawn_server(r#"{"text":"ok","model":"claude-sonnet"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(Some("project-123"));
    // A configured binding profile/provider/model must not leak into a pinned
    // call: the explicit chain supersedes the whole binding.
    cfg.bindings.text_generate.provider = Some("local:lm-studio".to_string());
    cfg.bindings.text_generate.model = Some("qwen/qwen3.6-35b-a3b".to_string());
    cfg.bindings.text_generate.profile = Some("feature_low".to_string());
    let candidates = vec![
        FeatureCandidate {
            candidate: "claude/sonnet".to_string(),
            reasoning_effort: Some("xhigh".to_string()),
        },
        FeatureCandidate {
            candidate: "codex/gpt-5.5".to_string(),
            reasoning_effort: None,
        },
    ];

    let result = generate_one_shot_pinned(
        &cfg,
        AiRouting::Daemon,
        GenerationTier::Aggregate,
        &candidates,
        "Pinned prompt",
        Some("Be brief"),
        Some(64),
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(request.starts_with("POST /api/llm/generate HTTP/1.1"));
    assert_eq!(
        body["candidates"],
        serde_json::json!([
            {"candidate":"claude/sonnet","reasoning_effort":"xhigh"},
            {"candidate":"codex/gpt-5.5"}
        ])
    );
    assert!(body.get("profile").is_none());
    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert!(body.get("reasoning_effort").is_none());
    assert_eq!(body["prompt"], "Pinned prompt");
    assert_eq!(body["system_prompt"], "Be brief");
    assert_eq!(body["max_tokens"], 64);
    assert_eq!(body["project_id"], "project-123");
    // Aggregate-tier candidates get the whole total budget (#18288) — opus/gpt
    // aggregate pages generate for minutes and must not die on the tight
    // interactive per-candidate budgets.
    assert_eq!(body["candidate_timeout_seconds"], 1200);
    assert_eq!(body["cli_candidate_timeout_seconds"], 1200);
    assert_eq!(body["total_timeout_seconds"], 1200);
    assert_eq!(result.text, "ok");
}

#[test]
fn daemon_profile_request_ignores_incidental_candidates_and_reasoning_pin() {
    let (port, request) = spawn_server(r#"{"text":"ok","applied_reasoning_effort":"high"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(None);
    cfg.bindings.text_generate.provider = None;
    cfg.bindings.text_generate.model = None;
    cfg.bindings.text_generate.candidates = Some(vec![
        FeatureCandidate {
            candidate: "codex/gpt-5.5".to_string(),
            reasoning_effort: Some("high".to_string()),
        },
        FeatureCandidate {
            candidate: "droid/qwen3.6".to_string(),
            reasoning_effort: None,
        },
    ]);
    cfg.bindings.text_generate.reasoning_effort = Some("medium".to_string());

    let result = generate_via_daemon_with_max_tokens(
        &cfg,
        "Use candidates",
        None,
        None,
        None,
        GenerationBudget::Interactive,
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["prompt"], "Use candidates");
    assert_eq!(body["profile"], "feature_low");
    assert!(body.get("reasoning_effort").is_none());
    assert!(body.get("candidates").is_none());
    assert_eq!(result.applied_reasoning_effort.as_deref(), Some("high"));
}

#[test]
fn per_call_profile_overrides_configured_binding_profile() {
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let mut cfg = test_context(None);
    cfg.bindings.text_generate.provider = None;
    cfg.bindings.text_generate.model = None;
    cfg.bindings.text_generate.profile = Some("feature_high".to_string());

    generate_via_daemon_with_max_tokens(
        &cfg,
        "Override profile",
        None,
        None,
        Some("feature_mid"),
        GenerationBudget::Interactive,
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["profile"], "feature_mid");
}

#[test]
fn per_call_profile_is_the_only_daemon_routing_override() {
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let cfg = test_context(None);

    generate_via_daemon_with_max_tokens(
        &cfg,
        "Explicit routing",
        None,
        None,
        Some("feature_mid"),
        GenerationBudget::Interactive,
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
    assert_eq!(body["profile"], "feature_mid");
}
