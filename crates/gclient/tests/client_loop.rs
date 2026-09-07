//! 3.3.5 select → spawn → attach → terminate against scripted endpoints.

mod mock_daemon;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_client::app::run_loop::{
    run_scripted_loop, ReconnectAttempt, ReconnectSupervisor, RENDER_TICK,
};
use gobby_client::app::{run_live_loop, AttachState};
use gobby_client::daemon::{
    Answer, Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, KillOutcome, LiveDaemon,
    Page, RosterEntry, ScriptedDaemon, SpawnOutcome, SpawnRequest, SubscribeSnapshot, TerminalRow,
    WsMessage, WsReply, CONTROL_REQUEST_DEADLINE,
};
use gobby_client::frame_source::{
    AttachLocator, PaneFrameSource, ScriptedFrameSource, Transport, UnixSocketFrameSource,
};
use gobby_client::startup::Ready;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneModes, ServerMessage, MAX_FRAME_SIZE,
};
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
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

fn encoded_frame(text: &str) -> String {
    let mut framed = Vec::new();
    write_message(&mut framed, &semantic_frame(text)).expect("encode test frame");
    STANDARD.encode(&framed[4..])
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
    let result = gobby_client::views::run_ready(Ready {
        daemon_url: "not a URL".to_string(),
        token: Some("test-token".to_string()),
        project: "project-1".to_string(),
        frame_delivery: gobby_client::FrameDelivery::Auto,
        host: None,
        host_notice: None,
        prefs: gobby_client::ui::settings::ClientPrefs::default(),
        gobby_home: std::path::PathBuf::new(),
    });

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
    workspace.select_project("project-1");
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

    let (result, ()) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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

    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError> {
        Daemon::roster(&self.inner).await
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
}

#[tokio::test(start_paused = true)]
async fn loop_routes_input_and_frames() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-loop", "native", "epoch-loop")
        .expect("open terminal");
    ws.force_held(pane);
    let mut source = ScriptedFrameSource::new(Transport::Direct);
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
    workspace.select_project("project-1");
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

    let (result, ()) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
    workspace.select_project("project-1");
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

        mock.enqueue_take_control_reply(true, 1, None);
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 3).await;
        assert!(
            websocket_requests(&mock, "terminal_input").is_empty(),
            "a stale grant cannot settle the pending key"
        );
        send_key(&input_tx, KeyCode::Char('z'), KeyModifiers::NONE).await;
        tokio::task::yield_now().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_take_control").len(),
            3,
            "further keys must not start another request while input is pending"
        );

        mock.enqueue_take_control_reply(true, 2, None);
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 4).await;
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
        mock.enqueue_take_control_reply(false, 3, Some("held by peer"));
        send_key(&input_tx, KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 5).await;
        tokio::task::yield_now().await;
        assert_eq!(
            websocket_requests(&mock, "terminal_input").len(),
            1,
            "a rejected pending key must be discarded"
        );
        drop(input_tx);
        focused_terminal_id
    };

    let (result, focused_terminal_id) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
            .status_message
            .as_deref()
            .is_some_and(|message| message.contains("held by peer")),
        "control refusal reason must remain visible: {:?}",
        chrome.status_message
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
    workspace.select_project("project-1");
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
        send_key(&input_tx, KeyCode::Char('r'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
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

    let (result, ()) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
            drop(input_tx);
        };
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
            settle_live_event().await;
            drop(input_tx);
        };
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (input_tx, input_rx) = mpsc::channel(16);
        let driver = async {
            wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
            settle_live_event().await;
            drop(input_tx);
        };
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
            send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
            wait_for_websocket_requests(&mock, "terminal_input", 1).await;
            assert_eq!(websocket_requests(&mock, "terminal_set_viewport").len(), 3);
            send_resize_burst(5);
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 5).await;
            wait_for_websocket_requests(&mock, "terminal_resize", 1).await;
            let direct_viewport = timeout(Duration::from_secs(1), async {
                loop {
                    if let Some(ClientMessage::SetViewport { rows, cols }) = direct_rx.recv().await
                    {
                        break (rows, cols);
                    }
                }
            })
            .await
            .expect("direct viewport after resize");
            drop(input_tx);
            direct_viewport
        };

        let (result, direct_viewport) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        assert_eq!(viewports.len(), 5, "the signal burst is coalesced once");
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
        assert_eq!(resizes.len(), 1);
        assert_eq!(
            resizes[0].get("terminal_id"),
            Some(&json!("term-controlled"))
        );
        assert_eq!(
            resizes[0].get("rows").and_then(Value::as_u64),
            Some(u64::from(direct_viewport.0))
        );
        assert_eq!(
            resizes[0].get("cols").and_then(Value::as_u64),
            Some(u64::from(direct_viewport.1))
        );
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
        workspace.select_project("project-1");
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
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
            driver
        );
        result.expect("zero-size resize loop");
        mock.shutdown().await;
    }
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
        1,
        "only a controlled native pane owns PTY geometry"
    );
    assert_eq!(
        resizes[0].get("terminal_id"),
        Some(&json!("term-controlled"))
    );
    assert_eq!(resizes[0].get("rows"), Some(&json!(30)));
    assert_eq!(resizes[0].get("cols"), Some(&json!(100)));
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
        workspace.select_project("project-selected");
        let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
        let mut chrome = Chrome::dark();
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
            send_key(&input_tx, KeyCode::Char('N'), KeyModifiers::SHIFT).await;
            wait_for_websocket_requests(&mock, "terminal_create", 1).await;
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
            let spawned_attachment = websocket_requests(&mock, "terminal_set_viewport")
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
                })
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

        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (input_tx, input_rx) = mpsc::channel(16);

        let driver = async {
            settle_live_event().await;
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('N'), KeyModifiers::SHIFT).await;
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

        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        let (input_tx, input_rx) = mpsc::channel(8);
        let driver = async {
            settle_live_event().await;
            send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
            send_key(&input_tx, KeyCode::Char('N'), KeyModifiers::SHIFT).await;
            wait_for_websocket_requests(&mock, "terminal_create", 1).await;
            settle_live_event().await;
            drop(input_tx);
        };
        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
            driver
        );
        result.expect("spawn refusal live loop");
        assert_eq!(workspace.pane_count(), 0);
        assert!(websocket_requests(&mock, "terminal_attach").is_empty());
        assert!(chrome
            .status_message
            .as_deref()
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

    for attempt in 0..5 {
        let started = Instant::now();
        let outcome = supervisor.attempt_when_due(&daemon).await;
        elapsed.push(Instant::now() - started);
        if attempt < 4 {
            assert_eq!(
                outcome,
                ReconnectAttempt::RetryScheduled {
                    delay: [250, 500, 1_000, 2_000].map(Duration::from_millis)[attempt]
                }
            );
        } else {
            assert_eq!(outcome, ReconnectAttempt::Exhausted(unavailable()));
        }
    }

    for (actual, expected) in elapsed
        .into_iter()
        .zip([0, 250, 500, 1_000, 2_000].map(Duration::from_millis))
    {
        assert!(actual >= expected && actual <= expected + Duration::from_millis(1));
    }
    assert_eq!(daemon.calls(), vec![Generation(7); 5]);
    assert_eq!(waiter.await.expect("exhaustion waiter"), Err(unavailable()));

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

        let (result, before_exit) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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

        let (result, before_exit) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
    workspace.select_project("project-1");
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

    let (result, old_attachments) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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

    let (result, ()) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
                {"terminal_id": "terminal-exhaustion", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-exhaustion", "seq": 1}
        }),
    );
    for _ in 0..3 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            500,
            json!({"code": "reconcile_failed", "message": "roster unavailable"}),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect budget-exhaustion daemon");
    mock.fail_next_websocket();
    mock.fail_next_websocket();
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
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

        for (expected_handshakes, expected_roster_reads) in [(3, 1), (4, 2), (5, 3), (6, 4)] {
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
    };

    let (result, ()) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
        driver
    );
    drop(input_tx);
    result.expect("budget exhaustion exits the live loop cleanly");
    let exit_reason = workspace.exit_reason();
    assert_eq!(
        exit_reason,
        Some("daemon unavailable"),
        "the shared socket-and-roster retry budget must latch the live-loop exit"
    );
    assert_eq!(mock.websocket_handshakes(), 6);
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|request| {
                request.method == "GET" && request.target.starts_with("/api/terminals?")
            })
            .count(),
        4,
        "one startup roster plus three failed reconnect rosters share the budget"
    );
    assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 1);
    assert_eq!(
        websocket_requests(&mock, "terminal_take_control").len(),
        1,
        "failed reconciliation never restores writable control"
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
    workspace.select_project("project-1");
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

    let (result, old_attachment) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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

        let (result, ()) = tokio::join!(
            run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
