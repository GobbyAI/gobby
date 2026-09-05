//! 3.3.5 select → spawn → attach → terminate against scripted endpoints.

mod mock_daemon;

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use gobby_client::app::run_loop::{
    run_scripted_loop, ReconnectAttempt, ReconnectSupervisor, RENDER_TICK,
};
use gobby_client::app::{run_live_loop, AttachState};
use gobby_client::daemon::{
    Answer, Daemon, DaemonError, EventReceiver, Generation, KillOutcome, LiveDaemon, Page,
    RosterEntry, ScriptedDaemon, SpawnOutcome, SpawnRequest, SubscribeSnapshot, TerminalRow,
    WsMessage, WsReply,
};
use gobby_client::frame_source::{PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_client::startup::Ready;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::{CellData, FrameData, PaneModes, ServerMessage};
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
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

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("live loop input");
}

async fn settle_live_event() {
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
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
        project: None,
        host: None,
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
        assert_eq!(detaches.len(), 1);
        assert_eq!(
            detaches[0].get("attachment_id").and_then(Value::as_str),
            Some(old_attachment.as_str())
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

#[test]
fn select_spawn_attach_terminate_loop() {
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

#[test]
fn latched_exit_issues_no_further_requests() {
    let mut ws = Workspace::scripted();
    ws.select_project("proj-exit");
    let pane = ws
        .open_terminal("term-exit", "native", "epoch-exit")
        .expect("open terminal");
    ws.force_held(pane);
    let sent_before = ws.daemon().ws_sent().len();

    assert!(ws.latch_exit("quit"));
    assert!(!ws.latch_exit("duplicate quit"));
    assert_eq!(ws.exit_reason(), Some("quit"));
    assert!(ws.send_input(pane, b"blocked").is_err());
    assert!(ws.take_control(pane).is_err());
    assert!(ws.release_control(pane).is_err());
    assert!(ws.kill_frame_stream(pane).is_err());
    assert!(ws.reconnect_daemon_ws().is_err());
    assert!(ws
        .spawn_agent(json!({"task_id": "task-after-exit"}))
        .is_err());
    assert!(ws.terminate_terminal("term-exit").is_err());

    ws.apply_ws(&json!({
        "type": "terminal_event",
        "event": "exited",
        "terminal_id": "term-exit"
    }))
    .expect("post-latch lifecycle is inert");
    assert_eq!(ws.pane_for_terminal("term-exit"), Some(pane));
    assert_eq!(ws.daemon().ws_sent().len(), sent_before);
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
    tokio::time::pause();
    let mut ws = Workspace::scripted();
    let first = ws
        .open_terminal("term-detach-1", "native", "epoch-detach")
        .expect("first pane");
    let second = ws
        .open_terminal("term-detach-2", "native", "epoch-detach")
        .expect("second pane");
    ws.kill_frame_stream(first).expect("detach first");
    ws.kill_frame_stream(second).expect("detach second");
    let daemon = ReconnectDaemon::new([Ok(Generation(2))]);
    let mut supervisor = ReconnectSupervisor::new();

    assert_eq!(
        ws.submit_expired_detaches(&mut supervisor, Instant::now()),
        0
    );
    tokio::time::advance(Duration::from_secs(2)).await;
    assert_eq!(
        ws.submit_expired_detaches(&mut supervisor, Instant::now()),
        2
    );
    assert_eq!(
        ws.submit_expired_detaches(&mut supervisor, Instant::now()),
        0,
        "a timed-out attachment submits at most one reconnect intent"
    );
    assert_eq!(
        supervisor.attempt_when_due(&daemon).await,
        ReconnectAttempt::Reconnected(Generation(2))
    );
    assert_eq!(daemon.calls().len(), 1, "one coalesced reconnect episode");
    supervisor.handshake_complete(Generation(2));
}

#[test]
fn daemon_loss_renders_read_only_until_recovery() {
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

#[test]
fn control_tombstone_retires_the_attachment() {
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
