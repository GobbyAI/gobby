//! Lease control on the live loop (#22530): a take-back displaces the
//! holder, a refusal is a status line rather than a frame error, and the
//! destroy-orphans dialog's buttons answer a click, as do the project
//! dialogs' (#22543).

mod mock_daemon;

use std::time::Duration;

use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{
    open_new_project_dialog, route_mouse, run_live_loop, Backend, ModalOutcome, MouseOutcome,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::dialogs::{Dialog, OrphanRow, WorktreeChoice};
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
/// Clicking or focusing a pane is the whole gesture (#22573): the take it
/// sends already displaces whoever holds the lease, so a person never presses
/// take-back to type in a pane they just chose. The key typed straight after
/// focus proves the grant landed, which is also the signal this assertion
/// waits on.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn focus_displaces_the_holder_in_one_gesture() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::defaults(HERDR_PREFIX);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
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
    assert_eq!(
        takes.len(),
        1,
        "focus asks once and needs no second gesture"
    );
    assert_eq!(
        takes[0].get("takeover"),
        Some(&json!(true)),
        "focus displaces the holder"
    );
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    assert!(workspace.pane(pane).is_held(), "the granted take holds it");
    assert!(
        !workspace.pane(pane).has_take_back(),
        "no take-back on the ordinary path"
    );
}

/// A daemon that refuses the take outright is the exceptional state that keeps
/// take-back: the pane says who holds it instead of typing into nothing.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_refused_take_keeps_take_back_and_names_the_holder() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    mock.enqueue_take_control_reply(false, 1, Some("held"));
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::defaults(HERDR_PREFIX);
    let (input_tx, input_rx) = mpsc::channel(256);
    let observed_daemon = workspace.daemon().clone();

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        // The refused take is applied off the loop. Wait for its daemon future
        // to finish before queuing take-back; the biased loop will then apply
        // that outcome before this input and make the test independent of
        // scheduler timing.
        for _ in 0..1_024 {
            if observed_daemon.pending_counts().2 == 0 {
                break;
            }
            tokio::task::yield_now().await;
        }
        assert_eq!(observed_daemon.pending_counts().2, 0);
        send_key(&input_tx, KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        send_key(&input_tx, KeyCode::Char('A'), KeyModifiers::SHIFT).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests(&mock, "terminal_input", 1).await;
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
    assert_eq!(takes.len(), 2, "the refusal, then the take-back");
    assert_eq!(takes[1].get("takeover"), Some(&json!(true)));
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    assert!(
        workspace.pane(pane).is_held(),
        "the granted take-back holds the pane"
    );
    let alerts: Vec<&str> = chrome
        .alert_log
        .iter()
        .map(|toast| toast.title.as_str())
        .collect();
    assert!(
        alerts.iter().any(|alert| alert.contains("take back")),
        "the refusal names take back: {alerts:?}"
    );
    assert!(
        !alerts
            .iter()
            .any(|alert| alert.contains("frame protocol failed")),
        "a refusal is not a protocol failure: {alerts:?}"
    );
}

fn orphan(terminal_id: &str) -> OrphanRow {
    OrphanRow {
        terminal_id: terminal_id.to_string(),
        backend: Backend::Native,
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

/// Draw the open dialog once and return its button hit areas in order.
fn drawn_dialog_buttons(ws: &Workspace, chrome: &mut Chrome) -> Vec<Rect> {
    let area = Rect::new(0, 0, 120, 40);
    chrome.compute_view(ws, area);
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).expect("test terminal");
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, ws, chrome)))
        .expect("draw the dialog");
    chrome.view.apply_hits(hits.expect("dialog drawn"));
    chrome.view.dialog_button_hit_areas.clone()
}

fn open_project_dialog(chrome: &mut Chrome, dialog: Dialog) {
    chrome.dialog = Some(dialog);
    chrome.mode = Mode::ProjectDialog;
}

fn new_worktree_dialog() -> Dialog {
    Dialog::NewWorktree {
        project_id: "proj-1".to_string(),
        branch: "feature".to_string(),
        base: String::new(),
        cursor: 7,
        base_focused: false,
        error: None,
    }
}

fn open_worktree_dialog() -> Dialog {
    Dialog::OpenWorktree {
        project_id: "proj-1".to_string(),
        choices: vec![WorktreeChoice {
            worktree_id: "wt-1".to_string(),
            branch: "feature".to_string(),
            path: "/tmp/wt-1".to_string(),
        }],
        selected: 0,
    }
}

fn remove_worktree_dialog() -> Dialog {
    Dialog::RemoveWorktree {
        worktree_id: "wt-1".to_string(),
        branch: "feature".to_string(),
        path: "/tmp/wt-1".to_string(),
        tabs: 1,
        panes: 2,
        error: None,
    }
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

/// Every project dialog's drawn buttons answer a click with the outcome of
/// the key they name (#22543).
#[test]
fn project_dialog_buttons_answer_a_click() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();

    open_new_project_dialog(&mut chrome);
    let buttons = drawn_dialog_buttons(&ws, &mut chrome);
    assert_eq!(
        buttons.len(),
        3,
        "open, complete and cancel drawn: {buttons:?}"
    );
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[0])),
        MouseOutcome::Modal(ModalOutcome::InitProject("~/".to_string())),
        "open submits the path like Enter"
    );
    open_new_project_dialog(&mut chrome);
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[2])),
        MouseOutcome::Modal(ModalOutcome::Close),
        "cancel closes like Esc"
    );
    assert!(chrome.dialog.is_none(), "the dialog closes on cancel");
    open_new_project_dialog(&mut chrome);
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[1])),
        MouseOutcome::Modal(ModalOutcome::Consumed),
        "complete edits the path in place like Tab"
    );
    assert!(chrome.dialog.is_some(), "completion keeps the dialog open");

    open_project_dialog(&mut chrome, new_worktree_dialog());
    let buttons = drawn_dialog_buttons(&ws, &mut chrome);
    assert_eq!(buttons.len(), 2, "create and cancel drawn: {buttons:?}");
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[0])),
        MouseOutcome::Modal(ModalOutcome::CreateWorktree {
            project_id: "proj-1".to_string(),
            branch: "feature".to_string(),
            base: None,
        }),
        "create submits the branch like Enter"
    );
    open_project_dialog(&mut chrome, new_worktree_dialog());
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[1])),
        MouseOutcome::Modal(ModalOutcome::Close),
        "cancel closes like Esc"
    );
    assert!(chrome.dialog.is_none(), "the dialog closes on cancel");

    open_project_dialog(&mut chrome, open_worktree_dialog());
    let buttons = drawn_dialog_buttons(&ws, &mut chrome);
    assert_eq!(buttons.len(), 2, "open and cancel drawn: {buttons:?}");
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[0])),
        MouseOutcome::Modal(ModalOutcome::OpenWorktree("wt-1".to_string())),
        "open picks the selected worktree like Enter"
    );
    open_project_dialog(&mut chrome, open_worktree_dialog());
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[1])),
        MouseOutcome::Modal(ModalOutcome::Close),
        "cancel closes like Esc"
    );
    assert!(chrome.dialog.is_none(), "the dialog closes on cancel");

    open_project_dialog(&mut chrome, remove_worktree_dialog());
    let buttons = drawn_dialog_buttons(&ws, &mut chrome);
    assert_eq!(buttons.len(), 2, "delete and cancel drawn: {buttons:?}");
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[0])),
        MouseOutcome::Modal(ModalOutcome::RemoveWorktree("wt-1".to_string())),
        "delete confirms like Enter"
    );
    open_project_dialog(&mut chrome, remove_worktree_dialog());
    assert_eq!(
        route_mouse(&ws, &mut chrome, &click(buttons[1])),
        MouseOutcome::Modal(ModalOutcome::Close),
        "cancel closes like Esc"
    );
    assert!(chrome.dialog.is_none(), "the dialog closes on cancel");
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
    let banner = chrome.last_alert().unwrap_or_default().to_string();
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

#[tokio::test]
async fn take_control_timeout_then_finalized_reattaches_on_the_same_generation() {
    let mock = MockDaemon::start("local-token").await;
    mock.suppress_ws("terminal_take_control");
    let (mut workspace, _home) = single_terminal_loop(&mock).await;
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.keymap = Keymap::defaults(HERDR_PREFIX);
    let (input_tx, input_rx) = mpsc::channel(256);

    let driver = async {
        // Attach completes and focus asks for control. The grant is withheld,
        // so the control deadline retires that attachment.
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        wait_for_websocket_requests_within(&mock, "terminal_detach", 1, Duration::from_secs(5))
            .await;
        let detach = websocket_requests(&mock, "terminal_detach");
        mock.send_event(json!({
            "type": "terminal_attachment_finalized",
            "daemon_epoch": "epoch-1",
            "seq": 2,
            "terminal_id": detach[0].get("terminal_id").cloned(),
            "attachment_id": detach[0].get("attachment_id").cloned(),
            "code": "control_timeout",
            "reason": "take_control deadline",
        }));
        mock.allow_ws("terminal_take_control");
        // The socket generation did not change. attach_ready_panes must issue
        // another attach; a generation entry left by the retire path skips it.
        // The resize is sent only after that attach is installed.
        wait_for_websocket_requests_within(&mock, "terminal_attach", 2, Duration::from_secs(3))
            .await;
        wait_for_websocket_requests_within(&mock, "terminal_resize", 2, Duration::from_secs(2))
            .await;
        send_key(&input_tx, KeyCode::Char('x'), KeyModifiers::NONE).await;
        wait_for_websocket_requests_within(
            &mock,
            "terminal_take_control",
            2,
            Duration::from_secs(3),
        )
        .await;
        wait_for_websocket_requests_within(&mock, "terminal_input", 1, Duration::from_secs(3))
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

    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal pane");
    assert!(
        workspace.pane(pane).is_live(),
        "the pane reattached on the same generation: {:?}",
        workspace.pane(pane).status_message()
    );
    assert!(
        workspace.pane(pane).is_held(),
        "input control was restored: {:?}",
        workspace.pane(pane).status_message()
    );
    assert_eq!(websocket_requests(&mock, "terminal_attach").len(), 2);

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
        !text.contains("Control result indeterminate"),
        "rendering resumed instead of keeping the retire note:\n{text}"
    );
    mock.shutdown().await;
}
