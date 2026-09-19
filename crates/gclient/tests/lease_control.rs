//! Lease control on the live loop (#22530): a take-back displaces the
//! holder, a refusal is a status line rather than a frame error, and the
//! destroy-orphans dialog's buttons answer a click.

mod mock_daemon;

use std::time::Duration;

use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{route_mouse, run_live_loop, ModalOutcome, MouseOutcome};
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::dialogs::{Dialog, OrphanRow};
use gobby_client::ui::keymap::{Keymap, HERDR_PREFIX};
use gobby_client::ui::{render_workspace, Chrome};
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::timeout;

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

/// One live terminal pinned into a project, as `client_loop.rs` sets it up.
async fn single_terminal_loop(mock: &MockDaemon) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-a", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    (workspace, home)
}

/// Focus asks politely and is refused because another viewer holds the
/// lease; the pane offers take-back and the status line says so without a
/// failure prefix. `prefix+shift+a` then sends a takeover, which the daemon
/// grants, and the pane is held.
#[tokio::test]
async fn take_back_displaces_the_holder_with_takeover() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    mock.enqueue_take_control_reply(false, 1, Some("held"));
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::defaults(HERDR_PREFIX);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        settle_live_event().await;
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        settle_live_event().await;
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

    let takes = websocket_requests(&mock, "terminal_take_control");
    assert_eq!(takes.len(), 2, "one polite focus take and one take-back");
    assert_eq!(
        takes[0].get("takeover"),
        Some(&json!(false)),
        "focus never displaces the holder"
    );
    assert_eq!(
        takes[1].get("takeover"),
        Some(&json!(true)),
        "take-back displaces the holder"
    );
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    assert!(
        workspace.pane(pane).is_held(),
        "the granted takeover holds the pane"
    );
    assert!(!workspace.pane(pane).has_take_back());
    let status = chrome.status_message.clone().unwrap_or_default();
    assert!(
        status.contains("take back"),
        "the refusal names take back: {status:?}"
    );
    assert!(
        !status.contains("frame protocol failed"),
        "a refusal is not a protocol failure: {status:?}"
    );
    mock.shutdown().await;
}

fn orphan(terminal_id: &str) -> OrphanRow {
    OrphanRow {
        terminal_id: terminal_id.to_string(),
        backend: "native".to_string(),
        name: terminal_id.to_string(),
        owner: None,
        last_seen: None,
    }
}

fn open_orphans_dialog(chrome: &mut Chrome, rows: Vec<OrphanRow>) {
    chrome.dialog = Some(Dialog::DestroyOrphans {
        checked: vec![true; rows.len()],
        rows,
        selected: 0,
    });
    chrome.mode = Mode::ProjectDialog;
}

fn click(rect: Rect) -> MouseEvent {
    MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: rect.x,
        row: rect.y,
        modifiers: KeyModifiers::NONE,
    }
}

/// The dialog's `destroy` button is Enter and `cancel` is Esc: a click on
/// either produces the outcome its key does and closes the dialog.
#[tokio::test]
async fn destroy_orphans_dialog_buttons_answer_a_click() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    open_orphans_dialog(&mut chrome, vec![orphan("term-orphan")]);
    let area = Rect::new(0, 0, 120, 40);
    chrome.compute_view(&ws, area);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, &ws, &chrome)))
        .expect("draw the dialog");
    chrome.view.apply_hits(hits.expect("dialog drawn"));
    let buttons = chrome.view.dialog_button_hit_areas.clone();
    assert_eq!(buttons.len(), 2, "destroy and cancel drawn: {buttons:?}");

    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[0])),
        MouseOutcome::Modal(ModalOutcome::DestroyOrphans(vec![orphan("term-orphan")])),
        "destroy confirms the checked rows"
    );
    assert!(chrome.dialog.is_none(), "the dialog closes on confirm");

    open_orphans_dialog(&mut chrome, vec![orphan("term-orphan")]);
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[1])),
        MouseOutcome::Modal(ModalOutcome::Close),
        "cancel closes without destroying"
    );
    assert!(chrome.dialog.is_none(), "the dialog closes on cancel");

    open_orphans_dialog(&mut chrome, vec![orphan("term-orphan")]);
    let beside = Rect::new(buttons[0].x, buttons[0].y.saturating_sub(2), 1, 1);
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(beside)),
        MouseOutcome::Handled,
        "a click elsewhere stays with the dialog"
    );
    assert!(chrome.dialog.is_some(), "the dialog stays open");
}

/// The attach deadline (5s) plus the base retry backoff (5s) plus slack.
const RETRY_WAIT: Duration = Duration::from_secs(15);

async fn wait_for_websocket_requests_within(
    mock: &MockDaemon,
    kind: &str,
    expected: usize,
    budget: Duration,
) {
    let mut poll = tokio::time::interval(Duration::from_millis(20));
    timeout(budget, async {
        loop {
            poll.tick().await;
            if websocket_requests(mock, kind).len() >= expected {
                break;
            }
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {expected} {kind} requests"));
}

fn screen_text(terminal: &Terminal<TestBackend>) -> String {
    let buffer = terminal.backend().buffer();
    let width = usize::from(buffer.area.width);
    buffer
        .content()
        .chunks(width)
        .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}

/// The daemon never answers the attach (#22544). After the request deadline
/// the pane is not blank: it names the unanswered request and the retry, the
/// loop keeps running instead of exiting, and nothing calls it a protocol
/// failure.
#[tokio::test]
async fn a_timed_out_request_names_itself_and_keeps_the_pane() {
    let mock = MockDaemon::start("local-token").await;
    mock.suppress_ws("terminal_attach");
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::defaults(HERDR_PREFIX);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        // The startup attach is withheld; its deadline passes before the
        // loop notices the closed input, so the exit is the input's.
        wait_for_websocket_requests(&mock, "terminal_attach", 1).await;
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
    result.expect("a timed-out attach does not end the loop");
    assert_eq!(workspace.exit_reason(), Some("terminal input closed"));

    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    let status = workspace
        .pane(pane)
        .status_message()
        .unwrap_or_default()
        .to_string();
    assert!(status.contains("attach_failed"), "{status:?}");
    assert!(
        status.contains("terminal_attach"),
        "the status names the unanswered request: {status:?}"
    );
    assert!(
        status.contains("retry in 5s"),
        "the status names the retry: {status:?}"
    );
    assert!(
        !status.contains("frame protocol failed"),
        "a timeout is not a protocol failure: {status:?}"
    );
    assert!(!workspace.pane(pane).is_live());
    let banner = chrome.status_message.clone().unwrap_or_default();
    assert!(
        !banner.contains("frame protocol failed"),
        "the banner never blames the frame protocol: {banner:?}"
    );

    // The pane body carries the note instead of staying blank.
    let area = Rect::new(0, 0, 140, 30);
    chrome.compute_view(&workspace, area);
    let mut screen = Terminal::new(TestBackend::new(140, 30)).expect("test terminal");
    screen
        .draw(|frame| {
            render_workspace(frame, &workspace, &chrome);
        })
        .expect("draw the workspace");
    let text = screen_text(&screen);
    assert!(
        text.contains("attach_failed") && text.contains("terminal_attach"),
        "the pane body says why it is empty:\n{text}"
    );
    mock.shutdown().await;
}

/// A deferred attach is tried again after the backoff and lands once the
/// daemon answers (#22544): one unanswered attach, one retry, a live pane.
#[tokio::test]
async fn a_deferred_attach_retries_after_the_backoff() {
    let mock = MockDaemon::start("local-token").await;
    mock.suppress_ws("terminal_attach");
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::defaults(HERDR_PREFIX);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_attach", 1).await;
        // The mock decided to withhold the first reply as it recorded the
        // request, with no await between; from here on attaches are
        // answered, so only the retry can land.
        mock.allow_ws("terminal_attach");
        wait_for_websocket_requests_within(&mock, "terminal_attach", 2, RETRY_WAIT).await;
        // The geometry pass runs after the retry installed the attachment,
        // so its resize is the first request the loop can only send then.
        wait_for_websocket_requests_within(&mock, "terminal_resize", 1, Duration::from_secs(2))
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
        websocket_requests(&mock, "terminal_attach").len(),
        2,
        "one unanswered attach and one retry"
    );
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    assert!(
        workspace.pane(pane).is_live(),
        "the retry attached the pane: {:?}",
        workspace.pane(pane).status_message()
    );
    assert_eq!(workspace.pane(pane).status_message(), None);
    mock.shutdown().await;
}
