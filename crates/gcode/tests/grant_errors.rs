//! Grant-gated dispatch and CLI error contract tests (plan 2.2).

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use gobby_core::grant::GrantError;
use serde_json::Value;

fn gcode_bin() -> &'static str {
    env!("CARGO_BIN_EXE_gcode")
}

fn write_home(home: &Path) {
    std::fs::write(home.join("machine_id"), "machine-test").expect("machine_id");
    std::fs::write(home.join("local_cli_token"), "operator-token").expect("token");
}

fn write_project(root: &Path, project_id: &str) {
    let gobby = root.join(".gobby");
    std::fs::create_dir_all(&gobby).expect("create .gobby");
    std::fs::write(
        gobby.join("project.json"),
        format!(r#"{{"id":"{project_id}","name":"grant-errors"}}"#),
    )
    .expect("write project.json");
}

fn unused_loopback() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind unused port");
    let port = listener.local_addr().expect("addr").port();
    drop(listener);
    format!("http://127.0.0.1:{port}")
}

fn isolated_gcode(home: &Path, cwd: &Path, args: &[&str]) -> Output {
    isolated_gcode_with_env(home, cwd, args, &[])
}

fn isolated_gcode_with_env(
    home: &Path,
    cwd: &Path,
    args: &[&str],
    extra: &[(&str, &str)],
) -> Output {
    let mut cmd = Command::new(gcode_bin());
    cmd.args(args)
        .current_dir(cwd)
        .env("HOME", home)
        .env("GOBBY_HOME", home)
        .env("GOBBY_DAEMON_URL", unused_loopback())
        .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
        .env_remove("GOBBY_AGENT_API_TOKEN")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in extra {
        cmd.env(key, value);
    }
    let mut child = cmd.spawn().expect("spawn gcode");
    match wait_timeout::ChildExt::wait_timeout(&mut child, Duration::from_secs(8))
        .expect("wait gcode")
    {
        Some(status) => {
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            if let Some(mut out) = child.stdout.take() {
                let _ = out.read_to_end(&mut stdout);
            }
            if let Some(mut err) = child.stderr.take() {
                let _ = err.read_to_end(&mut stderr);
            }
            Output {
                status,
                stdout,
                stderr,
            }
        }
        None => {
            let _ = child.kill();
            let _ = child.wait();
            panic!("gcode timed out: {args:?}");
        }
    }
}

fn parse_error_payload(output: &Output) -> Value {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    for stream in [stderr.as_ref(), stdout.as_ref()] {
        for line in stream.lines().rev() {
            let trimmed = line.trim();
            if let Ok(value) = serde_json::from_str::<Value>(trimmed)
                && value.get("error").is_some()
                && value.get("message").is_some()
            {
                return value;
            }
        }
    }
    panic!("expected JSON error payload\nstdout:\n{stdout}\nstderr:\n{stderr}");
}

fn spawn_accept_counter(listener: TcpListener) -> (Arc<Mutex<usize>>, thread::JoinHandle<()>) {
    let hits = Arc::new(Mutex::new(0usize));
    let hits_clone = Arc::clone(&hits);
    let handle = thread::spawn(move || {
        listener.set_nonblocking(true).ok();
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while std::time::Instant::now() < deadline {
            match listener.accept() {
                Ok(_) => *hits_clone.lock().expect("hits") += 1,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(_) => break,
            }
        }
    });
    (hits, handle)
}

#[test]
fn grant_errors_stable_contract() {
    let cases = [
        (
            GrantError::DaemonRequired,
            "daemon_required",
            "daemon required",
            2,
        ),
        (GrantError::Expired, "expired", "grant expired", 2),
        (
            GrantError::SchemaMismatch,
            "schema_mismatch",
            "schema identity mismatch",
            2,
        ),
        (
            GrantError::DeploymentMismatch,
            "deployment_mismatch",
            "deployment mismatch",
            2,
        ),
        (
            GrantError::ApiContractMismatch {
                grant_contract: Some(99),
                binary_contract: 1,
                source: None,
            },
            "api_contract_mismatch",
            "grant api contract 99 does not match this binary's supported contract 1",
            2,
        ),
        (
            GrantError::RemoteEndpoint,
            "remote_endpoint",
            "remote daemon endpoint refused",
            2,
        ),
        (
            GrantError::ConfigRevisionMismatch,
            "config_revision_mismatch",
            "config revision mismatch",
            2,
        ),
        (GrantError::Revoked, "revoked", "grant revoked", 2),
        (
            GrantError::Timeout,
            "timeout",
            "grant operation timed out",
            2,
        ),
        (
            GrantError::Malformed("bad json".into()),
            "malformed",
            "malformed grant: bad json",
            2,
        ),
        (
            GrantError::Io("disk".into()),
            "io",
            "grant io error: disk",
            1,
        ),
    ];
    for (error, code, message, exit) in cases {
        assert_eq!(error.cli_code(), code, "{error:?}");
        assert_eq!(error.to_string(), message, "{error:?}");
        assert_eq!(error.exit_status(), exit, "{error:?}");
        let json = serde_json::json!({
            "error": error.cli_code(),
            "message": error.to_string(),
        });
        assert_eq!(json["error"], code);
        assert_eq!(json["message"], message);
    }

    let home = tempfile::tempdir().expect("home");
    let project = tempfile::tempdir().expect("project");
    write_home(home.path());
    write_project(project.path(), "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    let output = isolated_gcode(home.path(), project.path(), &["status"]);
    let payload = parse_error_payload(&output);
    assert_eq!(payload["error"], "daemon_required");
    assert_eq!(payload["message"], "daemon required");
    assert_eq!(output.status.code(), Some(2));
}

#[test]
fn no_pregrant_datastore_access() {
    let home = tempfile::tempdir().expect("home");
    let project = tempfile::tempdir().expect("project");
    write_home(home.path());
    write_project(project.path(), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
    std::fs::write(
        home.path().join("bootstrap.yaml"),
        "database_url: postgresql://bootstrap.invalid/gobby\n",
    )
    .expect("bootstrap");

    let listener = TcpListener::bind("127.0.0.1:0").expect("dsn listener");
    let port = listener.local_addr().expect("addr").port();
    let dsn = format!("postgresql://gcode:gcode@127.0.0.1:{port}/gobby");
    let (hits, hits_done) = spawn_accept_counter(listener);

    let output = isolated_gcode_with_env(
        home.path(),
        project.path(),
        &["status"],
        &[
            ("GCODE_TEST_DATABASE_URL", &dsn),
            ("GOBBY_TEST_POSTGRES_DSN", &dsn),
        ],
    );
    let payload = parse_error_payload(&output);
    assert_eq!(payload["error"], "daemon_required");
    hits_done.join().expect("accept counter finished");
    assert_eq!(
        *hits.lock().expect("hits"),
        0,
        "env/bootstrap DSN must not be dialed before a grant"
    );
}

#[test]
fn projectless_rejection() {
    let home = tempfile::tempdir().expect("home");
    let cwd = tempfile::tempdir().expect("empty cwd");
    write_home(home.path());

    let listener = TcpListener::bind("127.0.0.1:0").expect("daemon listener");
    let daemon_url = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("addr").port()
    );
    let (daemon_hits, daemon_done) = spawn_accept_counter(listener);
    let dsn_listener = TcpListener::bind("127.0.0.1:0").expect("dsn listener");
    let dsn = format!(
        "postgresql://gcode:gcode@127.0.0.1:{}/gobby",
        dsn_listener.local_addr().expect("addr").port()
    );
    let (dsn_hits, dsn_done) = spawn_accept_counter(dsn_listener);

    let output = isolated_gcode_with_env(
        home.path(),
        cwd.path(),
        &["status"],
        &[
            ("GOBBY_DAEMON_URL", &daemon_url),
            ("GCODE_TEST_DATABASE_URL", &dsn),
        ],
    );
    let payload = parse_error_payload(&output);
    assert_eq!(payload["error"], "project_required");
    assert_eq!(output.status.code(), Some(2));
    daemon_done.join().expect("daemon accept counter finished");
    dsn_done.join().expect("dsn accept counter finished");
    assert_eq!(
        *daemon_hits.lock().expect("hits"),
        0,
        "projectless dispatch must not call the daemon"
    );
    assert_eq!(
        *dsn_hits.lock().expect("hits"),
        0,
        "projectless dispatch must not touch postgres"
    );
}

#[test]
fn project_name_lookup_authenticated() {
    let home = tempfile::tempdir().expect("home");
    let cwd = tempfile::tempdir().expect("cwd");
    write_home(home.path());

    let seen: Arc<Mutex<Vec<(String, String, String)>>> = Arc::new(Mutex::new(Vec::new()));
    let listener = TcpListener::bind("127.0.0.1:0").expect("scripted daemon");
    let daemon_url = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("addr").port()
    );
    let seen_clone = Arc::clone(&seen);
    thread::spawn(move || {
        listener.set_nonblocking(false).ok();
        while let Ok((mut stream, _)) = listener.accept() {
            let mut buf = [0u8; 4096];
            let n = stream.read(&mut buf).unwrap_or(0);
            let raw = String::from_utf8_lossy(&buf[..n]);
            let first = raw.lines().next().unwrap_or("").to_string();
            let auth = raw
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("authorization:")
                        .map(|value| value.trim().to_string())
                })
                .unwrap_or_default();
            let grant = raw.lines().any(|line| {
                line.to_ascii_lowercase()
                    .starts_with("x-gobby-runtime-grant:")
            });
            seen_clone.lock().expect("seen lock").push((
                first,
                auth,
                if grant { "grant" } else { "" }.to_string(),
            ));
            let body = b"[]";
            let _ = stream.write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .as_bytes(),
            );
            let _ = stream.write_all(body);
        }
    });

    let dsn_listener = TcpListener::bind("127.0.0.1:0").expect("dsn listener");
    let dsn = format!(
        "postgresql://gcode:gcode@127.0.0.1:{}/gobby",
        dsn_listener.local_addr().expect("addr").port()
    );
    let (dsn_hits, dsn_done) = spawn_accept_counter(dsn_listener);
    let output = isolated_gcode_with_env(
        home.path(),
        cwd.path(),
        &["--project", "named-proj", "status"],
        &[
            ("GOBBY_DAEMON_URL", &daemon_url),
            ("GCODE_TEST_DATABASE_URL", &dsn),
        ],
    );
    let requests = seen.lock().expect("seen lock").clone();
    assert!(
        !requests.is_empty(),
        "name lookup must contact the daemon\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let (first_line, auth, _) = &requests[0];
    assert!(
        first_line.contains("GET /api/projects"),
        "first daemon call must be authenticated project lookup, got {first_line}"
    );
    assert!(
        auth.to_ascii_lowercase().starts_with("bearer "),
        "project name lookup must send a bearer token, got {auth:?}"
    );
    dsn_done.join().expect("dsn accept counter finished");
    assert_eq!(
        *dsn_hits.lock().expect("hits"),
        0,
        "name lookup must precede every datastore touch"
    );
}

#[test]
fn project_prune_uses_project_grant() {
    let home = tempfile::tempdir().expect("home");
    let project = tempfile::tempdir().expect("project");
    write_home(home.path());
    write_project(project.path(), "cccccccc-cccc-4ccc-8ccc-cccccccccccc");

    let output = isolated_gcode(
        home.path(),
        project.path(),
        &[
            "prune",
            "--force",
            "--project",
            project.path().to_str().expect("utf8"),
        ],
    );
    let payload = parse_error_payload(&output);
    assert_eq!(
        payload["error"],
        "daemon_required",
        "project prune must acquire a project grant, not POST /api/code-index/prune\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );

    let prune_src = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/commands/status/prune.rs");
    let source = std::fs::read_to_string(&prune_src).expect("read prune.rs");
    assert!(
        source.contains("Context::resolve") || source.contains("acquire"),
        "gcode prune --project must stay on the project-grant path"
    );
    assert!(
        !source.contains("POST /api/code-index/prune")
            || source.contains("prune_global")
            || source.contains("global_prune"),
        "project prune source should keep a distinct global path"
    );
}
