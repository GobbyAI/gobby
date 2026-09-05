//! 3.3.3 attention respond through the daemon API.

mod mock_daemon;

use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::LiveDaemon;
use gobby_client::ui::dialogs::Dialog;
use gobby_client::ui::{render_workspace, Chrome};
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
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

async fn wait_for_http_requests(mock: &MockDaemon, method: &str, target: &str, count: usize) {
    timeout(Duration::from_secs(1), async {
        loop {
            let matching = mock
                .requests()
                .into_iter()
                .filter(|request| request.method == method && request.target.starts_with(target))
                .count();
            if matching >= count {
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

    let (result, ()) = tokio::join!(
        run_live_loop(&mut workspace, &mut terminal, &mut chrome, input_rx),
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
        .draw(|frame| render_workspace(frame, &workspace, &chrome))
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
