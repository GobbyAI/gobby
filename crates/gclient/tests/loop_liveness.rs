//! Loop liveness while a daemon request is outstanding (plan A0 of
//! gclient-daemon-resilience): the shared probe later P1-P3 tests reuse to
//! prove `run_live_loop` keeps drawing frames, ticking and delivering keys
//! while the mock holds one websocket reply (`MockDaemon::hold_ws`).
//!
//! Ordering rule (memory 679bf344): `run_live_loop` selects with `biased;`
//! in the order exit signals, terminal input, daemon events, frames, so
//! dropping the driver's input sender exits the loop on its next iteration
//! even with daemon events still queued. Before dropping it, wait for a
//! mock-visible request the loop can only send after it applied what the
//! test asserts on (a `terminal_input` carrying a key queued behind a grant,
//! a `terminal_set_viewport` for a newly shown pane), never for a fixed
//! number of yields. A reply the mock wrote (`MockDaemon::replies`) is not
//! that proof: it says the reply left the mock, not that the loop applied it.

mod mock_daemon;

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::run_loop::RENDER_TICK;
use gobby_client::app::{run_live_loop, spawn_job, JobKey, JobLedger, JobResult, PaneId};
use gobby_client::daemon::{Generation, LiveDaemon};
use gobby_client::frame_source::Transport;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneModes, ServerMessage, MAX_FRAME_SIZE,
};
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::{Backend, ClearType, TestBackend, WindowSize};
use ratatui::buffer::Cell;
use ratatui::layout::{Position, Size};
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};
use tokio::time::timeout;

/// How long a live loop may take to show one probe's progress.
const LIVENESS_DEADLINE: Duration = Duration::from_secs(2);

/// Frames fed per probe, one per render tick.
const PROBE_FRAMES: usize = 3;

/// A test backend that counts draws and keeps the text of each one, so a
/// driver beside the loop can watch it render while the loop borrows the
/// workspace and chrome.
struct DrawRecorder {
    inner: TestBackend,
    draws: Arc<AtomicUsize>,
    frames: Arc<Mutex<Vec<String>>>,
}

impl DrawRecorder {
    fn new(width: u16, height: u16) -> (Self, Arc<AtomicUsize>, Arc<Mutex<Vec<String>>>) {
        let draws = Arc::new(AtomicUsize::new(0));
        let frames = Arc::new(Mutex::new(Vec::new()));
        (
            Self {
                inner: TestBackend::new(width, height),
                draws: Arc::clone(&draws),
                frames: Arc::clone(&frames),
            },
            draws,
            frames,
        )
    }
}

impl Backend for DrawRecorder {
    type Error = <TestBackend as Backend>::Error;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a Cell)>,
    {
        self.inner.draw(content)?;
        self.draws.fetch_add(1, Ordering::SeqCst);
        let buffer = self.inner.buffer();
        let width = usize::from(buffer.area.width);
        let text = buffer
            .content()
            .chunks(width)
            .map(|row| row.iter().map(Cell::symbol).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n");
        self.frames.lock().expect("recorded frames").push(text);
        Ok(())
    }

    fn hide_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.hide_cursor()
    }

    fn show_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.show_cursor()
    }

    fn get_cursor_position(&mut self) -> Result<Position, Self::Error> {
        self.inner.get_cursor_position()
    }

    fn set_cursor_position<P: Into<Position>>(&mut self, position: P) -> Result<(), Self::Error> {
        self.inner.set_cursor_position(position)
    }

    fn clear(&mut self) -> Result<(), Self::Error> {
        self.inner.clear()
    }

    fn clear_region(&mut self, clear_type: ClearType) -> Result<(), Self::Error> {
        self.inner.clear_region(clear_type)
    }

    fn size(&self) -> Result<Size, Self::Error> {
        self.inner.size()
    }

    fn window_size(&mut self) -> Result<WindowSize, Self::Error> {
        self.inner.window_size()
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        self.inner.flush()
    }
}

fn encoded_frame(text: &str) -> String {
    let message = ServerMessage::Frame(FrameData {
        cells: text
            .chars()
            .map(|symbol| CellData {
                symbol: symbol.to_string(),
                fg: 0x10,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            })
            .collect(),
        width: u16::try_from(text.chars().count()).expect("test frame width"),
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    });
    let mut framed = Vec::new();
    write_message(&mut framed, &message).expect("encode test frame");
    STANDARD.encode(&framed[4..])
}

fn websocket_requests(mock: &MockDaemon, kind: &str) -> Vec<Value> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!(kind)))
        .collect()
}

async fn wait_until(what: &str, mut done: impl FnMut() -> bool) {
    timeout(LIVENESS_DEADLINE, async {
        while !done() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {what}"));
}

async fn wait_for_websocket_requests(mock: &MockDaemon, kind: &str, expected: usize) {
    wait_until(&format!("{expected} {kind} requests"), || {
        websocket_requests(mock, kind).len() >= expected
    })
    .await;
}

async fn wait_for_replies(mock: &MockDaemon, kind: &str, expected: usize) {
    wait_until(&format!("{expected} {kind} replies"), || {
        mock.replies(kind) >= expected
    })
    .await;
}

/// The keys `terminal_input` carried to `terminal_id`, in order.
fn written(mock: &MockDaemon, terminal_id: &str) -> Vec<String> {
    websocket_requests(mock, "terminal_input")
        .iter()
        .filter(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
        .filter_map(|request| request.get("data").and_then(Value::as_str))
        .map(str::to_string)
        .collect()
}

/// The attachment the latest viewport claim for `terminal_id` names.
fn attachment_for(mock: &MockDaemon, terminal_id: &str) -> String {
    websocket_requests(mock, "terminal_set_viewport")
        .iter()
        .rev()
        .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
        .and_then(|request| request.get("attachment_id").and_then(Value::as_str))
        .unwrap_or_else(|| panic!("no viewport claim for {terminal_id}"))
        .to_string()
}

/// What a driver beside `run_live_loop` can see of it.
///
/// The loop borrows the workspace and chrome mutably, so the probe watches
/// them from outside: a fed frame's text reaching the backend stands in for
/// the pane's `frames_rendered`, and the draw count for `chrome.ticker`
/// (each render tick advances the ticker and draws once). After the loop
/// returns, a test checks the counters themselves against `batches`.
struct LivenessProbe<'a> {
    mock: &'a MockDaemon,
    input: &'a mpsc::Sender<RawInputEvent>,
    draws: Arc<AtomicUsize>,
    frames: Arc<Mutex<Vec<String>>>,
    fed: usize,
    /// Frame batches seen drawn; each one rendered at least one frame.
    batches: usize,
}

impl<'a> LivenessProbe<'a> {
    fn new(
        mock: &'a MockDaemon,
        input: &'a mpsc::Sender<RawInputEvent>,
        draws: Arc<AtomicUsize>,
        frames: Arc<Mutex<Vec<String>>>,
    ) -> Self {
        Self {
            mock,
            input,
            draws,
            frames,
            fed: 0,
            batches: 0,
        }
    }

    async fn key(&self, code: KeyCode, modifiers: KeyModifiers) {
        timeout(
            LIVENESS_DEADLINE,
            self.input
                .send(RawInputEvent::Key(TerminalKey::new(code, modifiers))),
        )
        .await
        .expect("the loop takes input")
        .expect("live loop input");
    }

    /// Move keyboard focus to the next pane (`prefix` then Tab).
    async fn next_pane(&self) {
        self.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        self.key(KeyCode::Tab, KeyModifiers::NONE).await;
    }

    /// Toggle zoom on the focused pane (`prefix` then `z`).
    async fn zoom(&self) {
        self.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        self.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
    }

    /// Paste `text` into the focused pane as one bracketed paste.
    async fn paste(&self, text: String) {
        timeout(
            LIVENESS_DEADLINE,
            self.input.send(RawInputEvent::Paste(text)),
        )
        .await
        .expect("the loop takes the paste")
        .expect("live loop input");
    }

    /// Feed `terminal_id` a frame per render tick and wait until the last
    /// one is drawn, asserting the loop drew while it ran.
    async fn frames_render(&mut self, terminal_id: &str) {
        let attachment_id = attachment_for(self.mock, terminal_id);
        let draws_before = self.draws.load(Ordering::SeqCst);
        for _ in 0..PROBE_FRAMES {
            self.fed += 1;
            self.mock
                .send_event_and_wait(json!({
                    "type": "terminal_frame",
                    "terminal_id": terminal_id,
                    "attachment_id": attachment_id,
                    "encoding": "bincode-b64",
                    "payload": encoded_frame(&format!("tick-{}", self.fed)),
                }))
                .await;
            tokio::time::sleep(RENDER_TICK).await;
        }
        let tag = format!("tick-{}", self.fed);
        let frames = Arc::clone(&self.frames);
        wait_until(&format!("{terminal_id} to draw {tag}"), || {
            frames
                .lock()
                .expect("recorded frames")
                .last()
                .is_some_and(|frame| frame.contains(&tag))
        })
        .await;
        self.batches += 1;
        assert!(
            self.draws.load(Ordering::SeqCst) > draws_before,
            "the render tick keeps drawing"
        );
    }

    /// Type `key` into the focused pane and wait for it to reach
    /// `terminal_id` as a `terminal_input` write.
    async fn key_reaches(&self, terminal_id: &str, key: char) {
        let before = written(self.mock, terminal_id).len();
        self.key(KeyCode::Char(key), KeyModifiers::NONE).await;
        wait_until(&format!("{key:?} to reach {terminal_id}"), || {
            written(self.mock, terminal_id).len() > before
        })
        .await;
        assert_eq!(
            written(self.mock, terminal_id).last(),
            Some(&key.to_string())
        );
    }

    /// The loop is live: frames fed to `frames_to` are drawn, it keeps
    /// ticking, and a key typed into the focused, granted `keys_to` reaches
    /// its source.
    async fn assert_live(&mut self, frames_to: &str, keys_to: &str, key: char) {
        self.frames_render(frames_to).await;
        self.key_reaches(keys_to, key).await;
    }
}

fn for_terminal(terminal_id: &'static str) -> impl Fn(&Value) -> bool + Send + 'static {
    move |request| request.get("terminal_id") == Some(&json!(terminal_id))
}

/// The host epoch a [`StallingHost`] answers with.
const HOST_EPOCH: &str = "stalling-host-epoch";

/// A terminal host on a real frame socket, the way gterm serves one: it
/// answers one direct attach, hands on what the client sends, and stops
/// reading once `stall` is notified, so the client's bounded writer fills
/// behind it while the socket stays open.
struct StallingHost {
    _socket_dir: tempfile::TempDir,
    socket_path: PathBuf,
    received: mpsc::UnboundedReceiver<ClientMessage>,
    stall: Arc<Notify>,
    task: tokio::task::JoinHandle<()>,
}

impl StallingHost {
    async fn start() -> Self {
        let socket_dir = tempfile::tempdir().expect("direct socket dir");
        let socket_path = socket_dir.path().join("frames.sock");
        let listener =
            tokio::net::UnixListener::bind(&socket_path).expect("bind direct frame socket");
        let (received_tx, received) = mpsc::unbounded_channel();
        let stall = Arc::new(Notify::new());
        let stalled = Arc::clone(&stall);
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("direct client");
            let _: ClientMessage = read_message_async(&mut stream, MAX_FRAME_SIZE)
                .await
                .expect("direct hello");
            write_message_async(
                &mut stream,
                &ServerMessage::Welcome {
                    host_epoch: HOST_EPOCH.into(),
                },
            )
            .await
            .expect("direct welcome");
            let _: ClientMessage = read_message_async(&mut stream, MAX_FRAME_SIZE)
                .await
                .expect("direct attach");
            loop {
                tokio::select! {
                    biased;
                    () = stalled.notified() => break,
                    message = read_message_async(&mut stream, MAX_FRAME_SIZE) => match message {
                        Ok(message) => {
                            let _ = received_tx.send(message);
                        }
                        Err(_) => return,
                    },
                }
            }
            // Keep the socket open and unread until the test ends.
            std::future::pending::<()>().await;
            drop(stream);
        });
        Self {
            _socket_dir: socket_dir,
            socket_path,
            received,
            stall,
            task,
        }
    }

    /// The roster `attach` block that sends `terminal_id`'s attach direct.
    fn roster_attach(&self, terminal_id: &str) -> Value {
        json!({
            "backend": "native",
            "frame_host_epoch": HOST_EPOCH,
            "host_socket": self.socket_path.to_string_lossy(),
            "host_terminal_id": terminal_id,
        })
    }

    /// The `direct` locator the daemon returns with a direct attach result.
    fn attach_locator(&self, terminal_id: &str) -> Value {
        json!({
            "host_epoch": HOST_EPOCH,
            "host_terminal_id": terminal_id,
            "frame_socket_path": self.socket_path.to_string_lossy(),
            "pane": null,
        })
    }

    /// Wait until the host reads `data` as typed input.
    async fn wait_for_input(&mut self, data: &[u8]) {
        timeout(LIVENESS_DEADLINE, async {
            loop {
                match self.received.recv().await {
                    Some(ClientMessage::Input { data: typed }) if typed == data => break,
                    Some(_) => {}
                    None => panic!("the direct host closed before {data:?}"),
                }
            }
        })
        .await
        .unwrap_or_else(|_| panic!("timed out waiting for the direct host to read {data:?}"));
    }
}

impl Drop for StallingHost {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Two live panes in one tab, `terminal-b` focused, so the loop starts by
/// taking control of `terminal-b`. With `direct_b`, terminal-b attaches over
/// that host's frame socket and terminal-a stays proxied. Keep the returned
/// home alive for the loop's lifetime.
async fn two_pane_workspace(
    mock: &MockDaemon,
    direct_b: Option<&StallingHost>,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    let mut terminal_b = json!({"terminal_id": "terminal-b", "backend": "native", "state": "live"});
    if let Some(host) = direct_b {
        terminal_b["attach"] = host.roster_attach("terminal-b");
        mock.serve_direct_attach(host.attach_locator("terminal-b"));
    }
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                terminal_b
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    std::fs::write(
        home.path()
            .join(gobby_core::local_token::LOCAL_CLI_TOKEN_FILENAME),
        "local-token\n",
    )
    .expect("write local cli token");
    mock.seed_workspace(
        "project-1",
        &[(&["terminal-a", "terminal-b"][..], "terminal-b")],
    );
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    (workspace, home)
}

#[tokio::test]
async fn held_websocket_request_stays_pending_until_released() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);
    let control = "terminal_take_control";

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, control, 1).await;
        probe.key_reaches("terminal-b", 'x').await;

        // Visiting terminal-a asks for its control, which the mock holds;
        // back on terminal-b, the loop takes control there again and keeps
        // drawing terminal-a's frames and writing terminal-b's keys.
        let release = mock.hold_ws(control, for_terminal("terminal-a"));
        probe.next_pane().await;
        wait_for_websocket_requests(&mock, control, 2).await;
        probe.next_pane().await;
        wait_for_websocket_requests(&mock, control, 3).await;
        probe.assert_live("terminal-a", "terminal-b", 'y').await;
        assert_eq!(
            mock.replies(control),
            2,
            "terminal-a's control stays pending while it is held"
        );
        release.notify_one();
        wait_for_replies(&mock, control, 3).await;
        probe.assert_live("terminal-a", "terminal-b", 'z').await;

        // A held grant applies once released: the key typed into terminal-a
        // while its control was pending is written after the grant lands.
        let release = mock.hold_ws(control, for_terminal("terminal-a"));
        probe.next_pane().await;
        wait_for_websocket_requests(&mock, control, 4).await;
        probe.key(KeyCode::Char('q'), KeyModifiers::NONE).await;
        probe.frames_render("terminal-a").await;
        assert!(
            written(&mock, "terminal-a").is_empty(),
            "a key typed under a pending grant waits for it"
        );
        assert_eq!(mock.replies(control), 3);
        release.notify_one();
        wait_until("the released grant to write terminal-a's key", || {
            written(&mock, "terminal-a") == ["q"]
        })
        .await;
        assert_eq!(mock.replies(control), 4);
        let batches = probe.batches;
        drop(input_tx);
        batches
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, batches) = tokio::join!(
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
    let pane_a = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal-a pane");
    assert!(
        usize::try_from(workspace.pane(pane_a).frames_rendered()).expect("frame count") >= batches,
        "each frame batch fed to terminal-a rendered"
    );
    assert!(
        workspace.pane(pane_a).writable(),
        "the released grant applied"
    );
    assert!(chrome.ticker > ticker_before, "the render tick advanced");
    assert_eq!(written(&mock, "terminal-b"), ["x", "y", "z"]);
    mock.shutdown().await;
}

fn is_focus_hint(request: &Value) -> bool {
    request.get("op") == Some(&json!("workspace.set_focus_hints"))
}

/// The focus hints the loop reported, in order.
fn focus_hints(mock: &MockDaemon) -> Vec<Value> {
    websocket_requests(mock, "workspace_op")
        .into_iter()
        .filter(is_focus_hint)
        .collect()
}

#[tokio::test]
async fn a_held_focus_hint_op_never_stalls_frames_or_ticks() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);
    let op = "workspace_op";

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        assert!(
            focus_hints(&mock).is_empty(),
            "the window opened on the stored focus"
        );

        // Visiting terminal-a reports its focus, which the mock holds; the
        // loop keeps drawing terminal-b's frames and typing into terminal-a.
        let replies = mock.replies(op);
        let release = mock.hold_ws(op, is_focus_hint);
        probe.next_pane().await;
        wait_until("terminal-a's focus hint", || focus_hints(&mock).len() == 1).await;
        probe.assert_live("terminal-b", "terminal-a", 'y').await;
        // Back on terminal-b, the newer focus waits behind the held hint
        // instead of racing it.
        probe.next_pane().await;
        probe.assert_live("terminal-a", "terminal-b", 'z').await;
        assert_eq!(focus_hints(&mock).len(), 1, "one focus hint in flight");
        assert_eq!(mock.replies(op), replies, "the hint stays held");

        release.notify_one();
        wait_until("the follow-up focus hint", || focus_hints(&mock).len() == 2).await;
        let hints = focus_hints(&mock);
        assert_ne!(
            hints[1]["pane"], hints[0]["pane"],
            "the follow-up reports where focus ended"
        );
        wait_for_replies(&mock, op, replies + 2).await;
        probe.assert_live("terminal-a", "terminal-b", 'w').await;
        assert_eq!(
            focus_hints(&mock).len(),
            2,
            "an acknowledged focus is not reported again"
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
    result.expect("live loop exits cleanly");
    assert!(chrome.ticker > ticker_before, "the render tick advanced");
    assert_eq!(written(&mock, "terminal-a"), ["y"]);
    assert_eq!(written(&mock, "terminal-b"), ["x", "z", "w"]);
    mock.shutdown().await;
}

/// The `(rows, cols)` each size claim for `terminal_id` carried, in order.
fn size_claims(mock: &MockDaemon, terminal_id: &str) -> Vec<(u64, u64)> {
    websocket_requests(mock, "terminal_resize")
        .iter()
        .filter(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
        .map(|request| {
            (
                request["rows"].as_u64().expect("resize rows"),
                request["cols"].as_u64().expect("resize cols"),
            )
        })
        .collect()
}

/// The viewport and size requests for `terminal_id`, by kind, in the order
/// the mock received them.
fn geometry_requests(mock: &MockDaemon, terminal_id: &str) -> Vec<String> {
    mock.requests()
        .into_iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body)
        .filter(|body| body.get("terminal_id") == Some(&json!(terminal_id)))
        .filter_map(|body| body.get("type").and_then(Value::as_str).map(str::to_string))
        .filter(|kind| kind == "terminal_set_viewport" || kind == "terminal_resize")
        .collect()
}

/// Zoom toggles in one resize burst: odd, so the burst ends zoomed.
const ZOOM_TOGGLES: usize = 5;

#[tokio::test]
async fn resize_burst_sends_at_most_one_resize_per_pane_in_flight() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    // Room for the whole burst, so all of it is queued before the loop runs.
    let (input_tx, input_rx) = mpsc::channel(ZOOM_TOGGLES * 2);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        let unzoomed = *size_claims(&mock, "terminal-b")
            .last()
            .expect("terminal-b sized at startup");
        let claims_before = size_claims(&mock, "terminal-b").len();
        let requests_before = geometry_requests(&mock, "terminal-b").len();

        // The whole burst is queued before the loop runs again, so every
        // geometry change after the first lands while that resize is out.
        for _ in 0..ZOOM_TOGGLES {
            for (code, modifiers) in [
                (KeyCode::Char('b'), KeyModifiers::CONTROL),
                (KeyCode::Char('z'), KeyModifiers::NONE),
            ] {
                input_tx
                    .try_send(RawInputEvent::Key(TerminalKey::new(code, modifiers)))
                    .expect("the burst fits the input queue");
            }
        }
        wait_until("terminal-b's zoomed size claim", || {
            size_claims(&mock, "terminal-b").len() > claims_before
        })
        .await;
        // Zoomed, terminal-b fills the tab; the loop keeps drawing it and
        // typing into it.
        probe.assert_live("terminal-b", "terminal-b", 'y').await;

        let burst = size_claims(&mock, "terminal-b").split_off(claims_before);
        assert!(
            burst.len() <= 2,
            "one resize in flight and one latest follow-up, not one per change: {burst:?}"
        );
        let zoomed = *burst.last().expect("a zoomed size claim");
        assert!(
            zoomed.0 * zoomed.1 > unzoomed.0 * unzoomed.1,
            "the latest geometry, zoomed, wins: {burst:?} after {unzoomed:?}"
        );
        let order = geometry_requests(&mock, "terminal-b").split_off(requests_before);
        assert!(
            order
                .chunks(2)
                .all(|pair| pair == ["terminal_set_viewport", "terminal_resize"]),
            "each viewport precedes its size claim: {order:?}"
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
    result.expect("live loop exits cleanly");
    assert!(chrome.is_zoomed(), "the burst ended zoomed");
    mock.shutdown().await;
}

/// A paste larger than both socket buffers, so the direct writer blocks on
/// it, and under the client's paste limit.
const STALL_PASTE_BYTES: usize = 900 * 1024;

/// Keys typed behind the blocked paste: more than the direct writer's
/// bounded queue holds.
const QUEUE_FILL_KEYS: usize = 300;

#[tokio::test]
async fn direct_set_viewport_backpressure_is_visible_and_never_stalls_the_loop() {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        // terminal-b types on its own frame socket, never through the daemon.
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        host.wait_for_input(b"x").await;

        // The host stops reading: the paste blocks the writer on the socket
        // and the keys behind it fill the writer's bounded queue.
        host.stall.notify_one();
        probe.paste("p".repeat(STALL_PASTE_BYTES)).await;
        for _ in 0..QUEUE_FILL_KEYS {
            probe.key(KeyCode::Char('k'), KeyModifiers::NONE).await;
        }
        // terminal-b's viewport changes twice while its writer is full.
        probe.zoom().await;
        probe.zoom().await;
        // Frames, ticks and keys on the proxied terminal-a keep flowing.
        probe.next_pane().await;
        probe.assert_live("terminal-a", "terminal-a", 'y').await;
        let batches = probe.batches;
        drop(input_tx);
        batches
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, batches) = tokio::join!(
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
    let pane_b = workspace
        .pane_for_terminal("terminal-b")
        .expect("terminal-b pane");
    assert_eq!(workspace.pane(pane_b).transport(), Some(Transport::Direct));
    let status = workspace.pane(pane_b).status_message().unwrap_or_default();
    assert!(
        status.contains("viewport"),
        "terminal-b's status names the refused viewport: {status:?}"
    );
    assert!(
        chrome
            .toasts
            .iter()
            .all(|active| !active.toast.title.contains("backlog")),
        "a backlog is pane status, never a toast"
    );
    let pane_a = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal-a pane");
    assert!(
        usize::try_from(workspace.pane(pane_a).frames_rendered()).expect("frame count") >= batches,
        "each frame batch fed to terminal-a rendered"
    );
    assert!(chrome.ticker > ticker_before, "the render tick advanced");
    assert_eq!(written(&mock, "terminal-a"), ["y"]);
    assert!(
        written(&mock, "terminal-b").is_empty(),
        "terminal-b never types through the daemon"
    );
    mock.shutdown().await;
}

/// A job that finishes only once `release` is notified.
async fn held_job(release: Arc<Notify>, pane: PaneId) -> JobResult {
    release.notified().await;
    JobResult::Resized {
        pane,
        result: Ok(()),
    }
}

#[tokio::test]
async fn a_late_old_generation_outcome_never_settles_the_reissued_job() {
    let (jobs, mut outcomes) = mpsc::unbounded_channel();
    let mut ledger = JobLedger::default();
    let pane = PaneId(7);
    let key = JobKey::Geometry(pane);

    let (old_tag, _) = ledger
        .issue(key.clone(), Generation(1), (24, 80))
        .expect("an idle key issues");
    let old_release = Arc::new(Notify::new());
    spawn_job(
        &jobs,
        old_tag.clone(),
        held_job(Arc::clone(&old_release), pane),
    );

    // The reconnect frees the slot; the same key is issued anew on the new
    // generation and gains a coalesced follow-up.
    ledger.forget_generation(Generation(1));
    let (new_tag, _) = ledger
        .issue(key.clone(), Generation(2), (30, 90))
        .expect("the forgotten key issues anew");
    assert_ne!(new_tag.id, old_tag.id);
    let new_release = Arc::new(Notify::new());
    spawn_job(
        &jobs,
        new_tag.clone(),
        held_job(Arc::clone(&new_release), pane),
    );
    assert!(
        ledger.issue(key.clone(), Generation(2), (31, 91)).is_none(),
        "a busy key coalesces"
    );
    assert!(
        ledger.issue(key.clone(), Generation(2), (32, 92)).is_none(),
        "the latest offer replaces the held one"
    );

    // The old outcome lands first and settles nothing.
    old_release.notify_one();
    let old = timeout(LIVENESS_DEADLINE, outcomes.recv())
        .await
        .expect("the old outcome lands")
        .expect("job channel open");
    assert_eq!(old.tag, old_tag);
    assert!(
        ledger.settle(&old.tag, Generation(2)).is_none(),
        "the late old outcome releases no follow-up"
    );
    assert_eq!(
        ledger.in_flight(&key),
        Some(new_tag.id),
        "the reissued job keeps its marker"
    );

    // The new outcome clears its marker and releases exactly one follow-up.
    new_release.notify_one();
    let new = timeout(LIVENESS_DEADLINE, outcomes.recv())
        .await
        .expect("the new outcome lands")
        .expect("job channel open");
    assert_eq!(new.tag, new_tag);
    let (follow_up, geometry) = ledger
        .settle(&new.tag, Generation(2))
        .expect("the new outcome releases the follow-up");
    assert_eq!(geometry, (32, 92), "the latest coalesced offer");
    assert_eq!(follow_up.generation, Generation(2));
    assert_eq!(ledger.in_flight(&key), Some(follow_up.id));
    assert!(
        ledger.settle(&new.tag, Generation(2)).is_none(),
        "an outcome settles once"
    );
    assert!(
        ledger.settle(&follow_up, Generation(2)).is_none(),
        "exactly one follow-up"
    );
    assert_eq!(ledger.in_flight(&key), None);
}
