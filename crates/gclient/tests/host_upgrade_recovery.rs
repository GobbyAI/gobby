//! 2.3 gclient reconnects straight to the host across a host-image exec.
//!
//! The daemon may be away for the whole window: `Hello` authenticates against
//! the local token file, a user `AttachTerminal` needs no daemon reservation,
//! and the pane types on the restored stream with no grant (#25653). These
//! tests drive the real live loop against a real frame socket, so a reconnect
//! that went through the daemon would be visible as a second `terminal_attach`.

mod mock_daemon;

use std::time::Duration;

use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::{run_live_loop, AttachState};
use gobby_client::daemon::LiveDaemon;
use gobby_client::frame_source::Transport;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::ClientMessage;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::{live_workspace_on_direct_host, DirectHost, MockDaemon};
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::timeout;

const WAIT: Duration = Duration::from_secs(10);

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

/// Wait for the loop to report `terminal_id` focused: it runs with the pane
/// live and focused.
async fn wait_for_focus(mock: &MockDaemon, terminal_id: &str) {
    wait_until("the startup focus report", || {
        focused_attachment(mock, terminal_id).is_some()
    })
    .await;
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

/// Show every roster terminal in the chrome so the loop has a focused pane.
fn show_roster(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    for terminal_id in workspace.roster_terminal_ids() {
        if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
            chrome.open_pane(pane, workspace.pane(pane).display_name());
        }
    }
}

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode, modifiers: KeyModifiers) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
        .await
        .expect("live loop input");
}

async fn settle() {
    for _ in 0..64 {
        tokio::task::yield_now().await;
    }
}

/// 2.3.1: with the daemon disconnected across the host's exec, a pane whose
/// frame source hits EOF reconnects straight to the host, keeps the same
/// terminal and epoch, never retires, and its input types on the restored
/// stream with no grant.
#[tokio::test]
async fn pane_reconnects_to_host_without_daemon() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-host-reconnect";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let attachment_before = workspace.pane(pane_id).attachment_id().to_string();
    let epoch_before = workspace.pane(pane_id).expected_host_epoch.clone();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        // The daemon goes away and the host stream dies in the same window:
        // the exec that replaces the host image.
        mock.drop_websockets();
        host.disconnect();
        // The client reconnects straight to the host, with no daemon help.
        host.expect_attaches(2).await;
        assert!(host.connections() >= 2, "the host saw a second connect");
        // The daemon comes back and reconciles; the pane keeps the host stream
        // it just restored and takes a fresh daemon attachment, since the old
        // socket's close finalized the kept one (#23419).
        wait_until("the daemon to reconnect", || {
            mock.websocket_handshakes() >= 2
        })
        .await;
        settle().await;
        // The recovery has applied by now. A key still types into the host on
        // the restored stream: the first host write binds the attachment, then
        // delivers the payload.
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        let seen = host
            .wait_for("the carried key", |messages| {
                messages
                    .iter()
                    .any(|message| matches!(message, ClientMessage::Input { .. }))
            })
            .await;
        assert!(
            seen.iter().any(|message| matches!(
                message,
                ClientMessage::Input { data } if data == b"x"
            )),
            "the restored stream delivers the input payload: {seen:?}"
        );
        assert!(
            seen.iter()
                .any(|message| matches!(message, ClientMessage::BindAttachment { .. })),
            "the new stream is bound before it types: {seen:?}"
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
    result.expect("host reconnect loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        Some(pane_id),
        "the pane never retires"
    );
    assert!(matches!(
        workspace.pane(pane_id).attach_state(),
        AttachState::Attached { .. }
    ));
    assert_ne!(
        workspace.pane(pane_id).attachment_id(),
        attachment_before,
        "the new daemon generation issues a fresh attachment"
    );
    assert_eq!(
        workspace.pane(pane_id).expected_host_epoch,
        epoch_before,
        "the reconnect keeps the same host epoch"
    );
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    assert_eq!(
        host.attaches(),
        2,
        "the host answered exactly one reconnect, and that stream is kept"
    );
    assert_eq!(
        websocket_requests(&mock, "terminal_attach").len(),
        2,
        "the pane reconnected straight to the host, then re-registered with the daemon"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "the restored stream types on the frame socket, never through the daemon"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// Keys typed while the returned daemon replaces a native attachment reach the
/// host instead of disappearing during its host handshake. No grant gates
/// them, before or after the replacement (#25653).
#[tokio::test]
async fn native_input_during_daemon_reattach_reaches_the_host() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-daemon-reattach-input";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        let first = focused_attachment(&mock, terminal_id).expect("startup focus");
        send_key(&input_tx, KeyCode::Char('a'), KeyModifiers::NONE).await;
        host.wait_for("input before reconnect", |seen| {
            seen.iter()
                .any(|message| matches!(message, ClientMessage::Input { data } if data == b"a"))
        })
        .await;
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
        let gate = host.hold_accepts();
        mock.close_websockets_going_away();
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        // The daemon answered the replacement attach, but the host cannot yet
        // answer its handshake. This is a live-loop input event in that window.
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::NONE).await;
        settle().await;
        let early: Vec<_> = websocket_requests(&mock, "terminal_focus")
            .into_iter()
            .filter(|request| {
                request["focused"] == true && request["attachment_id"] != first.as_str()
            })
            .collect();
        assert!(
            early.is_empty(),
            "the new attachment is not reported focused before its host is ready: {early:?}"
        );
        host.release_accepts(&gate);
        host.wait_for("the key typed during native reattachment", |seen| {
            seen.iter()
                .any(|message| matches!(message, ClientMessage::Input { data } if data == b"b"))
        })
        .await;
        wait_until("focus on the replacement attachment", || {
            focused_attachment(&mock, terminal_id).is_some_and(|id| id != first)
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
    result.expect("native reattachment input loop");
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "native input typed during the reattachment stays on the host stream"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// 2.3.2: an epoch change or an exhausted budget falls back to the daemon
/// re-attach, which defers on `host_not_ready` and succeeds.
#[tokio::test]
async fn host_local_failure_falls_back_to_daemon_attach() {
    // The host replaced its image: the reconnect is answered with a new epoch,
    // which gclient cannot continue on.
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.refuse_next_proxy_attach("host_not_ready", "host not ready");
    let terminal_id = "terminal-epoch-change";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let attachment_before = workspace.pane(pane_id).attachment_id().to_string();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        host.set_host_epoch("epoch-2");
        host.disconnect();
        // The old attachment is detached and the daemon re-attach is asked
        // for; its first reply defers on `host_not_ready`, so it retries.
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        tokio::time::pause();
        for _ in 0..64 {
            if websocket_requests(&mock, "terminal_attach").len() >= 3 {
                break;
            }
            tokio::time::advance(Duration::from_secs(8)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_attach", 3).await;
        settle().await;
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
    result.expect("epoch-change fallback loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        Some(pane_id),
        "the pane survives the fallback"
    );
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Proxy),
        "the fallback lands on the daemon's proxy transport"
    );
    assert_ne!(
        workspace.pane(pane_id).attachment_id(),
        attachment_before,
        "the fallback attached through the daemon"
    );
    let attaches = websocket_requests(&mock, "terminal_attach");
    let attach_kinds: Vec<Option<&str>> = attaches
        .iter()
        .map(|request| request.get("frame_delivery").and_then(Value::as_str))
        .collect();
    assert_eq!(
        attach_kinds[1],
        Some("proxy"),
        "the fallback asks the daemon for a proxy attach: {attach_kinds:?}"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// 2.3.2 (continued): the host never comes back inside the client's budget, so
/// the reconnect gives up and the daemon re-attach takes over.
#[tokio::test]
async fn host_local_budget_falls_back_to_daemon_attach() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-budget";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let socket_path = host.socket_path().to_path_buf();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        // The host is gone for the whole exec window.
        std::fs::remove_file(&socket_path).expect("remove the host socket");
        host.disconnect();
        tokio::time::pause();
        for _ in 0..512 {
            if !websocket_requests(&mock, "terminal_detach").is_empty() {
                break;
            }
            tokio::time::advance(Duration::from_secs(1)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        settle().await;
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
    result.expect("budget fallback loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        Some(pane_id),
        "the pane survives the exhausted budget"
    );
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Proxy),
        "an exhausted host budget falls back to the daemon"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn same_epoch_attach_refusal_falls_back_to_daemon() {
    // The host answers the reconnect on the same epoch but refuses the attach
    // (its named terminal is gone) and leaves the socket open. The refusal
    // must route to the daemon fallback, never a false restore (#23076, R4 F1).
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.refuse_next_proxy_attach("host_not_ready", "host not ready");
    let terminal_id = "terminal-attach-refusal";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let attachment_before = workspace.pane(pane_id).attachment_id().to_string();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        // Same epoch: only the attach itself is refused, and the socket stays
        // open, so nothing but the reply tells the client it failed.
        host.refuse_next_attach();
        host.disconnect();
        // The refusal falls straight through to the daemon path: detach, then
        // an attach whose first reply defers on `host_not_ready`.
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        tokio::time::pause();
        for _ in 0..64 {
            if websocket_requests(&mock, "terminal_attach").len() >= 3 {
                break;
            }
            tokio::time::advance(Duration::from_secs(8)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_attach", 3).await;
        settle().await;
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
    result.expect("attach-refusal fallback loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        Some(pane_id),
        "the pane survives the refusal"
    );
    assert!(
        !websocket_requests(&mock, "terminal_detach").is_empty(),
        "the refused host attach was released before a daemon attach"
    );
    assert_ne!(
        workspace.pane(pane_id).attachment_id(),
        attachment_before,
        "the fallback attached through the daemon"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn stalled_handshake_falls_back_within_the_budget() {
    // The host accepts the connect but never answers the handshake — a host
    // still mid-exec. The absolute budget must end the recovery and reach the
    // daemon fallback; a stalled handshake is never a restore (#23076, R4 F2).
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-stalled-handshake";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    // Hold future accepts so the connect lands in the backlog and the
    // handshake never answers, exactly like a host mid-exec.
    let _gate = host.hold_accepts();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        host.disconnect();
        tokio::time::pause();
        for _ in 0..64 {
            if !websocket_requests(&mock, "terminal_detach").is_empty() {
                break;
            }
            tokio::time::advance(Duration::from_secs(1)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        settle().await;
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
    result.expect("stalled-handshake fallback loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        Some(pane_id),
        "the pane survives the stalled handshake"
    );
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Proxy),
        "a stalled handshake reaches the daemon fallback within the budget"
    );
    assert_eq!(
        host.attaches(),
        1,
        "the stalled handshake was never accepted as a host restore"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// #23419: a pane restored straight onto its host keeps an attachment the
/// daemon finalizes when its socket closes. Once the daemon is back on a new
/// generation, the pane takes a fresh attachment, keeps its host stream and
/// reports focus under the fresh id; no focus report after the first names the
/// dead one (`stale_attachment`).
#[tokio::test]
async fn host_recovered_pane_reattaches_after_daemon_generation_change() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-recovered-then-reconnected";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let attachment_before = workspace.pane(pane_id).attachment_id().to_string();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        // The host stream dies and the host-local reconnect restores it.
        host.disconnect();
        host.expect_attaches(2).await;
        settle().await;
        // The daemon socket then drops and comes back: a new generation, and
        // the daemon has finalized every attachment the old socket held.
        mock.close_websockets_going_away();
        wait_until("the daemon to reconnect", || {
            mock.websocket_handshakes() >= 2
        })
        .await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        wait_until("focus under the fresh attachment", || {
            focused_attachment(&mock, terminal_id).is_some_and(|id| id != attachment_before)
        })
        .await;
        settle().await;
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
    result.expect("host-recovered pane re-attaching after a daemon reconnect");

    assert_ne!(
        workspace.pane(pane_id).attachment_id(),
        attachment_before,
        "the pane must not keep an attachment from the dead daemon generation"
    );
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    assert_eq!(
        host.attaches(),
        2,
        "the restored host stream is kept; only the daemon attachment is new"
    );
    let stale_focus: Vec<_> = websocket_requests(&mock, "terminal_focus")
        .into_iter()
        .skip(1)
        .filter(|request| request["attachment_id"] == json!(attachment_before))
        .collect();
    assert!(
        stale_focus.is_empty(),
        "no focus report names the finalized attachment: {stale_focus:?}"
    );
    assert!(
        chrome
            .toasts
            .iter()
            .all(|active| !active.toast.title.contains("stale_attachment")),
        "no stale_attachment refusal reaches the user"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// 2.3.3: repeated failed daemon reconnect attempts and a daemon generation
/// change overlapping the host restore do not cancel the host-local recovery,
/// which succeeds. The restored pane's attachment died with the old daemon
/// socket, so once the restore lands it takes a fresh attachment on the new
/// generation and keeps its host stream (#23419).
#[tokio::test]
async fn host_local_recovery_survives_daemon_attempts() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-survives-daemon";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    // The hold is armed after the initial attach has been served, so only the
    // reconnect that follows the disconnect is held mid-exec.
    let gate = host.hold_accepts();
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let attachment_before = workspace.pane(pane_id).attachment_id().to_string();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        // The host stream dies while the host itself is mid-exec, so the
        // host-local reconnect stays in flight.
        host.disconnect();
        settle().await;
        // A daemon outage overlaps it: going away, then more failed handshakes
        // than the delay ladder has rungs, then recovery.
        for _ in 0..4 {
            mock.fail_next_websocket();
        }
        mock.close_websockets_going_away();
        wait_until("the daemon to keep retrying", || {
            mock.websocket_handshakes() >= 6
        })
        .await;
        settle().await;
        // Now the exec finishes and the host accepts.
        host.release_accepts(&gate);
        host.expect_attaches(2).await;
        // The daemon is back on a new generation that finalized the kept
        // attachment, so the pane takes a fresh one from it.
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        settle().await;
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
    result.expect("host recovery surviving daemon attempts");

    assert_ne!(
        workspace.pane(pane_id).attachment_id(),
        attachment_before,
        "the pane does not keep the attachment the old daemon socket finalized"
    );
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    assert_eq!(
        host.attaches(),
        2,
        "the host answered the reconnect, and the restored stream is kept"
    );
    assert_eq!(
        websocket_requests(&mock, "terminal_attach").len(),
        2,
        "the daemon issued a fresh attachment once the restore landed"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// 2.3.3 (continued): closing the pane during the recovery cancels it, so the
/// host is never asked to attach again.
#[tokio::test]
async fn closing_the_pane_during_recovery_sends_nothing_to_the_host() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-closed-mid-recovery";
    const BARRIER: &str = "terminal-ordering-barrier";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    // Armed after the initial attach so only the in-flight reconnect is held.
    let gate = host.hold_accepts();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        host.disconnect();
        settle().await;
        // The terminal leaves the workspace while the reconnect is in flight.
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 2}
            }),
        );
        mock.send_event_and_wait(json!({
            "type": "terminal_event",
            "event": "orphaned",
            "terminal_id": terminal_id,
            "daemon_epoch": "epoch-1",
            "seq": 3,
        }))
        .await;
        // The loop applies daemon events in arrival order, so the attach that a
        // later terminal's pane sends proves the orphaned event above applied.
        mock.send_event_and_wait(json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": BARRIER,
            "backend": "native",
            "daemon_epoch": "epoch-1",
            "seq": 4,
        }))
        .await;
        wait_until("the orphaned event applied", || {
            websocket_requests(&mock, "terminal_attach")
                .iter()
                .any(|request| request.get("terminal_id") == Some(&json!(BARRIER)))
        })
        .await;
        host.release_accepts(&gate);
        settle().await;
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
    result.expect("cancelled host recovery loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        None,
        "the closed pane is gone"
    );
    assert_eq!(
        host.attaches(),
        1,
        "closing the pane during recovery asked the host for nothing"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

/// 2.3: a host attach keeps the same terminal and epoch. A host that answers
/// on the same epoch but names a different terminal has not restored this
/// pane, so the pane must fall back to the daemon rather than adopt it.
#[tokio::test]
async fn attached_reply_for_a_different_host_terminal_falls_back_to_daemon() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.refuse_next_proxy_attach("host_not_ready", "host not ready");
    let terminal_id = "terminal-wrong-id";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace
        .pane_for_terminal(terminal_id)
        .expect("direct pane");
    let attachment_before = workspace.pane(pane_id).attachment_id().to_string();

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_focus(&mock, terminal_id).await;
        // Same epoch, but the reconnect's attach is answered as a different
        // terminal. Only the reply tells the pane it was not restored.
        host.answer_next_attach_with_terminal_id("terminal-somewhere-else");
        host.disconnect();
        // The mismatch falls straight through to the daemon path: detach, then
        // an attach whose first reply defers on `host_not_ready`.
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        tokio::time::pause();
        for _ in 0..64 {
            if websocket_requests(&mock, "terminal_attach").len() >= 3 {
                break;
            }
            tokio::time::advance(Duration::from_secs(8)).await;
            for _ in 0..256 {
                tokio::task::yield_now().await;
            }
        }
        tokio::time::resume();
        wait_for_websocket_requests(&mock, "terminal_attach", 3).await;
        settle().await;
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
    result.expect("different-terminal fallback loop");

    assert_eq!(
        workspace.pane_for_terminal(terminal_id),
        Some(pane_id),
        "the pane survives the mismatched attach"
    );
    assert!(
        !websocket_requests(&mock, "terminal_detach").is_empty(),
        "the mismatched host attach was released before a daemon attach"
    );
    assert!(
        websocket_requests(&mock, "terminal_attach").len() >= 3,
        "the daemon fallback ran its attach ladder after the mismatch"
    );
    assert_ne!(
        workspace.pane(pane_id).attachment_id(),
        attachment_before,
        "the mismatched host attach was not adopted as the restore"
    );
    host.shutdown().await;
    mock.shutdown().await;
}
