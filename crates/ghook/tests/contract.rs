use std::{
    fs, io,
    io::{Read, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use serde_json::Value;

type TestResult = Result<(), Box<dyn std::error::Error>>;

const VALID_STDIN: &str = r#"{"session_id":"test-session","transcript_path":"/tmp/test.jsonl"}"#;

#[test]
fn malformed_stdin_uses_cli_specific_json_error_contract() -> TestResult {
    for (cli, hook_type, expected_exit) in [
        ("claude", "session-start", 2),
        ("codex", "SessionStart", 2),
        ("qwen", "SessionStart", 2),
        ("qwen", "Stop", 2),
        ("qwen", "PreToolUse", 1),
        ("droid", "SessionStart", 1),
        ("grok", "session_start", 2),
        ("agy", "SessionStart", 2),
    ] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let daemon_url = closed_local_url()?;

        let output = run_ghook_with_dirs(
            home.path(),
            gobby_home.path(),
            Some(cli),
            Some(hook_type),
            &daemon_url,
            "not json",
            &[],
        )?;

        assert_eq!(
            output.status.code(),
            Some(expected_exit),
            "{cli} malformed stdin exited unexpectedly"
        );
        assert_json_stdout(&output, serde_json::json!({}))?;
        assert_stderr_empty(&output, cli)?;
    }

    Ok(())
}

#[test]
fn malformed_stdin_ignores_closed_stdout_pipe() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;
    let mut command = Command::new(env!("CARGO_BIN_EXE_ghook"));
    command
        .arg("--gobby-owned")
        .args(["--cli", "codex", "--type", "SessionStart"])
        .env("HOME", home.path())
        .env("GOBBY_HOME", gobby_home.path())
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_HOOKS_DISABLED")
        .env_remove("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS")
        .env_remove("GOBBY_SOURCE")
        .env_remove("CLAUDE_CODE_ENTRYPOINT")
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command.spawn()?;
    drop(child.stdout.take());
    if let Some(mut child_stdin) = child.stdin.take() {
        child_stdin.write_all(b"not json")?;
    }
    let output = child.wait_with_output()?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_stderr_empty(&output, "malformed stdin closed stdout")?;

    Ok(())
}

#[test]
fn missing_cli_or_type_prints_empty_json_and_exits_two() -> TestResult {
    for (cli, hook_type) in [(None, Some("SessionStart")), (Some("codex"), None)] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let daemon_url = closed_local_url()?;

        let output = run_ghook_with_dirs(
            home.path(),
            gobby_home.path(),
            cli,
            hook_type,
            &daemon_url,
            VALID_STDIN,
            &[],
        )?;

        assert_eq!(output.status.code(), Some(2));
        assert_json_stdout(&output, serde_json::json!({}))?;
        assert_stderr_empty(&output, "missing flag")?;
    }

    Ok(())
}

#[test]
fn unsupported_cli_exits_two_before_dispatch_side_effects() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("unsupported"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;

    assert_eq!(output.status.code(), Some(2));
    assert_json_stdout(&output, serde_json::json!({}))?;
    assert_stderr_empty(&output, "unsupported CLI")?;
    assert!(
        !gobby_home.path().join("hooks").exists(),
        "unsupported CLI invocations must not create an inbox"
    );

    Ok(())
}

#[test]
fn hooks_disabled_short_circuits_before_dispatch_side_effects() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[("GOBBY_HOOKS_DISABLED", "1")],
    )?;

    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::json!({}))?;
    assert_stderr_empty(&output, "disabled hooks")?;
    assert!(
        !gobby_home.path().join("hooks").exists(),
        "disabled hooks must not create an inbox"
    );

    Ok(())
}

#[test]
fn daemon_down_distinguishes_critical_and_noncritical_hooks() -> TestResult {
    for (cli, hook_type) in [
        ("codex", "Stop"),
        ("agy", "SessionStart"),
        ("grok", "session_start"),
        ("grok", "session_end"),
        ("grok", "pre_compact"),
        ("qwen", "SessionStart"),
        ("qwen", "SessionEnd"),
        ("qwen", "PreCompact"),
        ("qwen", "Stop"),
    ] {
        let critical = run_with_closed_daemon(cli, hook_type)?;
        assert_eq!(
            critical.status.code(),
            Some(2),
            "{cli} {hook_type} should fail closed"
        );
        assert!(critical.stdout.is_empty());
        let critical_stderr = String::from_utf8(critical.stderr)?;
        assert!(
            critical_stderr.contains(&format!(
                "Daemon connection failed on critical hook '{hook_type}'"
            )),
            "{cli} {hook_type} should report critical daemon failure"
        );
    }

    for (cli, hook_type) in [
        ("agy", "Stop"),
        ("grok", "stop"),
        ("claude", "Stop"),
        ("droid", "Stop"),
        ("qwen", "PreToolUse"),
    ] {
        let noncritical = run_with_closed_daemon(cli, hook_type)?;
        assert_eq!(
            noncritical.status.code(),
            Some(1),
            "{cli} {hook_type} should fail open"
        );
        if cli == "droid" {
            assert!(noncritical.stdout.is_empty());
            assert!(String::from_utf8(noncritical.stderr)?.contains("Daemon unreachable"));
            continue;
        }
        assert_json_stdout(
            &noncritical,
            serde_json::json!({
                "status": "error",
                "message": "Daemon unreachable",
            }),
        )?;
        assert_stderr_empty(&noncritical, &format!("{cli} {hook_type} daemon-down"))?;
    }

    Ok(())
}

#[test]
fn diagnose_json_reports_terminal_criticality_contract() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;

    let critical =
        run_diagnose_with_dirs(home.path(), gobby_home.path(), "codex", "Stop", &daemon_url)?;
    assert_eq!(critical.status.code(), Some(0));
    assert_stderr_empty(&critical, "codex Stop diagnose")?;
    let critical_json: Value = serde_json::from_slice(&critical.stdout)?;
    assert_eq!(critical_json["schema_version"], 2);
    assert_eq!(critical_json["cli"], "codex");
    assert_eq!(critical_json["hook_type"], "Stop");
    assert_eq!(critical_json["source"], "codex");
    assert_eq!(critical_json["critical"], true);
    assert_eq!(critical_json["terminal_context_enabled"], true);
    assert_eq!(critical_json["cli_recognized"], true);
    assert_eq!(critical_json["recent_failure_count"], 0);
    assert_eq!(critical_json["recent_failures"], serde_json::json!([]));
    assert!(
        critical_json["failure_dir"]
            .as_str()
            .is_some_and(|path| path.ends_with("hooks/inbox/failures"))
    );

    for (cli, hook_type, terminal_context_enabled) in [
        ("agy", "PostInvocation", true),
        ("grok", "stop", true),
        ("claude", "Stop", true),
        ("droid", "Stop", true),
        ("qwen", "AfterAgent", true),
        ("qwen", "PreToolUse", false),
    ] {
        let noncritical =
            run_diagnose_with_dirs(home.path(), gobby_home.path(), cli, hook_type, &daemon_url)?;
        assert_eq!(noncritical.status.code(), Some(0));
        assert_stderr_empty(&noncritical, &format!("{cli} {hook_type} diagnose"))?;
        let noncritical_json: Value = serde_json::from_slice(&noncritical.stdout)?;
        assert_eq!(noncritical_json["cli"], cli);
        assert_eq!(noncritical_json["hook_type"], hook_type);
        assert_eq!(noncritical_json["source"], cli);
        assert_eq!(noncritical_json["critical"], false);
        assert_eq!(
            noncritical_json["terminal_context_enabled"],
            terminal_context_enabled
        );
        assert_eq!(noncritical_json["cli_recognized"], true);
    }

    Ok(())
}

#[test]
fn lifecycle_hooks_enqueue_exact_agent_run_identity() -> TestResult {
    const RUN_ID: &str = "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764";

    for (cli, hook_type) in [
        ("claude", "session-end"),
        ("codex", "Stop"),
        ("qwen", "AfterAgent"),
        ("agy", "PostInvocation"),
        ("grok", "session_end"),
        ("droid", "SessionEnd"),
    ] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let daemon_url = closed_local_url()?;
        let output = run_ghook_with_dirs_and_args(
            home.path(),
            gobby_home.path(),
            Some(cli),
            Some(hook_type),
            &daemon_url,
            VALID_STDIN,
            RunGhookExtras {
                env: &[("GOBBY_AGENT_RUN_ID", RUN_ID)],
                args: &["--enqueue-only"],
                cwd: None,
            },
        )?;

        assert!(output.status.success(), "{cli} {hook_type}");
        let envelope = read_single_inbox_envelope(gobby_home.path())?;
        assert_eq!(
            envelope["input_data"]["terminal_context"]["gobby_agent_run_id"], RUN_ID,
            "{cli} {hook_type}"
        );
        // The daemon's auth layer requires the run id header on hook posts
        // from managed runs; the envelope mirrors what ghook sends live.
        assert_eq!(
            envelope["headers"]["X-Gobby-Agent-Run-Id"], RUN_ID,
            "{cli} {hook_type}"
        );
    }

    Ok(())
}

#[test]
fn lifecycle_hook_preserves_provider_context_and_replaces_untrusted_run_id() -> TestResult {
    const RUN_ID: &str = "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764";
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;
    let input = r#"{
        "session_id": "test-session",
        "terminal_context": {
            "provider_field": "preserved",
            "gobby_agent_run_id": "8292b975-f848-42c4-a8a9-6b6e8b30ddc6"
        }
    }"#;

    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("Stop"),
        &daemon_url,
        input,
        RunGhookExtras {
            env: &[("GOBBY_AGENT_RUN_ID", RUN_ID)],
            args: &["--enqueue-only"],
            cwd: None,
        },
    )?;

    assert!(output.status.success());
    let envelope = read_single_inbox_envelope(gobby_home.path())?;
    let context = &envelope["input_data"]["terminal_context"];
    assert_eq!(context["provider_field"], "preserved");
    assert_eq!(context["gobby_agent_run_id"], RUN_ID);

    Ok(())
}

#[test]
fn tool_hook_does_not_enqueue_terminal_context() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;
    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("PreToolUse"),
        &daemon_url,
        VALID_STDIN,
        RunGhookExtras {
            env: &[("GOBBY_AGENT_RUN_ID", "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764")],
            args: &["--enqueue-only"],
            cwd: None,
        },
    )?;

    assert!(output.status.success());
    let envelope = read_single_inbox_envelope(gobby_home.path())?;
    assert!(envelope["input_data"].get("terminal_context").is_none());
    assert_eq!(
        envelope["headers"]["X-Gobby-Agent-Run-Id"],
        "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764"
    );

    Ok(())
}

#[test]
fn unmanaged_hook_returns_continue_without_side_effects() -> TestResult {
    for stdin in [VALID_STDIN, "not json"] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let cwd = tempfile::tempdir()?;
        let daemon_url = closed_local_url()?;
        let output = run_ghook_with_dirs_and_args(
            home.path(),
            gobby_home.path(),
            Some("codex"),
            Some("PreToolUse"),
            &daemon_url,
            stdin,
            RunGhookExtras {
                env: &[],
                args: &["--enqueue-only"],
                cwd: Some(cwd.path()),
            },
        )?;

        assert!(output.status.success());
        assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
        assert_stderr_empty(&output, "unmanaged hook")?;
        assert!(inbox_envelopes(gobby_home.path())?.is_empty());
        assert!(read_failure_artifacts(gobby_home.path())?.is_empty());
        assert!(!gobby_home.path().join("hooks/inbox/quarantine").exists());
    }

    Ok(())
}

#[test]
fn unmanaged_agy_skip_stdout_is_protojson_legal() -> TestResult {
    for (hook_type, expected) in [
        ("PreToolUse", serde_json::json!({"decision": "allow"})),
        ("PreInvocation", serde_json::json!({})),
        ("Stop", serde_json::json!({})),
    ] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let cwd = tempfile::tempdir()?;
        let output = run_ghook_with_dirs_and_args(
            home.path(),
            gobby_home.path(),
            Some("agy"),
            Some(hook_type),
            &closed_local_url()?,
            VALID_STDIN,
            RunGhookExtras {
                env: &[],
                args: &["--enqueue-only"],
                cwd: Some(cwd.path()),
            },
        )?;

        assert!(output.status.success(), "{hook_type}");
        assert_json_stdout(&output, expected.clone())?;
        assert_stderr_empty(&output, &format!("unmanaged agy {hook_type}"))?;
        assert!(
            inbox_envelopes(gobby_home.path())?.is_empty(),
            "{hook_type}"
        );
    }

    Ok(())
}

#[test]
fn agy_workspace_paths_manage_when_cwd_is_not_a_project() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let cwd = tempfile::tempdir()?;
    let project_root = tempfile::tempdir()?;
    fs::create_dir_all(project_root.path().join(".gobby"))?;
    fs::write(
        project_root.path().join(".gobby/project.json"),
        r#"{"id":"agy-workspace-project"}"#,
    )?;
    let stdin = serde_json::json!({
        "conversationId": "probe",
        "workspacePaths": [project_root.path()],
    })
    .to_string();

    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("agy"),
        Some("PreToolUse"),
        &closed_local_url()?,
        &stdin,
        RunGhookExtras {
            env: &[],
            args: &["--enqueue-only"],
            cwd: Some(cwd.path()),
        },
    )?;

    assert!(output.status.success());
    assert_json_stdout(&output, serde_json::json!({"decision": "allow"}))?;
    let envelope = read_single_inbox_envelope(gobby_home.path())?;
    assert_eq!(envelope["source"], "agy");
    assert_eq!(
        envelope["headers"]
            .get("X-Gobby-Project-Id")
            .and_then(Value::as_str),
        Some("agy-workspace-project")
    );

    Ok(())
}

#[test]
fn unmanaged_statusline_never_emits_hook_action_json() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let cwd = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;

    let with_downstream = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("claude"),
        Some("statusline"),
        &daemon_url,
        VALID_STDIN,
        RunGhookExtras {
            env: &[("GOBBY_STATUSLINE_DOWNSTREAM", "printf 'MY-STATUS'")],
            args: &[],
            cwd: Some(cwd.path()),
        },
    )?;

    assert!(with_downstream.status.success());
    assert_eq!(with_downstream.stdout, b"MY-STATUS");
    assert_stderr_empty(&with_downstream, "unmanaged statusline with downstream")?;

    let without_downstream = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("claude"),
        Some("statusline"),
        &daemon_url,
        VALID_STDIN,
        RunGhookExtras {
            env: &[],
            args: &[],
            cwd: Some(cwd.path()),
        },
    )?;

    assert!(without_downstream.status.success());
    assert!(without_downstream.stdout.is_empty());
    assert_stderr_empty(
        &without_downstream,
        "unmanaged statusline without downstream",
    )?;

    Ok(())
}

#[test]
fn malformed_project_markers_manage_and_fail_closed() -> TestResult {
    for marker in ["project.json", "gcode.json"] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let project_root = tempfile::tempdir()?;
        let nested = project_root.path().join("nested");
        fs::create_dir_all(project_root.path().join(".gobby"))?;
        fs::create_dir_all(&nested)?;
        fs::write(project_root.path().join(".gobby").join(marker), "{not json")?;

        let output = run_ghook_with_dirs_and_args(
            home.path(),
            gobby_home.path(),
            Some("codex"),
            Some("SessionStart"),
            &closed_local_url()?,
            "not json",
            RunGhookExtras {
                env: &[],
                args: &[],
                cwd: Some(&nested),
            },
        )?;

        assert_eq!(output.status.code(), Some(2), "{marker}");
        assert_json_stdout(&output, serde_json::json!({}))?;
        assert!(
            gobby_home.path().join("hooks/inbox/quarantine").exists(),
            "{marker}"
        );
    }

    Ok(())
}

#[test]
fn managed_identity_evidence_enqueues_outside_project_folders() -> TestResult {
    for (env, stdin, expected_project_id) in [
        (
            vec![("GOBBY_PROJECT_ID", "environment-project")],
            VALID_STDIN,
            Some("environment-project"),
        ),
        (
            vec![("GOBBY_SESSION_ID", "managed-session")],
            VALID_STDIN,
            None,
        ),
        (
            vec![("GOBBY_AGENT_RUN_ID", "managed-run")],
            VALID_STDIN,
            None,
        ),
        (
            vec![],
            r#"{"project_id":"payload-project","session_id":"provider-session"}"#,
            Some("payload-project"),
        ),
    ] {
        let home = tempfile::tempdir()?;
        let gobby_home = tempfile::tempdir()?;
        let cwd = tempfile::tempdir()?;
        let output = run_ghook_with_dirs_and_args(
            home.path(),
            gobby_home.path(),
            Some("codex"),
            Some("PreToolUse"),
            &closed_local_url()?,
            stdin,
            RunGhookExtras {
                env: &env,
                args: &["--enqueue-only"],
                cwd: Some(cwd.path()),
            },
        )?;

        assert!(output.status.success());
        let envelope = read_single_inbox_envelope(gobby_home.path())?;
        assert_eq!(
            envelope["headers"]
                .get("X-Gobby-Project-Id")
                .and_then(Value::as_str),
            expected_project_id
        );
    }

    Ok(())
}

#[test]
fn environment_project_id_overrides_filesystem_identity() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let project_root = tempfile::tempdir()?;
    fs::create_dir_all(project_root.path().join(".gobby"))?;
    fs::write(
        project_root.path().join(".gobby/project.json"),
        r#"{"id":"filesystem-project"}"#,
    )?;

    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("PreToolUse"),
        &closed_local_url()?,
        VALID_STDIN,
        RunGhookExtras {
            env: &[("GOBBY_PROJECT_ID", "environment-project")],
            args: &["--enqueue-only"],
            cwd: Some(project_root.path()),
        },
    )?;

    assert!(output.status.success());
    let envelope = read_single_inbox_envelope(gobby_home.path())?;
    assert_eq!(
        envelope["headers"]["X-Gobby-Project-Id"],
        "environment-project"
    );

    Ok(())
}

#[test]
fn managed_hook_without_agent_run_env_omits_agent_run_header() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;
    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("PreToolUse"),
        &daemon_url,
        VALID_STDIN,
        RunGhookExtras {
            env: &[],
            args: &["--enqueue-only"],
            cwd: None,
        },
    )?;

    assert!(output.status.success());
    let envelope = read_single_inbox_envelope(gobby_home.path())?;
    assert!(envelope["headers"].get("X-Gobby-Agent-Run-Id").is_none());

    Ok(())
}

#[test]
fn detached_noncritical_action_ignores_closed_stdout_pipe() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;
    let mut command = Command::new(env!("CARGO_BIN_EXE_ghook"));
    command
        .arg("--gobby-owned")
        .arg("--detach")
        .args(["--cli", "codex", "--type", "PreToolUse"])
        .env("HOME", home.path())
        .env("GOBBY_HOME", gobby_home.path())
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_HOOKS_DISABLED")
        .env_remove("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS")
        .env_remove("GOBBY_SOURCE")
        .env_remove("CLAUDE_CODE_ENTRYPOINT")
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command.spawn()?;
    drop(child.stdout.take());
    if let Some(mut child_stdin) = child.stdin.take() {
        child_stdin.write_all(VALID_STDIN.as_bytes())?;
    }
    let output = child.wait_with_output()?;

    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_stderr_empty(&output, "detached closed stdout")?;

    Ok(())
}

#[test]
fn daemon_success_maps_deny_and_block_bodies() -> TestResult {
    let deny_body = r#"{"decision":"deny","reason":"policy"}"#;
    let deny_response = http_ok_json(deny_body);
    let (deny_url, deny_daemon) = start_daemon(deny_response)?;
    let deny_output = run_temp_ghook("codex", "PreToolUse", &deny_url, VALID_STDIN, &[])?;
    let deny_request = join_daemon(deny_daemon)?;

    assert_eq!(deny_output.status.code(), Some(0));
    assert_json_stdout(&deny_output, serde_json::from_str(deny_body)?)?;
    assert_stderr_empty(&deny_output, "deny response")?;
    assert!(deny_request.contains("POST /api/hooks/execute HTTP/1.1"));
    assert!(deny_request.contains("\"hook_type\":\"PreToolUse\""));

    let block_body = r#"{"decision":"block","reason":"stop now"}"#;
    let block_response = http_ok_json(block_body);
    let (block_url, block_daemon) = start_daemon(block_response)?;
    let block_output = run_temp_ghook("codex", "Stop", &block_url, VALID_STDIN, &[])?;
    let block_request = join_daemon(block_daemon)?;

    assert_eq!(block_output.status.code(), Some(2));
    assert!(block_output.stdout.is_empty());
    let block_stderr = String::from_utf8(block_output.stderr)?;
    assert!(block_stderr.contains("stop now"));
    assert!(block_request.contains("\"hook_type\":\"Stop\""));

    let grok_stop_body = r#"{"decision":"block","reason":"stop grok"}"#;
    let grok_stop_response = http_ok_json(grok_stop_body);
    let (grok_stop_url, grok_stop_daemon) = start_daemon(grok_stop_response)?;
    let grok_stop_output = run_temp_ghook("grok", "stop", &grok_stop_url, VALID_STDIN, &[])?;
    let grok_stop_request = join_daemon(grok_stop_daemon)?;

    assert_eq!(grok_stop_output.status.code(), Some(2));
    assert!(grok_stop_output.stdout.is_empty());
    let grok_stop_stderr = String::from_utf8(grok_stop_output.stderr)?;
    assert!(grok_stop_stderr.contains("stop grok"));
    assert!(grok_stop_request.contains("\"hook_type\":\"stop\""));

    let grok_pre_tool_body = r#"{"decision":"block","reason":"policy"}"#;
    let grok_pre_tool_response = http_ok_json(grok_pre_tool_body);
    let (grok_pre_tool_url, grok_pre_tool_daemon) = start_daemon(grok_pre_tool_response)?;
    let grok_pre_tool_output =
        run_temp_ghook("grok", "pre_tool_use", &grok_pre_tool_url, VALID_STDIN, &[])?;
    let grok_pre_tool_request = join_daemon(grok_pre_tool_daemon)?;

    assert_eq!(grok_pre_tool_output.status.code(), Some(0));
    assert_json_stdout(
        &grok_pre_tool_output,
        serde_json::from_str(grok_pre_tool_body)?,
    )?;
    assert_stderr_empty(&grok_pre_tool_output, "grok pre_tool_use block")?;
    assert!(grok_pre_tool_request.contains("\"hook_type\":\"pre_tool_use\""));

    for (hook_type, body) in [
        (
            "SessionStart",
            r#"{"continue":true,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"ready"}}"#,
        ),
        (
            "PreToolUse",
            r#"{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"policy"}}"#,
        ),
        (
            "Stop",
            r#"{"continue":true,"decision":"block","reason":"keep working"}"#,
        ),
    ] {
        let (qwen_url, qwen_daemon) = start_daemon(http_ok_json(body))?;
        let qwen_output = run_temp_ghook("qwen", hook_type, &qwen_url, VALID_STDIN, &[])?;
        let qwen_request = join_daemon(qwen_daemon)?;

        assert_eq!(qwen_output.status.code(), Some(0), "qwen {hook_type}");
        assert_json_stdout(&qwen_output, serde_json::from_str(body)?)?;
        assert_stderr_empty(&qwen_output, &format!("qwen {hook_type}"))?;
        assert!(qwen_request.contains(&format!("\"hook_type\":\"{hook_type}\"")));
    }

    for hook_type in [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    ] {
        let body = r#"{"decision":"accept","reason":"ok"}"#;
        let (agy_url, agy_daemon) = start_daemon(http_ok_json(body))?;
        let agy_output = run_temp_ghook("agy", hook_type, &agy_url, VALID_STDIN, &[])?;
        let agy_request = join_daemon(agy_daemon)?;

        assert_eq!(agy_output.status.code(), Some(0), "{hook_type}");
        assert_json_stdout(&agy_output, serde_json::from_str(body)?)?;
        assert_stderr_empty(&agy_output, "agy accepted hook")?;
        assert!(agy_request.contains("\"source\":\"agy\""), "{hook_type}");
        assert!(
            agy_request.contains(&format!("\"hook_type\":\"{hook_type}\"")),
            "{hook_type}"
        );
    }

    Ok(())
}

#[test]
fn daemon_post_includes_local_machine_identity() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    fs::write(gobby_home.path().join("machine_id"), "machine-client\n")?;
    let body = r#"{"decision":"accept","reason":"ok"}"#;
    let (daemon_url, daemon) = start_daemon(http_ok_json(body))?;
    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("PreToolUse"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::from_str(body)?)?;
    assert_stderr_empty(&output, "machine identity success")?;
    assert!(request.contains("\"machine_id\":\"machine-client\""));
    assert!(request.contains("\"os\":\""));
    assert!(!request.contains("machine_id_error"));

    Ok(())
}

#[test]
fn daemon_post_uses_bootstrap_daemon_url_when_env_url_is_empty() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let body = r#"{"decision":"accept","reason":"ok"}"#;
    let (daemon_url, daemon) = start_daemon(http_ok_json(body))?;
    fs::write(
        gobby_home.path().join("bootstrap.yaml"),
        format!("daemon_url: {daemon_url}/\n"),
    )?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("PreToolUse"),
        "",
        VALID_STDIN,
        &[],
    )?;
    let request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::from_str(body)?)?;
    assert_stderr_empty(&output, "bootstrap daemon_url success")?;
    assert!(request.contains("\"source\":\"codex\""));

    Ok(())
}

#[test]
fn valid_daemon_success_removes_envelope_and_writes_no_failure() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let body = r#"{"decision":"accept","reason":"ok"}"#;
    let (daemon_url, daemon) = start_daemon(http_ok_json(body))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("PreToolUse"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::from_str(body)?)?;
    assert_stderr_empty(&output, "valid daemon success")?;
    assert!(
        inbox_envelopes(gobby_home.path())?.is_empty(),
        "valid parsed success should remove the queued envelope"
    );
    assert!(
        read_failure_artifacts(gobby_home.path())?.is_empty(),
        "valid parsed success should not write failure diagnostics"
    );

    Ok(())
}

#[test]
fn daemon_success_with_invalid_json_writes_failure_and_removes_envelope() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let (daemon_url, daemon) = start_daemon(http_ok_json("not json"))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("daemon 2xx response could not be mapped"));
    assert!(
        inbox_envelopes(gobby_home.path())?.is_empty(),
        "invalid 2xx body should remove the envelope only after writing diagnostics"
    );

    let failures = read_failure_artifacts(gobby_home.path())?;
    assert_eq!(failures.len(), 1);
    let failure = &failures[0];
    assert_eq!(failure["failure_kind"], "invalid_success_json");
    assert_eq!(failure["status_code"], 200);
    assert_eq!(failure["response_body_preview"], "not json");
    assert_eq!(failure["response_body_truncated"], false);
    assert_eq!(failure["hook_type"], "SessionStart");
    assert_eq!(failure["source"], "codex");
    assert_eq!(failure["critical"], true);
    assert!(
        failure["envelope_id"]
            .as_str()
            .is_some_and(|id| !id.is_empty())
    );

    Ok(())
}

#[test]
fn daemon_http_error_writes_failure_and_keeps_envelope_for_replay() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let body = r#"{"error":"down"}"#;
    let (daemon_url, daemon) = start_daemon(http_json_status(500, "Internal Server Error", body))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("Hook error on critical hook 'SessionStart'"));
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);

    let failures = read_failure_artifacts(gobby_home.path())?;
    assert_eq!(failures.len(), 1);
    let failure = &failures[0];
    assert_eq!(failure["failure_kind"], "http");
    assert_eq!(failure["status_code"], 500);
    assert_eq!(failure["response_body_preview"], body);
    assert!(
        failure["envelope_id"]
            .as_str()
            .is_some_and(|id| !id.is_empty())
    );

    Ok(())
}

#[test]
fn daemon_retry_backpressure_continues_critical_hook_and_keeps_envelope() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let (daemon_url, daemon) = start_daemon(http_json_status(
        503,
        "Service Unavailable",
        r#"{"status":"retry"}"#,
    ))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("claude"),
        Some("session-end"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
    assert_stderr_empty(&output, "critical retry backpressure")?;
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);
    assert!(read_failure_artifacts(gobby_home.path())?.is_empty());

    Ok(())
}

#[test]
fn daemon_retry_backpressure_continues_noncritical_hook_and_keeps_envelope() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let (daemon_url, daemon) = start_daemon(http_json_status(
        503,
        "Service Unavailable",
        r#"{"status":"retry"}"#,
    ))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("qwen"),
        Some("PreToolUse"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
    assert_stderr_empty(&output, "noncritical retry backpressure")?;
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);
    assert!(read_failure_artifacts(gobby_home.path())?.is_empty());

    Ok(())
}

#[test]
fn daemon_503_without_retry_body_still_blocks_critical_hook() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let body = r#"{"error":"unavailable"}"#;
    let (daemon_url, daemon) = start_daemon(http_json_status(503, "Service Unavailable", body))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("claude"),
        Some("session-end"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("Hook error on critical hook 'session-end'"));
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);

    let failures = read_failure_artifacts(gobby_home.path())?;
    assert_eq!(failures.len(), 1);
    assert_eq!(failures[0]["status_code"], 503);
    assert_eq!(failures[0]["response_body_preview"], body);

    Ok(())
}

#[test]
fn daemon_503_without_retry_body_keeps_noncritical_fail_open() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let body = r#"{"error":"unavailable"}"#;
    let (daemon_url, daemon) = start_daemon(http_json_status(503, "Service Unavailable", body))?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("qwen"),
        Some("PreToolUse"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let _request = join_daemon(daemon)?;

    assert_eq!(output.status.code(), Some(1));
    assert_json_stdout(
        &output,
        serde_json::json!({
            "status": "error",
            "message": format!("Daemon error: {body}"),
        }),
    )?;
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);

    Ok(())
}

#[test]
fn daemon_connect_failure_writes_failure_and_keeps_envelope_for_replay() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);

    let failures = read_failure_artifacts(gobby_home.path())?;
    assert_eq!(failures.len(), 1);
    let failure = &failures[0];
    assert_eq!(failure["failure_kind"], "connect");
    assert_eq!(failure["status_code"], Value::Null);
    assert!(
        failure["transport_error"]
            .as_str()
            .is_some_and(|s| !s.is_empty())
    );

    Ok(())
}

#[cfg(unix)]
#[test]
fn enqueue_failure_followed_by_direct_post_failure_writes_artifact() -> TestResult {
    use std::os::unix::fs::PermissionsExt;

    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let inbox = gobby_home.path().join("hooks").join("inbox");
    let failure_dir = inbox.join("failures");
    fs::create_dir_all(&failure_dir)?;
    fs::set_permissions(&inbox, fs::Permissions::from_mode(0o555))?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    fs::set_permissions(&inbox, fs::Permissions::from_mode(0o755))?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("Hook failure on critical hook 'SessionStart'"));

    let failures = read_failure_artifacts(gobby_home.path())?;
    assert_eq!(failures.len(), 1);
    let failure = &failures[0];
    assert_eq!(failure["failure_kind"], "direct_post_after_enqueue_failure");
    assert!(
        failure["error"]
            .as_str()
            .is_some_and(|s| s.contains("enqueue failed") && s.contains("direct post failed"))
    );
    assert_eq!(failure["envelope_id"], Value::Null);

    Ok(())
}

#[test]
fn enqueue_failure_uses_enqueue_error_as_failure_action() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let hooks_dir = gobby_home.path().join("hooks");
    fs::create_dir_all(&hooks_dir)?;
    fs::write(hooks_dir.join("inbox"), b"not a directory")?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionStart"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("Hook failure on critical hook 'SessionStart'"));
    assert!(stderr.contains("create_dir_all"));
    assert!(
        !stderr.contains("Daemon connection failed"),
        "enqueue failures should report the enqueue error, not mask it with direct-post failure"
    );

    Ok(())
}

#[test]
fn enqueue_only_session_end_returns_without_contacting_slow_daemon() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    listener.set_nonblocking(true)?;
    let daemon_url = format!("http://{}", listener.local_addr()?);
    let started = Instant::now();

    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionEnd"),
        &daemon_url,
        VALID_STDIN,
        RunGhookExtras {
            env: &[],
            args: &["--enqueue-only"],
            cwd: None,
        },
    )?;

    assert!(
        started.elapsed() < Duration::from_secs(3),
        "enqueue-only exceeded the Codex SessionEnd deadline"
    );
    assert!(output.status.success());
    assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
    assert_stderr_empty(&output, "enqueue-only SessionEnd")?;
    assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);
    assert_eq!(read_failure_artifacts(gobby_home.path())?.len(), 0);
    assert!(
        listener
            .accept()
            .is_err_and(|error| error.kind() == io::ErrorKind::WouldBlock),
        "enqueue-only unexpectedly contacted the daemon"
    );

    Ok(())
}

#[test]
fn enqueue_only_failure_does_not_fall_back_to_direct_post() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let hooks_dir = gobby_home.path().join("hooks");
    fs::create_dir_all(&hooks_dir)?;
    fs::write(hooks_dir.join("inbox"), b"not a directory")?;
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    listener.set_nonblocking(true)?;
    let daemon_url = format!("http://{}", listener.local_addr()?);
    let started = Instant::now();

    let output = run_ghook_with_dirs_and_args(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("SessionEnd"),
        &daemon_url,
        VALID_STDIN,
        RunGhookExtras {
            env: &[],
            args: &["--enqueue-only"],
            cwd: None,
        },
    )?;

    assert!(started.elapsed() < Duration::from_secs(3));
    assert_eq!(output.status.code(), Some(1));
    let stdout: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(stdout["status"], "error");
    assert!(
        stdout["message"]
            .as_str()
            .is_some_and(|message| message.contains("enqueue"))
    );
    assert_stderr_empty(&output, "enqueue-only failure")?;
    assert_eq!(read_failure_artifacts(gobby_home.path())?.len(), 0);
    assert!(
        listener
            .accept()
            .is_err_and(|error| error.kind() == io::ErrorKind::WouldBlock),
        "enqueue failure unexpectedly fell back to a direct daemon POST"
    );

    Ok(())
}

#[test]
fn stop_post_connect_failure_with_shutdown_marker_is_suppressed_and_dequeued() -> TestResult {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
    fs::write(
        gobby_home.path().join("shutdown_intent_active.json"),
        format!(r#"{{"intent":"restart","timestamp":{now}}}"#),
    )?;
    let (daemon_url, health_daemon) = start_health_only_daemon()?;

    let output = run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some("Stop"),
        &daemon_url,
        VALID_STDIN,
        &[],
    )?;
    let health_request = join_daemon(health_daemon)?;

    assert!(health_request.contains("GET /api/admin/health HTTP/1.1"));
    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
    assert_stderr_empty(&output, "planned shutdown Stop")?;

    let inbox = gobby_home.path().join("hooks").join("inbox");
    assert!(
        inbox.is_dir(),
        "Stop envelope should be enqueued before suppression"
    );
    assert!(
        fs::read_dir(&inbox)?.next().is_none(),
        "suppressed Stop envelope should be deleted"
    );

    Ok(())
}

fn run_with_closed_daemon(
    cli: &str,
    hook_type: &str,
) -> Result<Output, Box<dyn std::error::Error>> {
    let daemon_url = closed_local_url()?;
    run_temp_ghook(cli, hook_type, &daemon_url, VALID_STDIN, &[])
}

fn run_temp_ghook(
    cli: &str,
    hook_type: &str,
    daemon_url: &str,
    stdin: &str,
    extra_env: &[(&str, &str)],
) -> Result<Output, Box<dyn std::error::Error>> {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some(cli),
        Some(hook_type),
        daemon_url,
        stdin,
        extra_env,
    )
}

fn run_diagnose_with_dirs(
    home: &Path,
    gobby_home: &Path,
    cli: &str,
    hook_type: &str,
    daemon_url: &str,
) -> Result<Output, Box<dyn std::error::Error>> {
    let output = Command::new(env!("CARGO_BIN_EXE_ghook"))
        .arg("--diagnose")
        .args(["--cli", cli, "--type", hook_type])
        .env("HOME", home)
        .env("GOBBY_HOME", gobby_home)
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_HOOKS_DISABLED")
        .env_remove("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS")
        .env_remove("GOBBY_SOURCE")
        .env_remove("CLAUDE_CODE_ENTRYPOINT")
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()?;
    Ok(output)
}

fn run_ghook_with_dirs(
    home: &Path,
    gobby_home: &Path,
    cli: Option<&str>,
    hook_type: Option<&str>,
    daemon_url: &str,
    stdin: &str,
    extra_env: &[(&str, &str)],
) -> Result<Output, Box<dyn std::error::Error>> {
    run_ghook_with_dirs_and_args(
        home,
        gobby_home,
        cli,
        hook_type,
        daemon_url,
        stdin,
        RunGhookExtras {
            env: extra_env,
            args: &[],
            cwd: None,
        },
    )
}

struct RunGhookExtras<'a> {
    env: &'a [(&'a str, &'a str)],
    args: &'a [&'a str],
    cwd: Option<&'a Path>,
}

fn run_ghook_with_dirs_and_args(
    home: &Path,
    gobby_home: &Path,
    cli: Option<&str>,
    hook_type: Option<&str>,
    daemon_url: &str,
    stdin: &str,
    extras: RunGhookExtras<'_>,
) -> Result<Output, Box<dyn std::error::Error>> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ghook"));
    command
        .arg("--gobby-owned")
        .env("HOME", home)
        .env("GOBBY_HOME", gobby_home)
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_PORT")
        .env_remove("GOBBY_DAEMON_PORT")
        .env_remove("GOBBY_HOOKS_DISABLED")
        .env_remove("GOBBY_PROJECT_ID")
        .env_remove("GOBBY_SESSION_ID")
        .env_remove("GOBBY_AGENT_RUN_ID")
        .env_remove("GOBBY_STATUSLINE_DOWNSTREAM")
        .env_remove("GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS")
        .env_remove("GOBBY_SOURCE")
        .env_remove("CLAUDE_CODE_ENTRYPOINT")
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = extras.cwd {
        command.current_dir(cwd);
    }
    if let Some(cli) = cli {
        command.args(["--cli", cli]);
    }
    if let Some(hook_type) = hook_type {
        command.args(["--type", hook_type]);
    }
    command.args(extras.args);
    for (key, value) in extras.env {
        command.env(key, value);
    }

    let mut child = command.spawn()?;
    if let Some(mut child_stdin) = child.stdin.take() {
        write_stdin_allow_broken_pipe(&mut child_stdin, stdin.as_bytes())?;
    }
    Ok(child.wait_with_output()?)
}

fn write_stdin_allow_broken_pipe(stdin: &mut impl Write, bytes: &[u8]) -> io::Result<()> {
    // Invalid-arg paths can exit before stdin is read; CI can win that race.
    match stdin.write_all(bytes) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => Ok(()),
        Err(error) => Err(error),
    }
}

fn assert_json_stdout(output: &Output, expected: Value) -> TestResult {
    let actual: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(actual, expected);
    Ok(())
}

fn assert_stderr_empty(output: &Output, context: &str) -> TestResult {
    let stderr = String::from_utf8(output.stderr.clone())?;
    assert!(stderr.is_empty(), "{context} stderr:\n{stderr}");
    Ok(())
}

fn inbox_envelopes(gobby_home: &Path) -> Result<Vec<PathBuf>, Box<dyn std::error::Error>> {
    let inbox = gobby_home.join("hooks").join("inbox");
    if !inbox.exists() {
        return Ok(Vec::new());
    }

    let mut envelopes = fs::read_dir(inbox)?
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(|entry| entry.path())
        .filter(|path| path.is_file())
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    envelopes.sort();
    Ok(envelopes)
}

fn read_single_inbox_envelope(gobby_home: &Path) -> Result<Value, Box<dyn std::error::Error>> {
    let paths = inbox_envelopes(gobby_home)?;
    assert_eq!(paths.len(), 1);
    Ok(serde_json::from_slice(&fs::read(&paths[0])?)?)
}

fn read_failure_artifacts(gobby_home: &Path) -> Result<Vec<Value>, Box<dyn std::error::Error>> {
    let failure_dir = gobby_home.join("hooks").join("inbox").join("failures");
    if !failure_dir.exists() {
        return Ok(Vec::new());
    }

    let mut paths = fs::read_dir(failure_dir)?
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    paths.sort();

    paths
        .into_iter()
        .map(|path| {
            let bytes = fs::read(path)?;
            Ok(serde_json::from_slice(&bytes)?)
        })
        .collect()
}

fn start_daemon(response: String) -> io::Result<(String, JoinHandle<io::Result<String>>)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        let request = read_http_request(&mut stream)?;
        stream.write_all(response.as_bytes())?;
        Ok(request)
    });
    Ok((format!("http://{addr}"), handle))
}

fn start_health_only_daemon() -> io::Result<(String, JoinHandle<io::Result<String>>)> {
    start_daemon(http_ok_json("{}"))
}

fn join_daemon(handle: JoinHandle<io::Result<String>>) -> io::Result<String> {
    handle
        .join()
        .map_err(|_| io::Error::other("daemon thread panicked"))?
}

fn read_http_request(stream: &mut impl Read) -> io::Result<String> {
    let mut bytes = Vec::new();
    let mut buf = [0; 512];
    let header_end = loop {
        let read = stream.read(&mut buf)?;
        if read == 0 {
            break bytes.len();
        }
        bytes.extend_from_slice(&buf[..read]);
        if let Some(header_end) = find_header_end(&bytes) {
            break header_end;
        }
    };

    let headers = String::from_utf8_lossy(&bytes[..header_end]);
    let content_length = headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        })
        .unwrap_or(0);

    let mut body_read = bytes.len().saturating_sub(header_end);
    while body_read < content_length {
        let read = stream.read(&mut buf)?;
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..read]);
        body_read += read;
    }

    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn find_header_end(bytes: &[u8]) -> Option<usize> {
    bytes
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|pos| pos + 4)
}

fn http_ok_json(body: &str) -> String {
    http_json_status(200, "OK", body)
}

fn http_json_status(status: u16, reason: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn closed_local_url() -> io::Result<String> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    drop(listener);
    Ok(format!("http://{addr}"))
}
