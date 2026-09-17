use std::collections::HashSet;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, watch};
use tokio::time::timeout;

use super::{
    encoded_message_bytes, enqueue_control, send_control, write_outbound, ControlClose,
    FrameMailbox, PushResult,
};
use crate::host::config::HostConfig;
use crate::host::helpers::push_terminal_ansi;
use crate::host::state::{
    Attachment, CommitState, HostState, Identity, ObserverBind, TerminalSlot,
};
use crate::host::{control, frames};
use crate::protocol::{
    CellData, FrameData, ObservationState, PaneModes, RenderEncoding, ServerMessage, TerminalFrame,
    DELTA_QUEUE_BYTES, MAX_DELTA_QUEUE_BYTES, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};

const CONTROL_TOKEN: &str = "test-control-token";

/// The exact bytes `write_outbound` writes when a peer misses the deadline.
const DEADLINE_LINE: &str = "{\"ok\":false,\"error\":\"control_deadline\"}\n";

/// A host with test-local caps, so nothing here sleeps a shipped duration.
fn test_state(config: HostConfig) -> Arc<HostState> {
    let config = config
        .validate()
        .expect("test-local caps stay inside the shipped bounds");
    HostState::new(
        config,
        CONTROL_TOKEN.to_string(),
        "local-token".to_string(),
        "test-epoch".to_string(),
        "0.0.0-test".to_string(),
        std::process::id(),
        watch::channel(false).0,
    )
}

/// A control peer on a real `AF_UNIX` socket, served by the host's own
/// `control::handle_connection` in this process. Every assertion below reads
/// this peer's socket, so each observable is a byte the host actually wrote.
struct ControlPeer {
    reader: BufReader<OwnedReadHalf>,
    writer: OwnedWriteHalf,
}

impl ControlPeer {
    /// Connect, hand the host its side, and finish the `hello` handshake so
    /// later requests are dispatched instead of refused.
    async fn connect(state: &Arc<HostState>) -> Self {
        Self::connect_with_send_buffer(state, None).await
    }

    /// `send_buffer` caps what the kernel holds for the host's side of the
    /// socket, which is what bounds the bytes in flight to this peer. A small
    /// cap makes a blocked response deterministic: the host stops after one
    /// buffer's worth, and when it gives up the peer has only that much left to
    /// read, so the close it measures is the host's deadline and not its own
    /// reading pace.
    async fn connect_with_send_buffer(state: &Arc<HostState>, send_buffer: Option<u32>) -> Self {
        let (host_side, peer_side) = UnixStream::pair().expect("unix socket pair");
        if let Some(bytes) = send_buffer {
            frames::set_send_buffer(&host_side, bytes);
        }
        let served = Arc::clone(state);
        tokio::spawn(async move {
            control::handle_connection(host_side, served).await;
        });
        let (reader, writer) = peer_side.into_split();
        let mut peer = Self {
            reader: BufReader::new(reader),
            writer,
        };
        peer.send(json!({
            "id": "hello",
            "method": "hello",
            "control_token": CONTROL_TOKEN,
            "protocol_version": PROTOCOL_VERSION,
        }))
        .await;
        let ack = peer
            .next_response()
            .await
            .expect("the host must answer hello");
        assert_eq!(ack["ok"], true, "the control peer must authenticate: {ack}");
        peer
    }

    async fn send(&mut self, request: Value) {
        let mut line = request.to_string();
        line.push('\n');
        self.writer
            .write_all(line.as_bytes())
            .await
            .expect("control peer write");
    }

    /// The next JSON line the host wrote, or `None` once the host closes.
    async fn next_response(&mut self) -> Option<Value> {
        let mut line = String::new();
        let read = self
            .reader
            .read_line(&mut line)
            .await
            .expect("control peer read");
        if read == 0 {
            return None;
        }
        Some(
            serde_json::from_str(line.trim_end())
                .unwrap_or_else(|err| panic!("host wrote a non-JSON line ({err}): {line}")),
        )
    }

    /// Creep through the socket until the host closes, taking it slower than
    /// the host fills it.
    ///
    /// Reading slowly rather than not at all is what arms the delivery
    /// deadline while leaving the host somewhere to put the reason: a peer that
    /// never reads can only be handed an EOF.
    /// Returns what arrived and whether the host closed inside `budget`.
    async fn drain_slowly(&mut self, budget: Duration) -> (Vec<u8>, bool) {
        let started = Instant::now();
        let mut received = Vec::new();
        let mut buffer = vec![0_u8; 4096];
        loop {
            let Some(remaining) = budget.checked_sub(started.elapsed()) else {
                return (received, false);
            };
            let Ok(read) = timeout(remaining, self.reader.read(&mut buffer)).await else {
                return (received, false);
            };
            let read = read.expect("control peer read");
            if read == 0 {
                return (received, true);
            }
            received.extend_from_slice(&buffer[..read]);
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    }
}

#[tokio::test]
async fn control_deadline_and_event_overflow() {
    let shipped = HostConfig::default();
    assert_eq!(
        shipped.lag_timeout(),
        Duration::from_secs(5),
        "shipped lag_timeout stays 5 s"
    );
    assert_eq!(
        shipped.control_deadline(),
        Duration::from_secs(2),
        "shipped control_deadline stays 2 s"
    );

    // Every control response below the cap reaches the peer, in request order,
    // one line per request. `resize` is dispatched through the single ordered
    // task, which is where that ordering is owed.
    const ORDERED: u64 = 6;
    let ordered_state = test_state(HostConfig {
        control_deadline_ms: 250,
        control_queue_entries: 4,
        ..HostConfig::default()
    });
    let mut peer = ControlPeer::connect(&ordered_state).await;
    for seq in 1..=ORDERED {
        peer.send(json!({
            "id": format!("resize-{seq}"),
            "method": "resize",
            "operation_seq": seq,
            "host_terminal_id": format!("missing-{seq}"),
            "rows": 24,
            "cols": 80,
        }))
        .await;
    }
    for seq in 1..=ORDERED {
        let response = timeout(Duration::from_secs(5), peer.next_response())
            .await
            .expect("every response below the cap must arrive")
            .expect("the control socket must stay open below the cap");
        let expected = format!("resize-{seq}");
        assert_eq!(
            response["id"].as_str(),
            Some(expected.as_str()),
            "control responses below the cap arrive in order, one per request: {response}"
        );
    }
    let extra = timeout(Duration::from_millis(250), peer.next_response()).await;
    assert!(
        extra.is_err(),
        "the host must send exactly one response per request, got {extra:?}"
    );

    // A peer that takes the socket slower than the host fills it is closed at
    // control_deadline, and the host names the reason on the wire first. The
    // deadline is tiny on purpose: nextest runs this beside hundreds of other
    // test processes, and the shorter the window the less of the machine's
    // scheduling noise lands inside it.
    let slow_state = test_state(HostConfig {
        control_deadline_ms: 20,
        control_queue_entries: 4,
        ..HostConfig::default()
    });
    let deadline = slow_state.config.control_deadline();
    // 4 KiB of in-flight bytes, so the host blocks almost immediately and the
    // peer is left with only a buffer's worth to read once the host gives up.
    let mut slow = ControlPeer::connect_with_send_buffer(&slow_state, Some(4096)).await;
    let huge_id = "x".repeat(1024 * 1024);
    let started = Instant::now();
    slow.send(json!({"id": huge_id, "method": "ping"})).await;
    let (received, closed) = slow.drain_slowly(Duration::from_secs(5)).await;
    let closed_after = started.elapsed();
    assert!(
        closed,
        "the host must close a peer that cannot keep up, not hold it forever; peer read {} bytes in {closed_after:?}",
        received.len()
    );
    assert!(
        closed_after >= deadline,
        "the host must hold the response for the whole deadline, closed after {closed_after:?}"
    );
    assert!(
        received.len() < huge_id.len() / 2,
        "the deadline must cut the oversized response short, peer read {} of {} bytes",
        received.len(),
        huge_id.len()
    );
    let tail = String::from_utf8_lossy(&received[received.len().saturating_sub(64)..]).into_owned();
    assert!(
        tail.ends_with(DEADLINE_LINE),
        "the host must name control_deadline before closing; tail={tail:?}"
    );

    // A real event subscriber that does not drain is told event_overflow on its
    // own socket. Only the `vt-engine` build has an emit path to overflow it
    // with, so the default-feature build compiles this test without the block.
    #[cfg(feature = "vt-engine")]
    {
        let event_state = test_state(HostConfig {
            control_deadline_ms: 500,
            control_queue_entries: 4,
            event_queue_bytes: 512,
            ..HostConfig::default()
        });
        let mut subscriber = ControlPeer::connect(&event_state).await;
        subscriber
            .send(json!({"id": "subscribe", "method": "subscribe_events"}))
            .await;
        let ack = subscriber
            .next_response()
            .await
            .expect("the host must answer subscribe_events");
        assert_eq!(
            ack["subscribed"], true,
            "the event subscriber must be registered: {ack}"
        );
        // A tight burst: nothing in this loop yields, so the forwarding task
        // never runs and the subscriber's queue is driven past
        // event_queue_bytes.
        for seq in 0..256_u32 {
            event_state
                .events
                .emit_terminal_exited("term".into(), "host-term".into(), Some(seq))
                .await;
        }
        let mut saw_overflow = false;
        for _ in 0..64 {
            let event = timeout(Duration::from_secs(5), subscriber.next_response())
                .await
                .expect("the subscriber must be told why it stopped receiving")
                .expect("the subscriber socket must stay open");
            if event.get("error").and_then(Value::as_str) == Some("event_overflow") {
                saw_overflow = true;
                break;
            }
        }
        assert!(
            saw_overflow,
            "an event subscriber that does not drain must receive event_overflow"
        );
    }
}

#[tokio::test]
async fn completed_rpc_reply_waits_for_outbound_capacity() {
    let (tx, mut rx) = mpsc::channel(1);
    enqueue_control(&tx, json!({"id": "held"})).expect("fill");
    let sender = tx.clone();
    let wait = tokio::spawn(async move { send_control(&sender, json!({"id": "rpc"})).await });
    tokio::task::yield_now().await;
    assert!(
        !wait.is_finished(),
        "a completed RPC reply must wait for outbound capacity instead of dropping"
    );
    assert_eq!(rx.recv().await.expect("held")["id"], "held");
    wait.await.expect("join").expect("rpc enqueued");
    assert_eq!(rx.recv().await.expect("rpc")["id"], "rpc");
}

fn error_msg(code: &str) -> ServerMessage {
    ServerMessage::Error {
        code: code.into(),
        message: None,
    }
}

fn queued_len(mailbox: &FrameMailbox) -> usize {
    mailbox.lock().items.len()
}

#[test]
fn empty_mailbox_does_not_admit_a_frame_over_cap() {
    let small = error_msg("n");
    let over = error_msg(&"n".repeat(64));
    let cap = encoded_message_bytes(&small);
    let over_bytes = encoded_message_bytes(&over);
    assert!(
        over_bytes > cap,
        "over-cap probe must exceed cap={cap}, got {over_bytes}"
    );

    let mailbox = FrameMailbox::new();
    assert_eq!(mailbox.try_push(&over, cap), PushResult::Overflow);
    assert_eq!(
        mailbox.queued_bytes(),
        0,
        "queued_bytes must stay at 0 when the first frame exceeds the cap"
    );
    assert_eq!(queued_len(&mailbox), 0);
}

#[test]
fn overflow_collapses_to_one_keyframe_within_cap() {
    let small = error_msg("n");
    let over = error_msg(&"n".repeat(64));
    let cap = encoded_message_bytes(&small);
    let mailbox = FrameMailbox::new();

    assert_eq!(mailbox.try_push(&small, cap), PushResult::Queued);
    assert_eq!(mailbox.try_push(&small, cap), PushResult::Overflow);
    assert_eq!(
        queued_len(&mailbox),
        1,
        "overflow must leave the in-cap queue untouched"
    );
    assert!(mailbox.queued_bytes() <= cap);

    mailbox.replace_with_keyframe(&small, cap);
    assert_eq!(
        queued_len(&mailbox),
        1,
        "resync must collapse the mailbox to one replacement keyframe"
    );
    assert!(
        mailbox.queued_bytes() <= cap,
        "replacement keyframe must respect the byte cap; queued={} cap={cap}",
        mailbox.queued_bytes()
    );

    mailbox.replace_with_keyframe(&over, cap);
    assert_eq!(
        queued_len(&mailbox),
        1,
        "an over-cap replacement must not grow the mailbox"
    );
    assert!(
        mailbox.queued_bytes() <= cap,
        "replace_with_keyframe must not land over cap; queued={} cap={cap}",
        mailbox.queued_bytes()
    );
}

#[test]
fn desynced_push_stays_at_one_keyframe() {
    let keyframe = error_msg("keyframe");
    let delta = error_msg("d");
    let cap = encoded_message_bytes(&keyframe) + encoded_message_bytes(&delta) * 8;
    let mailbox = FrameMailbox::new();

    assert_eq!(
        mailbox.push_observed(&keyframe, cap, false),
        PushResult::Queued
    );
    assert_eq!(
        mailbox.push_observed(&delta, cap, false),
        PushResult::Queued
    );
    assert_eq!(
        mailbox.push_observed(&delta, cap, false),
        PushResult::Queued
    );
    assert_eq!(
        queued_len(&mailbox),
        3,
        "keeping up may queue deltas under cap"
    );

    assert_eq!(
        mailbox.push_observed(&keyframe, cap, true),
        PushResult::Overflow
    );
    assert_eq!(
        queued_len(&mailbox),
        1,
        "a desynced observer must collapse to one replacement keyframe"
    );
    assert_eq!(
        mailbox.push_observed(&delta, cap, true),
        PushResult::Overflow
    );
    assert_eq!(
        queued_len(&mailbox),
        1,
        "further frames while desynced must replace, not restack"
    );
    assert!(mailbox.queued_bytes() <= cap);

    assert!(mailbox.try_pop().is_some());
    assert_eq!(
        mailbox.push_observed(&delta, cap, true),
        PushResult::Overflow
    );
    assert_eq!(
        queued_len(&mailbox),
        0,
        "do not queue on top of an in-flight write"
    );
}

#[test]
fn force_push_never_exceeds_cap() {
    let small = error_msg("n");
    let over = error_msg(&"n".repeat(64));
    let cap = encoded_message_bytes(&small);
    let mailbox = FrameMailbox::new();
    assert_eq!(mailbox.try_push(&small, cap), PushResult::Queued);
    mailbox.force_push(over, cap);
    assert!(
        mailbox.queued_bytes() <= cap,
        "force_push must not bypass the byte cap; queued={} cap={cap}",
        mailbox.queued_bytes()
    );
}

#[test]
fn enqueue_control_returns_overflow_at_cap() {
    let (tx, _rx) = mpsc::channel(1);
    tx.try_send(json!({"n": 1})).expect("fill control queue");
    assert_eq!(
        enqueue_control(&tx, json!({"n": 2})),
        Err(ControlClose::Overflow)
    );
}

#[test]
fn shipped_delta_queue_admits_one_max_frame() {
    assert_eq!(DELTA_QUEUE_BYTES, MAX_FRAME_SIZE);
    assert_eq!(MAX_DELTA_QUEUE_BYTES, MAX_FRAME_SIZE as u32);
    assert_eq!(
        HostConfig::default().delta_queue_bytes,
        MAX_FRAME_SIZE as u32
    );
    let test_local = HostConfig {
        delta_queue_bytes: 4096,
        ..HostConfig::default()
    };
    test_local
        .validate()
        .expect("4096 admits an 8x24 semantic keyframe and remains a valid test-local cap");
}

/// One `terminal_ansi` frame carrying `payload` bytes of escape data.
fn ansi_frame(seq: u64, full: bool, payload: usize) -> ServerMessage {
    ServerMessage::Terminal(TerminalFrame {
        seq,
        width: 80,
        height: 24,
        full,
        bytes: vec![b'x'; payload],
    })
}

/// The host-side half of the slow-observer contract. No control stat reports
/// per-observer queued bytes, so `slow_observer_resyncs_with_one_keyframe`
/// proves what the kernel held for the peer and this proves what the host held:
/// drive the mailbox past `delta_queue_bytes` with real encoded frames and the
/// queued high-water mark stays at or under the cap, with exactly one keyframe
/// standing in for every delta that was dropped.
#[test]
fn queued_bytes_never_exceed_the_delta_queue_cap() {
    let cap = HostConfig {
        delta_queue_bytes: 1024,
        ..HostConfig::default()
    }
    .validate()
    .expect("1024 is a valid test-local delta_queue_bytes")
    .delta_queue_bytes as usize;

    let keyframe = ansi_frame(0, true, 480);
    let keyframe_bytes = encoded_message_bytes(&keyframe);
    assert!(
        keyframe_bytes <= cap,
        "the repaint must fit the cap it replaces deltas under; keyframe={keyframe_bytes} cap={cap}"
    );

    let mailbox = FrameMailbox::new();
    let mut offered = 0_usize;
    let mut high_water = 0_usize;
    let mut overflowed = false;
    for seq in 1..=256_u64 {
        let delta = ansi_frame(seq, false, 96);
        offered += encoded_message_bytes(&delta);
        match mailbox.try_push(&delta, cap) {
            PushResult::Queued => {}
            PushResult::Overflow => {
                overflowed = true;
                break;
            }
            PushResult::Closed => panic!("an open mailbox must not report Closed"),
        }
        high_water = high_water.max(mailbox.queued_bytes());
        assert!(
            mailbox.queued_bytes() <= cap,
            "queued bytes {} must never exceed delta_queue_bytes={cap}",
            mailbox.queued_bytes()
        );
    }
    assert!(
        overflowed,
        "the burst must drive the mailbox past the cap; offered={offered} cap={cap}"
    );
    let owed = queued_len(&mailbox);
    assert!(
        owed >= 2,
        "the observer must owe several deltas before the collapse, owed={owed}"
    );

    assert!(
        mailbox.replace_with_keyframe(&keyframe, cap),
        "an in-cap keyframe must be accepted as the replacement"
    );
    high_water = high_water.max(mailbox.queued_bytes());
    assert!(
        high_water <= cap,
        "the queued high-water mark {high_water} must stay at or under delta_queue_bytes={cap}"
    );
    assert_eq!(
        queued_len(&mailbox),
        1,
        "exactly one keyframe replaces every dropped delta"
    );
    match mailbox.try_pop() {
        Some(ServerMessage::Terminal(frame)) => assert!(
            frame.full,
            "the replacement must be a keyframe, not another delta"
        ),
        other => panic!("the mailbox must hold the replacement keyframe, got {other:?}"),
    }
    assert!(
        mailbox.try_pop().is_none(),
        "nothing is owed after the replacement keyframe"
    );
}

struct FailingWriter;

impl AsyncWrite for FailingWriter {
    fn poll_write(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        _buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Poll::Ready(Err(std::io::Error::other("peer reset")))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

#[tokio::test]
async fn write_outbound_distinguishes_disconnect_from_peer_error() {
    let (tx, rx) = mpsc::channel(4);
    drop(tx);
    let closed = write_outbound(Vec::<u8>::new(), rx, Duration::from_millis(50)).await;
    assert_eq!(closed, ControlClose::Disconnected);

    let (tx, rx) = mpsc::channel(4);
    tx.try_send(json!({"ok": true})).expect("enqueue");
    drop(tx);
    let errored = write_outbound(FailingWriter, rx, Duration::from_millis(50)).await;
    assert_eq!(errored, ControlClose::Overflow);
}

/// A committed native slot with no child: `resize` only needs the slot's
/// bookkeeping, so this runs with and without `vt-engine`.
async fn insert_native_slot(state: &HostState, host_terminal_id: &str, rows: u16, cols: u16) {
    let identity = Identity {
        terminal_id: format!("term-{host_terminal_id}"),
        spawn_key: format!("spawn-{host_terminal_id}"),
    };
    let slot = TerminalSlot {
        identity: identity.clone(),
        host_terminal_id: host_terminal_id.to_owned(),
        commit_state: CommitState::Committed,
        pgid: 0,
        start_time: 0.0,
        title: String::new(),
        rows,
        cols,
        last_seq: 0,
        observation_state: ObservationState::Live,
        observation_reason: None,
        observation_generation: 1,
        fingerprint: 0,
        reservation_id: String::new(),
        reserve_key: String::new(),
        reserve_generation: 0,
        observer_bind: ObserverBind::None,
        commit_deadline: None,
        #[cfg(feature = "vt-engine")]
        child: None,
        #[cfg(feature = "vt-engine")]
        written_bytes: 0,
        #[cfg(feature = "vt-engine")]
        dropped_bytes: 0,
        #[cfg(feature = "vt-engine")]
        total_bytes: 0,
        #[cfg(feature = "vt-engine")]
        truncated: false,
        user_attachments: HashSet::new(),
        locator: None,
        tmux_history_bytes: 0,
        history: None,
        last_frame: None,
        #[cfg(feature = "vt-engine")]
        observer_generation: 1,
        consecutive_failures: 0,
    };
    let mut inner = state.inner.lock().await;
    inner
        .by_host_id
        .insert(host_terminal_id.to_owned(), identity.clone());
    inner.terminals.insert(identity, slot);
}

/// A frame of the attachment's viewport, as the frame pass renders one, with
/// `marks` written into its leading cells.
fn viewport_frame(att: &Attachment, marks: &str) -> FrameData {
    let blank = CellData {
        symbol: " ".to_owned(),
        fg: 0,
        bg: 0,
        modifier: 0,
        skip: false,
        hyperlink: None,
    };
    let mut cells = vec![blank; usize::from(att.cols) * usize::from(att.rows)];
    for (cell, mark) in cells.iter_mut().zip(marks.chars()) {
        cell.symbol = mark.to_string();
    }
    FrameData {
        cells,
        width: att.cols,
        height: att.rows,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    }
}

/// Take the next queued frame and finish writing it, as the frame writer does.
fn pop_terminal(mailbox: &FrameMailbox) -> TerminalFrame {
    let msg = mailbox.try_pop().expect("a frame was queued");
    mailbox.note_drain();
    match msg {
        ServerMessage::Terminal(frame) => frame,
        other => panic!("expected a terminal frame, got {other:?}"),
    }
}

/// Two viewers already streaming deltas at the geometry the PTY is resized to.
/// The resize reflows the grid both peers painted, so each viewer's next frame
/// must be one full keyframe at that geometry, never a delta over the reflowed
/// screen. A viewer of another terminal keeps streaming deltas.
#[tokio::test]
async fn resize_resyncs_every_attachment_with_a_keyframe() {
    let state = test_state(HostConfig::default());
    insert_native_slot(&state, "ht-resized", 24, 80).await;
    insert_native_slot(&state, "ht-other", 40, 120).await;
    let mut viewers = Vec::new();
    for host_terminal_id in ["ht-resized", "ht-resized", "ht-other"] {
        let (id, mailbox) = state
            .attach(
                host_terminal_id,
                None,
                RenderEncoding::TerminalAnsi,
                40,
                120,
            )
            .await
            .expect("a native terminal admits the viewer");
        viewers.push((id, mailbox));
    }
    {
        let mut inner = state.inner.lock().await;
        for (id, mailbox) in &viewers {
            let att = inner.attachments.get_mut(id).expect("attached");
            let settled = viewport_frame(att, "prompt");
            assert!(push_terminal_ansi(att, &settled, 1, usize::MAX));
            assert!(pop_terminal(mailbox).full, "attach starts with a keyframe");
            let typed = viewport_frame(att, "prompt$");
            assert!(push_terminal_ansi(att, &typed, 2, usize::MAX));
            assert!(
                !pop_terminal(mailbox).full,
                "a synced viewer streams deltas"
            );
        }
    }

    let request = json!({"host_terminal_id": "ht-resized", "rows": 40, "cols": 120});
    let resized = state
        .resize(request.as_object().expect("request is an object"))
        .await;
    assert_eq!(resized, json!({"ok": true, "rows": 40, "cols": 120}));

    let mut inner = state.inner.lock().await;
    for (id, mailbox) in &viewers {
        let att = inner.attachments.get_mut(id).expect("attached");
        let resynced = att.host_terminal_id == "ht-resized";
        let reflowed = viewport_frame(att, "prompt$ ls");
        assert!(push_terminal_ansi(att, &reflowed, 3, usize::MAX));
        let sent = pop_terminal(mailbox);
        assert_eq!(
            sent.full, resynced,
            "viewer {id} of {}: full={}",
            att.host_terminal_id, sent.full
        );
        if resynced {
            assert_eq!((sent.width, sent.height), (120, 40));
        }
        assert!(mailbox.try_pop().is_none(), "one frame per viewer");
        assert!(
            !push_terminal_ansi(att, &reflowed, 3, usize::MAX),
            "the keyframe is the new baseline, so an unchanged frame sends nothing"
        );
    }
}
