//! 4.3.3: the daemon's workspace rows carry the focus hints. A window seeds
//! its focus from them and writes them back whenever its focus moves, so the
//! next window (or the next start) lands where the last actor left off.

mod mock_daemon;

use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::{Daemon, LiveDaemon, WorkspaceOp};
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::timeout;

fn terminal_page(ids: &[&str]) -> Value {
    json!({
        "items": ids
            .iter()
            .map(|id| json!({
                "terminal_id": id,
                "backend": "native",
                "state": "live",
                "ownership": "gobby"
            }))
            .collect::<Vec<_>>(),
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
    })
}

fn focus_hint_ops(mock: &MockDaemon) -> Vec<(String, String)> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| {
            body.get("type") == Some(&json!("workspace_op"))
                && body.get("op") == Some(&json!("workspace.set_focus_hints"))
        })
        .map(|op| {
            (
                op["tab"].as_str().unwrap_or_default().to_string(),
                op["pane"].as_str().unwrap_or_default().to_string(),
            )
        })
        .collect()
}

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("live loop input");
}

async fn wait_until(mut condition: impl FnMut() -> bool) {
    timeout(Duration::from_secs(2), async {
        while !condition() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("condition holds before the deadline");
}

/// The rows say the second tab and its pane were focused last; the window
/// opens there, and selecting the next tab writes that tab and its focused
/// pane back as the new hints.
#[tokio::test]
async fn focus_hints_seed_and_follow_the_last_actor() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let ids = ["terminal-a", "terminal-b", "terminal-c"];
    mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&ids));
    mock.enqueue("GET", "/api/terminals?", 200, terminal_page(&ids));
    // mock-tab-1 holds mock-pane-2 (a) and mock-pane-3 (b, focused);
    // mock-tab-4 holds mock-pane-5 (c). The seed points the workspace's
    // hints at the first tab, so a previous actor moves them to the second.
    mock.seed_workspace(
        "project-1",
        &[
            (&["terminal-a", "terminal-b"], "terminal-b"),
            (&["terminal-c"], "terminal-c"),
        ],
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let workspace_id = daemon
        .attach_workspace(None, None)
        .await
        .expect("attach the seeded workspace")
        .workspace
        .id;
    daemon
        .workspace_op(WorkspaceOp::WorkspaceSetFocusHints {
            workspace: workspace_id,
            project_id: Some("project-1".to_string()),
            tab: Some("mock-tab-4".to_string()),
            pane: Some("mock-pane-5".to_string()),
            node: None,
        })
        .await
        .expect("a previous actor leaves the focus on the second tab");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        // The window's first hint echoes what it seeded from the rows.
        wait_until(|| !focus_hint_ops(&mock).is_empty()).await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('n'), KeyModifiers::NONE).await;
        wait_until(|| {
            focus_hint_ops(&mock)
                .iter()
                .any(|(tab, _)| tab == "mock-tab-1")
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

    // The loop may echo the seeded hint once more after its own attach;
    // the order of distinct hints is what the rows see.
    let mut hints = focus_hint_ops(&mock);
    hints.dedup();
    assert_eq!(
        hints,
        [
            ("mock-tab-4".to_string(), "mock-pane-5".to_string()),
            ("mock-tab-1".to_string(), "mock-pane-3".to_string()),
        ],
        "the window seeds from the rows' hints and writes the next focus back"
    );
    // The daemon's echo of the last hint may still be queued when the input
    // closes (the loop exits before applying it), so the request is the
    // contract; the window itself is on the first tab.
    assert_eq!(
        chrome.active_tab().map(|tab| tab.id.as_str()),
        Some("mock-tab-1"),
        "the next-tab chord moved the window to the first tab"
    );
    mock.shutdown().await;
}
