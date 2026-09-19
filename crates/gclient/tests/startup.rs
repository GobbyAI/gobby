//! 3.5 gclient starts independently of the Python CLI.

mod mock_daemon;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::LiveDaemon;
use gobby_client::prefs::{load_prefs, prefs_path, save_prefs, PrefsError};
use gobby_client::startup::{
    initial_project, parse_args, prepare_at, resolve_probe_env_at, resolve_project_at,
    start_session, start_session_at, AttachTarget, GtermHostState, HealthClient, HttpHealthClient,
    ProbeEnv, Ready, StartupError,
};
use gobby_client::teardown::{ModeBackend, RecordingBackend, TerminalGuard};
use gobby_client::ui::keymap::{default_prefix, Action, Keymap, HERDR_PREFIX};
use gobby_client::ui::settings::{render_settings, AgentSort, ClientPrefs, PassthroughModifier};
use gobby_client::ui::Chrome;
use gobby_client::{FrameDelivery, Workspace};
use gobby_core::project::PERSONAL_PROJECT_ID;
use gobby_terminal::protocol::PROTOCOL_VERSION;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Read, Write};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::timeout;

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

    let output = detached_gclient()
        .args(["--daemon-url", &url, "--token-file"])
        .arg(&token_file)
        .current_dir(root.path())
        .output()
        .expect("run gclient with explicit daemon URL");
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(
        stderr.contains(&format!("daemon unreachable at {url}")),
        "explicit daemon URL was not probed: {stderr}"
    );
    assert!(stderr.contains("gobby start"), "missing recovery: {stderr}");

    let controlled_home = root.path().join("controlled-gobby-home");
    std::fs::create_dir(&controlled_home).expect("create controlled home");
    std::fs::write(controlled_home.join("local_cli_token"), "home-token\n")
        .expect("write home token");
    let output = detached_gclient()
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
    let output = detached_gclient()
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
    let output = detached_gclient()
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
    let env = resolve_probe_env_at(
        &args,
        "http://bootstrap.invalid",
        &missing_default,
        false,
        false,
    )
    .expect("explicit token file");
    assert_eq!(env.daemon_url, url);
    assert_eq!(env.token.as_deref(), Some("task-token"));

    let default_token = root.path().join("local_cli_token");
    std::fs::write(&default_token, "default-token\n").expect("write default token");
    let args = parse_args(["gclient"]).expect("parse defaults");
    let env = resolve_probe_env_at(
        &args,
        "http://bootstrap.test:60887",
        &default_token,
        false,
        false,
    )
    .expect("default token file");
    assert_eq!(env.daemon_url, "http://bootstrap.test:60887");
    assert_eq!(env.token.as_deref(), Some("default-token"));

    let missing = root.path().join("missing-token");
    let error = resolve_probe_env_at(&args, "http://bootstrap.test:60887", &missing, false, false)
        .expect_err("missing default token succeeded");
    let message = error.to_string();
    assert!(message.contains(&missing.display().to_string()));
    assert!(message.contains("--token-file"));

    let unreadable = root.path().join("token-directory");
    std::fs::create_dir(&unreadable).expect("create unreadable token path");
    let error = resolve_probe_env_at(
        &args,
        "http://bootstrap.test:60887",
        &unreadable,
        false,
        false,
    )
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

#[test]
fn rejects_missing_project_values() {
    for argv in [
        vec!["gclient", "--project"],
        vec!["gclient", "--project="],
        vec!["gclient", "--project", "--no-mouse"],
    ] {
        let message = parse_args(argv.iter().copied())
            .expect_err(&format!("{argv:?} parsed"))
            .to_string();
        assert_eq!(message, "--project requires a project", "{argv:?}");
    }
}

/// 4.3.1: `--node` and `--workspace` parse, both absent leaves the attach
/// target empty so the daemon picks the local `default`, and a full
/// `n#:w#` ref carries its own node over `--node`.
#[test]
fn workspace_flags_default_to_the_local_default() {
    let target = |node: Option<&str>, workspace: Option<&str>| AttachTarget {
        node: node.map(str::to_string),
        workspace: workspace.map(str::to_string),
    };
    let attach = |argv: &[&str]| {
        let args = parse_args(argv.iter().copied()).expect("flags parse");
        AttachTarget::from_flags(args.node.as_deref(), args.workspace.as_deref())
    };

    let args = parse_args(["gclient"]).expect("bare invocation parses");
    assert_eq!(args.node, None);
    assert_eq!(args.workspace, None);
    assert_eq!(attach(&["gclient"]), AttachTarget::default());
    assert_eq!(AttachTarget::default(), target(None, None));

    let args = parse_args(["gclient", "--node", "5", "--workspace=2:1"]).expect("full ref parses");
    assert_eq!(args.node.as_deref(), Some("5"));
    assert_eq!(args.workspace.as_deref(), Some("2:1"));
    assert_eq!(
        attach(&["gclient", "--node", "5", "--workspace=2:1"]),
        target(Some("2"), Some("1")),
        "the ref's node wins over --node"
    );
    assert_eq!(
        attach(&["gclient", "--node=5", "--workspace", "1"]),
        target(Some("5"), Some("1"))
    );
    assert_eq!(
        attach(&["gclient", "--workspace", "default"]),
        target(None, Some("default"))
    );
    assert_eq!(attach(&["gclient", "--node", "5"]), target(Some("5"), None));

    for argv in [
        vec!["gclient", "--node"],
        vec!["gclient", "--node="],
        vec!["gclient", "--workspace"],
        vec!["gclient", "--workspace", "--no-mouse"],
    ] {
        let error = parse_args(argv.iter().copied()).expect_err("a value-less flag parsed");
        assert!(
            matches!(error, StartupError::Usage { .. }),
            "{argv:?}: {error:?}"
        );
    }

    // The target rides on `Ready` so the window attaches to it.
    let project_id = "77777777-7777-4777-8777-777777777777";
    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let args = parse_args(["gclient", "--project", project_id, "--workspace", "2:1"])
        .expect("ready args parse");
    let ready = prepare_at(
        &args,
        env_at("http://unused"),
        &HealthyHost,
        cwd.path(),
        home.path(),
    )
    .expect("prepare with a workspace ref");
    assert_eq!(ready.attach, target(Some("2"), Some("1")));

    let output = Command::new(env!("CARGO_BIN_EXE_gclient"))
        .arg("--help")
        .output()
        .expect("run gclient --help");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("--node NODE") && stdout.contains("--workspace WORKSPACE"),
        "usage text omits the workspace flags: {stdout}"
    );
}

/// 4.3.2: the sidebar collapse, project order, and project labels round-trip
/// through `prefs.toml`, and no client source reads or writes a snapshot or
/// session file any more.
#[test]
fn prefs_carry_sidebar_and_project_preferences() {
    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let client_dir = home.path().join("client");
    fs::create_dir_all(&client_dir).expect("create client dir");
    fs::write(
        client_dir.join("prefs.toml"),
        "[ui]\nsidebar_collapsed = true\nsidebar_width = 30\nproject_order = [\"b\", \"a\"]\n\n\
         [ui.project_labels]\na = \"Alpha\"\n",
    )
    .expect("write prefs");
    let args = parse_args(["gclient"]).expect("parse args");
    let ready = prepare_at(
        &args,
        env_at("http://unused"),
        &HealthyHost,
        cwd.path(),
        home.path(),
    )
    .expect("prefs with sidebar keys load");
    let labels = BTreeMap::from([("a".to_string(), "Alpha".to_string())]);
    assert!(ready.prefs.sidebar_collapsed);
    assert_eq!(ready.prefs.project_order, ["b", "a"]);
    assert_eq!(ready.prefs.project_labels, labels);

    let mut chrome = Chrome::dark();
    chrome.apply_prefs(ready.prefs.clone());
    assert!(chrome.sidebar.collapsed, "prefs seed the sidebar collapse");
    assert_eq!(chrome.sidebar.width, 30);
    assert_eq!(chrome.sidebar.project_order, ["b", "a"]);
    assert_eq!(chrome.sidebar.project_labels, labels);

    // The mirror in `chrome.prefs` is what gets written back.
    chrome.prefs.sidebar_collapsed = false;
    chrome
        .prefs
        .project_labels
        .insert("b".to_string(), "Beta".to_string());
    save_prefs(home.path(), &chrome.prefs).expect("save prefs");
    assert_eq!(load_prefs(home.path()).expect("reload prefs"), chrome.prefs);
    let mut files: Vec<String> = fs::read_dir(&client_dir)
        .expect("list client dir")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .collect();
    files.sort();
    assert_eq!(
        files,
        ["prefs.toml"],
        "the client dir holds only prefs.toml"
    );

    let mut offenders = Vec::new();
    let mut stack = vec![PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src")];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).expect("walk client source") {
            let path = entry.expect("source entry").path();
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                continue;
            }
            let text = fs::read_to_string(&path).expect("read client source");
            for needle in [
                "session.json",
                "load_session",
                "save_session",
                "load_snapshot",
                "save_snapshot",
                "persist::",
            ] {
                if text.contains(needle) {
                    offenders.push(format!("{}: {needle}", path.display()));
                }
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "client source still keeps snapshot or session files:\n{}",
        offenders.join("\n")
    );
}

/// 4.3.4: a gclient started inside a gclient pane shifts its prefix the way
/// a nested tmux client does and opens no terminal of its own; outside a
/// pane the first run still seeds one shell.
#[tokio::test]
async fn nested_pane_launch_shifts_prefix_and_opens_nothing() {
    let home = tempfile::tempdir().expect("temp gobby home");
    let cwd = tempfile::tempdir().expect("temp current dir");
    let token_file = home.path().join("local_cli_token");
    fs::write(&token_file, "token\n").expect("write token");
    let args = parse_args(["gclient"]).expect("parse args");
    let probe = |in_pane: bool| {
        resolve_probe_env_at(&args, "http://unused", &token_file, false, in_pane)
            .expect("probe env resolves")
    };
    let outside = probe(false);
    assert!(!outside.in_pane);
    assert!(
        !outside.nested(),
        "outside tmux and any pane the prefix stays"
    );
    let inside = probe(true);
    assert!(inside.in_pane);
    assert!(!inside.nested_tmux);
    assert!(inside.nested(), "a pane launch shifts the prefix");
    let ready = prepare_at(&args, inside, &HealthyHost, cwd.path(), home.path())
        .expect("prepare inside a pane");
    assert!(ready.nested);
    assert!(ready.in_pane);
    assert_eq!(
        ready.keymap.active_chords(),
        Keymap::defaults(default_prefix(true)).active_chords()
    );

    // Inside a pane the loop attaches and opens nothing; outside it seeds
    // the first-run shell (the control).
    for (in_pane, creates) in [(true, 0), (false, 1)] {
        let mock = MockDaemon::start("local-token").await;
        mock.enqueue("GET", "/api/projects", 200, project_rows());
        mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&[]));
        mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&[SPAWNED]));
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect live daemon");
        let mut workspace = Workspace::live(daemon);
        workspace.set_gobby_home(home.path().to_path_buf());
        workspace.set_in_pane(in_pane);
        workspace.select_project("project-1");
        let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
        let mut chrome = Chrome::dark();
        let (input_tx, input_rx) = mpsc::channel(8);
        let driver = async {
            wait_for_websocket_requests(&mock, "workspace_attach", 1).await;
            if creates > 0 {
                wait_for_websocket_requests(&mock, "terminal_create", creates).await;
                wait_for_http_requests(&mock, "GET", "/api/terminals?", 2).await;
            } else {
                wait_for_http_requests(&mock, "GET", "/api/terminals?", 1).await;
            }
            settle_live_event().await;
            drop(input_tx);
        };
        let mut switch = TerminalGuard::recording().0;
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
            driver
        );
        result.expect("live loop");
        assert_eq!(
            websocket_requests(&mock, "terminal_create").len(),
            creates,
            "in_pane={in_pane}"
        );
        assert_eq!(chrome.tabs().tabs.len(), creates, "in_pane={in_pane}");
        mock.shutdown().await;
    }
}

const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

fn project_rows() -> Value {
    json!([{
        "id": "project-1",
        "name": "gobby",
        "display_name": "gobby",
        "checkout": {"machine_id": "m-local", "root_path": "/repo"},
        "session_count": 1,
        "last_activity_at": null,
    }])
}

fn terminal_page(ids: &[&str]) -> Value {
    json!({
        "items": ids
            .iter()
            .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
            .collect::<Vec<_>>(),
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
    })
}

fn websocket_requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!(kind)))
        .collect()
}

async fn wait_for_websocket_requests(mock: &MockDaemon, kind: &str, expected: usize) {
    timeout(Duration::from_secs(1), async {
        while websocket_requests(mock, kind).len() < expected {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {expected} {kind} requests"));
}

async fn wait_for_http_requests(mock: &MockDaemon, method: &str, path: &str, expected: usize) {
    timeout(Duration::from_secs(1), async {
        loop {
            let count = mock
                .requests()
                .into_iter()
                .filter(|request| request.method == method && request.target.starts_with(path))
                .count();
            if count >= expected {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {expected} {method} {path} requests"));
}

async fn settle_live_event() {
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
}

#[test]
fn prefs_round_trip_and_reject_unknown_keys() {
    let dir = tempfile::tempdir().expect("temp dir");
    let home = dir.path().join("home");

    let defaults = load_prefs(&home).expect("missing file yields defaults");
    assert_eq!(defaults, ClientPrefs::default());
    assert!(defaults.mouse_capture);

    let prefs = ClientPrefs {
        theme: "light".to_string(),
        keybinds: "/tmp/keys.toml".to_string(),
        mouse_capture: false,
        pane_gaps: false,
        sidebar_width: 32,
        right_click_passthrough_modifier: PassthroughModifier::Alt,
        ..ClientPrefs::default()
    };
    let path = save_prefs(&home, &prefs).expect("save");
    assert_eq!(path, prefs_path(&home));
    assert_eq!(path, home.join("client").join("prefs.toml"));
    let text = fs::read_to_string(&path).expect("read prefs");
    assert!(text.starts_with("[ui]\n"), "{text}");
    assert!(text.contains("mouse_capture = false\n"), "{text}");
    assert!(
        text.contains("right_click_passthrough_modifier = \"alt\"\n"),
        "{text}"
    );
    assert!(
        text.contains("[keymap]\npath = \"/tmp/keys.toml\"\n"),
        "{text}"
    );
    assert!(!text.contains("layout"), "{text}");
    assert_eq!(load_prefs(&home).expect("reload"), prefs);
    let leftovers = fs::read_dir(path.parent().expect("client dir"))
        .expect("list client dir")
        .filter_map(Result::ok)
        .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
        .count();
    assert_eq!(leftovers, 0);

    fs::write(&path, "[ui]\nsidebar_width = 40\n").expect("write partial prefs");
    let partial = load_prefs(&home).expect("every key is optional");
    assert_eq!(
        partial,
        ClientPrefs {
            sidebar_width: 40,
            ..ClientPrefs::default()
        }
    );

    fs::write(&path, "[ui]\nmouse_captre = false\n").expect("write typo prefs");
    let error = load_prefs(&home).expect_err("unknown key must fail");
    assert!(matches!(error, PrefsError::Parse(_)), "{error:?}");
    let message = error.to_string();
    assert!(message.contains("mouse_captre"), "{message}");
    assert!(message.contains("line 2"), "{message}");

    fs::write(&path, "[keymapp]\npath = \"\"\n").expect("write typo table");
    let message = load_prefs(&home)
        .expect_err("unknown table must fail")
        .to_string();
    assert!(message.contains("keymapp"), "{message}");
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

/// The binary in a session of its own, with no controlling terminal: a
/// launch that reaches raw mode fails there instead of taking over the
/// terminal running the tests.
fn detached_gclient() -> Command {
    use std::os::unix::process::CommandExt;

    let mut command = Command::new(env!("CARGO_BIN_EXE_gclient"));
    // SAFETY: setsid is async-signal-safe and touches nothing the parent
    // shares with the child.
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    command
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
        in_pane: false,
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
fn unreachable_daemon_launches_with_a_notice_and_waits() {
    let url = closed_port_url();
    let args = parse_args(["gclient"]).expect("parse");
    let (backend, enters) = CountingBackend::new();
    let (ready, guard) = start_session(args, env_at(&url), &HttpHealthClient::new(), backend)
        .expect("a stopped daemon is a wait, not a launch failure");
    assert_eq!(
        enters.load(Ordering::SeqCst),
        1,
        "the window opens and waits for the daemon"
    );
    assert!(ready.host.is_none(), "no host state without a daemon");
    let notice = ready
        .host_notice
        .expect("the probe's report becomes the notice");
    assert!(
        notice.contains(&format!("daemon unreachable at {url}")),
        "notice names the probed URL: {notice}"
    );
    assert!(
        notice.contains("gobby start"),
        "actionable recovery missing `gobby start`: {notice}"
    );
    assert!(
        notice.contains("gobby status"),
        "actionable recovery missing `gobby status`: {notice}"
    );
    drop(guard);
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
        let env = resolve_probe_env_at(&args, fallback, &token_file, false, false)
            .expect("resolve host env");
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
            let env = resolve_probe_env_at(&args, fallback, &token_file, false, false)
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
        views_source.contains("chrome.notify(Toast::info(notice));"),
        "host notice must reach the alert log"
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
        views_source.contains("initial_project(ready.project, focused.as_deref())"),
        "run_ready must open on the initial project"
    );
    assert_eq!(
        initial_project(Some("cwd-project".to_string()), Some("saved-project")),
        "cwd-project"
    );
    assert_eq!(
        initial_project(None, Some("saved-project")),
        "saved-project"
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
