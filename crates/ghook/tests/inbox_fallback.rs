use serde_json::Value;
use std::fs;
use std::io::{self, Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::thread::{self, JoinHandle};

const ACCEPT_RESPONSE: &[u8] = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 35\r\n\r\n{\"decision\":\"accept\",\"reason\":\"ok\"}";

#[test]
fn blocked_inbox_falls_back_to_direct_daemon_post() -> Result<(), Box<dyn std::error::Error>> {
    let home = home_with_blocked_inbox()?;
    let (daemon_url, daemon) = start_daemon(ACCEPT_RESPONSE)?;

    let output = run_ghook(home.path(), &daemon_url)?;
    let request = daemon
        .join()
        .map_err(|_| io::Error::other("daemon thread panicked"))??;

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "expected ghook success, got {:?}\nstderr:\n{stderr}",
        output.status.code()
    );
    assert!(stderr.is_empty(), "unexpected stderr:\n{stderr}");
    let stdout: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(
        stdout,
        serde_json::json!({"decision": "accept", "reason": "ok"})
    );
    assert!(request.contains("POST /api/hooks/execute HTTP/1.1"));
    assert!(request.contains("\"schema_version\":1"));
    assert!(request.contains("\"hook_type\":\"SessionStart\""));
    assert!(request.contains("\"source\":\"codex\""));

    Ok(())
}

#[test]
fn blocked_inbox_keeps_failure_action_when_daemon_post_fails()
-> Result<(), Box<dyn std::error::Error>> {
    let home = home_with_blocked_inbox()?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook(home.path(), &daemon_url)?;

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr)?;
    assert!(stderr.contains("Hook failure on critical hook 'SessionStart'"));
    assert!(stderr.contains("create_dir_all"));
    assert!(!stderr.contains("Daemon connection failed"));

    Ok(())
}

#[test]
fn gobby_home_env_keeps_envelope_and_marker_under_same_root()
-> Result<(), Box<dyn std::error::Error>> {
    let real_home = tempfile::tempdir()?;
    let gobby_home = tempfile::tempdir()?;
    fs::write(
        gobby_home.path().join("shutdown_intent_active.json"),
        br#"{"source":"test","intent":"restart","timestamp":1}"#,
    )?;
    let daemon_url = closed_local_url()?;

    let output = run_ghook_with_gobby_home(real_home.path(), gobby_home.path(), &daemon_url)?;

    assert_eq!(output.status.code(), Some(2));
    let marker = gobby_home.path().join("shutdown_intent_active.json");
    let inbox = gobby_home.path().join("hooks").join("inbox");
    assert!(
        marker.exists(),
        "shutdown marker should stay under GOBBY_HOME"
    );
    let envelopes = fs::read_dir(&inbox)?.collect::<Result<Vec<_>, _>>()?;
    assert_eq!(
        envelopes.len(),
        1,
        "expected one queued envelope in {inbox:?}"
    );
    assert!(
        !real_home.path().join(".gobby").join("hooks").exists(),
        "raw HOME should not receive hook inbox writes when GOBBY_HOME is set"
    );

    Ok(())
}

fn home_with_blocked_inbox() -> Result<tempfile::TempDir, Box<dyn std::error::Error>> {
    let home = tempfile::tempdir()?;
    let hooks_dir = home.path().join(".gobby").join("hooks");
    fs::create_dir_all(&hooks_dir)?;
    fs::write(hooks_dir.join("inbox"), b"not a directory")?;
    Ok(home)
}

fn run_ghook(home: &Path, daemon_url: &str) -> Result<Output, Box<dyn std::error::Error>> {
    run_ghook_command(ghook_command(home, daemon_url))
}

fn run_ghook_with_gobby_home(
    home: &Path,
    gobby_home: &Path,
    daemon_url: &str,
) -> Result<Output, Box<dyn std::error::Error>> {
    let mut command = ghook_command(home, daemon_url);
    command.env("GOBBY_HOME", gobby_home);
    run_ghook_command(command)
}

fn ghook_command(home: &Path, daemon_url: &str) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ghook"));
    command
        .args(["--gobby-owned", "--cli", "codex", "--type", "SessionStart"])
        .env("HOME", home)
        .env("GOBBY_DAEMON_URL", daemon_url)
        .env_remove("GOBBY_HOOKS_DISABLED")
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn run_ghook_command(mut command: Command) -> Result<Output, Box<dyn std::error::Error>> {
    let mut child = command.spawn()?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| io::Error::other("missing child stdin"))?;
    stdin.write_all(br#"{"session_id":"s"}"#)?;
    drop(stdin);

    Ok(child.wait_with_output()?)
}

fn start_daemon(response: &'static [u8]) -> io::Result<(String, JoinHandle<io::Result<String>>)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        let request = read_http_request(&mut stream)?;
        stream.write_all(response)?;
        Ok(request)
    });

    Ok((format!("http://{addr}"), handle))
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
        .map(|position| position + 4)
}

fn closed_local_url() -> io::Result<String> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    drop(listener);
    Ok(format!("http://{addr}"))
}
