use super::*;
use crate::config::FeatureCandidate;

#[test]
fn forwards_provider_model_and_optional_project_id() {
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
    )
    .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(request.starts_with("POST /api/llm/generate HTTP/1.1"));
    assert_eq!(body["provider"], "local:lm-studio");
    assert_eq!(body["model"], "qwen/qwen3.6-35b-a3b");
    assert_eq!(body["project_id"], "project-123");
    assert_eq!(body["prompt"], "Write a title");
    assert_eq!(body["system_prompt"], "Be brief");
    assert!(body.get("system").is_none());
    assert!(body.get("profile").is_none());
    assert_eq!(body["max_tokens"], 64);
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

    assert_eq!(body["provider"], "local:lm-studio");
    assert_eq!(body["model"], "qwen/qwen3.6-35b-a3b");
    assert!(body.get("project_id").is_none());
    assert!(body.get("profile").is_none());
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
fn configured_binding_profile_replaces_feature_low_default() {
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

    assert_eq!(body["profile"], "feature_high");
    assert!(body.get("provider").is_none());
    assert!(body.get("model").is_none());
}

#[test]
fn forwards_candidates_and_reasoning_effort_from_binding() {
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

    let result =
        generate_via_daemon_with_max_tokens(&cfg, "Use candidates", None, None, None).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["prompt"], "Use candidates");
    assert!(body.get("profile").is_none());
    assert_eq!(body["reasoning_effort"], "medium");
    assert_eq!(
        body["candidates"],
        serde_json::json!([
            {"candidate":"codex/gpt-5.5","reasoning_effort":"high"},
            {"candidate":"droid/qwen3.6"}
        ])
    );
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

    generate_via_daemon_with_max_tokens(&cfg, "Override profile", None, None, Some("feature_mid"))
        .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["profile"], "feature_mid");
}

#[test]
fn explicit_provider_model_suppresses_profile_override() {
    let (port, request) = spawn_server(r#"{"text":"ok"}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "text-token");
    let cfg = test_context(None);

    generate_via_daemon_with_max_tokens(&cfg, "Explicit routing", None, None, Some("feature_mid"))
        .unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["provider"], "daemon-provider");
    assert_eq!(body["model"], "daemon-model");
    assert!(body.get("profile").is_none());
}
