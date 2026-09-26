mod mock_daemon;

use std::io::{self, Write};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::run_live_loop;
use gobby_client::app::startup_stages::StartupStages;
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
            },
            count,
        )
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
                    .expect("menu bar input");
            }
        })
        .await
        .expect("menu input is handled while attach waits");
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
    assert!(chrome.menu.is_some(), "the first-frame menu can open");
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
async fn the_sidebar_fan_out_runs_after_the_first_frame() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    let hold = mock.enqueue_held("GET", "/api/projects", 200, serde_json::json!([]));
    let daemon = LiveDaemon::connect_or_wait(mock.url(), "local-token")
        .await
        .expect("connect to mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    let (backend, draws) = DrawCounter::new(120, 40);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
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
async fn first_frame_attach_wait_keeps_menus_responsive() {
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
                    .expect("menu bar input");
            }
        })
        .await
        .unwrap_or_else(|_| {
            panic!(
                "menu input blocked during pane attach: {:?}",
                mock.activity()
            )
        });
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
    .expect("first-frame attach stays responsive");
    assert!(chrome.menu.is_some(), "menu opened during pane attach");
    mock.shutdown().await;
}

#[tokio::test]
async fn first_shell_spawn_wait_keeps_menus_responsive() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[]);
    mock.suppress_ws("terminal_create");
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
                    request.method == "WS"
                        && request.body.as_ref().and_then(|body| body.get("type"))
                            == Some(&serde_json::json!("terminal_create"))
                }) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("first shell spawn request");
        assert!(
            draws.load(Ordering::SeqCst) > 0,
            "a frame precedes shell spawn"
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
        .expect("menu input remains responsive during first shell spawn");
        mock.allow_ws("terminal_create");
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
    .expect("first shell spawn stays responsive");
    assert!(
        chrome.menu.is_some(),
        "menu opened while shell spawn waited"
    );
    mock.shutdown().await;
}
