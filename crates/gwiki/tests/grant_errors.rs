//! gwiki grant-gated DSN and daemon-required CLI contract.

use std::net::TcpListener;
use std::path::Path;
use std::process::{Command, Output, Stdio};

use serde_json::Value;

fn unused_loopback() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind unused port");
    let port = listener.local_addr().expect("addr").port();
    drop(listener);
    format!("http://127.0.0.1:{port}")
}

fn write_project(root: &Path) {
    let gobby = root.join(".gobby");
    std::fs::create_dir_all(&gobby).expect("create .gobby");
    std::fs::write(
        gobby.join("project.json"),
        r#"{"id":"dddddddd-dddd-4ddd-8ddd-dddddddddddd","name":"gwiki-grant"}"#,
    )
    .expect("write project.json");
}

fn run_gwiki(home: &Path, cwd: &Path, extra: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_gwiki"));
    cmd.args(["--project", cwd.to_str().expect("utf8"), "collect"])
        .current_dir(cwd)
        .env("HOME", home)
        .env("GOBBY_HOME", home)
        .env("GOBBY_DAEMON_URL", unused_loopback())
        .env_remove(&format!("{}{}", "GWIKI_", "DATABASE_URL"))
        .env_remove(&format!("{}{}", "GCODE_", "DATABASE_URL"))
        .env_remove(&format!("{}{}", "GOBBY_", "POSTGRES_DSN"))
        .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in extra {
        cmd.env(key, value);
    }
    cmd.output().expect("run gwiki")
}

fn parse_error_payload(output: &Output) -> Value {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    for stream in [stderr.as_ref(), stdout.as_ref()] {
        if let Ok(value) = serde_json::from_str::<Value>(stream.trim())
            && (value.get("error").is_some() || value.get("code").is_some())
        {
            return value;
        }
        for line in stream.lines().rev() {
            let trimmed = line.trim();
            if let Ok(value) = serde_json::from_str::<Value>(trimmed)
                && (value.get("error").is_some() || value.get("code").is_some())
            {
                return value;
            }
        }
    }
    panic!("expected JSON error payload\nstdout:\n{stdout}\nstderr:\n{stderr}");
}

#[test]
fn daemon_required_without_grant_or_daemon() {
    let home = tempfile::tempdir().expect("home");
    let project = tempfile::tempdir().expect("project");
    write_project(project.path());
    std::fs::write(home.path().join("machine_id"), "machine-test").expect("machine_id");
    std::fs::write(home.path().join("local_cli_token"), "operator-token").expect("token");
    let listener = TcpListener::bind("127.0.0.1:0").expect("dsn listener");
    let dsn = format!(
        "postgresql://gwiki:gwiki@127.0.0.1:{}/gobby",
        listener.local_addr().expect("addr").port()
    );
    let output = run_gwiki(
        home.path(),
        project.path(),
        &[
            ("GWIKI_TEST_DATABASE_URL", &dsn),
            ("GOBBY_TEST_POSTGRES_DSN", &dsn),
        ],
    );
    listener.set_nonblocking(true).expect("nonblocking");
    assert!(
        listener.accept().is_err(),
        "gwiki must not dial an env DSN before a grant"
    );
    let payload = parse_error_payload(&output);
    let code = payload
        .get("error")
        .or_else(|| payload.get("code"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    assert_eq!(code, "daemon_required");
    assert!(
        payload["message"]
            .as_str()
            .unwrap_or_default()
            .contains("daemon required")
    );
    assert_eq!(output.status.code(), Some(2));
}
