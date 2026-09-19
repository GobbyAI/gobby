//! 3.3.5 select → spawn → attach → terminate against scripted endpoints.

mod mock_daemon;

use std::collections::VecDeque;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::run_loop::{
    run_scripted_loop, ReconnectAttempt, ReconnectSupervisor, RECONNECT_DELAYS, RENDER_TICK,
};
use gobby_client::app::sidebar_model::{GIT_REFRESH_INTERVAL, ROSTER_REFRESH_INTERVAL};
use gobby_client::app::{
    close_project, close_project_confirmed, create_worktree, focus_agent, focus_project,
    open_new_worktree_dialog, open_open_worktree_dialog, open_remove_worktree_dialog,
    remove_worktree, route_modal_key, run_live_loop, sync_live_chrome, AttachState, ModalOutcome,
    HOST_GRANT_UNAVAILABLE,
};
use gobby_client::daemon::{
    Answer, Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, KillOutcome, LiveDaemon,
    Page, ProjectRow, RosterEntry, RunRow, ScriptedDaemon, SessionRow, SourceStatus, SpawnOutcome,
    SpawnRequest, SubscribeSnapshot, TerminalRow, WorkspaceOp, WorktreeRow, WsMessage, WsReply,
    CONTROL_REQUEST_DEADLINE,
};
use gobby_client::frame_source::{
    AttachLocator, FrameError, PaneFrameSource, ScriptedFrameSource, Transport,
    UnixSocketFrameSource,
};
use gobby_client::key_input::KeyInput;
use gobby_client::prefs::{prefs_path, save_prefs};
use gobby_client::startup::{initial_project, Ready};
use gobby_client::teardown::{RecordingBackend, TerminalGuard};
use gobby_client::ui::chrome::{Mode, RowState};
use gobby_client::ui::dialogs::{CloseScope, CloseTarget, Dialog, WorktreeChoice};
use gobby_client::ui::hit::{hit_test, Hit};
use gobby_client::ui::keymap::{default_prefix, Keymap, HERDR_PREFIX};
use gobby_client::ui::pane_layout::{metrics_for, pane_inner_rect, scrollbar_gutter};
use gobby_client::ui::scrollbar::{
    scrollbar_offset_from_drag_row, scrollbar_offset_from_row, scrollbar_thumb_grab_offset,
};
use gobby_client::ui::settings::{ClientPrefs, PassthroughModifier};
use gobby_client::ui::sidebar::TERMINAL_ROW;
use gobby_client::ui::status::{Toast, ToastKind};
use gobby_client::ui::{render_workspace, Chrome, WorkspaceView};
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneModes, ServerMessage, MAX_FRAME_SIZE,
};
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::net::UnixStream;
use tokio::sync::mpsc;
use tokio::time::{timeout, Instant};

fn websocket_requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!(kind)))
        .collect()
}

fn terminal_side_effects(mock: &MockDaemon) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| {
            matches!(
                body.get("type").and_then(Value::as_str),
                Some(
                    "terminal_attach"
                        | "terminal_detach"
                        | "terminal_take_control"
                        | "terminal_release_control"
                        | "terminal_set_viewport"
                        | "terminal_input"
                )
            )
        })
        .collect()
}

/// Every `workspace_op` request whose `op` is `op`.
fn workspace_ops(mock: &MockDaemon, op: &str) -> Vec<Value> {
    websocket_requests(mock, "workspace_op")
        .into_iter()
        .filter(|request| request.get("op") == Some(&json!(op)))
        .collect()
}

async fn wait_for_websocket_requests(mock: &MockDaemon, kind: &str, expected: usize) {
    timeout(Duration::from_secs(1), async {
        loop {
            if websocket_requests(mock, kind).len() >= expected {
                break;
            }
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

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("live loop input");
}

/// The prefix (ctrl+b) followed by one chord key.
async fn send_chord(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    send_key(input, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
    send_key(input, code, modifiers).await;
}

async fn send_mouse(
    input: &mpsc::Sender<RawInputEvent>,
    kind: MouseEventKind,
    column: u16,
    row: u16,
    modifiers: KeyModifiers,
) {
    input
        .send(RawInputEvent::Mouse(MouseEvent {
            kind,
            column,
            row,
            modifiers,
        }))
        .await
        .expect("live loop mouse input");
}

fn send_resize_burst(count: usize) {
    let pid = std::process::id().to_string();
    let mut command = std::process::Command::new("/bin/kill");
    command.arg("-WINCH");
    for _ in 0..count {
        command.arg(&pid);
    }
    assert!(command.status().expect("send SIGWINCH").success());
}

async fn settle_live_event() {
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
}

/// Show every roster pane the way the loop did before 2.2 restored tabs from
/// the snapshot: split into one tab in roster order, the last one focused.
fn show_roster(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    for terminal_id in workspace.roster_terminal_ids() {
        if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
            chrome.open_pane(pane, workspace.pane(pane).display_name());
        }
    }
}

/// Save a one-tab snapshot for `project` that nests `terminal_ids` as
/// horizontal splits with the last one focused, and point the workspace at
/// it so the loop restores that tab once the roster arrives. Keep the
/// returned home alive for the loop's lifetime.
/// Seed the mock's daemon workspace with one tab of `terminal_ids` for
/// `project` (the last one focused) and give the workspace a Gobby home, so
/// the loop projects that tab instead of spawning a shell (4.2: the layout
/// comes from the attached workspace, never from a snapshot file).
fn pin_tabs(
    mock: &MockDaemon,
    workspace: &mut Workspace<LiveDaemon>,
    project: &str,
    terminal_ids: &[&str],
) -> tempfile::TempDir {
    let home = tempfile::tempdir().expect("gobby home");
    let focused = terminal_ids.last().copied().unwrap_or_default();
    mock.seed_workspace(project, &[(terminal_ids, focused)]);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project(project);
    home
}

fn semantic_frame(text: &str) -> ServerMessage {
    ServerMessage::Frame(FrameData {
        cells: text
            .chars()
            .map(|symbol| CellData {
                symbol: symbol.to_string(),
                fg: 0x10,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            })
            .collect(),
        width: u16::try_from(text.chars().count()).expect("test frame width"),
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    })
}

fn encode_frame(message: &ServerMessage) -> String {
    let mut framed = Vec::new();
    write_message(&mut framed, message).expect("encode test frame");
    STANDARD.encode(&framed[4..])
}

fn encoded_frame(text: &str) -> String {
    encode_frame(&semantic_frame(text))
}

/// `encoded_frame` from a pane that switched to its alternate screen.
fn encoded_alternate_frame(text: &str) -> String {
    let ServerMessage::Frame(mut frame) = semantic_frame(text) else {
        unreachable!("semantic_frame builds a frame");
    };
    frame.modes.alternate_on = true;
    encode_frame(&ServerMessage::Frame(frame))
}

/// `semantic_frame` from a pane whose app tracks every motion with SGR
/// reports.
fn reporting_frame(text: &str) -> ServerMessage {
    let ServerMessage::Frame(mut frame) = semantic_frame(text) else {
        unreachable!("semantic_frame builds a frame");
    };
    frame.modes.mouse_all = true;
    frame.modes.mouse_sgr = true;
    ServerMessage::Frame(frame)
}

async fn live_workspace_with_scripted_direct(
    mock: &MockDaemon,
    terminal_id: &str,
    roster_reads: usize,
) -> (Workspace<LiveDaemon>, String) {
    for _ in 0..roster_reads {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": terminal_id, "backend": "native", "state": "live"}],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial proxy attachment");
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("initial terminal pane");
    let old_attachment = workspace.pane(pane_id).attachment_id().to_string();
    workspace
        .replace_frame_source(
            pane_id,
            PaneFrameSource::Scripted(ScriptedFrameSource::new(Transport::Direct)),
        )
        .expect("install scripted direct source");
    (workspace, old_attachment)
}

#[test]
fn live_entry_connects_before_running() {
    let result = gobby_client::views::run_ready(
        Ready {
            daemon_url: "not a URL".to_string(),
            token: Some("test-token".to_string()),
            project: Some("project-1".to_string()),
            frame_delivery: gobby_client::FrameDelivery::Auto,
            host: None,
            host_notice: None,
            prefs: gobby_client::ui::settings::ClientPrefs::default(),
            keymap: Keymap::defaults(HERDR_PREFIX),
            nested: false,
            in_pane: false,
            gobby_home: std::path::PathBuf::new(),
            launch_dir: std::path::PathBuf::new(),
            attach: gobby_client::startup::AttachTarget::default(),
        },
        &mut TerminalGuard::recording().0,
    );

    assert!(
        result.is_err(),
        "the app entry must attempt a live daemon connection"
    );
}

#[tokio::test]
async fn live_created_event_attaches_before_next_reconciliation() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-initial", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["terminal-initial"]);
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        timeout(Duration::from_secs(1), async {
            loop {
                let initial_attached = mock.requests().iter().any(|request| {
                    request.body.as_ref().is_some_and(|body| {
                        body.get("type") == Some(&json!("terminal_attach"))
                            && body.get("terminal_id") == Some(&json!("terminal-initial"))
                    })
                });
                if initial_attached {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("initial reconciliation");
        mock.send_event_and_wait(json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-created",
            "backend": "native",
            "daemon_epoch": "epoch-1",
            "seq": 2
        }))
        .await;
        timeout(Duration::from_secs(1), async {
            loop {
                let created_attached = mock.requests().iter().any(|request| {
                    request.body.as_ref().is_some_and(|body| {
                        body.get("type") == Some(&json!("terminal_attach"))
                            && body.get("terminal_id") == Some(&json!("terminal-created"))
                    })
                });
                if created_attached {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("created terminal attaches without waiting for a relist");
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
    result.expect("live loop exits cleanly");
    let created = workspace
        .pane_for_terminal("terminal-created")
        .expect("created terminal pane");
    assert!(workspace.pane(created).is_live());
    mock.shutdown().await;
}

#[derive(Debug, Clone)]
struct ReconnectDaemon {
    inner: ScriptedDaemon,
    outcomes: Arc<Mutex<VecDeque<Result<Generation, DaemonError>>>>,
    calls: Arc<Mutex<Vec<Generation>>>,
}

impl ReconnectDaemon {
    fn new(outcomes: impl IntoIterator<Item = Result<Generation, DaemonError>>) -> Self {
        Self {
            inner: ScriptedDaemon::new(),
            outcomes: Arc::new(Mutex::new(outcomes.into_iter().collect())),
            calls: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn calls(&self) -> Vec<Generation> {
        self.calls.lock().expect("reconnect calls lock").clone()
    }
}

impl Daemon for ReconnectDaemon {
    async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        self.inner.list_terminals(project, cursor).await
    }

    async fn inventory_page(
        &self,
        states: &[&str],
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        self.inner.inventory_page(states, cursor).await
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

    async fn init_project(&self, path: &str) -> Result<ProjectRow, DaemonError> {
        Daemon::init_project(&self.inner, path).await
    }

    async fn create_worktree(
        &self,
        project: &str,
        branch: &str,
        base: Option<&str>,
    ) -> Result<WorktreeRow, DaemonError> {
        Daemon::create_worktree(&self.inner, project, branch, base).await
    }

    async fn delete_worktree(&self, worktree_id: &str) -> Result<(), DaemonError> {
        Daemon::delete_worktree(&self.inner, worktree_id).await
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
        self.inner.mark_seen(entry, attention_id).await
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnOutcome, DaemonError> {
        self.inner.spawn(request).await
    }

    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError> {
        self.inner.terminate(terminal_id).await
    }

    fn subscribe(&self) -> (SubscribeSnapshot, EventReceiver) {
        Daemon::subscribe(&self.inner)
    }

    async fn send(&self, message: WsMessage) -> Result<WsReply, DaemonError> {
        self.inner.send(message).await
    }

    async fn notify(&self, message: WsMessage) -> Result<(), DaemonError> {
        self.inner.notify(message).await
    }

    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError> {
        self.calls
            .lock()
            .expect("reconnect calls lock")
            .push(observed);
        self.outcomes
            .lock()
            .expect("reconnect outcomes lock")
            .pop_front()
            .expect("scripted reconnect outcome")
    }

    async fn close(&self, deadline: Instant) -> Result<(), DaemonError> {
        self.inner.close(deadline).await
    }

    async fn attach_workspace(
        &self,
        node: Option<&str>,
        workspace: Option<&str>,
    ) -> Result<gobby_client::daemon::WorkspaceSnapshot, DaemonError> {
        self.inner.attach_workspace(node, workspace).await
    }

    async fn workspace_op(
        &self,
        op: gobby_client::daemon::WorkspaceOp,
    ) -> Result<gobby_client::daemon::WorkspaceReply, DaemonError> {
        self.inner.workspace_op(op).await
    }
}

#[tokio::test(start_paused = true)]
async fn loop_routes_input_and_frames() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-loop", "native", "epoch-loop")
        .expect("open terminal");
    ws.force_held(pane);
    // A proxy source, so the pane's keystrokes are the daemon's to carry; a
    // direct pane types on its own frame socket instead (#22573).
    let mut source = ScriptedFrameSource::new(Transport::Proxy);
    source.queue(ServerMessage::Frame(FrameData {
        cells: "HELLO"
            .chars()
            .map(|symbol| CellData {
                symbol: symbol.to_string(),
                fg: 0x10,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            })
            .collect(),
        width: 5,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    }));
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("scripted source");

    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "loop");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(256);
    let daemon = ws.daemon().clone();
    let (guard, restore_hits) = TerminalGuard::recording();

    let driver = async move {
        tokio::task::yield_now().await;
        input_tx
            .send(RawInputEvent::Key(TerminalKey::new(
                KeyCode::Char('x'),
                KeyModifiers::NONE,
            )))
            .await
            .expect("held input");
        tokio::task::yield_now().await;
        daemon.publish_event(gobby_client::daemon::DaemonEvent::Message(json!({
            "type": "attention",
            "epoch": "attention-loop",
            "seq": 1,
            "entry_id": "entry-loop"
        })));
        tokio::time::advance(RENDER_TICK * 2).await;
        tokio::task::yield_now().await;
        input_tx
            .send(RawInputEvent::Key(TerminalKey::new(
                KeyCode::Char('b'),
                KeyModifiers::CONTROL,
            )))
            .await
            .expect("prefix");
        input_tx
            .send(RawInputEvent::Key(TerminalKey::new(
                KeyCode::Char('Q'),
                KeyModifiers::SHIFT,
            )))
            .await
            .expect("quit");
    };

    let (result, ()) = tokio::join!(
        run_scripted_loop(&mut ws, &mut terminal, &mut chrome, input_rx),
        driver
    );
    result.expect("loop exits cleanly");
    drop(guard);

    assert_eq!(restore_hits.load(std::sync::atomic::Ordering::SeqCst), 1);
    assert!(ws
        .daemon()
        .ws_sent_types()
        .iter()
        .any(|kind| kind == "terminal_input"));
    assert!(
        !ws.daemon().ws_connected(),
        "the run loop must finish its shutdown seam before returning"
    );
    assert_eq!(ws.attention_entry_ids(), vec!["entry-loop".to_string()]);
    assert!(ws.pane(pane).frames_rendered() >= 2);
    let screen: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(screen.contains("HELLO"), "rendered grid: {screen:?}");
}

#[tokio::test]
async fn input_encoder_covers_named_keys() {
    use gobby_terminal::input::KeyboardProtocol;

    let cases = [
        (KeyCode::Up, KeyModifiers::NONE, b"\x1b[A".as_slice()),
        (KeyCode::F(5), KeyModifiers::NONE, b"\x1b[15~".as_slice()),
        (KeyCode::Up, KeyModifiers::CONTROL, b"\x1b[1;5A".as_slice()),
        (KeyCode::Char('x'), KeyModifiers::ALT, b"\x1bx".as_slice()),
    ];
    for (code, modifiers, expected) in cases {
        let encoded = gobby_client::input::key_to_bytes(KeyEvent::new(code, modifiers));
        assert_eq!(encoded.as_deref(), Some(expected));
    }

    let kitty = gobby_client::input::key_to_bytes_with_protocol(
        KeyEvent::new(KeyCode::F(5), KeyModifiers::CONTROL),
        KeyboardProtocol::Kitty { flags: 1 },
    );
    assert_eq!(kitty.as_deref(), Some(b"\x1b[15;5~".as_slice()));

    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-input", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["terminal-input"]);
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let live_cases = [
            (
                TerminalKey::new(KeyCode::Up, KeyModifiers::CONTROL),
                "\u{1b}[1;5A",
            ),
            (
                TerminalKey::new(KeyCode::F(5), KeyModifiers::CONTROL),
                "\u{1b}[15;5~",
            ),
            (
                TerminalKey::new(KeyCode::Char('x'), KeyModifiers::ALT),
                "\u{1b}x",
            ),
            // Physical-key metadata is not part of crossterm's KeyEvent. This
            // case distinguishes the required crate::input path from calling
            // gobby_terminal's TerminalKey encoder directly in the loop.
            (
                TerminalKey::new(KeyCode::Char('1'), KeyModifiers::SHIFT)
                    .with_shifted_codepoint('!' as u32),
                "1",
            ),
        ];
        for (index, (key, _)) in live_cases.iter().enumerate() {
            input_tx
                .send(RawInputEvent::Key(key.clone()))
                .await
                .expect("live loop input");
            wait_for_websocket_requests(&mock, "terminal_input", index + 1).await;
        }

        let writes = websocket_requests(&mock, "terminal_input");
        let data: Vec<_> = writes
            .iter()
            .map(|write| write.get("data").and_then(Value::as_str))
            .collect();
        let expected: Vec<_> = live_cases
            .iter()
            .map(|(_, expected)| Some(*expected))
            .collect();
        assert_eq!(data, expected, "the live loop must use crate::input");
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
    result.expect("live loop exits cleanly");
    mock.shutdown().await;
}

#[tokio::test]
async fn focus_moves_control_and_settles_pending_input_once() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(
        &mock,
        &mut workspace,
        "project-1",
        &["terminal-a", "terminal-b"],
    );
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let initially_focused = websocket_requests(&mock, "terminal_take_control")[0]
            .get("terminal_id")
            .and_then(Value::as_str)
            .expect("initial focused terminal")
            .to_string();
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Tab, KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 1).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;

        let releases = websocket_requests(&mock, "terminal_release_control");
        assert_eq!(
            releases[0].get("terminal_id").and_then(Value::as_str),
            Some(initially_focused.as_str())
        );
        assert!(
            websocket_requests(&mock, "terminal_detach")
                .iter()
                .all(|request| {
                    request.get("terminal_id").and_then(Value::as_str)
                        != Some(initially_focused.as_str())
                }),
            "focus change must release control without detaching the old pane"
        );
        let focused_request = websocket_requests(&mock, "terminal_take_control")
            .into_iter()
            .find(|request| {
                request.get("terminal_id").and_then(Value::as_str)
                    != Some(initially_focused.as_str())
            })
            .expect("focused pane take-control request");
        let focused_terminal_id = focused_request
            .get("terminal_id")
            .and_then(Value::as_str)
            .expect("focused terminal")
            .to_string();
        let attachment_id = focused_request
            .get("attachment_id")
            .and_then(Value::as_str)
            .expect("focused attachment")
            .to_string();

        mock.send_event_and_wait(json!({
            "type": "terminal_lease_lost",
            "terminal_id": focused_terminal_id,
            "attachment_id": attachment_id,
            "holder": "peer",
            "lease_generation": 2,
            "daemon_epoch": "epoch-1",
            "seq": 2
        }))
        .await;
        settle_live_event().await;

        // A lost lease refuses typing until control is taken explicitly.
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            2,
            "typing into a lost lease starts no request"
        );
        assert!(websocket_requests(&mock, "terminal_input").is_empty());
        // The take-back is refused: the pane observes with a take-back offer.
        mock.enqueue_take_control_reply(false, 2, Some("held by peer"));
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        settle_live_event().await;

        // Typing into the observed pane takes control; a stale grant leaves
        // the key pending.
        mock.enqueue_take_control_reply(true, 1, None);
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 4).await;
        assert!(
            websocket_requests(&mock, "terminal_input").is_empty(),
            "a stale grant cannot settle the pending key"
        );
        send_key(&input_tx, KeyCode::Char('z'), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            4,
            "further keys must not start another request while input is pending"
        );

        mock.enqueue_take_control_reply(true, 2, None);
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 5).await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
        let writes = websocket_requests(&mock, "terminal_input");
        assert_eq!(writes.len(), 1, "the pending key must be written once");
        assert_eq!(writes[0].get("data"), Some(&json!("x")));

        mock.send_event_and_wait(json!({
            "type": "terminal_lease_lost",
            "terminal_id": focused_terminal_id,
            "attachment_id": attachment_id,
            "holder": "peer",
            "lease_generation": 3,
            "daemon_epoch": "epoch-1",
            "seq": 3
        }))
        .await;
        settle_live_event().await;
        // Typing sends nothing again; the explicit take-back is refused and
        // the pane keeps its take-back offer.
        send_key(&input_tx, KeyCode::Char('y'), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert_eq!(websocket_requests(&mock, "terminal_take_control").len(), 5);
        mock.enqueue_take_control_reply(false, 3, Some("held by peer"));
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 6).await;
        tokio::task::yield_now().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            1,
            "a refused take-back writes nothing"
        );
        drop(input_tx);
        focused_terminal_id
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, focused_terminal_id) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    let pane_id = workspace
        .pane_for_terminal(&focused_terminal_id)
        .expect("focused terminal pane");
    assert!(workspace.pane(pane_id).is_observe());
    assert!(workspace.pane(pane_id).has_take_back());
    assert!(
        chrome
            .last_alert()
            .is_some_and(|message| message.contains("held by peer")),
        "control refusal reason must remain visible: {:?}",
        chrome.last_alert()
    );
    mock.shutdown().await;
}

/// A tab showing the user's own tmux session (`ownership: external`) next
/// to a gobby-owned terminal: the external pane holds the lease, so closing
/// the tab releases it and kills only the gobby-owned terminal.
async fn mixed_ownership_loop(mock: &MockDaemon) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mixed_ownership_loop_with(mock, false).await
}

/// `mixed_ownership_loop`, plus a second tab (`mock-tab-4`) holding one
/// more gobby-owned shell when `spare_tab`, so closing the first tab leaves
/// a tab for the window to show.
async fn mixed_ownership_loop_with(
    mock: &MockDaemon,
    spare_tab: bool,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    let external = json!({
        "terminal_id": "terminal-mine",
        "backend": "tmux",
        "state": "live",
        "ownership": "external"
    });
    let owned = json!({
        "terminal_id": "terminal-gobby",
        "backend": "native",
        "state": "live",
        "ownership": "gobby"
    });
    let spare = json!({
        "terminal_id": "terminal-spare",
        "backend": "native",
        "state": "live",
        "ownership": "gobby"
    });
    let mut items = vec![owned, external.clone()];
    let mut survivors = vec![external];
    let mut tabs: Vec<(&[&str], &str)> =
        vec![(&["terminal-gobby", "terminal-mine"], "terminal-mine")];
    if spare_tab {
        items.push(spare.clone());
        survivors.push(spare);
        tabs.push((&["terminal-spare"], "terminal-spare"));
    }
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": items,
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    // The relist after a kill lists only the survivors.
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": survivors,
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 2}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    mock.seed_workspace("project-1", &tabs);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    (workspace, home)
}

/// Closing a tab kills its gobby-owned pane and only releases the external
/// tmux session's lease: no `terminal_kill` names it, the tab goes, and the
/// session stays in the roster.
#[tokio::test]
async fn closing_a_tab_spares_external_tmux_sessions() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('X'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
        // The tab goes through the daemon once its owned pane is killed.
        wait_until(|| workspace_ops(&mock, "tab.close").len() == 1).await;
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
    result.expect("live loop exits cleanly");
    let kills = websocket_requests(&mock, "terminal_kill");
    assert_eq!(kills.len(), 1, "only the gobby-owned terminal is killed");
    assert_eq!(
        kills[0].get("terminal_id"),
        Some(&json!("terminal-gobby")),
        "the kill names the gobby-owned terminal"
    );
    let releases = websocket_requests(&mock, "terminal_release_control");
    assert_eq!(releases.len(), 1, "the external pane's lease is released");
    assert_eq!(
        releases[0].get("terminal_id"),
        Some(&json!("terminal-mine"))
    );
    // 4.2: the released external pane and then the tab leave through the
    // daemon, which disposes of the rows and tells every window.
    assert_eq!(
        workspace_ops(&mock, "pane.close")
            .iter()
            .map(|op| op["pane"].clone())
            .collect::<Vec<_>>(),
        [json!("mock-pane-3")],
        "the external pane's row (the seed mints tab-1, pane-2, pane-3) is closed, never its terminal"
    );
    assert_eq!(
        workspace_ops(&mock, "tab.close")
            .iter()
            .map(|op| op["tab"].clone())
            .collect::<Vec<_>>(),
        [json!("mock-tab-1")],
        "the tab is closed through the daemon"
    );
    assert!(
        workspace.pane_for_terminal("terminal-mine").is_some(),
        "the external session stays in the roster"
    );
    assert!(
        workspace.pane_for_terminal("terminal-gobby").is_none(),
        "the killed terminal left the roster"
    );
    mock.shutdown().await;
}

/// `close tab` when the daemon refuses the gobby-owned kill: the external
/// pane still leaves the tab with its lease released, the refused pane keeps
/// its place, and so the tab stays.
#[tokio::test]
async fn closing_a_tab_keeps_a_pane_whose_kill_is_refused() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop(&mock).await;
    mock.enqueue_kill_refusal("terminal_busy");
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('X'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
        // The released external pane leaves the daemon's tab through pane.close.
        wait_until(|| workspace_ops(&mock, "pane.close").len() == 1).await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        websocket_requests(&mock, "terminal_release_control").len(),
        1,
        "the external pane's lease is released"
    );
    assert!(
        workspace_ops(&mock, "tab.close").is_empty(),
        "a refused kill keeps the tab open"
    );
    let gobby = workspace
        .pane_for_terminal("terminal-gobby")
        .expect("the refused terminal stays in the roster");
    assert!(
        !workspace.pane(gobby).is_terminating(),
        "the pane is no longer marked terminating"
    );
    let tab = chrome.active_tab().expect("the tab keeps the refused pane");
    assert!(
        tab.slot_for(gobby).is_some(),
        "the refused pane keeps its slot"
    );
    assert_eq!(
        workspace_ops(&mock, "pane.close")
            .iter()
            .map(|op| op["pane"].clone())
            .collect::<Vec<_>>(),
        [json!("mock-pane-3")],
        "the external pane (the seed mints tab-1, pane-2, pane-3) left the tab through the daemon"
    );
    let mine = workspace
        .pane_for_terminal("terminal-mine")
        .expect("the external session stays in the roster");
    assert!(workspace.pane(mine).is_observe(), "its lease was released");
    mock.shutdown().await;
}

/// `close_pane` on an external tmux session releases its lease and drops the
/// slot; the session is never killed and the tab keeps its other pane.
#[tokio::test]
async fn closing_a_pane_spares_an_external_tmux_session() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 1).await;
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
    result.expect("live loop exits cleanly");
    assert!(
        websocket_requests(&mock, "terminal_kill").is_empty(),
        "an external session is never killed"
    );
    let tab = chrome.active_tab().expect("the tab keeps its other pane");
    assert_eq!(tab.slots.len(), 1);
    let mine = workspace
        .pane_for_terminal("terminal-mine")
        .expect("the external session stays in the roster");
    assert!(
        tab.slot_for(mine).is_none(),
        "the external pane left the tab"
    );
    assert!(workspace.pane(mine).is_observe(), "its lease was released");
    mock.shutdown().await;
}

/// A start outside every checkout with nothing saved opens on the personal
/// project: its empty snapshot seeds one shell tab, spawned for that
/// project in the directory gclient was launched from.
#[tokio::test]
async fn a_project_less_start_opens_one_shell_in_the_launch_directory() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 2}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.set_launch_dir(std::path::PathBuf::from("/home/me/notes"));
    let project = initial_project(None, None);
    workspace.select_project(&project);
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        // The daemon's tab.created event places the shell; the loop then
        // focuses its pane and reports that as the workspace's focus hint.
        wait_until(|| {
            websocket_requests(&mock, "workspace_op").iter().any(|op| {
                op.get("op") == Some(&json!("workspace.set_focus_hints"))
                    && op.get("pane").is_some_and(|pane| !pane.is_null())
            })
        })
        .await;
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
    result.expect("live loop exits cleanly");
    let creates = websocket_requests(&mock, "terminal_create");
    assert_eq!(creates.len(), 1, "exactly one shell is seeded");
    assert_eq!(
        creates[0].get("project_id"),
        Some(&json!(gobby_core::project::PERSONAL_PROJECT_ID)),
        "the shell belongs to the personal project"
    );
    assert_eq!(
        creates[0].get("cwd"),
        Some(&json!("/home/me/notes")),
        "the shell starts where gclient was launched"
    );
    assert_eq!(chrome.tabs().tabs.len(), 1, "one shell tab");
    mock.shutdown().await;
}

/// One live terminal pinned into a tab, for the control-refusal tests.
async fn single_terminal_loop(mock: &MockDaemon) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-a", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = pin_tabs(mock, &mut workspace, "project-1", &["terminal-a"]);
    (workspace, home)
}

/// A pane whose lease another viewer took refuses typing: nothing reaches
/// the daemon and the status line names take control (the scripted path's
/// `read_only` refusal, on the live loop).
#[tokio::test]
async fn lost_lease_refuses_typing_and_names_take_control() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let attachment_id = websocket_requests(&mock, "terminal_take_control")[0]
            .get("attachment_id")
            .and_then(Value::as_str)
            .expect("focused attachment")
            .to_string();
        mock.send_event_and_wait(json!({
            "type": "terminal_lease_lost",
            "terminal_id": "terminal-a",
            "attachment_id": attachment_id,
            "holder": "peer",
            "lease_generation": 2,
            "daemon_epoch": "epoch-1",
            "seq": 2
        }))
        .await;
        settle_live_event().await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        websocket_requests(&mock, "terminal_take_control").len(),
        1,
        "the key starts no take-control request"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "the key is not written"
    );
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    assert!(workspace.pane(pane).is_lease_lost());
    assert!(
        chrome
            .last_alert()
            .is_some_and(|message| message.contains("take control")),
        "the status names take control: {:?}",
        chrome.last_alert()
    );
    mock.shutdown().await;
}

/// Keys typed while a take-control request is still pending are dropped,
/// and the status line says control is being acquired.
#[tokio::test]
async fn keys_during_a_pending_take_report_acquiring_control() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let attachment_id = websocket_requests(&mock, "terminal_take_control")[0]
            .get("attachment_id")
            .and_then(Value::as_str)
            .expect("focused attachment")
            .to_string();
        // A lost lease and a refused take-back leave the pane observing at
        // lease generation 2; typing then takes control, and a stale grant
        // leaves the key pending.
        mock.send_event_and_wait(json!({
            "type": "terminal_lease_lost",
            "terminal_id": "terminal-a",
            "attachment_id": attachment_id,
            "holder": "peer",
            "lease_generation": 2,
            "daemon_epoch": "epoch-1",
            "seq": 2
        }))
        .await;
        settle_live_event().await;
        mock.enqueue_take_control_reply(false, 2, Some("held by peer"));
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        mock.enqueue_take_control_reply(true, 1, None);
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        send_key(&input_tx, KeyCode::Char('z'), KeyModifiers::NONE).await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        websocket_requests(&mock, "terminal_take_control").len(),
        3,
        "a pending take starts no second request"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "neither key is written before the grant"
    );
    assert!(
        chrome
            .last_alert()
            .is_some_and(|message| message.contains("acquiring control")),
        "the status reports the pending take: {:?}",
        chrome.last_alert()
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn write_outcomes_drive_pane_state() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue_write_outcome("delivered", None);
    mock.enqueue_write_outcome("indeterminate", Some("indeterminate_backend"));
    mock.enqueue_write_outcome("refused", Some("write_refused"));
    mock.enqueue_write_outcome("refused", Some("write_seq_conflict"));
    mock.enqueue_write_outcome("refused", Some("write_seq_expired"));
    mock.enqueue_write_outcome("refused", Some("write_seq_capacity"));
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "term-write", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-write", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["term-write"]);
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;

        send_key(&input_tx, KeyCode::Char('d'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            1,
            "a delivered write must leave the pane writable"
        );

        send_key(&input_tx, KeyCode::Char('i'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 2).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            2,
            "an indeterminate write must not be resent"
        );
        // An unknown write outcome refuses typing until control is taken.
        send_key(&input_tx, KeyCode::Char('r'), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            1,
            "typing into a read-only pane starts no request"
        );
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('t'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        send_key(&input_tx, KeyCode::Char('r'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 3).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            3,
            "a refused write must not be resent"
        );

        send_key(&input_tx, KeyCode::Char('c'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        wait_for_websocket_requests(&mock, "terminal_input", 4).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            4,
            "write_seq_conflict must not resend"
        );

        send_key(&input_tx, KeyCode::Char('e'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 5).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            5,
            "write_seq_expired must not resend"
        );

        send_key(&input_tx, KeyCode::Char('p'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 6).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            6,
            "write_seq_capacity must not resend"
        );
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
    result.expect("live loop exits cleanly");
    let pane_id = workspace
        .pane_for_terminal("term-write")
        .expect("live write pane");
    assert!(workspace.pane(pane_id).in_flight_write().is_none());
    assert!(workspace.pane(pane_id).writable());
    assert_eq!(websocket_requests(&mock, "terminal_input").len(), 6);
    mock.shutdown().await;
}

#[tokio::test]
async fn proxy_fallback_uses_fresh_attachment() {
    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        let terminal_id = "terminal-detach-result";
        let (mut workspace, old_attachment) =
            live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
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
        result.expect("detach-result fallback loop");
        let pane_id = workspace
            .pane_for_terminal(terminal_id)
            .expect("fallback pane");
        assert_eq!(workspace.pane(pane_id).attachment_id(), "attachment-2");
        assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Proxy));
        let detaches = websocket_requests(&mock, "terminal_detach");
        assert_eq!(detaches.len(), 2);
        assert_eq!(
            detaches[0].get("attachment_id").and_then(Value::as_str),
            Some(old_attachment.as_str())
        );
        assert_eq!(
            detaches[1].get("attachment_id").and_then(Value::as_str),
            Some("attachment-2"),
            "shutdown detaches the live replacement attachment"
        );
        assert_eq!(
            detaches
                .iter()
                .filter(|request| {
                    request.get("attachment_id").and_then(Value::as_str)
                        == Some(old_attachment.as_str())
                })
                .count(),
            1,
            "fallback detaches the old attachment exactly once"
        );
        assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 2);
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        let terminal_id = "terminal-finalization";
        let (mut workspace, old_attachment) =
            live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
        mock.suppress_ws("terminal_detach");
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
            mock.send_event_and_wait(json!({
                "type": "terminal_attachment_finalized",
                "daemon_epoch": "epoch-1",
                "seq": 2,
                "terminal_id": terminal_id,
                "attachment_id": old_attachment,
                "code": "host_eof",
                "reason": "direct observer ended"
            }))
            .await;
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
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
        result.expect("finalization fallback loop");
        let pane_id = workspace
            .pane_for_terminal(terminal_id)
            .expect("finalization pane");
        assert_eq!(workspace.pane(pane_id).attachment_id(), "attachment-2");
        assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Proxy));
        assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 2);
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        let terminal_id = "terminal-deadline";
        let (mut workspace, old_attachment) =
            live_workspace_with_scripted_direct(&mock, terminal_id, 3).await;
        mock.suppress_ws("terminal_detach");
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
            timeout(Duration::from_secs(4), async {
                loop {
                    if websocket_requests(&mock, "terminal_set_viewport").len() >= 2 {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("deadline recovery reattaches");
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
        result.expect("deadline fallback loop");
        let pane_id = workspace
            .pane_for_terminal(terminal_id)
            .expect("deadline pane");
        assert_ne!(workspace.pane(pane_id).attachment_id(), old_attachment);
        assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Proxy));
        assert_eq!(
            websocket_requests(&mock, "terminal_attach").len(),
            2,
            "the recovery episode must issue one fresh attach for the pane"
        );
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        let terminal_id = "terminal-refused";
        let (mut workspace, _) = live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
        mock.refuse_next_proxy_attach("observer_limit", "too many observers");
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
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
        result.expect("refused fallback loop");
        let pane_id = workspace
            .pane_for_terminal(terminal_id)
            .expect("refused pane");
        assert!(matches!(
            workspace.pane(pane_id).attach_state(),
            AttachState::Detached
        ));
        assert!(!workspace.pane(pane_id).writable());
        assert!(workspace.pane(pane_id).frame_source().is_none());
        assert_eq!(
            workspace.pane(pane_id).status_message(),
            Some("observer_limit: too many observers")
        );
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        let terminal_id = "terminal-buffered-finalization";
        let (mut workspace, _) = live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
        mock.finalize_next_proxy_attach_before_reply(
            "epoch-1",
            2,
            "attach_eof",
            "ended before attach result",
        );
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
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
        result.expect("buffered finalization loop");
        let pane_id = workspace
            .pane_for_terminal(terminal_id)
            .expect("buffered finalization pane");
        assert!(matches!(
            workspace.pane(pane_id).attach_state(),
            AttachState::Detached
        ));
        assert_eq!(
            workspace.pane(pane_id).status_message(),
            Some("attach_eof: ended before attach result")
        );
        let forbidden: Vec<_> = mock
            .requests()
            .into_iter()
            .filter_map(|request| request.body)
            .filter(|body| body.get("attachment_id") == Some(&json!("attachment-2")))
            .filter(|body| {
                matches!(
                    body.get("type").and_then(Value::as_str),
                    Some(
                        "terminal_detach"
                            | "terminal_set_viewport"
                            | "terminal_take_control"
                            | "terminal_release_control"
                    )
                )
            })
            .collect();
        assert!(
            forbidden.is_empty(),
            "retired attach requests: {forbidden:?}"
        );
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn live_resize_propagates_geometry_by_policy() {
    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        for _ in 0..2 {
            mock.enqueue(
                "GET",
                "/api/terminals?",
                200,
                json!({
                    "items": [
                        {"terminal_id": "term-controlled", "backend": "native", "state": "live"},
                        {"terminal_id": "term-observed", "backend": "native", "state": "live"},
                        {"terminal_id": "term-tmux", "backend": "tmux", "state": "live"}
                    ],
                    "next_cursor": null,
                    "snapshot": {"daemon_epoch": "epoch-resize", "seq": 1}
                }),
            );
        }
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect live daemon");
        let mut workspace = Workspace::live(daemon);
        workspace.select_project("project-1");
        workspace
            .reconcile_subscribe_first()
            .await
            .expect("initial resize panes");
        let controlled = workspace
            .pane_for_terminal("term-controlled")
            .expect("controlled pane");

        let (client, mut host) = UnixStream::pair().expect("direct resize socket pair");
        let (direct_tx, mut direct_rx) = mpsc::unbounded_channel();
        let host_task = tokio::spawn(async move {
            let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
                .await
                .expect("direct hello");
            write_message_async(
                &mut host,
                &ServerMessage::Welcome {
                    host_epoch: "direct-resize-epoch".into(),
                },
            )
            .await
            .expect("direct welcome");
            let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
                .await
                .expect("direct attach");
            while let Ok(message) = read_message_async(&mut host, MAX_FRAME_SIZE).await {
                if direct_tx.send(message).is_err() {
                    break;
                }
            }
        });
        let direct = UnixSocketFrameSource::connect_stream(
            client,
            &AttachLocator {
                backend: "native".into(),
                frame_host_epoch: "direct-resize-epoch".into(),
                host_terminal_id: "term-controlled".into(),
                frame_socket_path: "socket-pair".into(),
                pane: None,
            },
            "local-token",
            80,
            24,
        )
        .await
        .expect("direct resize source");
        workspace
            .replace_frame_source(controlled, PaneFrameSource::Direct(direct))
            .expect("install direct resize source");

        let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(8);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
            let mut take_count = 1;
            while websocket_requests(&mock, "terminal_take_control")
                .last()
                .and_then(|request| request.get("terminal_id"))
                != Some(&json!("term-controlled"))
            {
                send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
                send_key(&input_tx, KeyCode::Tab, KeyModifiers::NONE).await;
                take_count += 1;
                assert!(take_count <= 3, "controlled pane must be in the live grid");
                wait_for_websocket_requests(&mock, "terminal_take_control", take_count).await;
            }
            assert_eq!(
                websocket_requests(&mock, "terminal_take_control")
                    .last()
                    .and_then(|request| request.get("terminal_id")),
                Some(&json!("term-controlled"))
            );
            wait_for_websocket_requests(&mock, "terminal_resize", 3).await;
            let viewports_before = websocket_requests(&mock, "terminal_set_viewport").len();
            let resizes_before = websocket_requests(&mock, "terminal_resize").len();
            send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
            // The key lands on the pane's own frame socket, never on the
            // daemon (#22573); waiting for it drives one loop iteration, and
            // the resize that preceded it may arrive on the same socket first.
            let mut direct_viewport = None;
            timeout(Duration::from_secs(1), async {
                loop {
                    match direct_rx.recv().await {
                        Some(ClientMessage::Input { data }) => {
                            assert_eq!(data, b"x");
                            break;
                        }
                        Some(ClientMessage::SetViewport { rows, cols }) => {
                            direct_viewport = Some((rows, cols));
                        }
                        Some(_) => {}
                        None => panic!("direct host closed before the key"),
                    }
                }
            })
            .await
            .expect("the direct host receives the key");
            assert!(
                websocket_requests(&mock, "terminal_input").is_empty(),
                "a direct pane's keystrokes never reach the daemon"
            );
            assert_eq!(
                websocket_requests(&mock, "terminal_set_viewport").len(),
                viewports_before,
                "an iteration at an unchanged geometry resends no viewport"
            );
            assert_eq!(
                websocket_requests(&mock, "terminal_resize").len(),
                resizes_before,
                "an iteration at an unchanged geometry resends no size claim"
            );
            let direct_viewport = match direct_viewport {
                Some(viewport) => viewport,
                None => timeout(Duration::from_secs(1), async {
                    loop {
                        if let Some(ClientMessage::SetViewport { rows, cols }) =
                            direct_rx.recv().await
                        {
                            break (rows, cols);
                        }
                    }
                })
                .await
                .expect("direct viewport after resize"),
            };
            drop(input_tx);
            direct_viewport
        };

        let mut switch = TerminalGuard::recording().0;
        let (result, direct_viewport) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
            driver
        );
        result.expect("live resize loop");
        host_task.abort();
        assert!(host_task
            .await
            .expect_err("direct resize host remains attached")
            .is_cancelled());
        assert_eq!(
            workspace.pane(controlled).viewport(),
            direct_viewport,
            "direct source receives the recomputed rectangle"
        );
        let viewports = websocket_requests(&mock, "terminal_set_viewport");
        for terminal_id in ["term-observed", "term-tmux"] {
            let pane_id = workspace
                .pane_for_terminal(terminal_id)
                .expect("proxy resize pane");
            let latest = viewports
                .iter()
                .rev()
                .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
                .expect("latest proxy viewport");
            assert_eq!(
                latest.get("rows").and_then(Value::as_u64),
                Some(u64::from(workspace.pane(pane_id).viewport().0))
            );
            assert_eq!(
                latest.get("cols").and_then(Value::as_u64),
                Some(u64::from(workspace.pane(pane_id).viewport().1))
            );
        }
        let resizes = websocket_requests(&mock, "terminal_resize");
        for terminal_id in ["term-controlled", "term-observed", "term-tmux"] {
            let pane_id = workspace
                .pane_for_terminal(terminal_id)
                .expect("resized pane");
            let (rows, cols) = workspace.pane(pane_id).viewport();
            let latest = resizes
                .iter()
                .rev()
                .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
                .unwrap_or_else(|| panic!("{terminal_id} claimed no size"));
            assert_eq!(latest.get("viewer"), Some(&json!("gclient")));
            assert_eq!(
                latest.get("rows").and_then(Value::as_u64),
                Some(u64::from(rows))
            );
            assert_eq!(
                latest.get("cols").and_then(Value::as_u64),
                Some(u64::from(cols))
            );
        }
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "term-zero", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-zero", "seq": 1}
            }),
        );
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect zero-size daemon");
        let mut workspace = Workspace::live(daemon);
        let _home = pin_tabs(&mock, &mut workspace, "project-1", &["term-zero"]);
        let mut terminal = Terminal::new(TestBackend::new(1, 1)).expect("test terminal");
        terminal.backend_mut().resize(0, 0);
        let mut chrome = Chrome::dark();
        let (input_tx, input_rx) = mpsc::channel(8);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
            settle_live_event().await;
            let before = websocket_requests(&mock, "terminal_set_viewport");
            send_resize_burst(1);
            settle_live_event().await;
            assert_eq!(websocket_requests(&mock, "terminal_set_viewport"), before);
            assert!(websocket_requests(&mock, "terminal_resize").is_empty());
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
        result.expect("zero-size resize loop");
        mock.shutdown().await;
    }
}

/// A terminal host that speaks the frame protocol over a real Unix socket, the
/// way gterm does. `received` is every client message after the handshake, and
/// `to_client` injects server messages such as `InputRefused`.
struct DirectHost {
    socket_dir: tempfile::TempDir,
    socket_path: std::path::PathBuf,
    host_epoch: String,
    received: mpsc::UnboundedReceiver<ClientMessage>,
    to_client: mpsc::UnboundedSender<ServerMessage>,
    task: tokio::task::JoinHandle<()>,
}

impl DirectHost {
    /// Listen on a fresh socket and answer one attach with `host_epoch`.
    async fn start(host_epoch: &str) -> Self {
        let socket_dir = tempfile::tempdir().expect("direct socket dir");
        let socket_path = socket_dir.path().join("frames.sock");
        let listener =
            tokio::net::UnixListener::bind(&socket_path).expect("bind direct frame socket");
        let (received_tx, received) = mpsc::unbounded_channel();
        let (to_client, mut outbound) = mpsc::unbounded_channel::<ServerMessage>();
        let epoch = host_epoch.to_string();
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("direct client");
            let _: ClientMessage = read_message_async(&mut stream, MAX_FRAME_SIZE)
                .await
                .expect("direct hello");
            write_message_async(
                &mut stream,
                &ServerMessage::Welcome {
                    host_epoch: epoch.clone(),
                },
            )
            .await
            .expect("direct welcome");
            let _: ClientMessage = read_message_async(&mut stream, MAX_FRAME_SIZE)
                .await
                .expect("direct attach");
            loop {
                tokio::select! {
                    message = read_message_async(&mut stream, MAX_FRAME_SIZE) => {
                        match message {
                            Ok(message) => {
                                if received_tx.send(message).is_err() {
                                    break;
                                }
                            }
                            Err(_) => break,
                        }
                    }
                    outgoing = outbound.recv() => {
                        let Some(outgoing) = outgoing else { break };
                        if write_message_async(&mut stream, &outgoing).await.is_err() {
                            break;
                        }
                    }
                }
            }
        });
        Self {
            socket_dir,
            socket_path,
            host_epoch: host_epoch.to_string(),
            received,
            to_client,
            task,
        }
    }

    /// The roster `attach` block that tells gclient this terminal has a host
    /// socket, so the attach asks for direct frames before proxy frames.
    fn roster_attach(&self, terminal_id: &str) -> Value {
        json!({
            "backend": "native",
            "frame_host_epoch": self.host_epoch,
            "host_socket": self.socket_path.to_string_lossy(),
            "host_terminal_id": terminal_id,
        })
    }

    /// The `direct` locator the daemon returns with a direct attach result.
    fn attach_locator(&self, terminal_id: &str) -> Value {
        json!({
            "host_epoch": self.host_epoch,
            "host_terminal_id": terminal_id,
            "frame_socket_path": self.socket_path.to_string_lossy(),
            "pane": null,
        })
    }

    /// Every client message the host has received so far, without waiting.
    fn drain(&mut self) -> Vec<ClientMessage> {
        let mut messages = Vec::new();
        while let Ok(message) = self.received.try_recv() {
            messages.push(message);
        }
        messages
    }

    /// Collect client messages until `predicate` accepts the batch.
    async fn wait_for(
        &mut self,
        what: &str,
        mut predicate: impl FnMut(&[ClientMessage]) -> bool,
    ) -> Vec<ClientMessage> {
        let mut seen = Vec::new();
        timeout(Duration::from_secs(5), async {
            loop {
                if predicate(&seen) {
                    return;
                }
                let Some(message) = self.received.recv().await else {
                    panic!("direct host closed before {what}");
                };
                seen.push(message);
            }
        })
        .await
        .unwrap_or_else(|_| panic!("direct host never received {what}: {seen:?}"));
        seen
    }

    async fn shutdown(self) {
        drop(self.to_client);
        self.task.abort();
        let _ = self.task.await;
        drop(self.socket_dir);
    }
}

/// A live workspace whose single native pane is attached over `host`'s real
/// frame socket, so `Pane::transport()` is `Direct` and the pane types on that
/// socket instead of the daemon (#22573). Keep the returned home alive.
async fn live_workspace_on_direct_host(
    mock: &MockDaemon,
    host: &DirectHost,
    terminal_id: &str,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    let home = tempfile::tempdir().expect("gobby home");
    std::fs::write(
        home.path()
            .join(gobby_core::local_token::LOCAL_CLI_TOKEN_FILENAME),
        "local-token\n",
    )
    .expect("write local cli token");
    mock.serve_direct_attach(host.attach_locator(terminal_id));
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{
                    "terminal_id": terminal_id,
                    "backend": "native",
                    "state": "live",
                    "attach": host.roster_attach(terminal_id),
                }],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install the direct attachment");
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Direct),
        "the roster advertised a host socket, so the attach must be direct"
    );
    (workspace, home)
}

#[tokio::test(flavor = "current_thread")]
async fn direct_pane_keys_reach_the_host_not_the_daemon() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-direct";
    let mut host = DirectHost::start("epoch-direct").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        // Startup focus takes the lease, and the mock's grant carries the
        // host's input grant with it.
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        send_key(&input_tx, KeyCode::Char('y'), KeyModifiers::NONE).await;
        let typed = host
            .wait_for("both keys", |seen| {
                seen.iter()
                    .filter(|message| matches!(message, ClientMessage::Input { .. }))
                    .count()
                    >= 2
            })
            .await;
        assert!(
            websocket_requests(&mock, "terminal_input").is_empty(),
            "a direct pane's keystrokes never reach the daemon"
        );
        drop(input_tx);
        typed
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, typed) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("direct typing loop");

    let binds: Vec<&ClientMessage> = typed
        .iter()
        .filter(|message| matches!(message, ClientMessage::BindAttachment { .. }))
        .collect();
    assert_eq!(binds.len(), 1, "one bind per installed source: {typed:?}");
    let attachment_id = workspace.pane(pane_id).attachment_id().to_string();
    assert!(
        matches!(binds[0], ClientMessage::BindAttachment { attachment_id: bound } if *bound == attachment_id),
        "the bind names the pane's attachment: {:?}",
        binds[0]
    );
    let keys: Vec<Vec<u8>> = typed
        .iter()
        .filter_map(|message| match message {
            ClientMessage::Input { data } => Some(data.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(keys, vec![b"x".to_vec(), b"y".to_vec()]);
    assert!(
        typed
            .iter()
            .position(|message| matches!(message, ClientMessage::BindAttachment { .. }))
            < typed
                .iter()
                .position(|message| matches!(message, ClientMessage::Input { .. })),
        "the bind precedes the first key: {typed:?}"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "no keystroke reached the daemon"
    );
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn a_granted_lease_without_a_host_grant_offers_take_back() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-ungranted";
    let mut host = DirectHost::start("epoch-ungranted").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("ungranted pane");
    // The daemon hands out the writer lease and the terminal host never got the
    // matching input grant. Twice: once for the startup focus, once for the
    // take-back the keystroke below asks for.
    mock.enqueue_take_control_reply_without_host_grant(1);
    mock.enqueue_take_control_reply_without_host_grant(1);
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        settle_live_event().await;
        // Typing asks for the take-back the pane offered, and the host grant
        // is still missing, so there is still nowhere to type.
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
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
    result.expect("ungranted loop");

    let pane = workspace.pane(pane_id);
    assert!(pane.is_observe(), "an ungranted pane cannot hold control");
    assert!(pane.has_take_back(), "it offers take-back instead");
    assert_eq!(pane.status_message(), Some(HOST_GRANT_UNAVAILABLE));
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "gclient never falls back to daemon-mediated keys"
    );
    assert!(
        host.drain()
            .iter()
            .all(|message| !matches!(message, ClientMessage::Input { .. })),
        "and it types nothing at the host either"
    );
    assert!(
        chrome
            .alert_log
            .iter()
            .any(|toast| toast.title.contains(HOST_GRANT_UNAVAILABLE)),
        "the refusal is visible: {:?}",
        chrome.alert_log
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test(flavor = "current_thread")]
async fn an_input_refusal_returns_the_pane_to_observing_and_keeps_the_stream() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-refused-input";
    let mut host = DirectHost::start("epoch-refused-input").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("refused pane");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        host.wait_for("the key", |seen| {
            seen.iter()
                .any(|message| matches!(message, ClientMessage::Input { .. }))
        })
        .await;
        // The daemon moved the grant to a peer, so the host refuses the next
        // write while the frame stream keeps running.
        host.to_client
            .send(ServerMessage::InputRefused {
                code: "input_not_granted".into(),
            })
            .expect("send refusal");
        host.to_client
            .send(semantic_frame("still streaming"))
            .expect("send frame");
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
    result.expect("input refusal loop");

    let pane = workspace.pane(pane_id);
    assert!(pane.is_observe(), "a refused pane stops holding control");
    assert!(pane.has_take_back());
    assert_eq!(
        pane.status_message(),
        Some("terminal refused input (input_not_granted); take control again")
    );
    assert_eq!(
        pane.transport(),
        Some(Transport::Direct),
        "the refusal is not a transport failure"
    );
    assert!(
        pane.frame_source().is_some(),
        "and the frame stream survives it"
    );
    assert!(pane.frames_rendered() > 0, "frames still arrive");
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn resize_geometry_policy_reducer() {
    let mut ws = Workspace::scripted();
    let controlled_native = ws
        .open_terminal("term-controlled", "native", "epoch-resize")
        .expect("controlled native pane");
    ws.force_held(controlled_native);
    let observed_proxy = ws
        .open_terminal("term-observed", "native", "epoch-resize")
        .expect("observed native pane");
    ws.reattach_frames(observed_proxy)
        .expect("proxy frame source");
    let controlled_tmux = ws
        .open_terminal("term-tmux", "tmux", "epoch-resize")
        .expect("tmux pane");
    ws.force_held(controlled_tmux);

    ws.propagate_geometry(&[
        (controlled_native, 0, 0),
        (controlled_native, 28, 96),
        (controlled_native, 30, 100),
        (observed_proxy, 20, 70),
        (controlled_tmux, 15, 60),
    ])
    .await
    .expect("propagate geometry");

    assert_eq!(ws.pane(controlled_native).viewport(), (30, 100));
    assert_eq!(ws.pane(observed_proxy).viewport(), (20, 70));
    assert_eq!(ws.pane(controlled_tmux).viewport(), (15, 60));
    for pane in [controlled_native, observed_proxy, controlled_tmux] {
        let (rows, cols) = ws.pane(pane).viewport();
        assert!(matches!(
            ws.pane(pane)
                .scripted_source()
                .and_then(|source| source.last_client_message()),
            Some(gobby_terminal::protocol::ClientMessage::SetViewport {
                rows: sent_rows,
                cols: sent_cols,
            }) if sent_rows == rows && sent_cols == cols
        ));
    }
    let resizes: Vec<_> = ws
        .daemon()
        .ws_sent()
        .into_iter()
        .filter(|message| {
            message.get("type").and_then(serde_json::Value::as_str) == Some("terminal_resize")
        })
        .collect();
    assert_eq!(
        resizes.len(),
        3,
        "every live pane claims its size for the gclient viewer"
    );
    for (pane, terminal_id) in [
        (controlled_native, "term-controlled"),
        (observed_proxy, "term-observed"),
        (controlled_tmux, "term-tmux"),
    ] {
        let (rows, cols) = ws.pane(pane).viewport();
        let resize = resizes
            .iter()
            .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
            .unwrap_or_else(|| panic!("{terminal_id} claimed no size"));
        assert_eq!(resize.get("viewer"), Some(&json!("gclient")));
        assert_eq!(resize.get("rows"), Some(&json!(rows)));
        assert_eq!(resize.get("cols"), Some(&json!(cols)));
    }
}

#[tokio::test]
async fn select_spawn_attach_terminate_loop() {
    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-survivor", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-spawn", "seq": 1}
            }),
        );
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-survivor", "backend": "native", "state": "live"},
                    {"terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-spawn", "seq": 1}
            }),
        );
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-survivor", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-spawn", "seq": 3}
            }),
        );
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect live daemon");
        let mut workspace = Workspace::live(daemon);
        let _home = pin_tabs(
            &mock,
            &mut workspace,
            "project-selected",
            &["terminal-survivor"],
        );
        let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
        let mut chrome = Chrome::dark();
        // `new_terminal` ships without a chord (`prefix+shift+n` adds a
        // project); the loop under test spawns through an override.
        chrome.keymap =
            Keymap::from_toml("[bindings]\nnew_terminal = \"prefix+i\"\n", HERDR_PREFIX)
                .expect("test keymap");
        let (input_tx, input_rx) = mpsc::channel(32);

        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
            let survivor_attachment = websocket_requests(&mock, "terminal_set_viewport")[0]
                .get("attachment_id")
                .and_then(Value::as_str)
                .expect("survivor attachment")
                .to_string();
            mock.send_event_and_wait(json!({
                "type": "terminal_frame",
                "terminal_id": "terminal-survivor",
                "attachment_id": survivor_attachment,
                "encoding": "bincode-b64",
                "payload": encoded_frame("survivor-before"),
            }))
            .await;

            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('i'), KeyModifiers::NONE).await;
            wait_for_websocket_requests(&mock, "terminal_create", 1).await;
            // The survivor's viewport is claimed again as its slot shrinks,
            // so the spawned pane's claim is found by terminal, not by count.
            let spawned_attachment = timeout(Duration::from_secs(1), async {
                loop {
                    let found = websocket_requests(&mock, "terminal_set_viewport")
                        .into_iter()
                        .find(|request| {
                            request.get("terminal_id")
                                == Some(&json!("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
                        })
                        .and_then(|request| {
                            request
                                .get("attachment_id")
                                .and_then(Value::as_str)
                                .map(str::to_string)
                        });
                    if let Some(found) = found {
                        break found;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("spawned attachment");
            // The create reply and relist have already attached the pane. Delivering the
            // lifecycle afterward, twice, must remain idempotent.
            mock.send_event_and_wait(json!({
                "type": "terminal_event",
                "event": "created",
                "daemon_epoch": "epoch-spawn",
                "seq": 2,
                "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "terminal": {
                    "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "backend": "native",
                    "state": "live"
                }
            }))
            .await;
            mock.send_event_and_wait(json!({
                "type": "terminal_event",
                "event": "created",
                "daemon_epoch": "epoch-spawn",
                "seq": 3,
                "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "terminal": {
                    "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "backend": "native",
                    "state": "live"
                }
            }))
            .await;
            mock.send_event_and_wait(json!({
                "type": "terminal_frame",
                "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "attachment_id": spawned_attachment,
                "encoding": "bincode-b64",
                "payload": encoded_frame("spawned-first"),
            }))
            .await;
            mock.send_event_and_wait(json!({
                "type": "terminal_frame",
                "terminal_id": "terminal-survivor",
                "attachment_id": survivor_attachment,
                "encoding": "bincode-b64",
                "payload": encoded_frame("survivor-during"),
            }))
            .await;
            settle_live_event().await;

            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('2'), KeyModifiers::NONE).await;
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('D'), KeyModifiers::SHIFT).await;
            wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
            // Suppress the killed lifecycle: the post-reply relist must retire it.
            wait_for_http_requests(&mock, "GET", "/api/terminals?", 3).await;
            mock.send_event_and_wait(json!({
                "type": "terminal_frame",
                "terminal_id": "terminal-survivor",
                "attachment_id": survivor_attachment,
                "encoding": "bincode-b64",
                "payload": encoded_frame("survivor-after"),
            }))
            .await;
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
        result.expect("spawn and terminate live loop");
        assert_eq!(websocket_requests(&mock, "terminal_create").len(), 1);
        assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 2);
        let kills = websocket_requests(&mock, "terminal_kill");
        assert_eq!(kills.len(), 1);
        assert_eq!(
            kills[0].get("terminal_id"),
            Some(&json!("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
        );
        assert!(
            workspace
                .pane_for_terminal("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
                .is_none(),
            "lifecycle removal must retire the spawned pane"
        );
        assert_eq!(
            workspace.pane_count(),
            1,
            "late/duplicate create and lost kill converge"
        );
        let survivor = workspace
            .pane_for_terminal("terminal-survivor")
            .expect("streaming survivor");
        assert!(workspace.pane(survivor).frames_rendered() >= 3);
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        mock.enqueue_spawn_events_before_reply(vec![json!({
            "type": "terminal_event",
            "event": "created",
            "daemon_epoch": "epoch-early",
            "seq": 2,
            "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "terminal": {
                "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "backend": "native",
                "state": "live"
            }
        })]);
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-early", "seq": 1}
            }),
        );
        for _ in 0..2 {
            mock.enqueue(
                "GET",
                "/api/terminals?",
                200,
                json!({
                    "items": [
                        {"terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "backend": "native", "state": "live"}
                    ],
                    "next_cursor": null,
                    "snapshot": {"daemon_epoch": "epoch-early", "seq": 2}
                }),
            );
        }
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect live daemon");
        let mut workspace = Workspace::live(daemon);
        workspace.select_project("project-selected");
        let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
        let mut chrome = Chrome::dark();
        // `new_terminal` ships without a chord (`prefix+shift+n` adds a
        // project); the loop under test spawns through an override.
        chrome.keymap =
            Keymap::from_toml("[bindings]\nnew_terminal = \"prefix+i\"\n", HERDR_PREFIX)
                .expect("test keymap");
        let (input_tx, input_rx) = mpsc::channel(16);

        let driver = async {
            settle_live_event().await;
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('i'), KeyModifiers::NONE).await;
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
            assert_eq!(
                websocket_requests(&mock, "terminal_attach")
                    .into_iter()
                    .filter(|request| {
                        request.get("terminal_id")
                            == Some(&json!("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
                    })
                    .count(),
                1,
                "event-before-reply must attach the spawned terminal once"
            );

            mock.drop_websockets();
            timeout(Duration::from_secs(1), async {
                while mock.websocket_handshakes() < 2 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("spawned panes reconnect");
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;

            let viewports = websocket_requests(&mock, "terminal_set_viewport");
            let terminal_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
            let attachment_id = viewports
                .iter()
                .rev()
                .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
                .and_then(|request| request.get("attachment_id"))
                .and_then(Value::as_str)
                .expect("fresh attachment after reconnect");
            mock.send_event_and_wait(json!({
                "type": "terminal_frame",
                "terminal_id": terminal_id,
                "attachment_id": attachment_id,
                "encoding": "bincode-b64",
                "payload": encoded_frame("spawned-reconnected"),
            }))
            .await;
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
        result.expect("event-before-reply and reconnect live loop");
        assert_eq!(workspace.pane_count(), 1);
        assert!(workspace
            .pane_for_terminal("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            .is_some());
        assert_eq!(
            websocket_requests(&mock, "terminal_attach")
                .into_iter()
                .filter(|request| {
                    request.get("terminal_id")
                        == Some(&json!("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
                })
                .count(),
            2,
            "the spawned terminal attaches once per connection generation"
        );
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.set_spawn_refusal("capacity exhausted");
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-refused", "seq": 1}
            }),
        );
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect live daemon");
        let mut workspace = Workspace::live(daemon);
        workspace.select_project("project-selected");
        let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
        let mut chrome = Chrome::dark();
        // `new_terminal` ships without a chord (`prefix+shift+n` adds a
        // project); the loop under test spawns through an override.
        chrome.keymap =
            Keymap::from_toml("[bindings]\nnew_terminal = \"prefix+i\"\n", HERDR_PREFIX)
                .expect("test keymap");
        let (input_tx, input_rx) = mpsc::channel(8);
        let driver = async {
            settle_live_event().await;
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('i'), KeyModifiers::NONE).await;
            wait_for_websocket_requests(&mock, "terminal_create", 1).await;
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
        result.expect("spawn refusal live loop");
        assert_eq!(workspace.pane_count(), 0);
        assert!(websocket_requests(&mock, "terminal_attach").is_empty());
        assert!(chrome
            .last_alert()
            .is_some_and(|message| message.contains("capacity exhausted")));
        mock.shutdown().await;
    }
}

#[test]
fn spawn_attach_terminate_reducer_schedules_converge() {
    let mut ws = Workspace::scripted();
    ws.select_project("proj-1");
    assert_eq!(ws.project_id(), Some("proj-1"));

    // Reply before event: the reply records intent, but cannot invent a pane.
    ws.daemon_mut().set_spawn_response(json!({
        "success": true,
        "run_id": "run-1",
        "terminal_id": "term-spawn"
    }));
    ws.spawn_agent(json!({
        "task_id": "task-1",
        "agent_name": "default"
    }))
    .expect("spawn accepted");
    assert!(ws.pane_for_terminal("term-spawn").is_none());
    let spawn_body = ws.daemon().last_spawn_body();
    assert!(
        spawn_body.get("backend").is_none(),
        "backend selection is 4.2: {spawn_body}"
    );

    let created = json!({
        "type": "terminal_event",
        "event": "created",
        "terminal_id": "term-spawn",
        "terminal": {
            "terminal_id": "term-spawn",
            "backend": "native",
            "state": "live"
        }
    });
    ws.apply_ws(&created).expect("created lifecycle");
    let spawned = ws.pane_for_terminal("term-spawn").expect("reconciled pane");
    let pane_count = ws.pane_count();
    ws.apply_ws(&created).expect("duplicate created lifecycle");
    assert_eq!(ws.pane_count(), pane_count, "created is idempotent by id");

    ws.terminate_terminal("term-spawn").expect("terminate");
    ws.apply_ws(&json!({
        "type": "terminal_kill_result",
        "terminal_id": "term-spawn",
        "success": true
    }))
    .expect("kill result");
    assert!(ws.pane(spawned).is_terminating());
    assert!(!ws.pane(spawned).writable());
    ws.apply_ws(&json!({
        "type": "terminal_event",
        "event": "killed",
        "terminal_id": "term-spawn"
    }))
    .expect("killed lifecycle");
    assert!(ws.pane_for_terminal("term-spawn").is_none());

    // Event before reply converges through the same idempotent path.
    ws.apply_ws(&json!({
        "type": "terminal_event",
        "event": "created",
        "terminal_id": "term-early",
        "terminal": {
            "terminal_id": "term-early",
            "backend": "native",
            "state": "live"
        }
    }))
    .expect("early created lifecycle");
    let early_count = ws.pane_count();
    ws.daemon_mut().set_spawn_response(json!({
        "success": true,
        "terminal_id": "term-early"
    }));
    ws.spawn_agent(json!({
        "task_id": "task-1",
        "agent_name": "default"
    }))
    .expect("spawn reply after event");
    assert_eq!(ws.pane_count(), early_count, "spawn reply is idempotent");

    // A refusal is terminal and visible, with no pane side effect.
    ws.daemon_mut().set_spawn_response(json!({
        "success": false,
        "reason": "capacity exhausted"
    }));
    let refused = ws.spawn_agent(json!({"task_id": "task-refused"}));
    assert!(
        refused.is_err(),
        "spawn refusal must create no pane: {refused:?}"
    );
    assert!(ws
        .status_message()
        .is_some_and(|message| message.contains("capacity exhausted")));

    // A lost create event is recovered by listing; a lost kill event is too.
    ws.daemon_mut().set_spawn_response(json!({
        "success": true,
        "terminal_id": "term-listed"
    }));
    ws.spawn_agent(json!({"task_id": "task-listed"}))
        .expect("spawn awaiting listing");
    ws.daemon_mut().set_terminal_pages(vec![json!({
        "items": [{
            "terminal_id": "term-listed",
            "backend": "native",
            "state": "live"
        }],
        "next_cursor": null
    })]);
    ws.fetch_roster().expect("listing recovers create");
    let listed = ws
        .pane_for_terminal("term-listed")
        .expect("listed pane reconciled");
    ws.terminate_terminal("term-listed")
        .expect("terminate listed");
    ws.apply_ws(&json!({
        "type": "terminal_kill_result",
        "terminal_id": "term-listed",
        "success": true
    }))
    .expect("kill reply before lost event");
    assert!(ws.pane(listed).is_terminating());
    ws.daemon_mut().set_terminal_pages(vec![json!({
        "items": [],
        "next_cursor": null
    })]);
    ws.fetch_roster().expect("listing recovers kill");
    assert!(ws.pane_for_terminal("term-listed").is_none());
}

#[tokio::test]
async fn reconnect_supervisor_counts_delays_resets_and_cancels() {
    tokio::time::pause();
    let unavailable = || DaemonError::Unavailable { retry_after: None };
    let daemon = ReconnectDaemon::new((0..5).map(|_| Err(unavailable())));
    let mut supervisor = ReconnectSupervisor::new();
    let waiter = supervisor.request(Generation(7));
    let mut elapsed = Vec::new();

    // Five failures in a row: the ladder's last delay repeats past its end
    // instead of giving up.
    for attempt in 0..5 {
        let started = Instant::now();
        let outcome = supervisor.attempt_when_due(&daemon).await;
        elapsed.push(Instant::now() - started);
        assert_eq!(
            outcome,
            ReconnectAttempt::RetryScheduled {
                delay: [250, 500, 1_000, 2_000, 2_000].map(Duration::from_millis)[attempt]
            }
        );
    }

    for (actual, expected) in elapsed
        .into_iter()
        .zip([0, 250, 500, 1_000, 2_000].map(Duration::from_millis))
    {
        assert!(actual >= expected && actual <= expected + Duration::from_millis(1));
    }
    assert_eq!(daemon.calls(), vec![Generation(7); 5]);
    assert_eq!(supervisor.attempt_count(), 5);
    assert!(
        supervisor.next_attempt_at().is_some(),
        "a sixth attempt is scheduled; the episode never exhausts"
    );

    let clamped = ReconnectDaemon::new([
        Err(DaemonError::Unavailable {
            retry_after: Some(Duration::from_millis(1)),
        }),
        Err(DaemonError::Unavailable {
            retry_after: Some(Duration::from_secs(30)),
        }),
    ]);
    let cancelled = supervisor.request(Generation(8));
    assert_eq!(
        supervisor.attempt_when_due(&clamped).await,
        ReconnectAttempt::RetryScheduled {
            delay: Duration::from_millis(250)
        }
    );
    let started = Instant::now();
    assert_eq!(
        supervisor.attempt_when_due(&clamped).await,
        ReconnectAttempt::RetryScheduled {
            delay: Duration::from_secs(4)
        }
    );
    assert!((Duration::from_millis(250)..=Duration::from_millis(251))
        .contains(&(Instant::now() - started)));
    supervisor.cancel(DaemonError::Protocol {
        detail: "quit".to_string(),
    });
    assert!(matches!(
        cancelled.await.expect("cancelled waiter"),
        Err(DaemonError::Protocol { detail }) if detail == "quit"
    ));
    assert!(
        matches!(
            waiter.await.expect("first waiter"),
            Err(DaemonError::Protocol { detail }) if detail == "quit"
        ),
        "the first loss's waiter settles only when the episode is cancelled"
    );

    let reset = ReconnectDaemon::new([Ok(Generation(10)), Ok(Generation(11))]);
    let first = supervisor.request(Generation(9));
    assert_eq!(
        supervisor.attempt_when_due(&reset).await,
        ReconnectAttempt::Reconnected(Generation(10))
    );
    assert_eq!(supervisor.attempt_count(), 1);
    assert_eq!(
        supervisor.handshake_failed(unavailable()),
        ReconnectAttempt::RetryScheduled {
            delay: Duration::from_millis(250)
        }
    );
    assert_eq!(
        supervisor.attempt_when_due(&reset).await,
        ReconnectAttempt::Reconnected(Generation(11))
    );
    supervisor.handshake_complete(Generation(11));
    assert_eq!(first.await.expect("completed waiter"), Ok(Generation(11)));
    assert_eq!(supervisor.attempt_count(), 0);
}

#[tokio::test]
async fn latched_exit_issues_no_further_requests() {
    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        for _ in 0..2 {
            mock.enqueue(
                "GET",
                "/api/terminals?",
                200,
                json!({
                    "items": [{"terminal_id": "terminal-reconnect-exit", "backend": "native", "state": "live"}],
                    "next_cursor": null,
                    "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
                }),
            );
        }
        let daemon = LiveDaemon::connect(mock.url(), "local-token")
            .await
            .expect("connect live daemon");
        let mut workspace = Workspace::live(daemon);
        workspace.select_project("project-1");
        workspace
            .reconcile_subscribe_first()
            .await
            .expect("initial reconnect pane");
        let pane_id = workspace
            .pane_for_terminal("terminal-reconnect-exit")
            .expect("reconnect pane");
        let attachment = workspace.pane(pane_id).attachment_id().to_string();
        let reconnect_gate = mock.pause_next_websocket();
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);

        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
            mock.drop_websockets();
            timeout(Duration::from_secs(1), async {
                while mock.websocket_handshakes() < 2 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("reconnect attempt starts");
            let before_exit = terminal_side_effects(&mock);
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('Q'), KeyModifiers::SHIFT).await;
            reconnect_gate.notify_waiters();
            before_exit
        };

        let mut switch = TerminalGuard::recording().0;
        let (result, before_exit) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
            driver
        );
        result.expect("exit cancels reconnect");
        assert_eq!(terminal_side_effects(&mock), before_exit);
        assert_eq!(
            workspace
                .pane_for_terminal("terminal-reconnect-exit")
                .map(|id| workspace.pane(id).attachment_id()),
            Some(attachment.as_str())
        );
        mock.shutdown().await;
    }

    {
        let mock = MockDaemon::start("local-token").await;
        mock.use_unique_attachment_ids();
        mock.suppress_ws("terminal_detach");
        let terminal_id = "terminal-fallback-exit";
        let (mut workspace, old_attachment) =
            live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let mut chrome = Chrome::dark();
        show_roster(&workspace, &mut chrome);
        let (input_tx, input_rx) = mpsc::channel(16);

        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
            let before_exit = terminal_side_effects(&mock);
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('Q'), KeyModifiers::SHIFT).await;
            mock.send_event(json!({
                "type": "terminal_attachment_finalized",
                "daemon_epoch": "epoch-1",
                "seq": 2,
                "terminal_id": terminal_id,
                "attachment_id": old_attachment,
                "code": "host_eof",
                "reason": "direct observer ended"
            }));
            mock.send_event(json!({
                "type": "terminal_event",
                "event": "killed",
                "daemon_epoch": "epoch-1",
                "seq": 3,
                "terminal_id": terminal_id
            }));
            settle_live_event().await;
            before_exit
        };

        let mut switch = TerminalGuard::recording().0;
        let (result, before_exit) = tokio::join!(
            run_live_loop(
                &mut workspace,
                &mut terminal,
                &mut chrome,
                input_rx,
                &mut switch
            ),
            driver
        );
        result.expect("exit cancels fallback");
        assert_eq!(
            terminal_side_effects(&mock),
            before_exit,
            "fallback must issue no request after the exit input is queued"
        );
        let pane_id = workspace
            .pane_for_terminal(terminal_id)
            .expect("post-latch lifecycle is inert");
        assert_eq!(workspace.pane(pane_id).attachment_id(), old_attachment);
        mock.shutdown().await;
    }
}

#[tokio::test]
async fn reconnect_episode_rolls_generation_forward() {
    tokio::time::pause();
    let daemon = ReconnectDaemon::new([Ok(Generation(2)), Ok(Generation(3))]);
    let mut supervisor = ReconnectSupervisor::new();
    let first = supervisor.request(Generation(1));
    let first_attempt = supervisor
        .start_due_attempt(daemon.clone())
        .expect("first reconnect attempt");

    let rolled = supervisor.request(Generation(2));
    assert_eq!(
        supervisor.complete_attempt(first_attempt.await),
        ReconnectAttempt::Idle,
        "the completed G1 attempt cannot handshake after G2 was reported dead"
    );
    assert_eq!(
        supervisor
            .next_attempt_at()
            .expect("rolled retry deadline")
            .duration_since(Instant::now()),
        Duration::from_millis(250),
        "generation rollover preserves the episode's delay schedule"
    );
    assert_eq!(
        supervisor.attempt_when_due(&daemon).await,
        ReconnectAttempt::Reconnected(Generation(3))
    );
    assert_eq!(daemon.calls(), vec![Generation(1), Generation(2)]);
    assert_eq!(supervisor.attempt_count(), 2);

    supervisor.handshake_complete(Generation(3));
    assert_eq!(first.await.expect("first waiter"), Ok(Generation(3)));
    assert_eq!(rolled.await.expect("rolled waiter"), Ok(Generation(3)));
    assert_eq!(supervisor.attempt_count(), 0);
}

#[tokio::test]
async fn detach_deadlines_recover_through_the_supervisor() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "term-detach-1", "backend": "native", "state": "live"},
                    {"terminal_id": "term-detach-2", "backend": "native", "state": "live"},
                    {"terminal_id": "term-detach-3", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-detach", "seq": 1}
            }),
        );
    }
    for _ in 0..3 {
        mock.enqueue_detach_reply(false, Some("detach state indeterminate"));
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(
        &mock,
        &mut workspace,
        "project-1",
        &["term-detach-1", "term-detach-2", "term-detach-3"],
    );
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 3).await;
        tokio::time::pause();
        let attachments: Vec<_> = websocket_requests(&mock, "terminal_set_viewport")
            .into_iter()
            .map(|request| {
                (
                    request
                        .get("terminal_id")
                        .and_then(Value::as_str)
                        .expect("viewport terminal")
                        .to_string(),
                    request
                        .get("attachment_id")
                        .and_then(Value::as_str)
                        .expect("viewport attachment")
                        .to_string(),
                )
            })
            .collect();
        let mut seen = std::collections::HashSet::new();
        let attachments: Vec<(String, String)> = attachments
            .into_iter()
            .filter(|(terminal_id, _)| seen.insert(terminal_id.clone()))
            .collect();
        for (index, (terminal_id, attachment_id)) in attachments.iter().enumerate() {
            mock.send_event_and_wait(json!({
                "type": "terminal_frame",
                "terminal_id": terminal_id,
                "attachment_id": attachment_id,
                "encoding": "bincode-b64",
                "payload": "!",
            }))
            .await;
            wait_for_websocket_requests(&mock, "terminal_detach", index + 1).await;
        }
        assert_eq!(mock.websocket_handshakes(), 1);
        assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 3);

        tokio::time::advance(Duration::from_secs(2)).await;
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_attach", 6).await;
        drop(input_tx);
        attachments
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, old_attachments) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("deadline recovery loop");
    assert_eq!(mock.websocket_handshakes(), 2, "one coalesced reconnect");
    let detaches = websocket_requests(&mock, "terminal_detach");
    assert_eq!(detaches.len(), 6);
    assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 6);
    for (terminal_id, old_attachment) in old_attachments {
        let pane_id = workspace
            .pane_for_terminal(&terminal_id)
            .expect("reattached pane");
        let fresh_attachment = workspace.pane(pane_id).attachment_id();
        assert_ne!(fresh_attachment, old_attachment);
        assert!(workspace.pane(pane_id).is_live());
        assert_eq!(
            detaches
                .iter()
                .filter(|request| {
                    request.get("attachment_id").and_then(Value::as_str)
                        == Some(old_attachment.as_str())
                })
                .count(),
            1,
            "recovery detaches each old attachment exactly once"
        );
        assert_eq!(
            detaches
                .iter()
                .filter(|request| {
                    request.get("attachment_id").and_then(Value::as_str) == Some(fresh_attachment)
                })
                .count(),
            1,
            "shutdown detaches each live replacement exactly once"
        );
    }
    mock.shutdown().await;
}

#[tokio::test]
async fn daemon_loss_renders_read_only_until_recovery() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-loss", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-loss", "seq": 1}
            }),
        );
    }
    mock.enqueue(
        "GET",
        "/api/terminals?",
        500,
        json!({"code": "reconcile_failed", "message": "roster unavailable"}),
    );
    mock.enqueue_with_event(
        "GET",
        "/api/terminals?",
        json!({
            "items": [
                {"terminal_id": "terminal-loss", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-loss", "seq": 1}
        }),
        json!({
            "type": "terminal_event",
            "event": "updated",
            "terminal_id": "terminal-loss",
            "daemon_epoch": "epoch-loss",
            "seq": 2,
            "timestamp": "2026-01-01T00:00:00Z"
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial loss pane");
    let pane_id = workspace
        .pane_for_terminal("terminal-loss")
        .expect("loss pane");
    let old_attachment = workspace.pane(pane_id).attachment_id().to_string();

    let (client, mut host) = UnixStream::pair().expect("direct frame socket pair");
    let (frame_tx, mut frame_rx) = mpsc::channel(8);
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("direct hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "direct-loss-epoch".into(),
            },
        )
        .await
        .expect("direct welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("direct attach");
        while let Some(frame) = frame_rx.recv().await {
            write_message_async(&mut host, &frame)
                .await
                .expect("direct frame");
        }
    });
    let direct = UnixSocketFrameSource::connect_stream(
        client,
        &AttachLocator {
            backend: "native".into(),
            frame_host_epoch: "direct-loss-epoch".into(),
            host_terminal_id: "terminal-loss".into(),
            frame_socket_path: "socket-pair".into(),
            pane: None,
        },
        "local-token",
        80,
        24,
    )
    .await
    .expect("direct frame source");
    workspace
        .replace_frame_source(pane_id, PaneFrameSource::Direct(direct))
        .expect("install direct frame source");

    let observed_daemon = workspace.daemon().clone();
    let observed_generation = observed_daemon.generation();
    let (_, mut observed_events) = observed_daemon.subscribe();
    let reconnect_gate = mock.pause_next_websocket();
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        mock.drop_websockets();
        let disconnected_error = timeout(Duration::from_secs(1), async {
            loop {
                match observed_events.recv().await.expect("daemon loss event") {
                    DaemonEvent::Disconnected { generation, error }
                        if generation == observed_generation =>
                    {
                        break error;
                    }
                    _ => {}
                }
            }
        })
        .await
        .expect("initial generation disconnect deadline");
        assert!(matches!(
            disconnected_error,
            DaemonError::Unavailable { retry_after: None }
        ));
        timeout(Duration::from_secs(1), async {
            while mock.websocket_handshakes() < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("paused reconnect attempt");

        let take_count = websocket_requests(&mock, "terminal_take_control").len();
        frame_tx
            .send(semantic_frame("direct-during-loss"))
            .await
            .expect("direct frame during reconnect");
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('t'), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert!(websocket_requests(&mock, "terminal_input").is_empty());
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            take_count,
            "control is suppressed while the daemon is unavailable"
        );

        tokio::time::pause();
        reconnect_gate.notify_waiters();
        for _ in 0..1_024 {
            let roster_reads = mock
                .requests()
                .iter()
                .filter(|request| {
                    request.method == "GET" && request.target.starts_with("/api/terminals?")
                })
                .count();
            if roster_reads >= 3 {
                break;
            }
            tokio::task::yield_now().await;
        }
        assert_eq!(mock.websocket_handshakes(), 2);
        frame_tx
            .send(semantic_frame("direct-during-roster-backoff"))
            .await
            .expect("direct frame during roster backoff");
        send_key(&input_tx, KeyCode::Char('y'), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert!(websocket_requests(&mock, "terminal_input").is_empty());

        tokio::time::advance(Duration::from_millis(250)).await;
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        let mut initial_generation_disconnects = 1;
        while let Ok(event) = observed_events.try_recv() {
            if matches!(
                event,
                DaemonEvent::Disconnected { generation, .. }
                    if generation == observed_generation
            ) {
                initial_generation_disconnects += 1;
            }
        }
        assert_eq!(
            initial_generation_disconnects, 1,
            "one dropped socket emits one disconnect for its generation"
        );
        drop(frame_tx);
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
    result.expect("daemon loss recovery loop");
    host_task.await.expect("direct host task");
    let pane_id = workspace
        .pane_for_terminal("terminal-loss")
        .expect("recovered pane");
    assert!(workspace.pane(pane_id).frames_rendered() >= 2);
    assert_ne!(workspace.pane(pane_id).attachment_id(), old_attachment);
    assert!(workspace.pane(pane_id).writable());
    assert_eq!(mock.websocket_handshakes(), 3);
    let take_attachments: Vec<_> = websocket_requests(&mock, "terminal_take_control")
        .into_iter()
        .filter_map(|request| {
            request
                .get("attachment_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    assert_eq!(take_attachments.len(), 2);
    assert_ne!(take_attachments[0], take_attachments[1]);
    let recovery_activity = mock.activity();
    let relist = recovery_activity
        .iter()
        .rposition(|entry| entry.starts_with("GET /api/terminals?"))
        .expect("recovery roster request");
    let fresh_attach = recovery_activity
        .iter()
        .rposition(|entry| entry == "WS terminal_attach")
        .expect("fresh recovery attachment");
    let fresh_take = recovery_activity
        .iter()
        .rposition(|entry| entry == "WS terminal_take_control")
        .expect("fresh recovery control");
    assert!(
        relist < fresh_attach && fresh_attach < fresh_take,
        "the loop must relist and replay before fresh attachment and control"
    );
    mock.shutdown().await;

    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-outage", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-outage", "seq": 1}
        }),
    );
    for _ in 0..4 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            500,
            json!({"code": "reconcile_failed", "message": "roster unavailable"}),
        );
    }
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-outage", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-outage", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect long-outage daemon");
    mock.fail_next_websocket();
    mock.fail_next_websocket();
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["terminal-outage"]);
    let observed_daemon = workspace.daemon().clone();
    let observed_generation = observed_daemon.generation();
    let (_, mut observed_events) = observed_daemon.subscribe();
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        mock.drop_websockets();
        let disconnected_error = timeout(Duration::from_secs(1), async {
            loop {
                match observed_events.recv().await.expect("budget daemon event") {
                    DaemonEvent::Disconnected { generation, error }
                        if generation == observed_generation =>
                    {
                        break error;
                    }
                    _ => {}
                }
            }
        })
        .await
        .expect("budget disconnect deadline");
        assert!(matches!(
            disconnected_error,
            DaemonError::Unavailable { retry_after: None }
        ));

        timeout(Duration::from_secs(1), async {
            while mock.websocket_handshakes() < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("first socket failure");
        for _ in 0..128 {
            tokio::task::yield_now().await;
        }
        tokio::time::pause();

        // Two socket failures and four roster failures outlast the delay
        // ladder; the seventh attempt finds the daemon back.
        for (expected_handshakes, expected_roster_reads) in
            [(3, 1), (4, 2), (5, 3), (6, 4), (7, 5), (8, 6)]
        {
            for _ in 0..8 {
                let roster_reads = mock
                    .requests()
                    .iter()
                    .filter(|request| {
                        request.method == "GET" && request.target.starts_with("/api/terminals?")
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
            assert_eq!(mock.websocket_handshakes(), expected_handshakes);
            assert_eq!(
                mock.requests()
                    .iter()
                    .filter(|request| {
                        request.method == "GET" && request.target.starts_with("/api/terminals?")
                    })
                    .count(),
                expected_roster_reads
            );
        }

        let mut initial_generation_disconnects = 1;
        while let Ok(event) = observed_events.try_recv() {
            if matches!(
                event,
                DaemonEvent::Disconnected { generation, .. }
                    if generation == observed_generation
            ) {
                initial_generation_disconnects += 1;
            }
        }
        assert_eq!(
            initial_generation_disconnects, 1,
            "the shared retry episode originates from exactly one disconnect"
        );
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
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
    result.expect("a long outage never exits the live loop");
    assert_eq!(
        workspace.exit_reason(),
        Some("terminal input closed"),
        "an unexpected loss must never latch the daemon-unavailable exit"
    );
    assert_eq!(
        mock.websocket_handshakes(),
        8,
        "the client keeps retrying past the delay ladder until the daemon answers"
    );
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|request| {
                request.method == "GET" && request.target.starts_with("/api/terminals?")
            })
            .count(),
        6,
        "one startup roster, four failed reconnect rosters, one recovery roster"
    );
    assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 2);
    assert_eq!(
        websocket_requests(&mock, "terminal_take_control").len(),
        2,
        "the recovered handshake restores writable control"
    );
    let recovered = workspace
        .pane_for_terminal("terminal-outage")
        .expect("recovered outage pane");
    assert!(workspace.pane(recovered).writable());
    mock.shutdown().await;
}

/// #22002: a daemon stop or restart closes the socket with 1001. gclient must
/// keep its panes, retry past the delay ladder, and re-attach to the
/// same terminal ids once the daemon returns; the close frame never exits it.
#[tokio::test]
async fn daemon_restart_keeps_panes_and_reattaches() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..4 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-restart", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-restart", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect restart daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial restart pane");
    let pane_id = workspace
        .pane_for_terminal("terminal-restart")
        .expect("restart pane");
    let old_attachment = workspace.pane(pane_id).attachment_id().to_string();
    let observed_daemon = workspace.daemon().clone();
    let observed_generation = observed_daemon.generation();
    let (_, mut observed_events) = observed_daemon.subscribe();
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);
    // More failed handshakes than the delay ladder has rungs.
    let failed_handshakes = RECONNECT_DELAYS.len() + 2;
    let expected_handshakes = 1 + failed_handshakes + 1;

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        for _ in 0..failed_handshakes {
            mock.fail_next_websocket();
        }
        mock.close_websockets_going_away();
        let disconnected_error = timeout(Duration::from_secs(1), async {
            loop {
                match observed_events.recv().await.expect("restart daemon event") {
                    DaemonEvent::Disconnected { generation, error }
                        if generation == observed_generation =>
                    {
                        break error;
                    }
                    _ => {}
                }
            }
        })
        .await
        .expect("restart disconnect deadline");
        assert!(
            matches!(disconnected_error, DaemonError::GoingAway),
            "the daemon's shutdown close must surface as GoingAway: {disconnected_error:?}"
        );

        tokio::time::pause();
        for _ in 0..64 {
            if mock.websocket_handshakes() >= expected_handshakes
                && websocket_requests(&mock, "terminal_take_control").len() >= 2
            {
                break;
            }
            tokio::time::advance(Duration::from_secs(2)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
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
    result.expect("daemon restart recovery loop");
    assert_eq!(
        workspace.exit_reason(),
        Some("terminal input closed"),
        "a daemon restart must never latch the daemon-unavailable exit"
    );
    assert_eq!(
        mock.websocket_handshakes(),
        expected_handshakes,
        "the client keeps retrying past the delay ladder"
    );
    assert_eq!(
        workspace.pane_for_terminal("terminal-restart"),
        Some(pane_id),
        "the pane survives the restart"
    );
    assert_ne!(workspace.pane(pane_id).attachment_id(), old_attachment);
    assert!(workspace.pane(pane_id).writable());
    let attach_targets: Vec<String> = websocket_requests(&mock, "terminal_attach")
        .into_iter()
        .filter_map(|request| {
            request
                .get("terminal_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    assert_eq!(
        attach_targets,
        vec!["terminal-restart".to_string(); 2],
        "both attachments target the same terminal id"
    );
    assert!(
        websocket_requests(&mock, "workspace_attach").len() >= 2,
        "4.2.6: the reconnect re-attaches the workspace"
    );
    mock.shutdown().await;
}

#[test]
fn daemon_loss_reducer_clears_control_without_frames() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-loss", "native", "epoch-loss")
        .expect("pane");
    ws.force_held(pane);
    let rendered_before = ws.pane(pane).frames_rendered();
    let sent_before = ws.daemon().ws_sent().len();

    ws.observe_daemon_disconnect(
        Generation(1),
        DaemonError::Unavailable { retry_after: None },
    );
    assert!(!ws.pane(pane).writable());
    assert!(
        ws.pane(pane).is_live(),
        "direct attachment stays renderable"
    );
    ws.push_frame(pane, "direct frame after daemon loss");
    assert_eq!(ws.pane(pane).frames_rendered(), rendered_before + 1);
    assert!(ws.send_input(pane, b"blocked").is_err());
    assert!(ws.take_control(pane).is_err());
    assert_eq!(ws.daemon().ws_sent().len(), sent_before);
    assert!(ws
        .pane(pane)
        .status_message()
        .is_some_and(|message| message.contains("unavailable")));
}

#[tokio::test]
async fn control_tombstone_retires_the_attachment() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-tombstone", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-tombstone", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["terminal-tombstone"]);
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);
    let observed_daemon = workspace.daemon().clone();

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let old_attachment = websocket_requests(&mock, "terminal_take_control")[0]
            .get("attachment_id")
            .and_then(Value::as_str)
            .expect("initial control attachment")
            .to_string();

        mock.suppress_ws("terminal_take_control");
        tokio::time::pause();
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('t'), KeyModifiers::NONE).await;
        for _ in 0..1_024 {
            if websocket_requests(&mock, "terminal_take_control").len() >= 2 {
                break;
            }
            tokio::task::yield_now().await;
        }
        let timed_take = websocket_requests(&mock, "terminal_take_control");
        assert_eq!(timed_take.len(), 2);
        assert_eq!(
            timed_take[1].get("attachment_id").and_then(Value::as_str),
            Some(old_attachment.as_str()),
            "the indeterminate request reached the socket on the old scope"
        );
        assert_eq!(observed_daemon.pending_counts().2, 1);

        tokio::time::advance(CONTROL_REQUEST_DEADLINE + Duration::from_millis(1)).await;
        for _ in 0..1_024 {
            if observed_daemon.pending_counts().2 == 0 {
                break;
            }
            tokio::task::yield_now().await;
        }
        assert_eq!(observed_daemon.pending_counts().2, 0);
        assert!(
            websocket_requests(&mock, "terminal_detach").is_empty(),
            "the daemon fences even detach traffic on a tombstoned control scope"
        );
        mock.allow_ws("terminal_take_control");

        tokio::time::advance(Duration::from_secs(2) + RENDER_TICK * 2).await;
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        drop(input_tx);
        old_attachment
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, old_attachment) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("control tombstone recovery loop");
    let pane_id = workspace
        .pane_for_terminal("terminal-tombstone")
        .expect("recovered tombstone pane");
    let fresh_attachment = workspace.pane(pane_id).attachment_id().to_string();
    assert_ne!(fresh_attachment, old_attachment);
    assert!(workspace.pane(pane_id).writable());
    let takes = websocket_requests(&mock, "terminal_take_control");
    assert_eq!(takes.len(), 3);
    assert_eq!(
        takes[2].get("attachment_id").and_then(Value::as_str),
        Some(fresh_attachment.as_str())
    );
    let releases = websocket_requests(&mock, "terminal_release_control");
    assert_eq!(releases.len(), 1);
    assert_eq!(
        releases[0].get("attachment_id").and_then(Value::as_str),
        Some(fresh_attachment.as_str()),
        "shutdown releases only the recovered live attachment"
    );
    mock.shutdown().await;
}

#[test]
fn control_tombstone_reducer_retires_the_attachment() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-tombstone", "native", "epoch-tombstone")
        .expect("pane");
    let old_attachment = ws.pane(pane).attachment_id().to_string();

    let retired = ws
        .retire_indeterminate_control(pane, Instant::now())
        .expect("indeterminate control retires the attachment");
    assert_eq!(retired.0, "term-tombstone");
    assert_eq!(retired.1, old_attachment);
    assert!(!ws.pane(pane).is_live());
    assert!(!ws.pane(pane).writable());

    ws.apply_ws(&json!({
        "type": "terminal_attachment_finalized",
        "attachment_id": old_attachment,
        "reason": "control result indeterminate"
    }))
    .expect("finalize old attachment");
    ws.reattach_frames(pane).expect("fresh attachment");
    let fresh_attachment = ws.pane(pane).attachment_id().to_string();
    assert_ne!(fresh_attachment, old_attachment);
    ws.take_control(pane).expect("take fresh control");
    let take_attachments: Vec<_> = ws
        .daemon()
        .ws_sent()
        .into_iter()
        .filter(|message| message.get("type") == Some(&json!("terminal_take_control")))
        .filter_map(|message| {
            message
                .get("attachment_id")
                .and_then(serde_json::Value::as_str)
                .map(str::to_string)
        })
        .collect();
    assert_eq!(take_attachments, vec![fresh_attachment]);
}

/// The keymap advertises prefix `?` and prefix `s`, so the live loop must act
/// on them.
///
/// Both actions resolved and were then dropped by `handle_live_action`'s
/// wildcard arm, so the keys were recognised and silently ignored in the real
/// client while the help table and settings pane rendered fine under a mode
/// the loop never entered.
#[tokio::test]
async fn prefix_help_and_settings_open_their_modes_in_the_live_loop() {
    for (key, expected) in [
        ('?', gobby_client::ui::chrome::Mode::KeybindHelp),
        ('s', gobby_client::ui::chrome::Mode::Settings),
    ] {
        let mock = MockDaemon::start("local-token").await;
        let (mut workspace, _) =
            live_workspace_with_scripted_direct(&mock, "terminal-help", 1).await;
        let pane = workspace
            .pane_for_terminal("terminal-help")
            .expect("terminal pane");
        let mut chrome = Chrome::dark();
        chrome.open_pane(pane, "loop");
        let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
        let (input_tx, input_rx) = mpsc::channel(256);

        let driver = async move {
            tokio::task::yield_now().await;
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char(key), KeyModifiers::NONE).await;
            tokio::task::yield_now().await;
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
        result.expect("live loop exits cleanly");
        assert_eq!(chrome.mode, expected, "prefix {key} must open {expected:?}");
        mock.shutdown().await;
    }
}

/// The bare keys the default keymap binds to `navigate_*` are ordinary
/// characters and cursor keys inside a focused terminal. They were resolving
/// as chords and dying in the action dispatcher, so `echo GCLIENT-OK` reached
/// the shell as `eco GCLIENT-OK`. Drive the real loop, not just the resolver:
/// the seam was already correct in isolation, and only routing was wrong.
#[tokio::test(start_paused = true)]
async fn bare_navigation_keys_reach_a_focused_terminal() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-typing", "native", "epoch-typing")
        .expect("open terminal");
    // A proxy attachment, so every key lands in the daemon's write log where
    // this test can read it back; a direct pane types on its own frame socket
    // (#22573), which `direct_pane_keys_reach_the_host_not_the_daemon` covers.
    ws.reattach_frames(pane).expect("proxy frame source");
    ws.force_held(pane);

    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "typing");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async move {
        tokio::task::yield_now().await;
        for code in [
            KeyCode::Char('h'),
            KeyCode::Char('j'),
            KeyCode::Char('k'),
            KeyCode::Char('l'),
            KeyCode::Up,
            KeyCode::Down,
        ] {
            input_tx
                .send(RawInputEvent::Key(TerminalKey::new(
                    code,
                    KeyModifiers::NONE,
                )))
                .await
                .expect("typed key");
        }
        tokio::task::yield_now().await;
        drop(input_tx);
    };

    let (result, ()) = tokio::join!(
        run_scripted_loop(&mut ws, &mut terminal, &mut chrome, input_rx),
        driver
    );
    result.expect("loop exits cleanly");

    let typed: String = ws
        .daemon()
        .ws_sent()
        .iter()
        .filter(|message| message.get("type").and_then(Value::as_str) == Some("terminal_input"))
        .filter_map(|message| message.get("data")?.as_str().map(str::to_string))
        .collect();
    assert_eq!(
        typed, "hjkl\u{1b}[A\u{1b}[B",
        "every bare key must reach the pane verbatim"
    );
}

/// 2.1.1: a left click inside a pane that is not focused moves focus and the
/// lease with it (release the old pane, take the new one); alt+click moves
/// focus alone and leaves the pane observed.
#[tokio::test]
async fn pane_click_focuses_and_takes_control_unless_alt() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                    {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");

    // The loop opens one split per roster terminal, in roster order; mirror
    // that on a probe chrome to learn where each pane's content is drawn.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    for terminal_id in WorkspaceView::roster_terminal_ids(&workspace) {
        let pane = workspace
            .pane_for_terminal(&terminal_id)
            .expect("roster pane");
        probe.open_pane(pane, &terminal_id);
    }
    probe.compute_view(&workspace, area);
    let cells: Vec<(String, (u16, u16))> = probe
        .view
        .pane_infos
        .iter()
        .map(|info| {
            let pane = probe.pane_for_slot(info.id).expect("slot pane");
            let inner = info.inner_rect;
            (
                workspace.pane(pane).terminal_id.clone(),
                (inner.x + 1, inner.y + 1),
            )
        })
        .collect();
    assert_eq!(cells.len(), 2, "both terminals draw as panes");
    let cell_of = |terminal_id: &str| {
        cells
            .iter()
            .find(|(id, _)| id == terminal_id)
            .map(|(_, cell)| *cell)
            .expect("pane cell")
    };

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let initial = websocket_requests(&mock, "terminal_take_control")[0]
            .get("terminal_id")
            .and_then(Value::as_str)
            .expect("initial focused terminal")
            .to_string();
        let other = if initial == "terminal-a" {
            "terminal-b"
        } else {
            "terminal-a"
        };

        let (column, row) = cell_of(other);
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 1).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        let releases = websocket_requests(&mock, "terminal_release_control");
        assert_eq!(
            releases[0].get("terminal_id").and_then(Value::as_str),
            Some(initial.as_str()),
            "a click releases the pane focus left"
        );
        let takes = websocket_requests(&mock, "terminal_take_control");
        assert_eq!(
            takes[1].get("terminal_id").and_then(Value::as_str),
            Some(other),
            "a click takes the clicked pane"
        );

        let (column, row) = cell_of(&initial);
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            KeyModifiers::ALT,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 2).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            2,
            "alt+click must not take control"
        );
        let releases = websocket_requests(&mock, "terminal_release_control");
        assert_eq!(
            releases[1].get("terminal_id").and_then(Value::as_str),
            Some(other),
            "the lease still follows focus away from the clicked pane"
        );
        drop(input_tx);
        initial
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, initial) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    let pane = workspace
        .pane_for_terminal(&initial)
        .expect("initial terminal pane");
    assert_eq!(
        chrome.focused_pane(),
        Some(pane),
        "alt+click moved focus back"
    );
    assert!(
        workspace.pane(pane).is_observe(),
        "alt+click leaves the pane observed"
    );
    mock.shutdown().await;
}

/// 3.3: a pane whose app tracks the mouse gets SGR reports for its presses
/// and releases; a press in another such pane focuses it and takes the lease
/// before the report; a pane left observed by alt+click gets nothing; a
/// right-click passes through with the configured modifier held, hidden from
/// the app, or with the pane's own flag, and stays gclient's otherwise; a
/// shifted press selects instead of reporting.
#[tokio::test]
async fn mouse_forwarding_follows_pane_modes_and_passthrough() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                    {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let roster = WorkspaceView::roster_terminal_ids(&workspace).to_vec();
    for terminal_id in &roster {
        let pane = workspace
            .pane_for_terminal(terminal_id)
            .expect("roster pane");
        // Proxy sources, because these reports are the daemon write protocol;
        // a direct pane reports on its own frame socket (#22573).
        let mut source = ScriptedFrameSource::new(Transport::Proxy);
        source.queue(reporting_frame("mouse app"));
        workspace
            .replace_frame_source(pane, PaneFrameSource::Scripted(source))
            .expect("install scripted proxy source");
        workspace
            .recv_pane_frame(pane)
            .await
            .expect("the frame reaches the pane before the loop starts");
        workspace.pane_mut(pane).right_click_passthrough = true;
    }

    // Mirror the loop's chrome to learn where each pane's content is drawn.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    for terminal_id in &roster {
        let pane = workspace
            .pane_for_terminal(terminal_id)
            .expect("roster pane");
        probe.open_pane(pane, terminal_id);
    }
    probe.compute_view(&workspace, area);
    let cells: Vec<(String, (u16, u16))> = probe
        .view
        .pane_infos
        .iter()
        .map(|info| {
            let pane = probe.pane_for_slot(info.id).expect("slot pane");
            let inner = info.inner_rect;
            (
                workspace.pane(pane).terminal_id.clone(),
                (inner.x + 1, inner.y + 1),
            )
        })
        .collect();
    assert_eq!(cells.len(), 2, "both terminals draw as panes");
    let cell_of = |terminal_id: &str| {
        cells
            .iter()
            .find(|(id, _)| id == terminal_id)
            .map(|(_, cell)| *cell)
            .expect("pane cell")
    };

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    chrome.prefs.right_click_passthrough_modifier = PassthroughModifier::Alt;
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let initial = websocket_requests(&mock, "terminal_take_control")[0]
            .get("terminal_id")
            .and_then(Value::as_str)
            .expect("initial focused terminal")
            .to_string();
        let other = if initial == "terminal-a" {
            "terminal-b".to_string()
        } else {
            "terminal-a".to_string()
        };
        let mouse = |kind: MouseEventKind, (column, row): (u16, u16), modifiers: KeyModifiers| {
            send_mouse(&input_tx, kind, column, row, modifiers)
        };
        let left_down = MouseEventKind::Down(MouseButton::Left);
        let left_up = MouseEventKind::Up(MouseButton::Left);
        let right_down = MouseEventKind::Down(MouseButton::Right);
        let right_up = MouseEventKind::Up(MouseButton::Right);
        let inputs = || websocket_requests(&mock, "terminal_input").len();

        // A click in the held pane is reported: press, then release.
        mouse(left_down, cell_of(&initial), KeyModifiers::NONE).await;
        mouse(left_up, cell_of(&initial), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 2).await;

        // A press in the other pane focuses it and takes its lease first.
        mouse(left_down, cell_of(&other), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        wait_for_websocket_requests(&mock, "terminal_input", 3).await;
        mouse(left_up, cell_of(&other), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 4).await;

        // alt+click observes the first pane; a click there now reports nothing.
        mouse(left_down, cell_of(&initial), KeyModifiers::ALT).await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 2).await;
        settle_live_event().await;
        mouse(left_down, cell_of(&initial), KeyModifiers::NONE).await;
        mouse(left_up, cell_of(&initial), KeyModifiers::NONE).await;
        settle_live_event().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            2,
            "a report never takes the lease"
        );
        assert_eq!(inputs(), 4, "an observed pane gets no report");

        // alt+right-click passes through with the modifier hidden, after the
        // focus and lease move back; the pane flag passes a plain one through.
        mouse(right_down, cell_of(&other), KeyModifiers::ALT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        wait_for_websocket_requests(&mock, "terminal_input", 5).await;
        mouse(right_up, cell_of(&other), KeyModifiers::ALT).await;
        wait_for_websocket_requests(&mock, "terminal_input", 6).await;
        mouse(right_down, cell_of(&other), KeyModifiers::NONE).await;
        mouse(right_up, cell_of(&other), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 8).await;

        // ctrl is not the configured modifier, so the press opens gclient's
        // context menu instead of reporting; Esc dismisses it and shift keeps
        // the next press for a selection.
        mouse(right_down, cell_of(&other), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Esc, KeyModifiers::NONE).await;
        let (column, row) = cell_of(&other);
        mouse(left_down, (column, row), KeyModifiers::SHIFT).await;
        mouse(
            MouseEventKind::Drag(MouseButton::Left),
            (column + 3, row),
            KeyModifiers::SHIFT,
        )
        .await;
        settle_live_event().await;
        assert_eq!(
            inputs(),
            8,
            "ctrl+right-click and a shifted press are gclient's"
        );
        drop(input_tx);
        (initial, other)
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, (initial, other)) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    let writes = websocket_requests(&mock, "terminal_input");
    let data: Vec<&str> = writes
        .iter()
        .map(|write| {
            write
                .get("data")
                .and_then(Value::as_str)
                .expect("write data")
        })
        .collect();
    assert_eq!(
        data,
        [
            "\u{1b}[<0;2;2M",
            "\u{1b}[<0;2;2m",
            "\u{1b}[<0;2;2M",
            "\u{1b}[<0;2;2m",
            "\u{1b}[<2;2;2M",
            "\u{1b}[<2;2;2m",
            "\u{1b}[<2;2;2M",
            "\u{1b}[<2;2;2m",
        ],
        "every report is SGR at the pane cell, modifiers stripped"
    );
    let targets: Vec<&str> = writes
        .iter()
        .map(|write| {
            write
                .get("terminal_id")
                .and_then(Value::as_str)
                .expect("write target")
        })
        .collect();
    let (initial, other) = (initial.as_str(), other.as_str());
    assert_eq!(
        targets,
        [initial, initial, other, other, other, other, other, other],
        "reports go to the pane under the pointer"
    );
    assert!(
        chrome.selection.is_some(),
        "shift+drag selects instead of reporting"
    );
    mock.shutdown().await;
}

/// 2.4.2: a wheel notch over a pane that reports no mouse scrolls its
/// scrollback three rows a notch through `SetScrollOffset`, bootstrapped by
/// sending the first notch before any ceiling is known and clamped to the
/// depth the daemon then reports; the scrollbar track jumps and the thumb
/// drags to the offsets the scrollbar math gives; a pane on its alternate
/// screen gets arrow keys instead (herdr alternate-scroll).
#[tokio::test]
async fn wheel_and_scrollbar_drive_scrollback() {
    const MAX_ROWS: u32 = 10;
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue_write_outcome("delivered", None);
    mock.enqueue_write_outcome("delivered", None);
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": "terminal-scroll", "backend": "native", "state": "live"}],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane = workspace
        .pane_for_terminal("terminal-scroll")
        .expect("roster pane");
    let attachment = workspace.pane(pane).attachment_id().to_string();

    // The daemon only reports a scrollback depth in answer to a request, so an
    // attached pane starts out knowing no ceiling at all.
    assert_eq!(
        workspace.pane(pane).max_scroll,
        0,
        "a freshly attached pane has not learned its scrollback depth"
    );

    // Mirror the loop's one-pane chrome to learn where the content and the
    // scrollbar lane are drawn. Both rects are fixed by the layout rather than
    // the scroll state, so the lane can be sized from the depth the daemon is
    // about to report.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(pane, "terminal-scroll");
    probe.compute_view(&workspace, area);
    let info = probe.view.pane_infos[0].clone();
    let content = (info.inner_rect.x + 1, info.inner_rect.y + 1);
    let viewport_rows = info.inner_rect.height;
    let lane = scrollbar_gutter(
        pane_inner_rect(info.rect, info.borders),
        true,
        metrics_for(0, MAX_ROWS, viewport_rows),
    )
    .expect("a pane with scrollback draws a scrollbar lane");

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;

        let offsets = || -> Vec<u64> {
            websocket_requests(&mock, "terminal_set_scroll_offset")
                .iter()
                .map(|request| {
                    assert_eq!(
                        request.get("attachment_id").and_then(Value::as_str),
                        Some(attachment.as_str()),
                        "scrolling names the pane's attachment"
                    );
                    request
                        .get("rows_from_live_edge")
                        .and_then(Value::as_u64)
                        .expect("rows from the live edge")
                })
                .collect()
        };
        let (column, row) = content;

        // Bootstrap: with no ceiling learned yet the first notch must still go
        // out, because that request is what teaches the client the depth.
        send_mouse(
            &input_tx,
            MouseEventKind::ScrollUp,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 1).await;
        assert_eq!(
            offsets(),
            vec![3],
            "a pane that knows no ceiling sends its first notch unclamped"
        );

        // The daemon answers that request with the real depth. Input outranks
        // frames in the loop's biased select, so let it drain before the next
        // notch depends on the ceiling.
        mock.send_event_and_wait(json!({
            "type": "terminal_scroll_offset_applied",
            "terminal_id": "terminal-scroll",
            "attachment_id": attachment,
            "applied_rows": 3,
            "max_rows": MAX_ROWS,
        }))
        .await;
        settle_live_event().await;

        for _ in 0..4 {
            send_mouse(
                &input_tx,
                MouseEventKind::ScrollUp,
                column,
                row,
                KeyModifiers::NONE,
            )
            .await;
        }
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 4).await;
        settle_live_event().await;
        assert_eq!(
            offsets(),
            vec![3, 6, 9, 10],
            "up steps three rows a notch, clamps at the top, and a notch that cannot move sends nothing"
        );
        send_mouse(
            &input_tx,
            MouseEventKind::ScrollDown,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 5).await;
        assert_eq!(
            offsets()[4],
            7,
            "down steps three rows toward the live edge"
        );

        // A click on the track beside the thumb jumps the scrollback there.
        let metrics = metrics_for(7, MAX_ROWS, viewport_rows);
        let track_row = (lane.y..lane.y + lane.height)
            .find(|row| scrollbar_thumb_grab_offset(metrics, lane, *row).is_none())
            .expect("a track row beside the thumb");
        let jump = scrollbar_offset_from_row(metrics, lane, track_row) as u64;
        assert_ne!(jump, 7, "the track row moves the viewport");
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            lane.x,
            track_row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 6).await;
        assert_eq!(
            offsets()[5],
            jump,
            "a track click jumps the scrollback there"
        );
        send_mouse(
            &input_tx,
            MouseEventKind::Up(MouseButton::Left),
            lane.x,
            track_row,
            KeyModifiers::NONE,
        )
        .await;
        settle_live_event().await;

        // A thumb drag keeps the grabbed row under the pointer.
        let metrics = metrics_for(
            u32::try_from(jump).expect("offset"),
            MAX_ROWS,
            viewport_rows,
        );
        let (thumb_row, grab) = (lane.y..lane.y + lane.height)
            .find_map(|row| scrollbar_thumb_grab_offset(metrics, lane, row).map(|grab| (row, grab)))
            .expect("a thumb row");
        let drop_row = [lane.y + lane.height - 1, lane.y]
            .into_iter()
            .find(|row| scrollbar_offset_from_drag_row(metrics, lane, *row, grab) as u64 != jump)
            .expect("a drop row that moves the viewport");
        let dragged = scrollbar_offset_from_drag_row(metrics, lane, drop_row, grab) as u64;
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            lane.x,
            thumb_row,
            KeyModifiers::NONE,
        )
        .await;
        send_mouse(
            &input_tx,
            MouseEventKind::Drag(MouseButton::Left),
            lane.x,
            drop_row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 7).await;
        assert_eq!(
            offsets()[6],
            dragged,
            "a thumb drag maps the drop row to an offset"
        );
        send_mouse(
            &input_tx,
            MouseEventKind::Up(MouseButton::Left),
            lane.x,
            drop_row,
            KeyModifiers::NONE,
        )
        .await;
        settle_live_event().await;
        assert_eq!(
            offsets().len(),
            7,
            "a thumb press and its release send nothing of their own"
        );

        // A pane on its alternate screen gets arrow keys instead.
        mock.send_event_and_wait(json!({
            "type": "terminal_frame",
            "terminal_id": "terminal-scroll",
            "attachment_id": attachment,
            "encoding": "bincode-b64",
            "payload": encoded_alternate_frame("full-screen app"),
        }))
        .await;
        settle_live_event().await;
        send_mouse(
            &input_tx,
            MouseEventKind::ScrollUp,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
        send_mouse(
            &input_tx,
            MouseEventKind::ScrollDown,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_input", 2).await;
        settle_live_event().await;
        let inputs: Vec<String> = websocket_requests(&mock, "terminal_input")
            .iter()
            .map(|request| {
                request
                    .get("data")
                    .and_then(Value::as_str)
                    .expect("input data")
                    .to_string()
            })
            .collect();
        assert_eq!(
            inputs,
            vec![
                "\x1b[A\x1b[A\x1b[A".to_string(),
                "\x1b[B\x1b[B\x1b[B".to_string()
            ],
            "alternate-scroll sends three arrow keys a notch"
        );
        assert_eq!(
            offsets().len(),
            7,
            "a pane on its alternate screen never scrolls scrollback"
        );
        drop(input_tx);
        dragged
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, dragged) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    assert_eq!(
        u64::from(workspace.pane(pane).scroll_offset()),
        dragged,
        "the pane mirrors the last offset it asked for"
    );
    mock.shutdown().await;
}

/// A tmux-backed pane has no daemon-side scrollback to ask for: the daemon
/// skips the host verb for it and would answer with the client's own number,
/// so the notch is consumed rather than pretending to scroll.
#[tokio::test]
async fn wheel_over_a_tmux_pane_asks_the_daemon_for_nothing() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": "terminal-tmux", "backend": "tmux", "state": "live"}],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane = workspace
        .pane_for_terminal("terminal-tmux")
        .expect("roster pane");

    // Mirror the loop's one-pane chrome and confirm the notch's cell really
    // lands on the pane, so an empty request log means the wheel was consumed
    // rather than never routed.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(pane, "terminal-tmux");
    probe.compute_view(&workspace, area);
    let inner = probe.view.pane_infos[0].inner_rect;
    let (column, row) = (inner.x + 1, inner.y + 1);
    assert!(
        matches!(hit_test(&probe.view, column, row), Hit::Pane { .. }),
        "the notch lands on the pane"
    );

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(32);

    // The loop draws its hit map before its first select and reads input ahead
    // of the closed channel, so one notch then a drop is deterministic.
    let driver = async {
        send_mouse(
            &input_tx,
            MouseEventKind::ScrollUp,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
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
    result.expect("live loop exits cleanly");
    assert!(
        websocket_requests(&mock, "terminal_set_scroll_offset").is_empty(),
        "a tmux pane never asks the daemon to scroll"
    );
    assert_eq!(
        workspace.pane(pane).scroll_offset(),
        0,
        "the pane stays at the live edge"
    );
    mock.shutdown().await;
}

/// The daemon answers a request that carried no ceiling with `max_rows` equal
/// to the rows it applied, and that echo can arrive after gterm's real depth
/// has already been relayed. Taking it at face value would shrink the ceiling
/// to the pane's own position and wedge the wheel there for good.
#[tokio::test]
async fn a_scroll_reply_echoing_its_own_offset_never_lowers_the_ceiling() {
    const MAX_ROWS: u32 = 10;
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": "terminal-echo", "backend": "native", "state": "live"}],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane = workspace
        .pane_for_terminal("terminal-echo")
        .expect("roster pane");
    let attachment = workspace.pane(pane).attachment_id().to_string();

    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(pane, "terminal-echo");
    probe.compute_view(&workspace, area);
    let inner = probe.view.pane_infos[0].inner_rect;
    let (column, row) = (inner.x + 1, inner.y + 1);

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let offsets = || -> Vec<u64> {
            websocket_requests(&mock, "terminal_set_scroll_offset")
                .iter()
                .map(|request| {
                    request
                        .get("rows_from_live_edge")
                        .and_then(Value::as_u64)
                        .expect("rows from the live edge")
                })
                .collect()
        };
        let applied = |applied_rows: u32, max_rows: u32| {
            json!({
                "type": "terminal_scroll_offset_applied",
                "terminal_id": "terminal-echo",
                "attachment_id": attachment,
                "applied_rows": applied_rows,
                "max_rows": max_rows,
            })
        };

        send_mouse(
            &input_tx,
            MouseEventKind::ScrollUp,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 1).await;

        // gterm's real depth lands first, then the daemon's echo of the same
        // request overtakes it.
        mock.send_event_and_wait(applied(3, MAX_ROWS)).await;
        settle_live_event().await;
        mock.send_event_and_wait(applied(3, 3)).await;
        settle_live_event().await;

        send_mouse(
            &input_tx,
            MouseEventKind::ScrollUp,
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_set_scroll_offset", 2).await;
        settle_live_event().await;
        assert_eq!(
            offsets(),
            vec![3, 6],
            "the echo left the confirmed ceiling alone, so the wheel kept moving"
        );
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
    result.expect("live loop exits cleanly");
    mock.shutdown().await;
}

/// 2.5.1: the status line's control indicator is a button for the focused
/// pane's lease. A click while the pane is held releases control, a click
/// while it is observed takes control, and once the daemon reports the lease
/// lost the same click accepts the pending take-back.
#[tokio::test]
async fn control_indicator_click_toggles_control() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": "terminal-lease", "backend": "native", "state": "live"}],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane = workspace
        .pane_for_terminal("terminal-lease")
        .expect("roster pane");
    let attachment = workspace.pane(pane).attachment_id().to_string();

    // Mirror the loop's one-pane chrome to learn where the status line is
    // drawn; the indicator leads it.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(pane, "terminal-lease");
    probe.compute_view(&workspace, area);
    let status = probe.view.status_rect;
    let (column, row) = (status.x + 1, status.y);

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        let click = || async {
            send_mouse(
                &input_tx,
                MouseEventKind::Down(MouseButton::Left),
                column,
                row,
                KeyModifiers::NONE,
            )
            .await;
            send_mouse(
                &input_tx,
                MouseEventKind::Up(MouseButton::Left),
                column,
                row,
                KeyModifiers::NONE,
            )
            .await;
        };
        let lease_requests = |kind: &str| -> Vec<(Option<String>, Option<String>)> {
            websocket_requests(&mock, kind)
                .iter()
                .map(|request| {
                    (
                        request
                            .get("terminal_id")
                            .and_then(Value::as_str)
                            .map(str::to_owned),
                        request
                            .get("attachment_id")
                            .and_then(Value::as_str)
                            .map(str::to_owned),
                    )
                })
                .collect()
        };
        let lease = (Some("terminal-lease".to_string()), Some(attachment.clone()));

        // Startup focus took the lease, so the first click releases it.
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        click().await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 1).await;
        settle_live_event().await;
        assert_eq!(
            lease_requests("terminal_release_control"),
            vec![lease.clone()],
            "a click on a held pane releases its lease"
        );
        assert_eq!(
            lease_requests("terminal_take_control").len(),
            1,
            "the release click takes nothing"
        );

        // The pane is observed now, so the next click takes the lease back.
        click().await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        settle_live_event().await;
        assert_eq!(
            lease_requests("terminal_take_control")[1],
            lease,
            "a click on an observed pane takes its lease"
        );
        assert_eq!(
            lease_requests("terminal_release_control").len(),
            1,
            "the take click releases nothing"
        );

        // A peer takes the lease; the daemon offers a take-back, and the
        // click accepts it with a fresh grant at the peer's generation.
        mock.send_event_and_wait(json!({
            "type": "terminal_lease_lost",
            "terminal_id": "terminal-lease",
            "attachment_id": attachment,
            "holder": "peer",
            "lease_generation": 2,
            "daemon_epoch": "epoch-1",
            "seq": 2
        }))
        .await;
        settle_live_event().await;
        mock.enqueue_take_control_reply(true, 2, None);
        click().await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        settle_live_event().await;
        assert_eq!(
            lease_requests("terminal_take_control")[2],
            lease,
            "a click with a take-back pending accepts it"
        );
        assert_eq!(
            lease_requests("terminal_release_control").len(),
            1,
            "accepting a take-back releases nothing"
        );
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
    result.expect("live loop exits cleanly");
    let pane = workspace.pane(pane);
    assert!(
        pane.is_held() && !pane.has_take_back(),
        "the accepted take-back holds the lease again: {:?} take_back={}",
        pane.control,
        pane.has_take_back()
    );
    mock.shutdown().await;
}

/// Run the live loop over one terminal showing a URL, ctrl+click the URL
/// with `opener` configured, and return the chrome the loop left behind.
async fn ctrl_click_link_with(opener: &str) -> Chrome {
    const ROW: &str = "see https://example.com/docs now";
    let mock = MockDaemon::start("local-token").await;
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{"terminal_id": "terminal-link", "backend": "native", "state": "live"}],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane = workspace
        .pane_for_terminal("terminal-link")
        .expect("roster pane");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(semantic_frame(ROW));
    workspace
        .replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("install scripted direct source");
    workspace
        .recv_pane_frame(pane)
        .await
        .expect("the frame reaches the pane before the loop starts");

    // Mirror the loop's one-pane chrome to learn where the URL is drawn.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(pane, "terminal-link");
    probe.compute_view(&workspace, area);
    let inner = probe.view.pane_infos[0].inner_rect;
    let url_col = u16::try_from(ROW.find("example").expect("url in row")).expect("column");
    let (column, row) = (inner.x + url_col, inner.y);

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    chrome.link_opener = opener.to_string();
    let (input_tx, input_rx) = mpsc::channel(256);

    // Queue the click before the loop starts: the loop draws before its first
    // select and input outranks frames there, so the click routes before the
    // spent scripted source's EOF starts a proxy fallback, which would defer
    // it and drop it once input closes.
    send_mouse(
        &input_tx,
        MouseEventKind::Down(MouseButton::Left),
        column,
        row,
        KeyModifiers::CONTROL,
    )
    .await;
    send_mouse(
        &input_tx,
        MouseEventKind::Up(MouseButton::Left),
        column,
        row,
        KeyModifiers::CONTROL,
    )
    .await;
    drop(input_tx);

    let mut switch = TerminalGuard::recording().0;
    run_live_loop(
        &mut workspace,
        &mut terminal,
        &mut chrome,
        input_rx,
        &mut switch,
    )
    .await
    .expect("live loop exits cleanly");
    chrome
}

#[tokio::test]
async fn open_link_failure_surfaces_a_toast() {
    let opened = ctrl_click_link_with("true").await;
    assert!(
        opened.alert_log.is_empty(),
        "an opener that launches raises no toast"
    );

    let opener = "/nonexistent/gclient-link-opener";
    let failed = ctrl_click_link_with(opener).await;
    let toast = failed
        .alert_log
        .last()
        .expect("a failed launch raises a toast");
    assert!(
        matches!(toast.kind, ToastKind::Warning),
        "warning, not error: {toast:?}"
    );
    assert!(
        toast.title.contains(opener),
        "the toast names the opener: {toast:?}"
    );
}

/// 4.1.2: the keymap actions that were inert drive the live workspace. Focus
/// moves are read off the daemon's `terminal_take_control` requests, the
/// split off the spawn, and the swap off the first tab's layout.
#[tokio::test]
async fn wired_actions_split_focus_swap_and_switch_tabs() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let roster_page = |ids: &[&str]| {
        let items: Vec<Value> = ids
            .iter()
            .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
            .collect();
        json!({
            "items": items,
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        })
    };
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_page(&["terminal-a", "terminal-b", "terminal-c"]),
        );
    }
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        roster_page(&["terminal-a", "terminal-b", "terminal-c", SPAWNED]),
    );
    let attention = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:terminal-b",
            "terminal": {"terminal_id": "terminal-b", "backend": "native"},
            "attention": {
                "attention_id": "att-b",
                "state": "blocked",
                "kind": "actionable",
                "fingerprint": "fp-b",
                "payload": {"prompt": "Continue?", "options": [{"option": 1, "label": "Yes"}]}
            }
        }]
    });
    for _ in 0..4 {
        mock.enqueue("GET", "/api/attention/roster", 200, attention.clone());
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let seeded = mock.seed_workspace(
        "project-1",
        &[
            (&["terminal-a", "terminal-b"], "terminal-a"),
            (&["terminal-c"], "terminal-c"),
        ],
    );
    let (tab_1, panes_1) = &seeded[0];
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane_of = |workspace: &Workspace<LiveDaemon>, id: &str| {
        workspace.pane_for_terminal(id).expect("roster pane")
    };
    let (pane_a, pane_b, pane_c) = (
        pane_of(&workspace, "terminal-a"),
        pane_of(&workspace, "terminal-b"),
        pane_of(&workspace, "terminal-c"),
    );
    for pane in [pane_a, pane_b, pane_c] {
        workspace
            .replace_frame_source(
                pane,
                PaneFrameSource::Scripted(ScriptedFrameSource::new(Transport::Direct)),
            )
            .expect("install scripted direct source");
    }

    // Tab 0: a beside b (a focused); tab 1: c, all daemon rows the loop
    // projects at reconcile. The toast points at c.
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::from_toml(
        "[bindings]\nlast_pane = \"prefix+i\"\nnext_attention = \"prefix+f\"\n",
        HERDR_PREFIX,
    )
    .expect("test keymap");
    chrome.notify(Toast {
        kind: ToastKind::Info,
        title: "terminal-c".to_string(),
        body: None,
        target: Some("terminal-c".to_string()),
    });
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let chord =
            |c: char, modifiers: KeyModifiers| send_chord(&input_tx, KeyCode::Char(c), modifiers);
        // FocusPaneRight: the lease follows focus to the neighbour.
        chord('l', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        // SwapPaneLeft: b takes a's place, focus stays on b.
        chord('H', KeyModifiers::SHIFT).await;
        settle_live_event().await;
        // LastPane (custom chord): back to a.
        chord('i', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        chord('z', KeyModifiers::NONE).await;
        settle_live_event().await;
        // NextTab, PreviousTab, SwitchTab(2), SwitchTab(1).
        chord('n', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 4).await;
        chord('p', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 5).await;
        chord('2', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 6).await;
        chord('1', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 7).await;
        // SplitHorizontal spawns under the focused pane.
        chord('-', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        timeout(Duration::from_secs(1), async {
            loop {
                let attached = websocket_requests(&mock, "terminal_set_viewport")
                    .iter()
                    .any(|request| request.get("terminal_id") == Some(&json!(SPAWNED)));
                if attached {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the spawned terminal attaches");
        settle_live_event().await;
        let before_jumps = websocket_requests(&mock, "terminal_take_control").len();
        // OpenNotificationTarget: c, in the other tab.
        chord('o', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", before_jumps + 1).await;
        // NextAttention (custom chord): b's terminal, back in the first tab.
        chord('f', KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", before_jumps + 2).await;
        drop(input_tx);
        before_jumps
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, before_jumps) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    let targets: Vec<String> = websocket_requests(&mock, "terminal_take_control")
        .iter()
        .map(|request| {
            request
                .get("terminal_id")
                .and_then(Value::as_str)
                .expect("take_control target")
                .to_string()
        })
        .collect();
    assert_eq!(
        &targets[..7],
        [
            "terminal-a",
            "terminal-b",
            "terminal-a",
            "terminal-c",
            "terminal-a",
            "terminal-c",
            "terminal-a"
        ],
        "focus-direction, last-pane and tab switching move the lease"
    );
    assert_eq!(
        &targets[before_jumps..],
        ["terminal-c", "terminal-b"],
        "the toast target and the attention jump cross tabs"
    );
    let tab = &chrome.tabs().tabs[0];
    assert!(chrome.is_zoomed(), "zoom toggles the active tab");
    let area = Rect::new(0, 0, 120, 40);
    let rect_of = |pane| {
        let slot = tab.slot_for(pane).expect("pane shown in the first tab");
        tab.layout
            .panes(area, chrome.tab_focus(tab))
            .into_iter()
            .find(|info| info.id == slot)
            .expect("slot geometry")
            .rect
    };
    let (a, b) = (rect_of(pane_a), rect_of(pane_b));
    assert!(
        b.x < a.x && b.y == a.y,
        "swap exchanges the two slots: b {b:?} a {a:?}"
    );
    let spawned = rect_of(workspace.pane_for_terminal(SPAWNED).expect("spawned pane"));
    assert!(
        spawned.x == a.x && spawned.y > a.y,
        "split stacked lands under the focused pane: {spawned:?} vs {a:?}"
    );
    assert!(
        chrome.toasts.is_empty(),
        "the notification target clears the toast"
    );
    assert_eq!(chrome.active_index(), 0);
    assert!(
        chrome.dialog.is_none(),
        "the attention jump reveals the terminal and opens no prompt: {:?}",
        chrome.dialog
    );
    // 4.2.4: every layout mutation went to the daemon as a workspace_op with
    // explicit ids, and the tabs on screen are the daemon's rows.
    assert_eq!(
        tab.id, *tab_1,
        "the loop applies the daemon's workspace_event: tabs carry daemon ids"
    );
    let ops = websocket_requests(&mock, "workspace_op");
    let mutations: Vec<&str> = ops
        .iter()
        .filter_map(|op| op.get("op").and_then(Value::as_str))
        .filter(|kind| *kind != "workspace.set_focus_hints")
        .collect();
    assert_eq!(
        mutations,
        ["pane.swap", "pane.split"],
        "swap and split are daemon ops; focus, zoom and tab switches stay local"
    );
    let swap = ops
        .iter()
        .find(|op| op["op"] == "pane.swap")
        .expect("swap op");
    assert_eq!(swap["pane"], json!(panes_1[1]), "the focused pane b");
    assert_eq!(
        swap["other"],
        json!(panes_1[0]),
        "swaps with its neighbour a"
    );
    let split = ops
        .iter()
        .find(|op| op["op"] == "pane.split")
        .expect("split op");
    assert_eq!(
        split["pane"],
        json!(panes_1[0]),
        "splits the focused pane a"
    );
    assert_eq!(split["axis"], json!("vertical"));
    assert_eq!(
        split["terminal_id"],
        json!(SPAWNED),
        "carries the spawned terminal"
    );
    let hint = ops
        .iter()
        .rev()
        .find(|op| op["op"] == "workspace.set_focus_hints")
        .expect("focus hints follow the focus");
    assert_eq!(hint["workspace"], json!(WORKSPACE_ID));
    assert_eq!(hint["project_id"], json!("project-1"));
    assert_eq!(hint["tab"], json!(tab_1));
    assert_eq!(
        hint["pane"],
        json!(panes_1[1]),
        "the last focus landed on b"
    );
    mock.shutdown().await;
}

/// The golden fixture's workspace, which the mock's simulator keeps.
const WORKSPACE_ID: &str = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const DAEMON_EPOCH: &str = "00000000-0000-4000-8000-000000000000";

fn roster_items(ids: &[&str]) -> Value {
    let items: Vec<Value> = ids
        .iter()
        .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
        .collect();
    json!({
        "items": items,
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
    })
}

/// A `pane.added` event splitting `tab`'s only pane `first` with `added`
/// on `terminal`, shaped like the daemon's (tabs = the updated row, panes =
/// the new row).
fn pane_added_event(tab: &str, first: &str, added: &str, terminal: &str, seq: u64) -> Value {
    json!({
        "type": "workspace_event",
        "kind": "pane.added",
        "workspace_id": WORKSPACE_ID,
        "project_id": "project-1",
        "workspace": null,
        "tabs": [{
            "id": tab,
            "workspace_id": WORKSPACE_ID,
            "ref": 1,
            "title": null,
            "project_id": "project-1",
            "worktree_id": null,
            "position": 0,
            "focused_pane_id": first,
            "layout": {
                "kind": "split",
                "axis": "horizontal",
                "ratio": 0.5,
                "children": [
                    {"kind": "pane", "pane_id": first},
                    {"kind": "pane", "pane_id": added},
                ],
            },
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "panes": [{
            "id": added,
            "tab_id": tab,
            "ref": 2,
            "terminal_id": terminal,
            "owns_terminal": true,
            "label": null,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "daemon_epoch": DAEMON_EPOCH,
        "seq": seq,
        "timestamp": "2026-01-01T00:00:00+00:00",
    })
}

/// `pane_added_event` turned into a `pane.renamed` of the added pane.
fn pane_renamed_event(
    tab: &str,
    first: &str,
    pane: &str,
    terminal: &str,
    label: &str,
    seq: u64,
) -> Value {
    let mut event = pane_added_event(tab, first, pane, terminal, seq);
    event["kind"] = json!("pane.renamed");
    event["tabs"] = json!([]);
    event["panes"][0]["label"] = json!(label);
    event
}

fn terminal_row_requests(mock: &MockDaemon, terminal_id: &str) -> usize {
    let target = format!("/api/terminals/{terminal_id}");
    mock.requests()
        .iter()
        .filter(|request| request.method == "GET" && request.target == target)
        .count()
}

/// 4.2.5: a workspace_event naming a terminal the roster has not delivered
/// opens it on demand, and while the row is unavailable the slot renders
/// empty in the daemon's layout instead of being reaped.
#[tokio::test]
async fn workspace_events_open_terminals_on_demand() {
    let mock = MockDaemon::start("local-token").await;
    for _ in 0..2 {
        mock.enqueue("GET", "/api/terminals?", 200, roster_items(&["terminal-a"]));
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let seeded = mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    let (tab_id, panes) = seeded[0].clone();
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        // The row is not served yet: the loop asks for it and keeps the slot.
        mock.send_event_and_wait(pane_added_event(
            &tab_id,
            &panes[0],
            "mock-pane-late",
            "terminal-late",
            1,
        ))
        .await;
        wait_until(|| terminal_row_requests(&mock, "terminal-late") >= 1).await;
        settle_live_event().await;
        let unresolved = websocket_requests(&mock, "terminal_attach")
            .iter()
            .all(|request| request.get("terminal_id") != Some(&json!("terminal-late")));
        assert!(
            unresolved,
            "nothing attaches a terminal whose row is unavailable"
        );
        // The row arrives: the next event's re-projection opens it.
        mock.enqueue(
            "GET",
            "/api/terminals/terminal-late",
            200,
            json!({"terminal_id": "terminal-late", "backend": "native", "state": "live"}),
        );
        mock.send_event_and_wait(pane_renamed_event(
            &tab_id,
            &panes[0],
            "mock-pane-late",
            "terminal-late",
            "late",
            2,
        ))
        .await;
        timeout(Duration::from_secs(1), async {
            loop {
                let attached = websocket_requests(&mock, "terminal_attach")
                    .iter()
                    .any(|request| request.get("terminal_id") == Some(&json!("terminal-late")));
                if attached {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the late terminal attaches once its row is served");
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        chrome.tabs().tabs.len(),
        1,
        "the daemon tab is never reaped"
    );
    let tab = &chrome.tabs().tabs[0];
    assert_eq!(tab.id, tab_id);
    assert_eq!(
        tab.layout.pane_ids().len(),
        2,
        "the daemon's layout keeps the slot for the late terminal"
    );
    let late = workspace
        .pane_for_terminal("terminal-late")
        .expect("the late terminal was opened on demand");
    assert_eq!(
        tab.slots.len(),
        2,
        "both slots resolve once the row is served"
    );
    assert_eq!(
        workspace.pane(late).label.as_deref(),
        Some("late"),
        "the pane label comes from the daemon row"
    );
    mock.shutdown().await;
}

/// 4.2.6: a daemon restart's reconnect re-attaches the workspace and
/// re-fetches its snapshot under the pinned watermark; the panes stay.
#[tokio::test]
async fn daemon_restart_refetches_workspace_snapshot() {
    let mock = MockDaemon::start("local-token").await;
    for _ in 0..4 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_items(&["terminal-a", "terminal-b"]),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let seeded = mock.seed_workspace(
        "project-1",
        &[(&["terminal-a", "terminal-b"], "terminal-a")],
    );
    let (tab_id, panes) = seeded[0].clone();
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        assert_eq!(websocket_requests(&mock, "workspace_attach").len(), 1);
        // An event moves the model past the snapshot the mock serves.
        mock.send_event_and_wait(pane_renamed_event(
            &tab_id,
            &panes[0],
            &panes[1],
            "terminal-b",
            "b",
            7,
        ))
        .await;
        settle_live_event().await;
        mock.close_websockets_service_restart();
        tokio::time::pause();
        for _ in 0..64 {
            if websocket_requests(&mock, "workspace_attach").len() >= 2
                && websocket_requests(&mock, "terminal_set_viewport").len() >= 4
            {
                break;
            }
            tokio::time::advance(Duration::from_secs(2)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "workspace_attach", 2).await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        websocket_requests(&mock, "workspace_attach").len(),
        2,
        "the reconnect re-attaches the workspace"
    );
    let model = workspace.workspace_model().expect("attached workspace");
    assert!(
        model.watermark.seq < 7,
        "the re-fetched snapshot's watermark is pinned in place of the event's: {}",
        model.watermark.seq
    );
    assert_eq!(chrome.tabs().tabs.len(), 1);
    let tab = &chrome.tabs().tabs[0];
    assert_eq!(tab.id, tab_id);
    assert_eq!(tab.slots.len(), 2, "both panes survive the restart");
    mock.shutdown().await;
}

/// 4.2.7: dragging a split border applies the ratio locally on every move
/// and sends exactly one `pane.resize` when the button is released.
#[tokio::test]
async fn ratio_drag_sends_one_op_on_drop() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_items(&["terminal-a", "terminal-b"]),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let seeded = mock.seed_workspace(
        "project-1",
        &[(&["terminal-a", "terminal-b"], "terminal-a")],
    );
    let (_, panes) = seeded[0].clone();
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let (pane_a, pane_b) = (
        workspace.pane_for_terminal("terminal-a").expect("pane a"),
        workspace.pane_for_terminal("terminal-b").expect("pane b"),
    );
    for pane in [pane_a, pane_b] {
        workspace
            .replace_frame_source(
                pane,
                PaneFrameSource::Scripted(ScriptedFrameSource::new(Transport::Direct)),
            )
            .expect("install scripted direct source");
    }
    // The loop shows a beside b; mirror that on a probe chrome to find the
    // divider between them.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(pane_a, "a");
    probe.open_pane(pane_b, "b");
    probe.compute_view(&workspace, area);
    let border = &probe.view.split_borders[0];
    let (column, row) = (border.pos, border.area.y + 2);
    let (split_x, split_width) = (border.area.x, border.area.width);
    let dropped_at = column + 24;
    let expected_ratio = f64::from(dropped_at - split_x) / f64::from(split_width);

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        settle_live_event().await;
        let mouse = |kind, column, modifiers| send_mouse(&input_tx, kind, column, row, modifiers);
        mouse(
            MouseEventKind::Down(MouseButton::Left),
            column,
            KeyModifiers::NONE,
        )
        .await;
        for step in [8, 16, 24] {
            mouse(
                MouseEventKind::Drag(MouseButton::Left),
                column + step,
                KeyModifiers::NONE,
            )
            .await;
            settle_live_event().await;
        }
        assert!(
            websocket_requests(&mock, "workspace_op")
                .iter()
                .all(|op| op["op"] != "pane.resize"),
            "no resize op while the button is held"
        );
        mouse(
            MouseEventKind::Up(MouseButton::Left),
            dropped_at,
            KeyModifiers::NONE,
        )
        .await;
        wait_until(|| {
            websocket_requests(&mock, "workspace_op")
                .iter()
                .any(|op| op["op"] == "pane.resize")
        })
        .await;
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
    result.expect("live loop exits cleanly");
    let resizes: Vec<Value> = websocket_requests(&mock, "workspace_op")
        .into_iter()
        .filter(|op| op["op"] == "pane.resize")
        .collect();
    assert_eq!(resizes.len(), 1, "one resize per drop: {resizes:?}");
    assert_eq!(
        resizes[0]["pane"],
        json!(panes[0]),
        "the op names a pane directly under the dragged split"
    );
    let ratio = resizes[0]["ratio"].as_f64().expect("ratio");
    assert!(
        (ratio - expected_ratio).abs() < 0.02,
        "the dropped ratio {ratio} is sent, expected {expected_ratio}"
    );
    mock.shutdown().await;
}

/// Poll `condition` on the test runtime until it holds or two seconds pass.
async fn wait_until(mut condition: impl FnMut() -> bool) {
    timeout(Duration::from_secs(2), async {
        while !condition() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("condition holds before the deadline");
}

/// 4.1.2: toggling the settings `mouse capture` row flips the terminal's
/// capture through the guard while the loop runs and writes the pref to
/// `<gobby_home>/client/prefs.toml` at once.
#[tokio::test]
async fn settings_toggle_switches_mouse_capture_and_saves_prefs() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _) = live_workspace_with_scripted_direct(&mock, "terminal-prefs", 1).await;
    let home = tempfile::tempdir().expect("gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    let pane = workspace
        .pane_for_terminal("terminal-prefs")
        .expect("terminal pane");
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "loop");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let backend = RecordingBackend::default();
    let captured = backend.mouse_capture();
    let mut guard = TerminalGuard::new(backend);
    guard.arm(true).expect("arm the recording guard");
    let prefs_file = prefs_path(home.path());
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = {
        let captured = Arc::clone(&captured);
        let prefs_file = prefs_file.clone();
        async move {
            tokio::task::yield_now().await;
            send_chord(&input_tx, KeyCode::Char('s'), KeyModifiers::NONE).await;
            send_key(&input_tx, KeyCode::Down, KeyModifiers::NONE).await;
            send_key(&input_tx, KeyCode::Char(' '), KeyModifiers::NONE).await;
            wait_until(|| !captured.load(Ordering::SeqCst)).await;
            let saved = std::fs::read_to_string(&prefs_file).expect("prefs written on toggle");
            assert!(saved.contains("mouse_capture = false"), "{saved}");
            send_key(&input_tx, KeyCode::Char(' '), KeyModifiers::NONE).await;
            wait_until(|| captured.load(Ordering::SeqCst)).await;
            drop(input_tx);
        }
    };

    let (result, ()) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut guard
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    assert!(chrome.prefs.mouse_capture);
    let saved = std::fs::read_to_string(&prefs_file).expect("prefs written on toggle back");
    assert!(saved.contains("mouse_capture = true"), "{saved}");
    mock.shutdown().await;
}

/// 5.1.2: right-clicks open the pane, tab and empty-chrome menus; hover and
/// keys move the selection; enter and a left click activate; a press outside
/// closes the menu without reaching what it landed on.
#[tokio::test]
async fn context_menu_dispatches_items_and_closes_outside() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let roster_page = |ids: &[&str]| {
        let items: Vec<Value> = ids
            .iter()
            .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
            .collect();
        json!({
            "items": items,
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        })
    };
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_page(&["terminal-a", "terminal-b"]),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let home = tempfile::tempdir().expect("gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    let light = ClientPrefs {
        theme: "light".to_string(),
        ..ClientPrefs::default()
    };
    save_prefs(home.path(), &light).expect("prefs for reload");
    std::fs::write(
        home.path().join("client").join("keymap.toml"),
        "[bindings]\nhelp = \"prefix+f1\"\n",
    )
    .expect("keymap for reload");

    // Mirror the loop's roster split on a probe chrome to learn where each
    // pane, the first tab and the bare tab bar are drawn.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    for terminal_id in WorkspaceView::roster_terminal_ids(&workspace) {
        let pane = workspace
            .pane_for_terminal(&terminal_id)
            .expect("roster pane");
        probe.open_pane(pane, workspace.pane(pane).display_name());
    }
    probe.compute_view(&workspace, area);
    // The tab bar's hit areas come from the draw, as in the loop.
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let cells: Vec<(String, (u16, u16))> = probe
        .view
        .pane_infos
        .iter()
        .map(|info| {
            let pane = probe.pane_for_slot(info.id).expect("slot pane");
            let inner = info.inner_rect;
            (
                workspace.pane(pane).terminal_id.clone(),
                (inner.x + 1, inner.y + 1),
            )
        })
        .collect();
    let cell_of = |terminal_id: &str| {
        cells
            .iter()
            .find(|(id, _)| id == terminal_id)
            .map(|(_, cell)| *cell)
            .expect("pane cell")
    };
    let tab_cell = probe
        .view
        .tab_hit_areas
        .iter()
        .find(|(index, _)| *index == 0)
        .map(|(_, rect)| (rect.x, rect.y))
        .expect("first tab drawn");
    let bar = probe.view.tab_bar_rect.expect("tab bar drawn");
    // The middle of the bare stretch stays bare as tab titles change.
    let bare_columns: Vec<u16> = (bar.x..bar.right())
        .filter(|column| hit_test(&probe.view, *column, bar.y) == Hit::TabBarEmpty)
        .collect();
    let bare_column = bare_columns
        .get(bare_columns.len() / 2)
        .copied()
        .expect("bare tab bar space");
    let bare_cell = (bare_column, bar.y);
    // Menu rows start one cell inside the popup at the click.
    let item_cell = |anchor: (u16, u16), index: u16| (anchor.0 + 2, anchor.1 + 1 + index);

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let initial = websocket_requests(&mock, "terminal_take_control")[0]
            .get("terminal_id")
            .and_then(Value::as_str)
            .expect("initial focused terminal")
            .to_string();
        let other = if initial == "terminal-a" {
            "terminal-b"
        } else {
            "terminal-a"
        };
        let press = |button, (column, row): (u16, u16)| {
            send_mouse(
                &input_tx,
                MouseEventKind::Down(button),
                column,
                row,
                KeyModifiers::NONE,
            )
        };
        let hover = |(column, row): (u16, u16)| {
            send_mouse(
                &input_tx,
                MouseEventKind::Moved,
                column,
                row,
                KeyModifiers::NONE,
            )
        };
        let key = |code| send_key(&input_tx, code, KeyModifiers::NONE);
        let terminal_of = |request: &Value| {
            request
                .get("terminal_id")
                .and_then(Value::as_str)
                .expect("request terminal")
                .to_string()
        };

        // A press outside the open menu closes it and goes no further: the
        // pane under it is not focused, so the next key still reaches the
        // pane that had focus.
        press(MouseButton::Right, cell_of(&initial)).await;
        press(MouseButton::Left, cell_of(other)).await;
        key(KeyCode::Char('x')).await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
        assert_eq!(
            terminal_of(&websocket_requests(&mock, "terminal_input")[0]),
            initial,
            "the press that closed the menu must not reach the pane under it"
        );
        assert_eq!(websocket_requests(&mock, "terminal_take_control").len(), 1);
        press(MouseButton::Left, cell_of(other)).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;

        // Hover picks `split right` on the focused pane's menu and enter
        // activates it: the spawn lands beside that pane.
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_page(&["terminal-a", "terminal-b", SPAWNED]),
        );
        press(MouseButton::Right, cell_of(other)).await;
        hover(item_cell(cell_of(other), 1)).await;
        key(KeyCode::Enter).await;
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        timeout(Duration::from_secs(1), async {
            loop {
                let attached = websocket_requests(&mock, "terminal_set_viewport")
                    .iter()
                    .any(|request| request.get("terminal_id") == Some(&json!(SPAWNED)));
                if attached {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the spawned terminal attaches");
        settle_live_event().await;

        // Keys walk the unfocused pane's menu down to `close pane`, its last
        // row, past the clamp: that terminal dies and its slot is reaped.
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_page(&[other, SPAWNED]),
        );
        press(MouseButton::Right, cell_of(&initial)).await;
        for _ in 0..10 {
            key(KeyCode::Down).await;
        }
        key(KeyCode::Enter).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
        assert_eq!(
            terminal_of(&websocket_requests(&mock, "terminal_kill")[0]),
            initial,
            "close pane acts on the pane under the menu, not the focused one"
        );
        wait_for_http_requests(&mock, "GET", "/api/terminals?", 4).await;
        settle_live_event().await;

        // Bare tab-bar space opens the global menu; clicking `reload config`
        // (its seventh row, after `alerts…`) re-reads the prefs file.
        press(MouseButton::Right, bare_cell).await;
        hover(item_cell(bare_cell, 6)).await;
        press(MouseButton::Left, item_cell(bare_cell, 6)).await;
        settle_live_event().await;

        // The tab's menu: `close tab` runs the confirm-close path.
        press(MouseButton::Right, tab_cell).await;
        hover(item_cell(tab_cell, 2)).await;
        press(MouseButton::Left, item_cell(tab_cell, 2)).await;
        settle_live_event().await;
        drop(input_tx);
        (initial, other)
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, (initial, other)) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");
    assert!(chrome.menu.is_none(), "activation closes the menu");
    assert_eq!(chrome.mode, Mode::ConfirmClose, "close tab asks first");
    assert!(
        matches!(
            chrome.dialog,
            Some(Dialog::ConfirmClose {
                target: CloseTarget::Tab,
                ..
            })
        ),
        "the confirm-close dialog targets the tab: {:?}",
        chrome.dialog
    );
    assert_eq!(chrome.prefs.theme, "light", "reload config re-read prefs");
    assert_eq!(
        chrome
            .keymap
            .lookup_prefix(&KeyEvent::new(KeyCode::F(1), KeyModifiers::NONE)),
        Some(gobby_client::ui::Action::Help),
        "reload config re-read the keymap overrides"
    );
    assert!(
        workspace.pane_for_terminal(&initial).is_none(),
        "close pane retired the terminal"
    );
    let tab = &chrome.tabs().tabs[0];
    assert_eq!(tab.slots.len(), 2, "the closed pane's slot was reaped");
    let rect_of = |pane| {
        let slot = tab.slot_for(pane).expect("pane shown in the tab");
        tab.layout
            .panes(area, chrome.tab_focus(tab))
            .into_iter()
            .find(|info| info.id == slot)
            .expect("slot geometry")
            .rect
    };
    let other_rect = rect_of(workspace.pane_for_terminal(other).expect("other pane"));
    let spawned = rect_of(workspace.pane_for_terminal(SPAWNED).expect("spawned pane"));
    assert!(
        spawned.x > other_rect.x && spawned.y == other_rect.y,
        "split right lands beside the menu's pane: {spawned:?} vs {other_rect:?}"
    );
    mock.shutdown().await;
}

/// Deliver one daemon event over the mock socket and wait until the live
/// daemon has broadcast it, so a following drain sees it.
async fn send_daemon_event(mock: &MockDaemon, daemon: &LiveDaemon, event: Value) {
    let (_, mut observed) = daemon.subscribe();
    mock.send_event_and_wait(event).await;
    timeout(Duration::from_secs(1), observed.recv())
        .await
        .expect("daemon event delivery")
        .expect("daemon event");
}

fn sidebar_project_row() -> Value {
    json!([{
        "id": "project-1",
        "name": "gobby",
        "display_name": "gobby",
        "checkout": {"machine_id": "m-local", "root_path": "/repo"},
        "session_count": 1,
        "last_activity_at": null,
    }])
}

fn sidebar_roster_entry(entry_id: &str, run_id: &str, terminal_id: &str) -> Value {
    json!({
        "entry_id": entry_id,
        "run_id": run_id,
        "session_id": null,
        "lifecycle_status": "running",
        "attention": null,
        "task": null,
        "provider": "codex",
        "model": null,
        "terminal": {"terminal_id": terminal_id, "backend": "native"},
        "tmux": null,
        "last_activity_at": null,
    })
}

#[tokio::test]
async fn source_status_failure_does_not_block_sessions_or_worktrees() {
    let mock = MockDaemon::start("local-token").await;
    let mut roster_entry = sidebar_roster_entry("session:session-1", "run-1", "terminal-1");
    roster_entry["session_id"] = json!("session-1");
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-1", "seq": 1, "entries": [roster_entry]}),
    );
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue(
        "GET",
        "/api/source-control/status?",
        503,
        json!({"detail": "git status unavailable"}),
    );
    mock.enqueue(
        "GET",
        "/api/source-control/worktrees?",
        200,
        json!({"worktrees": [{
            "id": "wt-1",
            "project_id": "project-1",
            "branch_name": "feature",
            "worktree_path": "/repo-wt/feature",
            "status": "active",
            "workspace_role": "task",
        }]}),
    );
    mock.enqueue(
        "GET",
        "/api/sessions?project_id=project-1",
        200,
        json!({
            "sessions": [{
                "id": "session-1",
                "ref": "#13923",
                "title": "Restore gclient",
                "status": "active",
                "source": "codex",
            }],
            "count": 1,
            "next_cursor": null,
        }),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("git status is optional during reconcile");

    assert!(workspace.daemon_ready());
    let agent = &workspace.sidebar().agents[0];
    assert_eq!(agent.session_ref.as_deref(), Some("#13923"));
    assert_eq!(agent.name, "Restore gclient");
    let worktrees = &workspace.sidebar().projects[0].worktrees;
    assert_eq!(worktrees.len(), 1);
    assert_eq!(worktrees[0].worktree_id, "wt-1");

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
}

/// 2.1.3: a `worktree_event` or `project_event` on the live socket refetches
/// the affected project's status and worktrees once per drain however many
/// events asked, a `session_event` refetches the attention roster, and an
/// attention refetch drops the roster entries the daemon no longer returns.
#[tokio::test]
async fn sidebar_model_follows_daemon_events() {
    let mock = MockDaemon::start("local-token").await;
    let status_path = "/api/source-control/status?";
    let worktrees_path = "/api/source-control/worktrees?";
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue(
        "GET",
        status_path,
        200,
        json!({"current_branch": "0.5.0", "ahead": 1, "behind": 0, "repo_path": "/repo", "worktree_count": 0}),
    );
    mock.enqueue(
        "GET",
        status_path,
        200,
        json!({"current_branch": "gobby-21986-sidebar", "ahead": 2, "behind": 1, "repo_path": "/repo", "worktree_count": 1}),
    );
    mock.enqueue("GET", worktrees_path, 200, json!({"worktrees": []}));
    mock.enqueue(
        "GET",
        worktrees_path,
        200,
        json!({"worktrees": [{
            "id": "wt-1",
            "project_id": "project-1",
            "branch_name": "gobby-21986-sidebar",
            "worktree_path": "/w/1",
            "status": "active",
            "workspace_role": "task",
        }]}),
    );
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({
            "epoch": "attention-1",
            "seq": 1,
            "entries": [
                sidebar_roster_entry("run:a", "run-a", "terminal-a"),
                sidebar_roster_entry("run:b", "run-b", "terminal-b"),
                sidebar_roster_entry("run:c", "run-c", "terminal-c"),
            ],
        }),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let gets = |path: &str| {
        mock.requests()
            .into_iter()
            .filter(|request| request.method == "GET" && request.target.starts_with(path))
            .count()
    };
    assert_eq!(gets(status_path), 1, "reconcile fetched the status once");
    assert_eq!(gets("/api/attention/roster"), 1);
    assert_eq!(
        gets(worktrees_path),
        1,
        "reconcile fetched the worktrees once"
    );
    let project = &workspace.sidebar().projects[0];
    assert_eq!(project.branch.as_deref(), Some("0.5.0"));
    assert!(project.worktrees.is_empty(), "no worktree before the event");

    for event in [
        json!({"type": "worktree_event", "event": "worktree_created", "project_id": "project-1", "worktree_id": "wt-1"}),
        json!({"type": "worktree_event", "event": "worktree_updated", "project_id": "project-1", "worktree_id": "wt-1"}),
        json!({"type": "project_event", "event": "project_updated", "project_id": "project-1"}),
    ] {
        send_daemon_event(&mock, &daemon, event).await;
    }
    workspace
        .drain_live_events()
        .await
        .expect("drain sidebar events");
    assert_eq!(
        gets(status_path),
        2,
        "three events coalesce into one status refetch"
    );
    assert_eq!(
        gets(worktrees_path),
        2,
        "three events coalesce into one worktrees refetch"
    );
    assert_eq!(
        gets("/api/projects"),
        2,
        "the project event refetched the projects"
    );
    let project = &workspace.sidebar().projects[0];
    assert_eq!(project.branch.as_deref(), Some("gobby-21986-sidebar"));
    assert_eq!((project.ahead, project.behind), (Some(2), Some(1)));
    let worktrees: Vec<&str> = project
        .worktrees
        .iter()
        .map(|worktree| worktree.worktree_id.as_str())
        .collect();
    assert_eq!(
        worktrees,
        ["wt-1"],
        "the refetched worktree joined the project"
    );
    assert_eq!(
        gets("/api/attention/roster"),
        1,
        "project and worktree events leave the roster alone"
    );

    // An ended agent run fires no attention event; its session expiring
    // is what refetches the roster, and the roster no longer lists it.
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({
            "epoch": "attention-1",
            "seq": 2,
            "entries": [
                sidebar_roster_entry("run:a", "run-a", "terminal-a"),
                sidebar_roster_entry("run:c", "run-c", "terminal-c"),
            ],
        }),
    );
    send_daemon_event(
        &mock,
        &daemon,
        json!({"type": "session_event", "event": "session_expired", "project_id": "project-1", "session_id": "session-b"}),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("drain session event");
    assert_eq!(gets("/api/attention/roster"), 2);
    assert_eq!(
        workspace.attention_entry_ids(),
        ["run:a", "run:c"],
        "the session event refetched the roster and dropped the ended run"
    );

    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({
            "epoch": "attention-2",
            "seq": 1,
            "entries": [sidebar_roster_entry("run:a", "run-a", "terminal-a")],
        }),
    );
    send_daemon_event(
        &mock,
        &daemon,
        json!({
            "type": "agent_event",
            "event": "attention_changed",
            "epoch": "attention-2",
            "seq": 1,
            "entry_id": "run:a",
            "state": "blocked",
            "attention_id": "att-1",
            "kind": "actionable",
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("drain attention event");
    assert_eq!(
        workspace.attention_entry_ids(),
        ["run:a"],
        "the attention refetch dropped the entry the daemon no longer returns"
    );
    let agents: Vec<&str> = workspace
        .sidebar()
        .agents
        .iter()
        .map(|agent| agent.terminal_id.as_str())
        .collect();
    assert_eq!(
        agents,
        ["terminal-a"],
        "the sidebar model followed the roster"
    );

    // An attention event within the epoch carries only the attention; the
    // refetch it queues brings the lifecycle_status the glyph is drawn from.
    let mut waiting = sidebar_roster_entry("run:a", "run-a", "terminal-a");
    waiting["lifecycle_status"] = json!("awaiting_input");
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-2", "seq": 2, "entries": [waiting]}),
    );
    send_daemon_event(
        &mock,
        &daemon,
        json!({
            "type": "agent_event",
            "event": "attention_changed",
            "epoch": "attention-2",
            "seq": 2,
            "entry_id": "run:a",
            "state": "clear",
        }),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("drain attention event");
    assert_eq!(
        workspace.sidebar().agents[0].attention,
        None,
        "the event cleared the prompt"
    );
    assert_eq!(
        gets("/api/attention/roster"),
        4,
        "the attention event queued a roster refetch"
    );
    assert_eq!(
        workspace.sidebar().agents[0].lifecycle_status.as_deref(),
        Some("awaiting_input"),
        "the refetch reconciled the lifecycle status"
    );
    assert_eq!(workspace.sidebar().agents[0].state, RowState::Paused);

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
}

/// 1.3: the roster refetch has a periodic backstop, so a missed event cannot
/// leave a glyph stale for longer than `ROSTER_REFRESH_INTERVAL`.
#[tokio::test]
async fn roster_refresh_backstop_refetches_on_the_interval() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let roster_gets = || {
        mock.requests()
            .into_iter()
            .filter(|request| {
                request.method == "GET" && request.target.starts_with("/api/attention/roster")
            })
            .count()
    };
    assert_eq!(roster_gets(), 1, "reconcile fetched the roster once");

    workspace.request_roster_refresh_if_due();
    assert!(
        workspace.start_sidebar_refetch().is_none(),
        "nothing is due right after a fetch"
    );

    tokio::time::pause();
    tokio::time::advance(ROSTER_REFRESH_INTERVAL).await;
    tokio::time::resume();
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({"epoch": "attention-1", "seq": 2, "entries": []}),
    );
    workspace.request_roster_refresh_if_due();
    let job = workspace
        .start_sidebar_refetch()
        .expect("the interval passed");
    assert!(
        workspace.start_sidebar_refetch().is_none(),
        "a started refetch is not queued twice"
    );
    job.await.expect("roster refetched");
    assert_eq!(roster_gets(), 2);

    workspace.request_roster_refresh_if_due();
    assert!(
        workspace.start_sidebar_refetch().is_none(),
        "the clock restarted with the refetch"
    );

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
}

fn sidebar_two_project_rows() -> Value {
    json!([
        {
            "id": "project-1",
            "name": "gobby",
            "display_name": "gobby",
            "checkout": {"machine_id": "m-local", "root_path": "/repo"},
            "session_count": 1,
            "last_activity_at": null,
        },
        {
            "id": "project-2",
            "name": "other",
            "display_name": "other",
            "checkout": {"machine_id": "m-local", "root_path": "/other"},
            "session_count": 1,
            "last_activity_at": null,
        },
    ])
}

/// A session event names the project whose session changed, and only that
/// project's sessions and runs are refetched. An event that names none —
/// a deleted session leaves no row to read it from — still sweeps every
/// tracked project, which is what every session event used to cost.
#[tokio::test]
async fn a_named_session_event_refetches_only_its_project() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue("GET", "/api/projects", 200, sidebar_two_project_rows());
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let gets = |path: &str| {
        mock.requests()
            .into_iter()
            .filter(|request| request.method == "GET" && request.target.starts_with(path))
            .count()
    };
    let sessions_of = |project: &str| format!("/api/sessions?project_id={project}");
    let runs_of = |project: &str| format!("/api/agents/runs?project_id={project}");
    assert_eq!(
        (gets(&sessions_of("project-1")), gets(&runs_of("project-1"))),
        (1, 1),
        "reconcile fetched the first project's rows once"
    );
    assert_eq!(
        (gets(&sessions_of("project-2")), gets(&runs_of("project-2"))),
        (1, 1),
        "reconcile fetched the second project's rows once"
    );

    send_daemon_event(
        &mock,
        &daemon,
        json!({"type": "session_event", "event": "session_updated", "project_id": "project-2", "session_id": "session-b"}),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("drain the named session event");
    assert_eq!(
        (gets(&sessions_of("project-1")), gets(&runs_of("project-1"))),
        (1, 1),
        "the project the event did not name was left alone"
    );
    assert_eq!(
        (gets(&sessions_of("project-2")), gets(&runs_of("project-2"))),
        (2, 2),
        "the named project was refetched"
    );

    send_daemon_event(
        &mock,
        &daemon,
        json!({"type": "session_event", "event": "session_deleted", "session_id": "session-b"}),
    )
    .await;
    workspace
        .drain_live_events()
        .await
        .expect("drain the unnamed session event");
    assert_eq!(
        (gets(&sessions_of("project-1")), gets(&runs_of("project-1"))),
        (2, 2),
        "an event naming no project swept the first project too"
    );
    assert_eq!(
        (gets(&sessions_of("project-2")), gets(&runs_of("project-2"))),
        (3, 3),
        "and the second"
    );

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
}

/// The git refresh is a job: nothing starts before the interval, a run
/// that fails reports its error and is not retried before the next
/// interval, and a run that succeeds changes the sidebar only when applied.
#[tokio::test]
async fn git_refresh_runs_as_a_deferred_job() {
    let mock = MockDaemon::start("local-token").await;
    let status_path = "/api/source-control/status?";
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let gets = |path: &str| {
        mock.requests()
            .into_iter()
            .filter(|request| request.method == "GET" && request.target.starts_with(path))
            .count()
    };
    assert_eq!(gets(status_path), 1, "reconcile fetched the status once");

    workspace.request_git_refresh_if_due();
    assert!(
        workspace.start_sidebar_refetch().is_none(),
        "nothing is due right after a fetch"
    );

    tokio::time::pause();
    tokio::time::advance(GIT_REFRESH_INTERVAL).await;
    tokio::time::resume();
    mock.enqueue("GET", status_path, 503, json!({"detail": "git is busy"}));
    workspace.request_git_refresh_if_due();
    let job = workspace
        .start_sidebar_refetch()
        .expect("the interval passed");
    assert!(
        workspace.start_sidebar_refetch().is_none(),
        "a started refresh is not queued twice"
    );
    let fetch = job.await.expect("git status is optional");
    workspace.apply_sidebar_fetch(fetch);
    assert_eq!(
        workspace.sidebar().projects[0].branch,
        None,
        "the unavailable status leaves its previous value alone"
    );
    assert_eq!(gets(status_path), 2);

    tokio::time::pause();
    tokio::time::advance(GIT_REFRESH_INTERVAL / 2).await;
    tokio::time::resume();
    workspace.request_git_refresh_if_due();
    assert!(
        workspace.start_sidebar_refetch().is_none(),
        "a partial refresh waits out the interval"
    );

    tokio::time::pause();
    tokio::time::advance(GIT_REFRESH_INTERVAL / 2).await;
    tokio::time::resume();
    mock.enqueue(
        "GET",
        status_path,
        200,
        json!({"current_branch": "gobby-22160-tick", "ahead": 3, "behind": 0, "repo_path": "/repo", "worktree_count": 0}),
    );
    workspace.request_git_refresh_if_due();
    let job = workspace
        .start_sidebar_refetch()
        .expect("the next interval passed");
    let fetch = job.await.expect("status refetched");
    assert_eq!(
        workspace.sidebar().projects[0].branch.as_deref(),
        None,
        "the rows land only when applied"
    );
    workspace.apply_sidebar_fetch(fetch);
    assert_eq!(
        workspace.sidebar().projects[0].branch.as_deref(),
        Some("gobby-22160-tick")
    );
    assert_eq!(gets(status_path), 3);

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
}

/// A refetch started beside the loop that lands after an inline refetch
/// of the same rows is stale: the inline rows stay, and the next refetch
/// still lands.
#[tokio::test]
async fn a_late_background_refetch_leaves_the_inline_rows_in_place() {
    let mock = MockDaemon::start("local-token").await;
    let status_path = "/api/source-control/status?";
    let status = |branch: &str| json!({"current_branch": branch, "ahead": 0, "behind": 0, "repo_path": "/repo", "worktree_count": 0});
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    mock.wait_for_websocket().await;
    let mut workspace = Workspace::live(daemon.clone());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");

    tokio::time::pause();
    tokio::time::advance(GIT_REFRESH_INTERVAL).await;
    tokio::time::resume();
    workspace.request_git_refresh_if_due();
    let job = workspace
        .start_sidebar_refetch()
        .expect("the interval passed");

    // The inline refetch runs first and reads the current branch; the job,
    // not yet polled, then reads the reply queued behind it.
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue("GET", status_path, 200, status("fresh"));
    mock.enqueue("GET", status_path, 200, status("stale"));
    workspace
        .fetch_sidebar_rows()
        .await
        .expect("inline refetch");
    assert_eq!(
        workspace.sidebar().projects[0].branch.as_deref(),
        Some("fresh")
    );

    let fetch = job.await.expect("the late job lands");
    workspace.apply_sidebar_fetch(fetch);
    assert_eq!(
        workspace.sidebar().projects[0].branch.as_deref(),
        Some("fresh"),
        "the late job is stale"
    );

    tokio::time::pause();
    tokio::time::advance(GIT_REFRESH_INTERVAL).await;
    tokio::time::resume();
    mock.enqueue("GET", status_path, 200, status("newer"));
    workspace.request_git_refresh_if_due();
    let fetch = workspace
        .start_sidebar_refetch()
        .expect("the next interval passed")
        .await
        .expect("status refetched");
    workspace.apply_sidebar_fetch(fetch);
    assert_eq!(
        workspace.sidebar().projects[0].branch.as_deref(),
        Some("newer"),
        "a newer job still lands"
    );

    daemon
        .close(Instant::now() + Duration::from_secs(1))
        .await
        .expect("close live daemon");
    mock.shutdown().await;
}

/// The render tick starts the due git refresh beside the loop and applies
/// it when it lands, without a drain or a reconcile.
#[tokio::test]
async fn the_render_tick_applies_a_background_git_refresh() {
    let mock = MockDaemon::start("local-token").await;
    let status_path = "/api/source-control/status?";
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue(
        "GET",
        status_path,
        200,
        json!({"current_branch": "0.5.0", "ahead": 0, "behind": 0, "repo_path": "/repo", "worktree_count": 0}),
    );
    // Every later refresh answers with the new branch, however many ticks
    // run before the loop exits.
    for _ in 0..8 {
        mock.enqueue(
            "GET",
            status_path,
            200,
            json!({"current_branch": "gobby-22160-tick", "ahead": 3, "behind": 0, "repo_path": "/repo", "worktree_count": 0}),
        );
    }
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            terminal_page(&["terminal-a"]),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["terminal-a"]);
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_http_requests(&mock, "GET", status_path, 1).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        tokio::time::pause();
        tokio::time::advance(GIT_REFRESH_INTERVAL + RENDER_TICK * 2).await;
        tokio::time::resume();
        wait_for_http_requests(&mock, "GET", status_path, 2).await;
        settle_live_event().await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        workspace.sidebar().projects[0].branch.as_deref(),
        Some("gobby-22160-tick"),
        "the tick's refresh reached the sidebar"
    );
    mock.shutdown().await;
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

/// The terminal in each tab's focused slot, in tab order.
fn shown_terminals(workspace: &Workspace<LiveDaemon>, chrome: &Chrome) -> Vec<String> {
    chrome
        .tabs()
        .tabs
        .iter()
        .filter_map(|tab| chrome.viewer.focused_pane(tab))
        .map(|pane| workspace.pane(pane).terminal_id.clone())
        .collect()
}

/// 2.2.1: against eight roster terminals and no snapshot, the loop opens one
/// tab holding one shell it spawned into the project checkout; the eight are
/// agent rows only and never panes.
#[tokio::test]
async fn first_run_opens_one_shell_and_never_auto_opens() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    let roster: Vec<String> = (1..=8).map(|n| format!("terminal-{n}")).collect();
    let ids: Vec<&str> = roster.iter().map(String::as_str).collect();
    let mut relisted = ids.clone();
    relisted.push(SPAWNED);
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&ids));
    mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&relisted));
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        200,
        json!({
            "epoch": "attention-1",
            "seq": 1,
            "entries": ids
                .iter()
                .enumerate()
                .map(|(n, id)| sidebar_roster_entry(&format!("run:{n}"), &format!("run-{n}"), id))
                .collect::<Vec<_>>(),
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let home = tempfile::tempdir().expect("gobby home");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(8);
    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        wait_for_http_requests(&mock, "GET", "/api/terminals?", 2).await;
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
    result.expect("first-run live loop");

    assert_eq!(shown_terminals(&workspace, &chrome), [SPAWNED]);
    assert_eq!(
        chrome.tabs().tabs[0].slots.len(),
        1,
        "one slot in the one tab"
    );
    let creates = websocket_requests(&mock, "terminal_create");
    assert_eq!(creates.len(), 1);
    assert_eq!(creates[0].get("cwd"), Some(&json!("/repo")));
    assert_eq!(creates[0].get("project_id"), Some(&json!("project-1")));
    let agents: Vec<&str> = workspace
        .sidebar()
        .agents
        .iter()
        .map(|agent| agent.terminal_id.as_str())
        .collect();
    for id in &ids {
        assert!(agents.contains(id), "{id} is an agent row: {agents:?}");
        let pane = workspace.pane_for_terminal(id).expect("roster pane");
        assert!(
            !chrome
                .tabs()
                .tabs
                .iter()
                .any(|tab| tab.slots.values().any(|shown| *shown == pane)),
            "{id} was auto-opened"
        );
    }
    mock.shutdown().await;
}

#[derive(Debug, Clone, Copy)]
enum ExplicitActivation {
    SidebarClick,
    Navigator,
    Goto,
    MenuFocus,
    MenuNewTab,
}

impl ExplicitActivation {
    fn starts_with_pane(self) -> bool {
        matches!(self, Self::Navigator | Self::Goto)
    }
}

async fn activate_daemon_hosted_terminal(
    input: &mpsc::Sender<RawInputEvent>,
    path: ExplicitActivation,
    agent_cell: (u16, u16),
) {
    match path {
        ExplicitActivation::SidebarClick => {
            send_mouse(
                input,
                MouseEventKind::Down(MouseButton::Left),
                agent_cell.0,
                agent_cell.1,
                KeyModifiers::NONE,
            )
            .await;
        }
        ExplicitActivation::Navigator => {
            send_chord(input, KeyCode::Char('w'), KeyModifiers::NONE).await;
            send_key(input, KeyCode::Down, KeyModifiers::NONE).await;
            send_key(input, KeyCode::Enter, KeyModifiers::NONE).await;
        }
        ExplicitActivation::Goto => {
            send_chord(input, KeyCode::Char('g'), KeyModifiers::NONE).await;
            // Goto filters on the row name, which is the terminal's command;
            // the hosted terminal runs `codex`, the shown pane is a bare shell.
            for ch in "codex".chars() {
                send_key(input, KeyCode::Char(ch), KeyModifiers::NONE).await;
            }
            send_key(input, KeyCode::Enter, KeyModifiers::NONE).await;
        }
        ExplicitActivation::MenuFocus | ExplicitActivation::MenuNewTab => {
            send_mouse(
                input,
                MouseEventKind::Down(MouseButton::Right),
                agent_cell.0,
                agent_cell.1,
                KeyModifiers::NONE,
            )
            .await;
            if matches!(path, ExplicitActivation::MenuNewTab) {
                send_key(input, KeyCode::Down, KeyModifiers::NONE).await;
            }
            send_key(input, KeyCode::Enter, KeyModifiers::NONE).await;
        }
    }
}

async fn assert_daemon_hosted_activation(path: ExplicitActivation) -> usize {
    const SHOWN: &str = "client-terminal";
    const HOSTED: &str = "daemon-hosted-terminal";
    const ENTRY: &str = "run:hosted";
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let roster = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [sidebar_roster_entry(ENTRY, "run-hosted", HOSTED)],
    });
    for _ in 0..4 {
        mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    }
    for _ in 0..2 {
        let ids = if path.starts_with_pane() {
            vec![SHOWN, HOSTED]
        } else {
            vec![SHOWN]
        };
        let mut page = terminal_page(&ids);
        for item in page["items"].as_array_mut().expect("terminal items") {
            if item["terminal_id"] == HOSTED {
                item["command"] = json!("codex");
            }
        }
        mock.enqueue("GET", "/api/terminals?", 200, page);
        mock.enqueue("GET", "/api/attention/roster", 200, roster.clone());
    }
    if !path.starts_with_pane() {
        mock.enqueue(
            "GET",
            &format!("/api/terminals/{HOSTED}"),
            200,
            json!({"terminal_id": HOSTED, "backend": "native", "state": "live"}),
        );
    }

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial rows");
    assert_eq!(
        workspace.pane_for_terminal(HOSTED).is_some(),
        path.starts_with_pane(),
        "{path:?} starts in the intended pane state"
    );

    let shown = workspace.pane_for_terminal(SHOWN).expect("shown pane");
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(shown, workspace.pane(shown).display_name());
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let agent_cell = probe
        .view
        .agent_hit_areas
        .iter()
        .find(|(entry, _)| entry == ENTRY)
        .map(|(_, rect)| (rect.x + 1, rect.y))
        .expect("daemon-hosted row drawn");

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.open_pane(shown, workspace.pane(shown).display_name());
    let (input_tx, input_rx) = mpsc::channel(32);
    let driver = async {
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 2).await;
        activate_daemon_hosted_terminal(&input_tx, path, agent_cell).await;
        timeout(Duration::from_secs(1), async {
            loop {
                let shown = websocket_requests(&mock, "terminal_set_viewport")
                    .iter()
                    .any(|request| request.get("terminal_id") == Some(&json!(HOSTED)));
                if shown {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("{path:?} did not reveal the daemon-hosted pane"));
        activate_daemon_hosted_terminal(&input_tx, path, agent_cell).await;
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
    result.expect("live loop exits cleanly");

    let pane = workspace
        .pane_for_terminal(HOSTED)
        .unwrap_or_else(|| panic!("{path:?} opens the daemon-hosted terminal"));
    assert_eq!(chrome.focused_pane(), Some(pane));
    let shown_count = chrome
        .tabs()
        .tabs
        .iter()
        .flat_map(|tab| tab.slots.values())
        .filter(|candidate| **candidate == pane)
        .count();
    assert_eq!(shown_count, 1, "{path:?} keeps one pane slot");
    if matches!(path, ExplicitActivation::SidebarClick) {
        assert_eq!(chrome.tabs().tabs.len(), 2, "the opened pane gets a tab");
        assert_eq!(chrome.active_index(), 1, "the opened tab is active");
    }
    let attaches = websocket_requests(&mock, "terminal_attach")
        .into_iter()
        .filter(|request| request.get("terminal_id") == Some(&json!(HOSTED)))
        .count();
    assert_eq!(attaches, 1, "{path:?} attaches once");
    let takes = websocket_requests(&mock, "terminal_take_control")
        .into_iter()
        .filter(|request| request.get("terminal_id") == Some(&json!(HOSTED)))
        .count();
    assert_eq!(
        takes, 1,
        "{path:?} takes control once across repeat activation"
    );
    assert_eq!(
        websocket_requests(&mock, "terminal_create").len(),
        0,
        "{path:?} attaches instead of spawning"
    );
    let terminal_gets = mock
        .requests()
        .into_iter()
        .filter(|request| {
            request.method == "GET" && request.target == format!("/api/terminals/{HOSTED}")
        })
        .count();
    assert_eq!(
        terminal_gets,
        usize::from(!path.starts_with_pane()),
        "{path:?} resolves the terminal once"
    );
    mock.shutdown().await;
    attaches
}

#[tokio::test]
async fn explicit_activation_opens_daemon_hosted_terminal_without_spawning() {
    let mut attached = 0;
    for path in [
        ExplicitActivation::SidebarClick,
        ExplicitActivation::Navigator,
        ExplicitActivation::Goto,
        ExplicitActivation::MenuFocus,
        ExplicitActivation::MenuNewTab,
    ] {
        attached += assert_daemon_hosted_activation(path).await;
    }
    assert_eq!(attached, 5, "every explicit activation path attaches once");
    assert_agent_row_click_activates_tab_showing_existing_pane().await;
}

async fn assert_agent_row_click_activates_tab_showing_existing_pane() {
    const SHOWN: &str = "terminal-shown";
    const TABBED: &str = "terminal-tabbed";
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let roster = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [sidebar_roster_entry("run:tabbed", "run-tabbed", TABBED)],
    });
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            terminal_page(&[SHOWN, TABBED]),
        );
        mock.enqueue("GET", "/api/attention/roster", 200, roster.clone());
    }

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial rows");
    let pane_of = |terminal_id| {
        workspace
            .pane_for_terminal(terminal_id)
            .unwrap_or_else(|| panic!("pane for {terminal_id}"))
    };
    let (shown, tabbed) = (pane_of(SHOWN), pane_of(TABBED));

    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.open_pane(shown, SHOWN);
    probe.open_tab(tabbed, TABBED);
    probe.activate_tab(0);
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let row_cell = |entry_id: &str| {
        probe
            .view
            .agent_hit_areas
            .iter()
            .find(|(entry, _)| entry == entry_id)
            .map(|(_, rect)| (rect.x + 1, rect.y))
            .unwrap_or_else(|| panic!("row {entry_id} drawn"))
    };
    let tabbed_cell = row_cell("run:tabbed");

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.open_pane(shown, SHOWN);
    chrome.open_tab(tabbed, TABBED);
    chrome.activate_tab(0);
    let (input_tx, input_rx) = mpsc::channel(32);
    let driver = async {
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 2).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let before = websocket_requests(&mock, "terminal_take_control").len();
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            tabbed_cell.0,
            tabbed_cell.1,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_websocket_requests(&mock, "terminal_take_control", before + 1).await;
        drop(input_tx);
        before
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, before) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("live loop exits cleanly");

    let targets: Vec<String> = websocket_requests(&mock, "terminal_take_control")
        .into_iter()
        .map(|request| {
            request
                .get("terminal_id")
                .and_then(Value::as_str)
                .expect("take-control target")
                .to_string()
        })
        .collect();
    assert_eq!(&targets[before..], [TABBED]);
    assert_eq!(chrome.tabs().tabs.len(), 2, "no duplicate tab opens");
    assert_eq!(chrome.active_index(), 1, "the existing tab becomes active");
    assert_eq!(chrome.focused_pane(), Some(tabbed));
    mock.shutdown().await;
}

/// 2.2.2: two projects keep separate tab sets. Focusing the second swaps
/// the tab bar (seeded with a shell in its checkout), a tab opened from its
/// agent row lands in its set, and refocusing the first restores its tabs
/// and active tab from the snapshot the switch saved.
#[tokio::test]
async fn tab_sets_follow_the_focused_project() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    let projects = json!([
        {"id": "project-1", "name": "one", "display_name": "one",
         "checkout": {"machine_id": "m-local", "root_path": "/repo"}},
        {"id": "project-2", "name": "two", "display_name": "two",
         "checkout": {"machine_id": "m-local", "root_path": "/repo2"}},
    ]);
    for _ in 0..3 {
        mock.enqueue("GET", "/api/projects", 200, projects.clone());
    }
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-a1", "terminal-a2"]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-b1"]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-b1", SPAWNED]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        terminal_page(&["terminal-a1", "terminal-a2"]),
    );
    // 4.2: project-1's two tabs are rows of the attached workspace.
    mock.seed_workspace(
        "project-1",
        &[
            (&["terminal-a1"], "terminal-a1"),
            (&["terminal-a2"], "terminal-a2"),
        ],
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let home = tempfile::tempdir().expect("gobby home");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        shown_terminals(&workspace, &chrome),
        ["terminal-a1", "terminal-a2"]
    );
    assert!(chrome.activate_tab(1), "the second tab is activated");

    focus_project(&mut workspace, &mut chrome, "project-2")
        .await
        .expect("focus project-2");
    // The daemon's tab.created event places the shell: apply it and project
    // the row the way the loop does between actions.
    workspace
        .drain_live_events()
        .await
        .expect("apply the tab.created event");
    sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(chrome.project_tabs.focused.as_deref(), Some("project-2"));
    assert_eq!(workspace.project_id(), Some("project-2"));
    assert_eq!(
        shown_terminals(&workspace, &chrome),
        [SPAWNED],
        "a project without tabs is seeded with one shell"
    );
    let creates = websocket_requests(&mock, "terminal_create");
    assert_eq!(creates.len(), 1);
    assert_eq!(creates[0].get("cwd"), Some(&json!("/repo2")));
    // A second project-2 tab arrives from the daemon, as another window's
    // would.
    let workspace_id = workspace
        .workspace_model()
        .expect("attached workspace")
        .workspace
        .id
        .clone();
    daemon
        .workspace_op(WorkspaceOp::TabCreate {
            workspace: workspace_id,
            project_id: "project-2".into(),
            worktree_id: None,
            title: None,
            terminal_id: Some("terminal-b1".into()),
            node: None,
        })
        .await
        .expect("tab.create for terminal-b1");
    workspace
        .drain_live_events()
        .await
        .expect("apply the second tab.created event");
    sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(chrome.project_tabs.sets["project-2"].tabs.len(), 2);

    focus_project(&mut workspace, &mut chrome, "project-1")
        .await
        .expect("focus project-1");
    assert_eq!(chrome.project_tabs.focused.as_deref(), Some("project-1"));
    assert_eq!(
        shown_terminals(&workspace, &chrome),
        ["terminal-a1", "terminal-a2"]
    );
    assert_eq!(chrome.active_index(), 1, "the viewer's active tab is kept");
    assert_eq!(websocket_requests(&mock, "terminal_create").len(), 1);
    assert_eq!(
        chrome.project_tabs.sets["project-2"].tabs.len(),
        2,
        "project-2's tabs stay parked"
    );
    mock.shutdown().await;
}

/// Plan 2.3.1: after startup and after a slot change every shown pane, tmux
/// or native, held or observing, carries the geometry of its inner rect: a
/// `SetViewport` plus a `terminal_resize` claiming `viewer: "gclient"`.
#[tokio::test]
async fn every_shown_pane_sizes_its_terminal() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let page = |terminals: &[(&str, &str)]| {
        let items: Vec<Value> = terminals
            .iter()
            .map(|(id, backend)| json!({"terminal_id": id, "backend": backend, "state": "live"}))
            .collect();
        json!({
            "items": items,
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-size", "seq": 1}
        })
    };
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        page(&[("term-native", "native"), ("term-tmux", "tmux")]),
    );
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        page(&[
            ("term-native", "native"),
            ("term-tmux", "tmux"),
            (SPAWNED, "native"),
        ]),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(
        &mock,
        &mut workspace,
        "project-1",
        &["term-native", "term-tmux"],
    );
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        wait_for_websocket_requests(&mock, "terminal_resize", 2).await;
        let sized_at_startup: Vec<Value> = websocket_requests(&mock, "terminal_resize")
            .iter()
            .filter_map(|request| request.get("terminal_id").cloned())
            .collect();
        assert!(
            sized_at_startup.contains(&json!("term-native")),
            "startup sizes the native pane: {sized_at_startup:?}"
        );
        assert!(
            sized_at_startup.contains(&json!("term-tmux")),
            "startup sizes the tmux pane: {sized_at_startup:?}"
        );
        send_chord(&input_tx, KeyCode::Char('-'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        timeout(Duration::from_secs(1), async {
            loop {
                let sized = websocket_requests(&mock, "terminal_resize")
                    .iter()
                    .any(|request| request.get("terminal_id") == Some(&json!(SPAWNED)));
                if sized {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the spawned pane is sized once its slot opens");
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
    result.expect("sizing loop");

    let tab = chrome.active_tab().expect("active tab");
    assert_eq!(tab.slots.len(), 3, "the split opened a third slot");
    let viewports = websocket_requests(&mock, "terminal_set_viewport");
    let resizes = websocket_requests(&mock, "terminal_resize");
    assert!(
        resizes
            .iter()
            .all(|request| request.get("viewer") == Some(&json!("gclient"))),
        "every size claim names the gclient viewer: {resizes:?}"
    );
    for info in &chrome.view.pane_infos {
        let pane = workspace.pane(tab.slots[&info.id]);
        let rows = u64::from(info.inner_rect.height);
        let cols = u64::from(info.inner_rect.width);
        let latest = |requests: &[Value], kind: &str| -> Value {
            requests
                .iter()
                .rev()
                .find(|request| request.get("terminal_id") == Some(&json!(pane.terminal_id)))
                .cloned()
                .unwrap_or_else(|| panic!("{} received no {kind}", pane.terminal_id))
        };
        let viewport = latest(&viewports, "terminal_set_viewport");
        assert_eq!(
            (
                viewport.get("rows").and_then(Value::as_u64),
                viewport.get("cols").and_then(Value::as_u64),
            ),
            (Some(rows), Some(cols)),
            "{} viewport follows its inner rect",
            pane.terminal_id
        );
        let resize = latest(&resizes, "terminal_resize");
        assert_eq!(
            (
                resize.get("rows").and_then(Value::as_u64),
                resize.get("cols").and_then(Value::as_u64),
            ),
            (Some(rows), Some(cols)),
            "{} size claim follows its inner rect",
            pane.terminal_id
        );
        assert_eq!(
            resize.get("attachment_id").and_then(Value::as_str),
            Some(pane.attachment_id()),
            "{} claims on its live attachment",
            pane.terminal_id
        );
        assert_eq!(
            pane.viewport(),
            (info.inner_rect.height, info.inner_rect.width)
        );
    }
    let tmux = workspace.pane_for_terminal("term-tmux").expect("tmux pane");
    let native = workspace
        .pane_for_terminal("term-native")
        .expect("native pane");
    assert!(
        workspace.pane(tmux).is_held(),
        "the focused tmux pane holds control"
    );
    assert!(
        workspace.pane(native).is_observe(),
        "the native pane observes"
    );
    mock.shutdown().await;
}

/// Draw through the loop's render path and return each pane's body text,
/// rows joined by newlines.
fn draw_pane_bodies(
    terminal: &mut Terminal<TestBackend>,
    ws: &Workspace,
    chrome: &mut Chrome,
    panes: &[gobby_client::app::PaneId],
) -> Vec<String> {
    terminal
        .draw(|frame| {
            chrome.compute_view(ws, frame.area());
            let focused = chrome.focused_pane();
            let mut content =
                |frame: &mut ratatui::Frame<'_>, area: Rect, pane: gobby_client::app::PaneId| {
                    gobby_client::views::grid::render(
                        frame,
                        area,
                        ws.pane(pane),
                        focused == Some(pane),
                    );
                };
            gobby_client::ui::render_workspace_with(frame, ws, chrome, &mut content);
        })
        .expect("draw the workspace");
    let tab = chrome.active_tab().expect("active tab");
    let buffer = terminal.backend().buffer();
    panes
        .iter()
        .map(|pane| {
            let rect = chrome
                .view
                .pane_infos
                .iter()
                .find(|info| tab.slots.get(&info.id) == Some(pane))
                .expect("shown pane")
                .inner_rect;
            (rect.y..rect.y + rect.height)
                .map(|y| {
                    (rect.x..rect.x + rect.width)
                        .map(|x| buffer[(x, y)].symbol())
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join("\n")
        })
        .collect()
}

/// Plan 2.3.2: two panes fed by two sources both paint through the shared
/// render path, and a body that cannot be painted names the reason.
#[tokio::test]
async fn both_panes_render_and_report_size_owner() {
    let mut ws = Workspace::scripted();
    let left = ws
        .open_terminal("term-left", "native", "epoch-render")
        .expect("left pane");
    let right = ws
        .open_terminal("term-right", "native", "epoch-render")
        .expect("right pane");
    let ServerMessage::Frame(mut short) = semantic_frame("ALPHA") else {
        unreachable!("semantic_frame builds a frame");
    };
    short.cells.truncate(3);
    let mut left_source = ScriptedFrameSource::new(Transport::Direct);
    left_source.queue(semantic_frame("ALPHA"));
    left_source.queue(ServerMessage::Frame(short));
    left_source.queue(semantic_frame("ALPHA"));
    ws.replace_frame_source(left, PaneFrameSource::Scripted(left_source))
        .expect("left source");
    let mut right_source = ScriptedFrameSource::new(Transport::Direct);
    right_source.queue(semantic_frame("BRAVO"));
    ws.replace_frame_source(right, PaneFrameSource::Scripted(right_source))
        .expect("right source");
    let mut chrome = Chrome::dark();
    chrome.open_pane(left, "left");
    chrome.open_pane(right, "right");
    let mut terminal = Terminal::new(TestBackend::new(120, 30)).expect("test terminal");
    let panes = [left, right];

    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    assert!(
        bodies[0].contains("waiting for frames"),
        "a frameless live pane says so: {bodies:?}"
    );
    assert!(
        bodies[1].contains("waiting for frames"),
        "both frameless panes say so: {bodies:?}"
    );

    ws.recv_pane_frame(left).await.expect("left frame");
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    assert!(
        bodies[0].contains("ALPHA"),
        "the left body paints after its pump: {bodies:?}"
    );
    assert!(
        bodies[1].contains("waiting for frames"),
        "the right body still waits: {bodies:?}"
    );

    ws.recv_pane_frame(right).await.expect("right frame");
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    assert!(
        bodies[0].contains("ALPHA"),
        "the left body keeps its frame: {bodies:?}"
    );
    assert!(
        bodies[1].contains("BRAVO"),
        "the right body paints after its pump: {bodies:?}"
    );

    ws.recv_pane_frame(left).await.expect("short frame");
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    assert!(
        bodies[0].contains("frame_size_mismatch 5x1/3"),
        "a malformed frame names its dimensions and cell count: {bodies:?}"
    );
    assert!(
        bodies[1].contains("BRAVO"),
        "the right body is untouched by the left mismatch: {bodies:?}"
    );

    let left_attachment = ws.pane(left).attachment_id().to_string();
    ws.apply_ws(&json!({
        "type": "terminal_resize_result",
        "attachment_id": left_attachment,
        "applied": false,
        "owner_viewer": "web",
    }))
    .expect("resize result");
    ws.recv_pane_frame(left).await.expect("left frame again");
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    let left_rows: Vec<&str> = bodies[0].lines().collect();
    assert!(
        left_rows.first().is_some_and(|row| row.contains("ALPHA")),
        "the crop keeps the frame at its origin: {bodies:?}"
    );
    assert!(
        left_rows
            .last()
            .is_some_and(|row| row.contains("sized by web")),
        "the note names the owning viewer: {bodies:?}"
    );
    assert!(
        !bodies[1].contains("sized by"),
        "the right pane keeps its own sizing: {bodies:?}"
    );
}

/// A `width`x`height` frame with every cell painted `glyph`.
fn filled_frame(width: u16, height: u16, glyph: char) -> ServerMessage {
    let text: String = std::iter::repeat_n(glyph, usize::from(width)).collect();
    let ServerMessage::Frame(mut frame) = semantic_frame(&text) else {
        unreachable!("semantic_frame builds a frame");
    };
    let row = frame.cells.clone();
    frame.cells = (0..height).flat_map(|_| row.clone()).collect();
    frame.height = height;
    ServerMessage::Frame(frame)
}

/// The inner rect the last draw gave `pane`.
fn inner_rect_of(chrome: &Chrome, pane: gobby_client::app::PaneId) -> Rect {
    let tab = chrome.active_tab().expect("active tab");
    chrome
        .view
        .pane_infos
        .iter()
        .find(|info| tab.slots.get(&info.id) == Some(&pane))
        .expect("shown pane")
        .inner_rect
}

/// Every row of `body` carries `glyph` exactly `width` times on the first
/// `height` rows and nothing at all on the rest.
fn assert_body_is_exactly(body: &str, width: usize, height: usize, glyph: char) {
    for (y, row) in body.lines().enumerate() {
        let (glyphs, painted) = if y < height { (width, width) } else { (0, 0) };
        assert_eq!(
            row.matches(glyph).count(),
            glyphs,
            "row {y} of the {width}x{height} frame: {row:?}"
        );
        assert_eq!(
            row.trim_end_matches(' ').chars().count(),
            painted,
            "row {y} carries cells outside the {width}x{height} frame: {row:?}"
        );
    }
}

/// Plan 6.1.1: a frame that shrinks leaves nothing of its predecessor in the
/// pane, and a pane that moves leaves nothing where it was painted before.
#[tokio::test]
async fn stale_cells_are_cleared_on_shrink_and_move() {
    let mut ws = Workspace::scripted();
    let left = ws
        .open_terminal("term-left", "native", "epoch-stale")
        .expect("left pane");
    let right = ws
        .open_terminal("term-right", "native", "epoch-stale")
        .expect("right pane");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(filled_frame(40, 10, 'X'));
    source.queue(filled_frame(20, 5, 'Y'));
    ws.replace_frame_source(right, PaneFrameSource::Scripted(source))
        .expect("right source");
    let mut chrome = Chrome::dark();
    chrome.open_pane(left, "left");
    chrome.open_pane(right, "right");
    let mut terminal = Terminal::new(TestBackend::new(150, 30)).expect("test terminal");
    let panes = [right];

    ws.recv_pane_frame(right).await.expect("40x10 frame");
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    assert_body_is_exactly(&bodies[0], 40, 10, 'X');
    let before = inner_rect_of(&chrome, right);

    ws.recv_pane_frame(right).await.expect("20x5 frame");
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    assert_body_is_exactly(&bodies[0], 20, 5, 'Y');

    // Closing the left pane collapses the split and moves the right pane
    // into its place.
    assert!(chrome.focus_pane(left), "the left pane is shown");
    assert_eq!(chrome.close_focused(), Some(left));
    let bodies = draw_pane_bodies(&mut terminal, &ws, &mut chrome, &panes);
    let after = inner_rect_of(&chrome, right);
    assert!(
        after.x < before.x && after.width > before.width,
        "the collapse moves the pane: {before:?} -> {after:?}"
    );
    assert_body_is_exactly(&bodies[0], 20, 5, 'Y');
    let buffer = terminal.backend().buffer();
    for y in before.y..before.y + 5 {
        for x in before.x..before.x + 20 {
            assert_eq!(
                buffer[(x, y)].symbol(),
                " ",
                "({x},{y}) keeps a glyph from before the move"
            );
        }
    }
}

/// 3.3.1: `prefix+shift+n` opens the new-project dialog; enter posts the
/// typed path to `/api/projects/init`, a refusal keeps the dialog (and its
/// path) open with the daemon's reason, and the created project is focused
/// with one shell tab in its checkout.
#[tokio::test]
async fn new_project_dialog_inits_and_focuses() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    let new_root = tempfile::tempdir().expect("new project dir");
    let new_path = new_root.path().to_string_lossy().into_owned();
    let created = json!({
        "id": "project-2", "name": "two", "display_name": "two",
        "checkout": {"machine_id": "m-local", "root_path": new_path},
    });
    let both = json!([
        {"id": "project-1", "name": "one", "display_name": "one",
         "checkout": {"machine_id": "m-local", "root_path": "/repo"}},
        created.clone(),
    ]);
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue(
        "POST",
        "/api/projects/init",
        400,
        json!({"detail": {"error_code": "invalid_checkout_root", "message": "path is not a directory"}}),
    );
    mock.enqueue("POST", "/api/projects/init", 200, created);
    for _ in 0..3 {
        mock.enqueue("GET", "/api/projects", 200, both.clone());
    }
    for page in [
        terminal_page(&[]),
        terminal_page(&[SPAWNED]),
        terminal_page(&[]),
        terminal_page(&[SPAWNED]),
    ] {
        mock.enqueue("GET", "/api/terminals?", 200, page);
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let home = tempfile::tempdir().expect("gobby home");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(64);
    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        send_chord(&input_tx, KeyCode::Char('N'), KeyModifiers::SHIFT).await;
        // The dialog opens on `~/`; replace it with the absolute path.
        send_key(&input_tx, KeyCode::Backspace, KeyModifiers::NONE).await;
        send_key(&input_tx, KeyCode::Backspace, KeyModifiers::NONE).await;
        for ch in new_path.chars() {
            send_key(&input_tx, KeyCode::Char(ch), KeyModifiers::NONE).await;
        }
        send_key(&input_tx, KeyCode::Enter, KeyModifiers::NONE).await;
        wait_for_http_requests(&mock, "POST", "/api/projects/init", 1).await;
        // The refusal left the dialog open with the path still typed.
        send_key(&input_tx, KeyCode::Enter, KeyModifiers::NONE).await;
        wait_for_http_requests(&mock, "POST", "/api/projects/init", 2).await;
        wait_for_websocket_requests(&mock, "terminal_create", 2).await;
        // The shell lands on the bar only after its placement round trip.
        wait_until(|| workspace_ops(&mock, "tab.create").len() == 2).await;
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
    result.expect("new project live loop");

    let inits: Vec<Value> = mock
        .requests()
        .into_iter()
        .filter(|request| request.method == "POST" && request.target == "/api/projects/init")
        .filter_map(|request| request.body)
        .collect();
    assert_eq!(
        inits,
        [json!({"path": new_path}), json!({"path": new_path})]
    );
    assert_eq!(chrome.mode, Mode::Terminal, "the dialog closed on success");
    assert!(chrome.dialog.is_none(), "{:?}", chrome.dialog);
    assert_eq!(workspace.project_id(), Some("project-2"));
    assert_eq!(chrome.project_tabs.focused.as_deref(), Some("project-2"));
    assert_eq!(
        chrome.tabs().tabs.len(),
        1,
        "one shell tab in the new project"
    );
    assert_eq!(chrome.tabs().tabs[0].slots.len(), 1);
    let creates = websocket_requests(&mock, "terminal_create");
    assert_eq!(creates.len(), 2);
    assert_eq!(creates[1].get("cwd"), Some(&json!(new_path)));
    assert_eq!(creates[1].get("project_id"), Some(&json!("project-2")));
    mock.shutdown().await;
}

/// A detached worktree row (`branch_name: null`, valid on the daemon side)
/// decodes at startup: the loop stays live and the sidebar lists the row
/// with `~` for its branch beside a row that has one.
#[tokio::test]
async fn a_detached_worktree_row_does_not_latch_exit() {
    let mock = MockDaemon::start("local-token").await;
    let worktrees_path = "/api/source-control/worktrees?";
    mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
    mock.enqueue(
        "GET",
        worktrees_path,
        200,
        json!({"worktrees": [
            {
                "id": "wt-branch", "project_id": "project-1", "branch_name": "worktree/feature",
                "worktree_path": "/repo-wt/feature", "status": "active",
                "workspace_role": "client",
            },
            {
                "id": "wt-detached", "project_id": "project-1", "branch_name": null,
                "worktree_path": "/repo-wt/detached", "status": "active",
                "workspace_role": "task",
            },
        ]}),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.sidebar.expanded_project = Some("project-1".to_string());
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        timeout(Duration::from_secs(1), async {
            while !mock.requests().iter().any(|request| {
                request.method == "GET" && request.target.starts_with(worktrees_path)
            }) {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("startup fetches the worktrees");
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
    result.expect("a detached worktree row must not fail the loop");
    assert_eq!(
        workspace.exit_reason(),
        Some("terminal input closed"),
        "only the closed input ends the loop"
    );
    let worktrees: Vec<(&str, Option<&str>)> = workspace.sidebar().projects[0]
        .worktrees
        .iter()
        .map(|worktree| (worktree.worktree_id.as_str(), worktree.branch.as_deref()))
        .collect();
    assert_eq!(
        worktrees,
        [
            ("wt-branch", Some("worktree/feature")),
            ("wt-detached", None)
        ]
    );
    let buffer = terminal.backend().buffer();
    let rows: Vec<String> = (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect()
        })
        .collect();
    // The token after each tree prefix is the state glyph, then the label.
    let label_after = |prefix: &str| -> Vec<String> {
        rows.iter()
            .filter_map(|row| row.split_once(prefix))
            .filter_map(|(_, rest)| rest.split_whitespace().nth(1).map(str::to_string))
            .collect()
    };
    assert_eq!(label_after("├─"), ["feature"], "{rows:#?}");
    assert_eq!(label_after("└─"), ["~"], "{rows:#?}");
    mock.shutdown().await;
}

/// 3.3.2 and 3.3.3: the worktree flows post to the daemon's routes and
/// update the child rows; the open dialog lists only rows no tab shows;
/// closing a project with children asks with the group text; remove kills
/// the tagged tab's terminals first and refuses to delete while one
/// survives; close project terminates only the terminals the daemon lets
/// it and keeps the pane of a refused kill (decision 10).
#[tokio::test]
async fn worktree_flows_round_trip_the_daemon() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    let worktrees_path = "/api/source-control/worktrees?";
    let worktree = |id: &str, branch: &str| {
        json!({
            "id": id, "project_id": "project-1", "branch_name": branch,
            "worktree_path": format!("/repo-wt/{branch}"), "status": "active",
            "workspace_role": "client",
        })
    };
    for _ in 0..3 {
        mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
        mock.enqueue(
            "GET",
            "/api/source-control/status?",
            200,
            json!({"current_branch": "0.5.0", "ahead": 0, "behind": 0, "repo_path": "/repo", "worktree_count": 0}),
        );
    }
    mock.enqueue("GET", worktrees_path, 200, json!({"worktrees": []}));
    mock.enqueue(
        "POST",
        "/api/source-control/worktrees",
        200,
        worktree("wt-1", "feature"),
    );
    mock.enqueue(
        "GET",
        worktrees_path,
        200,
        json!({"worktrees": [worktree("wt-1", "feature"), worktree("wt-2", "spare")]}),
    );
    mock.enqueue(
        "DELETE",
        "/api/source-control/worktrees/wt-1",
        200,
        json!({"ok": true}),
    );
    mock.enqueue(
        "GET",
        worktrees_path,
        200,
        json!({"worktrees": [worktree("wt-2", "spare")]}),
    );
    for page in [
        terminal_page(&[]),
        terminal_page(&[SPAWNED]),
        terminal_page(&[]),
        terminal_page(&["term-x", "term-y"]),
        terminal_page(&["term-x"]),
    ] {
        mock.enqueue("GET", "/api/terminals?", 200, page);
    }

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let home = tempfile::tempdir().expect("gobby home");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let mut chrome = Chrome::dark();
    chrome.focus_project("project-1");

    // New worktree: the dialog defaults the base to the project's branch;
    // the submit posts the client role and opens a shell tab tagged with
    // the new row.
    open_new_worktree_dialog(&workspace, &mut chrome, "project-1");
    assert_eq!(chrome.mode, Mode::ProjectDialog);
    assert!(
        matches!(
            &chrome.dialog,
            Some(Dialog::NewWorktree { project_id, branch, base, error: None, .. })
                if project_id == "project-1" && branch.is_empty() && base == "0.5.0"
        ),
        "{:?}",
        chrome.dialog
    );
    create_worktree(
        &mut workspace,
        &mut chrome,
        "project-1",
        "feature",
        Some("0.5.0"),
    )
    .await
    .expect("create worktree");
    // The daemon's tab.created event places the shell: apply it and project
    // the row the way the loop does between actions.
    workspace
        .drain_live_events()
        .await
        .expect("apply the tab.created event");
    sync_live_chrome(&mut workspace, &mut chrome);
    let posts: Vec<Value> = mock
        .requests()
        .into_iter()
        .filter(|request| {
            request.method == "POST" && request.target == "/api/source-control/worktrees"
        })
        .filter_map(|request| request.body)
        .collect();
    assert_eq!(
        posts,
        [json!({
            "project_id": "project-1", "branch_name": "feature",
            "base_branch": "0.5.0", "workspace_role": "client",
        })]
    );
    assert!(chrome.dialog.is_none(), "{:?}", chrome.dialog);
    assert_eq!(chrome.mode, Mode::Terminal);
    let children = |workspace: &Workspace<LiveDaemon>| -> Vec<String> {
        workspace.sidebar().projects[0]
            .worktrees
            .iter()
            .map(|worktree| worktree.worktree_id.clone())
            .collect()
    };
    assert_eq!(children(&workspace), ["wt-1", "wt-2"]);
    let creates = websocket_requests(&mock, "terminal_create");
    assert_eq!(creates.len(), 1);
    assert_eq!(creates[0].get("cwd"), Some(&json!("/repo-wt/feature")));
    assert_eq!(chrome.tabs().tabs.len(), 1);
    assert_eq!(chrome.tabs().tabs[0].worktree_id.as_deref(), Some("wt-1"));

    // Open worktree lists only the rows no tab shows.
    open_open_worktree_dialog(&workspace, &mut chrome, "project-1");
    assert_eq!(
        chrome.dialog,
        Some(Dialog::OpenWorktree {
            project_id: "project-1".into(),
            choices: vec![WorktreeChoice {
                worktree_id: "wt-2".into(),
                branch: "spare".into(),
                path: "/repo-wt/spare".into(),
            }],
            selected: 0,
        })
    );
    chrome.dialog = None;
    chrome.mode = Mode::Terminal;

    // Close project with children asks with the group text.
    close_project(&mut workspace, &mut chrome, "project-1")
        .await
        .expect("close project asks first");
    assert_eq!(chrome.mode, Mode::ConfirmClose);
    assert_eq!(
        chrome.dialog,
        Some(Dialog::ConfirmClose {
            target: CloseTarget::WorktreeGroup("project-1".into()),
            title: "gobby".into(),
            scope: CloseScope::Group {
                workspaces: 3,
                panes: 1,
            },
        })
    );
    chrome.dialog = None;
    chrome.mode = Mode::Terminal;

    // Remove: the dialog counts the tagged tab; a refused kill keeps the
    // pane, the tab, and the checkout, and says so inline.
    open_remove_worktree_dialog(&workspace, &mut chrome, "wt-1");
    assert_eq!(chrome.mode, Mode::ProjectDialog);
    assert_eq!(
        chrome.dialog,
        Some(Dialog::RemoveWorktree {
            worktree_id: "wt-1".into(),
            branch: "feature".into(),
            path: "/repo-wt/feature".into(),
            tabs: 1,
            panes: 1,
            error: None,
        })
    );
    mock.enqueue_kill_refusal("held by another viewer");
    remove_worktree(&mut workspace, &mut chrome, "wt-1")
        .await
        .expect("refused remove");
    assert!(
        matches!(
            &chrome.dialog,
            Some(Dialog::RemoveWorktree { error: Some(error), .. }) if error.contains("1 terminal")
        ),
        "{:?}",
        chrome.dialog
    );
    assert_eq!(chrome.tabs().tabs.len(), 1, "a refused kill keeps the tab");
    assert!(
        mock.requests()
            .iter()
            .all(|request| request.method != "DELETE"),
        "no delete while a terminal survives"
    );
    remove_worktree(&mut workspace, &mut chrome, "wt-1")
        .await
        .expect("remove worktree");
    workspace
        .drain_live_events()
        .await
        .expect("apply the tab.closed event");
    sync_live_chrome(&mut workspace, &mut chrome);
    let deletes: Vec<String> = mock
        .requests()
        .into_iter()
        .filter(|request| request.method == "DELETE")
        .map(|request| request.target)
        .collect();
    assert_eq!(deletes, ["/api/source-control/worktrees/wt-1"]);
    assert!(chrome.dialog.is_none(), "{:?}", chrome.dialog);
    assert_eq!(chrome.mode, Mode::Terminal);
    assert!(
        chrome.tabs().tabs.is_empty(),
        "the worktree's tab went with its terminal"
    );
    assert_eq!(children(&workspace), ["wt-2"]);

    // Close project terminates every tab's terminals but keeps the pane
    // whose kill the daemon refuses.
    workspace.fetch_roster().await.expect("roster with x and y");
    let x = workspace.pane_for_terminal("term-x").expect("x pane");
    let y = workspace.pane_for_terminal("term-y").expect("y pane");
    chrome.open_tab(x, "x");
    chrome.open_tab(y, "y");
    mock.enqueue_kill_refusal("external owner");
    close_project_confirmed(&mut workspace, &mut chrome, "project-1")
        .await
        .expect("close project");
    let kills: Vec<Value> = websocket_requests(&mock, "terminal_kill")
        .into_iter()
        .map(|request| request["terminal_id"].clone())
        .collect();
    assert_eq!(
        kills,
        [
            json!(SPAWNED),
            json!(SPAWNED),
            json!("term-x"),
            json!("term-y")
        ]
    );
    assert_eq!(shown_terminals(&workspace, &chrome), ["term-x"]);
    assert!(workspace.pane_for_terminal("term-y").is_none());
    mock.shutdown().await;
}

/// 3.3.2: the project dialogs' keys. New project: typing edits the path,
/// tab completes a directory, enter hands the path to the loop and keeps
/// the dialog for the daemon's answer, esc cancels. New worktree: tab moves
/// between branch and base, enter submits both. Open worktree: down moves
/// the choice, enter opens it. Remove worktree: enter asks the loop to
/// remove, esc cancels.
#[test]
fn project_dialog_keys_produce_daemon_requests() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    let root = tempfile::tempdir().expect("completion root");
    std::fs::create_dir(root.path().join("alpha-one")).expect("alpha-one");
    std::fs::write(root.path().join("alpha.txt"), b"").expect("alpha.txt");
    let root = root.path().to_string_lossy().into_owned();
    let press = |chrome: &mut Chrome, code: KeyCode| {
        route_modal_key(
            &ws,
            chrome,
            &KeyInput {
                key: KeyEvent::new(code, KeyModifiers::NONE),
                bytes: Vec::new(),
            },
        )
    };

    chrome.mode = Mode::ProjectDialog;
    let typed = format!("{root}/al");
    chrome.dialog = Some(Dialog::NewProject {
        path: typed.clone(),
        cursor: typed.chars().count(),
        error: None,
    });
    assert_eq!(press(&mut chrome, KeyCode::Tab), ModalOutcome::Consumed);
    let completed = format!("{root}/alpha-one/");
    assert!(
        matches!(&chrome.dialog, Some(Dialog::NewProject { path, cursor, .. })
            if *path == completed && *cursor == completed.chars().count()),
        "{:?}",
        chrome.dialog
    );
    assert_eq!(
        press(&mut chrome, KeyCode::Enter),
        ModalOutcome::InitProject(completed.clone())
    );
    assert_eq!(
        chrome.mode,
        Mode::ProjectDialog,
        "enter waits for the daemon"
    );
    assert_eq!(press(&mut chrome, KeyCode::Esc), ModalOutcome::Close);
    assert_eq!(chrome.mode, Mode::Terminal);

    chrome.mode = Mode::ProjectDialog;
    chrome.dialog = Some(Dialog::NewWorktree {
        project_id: "project-1".into(),
        branch: String::new(),
        base: "0.5.0".into(),
        cursor: 0,
        base_focused: false,
        error: None,
    });
    for ch in "feat".chars() {
        assert_eq!(
            press(&mut chrome, KeyCode::Char(ch)),
            ModalOutcome::Consumed
        );
    }
    assert_eq!(press(&mut chrome, KeyCode::Tab), ModalOutcome::Consumed);
    assert_eq!(
        press(&mut chrome, KeyCode::Char('x')),
        ModalOutcome::Consumed
    );
    assert!(
        matches!(&chrome.dialog, Some(Dialog::NewWorktree { branch, base, base_focused: true, .. })
            if branch == "feat" && base == "0.5.0x"),
        "{:?}",
        chrome.dialog
    );
    assert_eq!(
        press(&mut chrome, KeyCode::Backspace),
        ModalOutcome::Consumed
    );
    assert_eq!(
        press(&mut chrome, KeyCode::Enter),
        ModalOutcome::CreateWorktree {
            project_id: "project-1".into(),
            branch: "feat".into(),
            base: Some("0.5.0".into()),
        }
    );
    assert_eq!(chrome.mode, Mode::ProjectDialog);
    assert_eq!(press(&mut chrome, KeyCode::Esc), ModalOutcome::Close);

    chrome.mode = Mode::ProjectDialog;
    chrome.dialog = Some(Dialog::OpenWorktree {
        project_id: "project-1".into(),
        choices: vec![
            WorktreeChoice {
                worktree_id: "wt-1".into(),
                branch: "feature".into(),
                path: "/repo-wt/feature".into(),
            },
            WorktreeChoice {
                worktree_id: "wt-2".into(),
                branch: "spare".into(),
                path: "/repo-wt/spare".into(),
            },
        ],
        selected: 0,
    });
    assert_eq!(press(&mut chrome, KeyCode::Down), ModalOutcome::Consumed);
    assert_eq!(
        press(&mut chrome, KeyCode::Enter),
        ModalOutcome::OpenWorktree("wt-2".into())
    );
    assert_eq!(chrome.mode, Mode::Terminal);
    assert!(chrome.dialog.is_none());

    chrome.mode = Mode::ProjectDialog;
    chrome.dialog = Some(Dialog::RemoveWorktree {
        worktree_id: "wt-1".into(),
        branch: "feature".into(),
        path: "/repo-wt/feature".into(),
        tabs: 1,
        panes: 2,
        error: None,
    });
    assert_eq!(
        press(&mut chrome, KeyCode::Enter),
        ModalOutcome::RemoveWorktree("wt-1".into())
    );
    assert_eq!(
        chrome.mode,
        Mode::ProjectDialog,
        "enter waits for the daemon"
    );
    assert_eq!(press(&mut chrome, KeyCode::Esc), ModalOutcome::Close);
    assert!(chrome.dialog.is_none());
}

/// 5.3.2: the sidebar rows' menus reach the daemon. `open in new tab` on the
/// agent row opens a tab holding its pane and `mark seen` posts the entry's
/// attention id; `new worktree` on the project card opens the dialog whose
/// submit posts the worktree and opens a shell tab in it, `delete worktree
/// checkout…` on the child row deletes the checkout, and `close` on the card
/// asks with the group text.
#[tokio::test]
async fn row_menus_dispatch_project_and_agent_actions() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let worktrees_path = "/api/source-control/worktrees?";
    let worktree = |id: &str, branch: &str| {
        json!({
            "id": id, "project_id": "project-1", "branch_name": branch,
            "worktree_path": format!("/repo-wt/{branch}"), "status": "active",
            "workspace_role": "client",
        })
    };
    let roster = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:a",
            "run_id": "run-a",
            "terminal": {"terminal_id": "terminal-a", "backend": "native"},
            "attention": {
                "attention_id": "att-1",
                "kind": "actionable",
                "fingerprint": "fp-1",
                "payload": {"prompt": "Ship it?", "options": [{"option": 1, "label": "Yes"}]},
            },
        }],
    });
    for _ in 0..4 {
        mock.enqueue("GET", "/api/projects", 200, sidebar_project_row());
        mock.enqueue(
            "GET",
            "/api/source-control/status?",
            200,
            json!({"current_branch": "0.5.0", "ahead": 0, "behind": 0, "repo_path": "/repo", "worktree_count": 1}),
        );
    }
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            worktrees_path,
            200,
            json!({"worktrees": [worktree("wt-1", "spare")]}),
        );
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            terminal_page(&["terminal-a"]),
        );
        mock.enqueue("GET", "/api/attention/roster", 200, roster.clone());
    }
    mock.enqueue(
        "POST",
        "/api/source-control/worktrees",
        200,
        worktree("wt-2", "feature"),
    );
    mock.enqueue(
        "GET",
        worktrees_path,
        200,
        json!({"worktrees": [worktree("wt-1", "spare"), worktree("wt-2", "feature")]}),
    );
    mock.enqueue(
        "DELETE",
        "/api/source-control/worktrees/wt-1",
        200,
        json!({"ok": true}),
    );
    mock.enqueue(
        "GET",
        worktrees_path,
        200,
        json!({"worktrees": [worktree("wt-2", "feature")]}),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");

    // Where the loop draws the card, its child row and the agent row. A card
    // folds by default, so the probe and the loop's chrome both expand it to
    // list the worktree row.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    probe.sidebar.toggle_group("project-1");
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let row_cell = |areas: &[(String, Rect)], id: &str| {
        areas
            .iter()
            .find(|(row, _)| row == id)
            .map(|(_, rect)| (rect.x + 1, rect.y))
            .unwrap_or_else(|| panic!("row {id} drawn"))
    };
    let project_cell = row_cell(&probe.view.project_hit_areas, "project-1");
    let worktree_cell = row_cell(&probe.view.worktree_hit_areas, "wt-1");
    let agent_cell = row_cell(&probe.view.agent_hit_areas, "run:a");
    // Menu rows start one cell inside the popup at the click.
    let item_cell = |anchor: (u16, u16), index: u16| (anchor.0 + 2, anchor.1 + 1 + index);
    let gets = |path: &str| {
        mock.requests()
            .into_iter()
            .filter(|request| request.method == "GET" && request.target.starts_with(path))
            .count()
    };

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.sidebar.toggle_group("project-1");
    let (input_tx, input_rx) = mpsc::channel(256);
    let driver = async {
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 2).await;
        settle_live_event().await;
        let press = |button, (column, row): (u16, u16)| {
            send_mouse(
                &input_tx,
                MouseEventKind::Down(button),
                column,
                row,
                KeyModifiers::NONE,
            )
        };
        let key = |code| send_key(&input_tx, code, KeyModifiers::NONE);

        // The agent row's `open in new tab`, then its `mark seen`. These go
        // first: the sessions list sits right under the project rows, so
        // the row keeps the probed position only while no worktree row has
        // come or gone.
        press(MouseButton::Right, agent_cell).await;
        press(MouseButton::Left, item_cell(agent_cell, 1)).await;
        settle_live_event().await;
        press(MouseButton::Right, agent_cell).await;
        press(MouseButton::Left, item_cell(agent_cell, 3)).await;
        wait_for_http_requests(&mock, "POST", "/api/attention/run:a/seen", 1).await;
        settle_live_event().await;

        // The card's `new worktree` opens the dialog; the branch typed there
        // posts the worktree and opens a shell tab in it.
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            terminal_page(&["terminal-a", SPAWNED]),
        );
        let fetched = gets(worktrees_path);
        press(MouseButton::Right, project_cell).await;
        press(MouseButton::Left, item_cell(project_cell, 2)).await;
        for ch in "feature".chars() {
            key(KeyCode::Char(ch)).await;
        }
        key(KeyCode::Enter).await;
        wait_for_http_requests(&mock, "POST", "/api/source-control/worktrees", 1).await;
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        wait_for_http_requests(&mock, "GET", worktrees_path, fetched + 1).await;
        timeout(Duration::from_secs(1), async {
            loop {
                let attached = websocket_requests(&mock, "terminal_set_viewport")
                    .iter()
                    .any(|request| request.get("terminal_id") == Some(&json!(SPAWNED)));
                if attached {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the worktree shell attaches");
        settle_live_event().await;

        // The child row's `delete worktree checkout…` asks; enter deletes.
        let fetched = gets(worktrees_path);
        press(MouseButton::Right, worktree_cell).await;
        press(MouseButton::Left, item_cell(worktree_cell, 2)).await;
        key(KeyCode::Enter).await;
        wait_for_http_requests(&mock, "DELETE", "/api/source-control/worktrees/wt-1", 1).await;
        wait_for_http_requests(&mock, "GET", worktrees_path, fetched + 1).await;
        settle_live_event().await;

        // The card's `close` asks with the group text.
        press(MouseButton::Right, project_cell).await;
        press(MouseButton::Left, item_cell(project_cell, 1)).await;
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
    result.expect("live loop exits cleanly");

    let bodies = |method: &str, target: &str| -> Vec<Value> {
        mock.requests()
            .into_iter()
            .filter(|request| request.method == method && request.target == target)
            .filter_map(|request| request.body)
            .collect()
    };
    assert_eq!(
        bodies("POST", "/api/source-control/worktrees"),
        [json!({
            "project_id": "project-1", "branch_name": "feature",
            "base_branch": "0.5.0", "workspace_role": "client",
        })]
    );
    let creates = websocket_requests(&mock, "terminal_create");
    assert_eq!(creates.len(), 1);
    assert_eq!(creates[0].get("cwd"), Some(&json!("/repo-wt/feature")));
    let deletes: Vec<String> = mock
        .requests()
        .into_iter()
        .filter(|request| request.method == "DELETE")
        .map(|request| request.target)
        .collect();
    assert_eq!(deletes, ["/api/source-control/worktrees/wt-1"]);
    assert_eq!(
        bodies("POST", "/api/attention/run:a/seen"),
        [json!({"attention_id": "att-1"})]
    );
    let children: Vec<String> = workspace.sidebar().projects[0]
        .worktrees
        .iter()
        .map(|worktree| worktree.worktree_id.clone())
        .collect();
    assert_eq!(children, ["wt-2"]);
    let agent = workspace
        .pane_for_terminal("terminal-a")
        .expect("agent pane");
    let tabs = &chrome.tabs().tabs;
    assert_eq!(
        tabs.len(),
        2,
        "the agent's new tab and the worktree shell tab: {:?}",
        shown_terminals(&workspace, &chrome)
    );
    assert_eq!(
        chrome.viewer.focused_pane(&tabs[0]),
        Some(agent),
        "open in new tab holds the agent's pane"
    );
    assert_eq!(tabs[1].worktree_id.as_deref(), Some("wt-2"));
    assert_eq!(
        chrome.active_index(),
        1,
        "the worktree shell tab opened last"
    );
    assert_eq!(
        chrome.mode,
        Mode::ConfirmClose,
        "close on the card asks first"
    );
    assert!(
        matches!(
            &chrome.dialog,
            Some(Dialog::ConfirmClose {
                target: CloseTarget::WorktreeGroup(project),
                ..
            }) if project == "project-1"
        ),
        "{:?}",
        chrome.dialog
    );
    mock.shutdown().await;
}

/// 4.3.2: a held pane keeps a keyboard exit under an outer tmux. `prefix
/// prefix` writes the prefix chord itself to the pane (herdr's `ctrl+b
/// ctrl+b`), and `ctrl+\` releases control with no prefix at all, so an outer
/// tmux eating the prefix can never lock the keyboard into a pane.
#[tokio::test]
async fn held_pane_has_literal_prefix_and_keyboard_escape() {
    let mock = MockDaemon::start("local-token").await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-held", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-held", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let _home = pin_tabs(&mock, &mut workspace, "project-1", &["terminal-held"]);
    let mut chrome = Chrome::dark();
    chrome.nested = true;
    chrome.keymap = Keymap::defaults(default_prefix(true));
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        // Startup focus takes the lease and the mock grants it: the pane is
        // held before the first key arrives.
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        settle_live_event().await;
        send_key(&input_tx, KeyCode::Char(']'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char(']'), KeyModifiers::CONTROL).await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
        send_key(&input_tx, KeyCode::Char('\\'), KeyModifiers::CONTROL).await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 1).await;
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
    result.expect("live loop exits cleanly");
    let pane = workspace
        .pane_for_terminal("terminal-held")
        .expect("terminal pane");

    let typed: Vec<String> = websocket_requests(&mock, "terminal_input")
        .iter()
        .filter_map(|request| request.get("data")?.as_str().map(str::to_string))
        .collect();
    assert_eq!(
        typed,
        vec!["\u{1d}".to_string()],
        "prefix prefix reaches the held pane as the literal chord; ctrl+\\ never does"
    );
    assert_eq!(
        websocket_requests(&mock, "terminal_release_control").len(),
        1,
        "ctrl+\\ releases control without the prefix"
    );
    assert!(
        !workspace.pane(pane).is_held(),
        "the pane is observed after the keyboard escape: {:?}",
        workspace.pane(pane).control
    );
    mock.shutdown().await;
}

/// The two-project fixture the placement tests share: project-1 has one
/// daemon tab, project-2 none, and every roster page carries the three
/// terminals the tests place.
async fn two_project_workspace(
    mock: &MockDaemon,
    spawned: &str,
) -> (LiveDaemon, Workspace<LiveDaemon>, Chrome, tempfile::TempDir) {
    let projects = json!([
        {"id": "project-1", "name": "one", "display_name": "one",
         "checkout": {"machine_id": "m-local", "root_path": "/repo"}},
        {"id": "project-2", "name": "two", "display_name": "two",
         "checkout": {"machine_id": "m-local", "root_path": "/repo2"}},
    ]);
    for _ in 0..4 {
        mock.enqueue("GET", "/api/projects", 200, projects.clone());
    }
    for _ in 0..8 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            terminal_page(&["terminal-a1", "terminal-b1", spawned]),
        );
    }
    mock.seed_workspace("project-1", &[(&["terminal-a1"], "terminal-a1")]);
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let home = tempfile::tempdir().expect("gobby home");
    let mut workspace = Workspace::live(daemon.clone());
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let mut chrome = Chrome::dark();
    sync_live_chrome(&mut workspace, &mut chrome);
    (daemon, workspace, chrome, home)
}

/// Create a project-2 tab for `terminal` the way another window would.
async fn other_window_creates_tab(daemon: &LiveDaemon, workspace_id: &str, terminal: &str) {
    daemon
        .workspace_op(WorkspaceOp::TabCreate {
            workspace: workspace_id.to_string(),
            project_id: "project-2".to_string(),
            worktree_id: None,
            title: None,
            terminal_id: Some(terminal.to_string()),
            node: None,
        })
        .await
        .expect("tab.create");
}

/// 4.2: a placement the daemon refuses is forgotten, so another window
/// placing the same terminal later does not move this window's focus.
#[tokio::test]
async fn a_refused_placement_is_not_claimed_when_another_window_places_it() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    let (daemon, mut workspace, mut chrome, _home) = two_project_workspace(&mock, SPAWNED).await;
    let workspace_id = workspace
        .workspace_model()
        .expect("attached")
        .workspace
        .id
        .clone();

    mock.enqueue_workspace_refusal("busy", "another window is moving the workspace");
    focus_project(&mut workspace, &mut chrome, "project-2")
        .await
        .expect("focus project-2");
    assert_eq!(
        workspace_ops(&mock, "tab.create").len(),
        1,
        "the empty bar spawned a shell and asked for its tab"
    );
    assert_eq!(
        chrome.last_alert(),
        Some("another window is moving the workspace")
    );

    // Another window gives project-2 a tab, then places the refused shell.
    for terminal in ["terminal-b1", SPAWNED] {
        other_window_creates_tab(&daemon, &workspace_id, terminal).await;
        workspace
            .drain_live_events()
            .await
            .expect("apply the tab.created event");
        sync_live_chrome(&mut workspace, &mut chrome);
    }
    assert_eq!(chrome.tabs().tabs.len(), 2);
    assert_eq!(
        chrome.active_index(),
        0,
        "the other window's placement is not this window's"
    );
    mock.shutdown().await;
}

/// 4.2: a placement that lands while another project is focused waits for
/// its own bar and focuses there when the project returns.
#[tokio::test]
async fn a_placement_landing_behind_another_project_focuses_on_return() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let mock = MockDaemon::start("local-token").await;
    let (daemon, mut workspace, mut chrome, _home) = two_project_workspace(&mock, SPAWNED).await;
    let workspace_id = workspace
        .workspace_model()
        .expect("attached")
        .workspace
        .id
        .clone();

    // The spawn's tab.created event is queued behind the op reply; the
    // user is back on project-1 before the loop applies it.
    focus_project(&mut workspace, &mut chrome, "project-2")
        .await
        .expect("focus project-2");
    focus_project(&mut workspace, &mut chrome, "project-1")
        .await
        .expect("focus project-1");
    workspace
        .drain_live_events()
        .await
        .expect("apply the tab.created event");
    sync_live_chrome(&mut workspace, &mut chrome);

    // Another window puts a tab in front of the shell's.
    other_window_creates_tab(&daemon, &workspace_id, "terminal-b1").await;
    workspace
        .drain_live_events()
        .await
        .expect("apply the second tab.created event");
    let front = workspace
        .workspace_model()
        .expect("attached")
        .tabs_for_project("project-2")
        .last()
        .expect("two project-2 tabs")
        .id
        .clone();
    daemon
        .workspace_op(WorkspaceOp::TabMove {
            tab: front,
            position: 0,
            workspace: None,
            node: None,
        })
        .await
        .expect("tab.move");
    workspace
        .drain_live_events()
        .await
        .expect("apply the tab.moved event");
    sync_live_chrome(&mut workspace, &mut chrome);

    focus_project(&mut workspace, &mut chrome, "project-2")
        .await
        .expect("focus project-2 again");
    assert_eq!(
        shown_terminals(&workspace, &chrome),
        ["terminal-b1", SPAWNED]
    );
    assert_eq!(
        chrome.active_index(),
        1,
        "the shell this window spawned is the active tab"
    );
    mock.shutdown().await;
}

/// 4.2: a tab a scripted path opened never came from the model, so the
/// projection keeps it after the daemon's rows instead of ending it.
#[tokio::test]
async fn local_tabs_stay_behind_the_projected_daemon_tabs() {
    let mock = MockDaemon::start("local-token").await;
    let projects = json!([
        {"id": "project-1", "name": "one", "display_name": "one",
         "checkout": {"machine_id": "m-local", "root_path": "/repo"}},
    ]);
    for _ in 0..2 {
        mock.enqueue("GET", "/api/projects", 200, projects.clone());
    }
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            terminal_page(&["terminal-a1", "terminal-local"]),
        );
    }
    mock.seed_workspace("project-1", &[(&["terminal-a1"], "terminal-a1")]);
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let home = tempfile::tempdir().expect("gobby home");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("subscribe-first reconcile");
    let mut chrome = Chrome::dark();
    let local = workspace
        .pane_for_terminal("terminal-local")
        .expect("the roster pane");
    chrome.open_tab(local, "local");

    sync_live_chrome(&mut workspace, &mut chrome);
    let tabs = &chrome.tabs().tabs;
    assert_eq!(tabs.len(), 2, "the daemon tab and the local tab");
    assert!(!tabs[0].is_local() && tabs[1].is_local());
    assert_eq!(
        shown_terminals(&workspace, &chrome),
        ["terminal-a1", "terminal-local"]
    );
    mock.shutdown().await;
}

/// The daemon reaps a killed terminal's pane and closes the emptied tab
/// itself, so the client's follow-up `tab.close` is refused `not_found`.
/// That refusal is the daemon saying "done": it never reaches the status
/// line, and the `tab.closed` event that preceded it took the tab out of
/// the model and the bar, which moved to the spare tab.
#[tokio::test]
async fn closing_a_tab_the_daemon_already_reaped_stays_quiet() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop_with(&mock, true).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('X'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
        wait_until(|| workspace_ops(&mock, "tab.close").len() == 1).await;
        // The spare tab takes the focus once the loop applies the daemon's
        // `tab.closed` event; the focus hint it sends says the loop got there.
        wait_until(|| {
            workspace_ops(&mock, "workspace.set_focus_hints")
                .iter()
                .any(|op| op["tab"] == "mock-tab-4")
        })
        .await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        workspace_ops(&mock, "tab.close")
            .iter()
            .map(|op| op["tab"].clone())
            .collect::<Vec<_>>(),
        [json!("mock-tab-1")],
        "the follow-up tab.close names the tab the daemon already closed"
    );
    let model = workspace.workspace_model().expect("attached workspace");
    assert!(
        model.tab("mock-tab-1").is_none() && model.tab("mock-tab-4").is_some(),
        "the daemon's tab.closed event took the reaped tab out of the model"
    );
    assert_eq!(
        chrome
            .tabs()
            .tabs
            .iter()
            .map(|tab| tab.id.clone())
            .collect::<Vec<_>>(),
        ["mock-tab-4"],
        "the reaped tab left the bar and the spare tab shows"
    );
    assert!(
        chrome
            .alert_log
            .iter()
            .all(|toast| !toast.title.contains("has id")),
        "a not_found refusal of the follow-up tab.close is not an error"
    );
    mock.shutdown().await;
}

/// A window that opens on the daemon's stored focus reports nothing: the
/// hint it would send is the snapshot's own, and echoing it would only
/// ripple a `focus_hints` event to every other window. The first real
/// change, the spare tab under prefix+n, still goes out once.
#[tokio::test]
async fn startup_sends_no_focus_hint_for_the_snapshot_focus() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop_with(&mock, true).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_chord(&input_tx, KeyCode::Char('n'), KeyModifiers::NONE).await;
        // The hint naming the spare tab says the loop applied the switch;
        // a startup echo would already be on record ahead of it.
        wait_until(|| {
            workspace_ops(&mock, "workspace.set_focus_hints")
                .iter()
                .any(|op| op["tab"] == "mock-tab-4")
        })
        .await;
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
    result.expect("live loop exits cleanly");
    let hints: Vec<(Value, Value)> = workspace_ops(&mock, "workspace.set_focus_hints")
        .iter()
        .map(|op| (op["tab"].clone(), op["pane"].clone()))
        .collect();
    assert_eq!(
        hints,
        [(json!("mock-tab-4"), json!("mock-pane-5"))],
        "only the tab switch reports a focus; the snapshot's own is never echoed"
    );
    mock.shutdown().await;
}

/// A `pane.close` the daemon already did (it reaped the killed pane) is
/// refused `not_found`; that is not an error either.
#[tokio::test]
async fn an_unknown_pane_close_stays_quiet() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        // The fixture focuses the external pane; cycle to the owned one.
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Tab, KeyModifiers::NONE).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
        wait_until(|| workspace_ops(&mock, "pane.close").len() == 1).await;
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
    result.expect("live loop exits cleanly");
    let kills = websocket_requests(&mock, "terminal_kill");
    assert_eq!(
        kills[0].get("terminal_id"),
        Some(&json!("terminal-gobby")),
        "the cycle landed on the owned pane, whose terminal the close kills"
    );
    assert!(
        chrome
            .alert_log
            .iter()
            .all(|toast| !toast.title.contains("has id")),
        "a not_found refusal of the follow-up pane.close is not an error"
    );
    mock.shutdown().await;
}

/// Only `not_found` is tolerated on a close: any other refusal of the same
/// op still tells the user why the layout did not change.
#[tokio::test]
async fn a_busy_refusal_of_a_pane_close_still_shows() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = mixed_ownership_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        // Enqueued after the startup focus hint, so the refusal meets the close.
        mock.enqueue_workspace_refusal("busy", "another window is moving the workspace");
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_until(|| !workspace_ops(&mock, "pane.close").is_empty()).await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        chrome.last_alert(),
        Some("another window is moving the workspace"),
        "a busy refusal of pane.close raises an alert"
    );
    mock.shutdown().await;
}

/// Wait for a `terminal_take_control` naming `terminal_id`.
///
/// The loop takes control of whatever it focuses, so counting requests would
/// also match the pane focused at startup. Per the loop-ordering rule, an
/// assertion after `run_live_loop` has to wait for a mock-visible request the
/// loop can only send once it applied the click, then drop the input.
async fn wait_for_take_control_of(mock: &MockDaemon, terminal_id: &str) {
    timeout(Duration::from_secs(1), async {
        loop {
            if websocket_requests(mock, "terminal_take_control")
                .iter()
                .any(|body| body.get("terminal_id") == Some(&json!(terminal_id)))
            {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for take control of {terminal_id}"));
}

/// A bare terminal row is a click target.
///
/// `bare_terminals` keys such a row `terminal:<terminal_id>`, and only rows the
/// roster joined to an agent entry appear in `sidebar().agents`. `agent_pane`
/// searched that list alone, so every bare row answered `None`, and each caller
/// reads `None` as nothing to do: the click, `open in new tab`, and the focus
/// retarget a right-click close depends on were all silent no-ops. The user saw
/// rows that highlighted when their pane was clicked but could never be clicked
/// themselves.
#[tokio::test]
async fn clicking_a_bare_terminal_row_focuses_that_terminal() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                    {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let first = workspace
        .pane_for_terminal("terminal-a")
        .expect("pane for terminal-a");
    let second = workspace
        .pane_for_terminal("terminal-b")
        .expect("pane for terminal-b");

    // Sidebar hit areas exist only once the rows have been drawn, so the probe
    // renders a real frame to find the row's cell.
    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    show_roster(&workspace, &mut probe);
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let row_id = format!("{TERMINAL_ROW}terminal-b");
    let (column, row) = probe
        .view
        .agent_hit_areas
        .iter()
        .find(|(entry, _)| *entry == row_id)
        .map(|(_, rect)| (rect.x + 1, rect.y))
        .expect("bare terminal row drawn");
    assert!(
        matches!(hit_test(&probe.view, column, row), Hit::Agent(_)),
        "the click lands on the bare terminal row"
    );

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    chrome.focus_pane(first);
    let (input_tx, input_rx) = mpsc::channel(32);
    let driver = async {
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            column,
            row,
            KeyModifiers::NONE,
        )
        .await;
        wait_for_take_control_of(&mock, "terminal-b").await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        chrome.focused_pane(),
        Some(second),
        "clicking the row focuses the terminal it names"
    );
    assert_ne!(second, first, "the click moved focus off the starting pane");
    mock.shutdown().await;
}

/// Right-click close on a bare terminal row acts on that row's terminal.
///
/// `focus_menu_target` retargets focus to the menu's subject before the action
/// runs, which is how every untargeted row action reaches the right pane. It
/// retargets through `agent_pane`, so while that answered `None` for a bare
/// terminal row the retarget silently did nothing and `Action::CloseTerminal`
/// fell through to `chrome.focused_pane()` -- killing whatever the user
/// happened to be looking at instead of the row they clicked.
#[tokio::test]
async fn closing_a_bare_terminal_row_kills_that_row_not_the_focused_pane() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                    {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let first = workspace
        .pane_for_terminal("terminal-a")
        .expect("pane for terminal-a");

    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    show_roster(&workspace, &mut probe);
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let row_id = format!("{TERMINAL_ROW}terminal-b");
    let anchor = probe
        .view
        .agent_hit_areas
        .iter()
        .find(|(entry, _)| *entry == row_id)
        .map(|(_, rect)| (rect.x + 1, rect.y))
        .expect("bare terminal row drawn");

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    chrome.focus_pane(first);
    let (input_tx, input_rx) = mpsc::channel(32);
    let driver = async {
        let press = |button, (column, row): (u16, u16)| {
            send_mouse(
                &input_tx,
                MouseEventKind::Down(button),
                column,
                row,
                KeyModifiers::NONE,
            )
        };
        // `agent_items` for an unblocked bare row: focus, open in new tab,
        // mark seen, take control, close terminal.
        press(MouseButton::Right, anchor).await;
        press(MouseButton::Left, (anchor.0 + 2, anchor.1 + 1 + 4)).await;
        wait_for_websocket_requests(&mock, "terminal_kill", 1).await;
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
    result.expect("live loop exits cleanly");
    let killed: Vec<Value> = websocket_requests(&mock, "terminal_kill");
    assert_eq!(
        killed
            .iter()
            .map(|body| body.get("terminal_id").cloned().unwrap_or(Value::Null))
            .collect::<Vec<_>>(),
        vec![json!("terminal-b")],
        "close acts on the row under the menu, never on the focused pane"
    );
    mock.shutdown().await;
}

/// Closing an external row detaches instead of killing.
///
/// An external row is a terminal the user attached -- a tmux pane gclient
/// never created -- so "close terminal" on it means hand the lease back and
/// drop the pane, which is what `close_live_pane` already does for the same
/// case. Without the guard the sidebar's close destroyed a terminal living
/// outside gclient entirely.
#[tokio::test]
async fn closing_an_external_row_releases_the_lease_instead_of_killing_it() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                    {
                        "terminal_id": "terminal-tmux",
                        "backend": "tmux",
                        "state": "live",
                        "ownership": "external"
                    }
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let external = workspace
        .pane_for_terminal("terminal-tmux")
        .expect("pane for terminal-tmux");
    assert!(
        workspace.pane(external).external,
        "the roster marks this pane external"
    );

    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    show_roster(&workspace, &mut probe);
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let row_id = format!("{TERMINAL_ROW}terminal-tmux");
    let anchor = probe
        .view
        .agent_hit_areas
        .iter()
        .find(|(entry, _)| *entry == row_id)
        .map(|(_, rect)| (rect.x + 1, rect.y))
        .expect("external terminal row drawn");

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(32);
    let driver = async {
        let press = |button, (column, row): (u16, u16)| {
            send_mouse(
                &input_tx,
                MouseEventKind::Down(button),
                column,
                row,
                KeyModifiers::NONE,
            )
        };
        press(MouseButton::Right, anchor).await;
        // `close terminal` is the fifth item of an unblocked row's menu.
        press(MouseButton::Left, (anchor.0 + 2, anchor.1 + 1 + 4)).await;
        wait_for_websocket_requests(&mock, "terminal_release_control", 1).await;
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
    result.expect("live loop exits cleanly");
    assert!(
        websocket_requests(&mock, "terminal_kill").is_empty(),
        "an external terminal is never killed from the sidebar"
    );
    assert!(
        websocket_requests(&mock, "terminal_release_control")
            .iter()
            .any(|body| body.get("terminal_id") == Some(&json!("terminal-tmux"))),
        "closing it hands the lease back instead"
    );
    mock.shutdown().await;
}

/// An id naming neither a sidebar agent nor a terminal row still resolves to
/// nothing, so the prefix branch widens the resolver without loosening it.
#[tokio::test]
async fn an_unknown_sidebar_id_still_resolves_to_no_pane() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-a", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("pane for terminal-a");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    chrome.focus_pane(pane);

    // Driven outside the loop, where ordering is deterministic. A bare
    // terminal id without the row prefix is not a row id either.
    for entry in ["run:missing", "session:missing", "terminal-a"] {
        focus_agent(&mut workspace, &mut chrome, entry)
            .await
            .expect("an unresolved row is not an error");
        assert_eq!(chrome.focused_pane(), Some(pane), "{entry} moves nothing");
    }
    mock.shutdown().await;
}

/// `open in new tab` on a bare terminal row reveals that terminal.
///
/// The user's report was that the item could be chosen and did nothing, and
/// this is the third caller of the same `None`: `open_agent_in_new_tab`
/// resolves through `agent_pane` and returns `Ok(())` when it answers nothing.
/// A bare row's pane is already placed -- `bare_terminals` only emits a row
/// once `pane_for_terminal` resolves -- so `chrome.focus_pane` succeeds and the
/// item reveals the terminal where it lives rather than duplicating it into a
/// second tab; the `Placement::Tab` arm is for a pane no tab holds. What the
/// fix changes is that the item acts at all.
#[tokio::test]
async fn opening_a_bare_terminal_row_in_a_new_tab_reveals_that_terminal() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                    {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install initial attachments");
    let first = workspace
        .pane_for_terminal("terminal-a")
        .expect("pane for terminal-a");
    let second = workspace
        .pane_for_terminal("terminal-b")
        .expect("pane for terminal-b");

    let area = Rect::new(0, 0, 120, 40);
    let mut probe = Chrome::dark();
    show_roster(&workspace, &mut probe);
    probe.compute_view(&workspace, area);
    let mut probe_terminal = Terminal::new(TestBackend::new(120, 40)).expect("probe terminal");
    let mut hits = None;
    probe_terminal
        .draw(|frame| hits = Some(render_workspace(frame, &workspace, &probe)))
        .expect("draw probe frame");
    probe.view.apply_hits(hits.expect("probe frame drawn"));
    let row_id = format!("{TERMINAL_ROW}terminal-b");
    let anchor = probe
        .view
        .agent_hit_areas
        .iter()
        .find(|(entry, _)| *entry == row_id)
        .map(|(_, rect)| (rect.x + 1, rect.y))
        .expect("bare terminal row drawn");

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    chrome.focus_pane(first);
    let (input_tx, input_rx) = mpsc::channel(32);
    let driver = async {
        let press = |button, (column, row): (u16, u16)| {
            send_mouse(
                &input_tx,
                MouseEventKind::Down(button),
                column,
                row,
                KeyModifiers::NONE,
            )
        };
        press(MouseButton::Right, anchor).await;
        // `open in new tab` is the second item of an unblocked row's menu.
        press(MouseButton::Left, (anchor.0 + 2, anchor.1 + 1 + 1)).await;
        wait_for_take_control_of(&mock, "terminal-b").await;
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
    result.expect("live loop exits cleanly");
    assert_eq!(
        chrome.focused_pane(),
        Some(second),
        "the item reveals the terminal the row names"
    );
    assert_ne!(second, first, "and moves focus off the starting pane");
    mock.shutdown().await;
}

/// #22534: a launch that finds the daemon down opens the window and waits.
/// The status line carries the condition, the supervisor connects when the
/// daemon returns, and the first handshake runs the restore the launch
/// skipped, so the window seeds its first shell then.
#[tokio::test]
async fn launch_with_the_daemon_down_waits_and_restores_when_it_returns() {
    const SPAWNED: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let home = tempfile::tempdir().expect("gobby home");
    let address = {
        let mock = MockDaemon::start("local-token").await;
        let address = mock.url().trim_start_matches("http://").to_string();
        mock.shutdown().await;
        address
    };
    let daemon = LiveDaemon::connect_or_wait(format!("http://{address}"), "local-token")
        .await
        .expect("a stopped daemon is a wait, not a launch failure");
    assert!(!daemon.ready());
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        // Two keys through a one-slot channel: the second send returns only
        // once the loop took the first, which is after its launch reconcile
        // failed against the closed port and the wait began.
        send_key(&input_tx, KeyCode::Null, KeyModifiers::NONE).await;
        send_key(&input_tx, KeyCode::Null, KeyModifiers::NONE).await;
        let mock = MockDaemon::start_at("local-token", &address).await;
        mock.enqueue(
            "GET",
            "/api/projects",
            200,
            json!([{
                "id": "project-1",
                "name": "gobby",
                "display_name": "gobby",
                "checkout": {"machine_id": "m-local", "root_path": "/repo"},
                "session_count": 1,
                "last_activity_at": null,
            }]),
        );
        mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&[]));
        mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&[SPAWNED]));
        wait_for_websocket_requests(&mock, "workspace_attach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_create", 1).await;
        wait_for_http_requests(&mock, "GET", "/api/terminals?", 2).await;
        settle_live_event().await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('Q'), KeyModifiers::SHIFT).await;
        mock
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, mock) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("a launch without a daemon never exits on its own");
    assert_eq!(workspace.exit_reason(), Some("quit"));
    assert!(
        workspace.daemon_ready(),
        "the returned daemon was handshaken"
    );
    assert_eq!(
        websocket_requests(&mock, "terminal_create").len(),
        1,
        "the first handshake seeds the shell the launch could not"
    );
    assert_eq!(chrome.tabs().tabs.len(), 1);
    mock.shutdown().await;
}

/// #22534: a source that reports a host epoch change has no recovery path,
/// but the loop shows the report instead of swallowing it; the end of
/// stream behind it still falls back to proxy.
#[tokio::test]
async fn host_epoch_change_on_a_pane_source_is_shown() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-epoch";
    let (mut workspace, _) = live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("epoch pane");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue_error(FrameError::HostEpochChanged {
        expected: "host-a".into(),
        actual: "host-b".into(),
    });
    workspace
        .replace_frame_source(pane_id, PaneFrameSource::Scripted(source))
        .expect("scripted source that reports an epoch change");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
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
    result.expect("epoch change loop");
    let titles: Vec<&str> = chrome
        .alert_log
        .iter()
        .map(|toast| toast.title.as_str())
        .collect();
    assert!(
        titles.contains(&"frame host epoch changed from host-a to host-b"),
        "the epoch change reaches the alert log: {titles:?}"
    );
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Proxy),
        "the end of stream behind the report still falls back"
    );
    mock.shutdown().await;
}

/// #22534: a proxy fallback clears the pane's direct offer for the rest of
/// the connection; the roster row a reconnect brings back re-arms it, so the
/// attach after the handshake asks for direct again.
#[tokio::test]
async fn reconnect_rearms_direct_from_the_fresh_roster_row() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-rearm";
    let (mut workspace, _) = live_workspace_with_scripted_direct(&mock, terminal_id, 2).await;
    // The reconnect's roster read: the same terminal, now with a locator.
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": terminal_id,
                "backend": "native",
                "state": "live",
                "attach": {
                    "backend": "native",
                    "frame_host_epoch": "host-epoch",
                    "host_socket": "/nonexistent/gobby-frames.sock",
                    "host_terminal_id": "ht-rearm",
                    "socket_path": null,
                    "pane_id": null,
                    "server_pid": null,
                    "server_start_time": null,
                }
            }],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 2}
        }),
    );
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        // The scripted source ends at once: proxy fallback, direct cleared.
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        let before_reconnect = websocket_requests(&mock, "terminal_attach").len();
        mock.drop_websockets();
        timeout(Duration::from_secs(4), async {
            loop {
                let rearmed = websocket_requests(&mock, "terminal_attach")
                    .iter()
                    .skip(before_reconnect)
                    .any(|request| request.get("frame_delivery") == Some(&json!("direct")));
                if rearmed {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("the reconnect's roster row re-arms direct");
        drop(input_tx);
        before_reconnect
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, before_reconnect) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("direct re-arm loop");
    let attaches = websocket_requests(&mock, "terminal_attach");
    assert!(
        attaches[..before_reconnect]
            .iter()
            .all(|request| request.get("frame_delivery") == Some(&json!("proxy"))),
        "every attach before the reconnect stayed proxy: {attaches:?}"
    );
    mock.shutdown().await;
}
