//! Claude Code statusline handler.
//!
//! This is intentionally separate from the normal enqueue-first hook path.
//! Claude reads statusline stdout directly on every tick, so the handler must
//! preserve downstream stdout bytes exactly and must never expose transport
//! failures to Claude as hook failures.

use serde_json::{Value, json};
use std::ffi::OsStr;
use std::io::{Read, Write};
use std::process::{Command, ExitCode, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

const STATUSLINE_ENDPOINT: &str = "/api/sessions/statusline";
const DAEMON_POST_TIMEOUT: Duration = Duration::from_secs(2);
const DAEMON_POST_JOIN_TIMEOUT: Duration = Duration::from_millis(300);
const DOWNSTREAM_TIMEOUT: Duration = Duration::from_secs(5);

pub(crate) fn is_statusline_hook(cli: &str, hook_type: &str) -> bool {
    cli.eq_ignore_ascii_case("claude") && hook_type == "statusline"
}

pub(crate) fn handle(stdin_raw: &[u8]) -> ExitCode {
    let downstream = std::env::var_os("GOBBY_STATUSLINE_DOWNSTREAM");
    let downstream = downstream.as_deref().filter(|command| !command.is_empty());
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let mut stdout = std::io::stdout().lock();
    handle_with(stdin_raw, &daemon_url, downstream, &mut stdout)
}

fn handle_with(
    stdin_raw: &[u8],
    daemon_url: &str,
    downstream: Option<&OsStr>,
    stdout: &mut impl Write,
) -> ExitCode {
    let input = match serde_json::from_slice::<Value>(stdin_raw) {
        Ok(Value::Object(map)) => Value::Object(map),
        Ok(_) => {
            eprintln!("ghook statusline: expected JSON object");
            return ExitCode::SUCCESS;
        }
        Err(e) => {
            eprintln!("ghook statusline: invalid JSON: {e}");
            return ExitCode::SUCCESS;
        }
    };

    if let Some(payload) = extract_payload(&input) {
        post_to_daemon_best_effort(payload, daemon_url);
    }

    if let Some(command) = downstream
        && let Some(bytes) = forward_downstream(command, stdin_raw)
    {
        let _ = stdout.write_all(&bytes);
        let _ = stdout.flush();
    }

    ExitCode::SUCCESS
}

fn extract_payload(input: &Value) -> Option<Value> {
    let session_id = input.get("session_id")?;
    if !python_truthy(session_id) {
        return None;
    }

    let empty = serde_json::Map::new();
    let cost = input
        .get("cost")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let model = input
        .get("model")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let context_window = input
        .get("context_window")
        .and_then(Value::as_object)
        .unwrap_or(&empty);

    Some(json!({
        "session_id": session_id.clone(),
        "model_id": model.get("id").cloned().unwrap_or_else(|| json!("")),
        "input_tokens": cost.get("input_tokens").cloned().unwrap_or_else(|| json!(0)),
        "output_tokens": cost.get("output_tokens").cloned().unwrap_or_else(|| json!(0)),
        "cache_creation_tokens": cost
            .get("cache_creation_tokens")
            .cloned()
            .unwrap_or_else(|| json!(0)),
        "cache_read_tokens": cost
            .get("cache_read_tokens")
            .cloned()
            .unwrap_or_else(|| json!(0)),
        "context_window_size": context_window.get("size").cloned().unwrap_or_else(|| json!(0)),
    }))
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => {
            number.as_i64().is_some_and(|n| n != 0)
                || number.as_u64().is_some_and(|n| n != 0)
                || number.as_f64().is_some_and(|n| n != 0.0)
        }
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn post_to_daemon_best_effort(payload: Value, daemon_url: &str) {
    let endpoint = format!("{daemon_url}{STATUSLINE_ENDPOINT}");
    let (tx, rx) = mpsc::channel();

    thread::spawn(move || {
        let _ = ureq::post(&endpoint)
            .timeout(DAEMON_POST_TIMEOUT)
            .set("Content-Type", "application/json")
            .send_json(payload);
        let _ = tx.send(());
    });

    let _ = rx.recv_timeout(DAEMON_POST_JOIN_TIMEOUT);
}

fn forward_downstream(command: &OsStr, stdin_raw: &[u8]) -> Option<Vec<u8>> {
    let mut child = downstream_shell_command(command)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;

    if let Some(mut stdin) = child.stdin.take() {
        // Python's Popen.communicate(input=...) tolerates a downstream that
        // exits without reading stdin (e.g. `printf`). Still collect stdout.
        let _ = stdin.write_all(stdin_raw);
    }

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => {
                let mut stdout = Vec::new();
                let mut stdout_pipe = child.stdout.take()?;
                let _ = stdout_pipe.read_to_end(&mut stdout);
                return (!stdout.is_empty()).then_some(stdout);
            }
            Ok(None) if started.elapsed() < DOWNSTREAM_TIMEOUT => {
                thread::sleep(Duration::from_millis(10));
            }
            Ok(None) | Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

#[cfg(not(target_os = "windows"))]
fn downstream_shell_command(command: &OsStr) -> Command {
    let mut shell = Command::new("sh");
    shell.arg("-c").arg(command);
    shell
}

#[cfg(target_os = "windows")]
fn downstream_shell_command(command: &OsStr) -> Command {
    let mut shell = Command::new("cmd");
    shell.arg("/C").arg(command);
    shell
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    const VALID_INPUT: &str = include_str!("../tests/fixtures/statusline/full-input.json");
    const VALID_PAYLOAD: &str = include_str!("../tests/fixtures/statusline/full-payload.json");
    const DEFAULT_INPUT: &str = include_str!("../tests/fixtures/statusline/defaults-input.json");
    const DEFAULT_PAYLOAD: &str =
        include_str!("../tests/fixtures/statusline/defaults-payload.json");

    fn read_http_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1024];
        let mut header_end = None;
        let mut content_length = None;

        loop {
            let n = stream.read(&mut chunk).unwrap();
            assert!(n > 0, "client closed before request body was fully read");
            request.extend_from_slice(&chunk[..n]);

            if header_end.is_none()
                && let Some(pos) = request.windows(4).position(|window| window == b"\r\n\r\n")
            {
                let end = pos + 4;
                header_end = Some(end);
                let headers = String::from_utf8_lossy(&request[..end]);
                content_length = Some(
                    headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("Content-Length")
                                .then(|| value.trim().parse::<usize>().ok())
                                .flatten()
                        })
                        .unwrap_or(0),
                );
            }

            if let (Some(end), Some(len)) = (header_end, content_length)
                && request.len() >= end + len
            {
                return String::from_utf8(request).unwrap();
            }
        }
    }

    #[test]
    fn recognizes_only_claude_statusline_hook() {
        assert!(is_statusline_hook("claude", "statusline"));
        assert!(is_statusline_hook("CLAUDE", "statusline"));
        assert!(!is_statusline_hook("claude", "PreToolUse"));
        assert!(!is_statusline_hook("codex", "statusline"));
    }

    #[test]
    fn extract_payload_matches_full_golden_fixture() {
        let input: Value = serde_json::from_str(VALID_INPUT).unwrap();
        let expected: Value = serde_json::from_str(VALID_PAYLOAD).unwrap();
        assert_eq!(extract_payload(&input), Some(expected));
    }

    #[test]
    fn extract_payload_matches_default_golden_fixture() {
        let input: Value = serde_json::from_str(DEFAULT_INPUT).unwrap();
        let expected: Value = serde_json::from_str(DEFAULT_PAYLOAD).unwrap();
        assert_eq!(extract_payload(&input), Some(expected));
    }

    #[test]
    fn extract_payload_returns_none_without_session_id() {
        assert!(extract_payload(&json!({"cost": {"input_tokens": 1}})).is_none());
        assert!(extract_payload(&json!({"session_id": ""})).is_none());
        assert!(extract_payload(&json!({"session_id": null})).is_none());
        assert!(extract_payload(&json!({"session_id": 0})).is_none());
        assert!(extract_payload(&json!({"session_id": false})).is_none());
    }

    #[test]
    fn malformed_json_exits_success_without_stdout() {
        let mut stdout = Vec::new();
        let exit = handle_with(b"not json", "http://127.0.0.1:9", None, &mut stdout);
        assert_eq!(exit, ExitCode::SUCCESS);
        assert!(stdout.is_empty());
    }

    #[test]
    fn posts_statusline_payload_to_daemon_endpoint() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let request = read_http_request(&mut stream);
            assert!(request.contains("POST /api/sessions/statusline HTTP/1.1"));
            let expected: Value = serde_json::from_str(VALID_PAYLOAD).unwrap();
            let body = request.split("\r\n\r\n").nth(1).unwrap();
            let actual: Value = serde_json::from_str(body).unwrap();
            assert_eq!(actual, expected);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{\"status\":\"ok\"}")
                .unwrap();
        });

        let mut stdout = Vec::new();
        let exit = handle_with(
            VALID_INPUT.as_bytes(),
            &format!("http://{addr}"),
            None,
            &mut stdout,
        );
        handle.join().unwrap();

        assert_eq!(exit, ExitCode::SUCCESS);
        assert!(stdout.is_empty());
    }

    #[test]
    fn downstream_stdout_passthrough_preserves_bytes() {
        let mut stdout = Vec::new();
        let exit = handle_with(
            br#"{"session_id":"sess-123"}"#,
            "http://127.0.0.1:9",
            Some(OsStr::new("printf 'status ok'")),
            &mut stdout,
        );

        assert_eq!(exit, ExitCode::SUCCESS);
        assert_eq!(stdout, b"status ok");
    }

    #[test]
    fn downstream_timeout_returns_before_six_seconds() {
        if cfg!(target_os = "windows") {
            return;
        }

        let started = Instant::now();
        let mut stdout = Vec::new();
        let exit = handle_with(
            br#"{"session_id":"sess-123"}"#,
            "http://127.0.0.1:9",
            Some(OsStr::new("sleep 10")),
            &mut stdout,
        );

        assert_eq!(exit, ExitCode::SUCCESS);
        assert!(stdout.is_empty());
        assert!(
            started.elapsed() < Duration::from_secs(6),
            "downstream timeout should fire before CI hangs"
        );
    }
}
