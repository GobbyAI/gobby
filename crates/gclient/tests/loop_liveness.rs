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
use gobby_client::daemon::{Daemon, Generation, LiveDaemon, REQUEST_DEADLINE};
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

/// How long a flood of serial daemon writes may take to drain. Draining is
/// throughput under a loaded test run, not liveness, so it gets more room.
const DRAIN_DEADLINE: Duration = Duration::from_secs(10);

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

async fn wait_until(what: &str, done: impl FnMut() -> bool) {
    wait_within(LIVENESS_DEADLINE, what, done).await;
}

async fn wait_within(deadline: Duration, what: &str, mut done: impl FnMut() -> bool) {
    timeout(deadline, async {
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

    /// A notification can cover frame text; observe the render loop itself.
    async fn draws_continue(&self) {
        let before = self.draws.load(Ordering::SeqCst);
        wait_until("render ticks after the focus reply", || {
            self.draws.load(Ordering::SeqCst) >= before + PROBE_FRAMES
        })
        .await;
    }

    /// The latest drawn status line, the frame's bottom row, comes to show
    /// `text` (`shown`) or to drop it.
    async fn status_line(&self, text: &str, shown: bool) {
        let what = format!(
            "the status line to {} {text:?}",
            if shown { "show" } else { "drop" }
        );
        wait_until(&what, || {
            self.frames
                .lock()
                .expect("recorded frames")
                .last()
                .and_then(|frame| frame.lines().last())
                .is_some_and(|row| row.contains(text) == shown)
        })
        .await;
    }
}

impl LivenessProbe<'_> {
    /// Wait until the latest drawn frame shows `text` anywhere on screen.
    async fn shows(&self, text: &str) {
        wait_until(&format!("the screen to show {text:?}"), || {
            self.frames
                .lock()
                .expect("recorded frames")
                .last()
                .is_some_and(|frame| frame.contains(text))
        })
        .await;
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
/// behind it while the socket stays open. `resume` drains it again.
struct StallingHost {
    _socket_dir: tempfile::TempDir,
    socket_path: PathBuf,
    received: mpsc::UnboundedReceiver<ClientMessage>,
    /// Sent to the client while the host is stalled.
    to_client: mpsc::UnboundedSender<ServerMessage>,
    stall: Arc<Notify>,
    resume: Arc<Notify>,
    task: tokio::task::JoinHandle<()>,
}

impl StallingHost {
    async fn start() -> Self {
        let socket_dir = tempfile::tempdir().expect("direct socket dir");
        let socket_path = socket_dir.path().join("frames.sock");
        let listener =
            tokio::net::UnixListener::bind(&socket_path).expect("bind direct frame socket");
        let (received_tx, received) = mpsc::unbounded_channel();
        let (to_client, mut outgoing) = mpsc::unbounded_channel();
        let stall = Arc::new(Notify::new());
        let stalled = Arc::clone(&stall);
        let resume = Arc::new(Notify::new());
        let resumed = Arc::clone(&resume);
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
            let ClientMessage::AttachTerminal {
                host_terminal_id, ..
            } = read_message_async(&mut stream, MAX_FRAME_SIZE)
                .await
                .expect("direct attach")
            else {
                panic!("the direct client attaches a terminal after the welcome");
            };
            // A host completes the attach with `Attached`; the client waits
            // for it before the pane goes live (#23076).
            write_message_async(
                &mut stream,
                &ServerMessage::Attached {
                    created: false,
                    host_terminal_id,
                },
            )
            .await
            .expect("direct attached");
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
            // Keep the socket open and unread until `resume`, answering with
            // whatever `to_client` carries meanwhile, then drain it.
            loop {
                tokio::select! {
                    biased;
                    () = resumed.notified() => break,
                    Some(message) = outgoing.recv() => {
                        write_message_async(&mut stream, &message)
                            .await
                            .expect("direct host message");
                    }
                }
            }
            while let Ok(message) = read_message_async(&mut stream, MAX_FRAME_SIZE).await {
                let _ = received_tx.send(message);
            }
        });
        Self {
            _socket_dir: socket_dir,
            socket_path,
            received,
            to_client,
            stall,
            resume,
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
    pane_workspace(mock, &["terminal-a", "terminal-b"], direct_b, false).await
}

/// One tab of `terminals` side by side, the last one focused.
async fn pane_workspace(
    mock: &MockDaemon,
    terminals: &[&str],
    direct_b: Option<&StallingHost>,
    external: bool,
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    let items: Vec<Value> = terminals
        .iter()
        .map(|terminal_id| {
            let mut item =
                json!({"terminal_id": terminal_id, "backend": "native", "state": "live"});
            if external {
                item["ownership"] = json!("external");
            }
            if let (Some(host), "terminal-b") = (direct_b, *terminal_id) {
                item["attach"] = host.roster_attach(terminal_id);
                mock.serve_direct_attach(host.attach_locator(terminal_id));
            }
            item
        })
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
    let focused = terminals.last().expect("at least one terminal");
    mock.seed_workspace("project-1", &[(terminals, focused)]);
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

async fn exercise_failed_focus_hint(coalesced_successor: bool) -> usize {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let op = "workspace_op";

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        assert!(
            focus_hints(&mock).is_empty(),
            "initial focus is already stored"
        );

        let replies = mock.replies(op);
        mock.enqueue_workspace_refusal("busy", "focus hint temporarily refused");
        let failed_hint = mock.hold_ws(op, is_focus_hint);
        probe.next_pane().await;
        wait_until("the failing focus hint", || focus_hints(&mock).len() == 1).await;
        probe.assert_live("terminal-b", "terminal-a", 'y').await;

        if coalesced_successor {
            // End on A again, so the pending hint differs from the stored B
            // and matches the failed A. Comparing hint values cannot tell
            // the failed job from the already offered successor.
            probe.next_pane().await;
            probe.assert_live("terminal-a", "terminal-b", 'z').await;
            probe.next_pane().await;
            probe.assert_live("terminal-b", "terminal-a", 'v').await;
            assert_eq!(focus_hints(&mock).len(), 1, "the successor is coalesced");
        }

        // Keep the retry/successor in flight while more loop iterations run.
        let recovered_hint = mock.hold_ws(op, is_focus_hint);
        failed_hint.notify_one();
        wait_until("the recovery focus hint", || focus_hints(&mock).len() == 2).await;
        let hints = focus_hints(&mock);
        assert_eq!(hints[1]["pane"], hints[0]["pane"], "focus still ends on A");
        probe.key_reaches("terminal-a", 'u').await;
        probe.draws_continue().await;
        assert_eq!(focus_hints(&mock).len(), 2, "one recovery hint in flight");

        recovered_hint.notify_one();
        wait_for_replies(&mock, op, replies + 2).await;
        probe.key_reaches("terminal-a", 'w').await;
        probe.draws_continue().await;
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
    let sent = focus_hints(&mock).len();
    mock.shutdown().await;
    sent
}

#[tokio::test]
async fn failed_focus_hint_sends_its_coalesced_successor_once() {
    assert_eq!(
        exercise_failed_focus_hint(true).await,
        2,
        "a failed hint must not offer its coalesced successor again"
    );
}

#[tokio::test]
async fn failed_focus_hint_without_a_successor_is_retried() {
    assert_eq!(
        exercise_failed_focus_hint(false).await,
        2,
        "a failed hint without a successor must be retried once"
    );
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

/// The status-line words for a viewport the direct writer refused.
const VIEWPORT_DEFERRED: &str = "resize deferred";

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
        // terminal-b's viewport changes twice while its writer is full, and
        // the status line says the resize waits.
        probe.zoom().await;
        probe.zoom().await;
        probe.status_line(VIEWPORT_DEFERRED, true).await;
        // Frames and ticks on the proxied terminal-a keep flowing while b's
        // viewport waits.
        probe.next_pane().await;
        probe.frames_render("terminal-a").await;
        probe.status_line(VIEWPORT_DEFERRED, true).await;
        // Once the host drains, the warning leaves the status line and a's
        // key goes out.
        host.resume.notify_one();
        probe.status_line(VIEWPORT_DEFERRED, false).await;
        probe.key_reaches("terminal-a", 'y').await;
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

/// Alerts in the log whose title is exactly `title`.
fn alerts(chrome: &Chrome, title: &str) -> usize {
    chrome
        .alert_log
        .iter()
        .filter(|toast| toast.title == title)
        .count()
}

#[tokio::test]
async fn typing_into_a_tmux_pane_while_terminal_input_is_held_keeps_frames_flowing() {
    let mock = MockDaemon::start("local-token").await;
    mock.set_attach_backend("terminal-b", "tmux");
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;

        // The daemon holds terminal-b's next write. Keys typed behind it
        // wait their turn, and the loop keeps drawing both panes meanwhile.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        probe.frames_render("terminal-a").await;
        probe.frames_render("terminal-b").await;
        assert_eq!(
            written(&mock, "terminal-b"),
            ["x", "y"],
            "a key typed behind a held write waits for it"
        );

        release.notify_one();
        wait_until("the queued key after the held write", || {
            written(&mock, "terminal-b") == ["x", "y", "z"]
        })
        .await;
        probe.frames_render("terminal-a").await;
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
    let pane_b = workspace
        .pane_for_terminal("terminal-b")
        .expect("terminal-b pane");
    assert!(
        workspace.pane(pane_b).writable(),
        "the held write's delivery keeps the pane writable"
    );
    assert_eq!(written(&mock, "terminal-b"), ["x", "y", "z"]);
    mock.shutdown().await;
}

#[tokio::test]
async fn a_held_attention_response_never_stalls_frames_or_applies_twice() {
    let mock = MockDaemon::start("local-token").await;
    for _ in 0..4 {
        mock.enqueue("GET", "/api/attention/roster", 200, attention_roster());
    }
    let respond = "/api/attention/run:1/respond";
    let release = mock.enqueue_held("POST", respond, 200, json!({"ok": true}));
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);
    let posted = |mock: &MockDaemon| {
        mock.requests()
            .iter()
            .filter(|request| request.method == "POST" && request.target == respond)
            .count()
    };

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;

        // Answer the blocked run; the daemon holds the response.
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(KeyCode::Char('a'), KeyModifiers::NONE).await;
        probe.shows("Ship this change?").await;
        probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        wait_until("the held response", || posted(&mock) == 1).await;
        probe.frames_render("terminal-a").await;
        // A second Enter while the first is held answers nothing.
        probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        probe.frames_render("terminal-b").await;

        release.notify_one();
        probe.shows("Response sent.").await;
        probe.draws_continue().await;
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
    assert_eq!(posted(&mock), 1, "one response reaches the daemon");
    assert_eq!(
        alerts(&chrome, "Response sent."),
        1,
        "the response applies once"
    );
    assert!(
        chrome.dialog.is_none(),
        "the applied response closes the dialog"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_stale_attention_response_is_never_retried_and_leaves_the_prompt_answerable() {
    let mock = MockDaemon::start("local-token").await;
    for _ in 0..4 {
        mock.enqueue("GET", "/api/attention/roster", 200, attention_roster());
    }
    let respond = "/api/attention/run:1/respond";
    mock.enqueue(
        "POST",
        respond,
        409,
        json!({"detail": {"code": "stale_episode", "message": "stale-episode"}}),
    );
    mock.enqueue("POST", respond, 200, json!({"ok": true}));
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let posted = |mock: &MockDaemon| {
        mock.requests()
            .iter()
            .filter(|request| request.method == "POST" && request.target == respond)
            .count()
    };

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(KeyCode::Char('a'), KeyModifiers::NONE).await;
        probe.shows("Ship this change?").await;
        probe.shows("Approve").await;
        probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        probe.shows("stale").await;
        assert_eq!(posted(&mock), 1, "a stale response is not retried");

        // The refusal left the prompt open and answerable by hand.
        probe.key(KeyCode::Enter, KeyModifiers::NONE).await;
        probe.shows("Response sent.").await;
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
    assert_eq!(posted(&mock), 2, "each answer posts exactly once");
    assert!(
        chrome
            .alert_log
            .iter()
            .any(|alert| alert.title.contains("stale_episode")),
        "the stale episode stays visible in the alert log"
    );
    assert!(
        chrome.dialog.is_none(),
        "the accepted answer closes the dialog"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_held_terminal_input_flood_is_bounded_ordered_and_nonblocking() {
    const PASTE: usize = 700 * 1024;
    const KEYS: usize = 300;
    let mock = MockDaemon::start("local-token").await;
    mock.set_attach_backend("terminal-b", "tmux");
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let keys: Vec<String> = (0..KEYS)
        .map(|index| char::from(b'c' + (index % 20) as u8).to_string())
        .collect();
    let pasted = |mock: &MockDaemon| {
        websocket_requests(mock, "terminal_paste")
            .iter()
            .filter(|request| request.get("terminal_id") == Some(&json!("terminal-b")))
            .filter_map(|request| request.get("text").and_then(Value::as_str))
            .map(str::len)
            .collect::<Vec<_>>()
    };

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;

        // 'y' is held in flight; everything after it queues behind it.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        // The second paste would take the queue past 1 MiB.
        probe.paste("p".repeat(PASTE)).await;
        probe.paste("q".repeat(PASTE)).await;
        // The first paste plus 255 keys fill the 256-message queue.
        for key in &keys {
            let key = key.chars().next().expect("one char");
            probe.key(KeyCode::Char(key), KeyModifiers::NONE).await;
        }
        probe.frames_render("terminal-b").await;
        assert_eq!(written(&mock, "terminal-b"), ["x", "y"], "the flood waits");

        // The other pane takes its key and draws while terminal-b is held.
        // Its take waits for terminal-b's release, which follows the
        // accepted flood, so the key lands after that.
        probe.next_pane().await;
        probe.key(KeyCode::Char('k'), KeyModifiers::NONE).await;
        probe.frames_render("terminal-a").await;
        probe.frames_render("terminal-b").await;
        assert!(
            written(&mock, "terminal-a").is_empty(),
            "terminal-a's key waits for terminal-b's release"
        );

        release.notify_one();
        wait_within(
            DRAIN_DEADLINE,
            "the accepted flood after the held write",
            || written(&mock, "terminal-b").len() == 2 + 255,
        )
        .await;
        wait_until("terminal-a's key after the release", || {
            written(&mock, "terminal-a") == ["k"]
        })
        .await;
        probe.frames_render("terminal-a").await;
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
    let mut accepted = vec!["x".to_string(), "y".to_string()];
    accepted.extend(keys[..255].iter().cloned());
    assert_eq!(
        written(&mock, "terminal-b"),
        accepted,
        "accepted keys arrive in typed order and only the newest are dropped"
    );
    assert_eq!(pasted(&mock), [PASTE], "only the paste under 1 MiB is sent");
    let pane_b = workspace
        .pane_for_terminal("terminal-b")
        .expect("terminal-b pane");
    assert_eq!(
        workspace.pane(pane_b).status_message(),
        Some("terminal input backlog; key dropped"),
        "the rejected input reports backpressure"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn held_input_then_focus_move_delivers_or_reports_before_release() {
    let mock = MockDaemon::start("local-token").await;
    mock.set_attach_backend("terminal-b", "tmux");
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);
    let released = |mock: &MockDaemon| {
        control_wire(mock)
            .iter()
            .any(|line| line.starts_with("release terminal-b"))
    };

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;

        // 'y' is held in flight and 'z' queues behind it; then focus leaves.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        probe.next_pane().await;
        probe.frames_render("terminal-a").await;
        probe.frames_render("terminal-b").await;
        assert!(
            !released(&mock),
            "the release waits for the accepted writes"
        );

        release.notify_one();
        wait_until("terminal-a's take", || {
            control_wire(&mock)
                .iter()
                .any(|line| line.starts_with("take terminal-a"))
        })
        .await;
        probe.draws_continue().await;
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
    let wire = control_wire(&mock);
    let after_x = wire
        .iter()
        .position(|line| line == "input terminal-b x")
        .expect("x on the wire");
    assert_eq!(
        until_take(&wire[after_x + 1..], "terminal-a"),
        [
            "input terminal-b y",
            "input terminal-b z",
            "release terminal-b",
            "take terminal-a",
        ],
        "A's accepted writes, then A's release, then B's take; nothing replayed"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_focus_move_writes_release_before_take_on_the_wire() {
    let mock = MockDaemon::start("local-token").await;
    mock.set_attach_backend("terminal-b", "tmux");
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        probe.next_pane().await;
        wait_until("terminal-a's take", || {
            control_wire(&mock)
                .iter()
                .any(|line| line.starts_with("take terminal-a"))
        })
        .await;
        probe.frames_render("terminal-a").await;
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
    let wire = control_wire(&mock);
    let after_x = wire
        .iter()
        .position(|line| line == "input terminal-b x")
        .expect("x on the wire");
    assert_eq!(
        until_take(&wire[after_x + 1..], "terminal-a"),
        ["release terminal-b", "take terminal-a"],
        "the old pane's release reaches the daemon before the new pane's take"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn control_waits_for_an_attachment_from_the_reconnected_generation() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("attach both panes");
    let pane_id = workspace.pane_for_terminal("terminal-b").expect("pane b");
    let daemon = workspace.daemon().clone();
    let generation = daemon.generation();
    mock.drop_websockets();
    let reconnected = daemon
        .reconnect(generation)
        .await
        .expect("reconnect daemon");
    assert_ne!(reconnected, generation);

    workspace.request_control(pane_id, false);
    workspace.start_control_request(&mpsc::unbounded_channel().0);
    assert!(
        !workspace.pane(pane_id).is_acquiring(),
        "a stale attachment must wait for reattachment before requesting control"
    );
    assert!(
        workspace.awaiting_control(pane_id),
        "the pending wish survives"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn focus_a_b_c_with_a_held_writer_orders_every_release_before_the_next_take() {
    let mock = MockDaemon::start("local-token").await;
    mock.set_attach_backend("terminal-c", "tmux");
    let (mut workspace, _home) = pane_workspace(
        &mock,
        &["terminal-a", "terminal-b", "terminal-c"],
        None,
        false,
    )
    .await;
    let (backend, draws, frames) = DrawRecorder::new(120, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 3).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-c", 'x').await;

        // 'y' is held on C; focus then moves C to A to B.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-c"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-c").len() == 2).await;
        probe.next_pane().await;
        probe.next_pane().await;
        probe.frames_render("terminal-b").await;
        let wire = control_wire(&mock);
        let after_x = wire.iter().position(|line| line == "input terminal-c x");
        assert_eq!(
            wire[after_x.expect("x on the wire") + 1..],
            ["input terminal-c y"],
            "nothing is released or taken while C's write is held"
        );

        release.notify_one();
        wait_until("terminal-b's take", || {
            control_wire(&mock)
                .iter()
                .any(|line| line.starts_with("take terminal-b"))
        })
        .await;
        probe.draws_continue().await;
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
    let wire = control_wire(&mock);
    let after_x = wire
        .iter()
        .position(|line| line == "input terminal-c x")
        .expect("x on the wire");
    assert_eq!(
        until_take(&wire[after_x + 1..], "terminal-b"),
        [
            "input terminal-c y",
            "release terminal-c",
            "take terminal-a",
            "release terminal-a",
            "take terminal-b",
        ],
        "each release follows its pane's bytes and take; each take follows every earlier release"
    );
    mock.shutdown().await;
}

/// `wire` up to and including the take of `terminal_id`; the loop's shutdown
/// releases the last lease after it.
fn until_take<'a>(wire: &'a [String], terminal_id: &str) -> Vec<&'a str> {
    let take = format!("take {terminal_id}");
    let end = wire
        .iter()
        .position(|line| *line == take)
        .map_or(wire.len(), |at| at + 1);
    wire[..end].iter().map(String::as_str).collect()
}

/// Input, release and take requests in the order the mock read them, as
/// `"<verb> <terminal> [data]"`.
fn control_wire(mock: &MockDaemon) -> Vec<String> {
    mock.requests()
        .iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body.as_ref())
        .filter_map(|body| {
            let verb = match body.get("type").and_then(Value::as_str)? {
                "terminal_input" => "input",
                "terminal_release_control" => "release",
                "terminal_take_control" => "take",
                "terminal_kill" => "kill",
                "workspace_op" => {
                    let op = body.get("op").and_then(Value::as_str)?;
                    return matches!(op, "pane.close" | "tab.close").then(|| format!("op {op}"));
                }
                _ => return None,
            };
            let terminal = body.get("terminal_id").and_then(Value::as_str)?;
            let line = match body.get("data").and_then(Value::as_str) {
                Some(data) => format!("{verb} {terminal} {data}"),
                None => format!("{verb} {terminal}"),
            };
            Some(line)
        })
        .collect()
}

/// One blocked run asking a yes/no question.
fn attention_roster() -> serde_json::Value {
    json!({
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
                    "options": [{"option": 1, "label": "Approve"}]
                }
            }
        }]
    })
}

const UNCONFIRMED_INPUT: &str = "Input delivery unconfirmed.";
const REFUSED_INPUT: &str = "Input refused: write_seq_conflict.";

/// Type into tmux pane B until a write's reply is held, queue two more
/// behind it, then drop the connection with that reply outstanding. After
/// the reconnect, focus leaves B and comes back, and `k` is typed under the
/// new generation. A refusal correlated to its request is typed first.
async fn drop_a_held_write() -> (MockDaemon, Chrome) {
    let mock = MockDaemon::start("local-token").await;
    mock.set_attach_backend("terminal-b", "tmux");
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        mock.enqueue_write_outcome("refused", Some("write_seq_conflict"));
        probe.key_reaches("terminal-b", 'r').await;
        probe.key_reaches("terminal-b", 'x').await;

        let _held = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 3).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('w'), KeyModifiers::NONE).await;
        mock.drop_websockets();
        wait_until("the reconnect", || mock.websocket_handshakes() >= 2).await;
        wait_until("both panes reattached", || {
            websocket_requests(&mock, "terminal_attach").len() >= 4
        })
        .await;
        probe.draws_continue().await;

        let takes = websocket_requests(&mock, "terminal_take_control").len();
        probe.next_pane().await;
        probe.next_pane().await;
        wait_until("terminal-b taken again", || {
            websocket_requests(&mock, "terminal_take_control")
                .iter()
                .skip(takes)
                .any(|take| take.get("terminal_id") == Some(&json!("terminal-b")))
        })
        .await;
        probe.key_reaches("terminal-b", 'k').await;
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
    (mock, chrome)
}

/// A2.12: the held write is unconfirmed, the two behind it are reported
/// unsent with their exact count, the reconnect sends none of them, and the
/// correlated refusal is reported as refused.
#[tokio::test]
async fn indeterminate_daemon_write_is_unconfirmed_and_never_replayed() {
    let (mock, chrome) = drop_a_held_write().await;
    assert_eq!(
        written(&mock, "terminal-b"),
        ["r", "x", "y", "k"],
        "z and w never go out and y is never replayed"
    );
    assert_eq!(alerts(&chrome, UNCONFIRMED_INPUT), 1);
    assert_eq!(alerts(&chrome, "Input not sent: 2 bytes."), 1);
    assert_eq!(alerts(&chrome, REFUSED_INPUT), 1);
    let body = |title: &str| {
        chrome
            .alert_log
            .iter()
            .find(|toast| toast.title == title)
            .and_then(|toast| toast.body.clone())
    };
    assert_eq!(
        body("Input not sent: 2 bytes."),
        Some("2 writes".to_owned()),
        "the abandoned report counts z and w"
    );
    assert_eq!(
        body(REFUSED_INPUT),
        Some("1 bytes".to_owned()),
        "the refusal carries its request's size"
    );
    assert_eq!(body(UNCONFIRMED_INPUT), None, "no byte count is known");
    mock.shutdown().await;
}

/// A2.15: the old generation's reports survive the reconnect and the new
/// attachment.
#[tokio::test]
async fn old_generation_write_uncertainty_is_reported_after_reconnect() {
    let (mock, chrome) = drop_a_held_write().await;
    let reported: Vec<&str> = chrome
        .alert_log
        .iter()
        .map(|toast| toast.title.as_str())
        .filter(|title| title.starts_with("Input "))
        .collect();
    assert_eq!(
        reported,
        [REFUSED_INPUT, UNCONFIRMED_INPUT, "Input not sent: 2 bytes."]
    );
    mock.shutdown().await;
}

/// A2.4: the old generation's write error leaves the new attachment
/// writable: `k` typed after the reconnect reaches the daemon.
#[tokio::test]
async fn a_write_error_from_a_stale_generation_does_not_mark_the_pane_read_only() {
    let (mock, _chrome) = drop_a_held_write().await;
    assert_eq!(
        written(&mock, "terminal-b").last().map(String::as_str),
        Some("k")
    );
    mock.shutdown().await;
}

/// The terminal-b lines after its first write 'x'.
fn wire_after_x(mock: &MockDaemon) -> Vec<String> {
    let wire = control_wire(mock);
    let after_x = wire
        .iter()
        .position(|line| line == "input terminal-b x")
        .expect("x on the wire");
    wire[after_x + 1..].to_vec()
}

/// Two panes with terminal-b on tmux and focused, closed without a
/// confirmation.
async fn closing_workspace(
    mock: &MockDaemon,
    external: bool,
) -> (Workspace<LiveDaemon>, tempfile::TempDir, Chrome) {
    mock.set_attach_backend("terminal-b", "tmux");
    let (workspace, home) =
        pane_workspace(mock, &["terminal-a", "terminal-b"], None, external).await;
    // The relist after a kill lists only terminal-a.
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-a", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 2}
        }),
    );
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    (workspace, home, chrome)
}

#[tokio::test]
async fn closing_a_pane_while_its_release_is_pending_detaches_after_the_release() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home, mut chrome) = closing_workspace(&mock, true).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        // 'y' is held on b, so b's release waits behind it once focus
        // moves to a; closing the tab then must wait for that release.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.next_pane().await;
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(KeyCode::Char('X'), KeyModifiers::SHIFT).await;
        probe.draws_continue().await;
        assert_eq!(
            wire_after_x(&mock),
            ["input terminal-b y"],
            "nothing is released or closed while b's write is held"
        );
        release.notify_one();
        wait_until("the tab close", || {
            control_wire(&mock)
                .iter()
                .any(|line| line == "op tab.close")
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
    let wire = wire_after_x(&mock);
    let releases: Vec<usize> = wire
        .iter()
        .enumerate()
        .filter(|(_, line)| *line == "release terminal-b")
        .map(|(at, _)| at)
        .collect();
    assert_eq!(releases.len(), 1, "b is released once: {wire:?}");
    let close = wire.iter().position(|line| line == "op tab.close");
    assert!(
        close.is_some_and(|close| close > releases[0]),
        "the close follows b's release: {wire:?}"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn close_and_generation_change_never_replay_queued_input() {
    // A close, of the pane or of its terminal: b's queued writes go out
    // before its terminal is killed.
    for close in [
        (KeyCode::Char('x'), KeyModifiers::NONE),
        (KeyCode::Char('D'), KeyModifiers::SHIFT),
    ] {
        let wire = close_behind_held_writes(close).await;
        assert_eq!(
            wire[..4],
            [
                "input terminal-b y",
                "input terminal-b z",
                "input terminal-b w",
                "kill terminal-b",
            ],
            "every queued write leaves before the kill ({close:?})"
        );
    }
    // A generation change: b's queued writes are dropped, and the next key
    // goes out under the new attachment.
    let writes = generation_change_drops_queued_input().await;
    let attachment = |data: &str| {
        writes
            .iter()
            .find(|(sent, _)| sent == data)
            .map(|(_, attachment)| attachment.clone())
            .expect("write on the wire")
    };
    assert_ne!(
        attachment("k"),
        attachment("y"),
        "k goes out under the new attachment"
    );
    assert!(
        writes.iter().all(|(data, _)| data != "z" && data != "w"),
        "the queued writes are never sent: {writes:?}"
    );
}

/// Holds b's write 'y' with 'z' and 'w' queued behind it, then closes b
/// with the prefix and `close`.
async fn close_behind_held_writes(close: (KeyCode, KeyModifiers)) -> Vec<String> {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home, mut chrome) = closing_workspace(&mock, false).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('w'), KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(close.0, close.1).await;
        probe.draws_continue().await;
        assert_eq!(
            wire_after_x(&mock),
            ["input terminal-b y"],
            "the kill waits for b's queued writes ({close:?})"
        );
        release.notify_one();
        wait_until("the kill", || {
            control_wire(&mock)
                .iter()
                .any(|line| line == "kill terminal-b")
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
    let wire = wire_after_x(&mock);
    mock.shutdown().await;
    wire
}

/// A generation change: nothing typed on the old connection goes out under
/// the new attachment.
async fn generation_change_drops_queued_input() -> Vec<(String, String)> {
    let (mock, _chrome) = drop_a_held_write().await;
    let writes: Vec<(String, String)> = websocket_requests(&mock, "terminal_input")
        .iter()
        .filter(|write| write.get("terminal_id") == Some(&json!("terminal-b")))
        .filter_map(|write| {
            let data = write.get("data").and_then(Value::as_str)?;
            let attachment = write.get("attachment_id").and_then(Value::as_str)?;
            Some((data.to_owned(), attachment.to_owned()))
        })
        .collect();
    mock.shutdown().await;
    writes
}

/// Types 'y' into the direct terminal-b, then moves focus to terminal-a.
/// With `late`, the host stops reading before 'y' and only refuses it after
/// the release reached the daemon, the way gterm does when the revoke on its
/// control socket overtakes the bytes on the frame socket. Returns the
/// chrome, any input the host read after 'y', and terminal-b's status and
/// take-back offer.
async fn release_behind_a_direct_write(
    late: bool,
) -> (Chrome, Vec<Vec<u8>>, Option<String>, bool, MockDaemon) {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        host.wait_for_input(b"x").await;
        if late {
            host.stall.notify_one();
        }
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        if !late {
            host.wait_for_input(b"y").await;
        }
        probe.next_pane().await;
        // The release waits only for 'y' to reach the frame socket, never
        // for the host to read it.
        wait_until("b's release", || {
            control_wire(&mock)
                .iter()
                .any(|line| line == "release terminal-b")
        })
        .await;
        if late {
            host.to_client
                .send(ServerMessage::InputRefused {
                    code: "input_not_granted".into(),
                })
                .expect("host refuses the late input");
            probe.shows(UNCONFIRMED_INPUT).await;
            host.resume.notify_one();
            host.wait_for_input(b"y").await;
        }
        probe.assert_live("terminal-a", "terminal-a", 'k').await;
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
    let mut typed = Vec::new();
    while let Ok(message) = host.received.try_recv() {
        if let ClientMessage::Input { data } = message {
            typed.push(data);
        }
    }
    let pane_b = workspace
        .pane_for_terminal("terminal-b")
        .expect("terminal-b pane");
    let pane_b = workspace.pane(pane_b);
    let status = pane_b.status_message().map(str::to_owned);
    let take_back = pane_b.has_take_back();
    (chrome, typed, status, take_back, mock)
}

/// A2.11: a direct pane's release orders behind the local flush of its
/// input alone, and a host that consumes that input only after the revoke
/// is reported as unconfirmed, never replayed.
#[tokio::test]
async fn direct_flush_receipt_orders_locally_and_late_consumption_is_unconfirmed() {
    let (chrome, typed, status, take_back, mock) = release_behind_a_direct_write(true).await;
    assert_eq!(alerts(&chrome, UNCONFIRMED_INPUT), 1);
    let unconfirmed = chrome
        .alert_log
        .iter()
        .find(|toast| toast.title == UNCONFIRMED_INPUT)
        .expect("unconfirmed report");
    assert_eq!(unconfirmed.body, None, "no byte count is known");
    assert_eq!(
        typed,
        Vec::<Vec<u8>>::new(),
        "the host read y once and nothing was replayed after it"
    );
    assert!(
        written(&mock, "terminal-b").is_empty(),
        "nothing falls back through the daemon"
    );
    assert_eq!(
        status, None,
        "a pane that let go is not told to take control"
    );
    assert!(!take_back);
    mock.shutdown().await;

    // The host reads 'y' before the revoke: accepted, nothing to report.
    let (chrome, typed, status, take_back, mock) = release_behind_a_direct_write(false).await;
    assert_eq!(alerts(&chrome, UNCONFIRMED_INPUT), 0);
    assert_eq!(typed, Vec::<Vec<u8>>::new(), "nothing was replayed after y");
    assert_eq!(status, None);
    assert!(!take_back);
    mock.shutdown().await;
}

/// R6 F1: a pane that leaves, is taken again and leaves again needs a second
/// release behind the first, even when its writer already holds a full
/// backlog. That barrier is never dropped, so the lease the second take wins
/// is always given back.
#[tokio::test]
async fn a_second_release_behind_a_full_backlog_is_never_lost() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        probe.key_reaches("terminal-b", 'x').await;
        // 'h' is held on the wire and the keys behind it fill b's queue.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('h'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        for _ in 0..QUEUE_FILL_KEYS {
            probe.key(KeyCode::Char('k'), KeyModifiers::NONE).await;
        }
        // b to a, back to b, and away again: b's second release queues
        // behind the first while its backlog is still full.
        probe.next_pane().await;
        probe.next_pane().await;
        probe.next_pane().await;
        probe.draws_continue().await;
        release.notify_one();
        let takes = || {
            control_wire(&mock)
                .into_iter()
                .filter(|line| !line.starts_with("input "))
                .collect::<Vec<_>>()
        };
        // This waits for the full serial write backlog before the releases
        // and takes. Rendering progress keeps its separate liveness bound.
        timeout(DRAIN_DEADLINE, async {
            while takes().iter().filter(|line| line.starts_with("take ")).count() < 4 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap_or_else(|_| {
            panic!(
                "timed out waiting for the last take: {:?} inputs={} input_replies={} take_replies={} release_replies={}",
                takes(),
                written(&mock, "terminal-b").len(),
                mock.replies("terminal_input"),
                mock.replies("terminal_take_control"),
                mock.replies("terminal_release_control")
            )
        });
        probe.draws_continue().await;
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
    let wire: Vec<String> = control_wire(&mock)
        .into_iter()
        .filter(|line| !line.starts_with("input "))
        .collect();
    let last = |verb: &str| wire.iter().rposition(|line| line == verb);
    let take_b = last("take terminal-b").expect("b taken again");
    assert!(
        last("release terminal-b").is_some_and(|release| release > take_b),
        "b's second lease is released after its take: {wire:?}"
    );
    assert!(
        last("take terminal-a").is_some_and(|take| take > take_b),
        "a holds the focus last: {wire:?}"
    );
    assert!(
        chrome
            .toasts
            .iter()
            .all(|active| !active.toast.title.contains("already in flight")),
        "a's second take waits for its first to be answered"
    );
    mock.shutdown().await;
}

/// Stall b's frame socket under a paste the host never reads.
async fn stall_direct_b(probe: &LivenessProbe<'_>, host: &mut StallingHost) {
    probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
    host.wait_for_input(b"x").await;
    host.stall.notify_one();
    probe.paste("p".repeat(STALL_PASTE_BYTES)).await;
    for _ in 0..QUEUE_FILL_KEYS {
        probe.key(KeyCode::Char('k'), KeyModifiers::NONE).await;
    }
}

/// R6 F3: a direct pane's close waits for its typed bytes to reach the frame
/// socket before the kill goes out, as its release does.
#[tokio::test]
async fn closing_a_direct_pane_kills_after_its_typed_bytes_flush() {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-a", "backend": "native", "state": "live"}],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 2}
        }),
    );
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    chrome.prefs.confirm_close = false;
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        wait_for_replies(&mock, "terminal_take_control", 1).await;
        stall_direct_b(&probe, &mut host).await;
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        probe.draws_continue().await;
        assert!(
            !control_wire(&mock).contains(&"kill terminal-b".to_string()),
            "the kill waits for b's paste to reach its frame socket"
        );
        host.resume.notify_one();
        wait_until("the kill", || {
            control_wire(&mock).contains(&"kill terminal-b".to_string())
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
        written(&mock, "terminal-b").is_empty(),
        "terminal-b never types through the daemon"
    );
    mock.shutdown().await;
}

/// F4 ruling: a direct pane whose host stopped reading holds its release for
/// at most `REQUEST_DEADLINE`. Then the pane reports its input unconfirmed
/// once, its source retires through recovery, and the take on the next pane
/// goes out; frames and ticks never wait on it.
#[tokio::test]
async fn a_stalled_direct_release_is_bounded_and_never_blocks_the_next_take() {
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
        stall_direct_b(&probe, &mut host).await;
        probe.next_pane().await;
        // Frames on terminal-a flow while b's release waits.
        probe.frames_render("terminal-a").await;
        timeout(REQUEST_DEADLINE + LIVENESS_DEADLINE, async {
            while !control_wire(&mock).contains(&"take terminal-a".to_string()) {
                probe.draws_continue().await;
            }
        })
        .await
        .expect("a's take goes out within the bound");
        // b's barrier reports before its release, so the toast is up by the
        // time a's take is on the wire; the next keypress clears it (D3).
        probe.shows(UNCONFIRMED_INPUT).await;
        probe.key_reaches("terminal-a", 'y').await;
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
    assert_eq!(alerts(&chrome, UNCONFIRMED_INPUT), 1, "reported once");
    let pane_b = workspace
        .pane_for_terminal("terminal-b")
        .expect("terminal-b pane");
    assert!(
        !workspace.pane(pane_b).is_uncertain_readonly(),
        "b already let go, so it keeps observing"
    );
    assert!(chrome.ticker > ticker_before, "the render tick advanced");
    assert_eq!(written(&mock, "terminal-a"), ["y"]);
    assert!(
        written(&mock, "terminal-b").is_empty(),
        "nothing typed on b is replayed through the daemon"
    );
    let wire = control_wire(&mock);
    let at = |line: &str| wire.iter().position(|sent| sent == line);
    assert!(
        at("release terminal-b") < at("take terminal-a"),
        "b's release still precedes a's take: {wire:?}"
    );
    mock.shutdown().await;
}
