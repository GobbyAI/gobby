use std::{
    fs, io,
    io::{Read, Write},
    net::TcpListener,
    path::Path,
    process::{Command, Output, Stdio},
    thread::{self, JoinHandle},
    time::{SystemTime, UNIX_EPOCH},
};

use serde_json::Value;

type TestResult = Result<(), Box<dyn std::error::Error>>;

const VALID_STDIN: &str = r#"{"session_id":"test-session","transcript_path":"/tmp/test.jsonl"}"#;

#[test]
fn malformed_stdin_uses_cli_specific_json_error_contract() -> TestResult {
    for (cli, hook_type, expected_exit) in [
        ("claude", "session-start", 2),
        ("codex", "SessionStart", 2),
        ("gemini", "SessionStart", 1),
        ("qwen", "SessionStart", 1),
        ("droid", "SessionStart", 1),
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
    let critical = run_with_closed_daemon("codex", "SessionStart")?;
    assert_eq!(critical.status.code(), Some(2));
    assert!(critical.stdout.is_empty());
    let critical_stderr = String::from_utf8(critical.stderr)?;
    assert!(critical_stderr.contains("Daemon connection failed on critical hook 'SessionStart'"));

    let noncritical = run_with_closed_daemon("codex", "PreToolUse")?;
    assert_eq!(noncritical.status.code(), Some(1));
    assert_json_stdout(
        &noncritical,
        serde_json::json!({
            "status": "error",
            "message": "Daemon unreachable",
        }),
    )?;
    assert_stderr_empty(&noncritical, "noncritical daemon-down")?;

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
        .env_remove("GOBBY_PLANNED_SHUTDOWN_ALLOW_SECONDS")
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

fn run_ghook_with_dirs(
    home: &Path,
    gobby_home: &Path,
    cli: Option<&str>,
    hook_type: Option<&str>,
    daemon_url: &str,
    stdin: &str,
    extra_env: &[(&str, &str)],
) -> Result<Output, Box<dyn std::error::Error>> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ghook"));
    command
        .arg("--gobby-owned")
        .env("HOME", home)
        .env("GOBBY_HOME", gobby_home)
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_HOOKS_DISABLED")
        .env_remove("GOBBY_PLANNED_SHUTDOWN_ALLOW_SECONDS")
        .env_remove("GOBBY_SOURCE")
        .env_remove("CLAUDE_CODE_ENTRYPOINT")
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cli) = cli {
        command.args(["--cli", cli]);
    }
    if let Some(hook_type) = hook_type {
        command.args(["--type", hook_type]);
    }
    for (key, value) in extra_env {
        command.env(key, value);
    }

    let mut child = command.spawn()?;
    if let Some(mut child_stdin) = child.stdin.take() {
        child_stdin.write_all(stdin.as_bytes())?;
    }
    Ok(child.wait_with_output()?)
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
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn closed_local_url() -> io::Result<String> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    drop(listener);
    Ok(format!("http://{addr}"))
}
