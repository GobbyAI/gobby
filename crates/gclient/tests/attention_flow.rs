//! 3.3.3 attention respond through the daemon API.

mod mock_daemon;

use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::chrome::attention_label;
use gobby_client::ui::dialogs::Dialog;
use gobby_client::ui::hit::SidebarSection;
use gobby_client::ui::sidebar::section_body_rect;
use gobby_client::ui::sidebar_rows::{RowKind, SidebarRow};
use gobby_client::ui::{render_workspace, Chrome, Mode};
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::json;
use tokio::sync::mpsc;
use tokio::time::{timeout, Duration};

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("live loop input");
}

async fn send_mouse(
    input: &mpsc::Sender<RawInputEvent>,
    kind: MouseEventKind,
    column: u16,
    row: u16,
) {
    input
        .send(RawInputEvent::Mouse(MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }))
        .await
        .expect("live loop mouse input");
}

fn http_requests(mock: &MockDaemon, method: &str, target: &str) -> usize {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == method && request.target.starts_with(target))
        .count()
}

fn websocket_requests(mock: &MockDaemon, kind: &str) -> usize {
    mock.requests()
        .into_iter()
        .filter(|request| {
            request.method == "WS"
                && request.body.as_ref().and_then(|body| body.get("type")) == Some(&json!(kind))
        })
        .count()
}

async fn wait_for_websocket_requests(mock: &MockDaemon, kind: &str, count: usize) {
    timeout(Duration::from_secs(1), async {
        loop {
            if websocket_requests(mock, kind) >= count {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {count} {kind} requests"));
}

async fn wait_for_http_requests(mock: &MockDaemon, method: &str, target: &str, count: usize) {
    timeout(Duration::from_secs(1), async {
        loop {
            if http_requests(mock, method, target) >= count {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {count} {method} {target} requests"));
}

#[tokio::test]
async fn respond_reaches_daemon() {
    let mock = MockDaemon::start("local-token").await;
    let roster = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [{
            "entry_id": "run:1",
            "attention": {
                "attention_id": "att-1",
                "state": "blocked",
                "kind": "actionable",
                "fingerprint": "fp-1",
                "payload": {
                    "prompt": "Ship this change?",
                    "options": [
                        {"option": 1, "label": "Approve"},
                        {"option": 2, "label": "Deny"}
                    ]
                }
            }
        }]
    });
    for _ in 0..3 {
        mock.enqueue("GET", "/api/attention/roster", 200, roster.clone());
    }
    mock.enqueue(
        "POST",
        "/api/attention/run:1/respond",
        200,
        json!({"ok": true}),
    );
    mock.enqueue(
        "POST",
        "/api/attention/run:1/respond",
        409,
        json!({"detail": {"code": "stale_episode", "message": "stale-episode"}}),
    );

    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(64, 18)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 1).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('a'), KeyModifiers::NONE).await;
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 2).await;
        send_key(&input_tx, KeyCode::Enter, KeyModifiers::NONE).await;
        wait_for_http_requests(&mock, "POST", "/api/attention/run:1/respond", 1).await;

        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('a'), KeyModifiers::NONE).await;
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 3).await;
        send_key(&input_tx, KeyCode::Enter, KeyModifiers::NONE).await;
        wait_for_http_requests(&mock, "POST", "/api/attention/run:1/respond", 2).await;
        for _ in 0..16 {
            tokio::task::yield_now().await;
        }
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

    let responses: Vec<_> = mock
        .requests()
        .into_iter()
        .filter(|request| {
            request.method == "POST" && request.target == "/api/attention/run:1/respond"
        })
        .collect();
    assert_eq!(responses.len(), 2, "a stale response must not be retried");
    for response in &responses {
        assert_eq!(
            response.body,
            Some(json!({
                "attention_id": "att-1",
                "fingerprint": "fp-1",
                "answer": {"option": 1}
            }))
        );
    }
    assert!(matches!(chrome.dialog, Some(Dialog::Respond { .. })));
    assert!(
        chrome
            .status_message
            .as_deref()
            .is_some_and(|message| message.contains("stale_episode")),
        "stale episode must remain visible: {:?}",
        chrome.status_message
    );

    terminal
        .draw(|frame| {
            render_workspace(frame, &workspace, &chrome);
        })
        .expect("render attention dialog");
    let screen: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(
        screen.contains("Ship this change?"),
        "rendered UI: {screen:?}"
    );
    assert!(screen.contains("Approve"), "rendered UI: {screen:?}");

    assert!(
        mock.requests().iter().all(|request| {
            request.method != "WS"
                || request.body.as_ref().and_then(|body| body.get("type"))
                    != Some(&json!("terminal_input"))
        }),
        "respond must use the attention API, not terminal input"
    );
    mock.shutdown().await;
}

/// 3.2 agent row click: an idle row focuses its terminal, a blocked row
/// jumps to its terminal without opening its prompt (the terminal already
/// shows it), and a row names its session by title and ref, never by the
/// uuid the entry is keyed on.
#[tokio::test]
async fn agent_row_click_jumps_and_labels_the_session() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [
                    {
                        "terminal_id": "terminal-1",
                        "backend": "tmux",
                        "state": "live",
                        "title": "15",
                        "session_id": "sess-1",
                        "attach": {"backend": "tmux", "pane_id": "%15"}
                    },
                    {
                        "terminal_id": "terminal-2",
                        "backend": "tmux",
                        "state": "live",
                        "title": "zsh",
                        "attach": {"backend": "tmux", "pane_id": "%16"}
                    }
                ],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let roster = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [
            {
                "entry_id": "session:sess-1",
                "session_id": "sess-1",
                "terminal": {"terminal_id": "terminal-1", "backend": "tmux"},
                "attention": {
                    "attention_id": "att-1",
                    "state": "blocked",
                    "kind": "actionable",
                    "fingerprint": "fp-1",
                    "payload": {
                        "prompt": "Ship this change?",
                        "options": [{"option": 1, "label": "Approve"}]
                    }
                }
            },
            {
                "entry_id": "run:terminal-2",
                "terminal": {"terminal_id": "terminal-2", "backend": "tmux"}
            }
        ]
    });
    let sessions = json!({
        "sessions": [{
            "id": "sess-1",
            "ref": "#12217",
            "title": "15",
            "status": "active",
            "source": "claude"
        }],
        "count": 1,
        "next_cursor": null
    });
    for _ in 0..4 {
        mock.enqueue("GET", "/api/attention/roster", 200, roster.clone());
        mock.enqueue(
            "GET",
            "/api/sessions?project_id=project-1",
            200,
            sessions.clone(),
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

    // Where the loop draws the two rows: both in the sessions section, the
    // blocked session on its first two-line row and the idle shell (an agent
    // run under no session) on the row after it. Wide enough that the
    // respond dialog leaves the sidebar uncovered.
    let area = Rect::new(0, 0, 120, 30);
    let mut probe = Chrome::dark();
    probe.compute_view(&workspace, area);
    let sessions = section_body_rect(
        probe.view.sidebar_section_rects[SidebarSection::Sessions.index()],
        false,
    );
    let row_height = SidebarRow {
        kind: RowKind::Agent,
        ..SidebarRow::default()
    }
    .height();
    let (column, blocked_row, idle_row) = (sessions.x + 1, sessions.y, sessions.y + row_height);

    let mut terminal =
        Terminal::new(TestBackend::new(area.width, area.height)).expect("test terminal");
    let mut chrome = Chrome::dark();
    for terminal_id in workspace.roster_terminal_ids() {
        let pane = workspace
            .pane_for_terminal(&terminal_id)
            .expect("roster pane");
        chrome.open_pane(pane, workspace.pane(pane).display_name());
    }
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        // The loop refetches the roster in its own reconcile and draws before
        // it selects, so the clicks route against a hit map holding the rows.
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 2).await;
        let taken = websocket_requests(&mock, "terminal_take_control");
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            column,
            idle_row,
        )
        .await;
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            column,
            blocked_row,
        )
        .await;
        // Each click takes control of the terminal it focuses; the blocked
        // row's click fetches no prompt.
        wait_for_websocket_requests(&mock, "terminal_take_control", taken + 2).await;
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
        chrome.dialog.is_none(),
        "a blocked row's click opens no dialog: {:?}",
        chrome.dialog
    );
    assert_ne!(chrome.mode, Mode::Respond);
    let blocked = workspace
        .pane_for_terminal("terminal-1")
        .expect("blocked terminal pane");
    let idle = workspace
        .pane_for_terminal("terminal-2")
        .expect("idle terminal pane");
    assert_eq!(
        chrome.focused_pane(),
        Some(blocked),
        "the blocked row's click jumps to its terminal"
    );
    assert_eq!(
        chrome.last_focused,
        Some(idle),
        "the idle row's click focused its terminal first"
    );
    assert_ne!(
        chrome.status_message.as_deref(),
        Some("No actionable attention prompt."),
        "an idle row asks for no prompt"
    );
    assert_eq!(attention_label(&workspace, "session:sess-1"), "#12217: 15");

    terminal
        .draw(|frame| {
            render_workspace(frame, &workspace, &chrome);
        })
        .expect("render agent rows");
    let screen: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(screen.contains("#12217: 15"), "rendered UI: {screen:?}");
    assert!(screen.contains("zsh %16"), "rendered UI: {screen:?}");
    assert!(!screen.contains("sess-1"), "rendered UI: {screen:?}");
    mock.shutdown().await;
}
