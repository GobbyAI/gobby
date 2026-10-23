//! A direct pane keeps scrolling, focus and typing while the daemon is away
//! (#24815). The host's frame socket and its control socket stay up across the
//! outage, so gclient scrolls through the frame socket and asks the control
//! socket for the grant a newly focused pane needs. A key it cannot deliver
//! says so instead of being dropped.

mod mock_daemon;

use std::os::unix::fs::PermissionsExt;
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
/// The status a key shows when the host's control socket could not be asked.
const CONTROL_UNREACHABLE: &str =
    "terminal control unavailable while the daemon is down; input not sent";
/// The status a key shows when the host refused the grant it asked for.
const HOST_GRANT_UNAVAILABLE: &str = "terminal did not grant input; take control again";
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

async fn wait_for_websocket_requests(mock: &MockDaemon, kind: &str, expected: usize) {
    timeout(WAIT, async {
        loop {
            if websocket_requests(mock, kind).len() >= expected {
                return;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("mock daemon never saw {expected} {kind} requests"));
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

/// The terminal the loop's startup focus took control of, and the other one.
fn focused_and_other(mock: &MockDaemon) -> (String, String) {
    let focused = websocket_requests(mock, "terminal_take_control")
        .first()
        .and_then(|request| request["terminal_id"].as_str())
        .expect("startup focus takes control")
        .to_string();
    let other = if focused == PANE_A { PANE_B } else { PANE_A }.to_string();
    (focused, other)
}

fn grant_for(host: &DirectHost, terminal_id: &str) -> Option<Value> {
    host.control_requests().into_iter().find(|request| {
        request["method"] == "grant_input" && request["host_terminal_id"] == terminal_id
    })
}

fn typed(seen: &[ClientMessage], key: &[u8]) -> bool {
    seen.iter()
        .any(|message| matches!(message, ClientMessage::Input { data } if data == key))
}

/// Two direct panes on one host, the daemon up and the startup focus taken.
/// Each pane's attachment is read before the loop starts, while it is stable.
async fn two_direct_panes(
    mock: &MockDaemon,
    host: &DirectHost,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    live_workspace_on_direct_host_terminals(mock, host, &[PANE_A, PANE_B]).await
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
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
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

#[tokio::test]
async fn outage_click_moves_focus_and_types_through_a_grant_from_the_host() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let mut host = DirectHost::start("epoch-1").await;
    host.serve_gterm_control(CONTROL_TOKEN);
    let (mut workspace, _home) = two_direct_panes(&mock, &host).await;
    let cells = content_cells(&workspace);
    let attachment = |terminal_id: &str| {
        let pane = workspace.pane_for_terminal(terminal_id).expect("pane");
        workspace.pane(pane).attachment_id().to_string()
    };
    let attachments = [(PANE_A, attachment(PANE_A)), (PANE_B, attachment(PANE_B))];
    let attachment_of = |terminal_id: &str| {
        attachments
            .iter()
            .find(|(id, _)| *id == terminal_id)
            .map(|(_, attachment)| attachment.clone())
            .expect("attachment")
    };
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let (focused, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        wait_until("the grant for the newly focused pane", || {
            grant_for(&host, &other).is_some()
        })
        .await;
        host.wait_for("the key typed into the granted pane", |seen| {
            typed(seen, b"y")
        })
        .await;
        drop(input_tx);
        (focused, other, reconnect)
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, (focused, other, reconnect)) = tokio::join!(
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
    result.expect("daemon-down focus loop");

    let grant = grant_for(&host, &other).expect("the host was asked for the grant");
    assert_eq!(
        grant["attachment_id"],
        attachment_of(&other),
        "the grant names the newly focused pane's attachment"
    );
    assert!(
        host.control_requests().iter().any(|request| {
            request["method"] == "revoke_input"
                && request["host_terminal_id"] == focused.as_str()
                && request["attachment_id"] == attachment_of(&focused)
        }),
        "leaving a pane revokes its host grant, as a daemon release would"
    );
    assert_eq!(host.control_hellos(), 2, "one authenticated hello per verb");
    assert!(workspace.pane(pane_of(&workspace, &other)).direct_input());
    assert!(!workspace.pane(pane_of(&workspace, &focused)).direct_input());
    assert!(
        websocket_requests(&mock, "terminal_release_control").is_empty(),
        "focus moves during the outage without a daemon release"
    );
    assert!(websocket_requests(&mock, "terminal_input").is_empty());
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn outage_typing_waits_for_the_grant_reply_before_any_input() {
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
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let (_, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        let gate = host.hold_control_replies();
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        wait_until("the grant request to reach the host", || {
            grant_for(&host, &other).is_some()
        })
        .await;
        sleep(Duration::from_millis(100)).await;
        assert!(
            !typed(&host.drain(), b"y"),
            "no key reaches the frame socket before the host answers the grant"
        );
        host.release_control_replies(&gate);
        host.wait_for("the held key, once granted", |seen| typed(seen, b"y"))
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
    result.expect("held grant loop");
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn refused_outage_grant_shows_the_grant_status_and_sends_no_input() {
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
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let (_, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        host.refuse_grants();
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        wait_until("the refused grant request", || {
            grant_for(&host, &other).is_some()
        })
        .await;
        sleep(Duration::from_millis(100)).await;
        drop(input_tx);
        (other, reconnect)
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, (other, reconnect)) = tokio::join!(
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
    result.expect("refused grant loop");
    let pane = workspace.pane(pane_of(&workspace, &other));
    assert_eq!(
        pane.status_message(),
        Some(HOST_GRANT_UNAVAILABLE),
        "a refused grant says so on the pane"
    );
    assert!(!pane.direct_input(), "a refused grant cannot type");
    assert!(
        !typed(&host.drain(), b"y"),
        "the refused key never reaches the frame socket"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn outage_grant_without_a_control_socket_shows_status_and_sends_no_input() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    // No control socket: the host never served one, so nothing can connect.
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = two_direct_panes(&mock, &host).await;
    let cells = content_cells(&workspace);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let (_, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        settle().await;
        drop(input_tx);
        (other, reconnect)
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, (other, reconnect)) = tokio::join!(
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
    result.expect("no control socket loop");
    let pane = workspace.pane(pane_of(&workspace, &other));
    assert_eq!(pane.status_message(), Some(CONTROL_UNREACHABLE));
    assert!(!pane.direct_input());
    assert!(
        !typed(&host.drain(), b"y"),
        "the key is not sent without a grant"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn outage_grant_refuses_a_control_token_that_is_not_owner_only() {
    let mock = MockDaemon::start(TOKEN).await;
    mock.use_unique_attachment_ids();
    let mut host = DirectHost::start("epoch-1").await;
    host.serve_gterm_control(CONTROL_TOKEN);
    let token_path = host
        .socket_path()
        .parent()
        .expect("socket dir")
        .join("gterm-control.token");
    std::fs::set_permissions(&token_path, std::fs::Permissions::from_mode(0o644))
        .expect("loosen the token");
    let (mut workspace, _home) = two_direct_panes(&mock, &host).await;
    let cells = content_cells(&workspace);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let (_, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        settle().await;
        drop(input_tx);
        (other, reconnect)
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, (other, reconnect)) = tokio::join!(
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
    result.expect("loose token loop");
    assert_eq!(
        host.control_hellos(),
        0,
        "gclient never speaks to a control socket whose token is readable by others"
    );
    let pane = workspace.pane(pane_of(&workspace, &other));
    assert_eq!(pane.status_message(), Some(CONTROL_UNREACHABLE));
    assert!(!typed(&host.drain(), b"y"));
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
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
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
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
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

#[tokio::test]
async fn outage_grant_is_adopted_when_the_daemon_returns() {
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
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let (_, other) = focused_and_other(&mock);
        let reconnect = daemon_outage(&mock).await;
        send_click(&input_tx, cell_of(&cells, &other)).await;
        settle().await;
        send_key(&input_tx, KeyCode::Char('y')).await;
        wait_until("the outage grant for the newly focused pane", || {
            grant_for(&host, &other).is_some()
        })
        .await;
        reconnect.notify_one();
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        send_key(&input_tx, KeyCode::Char('z')).await;
        host.wait_for("the key typed after the daemon returned", |seen| {
            typed(seen, b"z")
        })
        .await;
        drop(input_tx);
        other
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, other) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("daemon-return adoption loop");
    assert!(
        workspace.pane(pane_of(&workspace, &other)).direct_input(),
        "the returned daemon leaves the pane typing through its adopted grant"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "no key went through the daemon before or after it returned"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

fn pane_of(workspace: &Workspace<LiveDaemon>, terminal_id: &str) -> gobby_client::app::PaneId {
    workspace.pane_for_terminal(terminal_id).expect("pane")
}
