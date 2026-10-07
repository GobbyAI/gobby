//! #23714: a pane that fell back to the daemon's proxy goes back to direct
//! frame delivery once its host takes a direct attach again.
//!
//! The retry is make-before-break: the proxy stays live until the direct
//! attach is installed, and only then is the proxy attachment released. These
//! tests drive the real live loop against a real frame socket, so every attach
//! and detach is visible on the mock daemon and every key on the host.

mod mock_daemon;

use std::io;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::run_live_loop;
use gobby_client::daemon::LiveDaemon;
use gobby_client::frame_source::Transport;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::{FrameDelivery, Workspace};
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::ClientMessage;
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::{live_workspace_on_direct_host, DirectHost, MockDaemon};
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::time::timeout;
use tracing::subscriber::DefaultGuard;

const WAIT: Duration = Duration::from_secs(10);

struct LogWriter(Arc<Mutex<Vec<u8>>>);

impl io::Write for LogWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0
            .lock()
            .expect("captured logs")
            .extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Records this thread's log lines until the guard drops. The tests run on
/// tokio's current-thread runtime, so the live loop's lines land here too.
fn capture_logs() -> (DefaultGuard, Arc<Mutex<Vec<u8>>>) {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let writer = Arc::clone(&captured);
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .without_time()
        .with_target(false)
        .with_writer(move || LogWriter(Arc::clone(&writer)))
        .finish();
    (tracing::subscriber::set_default(subscriber), captured)
}

fn log_lines(captured: &Arc<Mutex<Vec<u8>>>, stage: &str) -> Vec<String> {
    let bytes = captured.lock().expect("captured logs").clone();
    String::from_utf8(bytes)
        .expect("UTF-8 logs")
        .lines()
        .filter(|line| line.contains(&format!("lifecycle_stage=\"{stage}\"")))
        .map(str::to_string)
        .collect()
}

fn websocket_requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!(kind)))
        .collect()
}

fn request_field(mock: &MockDaemon, kind: &str, field: &str) -> Vec<Option<String>> {
    websocket_requests(mock, kind)
        .iter()
        .map(|request| {
            request
                .get(field)
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect()
}

async fn wait_for_websocket_requests(mock: &MockDaemon, kind: &str, expected: usize) {
    timeout(WAIT, async {
        while websocket_requests(mock, kind).len() < expected {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("mock daemon never saw {expected} {kind} requests"));
}

/// Moves paused time on two seconds at a time, letting the loop run between
/// steps, until `reached` holds or 128 seconds have passed. Returns whether
/// it held.
async fn advance_until(mut reached: impl FnMut() -> bool) -> bool {
    tokio::time::pause();
    for _ in 0..64 {
        if reached() {
            break;
        }
        tokio::time::advance(Duration::from_secs(2)).await;
        for _ in 0..256 {
            tokio::task::yield_now().await;
        }
    }
    tokio::time::resume();
    reached()
}

fn show_roster(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    for terminal_id in workspace.roster_terminal_ids() {
        if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
            chrome.open_pane(pane, workspace.pane(pane).display_name());
        }
    }
}

async fn send_key(input: &mpsc::Sender<RawInputEvent>, code: KeyCode) {
    input
        .send(RawInputEvent::Key(TerminalKey::new(
            code,
            KeyModifiers::NONE,
        )))
        .await
        .expect("live loop input");
}

fn is_input(message: &ClientMessage, data: &[u8]) -> bool {
    matches!(message, ClientMessage::Input { data: typed } if typed == data)
}

async fn typed_on_host(host: &DirectHost, data: &[u8]) -> Vec<ClientMessage> {
    host.wait_for("the typed key on the host stream", |seen| {
        seen.iter().any(|message| is_input(message, data))
    })
    .await
}

/// The attachment the host bound last before `data` was typed.
fn bound_before_input(seen: &[ClientMessage], data: &[u8]) -> Option<String> {
    let typed = seen.iter().position(|message| is_input(message, data))?;
    seen[..typed]
        .iter()
        .rev()
        .find_map(|message| match message {
            ClientMessage::BindAttachment { attachment_id } => Some(attachment_id.clone()),
            _ => None,
        })
}

/// A live workspace on one terminal under `delivery`, whose roster row offers
/// the host's direct locator when `with_locator`. Unlike
/// `live_workspace_on_direct_host`, the first attach may land on either
/// transport.
async fn live_workspace(
    mock: &MockDaemon,
    host: &DirectHost,
    terminal_id: &str,
    with_locator: bool,
    delivery: FrameDelivery,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    let home = tempfile::tempdir().expect("gobby home");
    std::fs::write(
        home.path()
            .join(gobby_core::local_token::LOCAL_CLI_TOKEN_FILENAME),
        "local-token\n",
    )
    .expect("write local cli token");
    mock.serve_direct_attach(host.attach_locator(terminal_id));
    let mut row = json!({
        "terminal_id": terminal_id,
        "backend": "native",
        "state": "live",
    });
    if with_locator {
        row["attach"] = host.roster_attach(terminal_id);
    }
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            json!({
                "items": [row.clone()],
                "next_cursor": null,
                "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
            }),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    workspace.set_frame_delivery(delivery);
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("attach the roster's pane");
    (workspace, home)
}

#[tokio::test]
async fn a_failed_initial_direct_attach_is_promoted_after_a_bounded_proxy_interval() {
    let (log_guard, logs) = capture_logs();
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-initial-fallback";
    let host = DirectHost::start("epoch-1").await;
    // The host refuses the first direct attach only; the daemon still offers
    // the locator, so the pane is worth trying again.
    host.refuse_next_attach();
    let (mut workspace, _home) =
        live_workspace(&mock, &host, terminal_id, true, FrameDelivery::Auto).await;
    let pane_id = workspace.pane_for_terminal(terminal_id).expect("pane");
    assert_eq!(
        workspace.pane(pane_id).transport(),
        Some(Transport::Proxy),
        "the refused host attach fell back to the proxy"
    );
    assert_eq!(workspace.pane(pane_id).attachment_id(), "attachment-2");

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let promoted =
            advance_until(|| websocket_requests(&mock, "terminal_attach").len() >= 3).await;
        assert!(promoted, "no direct retry within 128s of proxy delivery");
        wait_for_websocket_requests(&mock, "terminal_detach", 2).await;
        wait_for_websocket_requests(&mock, "terminal_take_control", 2).await;
        send_key(&input_tx, KeyCode::Char('x')).await;
        let seen = typed_on_host(&host, b"x").await;
        drop(input_tx);
        seen
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, seen) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("promotion loop");
    drop(log_guard);

    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    assert_eq!(workspace.pane(pane_id).attachment_id(), "attachment-3");
    assert_eq!(
        request_field(&mock, "terminal_attach", "frame_delivery"),
        [
            Some("direct".into()),
            Some("proxy".into()),
            Some("direct".into())
        ],
        "one refused direct attach, the proxy, then one direct retry"
    );
    assert_eq!(
        request_field(&mock, "terminal_detach", "attachment_id"),
        [
            Some("attachment-1".into()),
            Some("attachment-2".into()),
            Some("attachment-3".into())
        ],
        "the refused direct attachment, the replaced proxy attachment, then the promoted one at exit"
    );
    let takes = websocket_requests(&mock, "terminal_take_control");
    assert_eq!(takes[1]["attachment_id"], json!("attachment-3"));
    assert_eq!(
        takes[1]["takeover"],
        json!(true),
        "the retake displaces this client's own proxy holder"
    );
    assert_eq!(
        bound_before_input(&seen, b"x").as_deref(),
        Some("attachment-3"),
        "the key goes out under the promoted attachment: {seen:?}"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "typing after the promotion stays on the host stream"
    );
    assert_eq!(host.attaches(), 1, "only the promotion reached the host");
    let fallbacks = log_lines(&logs, "direct-fallback");
    assert_eq!(fallbacks.len(), 1, "one fallback record: {fallbacks:?}");
    assert!(
        fallbacks[0].contains(terminal_id),
        "the fallback names its terminal: {fallbacks:?}"
    );
    assert!(
        fallbacks[0].contains("reason="),
        "the fallback says why: {fallbacks:?}"
    );
    let promotions = log_lines(&logs, "direct-promotion");
    assert_eq!(promotions.len(), 1, "one promotion record: {promotions:?}");
    assert!(
        promotions[0].contains(terminal_id),
        "the promotion names its terminal: {promotions:?}"
    );
    assert!(
        promotions[0].contains("attachment=attachment-3"),
        "the promotion names the new attachment: {promotions:?}"
    );
    host.shutdown().await;
    mock.shutdown().await;
}

#[tokio::test]
async fn a_recovered_proxy_pane_is_promoted_and_types_direct() {
    let (log_guard, logs) = capture_logs();
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-recovered-fallback";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, terminal_id).await;
    let pane_id = workspace.pane_for_terminal(terminal_id).expect("pane");

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        // The host-local reconnect is refused, so recovery lands on the proxy.
        host.refuse_next_attach();
        host.disconnect();
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        wait_for_websocket_requests(&mock, "terminal_attach", 2).await;
        let promoted =
            advance_until(|| websocket_requests(&mock, "terminal_attach").len() >= 3).await;
        assert!(promoted, "no direct retry within 128s of proxy delivery");
        wait_for_websocket_requests(&mock, "terminal_detach", 2).await;
        // The daemon re-attach left the pane observing, so the key asks.
        send_key(&input_tx, KeyCode::Char('x')).await;
        let seen = typed_on_host(&host, b"x").await;
        drop(input_tx);
        seen
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, seen) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("recovered promotion loop");
    let takes = websocket_requests(&mock, "terminal_take_control");
    assert_eq!(
        takes.len(),
        2,
        "the start-up take, then the key's: {takes:?}"
    );
    assert_eq!(takes[1]["attachment_id"], json!("attachment-3"));
    drop(log_guard);

    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Direct));
    assert_eq!(workspace.pane(pane_id).attachment_id(), "attachment-3");
    assert_eq!(
        request_field(&mock, "terminal_attach", "frame_delivery"),
        [
            Some("direct".into()),
            Some("proxy".into()),
            Some("direct".into())
        ]
    );
    assert_eq!(
        bound_before_input(&seen, b"x").as_deref(),
        Some("attachment-3"),
        "the key goes out under the promoted attachment: {seen:?}"
    );
    assert!(
        websocket_requests(&mock, "terminal_input").is_empty(),
        "typing after the promotion stays on the host stream"
    );
    let fallbacks = log_lines(&logs, "direct-fallback");
    assert_eq!(fallbacks.len(), 1, "one fallback record: {fallbacks:?}");
    assert!(
        fallbacks[0].contains(terminal_id),
        "the fallback names its terminal: {fallbacks:?}"
    );
    assert!(
        fallbacks[0].contains("reason=host-local reconnect failed"),
        "the fallback says why: {fallbacks:?}"
    );
    let promotions = log_lines(&logs, "direct-promotion");
    assert_eq!(promotions.len(), 1, "one promotion record: {promotions:?}");
    host.shutdown().await;
    mock.shutdown().await;
}

/// Runs a proxy pane for 128s of paused time. Returns whether it attached a
/// second time, the transports asked of the daemon, and the pane's transport.
async fn idle_proxy_pane(
    with_locator: bool,
    delivery: FrameDelivery,
) -> (bool, Vec<Option<String>>, Option<Transport>) {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let terminal_id = "terminal-proxy-only";
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) =
        live_workspace(&mock, &host, terminal_id, with_locator, delivery).await;
    let pane_id = workspace.pane_for_terminal(terminal_id).expect("pane");

    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    show_roster(&workspace, &mut chrome);
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_take_control", 1).await;
        let reattached =
            advance_until(|| websocket_requests(&mock, "terminal_attach").len() >= 2).await;
        drop(input_tx);
        reattached
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, reattached) = tokio::join!(
        run_live_loop(
            &mut workspace,
            &mut terminal,
            &mut chrome,
            input_rx,
            &mut switch
        ),
        driver
    );
    result.expect("proxy-only loop");
    let transport = workspace.pane(pane_id).transport();
    let kinds = request_field(&mock, "terminal_attach", "frame_delivery");
    host.shutdown().await;
    mock.shutdown().await;
    (reattached, kinds, transport)
}

#[tokio::test]
async fn a_pane_without_a_direct_locator_stays_on_the_proxy() {
    let (reattached, kinds, transport) = idle_proxy_pane(false, FrameDelivery::Auto).await;
    assert!(!reattached, "a pane with no locator never retries");
    assert_eq!(kinds, [Some("proxy".to_string())]);
    assert_eq!(transport, Some(Transport::Proxy));
}

#[tokio::test]
async fn proxy_delivery_never_tries_a_direct_attach() {
    let (reattached, kinds, transport) = idle_proxy_pane(true, FrameDelivery::Proxy).await;
    assert!(!reattached, "--frame-delivery proxy never retries");
    assert_eq!(kinds, [Some("proxy".to_string())]);
    assert_eq!(transport, Some(Transport::Proxy));
}
