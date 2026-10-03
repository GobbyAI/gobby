//! `gclient select` from a second client brings a running window to that tab
//! and pane, while another window's focus-hint persistence leaves it alone
//! (#23265).

mod mock_daemon;

use std::fs;
use std::process::{Command, Output};
use std::time::Duration;

use gobby_client::app::run_live_loop;
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::timeout;

const TOKEN: &str = "local-token";
const PROJECT: &str = "project-1";
// `seed_workspace` mints ids in order: the first tab holds terminal-a and
// terminal-b (focused), the second holds terminal-c.
const FIRST_TAB: &str = "mock-tab-1";
const FIRST_PANE: &str = "mock-pane-2";
const SECOND_TAB: &str = "mock-tab-4";
const SECOND_PANE: &str = "mock-pane-5";

fn websocket_requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!(kind)))
        .collect()
}

/// The `(tab, pane)` of each focus hint the running window persisted.
fn window_hints(mock: &MockDaemon) -> Vec<(Value, Value)> {
    websocket_requests(mock, "workspace_op")
        .into_iter()
        .filter(|op| op["op"] == "workspace.set_focus_hints")
        .map(|op| (op["tab"].clone(), op["pane"].clone()))
        .collect()
}

async fn wait_until(what: &str, mut condition: impl FnMut() -> bool) {
    timeout(Duration::from_secs(10), async {
        while !condition() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {what}"));
}

async fn two_tab_loop(mock: &MockDaemon) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    let items: Vec<Value> = ["terminal-a", "terminal-b", "terminal-c"]
        .iter()
        .map(|id| json!({"terminal_id": id, "backend": "native", "state": "live"}))
        .collect();
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
    let daemon = LiveDaemon::connect(mock.url(), TOKEN)
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    mock.seed_workspace(
        PROJECT,
        &[
            (&["terminal-a", "terminal-b"], "terminal-b"),
            (&["terminal-c"], "terminal-c"),
        ],
    );
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project(PROJECT);
    (workspace, home)
}

/// Run the `gclient select` binary as a second client of `mock`.
async fn select(mock: &MockDaemon, args: &[&str]) -> Output {
    let directory = tempfile::tempdir().expect("token directory");
    let token_file = directory.path().join("token");
    fs::write(&token_file, TOKEN).expect("token file");
    let url = mock.url().to_owned();
    let args: Vec<String> = args.iter().map(|arg| (*arg).to_owned()).collect();
    tokio::task::spawn_blocking(move || {
        Command::new(env!("CARGO_BIN_EXE_gclient"))
            .arg("select")
            .arg("--daemon-url")
            .arg(url)
            .arg("--token-file")
            .arg(token_file)
            .args(&args)
            .env_remove("GOBBY_PANE_REF")
            .env_remove("GOBBY_WORKSPACE_ID")
            .env_remove("GOBBY_TAB_ID")
            .output()
            .expect("run gclient select")
    })
    .await
    .expect("join gclient select")
}

/// Select from a second client and hand the daemon's event to the window.
async fn select_and_broadcast(mock: &MockDaemon, args: &[&str]) {
    let output = select(mock, args).await;
    assert_eq!(
        output.status.code(),
        Some(0),
        "select {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let event = mock
        .published_workspace_events()
        .pop()
        .expect("select publishes an event");
    mock.send_event_and_wait(event).await;
}

fn shown(workspace: &Workspace<LiveDaemon>, chrome: &Chrome) -> (String, Option<String>) {
    let tab = chrome.tabs().tabs[chrome.active_index()].id.clone();
    let pane = chrome.focused_pane().and_then(|pane| {
        ["terminal-a", "terminal-b", "terminal-c"]
            .into_iter()
            .find(|terminal| workspace.pane_for_terminal(terminal) == Some(pane))
            .map(str::to_owned)
    });
    (tab, pane)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn select_from_a_second_client_switches_the_running_window() {
    let mock = MockDaemon::start(TOKEN).await;
    let (mut workspace, _home) = two_tab_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_until("the window's first take", || {
            !websocket_requests(&mock, "terminal_take_control").is_empty()
        })
        .await;
        // Pane form, onto the other tab: the window persists the focus it
        // now shows, which is the signal that it switched.
        select_and_broadcast(
            &mock,
            &[
                SECOND_PANE,
                "--workspace",
                "default",
                "--tab-ref",
                SECOND_TAB,
            ],
        )
        .await;
        wait_until("the window to show the second tab's pane", || {
            window_hints(&mock).contains(&(json!(SECOND_TAB), json!(SECOND_PANE)))
        })
        .await;
        // Tab form: back to the first tab, on that tab's own focused pane.
        select_and_broadcast(&mock, &[FIRST_TAB, "--workspace", "default"]).await;
        wait_until("the window to show the first tab", || {
            window_hints(&mock).last() == Some(&(json!(FIRST_TAB), json!("mock-pane-3")))
        })
        .await;
        // Pane form inside the shown tab.
        select_and_broadcast(
            &mock,
            &[FIRST_PANE, "--workspace", "default", "--tab-ref", FIRST_TAB],
        )
        .await;
        wait_until("the window to focus the selected pane", || {
            window_hints(&mock).last() == Some(&(json!(FIRST_TAB), json!(FIRST_PANE)))
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
        shown(&workspace, &chrome),
        (FIRST_TAB.to_owned(), Some("terminal-a".to_owned()))
    );
    let selects: Vec<Value> = websocket_requests(&mock, "workspace_op")
        .into_iter()
        .filter(|op| op["op"] == "workspace.select")
        .collect();
    assert_eq!(selects.len(), 3, "each select is an explicit request");
    mock.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn another_windows_focus_hint_leaves_the_running_window_alone() {
    let mock = MockDaemon::start(TOKEN).await;
    let (mut workspace, _home) = two_tab_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(32);

    let driver = async {
        wait_until("the window's first take", || {
            !websocket_requests(&mock, "terminal_take_control").is_empty()
        })
        .await;
        // Another window persists its own focus on the second tab.
        let events = mock.apply_workspace_op(json!({
            "op": "workspace.set_focus_hints",
            "workspace": null,
            "project_id": PROJECT,
            "tab": SECOND_TAB,
            "pane": SECOND_PANE,
        }));
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["kind"], "focus_hints");
        mock.send_event_and_wait(events[0].clone()).await;
        // A select after it is applied after it, so once the window shows the
        // select, the hint has been applied too.
        select_and_broadcast(
            &mock,
            &[FIRST_PANE, "--workspace", "default", "--tab-ref", FIRST_TAB],
        )
        .await;
        wait_until("the window to focus the selected pane", || {
            window_hints(&mock).last() == Some(&(json!(FIRST_TAB), json!(FIRST_PANE)))
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
    assert!(
        !window_hints(&mock)
            .iter()
            .any(|(tab, _)| tab == &json!(SECOND_TAB)),
        "the window never showed the hinted tab: {:?}",
        window_hints(&mock)
    );
    assert_eq!(
        shown(&workspace, &chrome),
        (FIRST_TAB.to_owned(), Some("terminal-a".to_owned()))
    );
    mock.shutdown().await;
}
