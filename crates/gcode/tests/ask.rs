mod common;

use std::fs;
use std::process::Command;

use common::http::spawn_http_responses;
use serde_json::{Value, json};
use tempfile::TempDir;

const PROJECT_ID: &str = "11111111-1111-4111-8111-111111111111";

#[test]
fn test_ask_cli_lifecycle_contract() -> anyhow::Result<()> {
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

    let requests = requests.join().expect("join scripted Ask daemon")?;
    assert_eq!(requests.len(), 2);
    assert!(requests[0].starts_with("POST /api/ask/runs HTTP/1.1"));
    assert!(requests[0].contains("Authorization: Bearer ask-test-token"));
    assert!(requests[0].contains("X-Gobby-Project-Id: 11111111-1111-4111-8111-111111111111"));
    assert!(requests[0].contains("\"commit_ref\":\"HEAD\""));
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

    fn command(&self, daemon_url: &str, args: &[&str]) -> anyhow::Result<std::process::Output> {
        Ok(Command::new(env!("CARGO_BIN_EXE_gcode"))
            .current_dir(self.project.path())
            .args(["--quiet", "--format", "json", "--project"])
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
