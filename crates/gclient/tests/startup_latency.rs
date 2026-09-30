mod mock_daemon;

use std::io::{self, Write};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine as _;
use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::run_live_loop;
use gobby_client::app::startup_stages::{StageState, StartupStage, StartupStages};
use gobby_client::daemon::LiveDaemon;
use gobby_client::frame_source::FrameDelivery;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::{Backend, ClearType, TestBackend, WindowSize};
use ratatui::buffer::Cell;
use ratatui::layout::{Position, Size};
use ratatui::Terminal;
use tokio::sync::mpsc;
use tokio::time::timeout;

struct DrawCounter {
    inner: TestBackend,
    count: Arc<AtomicUsize>,
    /// The text of every frame drawn, one row per line.
    frames: Arc<Mutex<Vec<String>>>,
}

struct LogWriter(Arc<Mutex<Vec<u8>>>);

impl Write for LogWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0
            .lock()
            .expect("captured logs")
            .extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl DrawCounter {
    fn new(width: u16, height: u16) -> (Self, Arc<AtomicUsize>) {
        let count = Arc::new(AtomicUsize::new(0));
        (
            Self {
                inner: TestBackend::new(width, height),
                count: Arc::clone(&count),
                frames: Arc::default(),
            },
            count,
        )
    }

    fn frames(&self) -> Arc<Mutex<Vec<String>>> {
        Arc::clone(&self.frames)
    }
}

impl Backend for DrawCounter {
    type Error = <TestBackend as Backend>::Error;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a Cell)>,
    {
        self.inner.draw(content)?;
        self.count.fetch_add(1, Ordering::SeqCst);
        let buffer = self.inner.buffer();
        let width = usize::from(buffer.area.width);
        let text = buffer
            .content()
            .chunks(width)
            .map(|row| row.iter().map(Cell::symbol).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n");
        self.frames.lock().expect("recorded frames").push(text);
        Ok(())
    }

    fn hide_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.hide_cursor()
    }

    fn show_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.show_cursor()
    }

    fn get_cursor_position(&mut self) -> Result<Position, Self::Error> {
        self.inner.get_cursor_position()
    }

    fn set_cursor_position<P: Into<Position>>(&mut self, position: P) -> Result<(), Self::Error> {
        self.inner.set_cursor_position(position)
    }

    fn clear(&mut self) -> Result<(), Self::Error> {
        self.inner.clear()
    }

    fn clear_region(&mut self, clear_type: ClearType) -> Result<(), Self::Error> {
        self.inner.clear_region(clear_type)
    }

    fn size(&self) -> Result<Size, Self::Error> {
        self.inner.size()
    }

    fn window_size(&mut self) -> Result<WindowSize, Self::Error> {
        self.inner.window_size()
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        self.inner.flush()
    }
}

async fn exercise_first_frame() -> usize {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/admin/config",
        200,
        serde_json::json!({"status": "success", "config": {"server": {"version": "0.5.0"}}}),
    );
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let hold = mock.hold_attach();
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    let (backend, draws) = DrawCounter::new(120, 40);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            loop {
                let saw_attach = mock.requests().iter().any(|request| {
                    request.method == "WS"
                        && request.body.as_ref().and_then(|body| body.get("type"))
                            == Some(&serde_json::json!("workspace_attach"))
                });
                if saw_attach {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("workspace attach request");
        let first_frame_draws = draws.load(Ordering::SeqCst);
        assert!(
            first_frame_draws > 0,
            "the first frame must be drawn before the attach reply"
        );

        timeout(Duration::from_secs(5), async {
            for kind in [
                MouseEventKind::Down(MouseButton::Left),
                MouseEventKind::Up(MouseButton::Left),
                MouseEventKind::Moved,
            ] {
                input_tx
                    .send(RawInputEvent::Mouse(MouseEvent {
                        kind,
                        column: 2,
                        row: 0,
                        modifiers: KeyModifiers::NONE,
                    }))
                    .await
                    .expect("splash input");
            }
        })
        .await
        .expect("input is handled while the attach waits");
        hold.notify_one();
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET" && request.target.starts_with("/api/projects")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("sidebar fan-out after workspace attach");
        drop(input_tx);
        first_frame_draws
    };
    let first_frame_draws = timeout(Duration::from_secs(10), async {
        let (result, first_frame_draws) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
        first_frame_draws
    })
    .await
    .expect("startup stays responsive");
    assert!(
        chrome.menu.is_none(),
        "the splash draws no menu bar, so a click there opens nothing"
    );
    assert_eq!(chrome.connection.daemon_version.as_deref(), Some("0.5.0"));
    assert!(
        chrome
            .connection
            .stages
            .as_ref()
            .is_some_and(StartupStages::finished),
        "startup reaches a finished first-frame stage"
    );
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|request| {
                request.method == "WS"
                    && request.body.as_ref().and_then(|body| body.get("type"))
                        == Some(&serde_json::json!("workspace_attach"))
            })
            .count(),
        1,
        "startup must attach the workspace once"
    );
    mock.shutdown().await;
    first_frame_draws
}

#[tokio::test]
async fn the_first_frame_is_drawn_before_the_daemon_answers() {
    assert!(exercise_first_frame().await > 0, "the startup drew a frame");
}

#[tokio::test]
async fn non_retryable_startup_error_exits_after_first_frame() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/admin/config",
        401,
        serde_json::json!({"detail": "invalid token"}),
    );
    let daemon = LiveDaemon::unconnected(mock.url(), "local-token").expect("daemon handle");
    let mut workspace = Workspace::live(daemon);
    let (backend, draws) = DrawCounter::new(120, 40);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (_input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let result = timeout(
        Duration::from_secs(5),
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch,
        ),
    )
    .await
    .expect("a permanent startup error must exit instead of retrying");
    assert!(draws.load(Ordering::SeqCst) > 0, "draw before error");
    assert!(
        result
            .expect_err("authorization failure must end launch")
            .to_string()
            .contains("authorization"),
        "the launch error should explain the failed authorization"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn missing_daemon_version_keeps_the_splash_placeholder() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/admin/config",
        200,
        serde_json::json!({"status": "success", "config": {"server": {}}}),
    );
    let daemon = LiveDaemon::unconnected(mock.url(), "local-token").expect("daemon handle");
    let version = daemon.config_version().await;
    assert_eq!(
        version.expect("config request succeeds"),
        None,
        "an absent version should keep the splash placeholder"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn reconnect_attach_wait_keeps_menus_responsive() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/admin/config",
        200,
        serde_json::json!({"status": "success", "config": {"server": {"version": "0.5.0"}}}),
    );
    let daemon = LiveDaemon::unconnected(mock.url(), "local-token").expect("daemon handle");
    let mut workspace = Workspace::live(daemon);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET" && request.target.starts_with("/api/projects")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("initial sidebar fetch");
        let hold = mock.hold_attach();
        mock.drop_websockets();
        timeout(Duration::from_secs(5), async {
            loop {
                let attaches = mock
                    .requests()
                    .iter()
                    .filter(|request| {
                        request.method == "WS"
                            && request.body.as_ref().and_then(|body| body.get("type"))
                                == Some(&serde_json::json!("workspace_attach"))
                    })
                    .count();
                if attaches >= 2 {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("reconnect workspace attach");

        let menu_input = timeout(Duration::from_secs(5), async {
            for kind in [
                MouseEventKind::Down(MouseButton::Left),
                MouseEventKind::Up(MouseButton::Left),
                MouseEventKind::Moved,
            ] {
                input_tx
                    .send(RawInputEvent::Mouse(MouseEvent {
                        kind,
                        column: 2,
                        row: 0,
                        modifiers: KeyModifiers::NONE,
                    }))
                    .await
                    .expect("menu bar input");
            }
        })
        .await;
        hold.notify_one();
        drop(input_tx);
        menu_input
    };
    let (result, menu_input) = timeout(Duration::from_secs(15), async {
        tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        )
    })
    .await
    .expect("reconnect completes");
    menu_input.expect("menu input must be handled while reconnect attach waits");
    result.expect("reconnect loop");
    assert!(chrome.menu.is_some(), "the reconnect menu opens");
    mock.shutdown().await;
}

#[tokio::test]
async fn stage_timings_are_logged_once_per_launch() {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let writer = Arc::clone(&captured);
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .without_time()
        .with_target(false)
        .with_writer(move || LogWriter(Arc::clone(&writer)))
        .finish();
    let guard = tracing::subscriber::set_default(subscriber);
    let _ = exercise_first_frame().await;
    drop(guard);

    let bytes = captured.lock().expect("captured logs").clone();
    let logs = String::from_utf8(bytes).expect("UTF-8 logs");
    for stage in ["daemon health", "workspace attach", "roster", "first frame"] {
        let matching: Vec<_> = logs
            .lines()
            .filter(|line| {
                line.contains("gclient startup stage complete")
                    && line.contains(&format!("stage=\"{stage}\""))
            })
            .collect();
        assert_eq!(matching.len(), 1, "one timing for {stage}: {logs}");
        assert!(matching[0].contains("took_ms="), "stage has elapsed time");
    }
    assert_eq!(
        logs.lines()
            .filter(|line| line.contains("gclient startup complete"))
            .count(),
        1,
        "one summary per launch: {logs}"
    );
}

#[tokio::test]
async fn first_frame_stage_waits_for_the_focused_pane() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    mock.enqueue(
        "GET",
        "/api/terminals/terminal-a",
        200,
        serde_json::json!({"terminal_id": "terminal-a", "backend": "native", "state": "live"}),
    );
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace.set_frame_delivery(FrameDelivery::Proxy);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "WS"
                        && request.body.as_ref().and_then(|body| body.get("type"))
                            == Some(&serde_json::json!("terminal_attach"))
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("focused pane attach request");
        let sidebar_started = timeout(Duration::from_millis(300), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET" && request.target.starts_with("/api/projects")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await;
        assert!(
            sidebar_started.is_err(),
            "sidebar fetch must wait for the focused pane's first frame"
        );
        drop(input_tx);
    };
    timeout(Duration::from_secs(10), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("first-frame wait stays responsive");
    assert!(matches!(
        chrome
            .connection
            .stages
            .as_ref()
            .expect("startup stages")
            .state(StartupStage::FirstFrame),
        StageState::Running { .. }
    ));
    mock.shutdown().await;
}

/// The workspace is projected on attach, before the roster opens any pane, so
/// the focused tab's terminals are unresolved then. Opening them must
/// re-project the tab without a generation bump or a tab switch, so the first
/// frame after the splash already shows both of its panes (#22972).
#[tokio::test]
async fn focused_tab_panes_are_drawn_after_startup_opens_them() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace(
        "project-1",
        &[
            (&["terminal-a", "terminal-b"], "terminal-a"),
            (&["terminal-c"], "terminal-c"),
        ],
    );
    let row = |terminal_id: &str| serde_json::json!({"terminal_id": terminal_id, "backend": "native", "state": "live"});
    // The roster's first row fetch waits, so the loop projects the attached
    // workspace while none of its terminals has a pane yet.
    let roster_hold = mock.enqueue_held("GET", "/api/terminals/terminal-a", 200, row("terminal-a"));
    for terminal_id in ["terminal-b", "terminal-c"] {
        mock.enqueue(
            "GET",
            &format!("/api/terminals/{terminal_id}"),
            200,
            row(terminal_id),
        );
    }
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace.set_frame_delivery(FrameDelivery::Proxy);
    let (backend, _) = DrawCounter::new(120, 40);
    let frames = backend.frames();
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            while !mock.requests().iter().any(|request| {
                request.method == "GET" && request.target.starts_with("/api/terminals/terminal-a")
            }) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("roster fetches the focused tab's rows");
        // Any daemon event while the roster waits re-syncs the chrome.
        mock.send_event_and_wait(serde_json::json!({
            "type": "attention_event",
            "daemon_epoch": "epoch-1",
            "seq": 1
        }))
        .await;
        roster_hold.notify_one();
        // A pane claims its viewport with the attachment id the attach reply
        // carried, so both claims mean both attaches were answered.
        let attachment_of = |terminal_id: &str| {
            mock.requests().iter().find_map(|request| {
                let body = request.body.as_ref()?;
                (request.method == "WS"
                    && body.get("type") == Some(&serde_json::json!("terminal_set_viewport"))
                    && body.get("terminal_id") == Some(&serde_json::json!(terminal_id)))
                .then(|| body.get("attachment_id")?.as_str().map(str::to_string))
                .flatten()
            })
        };
        let attachment_id = timeout(Duration::from_secs(5), async {
            loop {
                if let (Some(attachment_id), Some(_)) =
                    (attachment_of("terminal-a"), attachment_of("terminal-b"))
                {
                    break attachment_id;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("focused tab panes attach");
        let frame = include_bytes!("../../gterminal/tests/fixtures/wire_golden/frame.bin");
        mock.send_event_and_wait(serde_json::json!({
            "type": "terminal_frame",
            "terminal_id": "terminal-a",
            "attachment_id": attachment_id,
            "encoding": "bincode-b64",
            "payload": base64::engine::general_purpose::STANDARD.encode(&frame[4..]),
        }))
        .await;
        // The sidebar fan-out starts only once the first-frame stage is done.
        timeout(Duration::from_secs(5), async {
            while !mock.requests().iter().any(|request| {
                request.method == "GET" && request.target.starts_with("/api/projects")
            }) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the focused pane's first frame ends the splash");
        drop(input_tx);
    };
    timeout(Duration::from_secs(10), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("startup stays responsive");

    assert!(matches!(
        chrome
            .connection
            .stages
            .as_ref()
            .expect("startup stages")
            .state(StartupStage::FirstFrame),
        StageState::Done { .. }
    ));
    let frames = std::mem::take(&mut *frames.lock().expect("recorded frames"));
    // The splash draws no menu bar; the chrome's first row does.
    let first = frames
        .iter()
        .find(|frame| frame.lines().next().is_some_and(|row| row.contains("File")))
        .expect("a frame after the splash");
    // Each drawn pane's border carries its `hub:workspace:tab:pane` address.
    for address in ["1:1:1:1", "1:1:1:2"] {
        assert!(
            first.contains(address),
            "the first frame after the splash draws pane {address}:\n{first}"
        );
    }
    assert!(
        !first.contains("No pane open."),
        "the first frame after the splash draws the focused tab's panes:\n{first}"
    );
    mock.shutdown().await;
}

/// Run startup with the focused pane's attach refused with `code`, and return
/// the first-frame stage once the sidebar fetch has started. A pane whose
/// attach fails draws its reason instead of a frame, and startup must treat
/// that as settled, or the splash never ends.
async fn first_frame_after_a_failed_focused_attach(code: &str, reason: &str) -> StageState {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    mock.enqueue(
        "GET",
        "/api/terminals/terminal-a",
        200,
        serde_json::json!({"terminal_id": "terminal-a", "backend": "native", "state": "live"}),
    );
    mock.refuse_next_proxy_attach(code, reason);
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace.set_frame_delivery(FrameDelivery::Proxy);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET" && request.target.starts_with("/api/projects")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the sidebar fetch starts once the refused pane settles");
        drop(input_tx);
    };
    timeout(Duration::from_secs(10), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("a refused attach does not hang startup");
    mock.shutdown().await;
    chrome
        .connection
        .stages
        .as_ref()
        .expect("startup stages")
        .state(StartupStage::FirstFrame)
}

#[tokio::test]
async fn first_frame_settles_when_the_focused_attach_is_refused() {
    let stage = first_frame_after_a_failed_focused_attach("observer_limit", "too many observers");
    assert!(matches!(stage.await, StageState::Done { .. }));
}

#[tokio::test]
async fn first_frame_settles_while_the_focused_attach_waits_to_retry() {
    let stage = first_frame_after_a_failed_focused_attach("host_not_ready", "host not ready");
    assert!(matches!(stage.await, StageState::Done { .. }));
}

#[tokio::test]
async fn the_sidebar_fan_out_runs_after_the_first_frame() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    mock.enqueue(
        "GET",
        "/api/terminals/terminal-a",
        200,
        serde_json::json!({"terminal_id": "terminal-a", "backend": "native", "state": "live"}),
    );
    let hold = mock.enqueue_held("GET", "/api/projects", 200, serde_json::json!([]));
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace.set_frame_delivery(FrameDelivery::Proxy);
    let (backend, draws) = DrawCounter::new(120, 40);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        let attachment_id = timeout(Duration::from_secs(5), async {
            loop {
                if let Some(attachment_id) = mock.requests().iter().find_map(|request| {
                    let body = request.body.as_ref()?;
                    (request.method == "WS"
                        && body.get("type") == Some(&serde_json::json!("terminal_set_viewport"))
                        && body.get("terminal_id") == Some(&serde_json::json!("terminal-a")))
                    .then(|| body.get("attachment_id")?.as_str().map(str::to_string))
                    .flatten()
                }) {
                    break attachment_id;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("focused pane attachment");
        let frame = include_bytes!("../../gterminal/tests/fixtures/wire_golden/frame.bin");
        mock.send_event_and_wait(serde_json::json!({
            "type": "terminal_frame",
            "terminal_id": "terminal-a",
            "attachment_id": attachment_id,
            "encoding": "bincode-b64",
            "payload": base64::engine::general_purpose::STANDARD.encode(&frame[4..]),
        }))
        .await;
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET" && request.target.starts_with("/api/projects")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("sidebar project request");
        assert!(
            draws.load(Ordering::SeqCst) > 0,
            "first frame preceded sidebar fetch"
        );
        timeout(Duration::from_millis(300), async {
            for kind in [
                MouseEventKind::Down(MouseButton::Left),
                MouseEventKind::Up(MouseButton::Left),
            ] {
                input_tx
                    .send(RawInputEvent::Mouse(MouseEvent {
                        kind,
                        column: 2,
                        row: 0,
                        modifiers: KeyModifiers::NONE,
                    }))
                    .await
                    .expect("menu bar input");
            }
        })
        .await
        .expect("the sidebar fan-out must not hold menu input");
        hold.notify_one();
        drop(input_tx);
    };
    timeout(Duration::from_secs(10), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("sidebar fetch stays responsive");
    assert!(chrome.menu.is_some(), "the first-frame menu can open");
    mock.shutdown().await;
}

#[tokio::test]
async fn pane_attach_wait_keeps_input_responsive_under_the_splash() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    mock.enqueue(
        "GET",
        "/api/terminals/terminal-a",
        200,
        serde_json::json!({"terminal_id": "terminal-a", "backend": "native", "state": "live"}),
    );
    mock.suppress_ws("terminal_attach");
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace.set_frame_delivery(FrameDelivery::Proxy);
    let (backend, draws) = DrawCounter::new(120, 40);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "WS"
                        && request.body.as_ref().and_then(|body| body.get("type"))
                            == Some(&serde_json::json!("terminal_attach"))
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("terminal attach request: {:?}", mock.activity()));
        assert!(
            draws.load(Ordering::SeqCst) > 0,
            "a frame precedes pane attach"
        );
        timeout(Duration::from_millis(300), async {
            for kind in [
                MouseEventKind::Down(MouseButton::Left),
                MouseEventKind::Up(MouseButton::Left),
            ] {
                input_tx
                    .send(RawInputEvent::Mouse(MouseEvent {
                        kind,
                        column: 2,
                        row: 0,
                        modifiers: KeyModifiers::NONE,
                    }))
                    .await
                    .expect("splash input");
            }
        })
        .await
        .unwrap_or_else(|_| panic!("input blocked during pane attach: {:?}", mock.activity()));
        mock.allow_ws("terminal_attach");
        drop(input_tx);
    };
    timeout(Duration::from_secs(10), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("the pane attach wait stays responsive");
    assert!(
        chrome
            .connection
            .stages
            .as_ref()
            .is_some_and(|stages| !stages.finished()),
        "the pane attach never answered, so the splash still stands"
    );
    assert!(
        chrome.menu.is_none(),
        "the splash draws no menu bar, so a click there opens nothing"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn empty_first_run_keeps_menus_responsive() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[]);
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let (backend, draws) = DrawCounter::new(120, 40);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.connection.stages = Some(StartupStages::begin(Instant::now()));
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;

    let drive = async {
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET" && request.target.starts_with("/api/projects")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("sidebar project request");
        assert!(draws.load(Ordering::SeqCst) > 0, "first frame drawn");
        timeout(Duration::from_millis(300), async {
            for kind in [
                MouseEventKind::Down(MouseButton::Left),
                MouseEventKind::Up(MouseButton::Left),
            ] {
                input_tx
                    .send(RawInputEvent::Mouse(MouseEvent {
                        kind,
                        column: 2,
                        row: 0,
                        modifiers: KeyModifiers::NONE,
                    }))
                    .await
                    .expect("menu bar input");
            }
        })
        .await
        .expect("menu input remains responsive on an empty first run");
        drop(input_tx);
    };
    timeout(Duration::from_secs(10), async {
        let (result, ()) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
    })
    .await
    .expect("empty first run stays responsive");
    assert!(chrome.menu.is_some(), "menu opens without an initial shell");
    assert!(
        mock.requests().iter().all(|request| {
            request.method != "WS"
                || request.body.as_ref().and_then(|body| body.get("type"))
                    != Some(&serde_json::json!("terminal_create"))
        }),
        "first run does not spawn a shell"
    );
    mock.shutdown().await;
}

/// The startup sidebar fetch asks for the project list once. When that list
/// fails, it must be asked for again after its backoff, or the sidebar shows
/// no projects until a project changes. `failing` names the queries that
/// answer 500 once, after startup's own roster read. Returns the project-list
/// requests seen half a second after the first one, and again once the
/// retried list has loaded its checkout rows.
async fn project_list_requests_around_a_failure(failing: &[&str]) -> (usize, usize) {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    mock.enqueue(
        "GET",
        "/api/terminals/terminal-a",
        200,
        serde_json::json!({"terminal_id": "terminal-a", "backend": "native", "state": "live"}),
    );
    // A refused attach settles the first frame without a frame to send.
    mock.refuse_next_proxy_attach("observer_limit", "too many observers");
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        serde_json::json!({"epoch": "attention-1", "seq": 0, "entries": []}),
    );
    for path in failing {
        mock.enqueue(
            "GET",
            path,
            500,
            serde_json::json!({"error": "daemon busy"}),
        );
    }
    mock.enqueue(
        "GET",
        "/api/projects",
        200,
        serde_json::json!([{
            "id": "project-1",
            "name": "gobby",
            "display_name": "gobby",
            "checkout": {"machine_id": "m-local", "root_path": "/repo"},
            "session_count": 0,
            "last_activity_at": null,
        }]),
    );
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace.set_frame_delivery(FrameDelivery::Proxy);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let mut switch = TerminalGuard::recording().0;
    let project_gets = || {
        mock.requests()
            .iter()
            .filter(|request| request.method == "GET" && request.target == "/api/projects")
            .count()
    };

    let drive = async {
        timeout(Duration::from_secs(5), async {
            while project_gets() == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the startup sidebar fetch asks for the project list");
        tokio::time::sleep(Duration::from_millis(500)).await;
        let during_backoff = project_gets();
        timeout(Duration::from_secs(5), async {
            while project_gets() < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the failed project list is asked for again");
        // Checkout rows are read only from a project list that loaded, so this
        // request shows the retry succeeded.
        timeout(Duration::from_secs(5), async {
            loop {
                if mock.requests().iter().any(|request| {
                    request.method == "GET"
                        && request.target.starts_with("/api/source-control/status?")
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the retried project list loads its checkout rows");
        let after_retry = project_gets();
        drop(input_tx);
        (during_backoff, after_retry)
    };
    let requests = timeout(Duration::from_secs(15), async {
        let (result, requests) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch,
            ),
            drive,
        );
        result.expect("input close ends the window");
        requests
    })
    .await
    .expect("the project retry keeps the loop responsive");
    mock.shutdown().await;
    requests
}

#[tokio::test]
async fn a_failed_project_list_is_fetched_again_beside_rows_that_loaded() {
    let requests = project_list_requests_around_a_failure(&["/api/projects"]).await;
    assert_eq!(requests, (1, 2), "one retry, after the backoff");
}

#[tokio::test]
async fn a_failed_project_list_is_fetched_again_after_the_whole_refetch_fails() {
    let requests = project_list_requests_around_a_failure(&[
        "/api/attention/roster",
        "/api/projects",
        "/api/sessions?",
    ])
    .await;
    assert_eq!(requests, (1, 2), "one retry, after the backoff");
}
