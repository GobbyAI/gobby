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

#[path = "loop_liveness/lifecycle.rs"]
mod lifecycle;

#[path = "mock_daemon/draw.rs"]
mod draw;
use draw::DrawRecorder;

use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{KeyCode, KeyModifiers, MouseEvent, MouseEventKind};
use gobby_client::app::run_loop::RENDER_TICK;
use gobby_client::app::{run_live_loop, spawn_job, JobKey, JobLedger, JobResult, PaneId};
use gobby_client::daemon::{Daemon, Generation, LiveDaemon};
use gobby_client::frame_source::{FrameError, PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::hit::{hit_test, Hit};
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneModes, ServerMessage, MAX_FRAME_SIZE,
};
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::{live_workspace_on_direct_host, DirectHost, MockDaemon};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};
use tokio::time::timeout;
use tracing::subscriber::DefaultGuard;

/// How long a live loop may take to show one probe's progress.
const LIVENESS_DEADLINE: Duration = Duration::from_secs(2);

/// How long a flood of serial daemon writes may take to drain. Draining is
/// throughput under a loaded test run, not liveness, so it gets more room.
const DRAIN_DEADLINE: Duration = Duration::from_secs(10);

/// Frames fed per probe, one per render tick.
const PROBE_FRAMES: usize = 3;

fn encoded_frame(text: &str) -> String {
    encode_message(&frame_message(text))
}

/// The same frame with its pane on the alternate screen.
fn encoded_alternate_frame(text: &str) -> String {
    let ServerMessage::Frame(mut frame) = frame_message(text) else {
        unreachable!("frame_message builds a frame");
    };
    frame.modes.alternate_on = true;
    encode_message(&ServerMessage::Frame(frame))
}

fn encode_message(message: &ServerMessage) -> String {
    let mut framed = Vec::new();
    write_message(&mut framed, message).expect("encode test frame");
    STANDARD.encode(&framed[4..])
}

/// One row of `text`, as a host or the daemon's proxy delivers it.
fn frame_message(text: &str) -> ServerMessage {
    ServerMessage::Frame(FrameData {
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
    })
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

/// The attachment named by `terminal_id`'s most recent viewport claim.
fn latest_claim(mock: &MockDaemon, terminal_id: &str) -> Option<String> {
    websocket_requests(mock, "terminal_set_viewport")
        .iter()
        .rev()
        .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
        .and_then(|request| request.get("attachment_id").and_then(Value::as_str))
        .map(str::to_string)
}

/// Every attachment that has made a viewport claim, in first-claim order.
fn claimed_attachments(mock: &MockDaemon) -> Vec<String> {
    let mut claimed: Vec<String> = Vec::new();
    for request in websocket_requests(mock, "terminal_set_viewport") {
        if let Some(id) = request.get("attachment_id").and_then(Value::as_str) {
            if !claimed.iter().any(|known| known == id) {
                claimed.push(id.to_string());
            }
        }
    }
    claimed
}

/// The attachment `terminal_id` was last reported focused on, while it is focused.
/// Focus is the observable on both transports: direct panes send no viewport claim
/// on the WebSocket, but every focused live pane sends `terminal_focus`.
fn focused_attachment(mock: &MockDaemon, terminal_id: &str) -> Option<String> {
    websocket_requests(mock, "terminal_focus")
        .iter()
        .rev()
        .find(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
        .filter(|request| request.get("focused").and_then(Value::as_bool) == Some(true))
        .and_then(|request| request.get("attachment_id").and_then(Value::as_str))
        .map(str::to_string)
}

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

    async fn mouse(&self, kind: MouseEventKind, column: u16, row: u16) {
        timeout(
            LIVENESS_DEADLINE,
            self.input.send(RawInputEvent::Mouse(MouseEvent {
                kind,
                column,
                row,
                modifiers: KeyModifiers::NONE,
            })),
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
        self.frames_render_as(terminal_id, false).await;
    }

    /// Feeds frames for `terminal_id`; `alternate` marks the pane as on the
    /// alternate screen, as a full-screen program leaves it.
    async fn frames_render_as(&mut self, terminal_id: &str, alternate: bool) {
        let attachment_id = attachment_for(self.mock, terminal_id);
        let draws_before = self.draws.load(Ordering::SeqCst);
        for _ in 0..PROBE_FRAMES {
            self.fed += 1;
            let text = format!("tick-{}", self.fed);
            let payload = if alternate {
                encoded_alternate_frame(&text)
            } else {
                encoded_frame(&text)
            };
            self.mock
                .send_event_and_wait(json!({
                    "type": "terminal_frame",
                    "terminal_id": terminal_id,
                    "attachment_id": attachment_id,
                    "encoding": "bincode-b64",
                    "payload": payload,
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
    stall: Arc<Notify>,
    resume: Arc<Notify>,
    /// Notified once the client's direct attach completes (`Attached` written).
    attached: Arc<Notify>,
    task: tokio::task::JoinHandle<()>,
}

impl StallingHost {
    async fn start() -> Self {
        Self::start_gated(None).await
    }

    /// As `start`, but the host answers the client's hello only once
    /// `welcome` is notified, so the direct handshake stays open until then.
    async fn start_gated(welcome: Option<Arc<Notify>>) -> Self {
        let socket_dir = tempfile::tempdir().expect("direct socket dir");
        let socket_path = socket_dir.path().join("frames.sock");
        let listener =
            tokio::net::UnixListener::bind(&socket_path).expect("bind direct frame socket");
        let (received_tx, received) = mpsc::unbounded_channel();
        let stall = Arc::new(Notify::new());
        let stalled = Arc::clone(&stall);
        let resume = Arc::new(Notify::new());
        let resumed = Arc::clone(&resume);
        let attached = Arc::new(Notify::new());
        let attached_tx = Arc::clone(&attached);
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("direct client");
            let _: ClientMessage = read_message_async(&mut stream, MAX_FRAME_SIZE)
                .await
                .expect("direct hello");
            if let Some(welcome) = welcome {
                welcome.notified().await;
            }
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
                    host_attachment_id: 1,
                },
            )
            .await
            .expect("direct attached");
            attached_tx.notify_one();
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
            // Keep the socket open and unread until `resume`, then drain it.
            resumed.notified().await;
            while let Ok(message) = read_message_async(&mut stream, MAX_FRAME_SIZE).await {
                let _ = received_tx.send(message);
            }
        });
        Self {
            _socket_dir: socket_dir,
            socket_path,
            received,
            stall,
            resume,
            attached,
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
    pane_workspace(mock, &["terminal-a", "terminal-b"], direct_b, false, &[]).await
}

/// One tab of `terminals` side by side, the last one focused. A pane without
/// a host stream is tmux-backed, so its keys relay through the daemon, unless
/// it is listed in `native_on_proxy`: a native pane on the daemon's proxy
/// refuses its keys (B2), and only a test about that refusal lists it.
async fn pane_workspace(
    mock: &MockDaemon,
    terminals: &[&str],
    direct_b: Option<&StallingHost>,
    external: bool,
    native_on_proxy: &[&str],
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
            } else if !native_on_proxy.contains(terminal_id) {
                mock.set_attach_backend(terminal_id, "tmux");
                item["backend"] = json!("tmux");
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
    std::fs::write(home.path().join("bootstrap.yaml"), "api_key: local-token\n")
        .expect("write API key");
    let focused = terminals.last().expect("at least one terminal");
    mock.seed_workspace("project-1", &[(terminals, focused)]);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    (workspace, home)
}

const PROXY_NATIVE_INPUT: &str =
    "this terminal's input runs on its host stream; reattaching it, so the key was not sent";
/// The start of the toast, short enough to stay on one row of the frame.
const PROXY_NATIVE_SCREEN: &str = "this terminal's input runs";

/// How the daemon answers the `terminal_input` requests a keypress could queue
/// behind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DaemonReply {
    /// The daemon answers at once.
    Healthy,
    /// The daemon's reply is held until the first key is on the host, then released.
    Slowed,
    /// The daemon never answers.
    Absent,
}

/// The most a keystroke may take to reach the host stream in any daemon state.
/// A direct key took up to 0.73s with the full suite running in parallel, which
/// a bound of 0.5s flaked on. A key that waited on the daemon could not arrive
/// before the held reply, and the slowed daemon releases that reply only after
/// the first key is already on the host.
const KEY_BOUND: Duration = Duration::from_millis(1500);

/// Keystroke latency on a native direct pane with the daemon healthy, slowed
/// and absent. Its keys go straight to the host stream, so every key reaches
/// the host within `KEY_BOUND` in every state and none awaits the daemon
/// (memory b59e4ce9).
#[tokio::test]
async fn keystroke_latency_is_the_same_whether_the_daemon_is_healthy_slowed_or_absent() {
    for reply in [
        DaemonReply::Healthy,
        DaemonReply::Slowed,
        DaemonReply::Absent,
    ] {
        let mock = MockDaemon::start("local-token").await;
        let mut host = StallingHost::start().await;
        let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
        let (backend, draws, frames) = DrawRecorder::new(96, 30);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut chrome = Chrome::dark();
        let (input_tx, input_rx) = mpsc::channel(1);
        // The daemon holds the one request a keystroke could wrongly wait on:
        // the daemon-routed write. Keys must reach the host without it.
        let holds: Vec<Arc<Notify>> = if reply == DaemonReply::Healthy {
            Vec::new()
        } else {
            vec![mock.hold_ws("terminal_input", for_terminal("terminal-b"))]
        };

        let driver = async {
            let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
            // Type once terminal-b draws from its host stream, so the keys
            // measure the direct path rather than the attach in flight.
            wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
            let started = std::time::Instant::now();
            probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
            host.wait_for_input(b"x").await;
            let first = started.elapsed();
            // A slowed daemon answers only once the first key is already on the
            // host, so a key that had waited on the reply would never have arrived.
            if reply == DaemonReply::Slowed {
                for hold in &holds {
                    hold.notify_one();
                }
            }
            probe.draws_continue().await;
            let started = std::time::Instant::now();
            probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
            host.wait_for_input(b"y").await;
            let second = started.elapsed();
            drop(input_tx);
            (first, second)
        };

        let mut switch = TerminalGuard::recording().0;
        let (result, (first, second)) = tokio::join!(
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
        eprintln!("keystroke latency, daemon {reply:?}: first {first:?}, second {second:?}");
        assert!(
            first < KEY_BOUND,
            "the first key took {first:?} to reach the host with the daemon {reply:?}"
        );
        assert!(
            second < KEY_BOUND,
            "the second key took {second:?} to reach the host with the daemon {reply:?}"
        );
        assert!(
            written(&mock, "terminal-b").is_empty(),
            "no key rode the daemon with the daemon {reply:?}"
        );
        for hold in &holds {
            hold.notify_one();
        }
        mock.shutdown().await;
    }
}

/// Extra native panes beside the probed one, one entry per pane count the
/// keystroke-latency table covers.
const LATENCY_PANE_COUNTS: [usize; 4] = [0, 10, 50, 100];
/// Keys typed before measuring starts.
const LATENCY_WARMUP_KEYS: usize = 20;
/// Keys measured per pane count.
const LATENCY_SAMPLE_KEYS: usize = 200;
/// The terminal the latency table draws into, the same size for every count.
const LATENCY_COLS: u16 = 240;
const LATENCY_ROWS: u16 = 60;

/// One tab of native panes, each on its own direct host stream, the first one
/// focused. Like `pane_workspace`, but every pane carries its attach block and
/// has its locator served, so no pane is proxied.
async fn direct_panes_workspace(
    mock: &MockDaemon,
    panes: &[(&str, &StallingHost)],
) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    let items: Vec<Value> = panes
        .iter()
        .map(|(terminal_id, host)| {
            mock.serve_direct_attach(host.attach_locator(terminal_id));
            json!({
                "terminal_id": terminal_id,
                "backend": "native",
                "state": "live",
                "attach": host.roster_attach(terminal_id),
            })
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
    std::fs::write(home.path().join("bootstrap.yaml"), "api_key: local-token\n")
        .expect("write API key");
    let ids: Vec<&str> = panes.iter().map(|(terminal_id, _)| *terminal_id).collect();
    let focused = *ids.first().expect("at least one pane");
    mock.seed_balanced_workspace("project-1", &[(ids.as_slice(), focused)]);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    (workspace, home)
}

/// The budget for every pane's direct attach to complete. Setup only: no key
/// sample is taken while it runs.
const ATTACH_DEADLINE: Duration = Duration::from_secs(10);

/// Wait until every pane's direct attach completes, each host having written
/// `Attached`, the reply the client waits on before its pane goes live (#23076).
/// The loop attaches panes one after another, so the budget covers the whole
/// set, not each pane. Returns how long the set took.
async fn wait_for_all_attached(mock: &MockDaemon, hosts: &[StallingHost]) -> Duration {
    let started = std::time::Instant::now();
    for (index, host) in hosts.iter().enumerate() {
        timeout(
            ATTACH_DEADLINE.saturating_sub(started.elapsed()),
            host.attached.notified(),
        )
        .await
        .unwrap_or_else(|_| {
            panic!(
                "timed out waiting for pane {index} to attach directly: {} terminal_attach requests, {} panes resized",
                websocket_requests(mock, "terminal_attach").len(),
                resized_terminals(mock),
            )
        });
    }
    started.elapsed()
}

/// How many panes the loop sized: each laid-out pane gets a `terminal_resize`
/// naming its terminal, so this counts the distinct terminals resized.
fn resized_terminals(mock: &MockDaemon) -> usize {
    websocket_requests(mock, "terminal_resize")
        .iter()
        .filter_map(|body| body.get("terminal_id").and_then(Value::as_str))
        .collect::<std::collections::BTreeSet<_>>()
        .len()
}

/// The nearest-rank `percent` of `sorted` (ascending, non-empty): the sample at
/// rank `ceil(percent / 100 * len)`, counted from one.
fn nearest_rank(sorted: &[Duration], percent: usize) -> Duration {
    let rank = (percent * sorted.len()).div_ceil(100).max(1);
    sorted[rank - 1]
}

/// One row of the keystroke-latency table; durations print in microseconds.
struct LatencyRow {
    panes: usize,
    attached: usize,
    laid_out: usize,
    attach: Duration,
    samples: usize,
    p50: Duration,
    p99: Duration,
    max: Duration,
}

/// Keystroke latency by pane count (validation criterion 4 of task #25653).
/// The probed native pane is one of N+1 native panes in one tab, each on its
/// own host stream. Every key rides the probe's direct stream, so a sample is
/// the loop's per-key work plus one host hop, with no daemon round trip on the
/// path (memory b59e4ce9). Prints one row per pane count, then the table.
#[tokio::test]
async fn keystroke_latency_by_pane_count() {
    let mut table = Vec::new();
    for extra in LATENCY_PANE_COUNTS {
        let mock = MockDaemon::start("local-token").await;
        // The probe is first: the largest pane in the seeded chain, and focused.
        let mut ids: Vec<String> = vec!["terminal-probe".to_string()];
        ids.extend((0..extra).map(|index| format!("terminal-extra-{index}")));
        let mut hosts = Vec::with_capacity(ids.len());
        for _ in &ids {
            hosts.push(StallingHost::start().await);
        }
        let panes: Vec<(&str, &StallingHost)> =
            ids.iter().map(String::as_str).zip(hosts.iter()).collect();
        let (mut workspace, _home) = direct_panes_workspace(&mock, &panes).await;
        let (backend, draws, frames) = DrawRecorder::new(LATENCY_COLS, LATENCY_ROWS);
        let mut terminal = Terminal::new(backend).expect("test terminal");
        let mut chrome = Chrome::dark();
        let (input_tx, input_rx) = mpsc::channel(1);

        let driver = async {
            let attach = wait_for_all_attached(&mock, &hosts).await;
            let probe_host = hosts.first_mut().expect("the probe host");
            let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
            let mut samples = Vec::with_capacity(LATENCY_SAMPLE_KEYS);
            for index in 0..LATENCY_WARMUP_KEYS + LATENCY_SAMPLE_KEYS {
                let started = std::time::Instant::now();
                probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
                probe_host.wait_for_input(b"x").await;
                let elapsed = started.elapsed();
                if index >= LATENCY_WARMUP_KEYS {
                    samples.push(elapsed);
                }
            }
            drop(input_tx);
            (attach, samples)
        };

        let mut switch = TerminalGuard::recording().0;
        let (result, (attach, samples)) = tokio::join!(
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
        let laid_out = resized_terminals(&mock);
        assert!(
            written(&mock, "terminal-probe").is_empty(),
            "no key rode the daemon at {extra} extra panes"
        );
        for (index, sample) in samples.iter().enumerate() {
            assert!(
                *sample < KEY_BOUND,
                "key {index} took {sample:?} to reach the host at {extra} extra panes"
            );
        }

        let mut sorted = samples;
        sorted.sort();
        let row = LatencyRow {
            panes: extra,
            attached: ids.len(),
            laid_out,
            attach,
            samples: sorted.len(),
            p50: nearest_rank(&sorted, 50),
            p99: nearest_rank(&sorted, 99),
            max: *sorted.last().expect("measured samples"),
        };
        eprintln!(
            "keystroke latency panes={} attached={} laid_out={} attach_ms={} samples={} p50={} p99={} max={}",
            row.panes,
            row.attached,
            row.laid_out,
            row.attach.as_millis(),
            row.samples,
            row.p50.as_micros(),
            row.p99.as_micros(),
            row.max.as_micros()
        );
        table.push(row);
        mock.shutdown().await;
    }
    eprintln!("keystroke latency by pane count (us; attach_ms in ms):");
    eprintln!(
        "{:>6} {:>9} {:>10} {:>10} {:>8} {:>8} {:>8} {:>8}",
        "panes", "attached", "laid_out", "attach_ms", "samples", "p50", "p99", "max"
    );
    for row in &table {
        eprintln!(
            "{:>6} {:>9} {:>10} {:>10} {:>8} {:>8} {:>8} {:>8}",
            row.panes,
            row.attached,
            row.laid_out,
            row.attach.as_millis(),
            row.samples,
            row.p50.as_micros(),
            row.p99.as_micros(),
            row.max.as_micros()
        );
    }
}

/// A native pane on the daemon proxy refuses its key on the stream and
/// relays nothing through the daemon.
#[tokio::test]
async fn native_pane_on_the_daemon_proxy_refuses_keys_on_the_stream() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = pane_workspace(
        &mock,
        &["terminal-a", "terminal-b"],
        None,
        false,
        &["terminal-a", "terminal-b"],
    )
    .await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        // The focused pane's proxy attach has to land before its key can meet it.
        wait_until("terminal-b's proxy attach", || {
            latest_claim(&mock, "terminal-b").is_some()
        })
        .await;
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        probe.shows(PROXY_NATIVE_SCREEN).await;
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
        websocket_requests(&mock, "terminal_input").is_empty(),
        "no per-key daemon write for a native pane on the proxy"
    );
    assert_eq!(
        alerts(&chrome, PROXY_NATIVE_INPUT),
        1,
        "the refused key says why, once"
    );
    mock.shutdown().await;
}

/// A wheel over a full-screen program sends arrow keys. On a native pane on
/// the daemon proxy those arrows are refused on the stream like any key
/// (memory b59e4ce9: keystrokes never depend on the daemon), not relayed per
/// arrow through `terminal_input`.
#[tokio::test]
async fn an_alternate_screen_wheel_on_a_native_pane_on_the_proxy_is_refused() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) =
        pane_workspace(&mock, &["terminal-a"], None, false, &["terminal-a"]).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("roster before the layout");
    let pane = workspace
        .pane_for_terminal("terminal-a")
        .expect("terminal-a pane");
    // Lay the chrome out the way the loop will (one pane), so the wheel lands
    // on that pane's rectangle.
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, workspace.pane(pane).display_name());
    chrome.compute_view(&workspace, Rect::new(0, 0, 96, 30));
    let inner = chrome.view.pane_infos[0].inner_rect;
    let (column, row) = (inner.x + 1, inner.y + 1);
    assert!(
        matches!(hit_test(&chrome.view, column, row), Hit::Pane { .. }),
        "the wheel lands on the pane"
    );
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(4);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        // The pane's viewport claim has to land before its frames can be attributed.
        wait_until("terminal-a's viewport claim", || {
            latest_claim(&mock, "terminal-a").is_some()
        })
        .await;
        probe.frames_render_as("terminal-a", true).await;
        probe.mouse(MouseEventKind::ScrollUp, column, row).await;
        probe.shows(PROXY_NATIVE_SCREEN).await;
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
        websocket_requests(&mock, "terminal_input").is_empty(),
        "the wheel's arrows never relay per key through the daemon: {:?}",
        websocket_requests(&mock, "terminal_input")
    );
    assert_eq!(
        alerts(&chrome, PROXY_NATIVE_INPUT),
        1,
        "the refused wheel says why, once"
    );
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

const WORKSPACE_ID: &str = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const DAEMON_EPOCH: &str = "00000000-0000-4000-8000-000000000000";

fn roster_items(ids: &[&str]) -> Value {
    roster_items_on("native", ids)
}

/// [`roster_items`] with every row on `backend`. The roster's row is what a
/// refresh sets a pane's backend from, so a fixture whose attach is tmux
/// reports tmux here too.
fn roster_items_on(backend: &str, ids: &[&str]) -> Value {
    let items: Vec<Value> = ids
        .iter()
        .map(|id| json!({"terminal_id": id, "backend": backend, "state": "live"}))
        .collect();
    json!({
        "items": items,
        "next_cursor": null,
        "snapshot": {"daemon_epoch": "epoch-1", "seq": 1}
    })
}

#[tokio::test]
async fn a_roster_for_a_project_no_longer_focused_is_dropped() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial workspace");
    mock.seed_other_workspace("project-2", &[(&["terminal-late"], "terminal-late")]);
    let release = mock.enqueue_held(
        "GET",
        "/api/terminals?project_id=project-2",
        200,
        roster_items(&["terminal-late"]),
    );
    let mut chrome = Chrome::dark();
    let selected = timeout(
        LIVENESS_DEADLINE,
        gobby_client::app::focus_project(&mut workspace, &mut chrome, "project-2"),
    )
    .await;
    assert!(
        selected.is_ok(),
        "selecting a project must not await its held roster"
    );
    selected.unwrap().expect("select second project");
    assert_eq!(workspace.project_id(), Some("project-2"));

    mock.enqueue(
        "GET",
        "/api/terminals?project_id=project-1",
        200,
        roster_items_on("tmux", &["terminal-a", "terminal-b"]),
    );
    timeout(
        LIVENESS_DEADLINE,
        gobby_client::app::focus_project(&mut workspace, &mut chrome, "project-1"),
    )
    .await
    .expect("switch back while the old roster is held")
    .expect("select first project");
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        wait_until("the held project roster", || {
            mock.requests().iter().any(|request| {
                request
                    .target
                    .starts_with("/api/terminals?project_id=project-2")
            })
        })
        .await;
        release.notify_one();
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        probe.key_reaches("terminal-b", 'y').await;
        probe.frames_render("terminal-a").await;
        probe.key_reaches("terminal-b", 'x').await;
        probe.frames_render("terminal-b").await;
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
    assert_eq!(workspace.project_id(), Some("project-1"));
    assert!(
        !workspace
            .roster_terminal_ids()
            .iter()
            .any(|id| id == "terminal-late"),
        "the late project's roster must not replace the selected project's roster"
    );
    assert!(
        attach_requests(&mock, "terminal-late").is_empty(),
        "discarded roster rows must never attach"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn rest_failures_and_lagged_events_warn_with_elapsed_time() {
    let (_guard, logs) = capture_logs();
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial workspace");
    mock.enqueue(
        "GET",
        "/api/attention/roster",
        503,
        json!({"error": "roster unavailable"}),
    );
    mock.enqueue(
        "GET",
        "/api/sessions?",
        503,
        json!({"error": "sessions unavailable"}),
    );
    mock.enqueue(
        "GET",
        "/api/projects",
        200,
        json!([{"id": "surviving-project", "name": "Surviving project"}]),
    );
    workspace.queue_initial_sidebar_fetch();
    let fetch = workspace
        .start_sidebar_refetch()
        .expect("queued fetch")
        .await
        .expect("successful components survive partial failures");
    workspace.apply_sidebar_fetch(fetch);
    assert!(
        workspace
            .sidebar()
            .projects
            .iter()
            .any(|project| project.project_id == "surviving-project"),
        "successful project rows still apply"
    );
    let rest_warnings: Vec<_> = records(&logs, "WARN")
        .into_iter()
        .filter(|line| line.contains("operation="))
        .collect();
    assert_eq!(
        rest_warnings.len(),
        2,
        "one WARN per failed REST component: {rest_warnings:?}"
    );
    for operation in ["attention roster", "sessions"] {
        let warning = rest_warnings
            .iter()
            .find(|line| line.contains(operation))
            .unwrap_or_else(|| panic!("missing WARN for {operation}: {rest_warnings:?}"));
        for field in ["generation=", "error_class=", "elapsed_ms="] {
            assert!(warning.contains(field), "missing {field}: {warning}");
        }
        if operation == "sessions" {
            assert!(warning.contains("project-1"), "project context: {warning}");
        }
    }

    assert_eq!(assert_lagged_warning(&mock, &mut workspace, &logs).await, 1);
    mock.shutdown().await;
}

#[tokio::test]
async fn a_periodic_roster_that_beats_the_scoped_roster_focuses_the_selected_pane() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial workspace");
    // The periodic refresh must become due before the scoped job's
    // five-second HTTP deadline. Age only the previous refresh's clock.
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(3)).await;
    tokio::time::resume();
    mock.seed_other_workspace("project-2", &[(&["terminal-new"], "terminal-new")]);
    let project_roster = "/api/terminals?project_id=project-2";
    let release = mock.enqueue_held("GET", project_roster, 200, roster_items(&["terminal-new"]));
    mock.enqueue("GET", project_roster, 200, roster_items(&["terminal-new"]));
    let mut chrome = Chrome::dark();
    gobby_client::app::focus_project(&mut workspace, &mut chrome, "project-2")
        .await
        .expect("select second project");
    let (backend, _, _) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(16);
    let mut automatic_focus = false;
    let driver = async {
        // The scoped job owns the first held request. Only the periodic
        // relist can answer while that request remains outstanding.
        wait_within(DRAIN_DEADLINE, "the periodic project roster", || {
            mock.requests()
                .iter()
                .filter(|request| request.target.starts_with(project_roster))
                .count()
                == 2
        })
        .await;
        automatic_focus = timeout(LIVENESS_DEADLINE, async {
            while focused_attachment(&mock, "terminal-new").is_none() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .is_ok();
        release.notify_one();
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
        workspace
            .roster_terminal_ids()
            .iter()
            .any(|id| id == "terminal-new"),
        "the periodic roster installed the newly selected project's terminal"
    );
    assert!(
        automatic_focus,
        "an accepted periodic roster must focus the selected pane's terminal while the scoped roster is held"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn roster_and_attention_job_failures_warn_with_generation_class_and_elapsed_time() {
    let (_guard, logs) = capture_logs();
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial workspace");
    let (backend, _, _) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);
    let job_warnings = || {
        records(&logs, "WARN")
            .into_iter()
            .filter(|line| line.contains("refresh failed") && line.contains("operation="))
            .collect::<Vec<_>>()
    };
    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        mock.enqueue(
            "GET",
            "/api/terminals?project_id=project-1",
            503,
            json!({"error": "roster job unavailable"}),
        );
        mock.enqueue(
            "GET",
            "/api/attention/roster",
            503,
            json!({"error": "attention job unavailable"}),
        );
        // Epoch changes request the actual coalesced jobs, rather than the
        // sidebar aggregate fetch that has separate failure diagnostics.
        mock.send_event_and_wait(json!({"type": "terminal_event", "event": "noop",
            "terminal_id": "terminal-a", "daemon_epoch": "different-epoch", "seq": 2,
            "timestamp": "2026-01-01T00:00:00Z"}))
            .await;
        mock.send_event_and_wait(json!({"type": "agent_event",
            "event": "attention_metadata_changed", "epoch": "attention-2", "seq": 1,
            "metadata": {"entry_id": "run:changed"}}))
            .await;
        wait_until("both job failure WARNs", || job_warnings().len() == 2).await;
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
    let warnings = job_warnings();
    assert_eq!(
        warnings.len(),
        2,
        "one failure WARN from each job: {warnings:?}"
    );
    let mut missing = Vec::new();
    for operation in ["operation=\"roster\"", "operation=\"attention roster\""] {
        let warning = warnings
            .iter()
            .find(|line| line.contains(operation))
            .unwrap_or_else(|| panic!("missing {operation}: {warnings:?}"));
        for field in ["generation=", "error_class=", "elapsed_ms="] {
            if !warning.contains(field) {
                missing.push(format!("{operation}: missing {field}"));
            }
        }
    }
    assert!(
        missing.is_empty(),
        "job WARN metadata missing: {missing:?}; records: {warnings:?}"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn receiver_overflow_warns_with_elapsed_time() {
    let (_guard, logs) = capture_logs();
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial workspace");
    assert_eq!(assert_lagged_warning(&mock, &mut workspace, &logs).await, 1);
    mock.shutdown().await;
}

async fn assert_lagged_warning(
    mock: &MockDaemon,
    workspace: &mut Workspace<LiveDaemon>,
    logs: &Arc<Mutex<Vec<u8>>>,
) -> usize {
    // Keep the workspace receiver idle until the actual daemon reader has
    // published more events than its bounded broadcast channel can hold.
    let (_, mut monitor) = workspace.daemon().subscribe();
    let burst = async {
        for seq in 1..=2049 {
            mock.send_event(json!({"type": "terminal_event", "event": "noop",
                "terminal_id": "terminal-a", "daemon_epoch": "epoch-1", "seq": seq,
                "timestamp": "2026-01-01T00:00:00Z"}));
        }
        loop {
            match monitor.recv().await {
                Ok(gobby_client::daemon::DaemonEvent::Terminal { seq: 2049, .. }) => break,
                Ok(_) | Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {}
                Err(error) => panic!("monitor closed before overflow: {error}"),
            }
        }
    };
    timeout(DRAIN_DEADLINE, burst)
        .await
        .expect("daemon reader published overflow");
    // Lag repair must retain the panes the liveness probe exercises.
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_items(&["terminal-a", "terminal-b"]),
        );
    }
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);
    let driver = async {
        let mut probe = LivenessProbe::new(mock, &input_tx, draws, frames);
        // The lag retires the first two attachments; frames sent before both
        // panes claim their fresh ones name a dead attachment and are dropped.
        wait_until("both panes reattached after the lag", || {
            let attachments = claimed_attachments(mock);
            attachments.len() >= 4
                && ["terminal-a", "terminal-b"].iter().all(|terminal| {
                    latest_claim(mock, terminal).is_some_and(|id| attachments[2..].contains(&id))
                })
        })
        .await;
        probe.key_reaches("terminal-b", 'x').await;
        probe.frames_render("terminal-a").await;
        probe.key_reaches("terminal-b", 'z').await;
        drop(input_tx);
    };
    let mut switch = TerminalGuard::recording().0;
    let (result, ()) = tokio::join!(
        run_live_loop(workspace, &mut terminal, &mut chrome, input_rx, &mut switch),
        driver
    );
    result.expect("live loop recovers from lag");
    let lagged: Vec<_> = records(logs, "WARN")
        .into_iter()
        .filter(|line| line.contains("source=") && line.contains("lag"))
        .collect();
    assert_eq!(
        lagged.len(),
        1,
        "one WARN for the overflow episode: {lagged:?}"
    );
    for field in ["source=", "generation=", "elapsed_ms="] {
        assert!(lagged[0].contains(field), "missing {field}: {}", lagged[0]);
    }
    lagged.len()
}

fn pane_added_event(tab: &str, first: &str, added: &str, terminal: &str, seq: u64) -> Value {
    json!({
        "type": "workspace_event",
        "kind": "pane.added",
        "workspace_id": WORKSPACE_ID,
        "project_id": "project-1",
        "workspace": null,
        "tabs": [{
            "id": tab,
            "workspace_id": WORKSPACE_ID,
            "ref": 1,
            "title": null,
            "project_id": "project-1",
            "worktree_id": null,
            "position": 0,
            "focused_pane_id": first,
            "layout": {
                "kind": "split",
                "axis": "horizontal",
                "ratio": 0.5,
                "children": [
                    {"kind": "pane", "pane_id": first},
                    {"kind": "pane", "pane_id": added},
                ],
            },
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "panes": [{
            "id": added,
            "tab_id": tab,
            "ref": 2,
            "terminal_id": terminal,
            "owns_terminal": true,
            "label": null,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "daemon_epoch": DAEMON_EPOCH,
        "seq": seq,
        "timestamp": "2026-01-01T00:00:00+00:00",
    })
}

fn pane_removed_event(tab: &str, first: &str, removed: &str, terminal: &str, seq: u64) -> Value {
    json!({
        "type": "workspace_event",
        "kind": "pane.removed",
        "workspace_id": WORKSPACE_ID,
        "project_id": "project-1",
        "workspace": null,
        "tabs": [{
            "id": tab,
            "workspace_id": WORKSPACE_ID,
            "ref": 1,
            "title": null,
            "project_id": "project-1",
            "worktree_id": null,
            "position": 0,
            "focused_pane_id": first,
            "layout": {"kind": "pane", "pane_id": first},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "panes": [{
            "id": removed,
            "tab_id": tab,
            "ref": 2,
            "terminal_id": terminal,
            "owns_terminal": true,
            "label": null,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "daemon_epoch": DAEMON_EPOCH,
        "seq": seq,
        "timestamp": "2026-01-01T00:00:00+00:00",
    })
}

fn pane_renamed_event(
    tab: &str,
    first: &str,
    pane: &str,
    terminal: &str,
    label: &str,
    seq: u64,
) -> Value {
    let mut event = pane_added_event(tab, first, pane, terminal, seq);
    event["kind"] = json!("pane.renamed");
    event["tabs"] = json!([]);
    event["panes"][0]["label"] = json!(label);
    event
}

fn terminal_row_requests(mock: &MockDaemon, terminal_id: &str) -> usize {
    let target = format!("/api/terminals/{terminal_id}");
    mock.requests()
        .iter()
        .filter(|request| request.method == "GET" && request.target == target)
        .count()
}

/// A live workspace whose one pane shows `terminal-a`. Returns the workspace,
/// the daemon tab and that pane, so a test can add a pane for an unserved
/// terminal to the tab.
async fn on_demand_workspace(mock: &MockDaemon) -> (Workspace<LiveDaemon>, String, String) {
    // Keys to terminal-a relay through the daemon, so it is tmux-backed (see
    // `pane_workspace`); a native pane on the proxy refuses them (B2).
    mock.set_attach_backend("terminal-a", "tmux");
    for _ in 0..2 {
        mock.enqueue(
            "GET",
            "/api/terminals?",
            200,
            roster_items_on("tmux", &["terminal-a"]),
        );
    }
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let seeded = mock.seed_workspace("project-1", &[(&["terminal-a"], "terminal-a")]);
    let (tab_id, panes) = seeded[0].clone();
    workspace.select_project("project-1");
    (workspace, tab_id, panes[0].clone())
}

fn warnings_for(logs: &Arc<Mutex<Vec<u8>>>, terminal_id: &str) -> Vec<String> {
    let text = String::from_utf8(logs.lock().expect("captured logs").clone()).expect("UTF-8 logs");
    text.lines()
        .filter(|line| line.contains("WARN") && line.contains(terminal_id))
        .map(str::to_owned)
        .collect()
}

#[tokio::test]
async fn a_held_open_unresolved_terminal_job_is_coalesced_and_nonblocking() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, tab_id, first) = on_demand_workspace(&mock).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        let release = mock.enqueue_held(
            "GET",
            "/api/terminals/terminal-late",
            200,
            json!({"terminal_id": "terminal-late", "backend": "native", "state": "live"}),
        );
        mock.send_event_and_wait(pane_added_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            1,
        ))
        .await;
        wait_until("the held row request", || {
            terminal_row_requests(&mock, "terminal-late") == 1
        })
        .await;
        // The held read leaves frames, ticks and typing running.
        probe.assert_live("terminal-a", "terminal-a", 'k').await;
        // A repeat event joins the held open instead of reading the row again.
        mock.send_event_and_wait(pane_renamed_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            "late",
            2,
        ))
        .await;
        probe.assert_live("terminal-a", "terminal-a", 'm').await;
        assert_eq!(
            terminal_row_requests(&mock, "terminal-late"),
            1,
            "the repeat event coalesces onto the held read"
        );
        release.notify_one();
        wait_until("the released terminal attaches", || {
            websocket_requests(&mock, "terminal_attach")
                .iter()
                .any(|request| request.get("terminal_id") == Some(&json!("terminal-late")))
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
        terminal_row_requests(&mock, "terminal-late"),
        1,
        "one read opens the terminal"
    );
    assert!(
        workspace.pane_for_terminal("terminal-late").is_some(),
        "the released read installs the terminal's pane"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn an_unresolved_terminal_open_failure_is_logged_with_elapsed_time() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, tab_id, first) = on_demand_workspace(&mock).await;
    let (backend, _draws, _frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);
    let (_guard, logs) = capture_logs();

    let driver = async {
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        // No row is served for the terminal, so its open fails.
        mock.send_event_and_wait(pane_added_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            1,
        ))
        .await;
        wait_until("the failed row request", || {
            terminal_row_requests(&mock, "terminal-late") == 1
        })
        .await;
        let _ = tokio::time::timeout(LIVENESS_DEADLINE, async {
            while warnings_for(&logs, "terminal-late").is_empty() {
                tokio::task::yield_now().await;
            }
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
    let warned = warnings_for(&logs, "terminal-late");
    assert_eq!(warned.len(), 1, "one WARN per failed open: {warned:?}");
    for field in [
        "terminal_id=terminal-late",
        "generation=",
        "error_class=",
        "elapsed_ms=",
    ] {
        assert!(
            warned[0].contains(field),
            "the WARN carries `{field}`: {}",
            warned[0]
        );
    }
    assert_eq!(
        terminal_row_requests(&mock, "terminal-late"),
        1,
        "a failed open is not retried"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_read_that_outlives_its_pane_never_installs_or_attaches_the_terminal() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let (mut workspace, tab_id, first) = on_demand_workspace(&mock).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        let release = mock.enqueue_held(
            "GET",
            "/api/terminals/terminal-late",
            200,
            json!({"terminal_id": "terminal-late", "backend": "native", "state": "live"}),
        );
        mock.send_event_and_wait(pane_added_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            1,
        ))
        .await;
        wait_until("the held row request", || {
            terminal_row_requests(&mock, "terminal-late") == 1
        })
        .await;
        // The pane closes while its read is still held.
        mock.send_event_and_wait(pane_removed_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            2,
        ))
        .await;
        release.notify_one();
        // The released read lands after its pane is gone; frames keep running.
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
    assert!(
        workspace.pane_for_terminal("terminal-late").is_none(),
        "a read whose pane closed installs no pane"
    );
    assert!(
        websocket_requests(&mock, "terminal_attach")
            .iter()
            .all(|request| request.get("terminal_id") != Some(&json!("terminal-late"))),
        "nothing attaches a terminal its layout no longer places"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn removal_with_an_attach_in_flight_does_not_resurrect_the_opened_pane() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let (mut workspace, tab_id, first) = on_demand_workspace(&mock).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(16);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 1).await;
        let attach = mock.hold_ws("terminal_attach", for_terminal("terminal-late"));
        mock.enqueue(
            "GET",
            "/api/terminals/terminal-late",
            200,
            json!({"terminal_id": "terminal-late", "backend": "native", "state": "live"}),
        );
        mock.send_event_and_wait(pane_added_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            1,
        ))
        .await;
        // Only start_due_attaches can send this: apply_open installed PaneId(2)
        // and the first tick started its attach, whose reply remains held.
        wait_until("the opened terminal's held attach", || {
            websocket_requests(&mock, "terminal_attach")
                .iter()
                .any(|request| request["terminal_id"] == "terminal-late")
        })
        .await;
        mock.send_event_and_wait(pane_removed_event(
            &tab_id,
            &first,
            "mock-pane-late",
            "terminal-late",
            2,
        ))
        .await;
        // Frame processing follows queued daemon events in the biased loop.
        // This proves the removal applied before the held reply is released.
        probe.frames_render("terminal-a").await;
        let replies_before_release = mock.replies("terminal_attach");
        attach.notify_one();
        wait_for_replies(&mock, "terminal_attach", replies_before_release + 1).await;
        // Drive recovery completion and later ticks; the surviving pane must
        // still render frames and deliver input after the stale reply lands.
        probe.assert_live("terminal-a", "terminal-a", 'k').await;
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
    result.expect("stale attach completion must not panic or stop the loop");
    assert!(
        websocket_requests(&mock, "terminal_focus")
            .iter()
            .all(|request| {
                request["terminal_id"] != "terminal-late" || request["focused"] != true
            }),
        "the removed pane must never take focus"
    );
    assert!(
        workspace.pane_for_terminal("terminal-late").is_none(),
        "the stale attach must not resurrect the removed live pane"
    );
    mock.shutdown().await;
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
        probe.key_reaches("terminal-b", 'x').await;

        // The daemon holds terminal-b's write. A key typed behind it is sent
        // without waiting for that reply, and the loop keeps drawing both
        // panes meanwhile.
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        wait_within(KEY_BOUND, "the key typed behind the held write", || {
            written(&mock, "terminal-b") == ["x", "y", "z"]
        })
        .await;
        probe.frames_render("terminal-a").await;
        probe.frames_render("terminal-b").await;

        release.notify_one();
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
async fn a_lost_write_reply_does_not_stop_the_keys_typed_after_it() {
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
        probe.key_reaches("terminal-b", 'x').await;

        // The daemon receives terminal-b's write and never answers it. Once
        // the request deadline passes, the write is unconfirmed and the key
        // typed after that still goes out.
        mock.suppress_ws("terminal_input");
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the unanswered write", || {
            written(&mock, "terminal-b").len() == 2
        })
        .await;
        tokio::time::sleep(gobby_client::daemon::REQUEST_DEADLINE + Duration::from_millis(500))
            .await;
        mock.allow_ws("terminal_input");
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
    let pane_b = workspace
        .pane_for_terminal("terminal-b")
        .expect("terminal-b pane");
    assert!(
        workspace.pane(pane_b).writable(),
        "a lost reply leaves the pane writable"
    );
    assert_eq!(written(&mock, "terminal-b"), ["x", "y", "k"]);
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
    // terminal-a only shows frames here, and a native frame sits at its pane's
    // origin, clear of the centred attention prompt; terminal-b takes the keys,
    // so it relays through the daemon.
    let (mut workspace, _home) = pane_workspace(
        &mock,
        &["terminal-a", "terminal-b"],
        None,
        false,
        &["terminal-a"],
    )
    .await;
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
        probe.frames_render("terminal-a").await;

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
    // The backlog holds 256 unsettled writes. x, y and the first paste are
    // held, so 256 - 3 keys fit and the rest are dropped.
    const ACCEPTED_KEYS: usize = 256 - 3;
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
        // 'x' and 'y' are held in flight, so each stays unsettled.
        let x_release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key_reaches("terminal-b", 'x').await;
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        // Acknowledge each accepted key by its request, rather than requiring
        // 253 serialized WebSocket round trips to finish in ten wall-clock seconds.
        let accepted_keys: Vec<_> = (0..ACCEPTED_KEYS)
            .map(|_| {
                let received = Arc::new(Notify::new());
                let signal = Arc::clone(&received);
                let release = mock.hold_ws("terminal_input", move |request| {
                    let matches = for_terminal("terminal-b")(request);
                    if matches {
                        signal.notify_one();
                    }
                    matches
                });
                (received, release)
            })
            .collect();
        let other_received = Arc::new(Notify::new());
        let signal = Arc::clone(&other_received);
        let other_release = mock.hold_ws("terminal_input", move |request| {
            let matches = for_terminal("terminal-a")(request);
            if matches {
                signal.notify_one();
            }
            matches
        });
        // The first paste stays in flight too, so it holds a backlog slot.
        let paste_release = mock.hold_ws("terminal_paste", for_terminal("terminal-b"));
        // The second paste would take the backlog past 1 MiB.
        probe.paste("p".repeat(PASTE)).await;
        probe.paste("q".repeat(PASTE)).await;
        for key in &keys {
            let key = key.chars().next().expect("one char");
            probe.key(KeyCode::Char(key), KeyModifiers::NONE).await;
        }
        probe.frames_render("terminal-b").await;
        // Keys past the bound are dropped; the accepted ones are all on the
        // wire before any release.
        wait_until("the accepted keys reach the wire", || {
            written(&mock, "terminal-b").len() == 2 + ACCEPTED_KEYS
        })
        .await;

        // The other pane types and draws while terminal-b is held: each pane
        // writes on its own queue, so terminal-b's flood never blocks it.
        probe.next_pane().await;
        probe.key(KeyCode::Char('k'), KeyModifiers::NONE).await;
        probe.frames_render("terminal-a").await;
        probe.frames_render("terminal-b").await;
        assert_eq!(
            written(&mock, "terminal-a"),
            ["k"],
            "terminal-a's key is not queued behind terminal-b's held flood"
        );

        x_release.notify_one();
        release.notify_one();
        paste_release.notify_one();
        for (received, release) in accepted_keys {
            received.notified().await;
            release.notify_one();
        }
        other_received.notified().await;
        other_release.notify_one();
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
    accepted.extend(keys[..ACCEPTED_KEYS].iter().cloned());
    assert_eq!(
        written(&mock, "terminal-b"),
        accepted,
        "accepted keys arrive in typed order and only the newest are dropped"
    );
    assert_eq!(written(&mock, "terminal-a"), ["k"]);
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

/// Input, kill and close requests in the order the mock read them, as
/// `"<verb> <terminal> [data]"`.
fn control_wire(mock: &MockDaemon) -> Vec<String> {
    mock.requests()
        .iter()
        .filter(|request| request.method == "WS")
        .filter_map(|request| request.body.as_ref())
        .filter_map(|body| {
            let verb = match body.get("type").and_then(Value::as_str)? {
                "terminal_input" => "input",
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
        mock.enqueue_write_outcome("refused", Some("write_seq_conflict"));
        probe.key_reaches("terminal-b", 'r').await;
        probe.key_reaches("terminal-b", 'x').await;

        let _held = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 3).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('w'), KeyModifiers::NONE).await;
        wait_until("z and w reach the wire", || {
            written(&mock, "terminal-b").len() == 5
        })
        .await;
        let old_attachment = focused_attachment(&mock, "terminal-b");
        mock.drop_websockets();
        wait_until("the reconnect", || mock.websocket_handshakes() >= 2).await;
        wait_until("both panes reattached", || {
            websocket_requests(&mock, "terminal_attach").len() >= 4
        })
        .await;
        probe.draws_continue().await;

        probe.next_pane().await;
        probe.next_pane().await;
        wait_until("terminal-b focused on its new attachment", || {
            focused_attachment(&mock, "terminal-b")
                .is_some_and(|id| Some(&id) != old_attachment.as_ref())
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
    let (workspace, home) = pane_workspace(
        mock,
        &["terminal-a", "terminal-b"],
        None,
        external,
        &["terminal-a", "terminal-b"],
    )
    .await;
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
    // z and w left the wire before the change, under y's attachment; the
    // change never replays them.
    for data in ["z", "w"] {
        let sent: Vec<_> = writes.iter().filter(|(sent, _)| sent == data).collect();
        assert_eq!(
            sent.len(),
            1,
            "{data} is sent once, never replayed: {writes:?}"
        );
        assert_eq!(
            sent[0].1,
            attachment("y"),
            "{data} went out under the attachment it was typed for"
        );
    }
}

/// Holds b's write 'y' with 'z' and 'w' queued behind it, then closes b
/// with the prefix and `close`.
#[tokio::test]
async fn a_refused_write_leaves_the_daemon_routed_pane_writable_and_direct_keys_prompt() {
    let mock = MockDaemon::start("local-token").await;
    let mut host = StallingHost::start().await;
    // terminal-a is daemon-routed; terminal-b is native direct to its host.
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    // The daemon refuses x, and only when its reply is released.
    mock.enqueue_write_outcome("refused", Some("write_seq_conflict"));
    let refusal = mock.hold_ws("terminal_input", for_terminal("terminal-a"));

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        probe.next_pane().await;
        probe.key_reaches("terminal-a", 'x').await;
        // The next key is still written while x's refusal is pending.
        probe.key_reaches("terminal-a", 'y').await;
        probe.next_pane().await;
        let started = std::time::Instant::now();
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        host.wait_for_input(b"z").await;
        let direct = started.elapsed();
        refusal.notify_one();
        probe.next_pane().await;
        probe.key_reaches("terminal-a", 'w').await;
        drop(input_tx);
        direct
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, direct) = tokio::join!(
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
        direct < KEY_BOUND,
        "the direct key took {direct:?} to reach the host while the daemon refused a write"
    );
    assert_eq!(
        written(&mock, "terminal-a"),
        ["x", "y", "w"],
        "the pane stays writable after its refusal"
    );
    let titles: Vec<&str> = chrome
        .alert_log
        .iter()
        .map(|toast| toast.title.as_str())
        .collect();
    assert_eq!(
        titles,
        ["Input refused: write_seq_conflict."],
        "the refused write is reported"
    );
    mock.shutdown().await;
}

async fn close_behind_held_writes(close: (KeyCode, KeyModifiers)) -> Vec<String> {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home, mut chrome) = closing_workspace(&mock, false).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        probe.key_reaches("terminal-b", 'x').await;
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('w'), KeyModifiers::NONE).await;
        wait_until("z and w reach the wire", || {
            written(&mock, "terminal-b").len() == 4
        })
        .await;
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(close.0, close.1).await;
        probe.draws_continue().await;
        assert_eq!(
            wire_after_x(&mock),
            [
                "input terminal-b y",
                "input terminal-b z",
                "input terminal-b w",
            ],
            "b's writes leave before the kill, which waits for them to settle ({close:?})"
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

// ---- Attach and recovery beside the loop (plan A3) ----

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

/// The captured lines that carry the record `name`.
fn records(captured: &Arc<Mutex<Vec<u8>>>, name: &str) -> Vec<String> {
    let bytes = captured.lock().expect("captured logs").clone();
    String::from_utf8(bytes)
        .expect("UTF-8 logs")
        .lines()
        .filter(|line| line.contains(name))
        .map(str::to_string)
        .collect()
}

/// Asserts `line` carries the pane context every recovery record names.
fn assert_recovery_context(line: &str, terminal_id: &str, stage: &str, error_class: &str) {
    for field in [
        "pane_id=".to_string(),
        format!("terminal_id={terminal_id}"),
        "generation=".to_string(),
        format!("stage=\"{stage}\""),
        format!("error_class=\"{error_class}\""),
        "elapsed_ms=".to_string(),
    ] {
        assert!(line.contains(&field), "{field} missing from {line}");
    }
}

fn attach_requests(mock: &MockDaemon, terminal_id: &str) -> Vec<Value> {
    websocket_requests(mock, "terminal_attach")
        .into_iter()
        .filter(|request| request.get("terminal_id") == Some(&json!(terminal_id)))
        .collect()
}

fn detaches_of(mock: &MockDaemon, attachment_id: &str) -> usize {
    websocket_requests(mock, "terminal_detach")
        .iter()
        .filter(|request| request.get("attachment_id") == Some(&json!(attachment_id)))
        .count()
}

fn roster(mock: &MockDaemon, items: Vec<Value>) {
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
}

/// A3.2: a pane whose attach the daemon holds never holds up the pane
/// already attached beside it.
#[tokio::test]
async fn a_held_attach_on_one_pane_keeps_the_other_pane_rendering() {
    let mock = MockDaemon::start("local-token").await;
    let release = mock.hold_ws("terminal_attach", for_terminal("terminal-a"));
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let ticker_before = chrome.ticker;
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        // The focused pane's attach is not gated on the held one; wait for its claim.
        wait_until("terminal-b's viewport claim", || {
            latest_claim(&mock, "terminal-b").is_some()
        })
        .await;
        probe.assert_live("terminal-b", "terminal-b", 'x').await;
        probe.assert_live("terminal-b", "terminal-b", 'y').await;
        assert!(
            attach_requests(&mock, "terminal-a").len() == 1 && mock.replies("terminal_attach") == 1,
            "terminal-a's attach is still held while terminal-b streams"
        );
        release.notify_one();
        wait_until("terminal-a's viewport claim", || {
            websocket_requests(&mock, "terminal_set_viewport")
                .iter()
                .any(|request| request.get("terminal_id") == Some(&json!("terminal-a")))
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
    assert_eq!(written(&mock, "terminal-b"), ["x", "y"]);
    mock.shutdown().await;
}

/// Holds terminal-b's direct handshake open across a reconnect, then lets it
/// finish once the new connection has rolled the generation forward. Once
/// the loop logs dropping that result, the new connection comes up and
/// terminal-b attaches again. Returns the attachment the stale attach won.
async fn finish_a_direct_attach_after_a_reconnect(
    mock: &MockDaemon,
    logs: &Arc<Mutex<Vec<u8>>>,
) -> (Workspace<LiveDaemon>, String) {
    let welcome = Arc::new(Notify::new());
    let host = StallingHost::start_gated(Some(Arc::clone(&welcome))).await;
    let (mut workspace, _home) = two_pane_workspace(mock, Some(&host)).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        wait_until("terminal-b's direct attach", || {
            !attach_requests(mock, "terminal-b").is_empty()
        })
        .await;
        wait_for_replies(mock, "terminal_attach", 2).await;
        // The mock numbers attachments in request order.
        let index = websocket_requests(mock, "terminal_attach")
            .iter()
            .position(|request| request.get("terminal_id") == Some(&json!("terminal-b")))
            .expect("terminal-b attach");
        let stale = format!("attachment-{}", index + 1);
        let subscribes = websocket_requests(mock, "subscribe").len();
        let subscribed = mock.hold_ws("subscribe", |_| true);
        mock.drop_websockets();
        wait_within(DRAIN_DEADLINE, "the reconnect's subscribe", || {
            websocket_requests(mock, "subscribe").len() > subscribes
        })
        .await;
        welcome.notify_one();
        // terminal-a's proxy can give up a recovery of its own across the
        // same reconnect; only terminal-b's attach was won on the old one.
        wait_until("the stale attach result's record", || {
            records(logs, "frame_recovery_gave_up")
                .iter()
                .any(|line| line.contains("terminal_id=terminal-b"))
        })
        .await;
        subscribed.notify_one();
        wait_until("terminal-b's attach on the new connection", || {
            attach_requests(mock, "terminal-b").len() == 2
        })
        .await;
        drop(input_tx);
        stale
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, stale) = tokio::join!(
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
    (workspace, stale)
}

/// A3.3: an attach that lands after a reconnect is never installed, and the
/// pane attaches again on the new connection. The attachment it won died
/// with the old connection, which the daemon finalizes on close, so no
/// detach names it. A result superseded on the current connection still
/// holds a live attachment, so that one is detached exactly once.
#[tokio::test]
async fn an_attach_outcome_from_a_previous_generation_is_dropped() {
    let (_guard, logs) = capture_logs();
    let mock = MockDaemon::start("local-token").await;
    let (workspace, stale) = finish_a_direct_attach_after_a_reconnect(&mock, &logs).await;
    let pane_b = workspace.pane_for_terminal("terminal-b").expect("pane b");
    assert_ne!(workspace.pane(pane_b).attachment_id(), stale);
    assert_eq!(
        detaches_of(&mock, &stale),
        0,
        "the old connection's close finalized it"
    );
    let attaches = attach_requests(&mock, "terminal-b");
    assert_eq!(
        attaches.len(),
        2,
        "terminal-b attaches afresh: {attaches:?}"
    );
    mock.shutdown().await;

    let mock = MockDaemon::start("local-token").await;
    let superseded = supersede_an_attach_on_the_current_connection(&mock).await;
    assert_eq!(detaches_of(&mock, &superseded), 1, "detached exactly once");
    mock.shutdown().await;
}

/// Holds terminal-a's attach while its terminal exits, so the pane is gone
/// when the attach lands on the same connection. Returns the attachment that
/// attach won.
async fn supersede_an_attach_on_the_current_connection(mock: &MockDaemon) -> String {
    let release = mock.hold_ws("terminal_attach", for_terminal("terminal-a"));
    let (mut workspace, _home) = two_pane_workspace(mock, None).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        wait_until("terminal-a's attach", || {
            !attach_requests(mock, "terminal-a").is_empty()
        })
        .await;
        // The mock numbers attachments in request order.
        let index = websocket_requests(mock, "terminal_attach")
            .iter()
            .position(|request| request.get("terminal_id") == Some(&json!("terminal-a")))
            .expect("terminal-a attach");
        let superseded = format!("attachment-{}", index + 1);
        // Written ahead of the attach reply, the exit reaches the loop's
        // event branch, which it polls before recoveries.
        mock.send_event_and_wait(json!({
            "type": "terminal_event",
            "event": "exited",
            "terminal_id": "terminal-a",
            "daemon_epoch": "epoch-1",
            "seq": 2,
            "timestamp": "2026-01-01T00:00:00Z"
        }))
        .await;
        release.notify_one();
        wait_until("the superseded attachment's detach", || {
            detaches_of(mock, &superseded) == 1
        })
        .await;
        drop(input_tx);
        superseded
    };

    let mut switch = TerminalGuard::recording().0;
    let (result, superseded) = tokio::join!(
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
    assert!(workspace.pane_for_terminal("terminal-a").is_none());
    superseded
}

/// A3.4: a recovery's detach and attach run beside the loop, so keys keep
/// reaching the pane next to it while both are held.
#[tokio::test]
async fn recovery_detaches_then_attaches_while_input_still_routes() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home) = two_pane_workspace(&mock, None).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let mut probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        let old = attachment_for(&mock, "terminal-a");
        let detach = mock.hold_ws("terminal_detach", for_terminal("terminal-a"));
        let attach = mock.hold_ws("terminal_attach", for_terminal("terminal-a"));
        // A frame with no payload fails terminal-a's proxy source.
        mock.send_event_and_wait(json!({
            "type": "terminal_frame",
            "terminal_id": "terminal-a",
            "attachment_id": old,
            "encoding": "bincode-b64",
        }))
        .await;
        wait_for_websocket_requests(&mock, "terminal_detach", 1).await;
        probe.assert_live("terminal-b", "terminal-b", 'x').await;
        detach.notify_one();
        wait_until("the recovery's attach", || {
            attach_requests(&mock, "terminal-a").len() == 2
        })
        .await;
        probe.assert_live("terminal-b", "terminal-b", 'y').await;
        attach.notify_one();
        wait_until("terminal-a's fresh attachment", || {
            attachment_for(&mock, "terminal-a") != old
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
    assert_eq!(written(&mock, "terminal-b"), ["x", "y"]);
    mock.shutdown().await;
}

/// Runs the loop over one proxy pane whose frame source fails with `error`
/// at once, until a captured line carries `until`.
async fn recover_a_failed_pane(
    error: FrameError,
    arrange: impl FnOnce(&MockDaemon),
    logs: &Arc<Mutex<Vec<u8>>>,
    until: &str,
) {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let items = vec![json!({"terminal_id": "terminal-a", "backend": "native", "state": "live"})];
    roster(&mock, items.clone());
    roster(&mock, items);
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("attach terminal-a");
    let pane = workspace.pane_for_terminal("terminal-a").expect("pane a");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue_error(error);
    workspace
        .replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("install failing source");
    arrange(&mock);
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let driver = async {
        wait_within(DRAIN_DEADLINE, until, || !records(logs, until).is_empty()).await;
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
    mock.shutdown().await;
}

/// A3.6: every frame failure, give-up and recovered pane leaves one record
/// naming the pane, so `gclient.log` closes each attempt.
#[tokio::test]
async fn frame_errors_and_recovery_giveups_are_logged_with_pane_context() {
    let (guard, logs) = capture_logs();

    // A lagged source starts a recovery that lands on a fresh proxy attach.
    recover_a_failed_pane(FrameError::Lag, |_| {}, &logs, "frame_recovery_completed").await;
    let started = records(&logs, "frame_recovery_started");
    assert_eq!(started.len(), 1, "{started:?}");
    assert_recovery_context(&started[0], "terminal-a", "detach", "lag");
    assert!(started[0].contains("attachment_id="), "{}", started[0]);
    let completed = records(&logs, "frame_recovery_completed");
    assert_eq!(completed.len(), 1, "{completed:?}");
    assert!(
        completed[0].contains("transport=\"proxy\""),
        "{}",
        completed[0]
    );
    assert!(completed[0].contains("elapsed_ms="), "{}", completed[0]);

    // A detach the daemon never answers gives up at its deadline.
    recover_a_failed_pane(
        FrameError::Eof,
        |mock| mock.suppress_ws("terminal_detach"),
        &logs,
        "frame_recovery_gave_up",
    )
    .await;
    let gave_up = records(&logs, "frame_recovery_gave_up");
    assert_eq!(gave_up.len(), 1, "{gave_up:?}");
    assert_recovery_context(&gave_up[0], "terminal-a", "detach", "deadline");

    // A refused re-attach gives up with the daemon's code.
    recover_a_failed_pane(
        FrameError::Io("frame socket reset".into()),
        |mock| mock.refuse_next_proxy_attach("terminal_gone", "terminal exited"),
        &logs,
        // Earlier cases already logged proxy_attach records; wait for this
        // refusal's own.
        "error_class=\"terminal_gone\"",
    )
    .await;
    let gave_up = records(&logs, "frame_recovery_gave_up");
    assert_eq!(gave_up.len(), 2, "{gave_up:?}");
    assert_recovery_context(&gave_up[1], "terminal-a", "proxy_attach", "terminal_gone");

    // A stale attach result is discarded without a replacement.
    let mock = MockDaemon::start("local-token").await;
    let _ = finish_a_direct_attach_after_a_reconnect(&mock, &logs).await;
    mock.shutdown().await;
    let stale: Vec<String> = records(&logs, "frame_recovery_gave_up")
        .into_iter()
        .filter(|line| line.contains("terminal_id=terminal-b"))
        .collect();
    assert_eq!(stale.len(), 1, "{stale:?}");
    assert_recovery_context(&stale[0], "terminal-b", "direct_attach", "stale");
    let completed_before = records(&logs, "frame_recovery_completed").len();

    // A direct handshake the host never answers times out, and the pane
    // closes the attempt on the proxy.
    let mock = MockDaemon::start("local-token").await;
    let host = StallingHost::start_gated(Some(Arc::new(Notify::new()))).await;
    let (mut workspace, _home) = two_pane_workspace(&mock, Some(&host)).await;
    let mut terminal = Terminal::new(TestBackend::new(96, 30)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let driver = async {
        wait_within(Duration::from_secs(15), "the proxy fallback", || {
            records(&logs, "frame_recovery_completed")[completed_before..]
                .iter()
                .any(|line| {
                    line.contains("terminal_id=terminal-b") && line.contains("transport=\"proxy\"")
                })
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
    mock.shutdown().await;

    // A real direct source the pane does not drain retires with
    // `RetireReason::Lag`: the host floods frames before the loop runs, the
    // reader's bounded channel fills, and the reader closes its socket. The
    // loop drains what was buffered, reconnects straight to the host and
    // closes the attempt on the restored direct stream.
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let host = DirectHost::start("epoch-1").await;
    let (mut workspace, _home) = live_workspace_on_direct_host(&mock, &host, "terminal-d").await;
    wait_within(DRAIN_DEADLINE, "the direct source to retire on lag", || {
        // The host's connection task drops its receiver only once the client
        // closed the socket, which the reader does when it retires.
        while host.to_client.receiver_count() > 0 && host.to_client.len() < 32 {
            let _ = host.to_client.send(frame_message("flood"));
        }
        host.to_client.receiver_count() == 0
    })
    .await;
    let of_d = |name: &str| -> Vec<String> {
        records(&logs, name)
            .into_iter()
            .filter(|line| line.contains("terminal_id=terminal-d"))
            .collect()
    };
    let mut terminal = Terminal::new(TestBackend::new(48, 12)).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);
    let driver = async {
        host.expect_attaches(2).await;
        // The restored source is installed when the recovery applies; give it
        // the drain deadline, then let the assertions below judge the record.
        let _ = timeout(DRAIN_DEADLINE, async {
            while of_d("frame_recovery_completed").is_empty() {
                tokio::task::yield_now().await;
            }
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
    mock.shutdown().await;
    let started = of_d("frame_recovery_started");
    assert_eq!(started.len(), 1, "{started:?}");
    assert_recovery_context(&started[0], "terminal-d", "host_reconnect", "lag");
    assert!(started[0].contains("attachment_id="), "{}", started[0]);
    assert!(of_d("frame_recovery_gave_up").is_empty());
    let completed = of_d("frame_recovery_completed");
    assert_eq!(completed.len(), 1, "{completed:?}");
    assert!(
        completed[0].contains("transport=\"direct\""),
        "{}",
        completed[0]
    );
    assert!(completed[0].contains("elapsed_ms="), "{}", completed[0]);
    assert_eq!(
        host.attaches(),
        2,
        "the lagged pane reconnected to its host"
    );

    drop(guard);
    let timed_out = records(&logs, "direct_attach_timed_out");
    assert_eq!(timed_out.len(), 1, "{timed_out:?}");
    assert_recovery_context(&timed_out[0], "terminal-b", "direct_attach", "timeout");
}

/// A3.7: a new terminal whose attach the daemon holds for three seconds
/// never stops frames, ticks or keys on a pane streaming from its host.
#[tokio::test]
async fn a_slow_new_terminal_attach_never_stalls_streaming_direct_panes() {
    let mock = MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    let host = DirectHost::start("epoch-1").await;
    mock.serve_direct_attach(host.attach_locator("terminal-b"));
    roster(
        &mock,
        vec![json!({
            "terminal_id": "terminal-b",
            "backend": "native",
            "state": "live",
            "attach": host.roster_attach("terminal-b"),
        })],
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect live daemon");
    let mut workspace = Workspace::live(daemon);
    let home = tempfile::tempdir().expect("gobby home");
    std::fs::write(home.path().join("bootstrap.yaml"), "api_key: local-token\n")
        .expect("write API key");
    mock.seed_workspace("project-1", &[(&["terminal-b"], "terminal-b")]);
    workspace.set_gobby_home(home.path().to_path_buf());
    workspace.select_project("project-1");
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let mut chrome = Chrome::dark();
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, Arc::clone(&draws), Arc::clone(&frames));
        host.expect_attaches(1).await;
        let release = mock.hold_ws("terminal_attach", for_terminal("terminal-c"));
        mock.send_event(json!({
            "type": "terminal_event",
            "event": "created",
            "terminal_id": "terminal-c",
            "daemon_epoch": "epoch-1",
            "seq": 2,
            "timestamp": "2026-01-01T00:00:00Z"
        }));
        wait_until("terminal-c's attach", || {
            !attach_requests(&mock, "terminal-c").is_empty()
        })
        .await;
        tokio::time::pause();
        for second in 1..=3 {
            let tag = format!("slow-{second}");
            host.to_client
                .send(frame_message(&tag))
                .expect("host frame");
            let ticks = draws.load(Ordering::SeqCst);
            tokio::time::advance(Duration::from_secs(1)).await;
            wait_until(&format!("terminal-b to draw {tag}"), || {
                draws.load(Ordering::SeqCst) > ticks
                    && frames
                        .lock()
                        .expect("recorded frames")
                        .last()
                        .is_some_and(|frame| frame.contains(&tag))
            })
            .await;
            let key = char::from(b'0' + second);
            probe.key(KeyCode::Char(key), KeyModifiers::NONE).await;
            let typed = key.to_string().into_bytes();
            host.wait_for(&format!("key {key}"), |seen| {
                seen.iter().any(
                    |message| matches!(message, ClientMessage::Input { data } if *data == typed),
                )
            })
            .await;
        }
        tokio::time::resume();
        assert_eq!(
            mock.replies("terminal_attach"),
            1,
            "terminal-c's attach stayed held for three seconds"
        );
        release.notify_one();
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
    mock.shutdown().await;
}

/// A tmux pane closed while its own write is unsettled queues a detach behind
/// that write. The daemon holds the reply, so the detach cannot be sent yet. A
/// key typed into the still-focused pane must reach the daemon within
/// `KEY_BOUND` rather than wait on the detach.
#[tokio::test]
async fn a_key_typed_into_a_tmux_pane_closing_behind_a_held_write_is_not_held() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, _home, mut chrome) = closing_workspace(&mock, true).await;
    let (backend, draws, frames) = DrawRecorder::new(96, 30);
    let mut terminal = Terminal::new(backend).expect("test terminal");
    let (input_tx, input_rx) = mpsc::channel(1);

    let driver = async {
        let probe = LivenessProbe::new(&mock, &input_tx, draws, frames);
        wait_for_websocket_requests(&mock, "terminal_set_viewport", 2).await;
        probe.key_reaches("terminal-b", 'x').await;
        let release = mock.hold_ws("terminal_input", for_terminal("terminal-b"));
        probe.key(KeyCode::Char('y'), KeyModifiers::NONE).await;
        wait_until("the held write", || written(&mock, "terminal-b").len() == 2).await;
        probe.key(KeyCode::Char('b'), KeyModifiers::CONTROL).await;
        probe.key(KeyCode::Char('x'), KeyModifiers::NONE).await;
        probe.key(KeyCode::Char('z'), KeyModifiers::NONE).await;
        let landed = timeout(KEY_BOUND, async {
            while written(&mock, "terminal-b") != ["x", "y", "z"] {
                tokio::task::yield_now().await;
            }
        })
        .await;
        assert!(
            landed.is_ok(),
            "the key typed behind the closing detach never reached terminal-b: \
             terminal-b wrote {:?}, terminal-a wrote {:?}",
            written(&mock, "terminal-b"),
            written(&mock, "terminal-a"),
        );
        release.notify_one();
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
    mock.shutdown().await;
}
