mod common;

use std::fs;
use std::process::Command;

use common::http::spawn_http_responses;
use serde_json::{Value, json};
use tempfile::TempDir;

const PROJECT_ID: &str = "11111111-1111-4111-8111-111111111111";

/// A run outlives its wait connection, and the recovery the CLI prints is the
/// command that finishes it. The printed command is executed verbatim rather than
/// matched as text, so a hint naming a stale run or a flag the parser rejects
/// fails here. The real authenticated service acceptance is
/// `tests/servers/routes/test_ask.py::test_installed_cli_lifecycle_against_authenticated_service`,
/// which owns the Python side of this contract.
#[test]
fn test_ask_cli_lifecycle_contract() -> anyhow::Result<()> {
    let fixture = AskCliFixture::new()?;

    let (daemon_url, requests) =
        spawn_http_responses(vec![(202, run_payload("running", None, None))]);
    let interrupted = fixture.command(&daemon_url, &["ask", "Where is the source of truth?"])?;
    let started = requests.join().expect("join interrupted start daemon")?;
    assert_eq!(
        started.len(),
        1,
        "the wait must outlive the scripted daemon"
    );
    assert_eq!(interrupted.status.code(), Some(2));

    let stderr = String::from_utf8_lossy(&interrupted.stderr).into_owned();
    let failure = stderr
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|payload| payload.get("error").is_some())
        .unwrap_or_else(|| panic!("interrupted start reports a typed error: {stderr}"));
    assert_eq!(failure["error"], "ask_wait_disconnected");
    let recovery = failure["recovery"]
        .as_str()
        .unwrap_or_else(|| panic!("interrupted start names a recovery: {stderr}"));
    let mut recovered = recovery
        .split('`')
        .nth(1)
        .unwrap_or_else(|| panic!("the recovery quotes a command: {recovery}"))
        .split_whitespace();
    assert_eq!(recovered.next(), Some("gcode"), "recovery: {recovery}");
    let resume: Vec<&str> = recovered.collect();

    let (daemon_url, requests) = spawn_http_responses(vec![
        (200, run_payload("running", None, None)),
        (200, run_payload("completed", Some("complete"), None)),
    ]);
    let resumed = fixture.command(&daemon_url, &resume)?;
    let served = requests.join().expect("join resume daemon")?;
    assert!(
        resumed.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&resumed.stderr)
    );
    assert!(
        served[0].starts_with("POST /api/ask/runs/ask-run-1/resume?"),
        "the printed recovery must address the started run: {}",
        served[0]
    );
    let answer: Value = serde_json::from_slice(&resumed.stdout)?;
    assert_eq!(answer["run_id"], "ask-run-1");
    assert_eq!(answer["status"], "completed");
    assert_eq!(answer["answer_outcome"], "complete");
    Ok(())
}

#[test]
fn test_ask_cli_scripted_transport_contract() -> anyhow::Result<()> {
    let fixture = AskCliFixture::new()?;
    let running = run_payload("running", None, None);
    let completed = run_payload("completed", Some("unknown"), None);
    let (daemon_url, requests) = spawn_http_responses(vec![(202, running), (200, completed)]);

    let output = fixture.command(&daemon_url, &["ask", "Where is the source of truth?"])?;
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let response: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(response["run_id"], "ask-run-1");
    assert_eq!(response["status"], "completed");
    assert_eq!(response["answer_outcome"], "unknown");
    assert!(String::from_utf8_lossy(&output.stderr).contains("Ask run ask-run-1"));

    let requests = requests.join().expect("join scripted Ask daemon")?;
    assert_eq!(requests.len(), 2);
    assert!(requests[0].starts_with("POST /api/ask/runs HTTP/1.1"));
    assert!(requests[0].contains("Authorization: Bearer ask-test-token"));
    assert!(requests[0].contains("X-Gobby-Project-Id: 11111111-1111-4111-8111-111111111111"));
    assert!(!requests[0].contains("commit_ref"));
    assert!(requests[0].contains("\"project_path\":"));
    assert!(requests[0].contains("\"timeout_seconds\":600"));
    assert!(requests[0].contains("\"retrieval_mode\":\"deterministic\""));
    assert!(requests[1].starts_with("GET /api/ask/runs/ask-run-1/wait?"));

    let (daemon_url, requests) =
        spawn_http_responses(vec![(200, run_payload("running", None, None))]);
    let output = fixture.command(&daemon_url, &["ask", "--status", "ask-run-1"])?;
    assert!(output.status.success());
    let requests = requests.join().expect("join status daemon")?;
    assert_eq!(requests.len(), 1);
    assert!(requests[0].starts_with("GET /api/ask/runs/ask-run-1?"));

    let (daemon_url, requests) =
        spawn_http_responses(vec![(202, run_payload("running", None, None))]);
    let output = fixture.command(
        &daemon_url,
        &["ask", "Where is the source of truth?", "--background"],
    )?;
    assert!(output.status.success());
    let requests = requests.join().expect("join background daemon")?;
    assert_eq!(requests.len(), 1, "background start must not wait");

    let (daemon_url, requests) = spawn_http_responses(vec![
        (200, run_payload("running", None, None)),
        (200, run_payload("completed", Some("partial"), None)),
    ]);
    let output = fixture.command(&daemon_url, &["ask", "--resume", "ask-run-1"])?;
    assert!(output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(response["answer_outcome"], "partial");
    let requests = requests.join().expect("join resume daemon")?;
    assert!(requests[0].starts_with("POST /api/ask/runs/ask-run-1/resume?"));
    assert!(requests[1].starts_with("GET /api/ask/runs/ask-run-1/wait?"));

    let (daemon_url, requests) = spawn_http_responses(vec![(
        200,
        run_payload("cancelled", None, Some(json!({"code": "cancelled"}))),
    )]);
    let output = fixture.command(&daemon_url, &["ask", "--cancel", "ask-run-1"])?;
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("ask_cancelled"));
    let requests = requests.join().expect("join cancel daemon")?;
    assert_eq!(requests.len(), 1);
    assert!(requests[0].starts_with("POST /api/ask/runs/ask-run-1/cancel?"));

    let (daemon_url, requests) = spawn_http_responses(vec![(
        200,
        run_payload(
            "failed",
            None,
            Some(json!({"code": "review_rejected", "message": "unsupported claim"})),
        ),
    )]);
    let output = fixture.command(&daemon_url, &["ask", "--status", "ask-run-1"])?;
    assert_eq!(output.status.code(), Some(2));
    let response: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(response["typed_error"]["code"], "review_rejected");
    assert!(String::from_utf8_lossy(&output.stderr).contains("ask_failed"));
    requests.join().expect("join failed-status daemon")?;

    let export_dir = fixture.project.path().join("exports");
    let export_dir_arg = export_dir.to_string_lossy().into_owned();
    let (daemon_url, requests) = spawn_http_responses(vec![(200, json!({"bundle": "immutable"}))]);
    let output = fixture.command(
        &daemon_url,
        &["ask", "--export", "ask-run-1", "--output", &export_dir_arg],
    )?;
    assert!(output.status.success());
    assert!(export_dir.join("ask-ask-run-1.tar").is_file());
    let requests = requests.join().expect("join export daemon")?;
    assert!(requests[0].starts_with("GET /api/ask/runs/ask-run-1/export?"));

    let (daemon_url, requests) =
        spawn_http_responses(vec![(202, run_payload("running", None, None))]);
    let output = fixture.command(&daemon_url, &["ask", "disconnect after start"])?;
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("ask-run-1"));
    assert!(stderr.contains("gcode ask --resume ask-run-1"));
    let requests = requests.join().expect("join disconnect daemon")?;
    assert_eq!(
        requests.len(),
        1,
        "disconnect must not trigger cancellation"
    );

    let (daemon_url, requests) = spawn_http_responses(vec![
        (202, run_payload("running", None, None)),
        (408, json!({"detail": "wait expired"})),
    ]);
    let output = fixture.command(&daemon_url, &["ask", "bounded wait"])?;
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("ask_wait_timeout"), "stderr: {stderr}");
    assert!(stderr.contains("Ask run ask-run-1"), "stderr: {stderr}");
    assert!(
        !stderr.contains("ask_wait_disconnected"),
        "stderr: {stderr}"
    );
    let requests = requests.join().expect("join timed-out wait daemon")?;
    assert_eq!(
        requests.len(),
        2,
        "wait timeout must not trigger cancellation"
    );

    let output = fixture.command(
        "http://127.0.0.1:9",
        &["ask", "question", "--cancel", "ask-run-1"],
    )?;
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr).contains("cannot be used with"));

    Ok(())
}

#[test]
fn test_ask_export_preserves_existing_bundle_on_disconnect() -> anyhow::Result<()> {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::time::Duration;

    let fixture = AskCliFixture::new()?;
    let destination = fixture.project.path().join("exports");
    fs::create_dir(&destination)?;
    let bundle = destination.join("ask-ask-run-1.tar");
    fs::write(&bundle, b"previous complete bundle")?;
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let daemon_url = format!("http://{}", listener.local_addr()?);
    let server = std::thread::spawn(move || -> std::io::Result<()> {
        let (mut stream, _) = listener.accept()?;
        stream.set_read_timeout(Some(Duration::from_secs(5)))?;
        let mut request = [0_u8; 4096];
        let received = stream.read(&mut request)?;
        assert!(received > 0, "export client must send a request");
        stream.write_all(
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\npartial",
        )
    });
    let output = fixture.command(
        &daemon_url,
        &[
            "ask",
            "--export",
            "ask-run-1",
            "--output",
            &destination.to_string_lossy(),
        ],
    )?;
    server.join().expect("join truncated export server")?;
    assert!(!output.status.success());
    assert_eq!(fs::read(&bundle)?, b"previous complete bundle");
    assert_eq!(fs::read_dir(&destination)?.count(), 1);
    Ok(())
}

fn run_payload(status: &str, outcome: Option<&str>, error: Option<Value>) -> Value {
    json!({
        "run_id": "ask-run-1",
        "status": status,
        "current_stage": if status == "completed" { Value::Null } else { json!("investigate") },
        "answer_outcome": outcome,
        "typed_error": error,
        "deadline_at": "2026-09-09T12:10:00Z",
        "profile_identities": {
            "investigator": "ask-investigator@sha256:one",
            "reviewer": "ask-reviewer@sha256:two"
        },
        "tool_identities": ["gcode@contract-9"],
        "artifact_manifest": null,
        "attempt_count": 1,
        "repair_count": 0,
        "binding": {
            "project_id": PROJECT_ID,
            "commit_oid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "tree_oid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "deadline_at": "2026-09-09T12:10:00Z",
            "retrieval_mode": "deterministic",
            "inventory_digest": "sha256:inventory",
            "snapshot_artifact": null
        },
        "evidence": [],
        "result_artifact": null,
        "usage": null
    })
}

struct AskCliFixture {
    project: TempDir,
    home: TempDir,
}

impl AskCliFixture {
    fn new() -> anyhow::Result<Self> {
        let project = tempfile::tempdir()?;
        let home = tempfile::tempdir()?;
        fs::create_dir(project.path().join(".gobby"))?;
        fs::write(
            project.path().join(".gobby/project.json"),
            serde_json::to_vec(&json!({"id": PROJECT_ID, "name": "ask-test"}))?,
        )?;
        fs::write(home.path().join("local_cli_token"), "ask-test-token\n")?;
        let status = Command::new("git")
            .args(["init", "--quiet"])
            .current_dir(project.path())
            .status()?;
        anyhow::ensure!(status.success(), "initialize Ask test repository");
        Ok(Self { project, home })
    }

    /// The binary under test. Defaults to this crate's build; acceptance 3.1.3
    /// wants the rebuilt installed binary, which `GOBBY_GCODE_BIN` selects.
    fn binary() -> std::ffi::OsString {
        std::env::var_os("GOBBY_GCODE_BIN").unwrap_or_else(|| env!("CARGO_BIN_EXE_gcode").into())
    }

    fn command(&self, daemon_url: &str, args: &[&str]) -> anyhow::Result<std::process::Output> {
        self.command_format(daemon_url, args, "json")
    }

    fn command_format(
        &self,
        daemon_url: &str,
        args: &[&str],
        format: &str,
    ) -> anyhow::Result<std::process::Output> {
        Ok(Command::new(Self::binary())
            .current_dir(self.project.path())
            .args(["--quiet", "--format", format, "--project"])
            .arg(self.project.path())
            .args(args)
            .env("GOBBY_HOME", self.home.path())
            .env("GOBBY_DAEMON_URL", daemon_url)
            .env("GOBBY_SESSION_ID", "22222222-2222-4222-8222-222222222222")
            .env_remove("GOBBY_AGENT_API_TOKEN")
            .env_remove("GOBBY_AGENT_RUN_ID")
            .output()?)
    }
}

#[test]
fn direct_markdown_and_explicit_local_debug_output() -> anyhow::Result<()> {
    let fixture = AskCliFixture::new()?;
    let markdown = "# Answer\n\n[source.rs:1-2](/api/ask/runs/run/citations/evidence)\n";
    let mut completed = run_payload("completed", Some("complete"), None);
    completed["markdown"] = json!(markdown);
    completed["answer"] = json!({"claims": [{"id": "claim-1"}]});
    for debug in [false, true] {
        let (url, server) = spawn_http_responses(vec![
            (202, run_payload("running", None, None)),
            (200, completed.clone()),
        ]);
        let mut args = vec!["ask", "Explain the source"];
        if debug {
            args.push("--output-debug-files");
        }
        let output = fixture.command_format(&url, &args, "text")?;
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(String::from_utf8(output.stdout)?.trim(), markdown.trim());
        let requests = server.join().expect("join debug fixture")?;
        assert!(
            requests
                .iter()
                .all(|request| !request.contains("output_debug_files"))
        );
        let root = fixture.home.path().join("ask-debug");
        assert_eq!(root.exists(), debug);
        if debug {
            let bundle = fs::read_dir(root)?
                .next()
                .expect("one debug bundle")?
                .path();
            let saved: Value = serde_json::from_slice(&fs::read(bundle.join("ask.json"))?)?;
            assert_eq!(saved, completed);
        }
    }
    Ok(())
}

#[test]
fn debug_write_failure_preserves_successful_answer() -> anyhow::Result<()> {
    let fixture = AskCliFixture::new()?;
    fs::write(fixture.home.path().join("ask-debug"), b"occupied")?;
    let completed = run_payload("completed", Some("partial"), None);
    let (url, server) = spawn_http_responses(vec![(202, completed.clone())]);
    let output = fixture.command(&url, &["ask", "question", "--output-debug-files"])?;
    assert!(output.status.success());
    assert_eq!(serde_json::from_slice::<Value>(&output.stdout)?, completed);
    assert!(String::from_utf8_lossy(&output.stderr).contains("Diagnostic write failed"));
    server.join().expect("join failed debug fixture")?;
    Ok(())
}
