//! A direct pane keeps scrolling, focus and typing while the daemon is away
//! (#24815). The host's frame socket stays up across the outage, so gclient
//! scrolls and types through it. Every attached native pane is writable: no
//! grant, take, or reply from the daemon or the host's control socket gates a
//! key (#25653). A key it cannot deliver says so instead of being dropped.

use super::mock_daemon;

use std::sync::Arc;
use std::time::Duration;

use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEventKind};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::ClientMessage;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::{
    live_workspace_on_direct_host, live_workspace_on_direct_host_terminals, DirectHost, MockDaemon,
};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};
use tokio::time::{sleep, timeout};

const WAIT: Duration = Duration::from_secs(10);
const TOKEN: &str = "local-token";
const CONTROL_TOKEN: &str = "control-token";
const PANE_A: &str = "terminal-outage-a";
const PANE_B: &str = "terminal-outage-b";
/// The status a key shows when a pane has no host to type into during an outage.
const DAEMON_DOWN_INPUT: &str = "daemon is down; input not sent";
const DAEMON_DOWN_SCROLL: &str = "daemon is down; scroll not sent";

fn websocket_requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!(kind)))
        .collect()
}

/// The attachment the last `terminal_focus` report for `terminal_id` names,
/// while that report says the pane is focused.
fn focused_attachment(mock: &MockDaemon, terminal_id: &str) -> Option<String> {
    websocket_requests(mock, "terminal_focus")
        .into_iter()
        .rev()
        .find(|request| request["terminal_id"] == terminal_id)
        .filter(|request| request["focused"] == true)
        .and_then(|request| request["attachment_id"].as_str().map(str::to_string))
}

/// The terminal the loop's startup focus reported focused, once it has.
fn startup_focus(mock: &MockDaemon) -> Option<String> {
    websocket_requests(mock, "terminal_focus")
        .into_iter()
        .find(|request| request["focused"] == true)
        .and_then(|request| request["terminal_id"].as_str().map(str::to_string))
}

/// Wait for the loop's startup focus report: the loop runs with a live,
/// focused pane.
async fn wait_for_startup_focus(mock: &MockDaemon) {
    wait_until("the startup focus report", || startup_focus(mock).is_some()).await;
}

/// Wait for `predicate`, yielding the loop, then fail with `what`.
async fn wait_until(what: &str, mut predicate: impl FnMut() -> bool) {
    timeout(WAIT, async {
        loop {
            if predicate() {
                return;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {what}"));
}

fn show_roster(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    for terminal_id in workspace.roster_terminal_ids() {
        if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
            chrome.open_pane(pane, workspace.pane(pane).display_name());
        }
    }
}

/// Each shown pane's terminal and the cell inside its content, as the loop's
/// chrome lays them out in a 120x40 terminal.
fn content_cells(workspace: &Workspace<LiveDaemon>) -> Vec<(String, (u16, u16))> {
    let mut probe = Chrome::dark();
    show_roster(workspace, &mut probe);
    probe.compute_view(workspace, Rect::new(0, 0, 120, 40));
    probe
        .view
        .pane_infos
        .iter()
        .map(|info| {
            let pane = probe.pane_for_slot(info.id).expect("slot pane");
            (
                workspace.pane(pane).terminal_id.clone(),
                (info.inner_rect.x + 1, info.inner_rect.y + 1),
            )
        })
        .collect()
}

fn cell_of(cells: &[(String, (u16, u16))], terminal_id: &str) -> (u16, u16) {
    cells
        .iter()
        .find(|(id, _)| id == terminal_id)
        .map(|(_, cell)| *cell)
        .expect("pane cell")
}

async fn send_event(input: &mpsc::Sender<RawInputEvent>, event: RawInputEvent) {
    input.send(event).await.expect("live loop input");
}

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode) {
    send_event(
        input,
        RawInputEvent::Key(TerminalKey::new(code, KeyModifiers::NONE)),
    )
    .await;
}

async fn send_click(input: &mpsc::Sender<RawInputEvent>, (column, row): (u16, u16)) {
    for kind in [
        MouseEventKind::Down(MouseButton::Left),
        MouseEventKind::Up(MouseButton::Left),
    ] {
        send_event(
            input,
            RawInputEvent::Mouse(crossterm::event::MouseEvent {
                kind,
                column,
                row,
                modifiers: KeyModifiers::NONE,
            }),
        )
        .await;
    }
}

async fn send_wheel(input: &mpsc::Sender<RawInputEvent>, (column, row): (u16, u16)) {
    send_event(
        input,
        RawInputEvent::Mouse(crossterm::event::MouseEvent {
            kind: MouseEventKind::ScrollUp,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
}

/// Let the loop and the host see the events sent so far.
async fn settle() {
    for _ in 0..64 {
        tokio::task::yield_now().await;
    }
    sleep(Duration::from_millis(100)).await;
}

/// Drop the daemon's socket and keep its reconnect held, so the loop runs
/// through the outage. Returns the hold to release after the loop exits.
async fn daemon_outage(mock: &MockDaemon) -> Arc<Notify> {
    let reconnect = mock.pause_next_websocket();
    mock.close_websockets_going_away();
    wait_until("the held daemon reconnect", || {
        mock.websocket_handshakes() >= 2
    })
    .await;
    reconnect
}

/// The terminal the loop's startup focus reported, and the other one.
fn focused_and_other(mock: &MockDaemon) -> (String, String) {
    let focused = startup_focus(mock).expect("startup focus reported");
    let other = if focused == PANE_A { PANE_B } else { PANE_A }.to_string();
    (focused, other)
}

fn typed(seen: &[ClientMessage], key: &[u8]) -> bool {
    seen.iter()
        .any(|message| matches!(message, ClientMessage::Input { data } if data == key))
}

/// Two direct panes on one host with the daemon up.
async fn two_direct_panes(
    mock: &MockDaemon,
    host: &DirectHost,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    live_workspace_on_direct_host_terminals(mock, host, &[PANE_A, PANE_B]).await
}

/// Every attached native pane is writable: a key goes straight to its host
/// stream with no reply asked of the daemon or of the host's control socket.
/// No take, grant, or read-only state gates it (#25653).
#[tokio::test]
async fn attached_native_pane_types_without_waiting_on_a_control_reply() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let mut host = DirectHost::start("epoch-1").await;
    // The host serves a control socket, so a grant could be asked of it.
    host.serve_gterm_control(CONTROL_TOKEN);
    let (mut workspace, _home) = two_direct_panes(&mock, &host).await;
    let cells = content_cells(&workspace);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_startup_focus(&mock).await;
        let (_, other) = focused_and_other(&mock);
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        host.wait_for("the key on the host stream", |seen| typed(seen, b"y"))
            .await;
        // Focus is reported to the daemon, but nothing waited on it.
        wait_until("the focus report for the clicked pane", || {
            focused_attachment(&mock, &other).is_some()
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
    result.expect("live loop");
    assert_eq!(
        host.control_hellos(),
        0,
        "typing never asks the host's control socket for anything"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "a native pane's key never goes through the daemon"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn outage_wheel_scrolls_a_direct_pane_without_the_daemon() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-outage-scroll";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let cell = cell_of(&content_cells(&workspace), terminal_id);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_startup_focus(&mock).await;
        let reconnect = daemon_outage(&mock).await;
        send_wheel(&input_tx, cell).await;
        host.wait_for("the scroll offset on the frame socket", |seen| {
            seen.iter().any(|message| {
                matches!(
                    message,
                    ClientMessage::SetScrollOffset {
                        rows_from_live_edge: 3
                    }
                )
            })
        })
        .await;
        drop(input_tx);
        reconnect
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, reconnect) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    reconnect.notify_one();
    result.expect("daemon-down scroll loop");
    assert_eq!(
        workspace.pane(pane_id).scroll_offset(),
        3,
        "the pane mirrors the offset it asked the host for"
    );
    assert!(
        websocket_requests(&mock, "terminal_set_scroll_offset").is_empty(),
        "a direct pane scrolls on its frame socket, never through the daemon"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// While the daemon is away, a key typed into a newly focused native pane goes
/// straight onto its host stream. Nothing asks the host's control socket for a
/// grant, so no reply can hold the key (#25653).
#[tokio::test]
async fn outage_typing_reaches_the_host_without_a_grant_reply() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let mut host = DirectHost::start("epoch-1").await;
    host.serve_gterm_control(CONTROL_TOKEN);
    let (mut workspace, _home) = two_direct_panes(&mock, &host).await;
    let cells = content_cells(&workspace);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_startup_focus(&mock).await;
        let (_, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        host.wait_for("the key on the host stream during the outage", |seen| {
            typed(seen, b"y")
        })
        .await;
        drop(input_tx);
        reconnect
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, reconnect) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    reconnect.notify_one();
    result.expect("outage typing loop");
    assert_eq!(
        host.control_hellos(),
        0,
        "outage typing never asks the host's control socket for a grant"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "no key goes through a daemon that is away"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn outage_key_into_a_proxied_pane_shows_status_instead_of_dropping() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-outage-proxy";
    for _ in 0..2 {
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
    let daemon = LiveDaemon::connect(mock.url(), TOKEN)
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install the proxied attachment");
    let pane_id = workspace.pane_for_terminal(terminal_id).expect("pane");
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_startup_focus(&mock).await;
        let reconnect = daemon_outage(&mock).await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        settle().await;
        drop(input_tx);
        reconnect
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, reconnect) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    reconnect.notify_one();
    result.expect("proxied outage loop");
    assert_eq!(
        workspace.pane(pane_id).status_message(),
        Some(DAEMON_DOWN_INPUT),
        "a proxied pane cannot type while the daemon is away, and says so"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "no input goes through a daemon that is away"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn outage_wheel_into_a_proxied_pane_shows_status_instead_of_dropping() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-outage-proxy-scroll";
    for _ in 0..2 {
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
    let daemon = LiveDaemon::connect(mock.url(), TOKEN)
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("install the proxied attachment");
    let pane_id = workspace.pane_for_terminal(terminal_id).expect("pane");
    let cells = content_cells(&workspace);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_startup_focus(&mock).await;
        let reconnect = daemon_outage(&mock).await;
        send_wheel(&input_tx, cell_of(&cells, terminal_id)).await;
        settle().await;
        drop(input_tx);
        reconnect
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, reconnect) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    reconnect.notify_one();
    result.expect("proxied outage scroll loop");
    assert_eq!(
        workspace.pane(pane_id).scroll_offset(),
        0,
        "a proxied pane's scroll offset is not set while the daemon is away"
    );
    assert_eq!(
        workspace.pane(pane_id).status_message(),
        Some(DAEMON_DOWN_SCROLL),
        "a proxied pane cannot scroll while the daemon is away, and says so"
    );
    assert!(
        websocket_requests(&mock, "terminal_set_scroll_offset").is_empty(),
        "no scroll goes through a daemon that is away"
    );
    mock.shutdown().await;
}
