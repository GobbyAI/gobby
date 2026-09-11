//! 3.5 gclient starts independently of the Python CLI.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_client::persist::ClientSession;
use gobby_client::prefs::{load_prefs, save_prefs};
use gobby_client::startup::{
    initial_project, parse_args, prepare_at, resolve_probe_env_at, resolve_project_at,
    start_session, start_session_at, GtermHostState, HealthClient, HttpHealthClient, ProbeEnv,
    Ready, StartupError,
};
use gobby_client::teardown::{ModeBackend, RecordingBackend, TerminalGuard};
use gobby_client::ui::keymap::{Action, Keymap, HERDR_PREFIX};
use gobby_client::ui::settings::{render_settings, AgentSort, ClientPrefs};
use gobby_client::ui::Chrome;
use gobby_client::FrameDelivery;
use gobby_core::project::PERSONAL_PROJECT_ID;
use gobby_terminal::protocol::PROTOCOL_VERSION;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
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
    let env = resolve_probe_env_at(&args, "http://bootstrap.invalid", &missing_default, false)
        .expect("explicit token file");
    assert_eq!(env.daemon_url, url);
    assert_eq!(env.token.as_deref(), Some("task-token"));

    let default_token = root.path().join("local_cli_token");
    std::fs::write(&default_token, "default-token\n").expect("write default token");
    let args = parse_args(["gclient"]).expect("parse defaults");
    let env = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &default_token, false)
        .expect("default token file");
    assert_eq!(env.daemon_url, "http://bootstrap.test:60887");
    assert_eq!(env.token.as_deref(), Some("default-token"));

    let missing = root.path().join("missing-token");
    let error = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &missing, false)
        .expect_err("missing default token succeeded");
    let message = error.to_string();
    assert!(message.contains(&missing.display().to_string()));
    assert!(message.contains("--token-file"));

    let unreadable = root.path().join("token-directory");
    std::fs::create_dir(&unreadable).expect("create unreadable token path");
    let error = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &unreadable, false)
        .expect_err("directory token path succeeded");
    assert!(error.to_string().contains("--token-file"));
}

/// `--frame-delivery` is the only way to reach the proxy transport on one
/// machine, where the frame host socket is always reachable and `auto` always
/// resolves to direct.
#[test]
fn frame_delivery_parses_both_spellings_and_defaults_to_auto() {
    assert_eq!(
        parse_args(["gclient"])
            .expect("parse defaults")
            .frame_delivery,
        FrameDelivery::Auto,
        "omitting the flag must not change today's negotiation"
    );

    for (value, expected) in [
        ("auto", FrameDelivery::Auto),
        ("direct", FrameDelivery::Direct),
        ("proxy", FrameDelivery::Proxy),
    ] {
        assert_eq!(
            parse_args(["gclient", "--frame-delivery", value])
                .expect("parse separated value")
                .frame_delivery,
            expected
        );
        assert_eq!(
            parse_args(["gclient", &format!("--frame-delivery={value}")])
                .expect("parse joined value")
                .frame_delivery,
            expected
        );
    }
}

#[test]
fn frame_delivery_rejects_unknown_values_and_a_swallowed_flag() {
    // A missing value would otherwise consume the next flag as its argument.
    for argv in [
        vec!["gclient", "--frame-delivery"],
        vec!["gclient", "--frame-delivery", "sideways"],
        vec!["gclient", "--frame-delivery="],
        vec!["gclient", "--frame-delivery", "--version"],
    ] {
        let message = parse_args(&argv)
            .expect_err(&format!("{argv:?} parsed"))
            .to_string();
        assert!(
            message.contains("--frame-delivery requires auto, direct, or proxy"),
            "{argv:?} gave an unhelpful error: {message}"
        );
    }
}

#[test]
fn help_text_lists_frame_delivery() {
    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .arg("--help")
        .output()
        .expect("run gclient --help");
    assert!(output.status.success(), "--help did not exit zero");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("--frame-delivery auto|direct|proxy"),
        "usage text omits the flag: {stdout}"
    );
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
        nested_tmux: false,
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
        let env =
            resolve_probe_env_at(&args, fallback, &token_file, false).expect("resolve host env");
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
            let env = resolve_probe_env_at(&args, fallback, &token_file, false)
                .expect("resolve host env");
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
        resolve_project_at(None, &nested)
            .expect("discover containing project")
            .as_deref(),
        Some(project_id)
    );
    assert_eq!(
        resolve_project_at(Some(project_id), &nested)
            .expect("accept project UUID")
            .as_deref(),
        Some(project_id)
    );
    assert_eq!(
        resolve_project_at(
            Some(root.path().to_str().expect("UTF-8 project path")),
            &nested
        )
        .expect("resolve project path")
        .as_deref(),
        Some(project_id)
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
    assert_eq!(ready.project.as_deref(), Some(project_id));
    // `start_session` launches from the real working directory.
    assert_eq!(
        ready.launch_dir,
        std::env::current_dir().expect("current directory")
    );
    assert_eq!(enters.load(Ordering::SeqCst), 1);
    drop(guard);

    // Outside every checkout the client still starts: no project, the
    // launch directory kept for its first shell, and the probe reached.
    let outside = tempfile::tempdir().expect("outside project");
    let args = parse_args(["gclient"]).expect("parse discovery");
    let ready = prepare_at(
        &args,
        env_at("http://unused"),
        &HealthyHost,
        outside.path(),
        outside.path(),
    )
    .expect("outside-project startup reaches the health probe");
    assert_eq!(ready.project, None);
    assert_eq!(ready.launch_dir, outside.path());

    // An explicit `--project` that does not resolve still fails first.
    let missing = outside.path().join("missing").display().to_string();
    let args = parse_args(["gclient", "--project", &missing]).expect("parse --project");
    let (health, health_calls) = CountingHealth::new();
    let error = prepare_at(
        &args,
        env_at("http://unused"),
        &health,
        outside.path(),
        outside.path(),
    )
    .expect_err("missing explicit project succeeded");
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
    let error = prepare_at(
        &args,
        env_at("http://unused"),
        &health,
        unreadable.path(),
        unreadable.path(),
    )
    .expect_err("unreadable project id succeeded");
    assert_eq!(health_calls.load(Ordering::SeqCst), 0);
    let message = error.to_string();
    assert!(message.contains(&unreadable.path().display().to_string()));
    assert!(message.contains("gobby init"));
    assert!(message.contains("--project"));
}

/// `Ready.project` is optional and `run_ready` opens on `initial_project`:
/// the resolved project, else the one the saved session had focused, else
/// the personal project.
#[test]
fn ready_carries_an_optional_project() {
    fn project_field_is_optional(ready: &Ready) -> Option<&str> {
        ready.project.as_deref()
    }

    let _: fn(&Ready) -> Option<&str> = project_field_is_optional;
    let views_source = include_str!("../src/views/mod.rs");
    assert!(
        views_source.contains("initial_project(ready.project, session.as_ref())"),
        "run_ready must open on the initial project"
    );
    let saved = ClientSession {
        focused_project: Some("saved-project".to_string()),
        ..ClientSession::default()
    };
    assert_eq!(
        initial_project(Some("cwd-project".to_string()), Some(&saved)),
        "cwd-project"
    );
    assert_eq!(initial_project(None, Some(&saved)), "saved-project");
    assert_eq!(
        initial_project(None, Some(&ClientSession::default())),
        PERSONAL_PROJECT_ID
    );
    assert_eq!(initial_project(None, None), PERSONAL_PROJECT_ID);
}

struct HealthyHost;

impl HealthClient for HealthyHost {
    fn fetch_health(&self, _daemon_url: &str) -> Result<Option<GtermHostState>, StartupError> {
        Ok(Some(GtermHostState {
            enabled: true,
            running: true,
            adopted: false,
            host_epoch: Some("epoch-ok".to_string()),
            protocol_version: Some(PROTOCOL_VERSION),
            restart_count: 0,
            backoff_seconds: 0.0,
            last_error: None,
        }))
    }
}

#[test]
fn no_mouse_flag_and_prefs_file_shape_ready() {
    let project_id = "44444444-4444-4444-8444-444444444444";
    assert!(!parse_args(["gclient"]).expect("parse defaults").no_mouse);
    let plain = parse_args(["gclient", "--project", project_id]).expect("parse --project");
    let no_mouse =
        parse_args(["gclient", "--project", project_id, "--no-mouse"]).expect("parse --no-mouse");
    assert!(no_mouse.no_mouse);

    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let env = || env_at("http://unused");

    // No prefs file: defaults, and the flag forces mouse capture off.
    let ready = prepare_at(&plain, env(), &HealthyHost, cwd.path(), home.path())
        .expect("missing prefs file yields defaults");
    assert_eq!(ready.prefs, ClientPrefs::default());
    assert!(ready.prefs.mouse_capture);
    assert_eq!(ready.gobby_home.as_path(), home.path());
    let ready = prepare_at(&no_mouse, env(), &HealthyHost, cwd.path(), home.path())
        .expect("--no-mouse without a prefs file");
    assert!(!ready.prefs.mouse_capture);

    // The [ui]/[keymap] file shape is honored; --no-mouse still wins.
    let path = home.path().join("client").join("prefs.toml");
    std::fs::create_dir_all(path.parent().expect("prefs parent")).expect("create client dir");
    std::fs::write(
        &path,
        "[ui]\ntheme = \"light\"\nmouse_capture = true\nsidebar_width = 31\n[keymap]\npath = \"custom.toml\"\n",
    )
    .expect("write prefs");
    let ready = prepare_at(&plain, env(), &HealthyHost, cwd.path(), home.path())
        .expect("shaped prefs file");
    assert_eq!(ready.prefs.theme, "light");
    assert_eq!(ready.prefs.sidebar_width, 31);
    assert_eq!(ready.prefs.keybinds, "custom.toml");
    assert!(ready.prefs.mouse_capture);
    let ready = prepare_at(&no_mouse, env(), &HealthyHost, cwd.path(), home.path())
        .expect("--no-mouse over a prefs file");
    assert!(!ready.prefs.mouse_capture);

    // A malformed file fails before the health probe and names the path and key.
    std::fs::write(&path, "[ui]\nmouse_captur = false\n").expect("write malformed prefs");
    let (health, health_calls) = CountingHealth::new();
    let error = prepare_at(&plain, env(), &health, cwd.path(), home.path())
        .expect_err("malformed prefs file succeeded");
    assert_eq!(health_calls.load(Ordering::SeqCst), 0);
    assert!(matches!(error, StartupError::Prefs { .. }), "{error:?}");
    let message = error.to_string();
    assert!(message.contains(&path.display().to_string()), "{message}");
    assert!(message.contains("mouse_captur"), "{message}");

    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .arg("--help")
        .output()
        .expect("run gclient --help");
    assert!(output.status.success(), "--help did not exit zero");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("--no-mouse"),
        "usage text omits --no-mouse: {stdout}"
    );
}

/// 3.2.3 `[ui] agent_sort`: read at startup, shown on the settings dialog,
/// written back by the prefs file, and an unknown value names its line.
#[test]
fn agent_sort_pref_round_trips() {
    let project_id = "66666666-6666-4666-8666-666666666666";
    let args = parse_args(["gclient", "--project", project_id]).expect("parse args");
    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let path = home.path().join("client").join("prefs.toml");
    std::fs::create_dir_all(path.parent().expect("prefs parent")).expect("create client dir");
    std::fs::write(&path, "[ui]\nagent_sort = \"priority\"\n").expect("write prefs");

    let ready = prepare_at(
        &args,
        env_at("http://unused"),
        &HealthyHost,
        cwd.path(),
        home.path(),
    )
    .expect("prefs file with agent_sort");
    assert_eq!(ready.prefs.agent_sort, AgentSort::Priority);

    save_prefs(home.path(), &ready.prefs).expect("save prefs");
    let reloaded = load_prefs(home.path()).expect("load saved prefs");
    assert_eq!(reloaded.agent_sort, AgentSort::Priority);
    assert_eq!(reloaded, ready.prefs);

    let mut chrome = Chrome::dark();
    chrome.prefs = ready.prefs;
    let mut terminal = Terminal::new(TestBackend::new(80, 30)).expect("test terminal");
    terminal
        .draw(|frame| {
            render_settings(frame, frame.area(), &chrome);
        })
        .expect("render settings");
    let screen: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(screen.contains("agent sort"), "settings dialog: {screen:?}");
    assert!(screen.contains("priority"), "settings dialog: {screen:?}");

    std::fs::write(&path, "[ui]\nagent_sort = \"sideways\"\n").expect("write malformed prefs");
    let error = prepare_at(
        &args,
        env_at("http://unused"),
        &HealthyHost,
        cwd.path(),
        home.path(),
    )
    .expect_err("an unknown agent sort was accepted");
    assert!(matches!(error, StartupError::Prefs { .. }), "{error:?}");
    let message = error.to_string();
    assert!(message.contains("line 2"), "{message}");
    assert!(message.contains("sideways"), "{message}");
}

#[test]
fn start_session_arms_mouse_capture_from_prefs() {
    let project_id = "55555555-5555-4555-8555-555555555555";
    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let session = |no_mouse: bool| {
        let args = if no_mouse {
            parse_args(["gclient", "--project", project_id, "--no-mouse"])
        } else {
            parse_args(["gclient", "--project", project_id])
        }
        .expect("parse args");
        let backend = RecordingBackend::default();
        let captured = backend.mouse_capture();
        let (ready, guard) = start_session_at(
            args,
            env_at("http://unused"),
            &HealthyHost,
            backend,
            cwd.path(),
            home.path(),
        )
        .expect("start session");
        (ready, guard, captured)
    };

    // Default prefs enable capture while the guard is armed and release it on drop.
    let (ready, guard, captured) = session(false);
    assert!(ready.prefs.mouse_capture);
    assert!(
        captured.load(Ordering::SeqCst),
        "default prefs arm mouse capture"
    );
    drop(guard);
    assert!(
        !captured.load(Ordering::SeqCst),
        "dropping the guard disables capture"
    );

    // --no-mouse keeps capture off.
    let (ready, guard, captured) = session(true);
    assert!(!ready.prefs.mouse_capture);
    assert!(
        !captured.load(Ordering::SeqCst),
        "--no-mouse never enables capture"
    );
    drop(guard);

    // A prefs file with mouse_capture = false keeps capture off too.
    let path = home.path().join("client").join("prefs.toml");
    std::fs::create_dir_all(path.parent().expect("prefs parent")).expect("create client dir");
    std::fs::write(&path, "[ui]\nmouse_capture = false\n").expect("write prefs");
    let (ready, guard, captured) = session(false);
    assert!(!ready.prefs.mouse_capture);
    assert!(
        !captured.load(Ordering::SeqCst),
        "prefs file keeps capture off"
    );
    drop(guard);
}

/// 4.2.1: the keymap override file is resolved and loaded before the health
/// probe. A malformed file fails loud naming its path, a missing one is the
/// default keymap, and `[keymap] path` in prefs redirects the lookup.
#[test]
fn keymap_overrides_load_or_fail_loud() {
    let project_id = "66666666-6666-4666-8666-666666666666";
    let args = parse_args(["gclient", "--project", project_id]).expect("parse args");
    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let env = || env_at("http://unused");
    let f = |n: u8| KeyEvent::new(KeyCode::F(n), KeyModifiers::NONE);
    let ch = |c: char| KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE);

    // No override file: the defaults.
    let ready = prepare_at(&args, env(), &HealthyHost, cwd.path(), home.path())
        .expect("missing keymap file yields defaults");
    assert_eq!(ready.keymap.lookup_prefix(&ch('?')), Some(Action::Help));
    assert_eq!(
        ready.keymap.active_chords(),
        Keymap::defaults(HERDR_PREFIX).active_chords()
    );

    // The client-local file rebinds help and new_project; the replaced
    // chords are released.
    let client_dir = home.path().join("client");
    std::fs::create_dir_all(&client_dir).expect("create client dir");
    std::fs::write(
        client_dir.join("keymap.toml"),
        "[bindings]\nhelp = \"prefix+f1\"\nnew_project = \"prefix+f2\"\n",
    )
    .expect("write keymap");
    let ready = prepare_at(&args, env(), &HealthyHost, cwd.path(), home.path())
        .expect("override file loads");
    assert_eq!(ready.keymap.lookup_prefix(&f(1)), Some(Action::Help));
    assert_eq!(ready.keymap.lookup_prefix(&f(2)), Some(Action::NewProject));
    assert_eq!(ready.keymap.lookup_prefix(&ch('?')), None);
    assert_eq!(
        ready
            .keymap
            .lookup_prefix(&KeyEvent::new(KeyCode::Char('N'), KeyModifiers::SHIFT)),
        None
    );

    // `[keymap] path` in prefs redirects the lookup; a relative path lands
    // under the gobby home and the client-local file is no longer consulted.
    std::fs::write(
        client_dir.join("prefs.toml"),
        "[keymap]\npath = \"keys/mine.toml\"\n",
    )
    .expect("write prefs");
    let custom = home.path().join("keys").join("mine.toml");
    std::fs::create_dir_all(custom.parent().expect("keys parent")).expect("create keys dir");
    std::fs::write(&custom, "[bindings]\nhelp = \"prefix+f3\"\n").expect("write custom keymap");
    let ready = prepare_at(&args, env(), &HealthyHost, cwd.path(), home.path())
        .expect("prefs keymap path loads");
    assert_eq!(ready.keymap.lookup_prefix(&f(3)), Some(Action::Help));
    assert_eq!(ready.keymap.lookup_prefix(&f(1)), None);
    assert_eq!(ready.keymap.lookup_prefix(&f(2)), None);

    // A malformed file fails before the health probe and names the path and
    // the parse error.
    std::fs::write(&custom, "[bindings\nhelp = 1\n").expect("write malformed keymap");
    let (health, health_calls) = CountingHealth::new();
    let error = prepare_at(&args, env(), &health, cwd.path(), home.path())
        .expect_err("malformed keymap file succeeded");
    assert_eq!(health_calls.load(Ordering::SeqCst), 0);
    assert!(matches!(error, StartupError::Keymap { .. }), "{error:?}");
    let message = error.to_string();
    assert!(message.contains(&custom.display().to_string()), "{message}");
    assert!(message.contains("not valid TOML"), "{message}");
}
