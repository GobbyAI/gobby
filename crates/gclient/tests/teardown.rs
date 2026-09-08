//! 3.3.12 RAII terminal restore.

mod mock_daemon;

use mock_daemon::MockDaemon;

use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::{
    Answer, Daemon, DaemonError, EventReceiver, Generation, KillOutcome, LiveDaemon, Page,
    ProjectRow, RosterEntry, RunRow, ScriptedDaemon, SessionRow, SourceStatus, SpawnOutcome,
    SpawnRequest, SubscribeSnapshot, TerminalRow, WorktreeRow, WsMessage, WsReply,
};
use gobby_client::persist::{save_snapshot, LayoutNode, TabSnapshot, WorkspaceSnapshot};
use gobby_client::teardown::{
    shutdown, ModeBackend, RecordingBackend, ShutdownWorkspace, TerminalGuard,
};
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::raw_input::RawInputEvent;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
use std::future::pending;
use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;
use tokio::time::{timeout, Duration, Instant};
use tracing::{Event, Subscriber};
use tracing_subscriber::layer::Context as LayerContext;
use tracing_subscriber::prelude::*;
use tracing_subscriber::Layer;

/// Save a one-pane snapshot for `project` and point the workspace at it, so
/// the loop shows `terminal_id` once the roster arrives. Keep the returned
/// home alive for the loop's lifetime.
fn pin_tab(
    workspace: &mut Workspace<LiveDaemon>,
    project: &str,
    terminal_id: &str,
) -> tempfile::TempDir {
    let home = tempfile::tempdir().expect("gobby home");
    let snapshot = WorkspaceSnapshot {
        project_id: project.to_string(),
        tabs: vec![TabSnapshot {
            title: terminal_id.to_string(),
            layout: LayoutNode::Pane {
                terminal_id: terminal_id.to_string(),
            },
            focused: Some(terminal_id.to_string()),
            worktree_id: None,
        }],
        active_tab: 0,
        focused_terminal_id: Some(terminal_id.to_string()),
    };
    save_snapshot(home.path(), &snapshot).expect("save the pinned snapshot");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace
        .restore_project(project)
        .expect("restore the pinned snapshot");
    home
}

#[test]
fn guard_restores_on_quit_failure_panic_and_signal() {
    let (guard, hits) = TerminalGuard::recording();
    drop(guard);
    assert_eq!(hits.load(Ordering::SeqCst), 1);

    let (mut guard, hits) = TerminalGuard::recording();
    guard.inject_startup_failure();
    drop(guard);
    assert_eq!(
        hits.load(Ordering::SeqCst),
        1,
        "startup failure still restores"
    );

    let hits = {
        let (guard, hits) = TerminalGuard::recording();
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _guard = guard;
            panic!("injected");
        }));
        hits
    };
    assert_eq!(hits.load(Ordering::SeqCst), 1, "panic restores");

    let (guard, hits) = TerminalGuard::recording();
    guard.handle_signal();
    assert_eq!(hits.load(Ordering::SeqCst), 1);
    drop(guard);
    assert_eq!(hits.load(Ordering::SeqCst), 1, "second drop is a no-op");

    let backend = RecordingBackend::default();
    let hits = backend.hits();
    let mut guard = TerminalGuard::new(backend);
    let _ = guard.arm(false);
    guard.disarm_for_test();
    drop(guard);
    assert_eq!(hits.load(Ordering::SeqCst), 0);
}

struct RetryBackend {
    attempts: Arc<AtomicUsize>,
}

impl gobby_client::teardown::ModeBackend for RetryBackend {
    fn enter(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        let attempt = self.attempts.fetch_add(1, Ordering::SeqCst);
        if attempt == 0 {
            Err(io::Error::other("injected restore failure"))
        } else {
            Ok(())
        }
    }
}

#[test]
fn legacy_backend_restore_failure_is_retried() {
    let attempts = Arc::new(AtomicUsize::new(0));
    let mut guard = TerminalGuard::new(RetryBackend {
        attempts: Arc::clone(&attempts),
    });
    guard.arm(false).expect("arm guard");

    guard.handle_signal();
    assert_eq!(attempts.load(Ordering::SeqCst), 1);

    drop(guard);
    assert_eq!(
        attempts.load(Ordering::SeqCst),
        2,
        "Drop must retry a failed outstanding restoration"
    );
}

#[derive(Default)]
struct StageBackend {
    events: Arc<Mutex<Vec<&'static str>>>,
    fail_entry: Option<&'static str>,
    fail_restore: Option<&'static str>,
    failed_restore: bool,
    panic_restore: Option<&'static str>,
}

impl StageBackend {
    fn entry(&mut self, stage: &'static str) -> io::Result<()> {
        self.events.lock().unwrap().push(stage);
        if self.fail_entry == Some(stage) {
            Err(io::Error::other(format!("injected {stage} failure")))
        } else {
            Ok(())
        }
    }

    fn restoration(&mut self, stage: &'static str) -> io::Result<()> {
        self.events.lock().unwrap().push(stage);
        assert_ne!(self.panic_restore, Some(stage), "injected {stage} panic");
        if self.fail_restore == Some(stage) && !self.failed_restore {
            self.failed_restore = true;
            Err(io::Error::other(format!("injected {stage} failure")))
        } else {
            Ok(())
        }
    }
}

impl ModeBackend for StageBackend {
    fn enter(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn enable_raw_mode(&mut self) -> io::Result<()> {
        self.entry("raw+")
    }

    fn enter_alternate_screen(&mut self) -> io::Result<()> {
        self.entry("alt+")
    }

    fn enable_bracketed_paste(&mut self) -> io::Result<()> {
        self.entry("bracket+")
    }

    fn hide_cursor(&mut self) -> io::Result<()> {
        self.entry("cursor+")
    }

    fn show_cursor(&mut self) -> io::Result<()> {
        self.restoration("cursor-")
    }

    fn disable_bracketed_paste(&mut self) -> io::Result<()> {
        self.restoration("bracket-")
    }

    fn leave_alternate_screen(&mut self) -> io::Result<()> {
        self.restoration("alt-")
    }

    fn disable_raw_mode(&mut self) -> io::Result<()> {
        self.restoration("raw-")
    }

    fn enable_mouse_capture(&mut self) -> io::Result<()> {
        self.entry("mouse+")
    }

    fn disable_mouse_capture(&mut self) -> io::Result<()> {
        self.restoration("mouse-")
    }
}

#[test]
fn partial_arming_rolls_back_completed_stages() {
    let cases = [
        ("alt+", vec!["raw+", "alt+", "raw-"]),
        ("bracket+", vec!["raw+", "alt+", "bracket+", "alt-", "raw-"]),
        (
            "cursor+",
            vec![
                "raw+", "alt+", "bracket+", "cursor+", "bracket-", "alt-", "raw-",
            ],
        ),
    ];

    for (failure, expected) in cases {
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut guard = TerminalGuard::new(StageBackend {
            events: Arc::clone(&events),
            fail_entry: Some(failure),
            ..StageBackend::default()
        });
        assert!(guard.arm(false).is_err());
        drop(guard);
        assert_eq!(*events.lock().unwrap(), expected, "failure at {failure}");
    }

    let events = Arc::new(Mutex::new(Vec::new()));
    let mut guard = TerminalGuard::new(StageBackend {
        events: Arc::clone(&events),
        ..StageBackend::default()
    });
    guard.arm(false).unwrap();
    guard.restore().unwrap();
    drop(guard);
    assert_eq!(
        *events.lock().unwrap(),
        vec!["raw+", "alt+", "bracket+", "cursor+", "cursor-", "bracket-", "alt-", "raw-",]
    );
}

#[test]
fn mouse_capture_follows_the_pref_and_restores_first() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut guard = TerminalGuard::new(StageBackend {
        events: Arc::clone(&events),
        ..StageBackend::default()
    });
    guard.arm(true).unwrap();
    assert_eq!(
        *events.lock().unwrap(),
        vec!["raw+", "alt+", "mouse+", "bracket+", "cursor+"],
        "capture is enabled right after the alternate screen"
    );
    guard.restore().unwrap();
    drop(guard);
    assert_eq!(
        *events.lock().unwrap(),
        vec![
            "raw+", "alt+", "mouse+", "bracket+", "cursor+", "mouse-", "cursor-", "bracket-",
            "alt-", "raw-",
        ],
        "capture is disabled before the cursor is shown"
    );

    let events = Arc::new(Mutex::new(Vec::new()));
    let mut guard = TerminalGuard::new(StageBackend {
        events: Arc::clone(&events),
        ..StageBackend::default()
    });
    guard.arm(false).unwrap();
    drop(guard);
    let stages = events.lock().unwrap().clone();
    assert!(
        !stages.iter().any(|stage| stage.starts_with("mouse")),
        "mouse capture off never touches the backend: {stages:?}"
    );

    // A failure while enabling capture rolls back the alternate screen and raw mode.
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut guard = TerminalGuard::new(StageBackend {
        events: Arc::clone(&events),
        fail_entry: Some("mouse+"),
        ..StageBackend::default()
    });
    assert!(guard.arm(true).is_err());
    drop(guard);
    assert_eq!(
        *events.lock().unwrap(),
        vec!["raw+", "alt+", "mouse+", "alt-", "raw-"]
    );
}

#[test]
fn mouse_capture_toggle_is_idempotent() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut guard = TerminalGuard::new(StageBackend {
        events: Arc::clone(&events),
        ..StageBackend::default()
    });
    guard.arm(false).unwrap();
    let armed = events.lock().unwrap().len();

    guard.set_mouse_capture(false).unwrap();
    assert_eq!(events.lock().unwrap().len(), armed, "off -> off is a no-op");
    guard.set_mouse_capture(true).unwrap();
    guard.set_mouse_capture(true).unwrap();
    assert_eq!(
        events.lock().unwrap()[armed..],
        ["mouse+"],
        "on -> on is a no-op"
    );
    guard.set_mouse_capture(false).unwrap();
    guard.set_mouse_capture(false).unwrap();
    assert_eq!(events.lock().unwrap()[armed..], ["mouse+", "mouse-"]);

    guard.set_mouse_capture(true).unwrap();
    guard.restore().unwrap();
    drop(guard);
    assert_eq!(
        events.lock().unwrap()[armed..],
        ["mouse+", "mouse-", "mouse+", "mouse-", "cursor-", "bracket-", "alt-", "raw-"],
        "restore disables capture exactly once, before show_cursor"
    );
}

#[test]
fn restore_failures_stay_outstanding_and_never_panic() {
    for failure in ["cursor-", "bracket-", "alt-", "raw-"] {
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut guard = TerminalGuard::new(StageBackend {
            events: Arc::clone(&events),
            fail_restore: Some(failure),
            ..StageBackend::default()
        });
        guard.arm(false).unwrap();
        let error = guard.restore().expect_err("one restore stage fails");
        assert!(error.to_string().contains(failure));
        let before_drop = events.lock().unwrap().clone();
        for stage in ["cursor-", "bracket-", "alt-", "raw-"] {
            assert!(before_drop.contains(&stage), "restore skipped {stage}");
        }
        drop(guard);
        assert_eq!(
            events
                .lock()
                .unwrap()
                .iter()
                .filter(|event| **event == failure)
                .count(),
            2,
            "Drop retries only the outstanding stage"
        );
    }

    let mut guard = TerminalGuard::new(StageBackend {
        panic_restore: Some("raw-"),
        ..StageBackend::default()
    });
    guard.arm(false).unwrap();
    let unwind = catch_unwind(AssertUnwindSafe(|| {
        let _guard = guard;
        panic!("primary panic");
    }));
    assert!(unwind.is_err());
}

#[derive(Clone)]
struct TraceDaemon {
    inner: ScriptedDaemon,
    trace: Arc<Mutex<Vec<String>>>,
    stall_detach: bool,
    stall_close: Option<&'static str>,
    completed_cleanup: Arc<AtomicUsize>,
    closed: Arc<AtomicUsize>,
}

impl TraceDaemon {
    fn new(
        trace: Arc<Mutex<Vec<String>>>,
        stall_detach: bool,
        stall_close: Option<&'static str>,
    ) -> Self {
        Self {
            inner: ScriptedDaemon::new(),
            trace,
            stall_detach,
            stall_close,
            completed_cleanup: Arc::new(AtomicUsize::new(0)),
            closed: Arc::new(AtomicUsize::new(0)),
        }
    }
}

impl Daemon for TraceDaemon {
    async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        Daemon::list_terminals(&self.inner, project, cursor).await
    }

    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError> {
        Daemon::roster(&self.inner).await
    }

    async fn projects(&self) -> Result<Vec<ProjectRow>, DaemonError> {
        Daemon::projects(&self.inner).await
    }

    async fn source_status(&self, project: &str) -> Result<SourceStatus, DaemonError> {
        Daemon::source_status(&self.inner, project).await
    }

    async fn worktrees(&self, project: &str) -> Result<Vec<WorktreeRow>, DaemonError> {
        Daemon::worktrees(&self.inner, project).await
    }

    async fn sessions(&self, project: &str) -> Result<Vec<SessionRow>, DaemonError> {
        Daemon::sessions(&self.inner, project).await
    }

    async fn agent_runs(&self, project: &str) -> Result<Vec<RunRow>, DaemonError> {
        Daemon::agent_runs(&self.inner, project).await
    }

    async fn respond(
        &self,
        entry: &str,
        attention_id: &str,
        answer: &Answer,
    ) -> Result<(), DaemonError> {
        Daemon::respond(&self.inner, entry, attention_id, answer).await
    }

    async fn mark_seen(&self, entry: &str, attention_id: &str) -> Result<(), DaemonError> {
        Daemon::mark_seen(&self.inner, entry, attention_id).await
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnOutcome, DaemonError> {
        Daemon::spawn(&self.inner, request).await
    }

    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError> {
        Daemon::terminate(&self.inner, terminal_id).await
    }

    fn subscribe(&self) -> (SubscribeSnapshot, EventReceiver) {
        Daemon::subscribe(&self.inner)
    }

    async fn send(&self, message: WsMessage) -> Result<WsReply, DaemonError> {
        let kind = message["type"].as_str().unwrap_or("unknown").to_string();
        self.trace.lock().unwrap().push(kind.clone());
        if self.stall_detach && kind == "terminal_detach" {
            pending::<()>().await;
        }
        if self.completed_cleanup.fetch_add(1, Ordering::SeqCst) == 1 {
            self.trace
                .lock()
                .unwrap()
                .push("cleanup-awaited".to_string());
        }
        Ok(json!({"ok": true}))
    }

    async fn notify(&self, message: WsMessage) -> Result<(), DaemonError> {
        self.send(message).await.map(|_| ())
    }

    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError> {
        Daemon::reconnect(&self.inner, observed).await
    }

    async fn close(&self, _deadline: Instant) -> Result<(), DaemonError> {
        let event = self
            .stall_close
            .map_or_else(|| "close".to_string(), |stage| format!("close:{stage}"));
        self.trace.lock().unwrap().push(event);
        self.closed.fetch_add(1, Ordering::SeqCst);
        if self.stall_close.is_some() {
            pending::<()>().await;
        }
        Ok(())
    }
}

struct ShutdownFixture {
    started: bool,
    reason: &'static str,
    requests: Vec<Value>,
    trace: Arc<Mutex<Vec<String>>>,
}

impl ShutdownWorkspace for ShutdownFixture {
    fn begin_shutdown(&mut self) -> bool {
        if self.started {
            false
        } else {
            self.started = true;
            self.trace.lock().unwrap().push("latch-exit".to_string());
            true
        }
    }

    fn shutdown_reason(&self) -> &str {
        self.trace
            .lock()
            .unwrap()
            .push(format!("exit-reason:{}", self.reason));
        self.reason
    }

    fn take_shutdown_requests(&mut self) -> Vec<Value> {
        std::mem::take(&mut self.requests)
    }
}

fn cleanup_requests() -> Vec<Value> {
    vec![
        json!({"type": "terminal_release_control"}),
        json!({"type": "terminal_detach"}),
    ]
}

struct TraceModeBackend(Arc<Mutex<Vec<String>>>);

impl ModeBackend for TraceModeBackend {
    fn enter(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        self.0.lock().unwrap().push("restore-terminal".to_string());
        Ok(())
    }
}

#[derive(Clone)]
struct LifecycleLayer(Arc<Mutex<Vec<String>>>);

#[derive(Default)]
struct LifecycleVisitor {
    stage: Option<String>,
}

impl tracing::field::Visit for LifecycleVisitor {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "lifecycle_stage" {
            self.stage = Some(format!("{value:?}").trim_matches('"').to_string());
        }
    }

    fn record_str(&mut self, field: &tracing::field::Field, value: &str) {
        if field.name() == "lifecycle_stage" {
            self.stage = Some(value.to_string());
        }
    }
}

impl<S> Layer<S> for LifecycleLayer
where
    S: Subscriber,
{
    fn on_event(&self, event: &Event<'_>, _context: LayerContext<'_, S>) {
        let mut visitor = LifecycleVisitor::default();
        event.record(&mut visitor);
        if let Some(stage) = visitor.stage {
            self.0.lock().unwrap().push(stage);
        }
    }
}

#[derive(Clone, Copy, Debug)]
enum LiveShutdownStall {
    Detach,
    ReconnectJoin,
    ReaderShutdown,
    SinkClose,
}

#[derive(Clone, Copy, Debug)]
enum LiveShutdownTrigger {
    Quit,
    Sigterm,
}

impl LiveShutdownTrigger {
    fn reason(self) -> &'static str {
        match self {
            Self::Quit => "quit",
            Self::Sigterm => "SIGTERM",
        }
    }
}

impl LiveShutdownStall {
    fn close_stage(self) -> Option<&'static str> {
        match self {
            Self::Detach => None,
            Self::ReconnectJoin => Some("reconnect-join"),
            Self::ReaderShutdown => Some("reader-shutdown"),
            Self::SinkClose => Some("sink-close"),
        }
    }
}

async fn wait_for_ws_request(mock: &MockDaemon, kind: &str) {
    timeout(Duration::from_secs(1), async {
        loop {
            if mock.requests().iter().any(|request| {
                request
                    .body
                    .as_ref()
                    .and_then(|body| body.get("type"))
                    .and_then(Value::as_str)
                    == Some(kind)
            }) {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {kind}"));
}

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("live loop input");
}

fn send_process_signal(signal: &str) {
    let pid = std::process::id().to_string();
    let status = std::process::Command::new("kill")
        .args([signal, pid.as_str()])
        .status()
        .unwrap_or_else(|error| panic!("send {signal} to the gclient test process: {error}"));
    assert!(status.success(), "kill {signal} failed: {status}");
}

async fn assert_live_shutdown_stall(trigger: LiveShutdownTrigger, stall: LiveShutdownStall) {
    let mock = MockDaemon::start("shutdown-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-shutdown", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    if matches!(stall, LiveShutdownStall::Detach) {
        mock.suppress_ws("terminal_detach");
    }
    let daemon = LiveDaemon::connect(mock.url(), "shutdown-token")
        .await
        .expect("connect live daemon");
    if let Some(stage) = stall.close_stage() {
        daemon.inject_close_stall(stage);
    }
    let initial_generation = daemon.generation();
    let observer = daemon.clone();
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tab(&mut workspace, "project-1", "terminal-shutdown");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);
    let (guard, restore_hits) = TerminalGuard::recording();

    let driver = async {
        wait_for_ws_request(&mock, "terminal_attach").await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_ws_request(&mock, "terminal_input").await;
        let reconnect = if matches!(stall, LiveShutdownStall::ReconnectJoin) {
            let gate = mock.pause_next_websocket();
            let reconnect = {
                let daemon = observer.clone();
                tokio::spawn(async move { daemon.reconnect(initial_generation).await })
            };
            timeout(Duration::from_secs(1), async {
                while mock.websocket_handshakes() < 2 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("reconnect reached the controlled handshake stall");
            Some((gate, reconnect))
        } else {
            None
        };
        let exit_started = Instant::now();
        match trigger {
            LiveShutdownTrigger::Quit => {
                send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
                send_key(&input_tx, KeyCode::Char('Q'), KeyModifiers::SHIFT).await;
            }
            LiveShutdownTrigger::Sigterm => {
                send_process_signal("-TERM");
            }
        }
        (exit_started, reconnect)
    };

    let mut switch = TerminalGuard::recording().0;
    let (loop_result, (exit_started, reconnect)) = tokio::join!(
        timeout(
            Duration::from_millis(2_500),
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
        ),
        driver,
    );
    loop_result
        .unwrap_or_else(|_| panic!("{trigger:?}/{stall:?} exceeded the outer shutdown bound"))
        .unwrap_or_else(|error| panic!("{trigger:?}/{stall:?} shutdown failed: {error}"));
    drop(guard);
    assert!(
        exit_started.elapsed() <= Duration::from_millis(2_250),
        "{trigger:?}/{stall:?} exceeded the two-second shutdown deadline"
    );
    if matches!(stall, LiveShutdownStall::Detach) {
        assert!(
            exit_started.elapsed() >= Duration::from_secs(2),
            "{trigger:?} detach stall did not exhaust the close deadline"
        );
    }
    assert_eq!(workspace.exit_reason(), Some(trigger.reason()));
    assert_eq!(
        restore_hits.load(Ordering::SeqCst),
        1,
        "{trigger:?}/{stall:?} must restore the terminal exactly once"
    );

    timeout(Duration::from_millis(250), async {
        while observer.shutdown_resources() != (false, false, false) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| {
        panic!(
            "{stall:?} left reconnect/reader/sink resources alive: {:?}",
            observer.shutdown_resources()
        )
    });
    let snapshot = observer.subscribe().0;
    assert_eq!(snapshot.generation, initial_generation);
    assert!(!snapshot.ready, "{stall:?} republished daemon readiness");
    assert!(matches!(
        observer.reconnect(initial_generation).await,
        Err(DaemonError::Unavailable { .. })
    ));

    if let Some((gate, reconnect)) = reconnect {
        assert_eq!(
            timeout(Duration::from_millis(250), reconnect)
                .await
                .expect("close must settle the reconnect owner")
                .expect("reconnect owner task"),
            Err(DaemonError::Unavailable { retry_after: None })
        );
        gate.notify_waiters();
    }
    timeout(Duration::from_millis(250), mock.wait_for_no_websockets())
        .await
        .unwrap_or_else(|_| panic!("{trigger:?}/{stall:?} left the WebSocket connected"));
    mock.shutdown().await;
}

#[derive(Clone, Copy, Debug)]
enum LiveExitCause {
    Quit,
    Sigint,
    Sigterm,
    Sighup,
    DaemonLoss,
}

impl LiveExitCause {
    fn reason(self) -> &'static str {
        match self {
            Self::Quit => "quit",
            Self::Sigint => "SIGINT",
            Self::Sigterm => "SIGTERM",
            Self::Sighup => "SIGHUP",
            Self::DaemonLoss => "daemon unavailable",
        }
    }
}

async fn assert_live_exit_trace(cause: LiveExitCause, trace: Arc<Mutex<Vec<String>>>) {
    let mock = MockDaemon::start("exit-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-exit", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-exit", "seq": 1}
        }),
    );
    if matches!(cause, LiveExitCause::DaemonLoss) {
        for _ in 0..3 {
            mock.enqueue(
                "GET",
                "/api/terminals?",
                500,
                json!({"code": "reconcile_failed", "message": "roster unavailable"}),
            );
        }
    }
    let daemon = LiveDaemon::connect(mock.url(), "exit-token")
        .await
        .expect("connect exit daemon");
    if matches!(cause, LiveExitCause::DaemonLoss) {
        mock.fail_next_websocket();
        mock.fail_next_websocket();
    }
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tab(&mut workspace, "project-1", "terminal-exit");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);
    let mut guard = TerminalGuard::new(TraceModeBackend(Arc::clone(&trace)));
    guard.arm(false).expect("arm recording terminal");

    let driver = async {
        wait_for_ws_request(&mock, "terminal_attach").await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_ws_request(&mock, "terminal_input").await;
        match cause {
            LiveExitCause::Quit => {
                send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
                send_key(&input_tx, KeyCode::Char('Q'), KeyModifiers::SHIFT).await;
            }
            LiveExitCause::Sigint => send_process_signal("-INT"),
            LiveExitCause::Sigterm => send_process_signal("-TERM"),
            LiveExitCause::Sighup => send_process_signal("-HUP"),
            LiveExitCause::DaemonLoss => {
                mock.drop_websockets();
                timeout(Duration::from_secs(1), async {
                    while mock.websocket_handshakes() < 2 {
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .expect("daemon-loss reconnect started");
                tokio::time::pause();
                for (expected_handshakes, expected_roster_reads) in [(3, 1), (4, 2), (5, 3), (6, 4)]
                {
                    for _ in 0..8 {
                        let roster_reads = mock
                            .requests()
                            .iter()
                            .filter(|request| {
                                request.method == "GET"
                                    && request.target.starts_with("/api/terminals?")
                            })
                            .count();
                        if mock.websocket_handshakes() >= expected_handshakes
                            && roster_reads >= expected_roster_reads
                        {
                            break;
                        }
                        tokio::time::advance(Duration::from_secs(2)).await;
                        for _ in 0..256 {
                            tokio::task::yield_now().await;
                        }
                    }
                }
                tokio::time::resume();
            }
        }
    };

    let mut switch = TerminalGuard::recording().0;
    let (loop_result, ()) = tokio::join!(
        timeout(
            Duration::from_secs(30),
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
        ),
        driver,
    );
    loop_result
        .unwrap_or_else(|_| panic!("{cause:?} exceeded the outer exit bound"))
        .unwrap_or_else(|error| panic!("{cause:?} live-loop exit failed: {error}"));
    drop(guard);
    assert_eq!(workspace.exit_reason(), Some(cause.reason()));
    mock.shutdown().await;
}

#[tokio::test]
async fn graceful_exit_releases_and_detaches_within_deadline() {
    let trace = Arc::new(Mutex::new(Vec::new()));
    let daemon = TraceDaemon::new(Arc::clone(&trace), false, None);
    let mut workspace = ShutdownFixture {
        started: false,
        reason: "quit",
        requests: cleanup_requests(),
        trace: Arc::clone(&trace),
    };
    let mut guard = TerminalGuard::new(TraceModeBackend(Arc::clone(&trace)));
    guard.arm(false).unwrap();

    shutdown(
        &mut workspace,
        daemon,
        Instant::now() + Duration::from_millis(100),
    )
    .await
    .unwrap();
    drop(guard);

    assert_eq!(
        *trace.lock().unwrap(),
        [
            "latch-exit",
            "exit-reason:quit",
            "terminal_release_control",
            "terminal_detach",
            "cleanup-awaited",
            "close",
            "restore-terminal",
        ]
    );

    for trigger in [LiveShutdownTrigger::Quit, LiveShutdownTrigger::Sigterm] {
        for stall in [
            LiveShutdownStall::Detach,
            LiveShutdownStall::ReconnectJoin,
            LiveShutdownStall::ReaderShutdown,
            LiveShutdownStall::SinkClose,
        ] {
            assert_live_shutdown_stall(trigger, stall).await;
        }
    }
}

#[tokio::test]
async fn shutdown_closes_the_daemon_through_the_trait() {
    let trace = Arc::new(Mutex::new(Vec::new()));
    let daemon = TraceDaemon::new(Arc::clone(&trace), true, None);
    let closed = Arc::clone(&daemon.closed);
    let mut workspace = ShutdownFixture {
        started: false,
        reason: "quit",
        requests: cleanup_requests(),
        trace: Arc::clone(&trace),
    };

    let result = tokio::time::timeout(
        Duration::from_millis(250),
        shutdown(
            &mut workspace,
            daemon.clone(),
            Instant::now() + Duration::from_millis(25),
        ),
    )
    .await
    .expect("shutdown respects its deadline");
    assert_eq!(result, Ok(()));
    assert_eq!(closed.load(Ordering::SeqCst), 1);
    assert_eq!(
        *trace.lock().unwrap(),
        [
            "latch-exit",
            "exit-reason:quit",
            "terminal_release_control",
            "terminal_detach",
            "close"
        ]
    );

    shutdown(&mut workspace, daemon, Instant::now())
        .await
        .unwrap();
    assert_eq!(
        closed.load(Ordering::SeqCst),
        1,
        "shutdown seam is idempotent"
    );

    let trace = Arc::new(Mutex::new(Vec::new()));
    let daemon = TraceDaemon::new(Arc::clone(&trace), false, Some("expired"));
    let closed = Arc::clone(&daemon.closed);
    let mut workspace = ShutdownFixture {
        started: false,
        reason: "quit",
        requests: cleanup_requests(),
        trace: Arc::clone(&trace),
    };
    shutdown(&mut workspace, daemon, Instant::now())
        .await
        .unwrap();
    assert_eq!(closed.load(Ordering::SeqCst), 1);
    assert!(
        trace
            .lock()
            .unwrap()
            .iter()
            .any(|event| event == "close:expired"),
        "close must receive one first poll even after the deadline expires"
    );
}

#[tokio::test]
async fn live_daemon_expired_close_latches_before_returning() {
    let mock = MockDaemon::start("close-token").await;
    let daemon = LiveDaemon::new(mock.url(), "close-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;

    let generation = daemon.generation();
    let result = Daemon::close(&daemon, Instant::now()).await;
    assert_eq!(result, Err(DaemonError::Timeout));
    assert!(matches!(
        daemon.reconnect(generation).await,
        Err(DaemonError::Unavailable { .. })
    ));
    tokio::time::timeout(Duration::from_millis(250), mock.wait_for_no_websockets())
        .await
        .expect("expired close must synchronously stop the live connection");
    mock.shutdown().await;
}

#[tokio::test]
async fn every_exit_cause_uses_one_shutdown_seam() {
    let trace = Arc::new(Mutex::new(Vec::new()));
    let subscriber = tracing_subscriber::registry().with(LifecycleLayer(Arc::clone(&trace)));
    let _subscriber = tracing::subscriber::set_default(subscriber);

    for cause in [
        LiveExitCause::Quit,
        LiveExitCause::Sigint,
        LiveExitCause::Sigterm,
        LiveExitCause::Sighup,
        LiveExitCause::DaemonLoss,
    ] {
        assert_live_exit_trace(cause, Arc::clone(&trace)).await;
        assert_eq!(
            std::mem::take(&mut *trace.lock().unwrap()),
            [
                "latch-exit",
                "release-held-leases",
                "detach-attachments",
                "cleanup-settled",
                "daemon-close",
                "restore-terminal"
            ],
            "{cause:?} must traverse the same shutdown seam exactly once"
        );
    }

    let panic_result = catch_unwind(AssertUnwindSafe({
        let trace = Arc::clone(&trace);
        move || {
            let mut guard = TerminalGuard::new(TraceModeBackend(trace));
            guard.arm(false).expect("arm panic terminal");
            let _guard = guard;
            panic!("injected panic");
        }
    }));
    assert!(panic_result.is_err());
    assert_eq!(
        std::mem::take(&mut *trace.lock().unwrap()),
        ["restore-terminal"],
        "panic must perform only synchronous guard restoration"
    );
}
