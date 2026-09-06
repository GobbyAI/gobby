//! 3.5 gclient starts independently of the Python CLI.

use gobby_client::startup::{
    parse_args, prepare_at, resolve_probe_env_at, resolve_project_at, start_session,
    GtermHostState, HealthClient, HttpHealthClient, ProbeEnv, Ready, StartupError,
};
use gobby_client::teardown::{ModeBackend, TerminalGuard};
use gobby_terminal::protocol::PROTOCOL_VERSION;
use std::io::{self, Read, Write};
use std::net::TcpListener;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

#[test]
fn version_flag_prints_and_exits_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .arg("--version")
        .output()
        .expect("run gclient --version");

    assert!(
        output.status.success(),
        "--version failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        format!("gclient {}\n", env!("CARGO_PKG_VERSION"))
    );

    let help = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .arg("--help")
        .output()
        .expect("run gclient --help");
    assert!(help.status.success(), "--help must exit zero");
    let help = String::from_utf8_lossy(&help.stdout);
    for flag in ["--project", "--daemon-url", "--token-file", "--version"] {
        assert!(help.contains(flag), "help omitted {flag}: {help}");
    }
}

#[test]
fn daemon_url_overrides_bootstrap_before_raw_mode() {
    let root = tempfile::tempdir().expect("temp project");
    let gobby_dir = root.path().join(".gobby");
    std::fs::create_dir(&gobby_dir).expect("create .gobby");
    std::fs::write(
        gobby_dir.join("project.json"),
        r#"{"id":"11111111-1111-4111-8111-111111111111"}"#,
    )
    .expect("write project id");
    let token_file = root.path().join("token");
    std::fs::write(&token_file, "task-token\n").expect("write token");
    let url = closed_port_url();

    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .args(["--daemon-url", &url, "--token-file"])
        .arg(&token_file)
        .current_dir(root.path())
        .output()
        .expect("run gclient with explicit daemon URL");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(!output.status.success(), "unreachable daemon succeeded");
    assert!(
        stderr.contains(&format!("daemon unreachable at {url}")),
        "explicit daemon URL was not probed: {stderr}"
    );
    assert!(stderr.contains("gobby start"), "missing recovery: {stderr}");

    let controlled_home = root.path().join("controlled-gobby-home");
    std::fs::create_dir(&controlled_home).expect("create controlled home");
    std::fs::write(controlled_home.join("local_cli_token"), "home-token\n")
        .expect("write home token");
    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .args(["--daemon-url", &url])
        .env("GOBBY_HOME", &controlled_home)
        .current_dir(root.path())
        .output()
        .expect("run gclient with default token path");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(&format!("daemon unreachable at {url}")),
        "default token did not reach daemon probe: {stderr}"
    );

    let missing_home = root.path().join("missing-token-home");
    std::fs::create_dir(&missing_home).expect("create missing-token home");
    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .args(["--daemon-url", &url])
        .env("GOBBY_HOME", &missing_home)
        .current_dir(root.path())
        .output()
        .expect("run gclient with missing default token");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("local_cli_token"),
        "missing token path: {stderr}"
    );
    assert!(
        stderr.contains("--token-file"),
        "missing recovery: {stderr}"
    );
    assert!(
        !stderr.contains("daemon unreachable"),
        "daemon was probed before token validation: {stderr}"
    );

    let bootstrap_home = root.path().join("bootstrap-gobby-home");
    std::fs::create_dir(&bootstrap_home).expect("create bootstrap home");
    std::fs::write(bootstrap_home.join("local_cli_token"), "bootstrap-token\n")
        .expect("write bootstrap token");
    std::fs::write(
        bootstrap_home.join("bootstrap.yaml"),
        format!("daemon_url: '{url}/'\n"),
    )
    .expect("write bootstrap");
    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .env("GOBBY_HOME", &bootstrap_home)
        .env_remove("GOBBY_DAEMON_URL")
        .env_remove("GOBBY_PORT")
        .env_remove("GOBBY_DAEMON_PORT")
        .current_dir(root.path())
        .output()
        .expect("run gclient with bootstrap discovery");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(&format!("daemon unreachable at {url}")),
        "bootstrap daemon URL was not discovered: {stderr}"
    );

    let args = parse_args([
        "gclient",
        "--daemon-url",
        &url,
        "--token-file",
        token_file.to_str().expect("UTF-8 token path"),
    ])
    .expect("parse explicit discovery overrides");
    let missing_default = root.path().join("missing-default-token");
    let env = resolve_probe_env_at(&args, "http://bootstrap.invalid", &missing_default)
        .expect("explicit token file");
    assert_eq!(env.daemon_url, url);
    assert_eq!(env.token.as_deref(), Some("task-token"));

    let default_token = root.path().join("local_cli_token");
    std::fs::write(&default_token, "default-token\n").expect("write default token");
    let args = parse_args(["gclient"]).expect("parse defaults");
    let env = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &default_token)
        .expect("default token file");
    assert_eq!(env.daemon_url, "http://bootstrap.test:60887");
    assert_eq!(env.token.as_deref(), Some("default-token"));

    let missing = root.path().join("missing-token");
    let error = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &missing)
        .expect_err("missing default token succeeded");
    let message = error.to_string();
    assert!(message.contains(&missing.display().to_string()));
    assert!(message.contains("--token-file"));

    let unreadable = root.path().join("token-directory");
    std::fs::create_dir(&unreadable).expect("create unreadable token path");
    let error = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &unreadable)
        .expect_err("directory token path succeeded");
    assert!(error.to_string().contains("--token-file"));
}

struct CountingBackend {
    enters: Arc<AtomicUsize>,
}

struct CountingHealth {
    calls: Arc<AtomicUsize>,
}

impl CountingHealth {
    fn new() -> (Self, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        (
            Self {
                calls: Arc::clone(&calls),
            },
            calls,
        )
    }
}

impl HealthClient for CountingHealth {
    fn fetch_health(&self, _daemon_url: &str) -> Result<Option<GtermHostState>, StartupError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Ok(None)
    }
}

impl CountingBackend {
    fn new() -> (Self, Arc<AtomicUsize>) {
        let enters = Arc::new(AtomicUsize::new(0));
        (
            Self {
                enters: Arc::clone(&enters),
            },
            enters,
        )
    }
}

impl ModeBackend for CountingBackend {
    fn enter(&mut self) -> io::Result<()> {
        self.enters.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn closed_port_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let addr = listener.local_addr().expect("addr");
    drop(listener);
    format!("http://{addr}")
}

fn serve_health_json(body: &str) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind health");
    listener.set_nonblocking(false).expect("blocking accept");
    let addr = listener.local_addr().expect("addr");
    let body = body.to_string();
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept health");
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("read timeout");
        let mut buf = [0u8; 4096];
        let mut collected = Vec::new();
        loop {
            match stream.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    collected.extend_from_slice(&buf[..n]);
                    if collected.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
                Err(err) if err.kind() == io::ErrorKind::TimedOut => break,
                Err(_) => break,
            }
        }
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes());
        let _ = stream.flush();
    });
    (format!("http://{addr}"), handle)
}

fn env_at(url: &str) -> ProbeEnv {
    ProbeEnv {
        daemon_url: url.to_string(),
        token: None,
    }
}

fn host_args(url: &str, remote: bool) -> gobby_client::startup::CliArgs {
    let project_id = "33333333-3333-4333-8333-333333333333";
    if remote {
        parse_args(["gclient", "--project", project_id, "--daemon-url", url])
            .expect("parse remote host args")
    } else {
        parse_args(["gclient", "--project", project_id]).expect("parse local host args")
    }
}

#[test]
fn test_unreachable_daemon_before_raw_mode() {
    let url = closed_port_url();
    let args = parse_args(["gclient"]).expect("parse");
    let (backend, enters) = CountingBackend::new();
    let err = match start_session(args, env_at(&url), &HttpHealthClient::new(), backend) {
        Err(err) => err,
        Ok(_) => panic!("unreachable daemon succeeded"),
    };
    assert_eq!(
        enters.load(Ordering::SeqCst),
        0,
        "raw-mode must not run before the daemon probe fails"
    );
    let message = err.to_string();
    assert!(
        message.contains("gobby start"),
        "actionable recovery missing `gobby start`: {message}"
    );
    assert!(
        message.contains("gobby status"),
        "actionable recovery missing `gobby status`: {message}"
    );
    assert!(
        matches!(err, StartupError::Unreachable { .. }),
        "distinct unreachable error, got {err:?}"
    );
}

#[test]
fn test_reports_degraded_host_state() {
    let root = tempfile::tempdir().expect("temp token home");
    let token_file = root.path().join("local_cli_token");
    std::fs::write(&token_file, "host-token").expect("write host token");
    let usable = format!(
        r#"{{
            "status": "degraded",
            "degraded_services": ["gterm_host"],
            "gterm_host": {{
                "enabled": false,
                "running": true,
                "adopted": true,
                "host_epoch": "epoch-recovered",
                "protocol_version": {PROTOCOL_VERSION},
                "restart_count": 3,
                "backoff_seconds": 4.5,
                "last_error": "stale socket failure"
            }}
        }}"#
    );

    for remote in [false, true] {
        let (url, server) = serve_health_json(&usable);
        let args = host_args(&url, remote);
        let fallback = if remote {
            "http://bootstrap.invalid"
        } else {
            &url
        };
        let env = resolve_probe_env_at(&args, fallback, &token_file).expect("resolve host env");
        assert_eq!(env.daemon_url, url);
        let (backend, enters) = CountingBackend::new();
        let (ready, guard) = start_session(args, env, &HttpHealthClient::new(), backend)
            .expect("matching running host must proceed");
        server.join().expect("join health server");
        assert_eq!(enters.load(Ordering::SeqCst), 1);
        let notice = ready.host_notice.expect("stale error notice");
        assert!(notice.contains("stale socket failure"));
        assert!(notice.contains("restart_count=3"));
        assert!(notice.contains("backoff_seconds=4.5"));
        drop(guard);
    }

    let unavailable = [
        (
            "absent",
            r#"{"status":"degraded","degraded_services":["gterm_host"]}"#.to_string(),
            "protocol_version=none",
        ),
        (
            "stopped",
            format!(
                r#"{{"gterm_host":{{"running":false,"adopted":true,"host_epoch":"epoch-7","protocol_version":{PROTOCOL_VERSION},"restart_count":2,"backoff_seconds":1.5,"last_error":"host stopped"}}}}"#
            ),
            "protocol_version=1",
        ),
        (
            "mismatched",
            r#"{"gterm_host":{"running":true,"adopted":true,"host_epoch":"epoch-7","protocol_version":999,"restart_count":3,"backoff_seconds":4.5,"last_error":"socket missing"}}"#.to_string(),
            "protocol_version=999",
        ),
    ];
    for (name, body, observed_version) in unavailable {
        for remote in [false, true] {
            let (url, server) = serve_health_json(&body);
            let args = host_args(&url, remote);
            let fallback = if remote {
                "http://bootstrap.invalid"
            } else {
                &url
            };
            let env = resolve_probe_env_at(&args, fallback, &token_file).expect("resolve host env");
            let (backend, enters) = CountingBackend::new();
            let error = match start_session(args, env, &HttpHealthClient::new(), backend) {
                Err(error) => error,
                Ok(_) => panic!("{name} host succeeded on remote={remote}"),
            };
            server.join().expect("join health server");
            assert_eq!(enters.load(Ordering::SeqCst), 0, "{name} remote={remote}");
            let message = error.to_string();
            assert!(message.contains(observed_version), "{name}: {message}");
            assert!(
                message.contains(&format!("expected_protocol_version={PROTOCOL_VERSION}")),
                "{name}: {message}"
            );
            assert!(
                matches!(error, StartupError::DegradedHost { .. }),
                "{name}: {error:?}"
            );
        }
    }

    let views_source = include_str!("../src/views/mod.rs");
    assert!(
        views_source.contains("chrome.status_message = ready.host_notice;"),
        "host notice must reach the status bar"
    );
}

#[test]
fn project_is_resolved_before_raw_mode() {
    let root = tempfile::tempdir().expect("temp project");
    let gobby_dir = root.path().join(".gobby");
    std::fs::create_dir(&gobby_dir).expect("create .gobby");
    let project_id = "22222222-2222-4222-8222-222222222222";
    std::fs::write(
        gobby_dir.join("project.json"),
        format!(r#"{{"id":"{project_id}"}}"#),
    )
    .expect("write project id");
    let nested = root.path().join("nested/deeper");
    std::fs::create_dir_all(&nested).expect("create nested project path");

    assert_eq!(
        resolve_project_at(None, &nested).expect("discover containing project"),
        project_id
    );
    assert_eq!(
        resolve_project_at(Some(project_id), &nested).expect("accept project UUID"),
        project_id
    );
    assert_eq!(
        resolve_project_at(
            Some(root.path().to_str().expect("UTF-8 project path")),
            &nested
        )
        .expect("resolve project path"),
        project_id
    );
    let body = r#"{
        "status": "ok",
        "degraded_services": [],
        "gterm_host": {
            "enabled": true,
            "running": true,
            "adopted": false,
            "host_epoch": "epoch-ok",
            "protocol_version": 1,
            "restart_count": 0,
            "last_error": null
        }
    }"#;
    let (url, server) = serve_health_json(body);
    let project_path = root.path().to_string_lossy().into_owned();
    let args = parse_args(["gclient", "--project", &project_path]).expect("parse --project");
    let (backend, enters) = CountingBackend::new();
    let (ready, guard): (_, TerminalGuard<CountingBackend>) =
        match start_session(args, env_at(&url), &HttpHealthClient::new(), backend) {
            Ok(ok) => ok,
            Err(err) => panic!("healthy daemon failed: {err}"),
        };
    let _ = server.join();
    assert_eq!(ready.project, project_id);
    assert_eq!(enters.load(Ordering::SeqCst), 1);
    drop(guard);

    let outside = tempfile::tempdir().expect("outside project");
    let args = parse_args(["gclient"]).expect("parse discovery");
    let (health, health_calls) = CountingHealth::new();
    let error = prepare_at(&args, env_at("http://unused"), &health, outside.path())
        .expect_err("outside-project startup succeeded");
    assert_eq!(health_calls.load(Ordering::SeqCst), 0);
    let message = error.to_string();
    assert!(message.contains(&outside.path().display().to_string()));
    assert!(message.contains("gobby init"));
    assert!(message.contains("--project"));

    let unreadable = tempfile::tempdir().expect("unreadable project");
    let unreadable_gobby = unreadable.path().join(".gobby");
    std::fs::create_dir(&unreadable_gobby).expect("create unreadable .gobby");
    std::fs::write(unreadable_gobby.join("project.json"), "not JSON")
        .expect("write malformed project id");
    let (health, health_calls) = CountingHealth::new();
    let error = prepare_at(&args, env_at("http://unused"), &health, unreadable.path())
        .expect_err("unreadable project id succeeded");
    assert_eq!(health_calls.load(Ordering::SeqCst), 0);
    let message = error.to_string();
    assert!(message.contains(&unreadable.path().display().to_string()));
    assert!(message.contains("gobby init"));
    assert!(message.contains("--project"));
}

#[test]
fn ready_carries_a_resolved_project() {
    fn project_field_is_string(ready: &Ready) -> &String {
        &ready.project
    }

    let _: fn(&Ready) -> &String = project_field_is_string;
    let views_source = include_str!("../src/views/mod.rs");
    assert!(
        views_source.contains("workspace.select_project(ready.project);"),
        "run_ready must select the resolved project"
    );
    assert!(
        !views_source.contains("if let Some(project) = ready.project"),
        "run_ready retained a project-less branch"
    );
}
