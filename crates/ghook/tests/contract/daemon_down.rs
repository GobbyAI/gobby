use super::*;

const MESSAGE: &str = "[Gobby] Daemon down. Please wait.";

#[test]
fn typed_503_pauses_spawned_but_preserves_operator_and_generic_http() -> TestResult {
    let typed =
        r#"{"status":"unavailable","backend":{"state":"starting","target":"127.0.0.1:60888"}}"#;
    for (body, is_typed) in [(typed, true), (r#"{"error":"unavailable"}"#, false)] {
        for marker in ["0", "1"] {
            let home = tempfile::tempdir()?;
            let gobby_home = tempfile::tempdir()?;
            let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
            fs::write(
                gobby_home.path().join("shutdown_intent_active.json"),
                format!(r#"{{"intent":"restart","timestamp":{now}}}"#),
            )?;
            let (url, server) = start_daemon(http_json_status(503, "Service Unavailable", body))?;
            let output = run_ghook_with_dirs(
                home.path(),
                gobby_home.path(),
                Some("codex"),
                Some("PreCompact"),
                &url,
                VALID_STDIN,
                &[
                    ("GOBBY_IS_SPAWNED_AGENT", marker),
                    ("GOBBY_PROJECT_ID", "test-project"),
                ],
            )?;
            join_daemon(server)?;
            if is_typed && marker == "0" {
                assert_eq!(output.status.code(), Some(0));
                assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
                assert_stderr_empty(&output, "operator typed 503")?;
            } else {
                assert_eq!(output.status.code(), Some(2));
                assert!(output.stdout.is_empty());
                let stderr = String::from_utf8(output.stderr)?;
                if is_typed {
                    assert_eq!(stderr.trim(), MESSAGE);
                } else {
                    assert!(
                        stderr
                            .trim()
                            .starts_with("Hook error on critical hook 'PreCompact'"),
                        "{stderr}"
                    );
                }
            }
            assert_eq!(inbox_envelopes(gobby_home.path())?.len(), 1);
        }
    }
    Ok(())
}

#[test]
fn spawned_reachable_preserves_success_for_every_cli() -> TestResult {
    for cli in ["claude", "codex", "droid", "grok", "agy"] {
        let mut outputs = Vec::new();
        for marker in ["0", "1"] {
            let home = tempfile::tempdir()?;
            let gobby_home = tempfile::tempdir()?;
            let body = if cli == "agy" {
                r#"{"decision":"allow"}"#
            } else {
                r#"{"continue":true}"#
            };
            let (url, server) = start_daemon(http_ok_json(body))?;
            let output = run_ghook_with_dirs(
                home.path(),
                gobby_home.path(),
                Some(cli),
                Some(if cli == "grok" {
                    "pre_tool_use"
                } else {
                    "PreToolUse"
                }),
                &url,
                VALID_STDIN,
                &[
                    ("GOBBY_IS_SPAWNED_AGENT", marker),
                    ("GOBBY_PROJECT_ID", "test-project"),
                ],
            )?;
            join_daemon(server)?;
            assert_eq!(output.status.code(), Some(0), "{cli} marker={marker}");
            assert!(inbox_envelopes(gobby_home.path())?.is_empty());
            outputs.push((output.stdout, output.stderr));
        }
        assert_eq!(outputs[0], outputs[1], "{cli} reachable behavior changed");
    }
    Ok(())
}

fn run_spawned_connect(
    hook: &str,
    planned: bool,
    broken_inbox: bool,
) -> Result<Output, Box<dyn std::error::Error>> {
    let home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    if planned {
        let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
        fs::write(
            gobby_home.path().join("shutdown_intent_active.json"),
            format!(r#"{{"intent":"restart","timestamp":{now}}}"#),
        )?;
    }
    if broken_inbox {
        fs::create_dir_all(gobby_home.path().join("hooks"))?;
        fs::write(gobby_home.path().join("hooks/inbox"), "not a directory")?;
    }
    run_ghook_with_dirs(
        home.path(),
        gobby_home.path(),
        Some("codex"),
        Some(hook),
        &closed_local_url()?,
        VALID_STDIN,
        &[
            ("GOBBY_IS_SPAWNED_AGENT", "1"),
            ("GOBBY_PROJECT_ID", "test-project"),
        ],
    )
}

#[test]
fn spawned_connect_blocks_before_planned_precompact_bypass() -> TestResult {
    let output = run_spawned_connect("PreCompact", true, false)?;
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(String::from_utf8(output.stderr)?.trim(), MESSAGE);
    Ok(())
}

#[test]
fn spawned_connect_direct_post_fallback_blocks() -> TestResult {
    let output = run_spawned_connect("PreToolUse", false, true)?;
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(String::from_utf8(output.stderr)?.trim(), MESSAGE);
    Ok(())
}

#[test]
fn spawned_connect_stop_continues_without_shutdown_marker() -> TestResult {
    let output = run_spawned_connect("Stop", false, false)?;
    assert_eq!(output.status.code(), Some(0));
    assert_json_stdout(&output, serde_json::json!({"continue": true}))?;
    assert_stderr_empty(&output, "spawned Stop")?;
    Ok(())
}
