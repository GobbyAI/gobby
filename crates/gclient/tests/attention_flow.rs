//! 3.3.3 attention respond through the daemon API.

mod mock_daemon;

use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::run_live_loop;
use gobby_client::app::run_loop::RENDER_TICK;
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::chrome::attention_label;
use gobby_client::ui::dialogs::Dialog;
use gobby_client::ui::sidebar::{attention_body_rect, expanded_sections};
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

/// 2.3 attention click: the row jumps to its terminal and opens that entry's
/// prompt, and the row names the terminal by title and tmux address, never
/// by the session uuid the entry is keyed on.
#[tokio::test]
async fn attention_click_jumps_and_labels_the_terminal() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [{
                    "terminal_id": "terminal-1",
                    "backend": "tmux",
                    "state": "live",
                    "title": "15",
                    "session_id": "sess-1",
                    "attach": {"backend": "tmux", "pane_id": "%15"}
                }],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let roster = json!({
        "epoch": "attention-1",
        "seq": 1,
        "entries": [{
            "entry_id": "session:sess-1",
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
        }]
    });
    for _ in 0..4 {
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
        .expect("install initial attachments");

    // Where the loop draws the one attention row: the first body row of the
    // attention section, which only the sidebar geometry decides.
    let area = Rect::new(0, 0, 64, 18);
    let mut probe = Chrome::dark();
    probe.compute_view(&workspace, area);
    let (_, attention) = expanded_sections(probe.view.sidebar_rect, None);
    let body = attention_body_rect(attention, false);
    let (column, row) = (body.x + 1, body.y);

    let mut terminal =
        Terminal::new(TestBackend::new(area.width, area.height)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", 1).await;
        // The hit map exists once the loop has drawn; the first render tick
        // fires as soon as the loop starts selecting.
        tokio::time::sleep(RENDER_TICK * 4).await;
        let fetched = http_requests(&mock, "GET", "/api/attention/roster");
        send_mouse(
            &input_tx,
            MouseEventKind::Down(MouseButton::Left),
            column,
            row,
        )
        .await;
        wait_for_http_requests(&mock, "GET", "/api/attention/roster", fetched + 1).await;
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

    assert!(
        matches!(&chrome.dialog, Some(Dialog::Respond { entry_id, .. }) if entry_id == "session:sess-1"),
        "the clicked entry's prompt opens: {:?}",
        chrome.dialog
    );
    assert_eq!(chrome.mode, Mode::Respond);
    let pane = workspace
        .pane_for_terminal("terminal-1")
        .expect("terminal pane");
    assert_eq!(
        chrome.focused_pane(),
        Some(pane),
        "the click jumps to the terminal"
    );
    assert_eq!(attention_label(&workspace, "session:sess-1"), "15 %15");

    terminal
        .draw(|frame| {
            render_workspace(frame, &workspace, &chrome);
        })
        .expect("render attention row");
    let screen: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(screen.contains("15 %15"), "rendered UI: {screen:?}");
    assert!(!screen.contains("sess-1"), "rendered UI: {screen:?}");
    mock.shutdown().await;
}
