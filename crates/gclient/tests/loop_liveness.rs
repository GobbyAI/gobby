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

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::app::run_live_loop;
use gobby_client::app::run_loop::RENDER_TICK;
use gobby_client::daemon::LiveDaemon;
use gobby_client::teardown::TerminalGuard;
use gobby_client::ui::Chrome;
use gobby_client::Workspace;
use gobby_terminal::input::TerminalKey;
use gobby_terminal::protocol::{write_message, CellData, FrameData, PaneModes, ServerMessage};
use gobby_terminal::raw_input::RawInputEvent;
use mock_daemon::MockDaemon;
use ratatui::backend::{Backend, ClearType, TestBackend, WindowSize};
use ratatui::buffer::Cell;
use ratatui::layout::{Position, Size};
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc;
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

/// Two live panes in one tab, `terminal-b` focused, so the loop starts by
/// taking control of `terminal-b`. Keep the returned home alive for the
/// loop's lifetime.
async fn two_pane_workspace(mock: &MockDaemon) -> (Workspace<LiveDaemon>, tempfile::TempDir) {
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-a", "backend": "native", "state": "live"},
                {"terminal_id": "terminal-b", "backend": "native", "state": "live"}
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
    let (mut workspace, _home) = two_pane_workspace(&mock).await;
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
