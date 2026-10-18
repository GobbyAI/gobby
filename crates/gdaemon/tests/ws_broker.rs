//! Native broadcast fanout through `BroadcastBroker`, driven with in-memory
//! sinks under paused time; no listener, socket or credential is involved.
//! Corpus broadcasts publish the exact strings the Python oracle put on the
//! wire, so the native path is checked byte-for-byte against
//! `tests/fixtures/ws_envelope_golden`.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use bytes::Bytes;
use futures_util::Sink;
use gobby_daemon::websocket::broker::{
    Batch, BroadcastBroker, ConnectionId, MAX_QUEUED_BATCHES, MAX_QUEUED_BYTES, Registration,
};
use gobby_daemon::websocket::subscriptions::EntryMode;
use serde_json::{Value, json};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel};
use tokio::time::{Instant, advance, sleep};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;

/// Paused time only advances once every task is idle, so sleeping this long
/// lets each writer finish whatever it can do without waiting on a timer.
const SETTLE: Duration = Duration::from_millis(1);

const CONFIG_EVENT: &str = r#"{"type": "config_event", "revision": 1}"#;

#[derive(Debug)]
struct Gone;

/// In-memory socket. A stalling sink accepts a data frame but never finishes
/// sending it, like a peer that stopped reading; close frames still go out.
struct FakeSink {
    tx: UnboundedSender<Message>,
    stall: bool,
    stalled: bool,
}

impl Sink<Message> for FakeSink {
    type Error = Gone;

    fn poll_ready(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Result<(), Gone>> {
        Poll::Ready(Ok(()))
    }

    fn start_send(self: Pin<&mut Self>, item: Message) -> Result<(), Gone> {
        let this = self.get_mut();
        this.stalled = this.stall && matches!(item, Message::Text(_) | Message::Binary(_));
        if this.stalled {
            return Ok(());
        }
        this.tx.send(item).map_err(|_| Gone)
    }

    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Result<(), Gone>> {
        if self.stalled {
            Poll::Pending
        } else {
            Poll::Ready(Ok(()))
        }
    }

    fn poll_close(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Result<(), Gone>> {
        Poll::Ready(Ok(()))
    }
}

struct Client {
    registration: Registration,
    rx: UnboundedReceiver<Message>,
    cleanups: Arc<AtomicUsize>,
}

impl Client {
    /// Every frame the client has received once the writers are idle.
    async fn drain(&mut self) -> Vec<Message> {
        sleep(SETTLE).await;
        let mut received = Vec::new();
        while let Ok(message) = self.rx.try_recv() {
            received.push(message);
        }
        received
    }

    fn cleanups(&self) -> usize {
        self.cleanups.load(Ordering::SeqCst)
    }
}

fn connect(broker: &BroadcastBroker, id: u64, entry: EntryMode, stall: bool) -> Client {
    let (tx, rx) = unbounded_channel();
    let cleanups = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&cleanups);
    let sink = FakeSink {
        tx,
        stall,
        stalled: false,
    };
    let registration = broker
        .register(ConnectionId::new(id), entry, sink, move || {
            counter.fetch_add(1, Ordering::SeqCst);
        })
        .expect("register a fresh connection");
    Client {
        registration,
        rx,
        cleanups,
    }
}

fn batch(frames: &[&str]) -> Batch {
    Batch::new(frames.iter().map(|frame| (*frame).to_owned()).collect()).expect("batch")
}

fn texts(messages: &[Message]) -> Vec<&str> {
    messages
        .iter()
        .map(|message| match message {
            Message::Text(text) => text.as_str(),
            other => panic!("expected a text frame, got {other:?}"),
        })
        .collect()
}

fn assert_closed_1011(messages: &[Message], context: &str) {
    assert!(
        matches!(messages, [Message::Close(Some(frame))] if frame.code == CloseCode::Error),
        "{context}: expected only a 1011 close, got {messages:?}"
    );
}

fn corpus_cases() -> Vec<Value> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/ws_envelope_golden/cases.json");
    let corpus: Value =
        serde_json::from_slice(&std::fs::read(path).expect("read corpus")).expect("parse corpus");
    corpus["cases"].as_array().expect("corpus cases").clone()
}

/// Acknowledgement `events` arrays are sets; only they are sorted.
fn assert_reply(actual: &str, expected: &Value, context: &str) {
    match expected {
        Value::String(bytes) => assert_eq!(actual, bytes, "{context}: reply bytes"),
        Value::Object(reply) => {
            let mut ack = reply["ack"].clone();
            ack["events"]
                .as_array_mut()
                .expect("ack events")
                .sort_by(|a, b| a.as_str().cmp(&b.as_str()));
            let actual: Value = serde_json::from_str(actual).expect("reply is JSON");
            assert_eq!(actual, ack, "{context}: acknowledgement");
        }
        other => panic!("{context}: unsupported reply {other}"),
    }
}

/// Replays every corpus case whose clients arrive through a public entry
/// mode; registration cannot produce the internal explicit-null state.
#[tokio::test(start_paused = true)]
async fn fanout_preserves_selection_and_frame_order() {
    let mut fragmented = 0;
    for case in corpus_cases() {
        let name = case["name"].as_str().expect("case name");
        let specs = case["clients"].as_object().expect("case clients");
        if specs
            .values()
            .any(|spec| spec.get("explicit_null").is_some())
        {
            continue;
        }
        let broker = BroadcastBroker::default();
        let mut clients: BTreeMap<&str, Client> = BTreeMap::new();
        for (index, (label, spec)) in (0u64..).zip(specs) {
            let entry = match spec["entry"].as_str() {
                Some("direct") => EntryMode::Direct,
                Some("browser") => EntryMode::Browser,
                other => panic!("{name}: unknown entry {other:?}"),
            };
            clients.insert(label.as_str(), connect(&broker, index, entry, false));
        }
        let steps = case["steps"].as_array().expect("case steps");
        for (index, step) in steps.iter().enumerate() {
            let context = format!("{name} step {index}");
            match step["op"].as_str() {
                Some("send") => {
                    let sender = step["client"].as_str().expect("send client");
                    let text = step["text"].as_str().expect("send text");
                    let forwarded = broker.receive(&clients[sender].registration, text);
                    assert_eq!(forwarded, None, "{context}: control was forwarded");
                    let replies = step["replies"].as_array().expect("send replies");
                    for (label, client) in &mut clients {
                        let received = client.drain().await;
                        if *label == sender {
                            let received = texts(&received);
                            assert_eq!(received.len(), replies.len(), "{context}: reply count");
                            for (actual, expected) in received.iter().zip(replies) {
                                assert_reply(actual, expected, &context);
                            }
                        } else {
                            assert!(received.is_empty(), "{context}: {label} got {received:?}");
                        }
                    }
                }
                Some("broadcast") => {
                    let deliveries = step["deliveries"].as_object().expect("deliveries");
                    let wire = deliveries
                        .values()
                        .filter_map(Value::as_array)
                        .find(|frames| !frames.is_empty())
                        .map(|frames| {
                            frames
                                .iter()
                                .map(|frame| frame.as_str().expect("frame").to_owned())
                                .collect()
                        })
                        .unwrap_or_else(|| vec![step["message"].to_string()]);
                    if step["frames"]
                        .as_array()
                        .is_some_and(|frames| !frames.is_empty())
                    {
                        fragmented += 1;
                    }
                    let wire = Batch::new(wire).expect("corpus batch");
                    broker.publish(&step["message"], &wire);
                    for (label, client) in &mut clients {
                        let expected: Vec<&str> = deliveries[*label]
                            .as_array()
                            .expect("client deliveries")
                            .iter()
                            .map(|frame| frame.as_str().expect("frame"))
                            .collect();
                        let received = client.drain().await;
                        assert_eq!(texts(&received), expected, "{context}: frames for {label}");
                    }
                }
                other => panic!("{context}: unexpected op {other:?}"),
            }
        }
    }
    assert!(
        fragmented >= 2,
        "fragmented corpus broadcasts were not replayed"
    );
}

#[tokio::test(start_paused = true)]
async fn acknowledgement_precedes_newly_selected_events() {
    let broker = BroadcastBroker::default();
    let mut client = connect(&broker, 1, EntryMode::Direct, false);
    let event = json!({"type": "session_message", "session_id": "s-1"});
    let frame = r#"{"type": "session_message", "session_id": "s-1"}"#;

    // Absent subscriptions filter the first publication.
    broker.publish(&event, &batch(&[frame]));
    let subscribe = r#"{"type": "subscribe", "events": ["session_message"]}"#;
    assert_eq!(broker.receive(&client.registration, subscribe), None);
    broker.publish(&event, &batch(&[frame]));

    let ack = r#"{"type": "subscribe_success", "events": ["session_message"]}"#;
    assert_eq!(texts(&client.drain().await), vec![ack, frame]);

    let unsubscribe = r#"{"type": "unsubscribe", "events": ["session_message"]}"#;
    assert_eq!(broker.receive(&client.registration, unsubscribe), None);
    broker.publish(&event, &batch(&[frame]));
    let ack = r#"{"type": "unsubscribe_success", "events": []}"#;
    assert_eq!(texts(&client.drain().await), vec![ack]);
}

#[tokio::test(start_paused = true)]
async fn slow_client_isolated_and_bounded() {
    let broker = BroadcastBroker::default();
    let event = json!({"type": "config_event", "revision": 1});
    let one = batch(&[CONFIG_EVENT]);
    let mut healthy = connect(&broker, 1, EntryMode::Browser, false);

    // Batch limit: the first batch is in flight on the stalled socket and the
    // queue fills to its limit without delaying the healthy client.
    let mut by_count = connect(&broker, 2, EntryMode::Browser, true);
    broker.publish(&event, &one);
    assert_eq!(texts(&healthy.drain().await), vec![CONFIG_EVENT]);
    for _ in 0..MAX_QUEUED_BATCHES {
        broker.publish(&event, &one);
    }
    let received = healthy.drain().await;
    assert_eq!(texts(&received), vec![CONFIG_EVENT; MAX_QUEUED_BATCHES]);
    assert!(by_count.drain().await.is_empty());
    assert_eq!(by_count.cleanups(), 0, "a full queue is not an overflow");
    let overflowed = Instant::now();
    broker.publish(&event, &one);
    assert_eq!(texts(&healthy.drain().await), vec![CONFIG_EVENT]);
    assert_closed_1011(&by_count.drain().await, "batch overflow");
    assert!(overflowed.elapsed() < Duration::from_secs(1));
    assert_eq!(by_count.cleanups(), 1);

    // Byte limit, reached below the batch limit. Recipients share one copy of
    // each frame, so the healthy client holds no extra 8 MiB per batch.
    let mut by_bytes = connect(&broker, 3, EntryMode::Browser, true);
    let large = "x".repeat(MAX_QUEUED_BYTES / 8);
    let large = batch(&[&large]);
    broker.publish(&event, &large);
    assert!(by_bytes.drain().await.is_empty());
    for _ in 0..8 {
        broker.publish(&event, &large);
    }
    assert!(by_bytes.drain().await.is_empty());
    assert_eq!(by_bytes.cleanups(), 0, "exactly the byte limit fits");
    broker.publish(&event, &one);
    assert_closed_1011(&by_bytes.drain().await, "byte overflow");
    assert_eq!(by_bytes.cleanups(), 1);
    assert_eq!(healthy.drain().await.len(), 10);

    // A batch that could never fit is refused before reaching any queue.
    let oversize = "x".repeat(MAX_QUEUED_BYTES + 1);
    assert!(Batch::new(vec![oversize]).is_err());

    // Send timeout: a stalled send is abandoned after two seconds and the
    // client is closed with 1011.
    let mut stalled = connect(&broker, 4, EntryMode::Browser, true);
    broker.publish(&event, &one);
    // Draining lets the writer start its send before the clock moves.
    assert!(stalled.drain().await.is_empty());
    advance(Duration::from_secs(2) - SETTLE * 3).await;
    assert!(stalled.drain().await.is_empty());
    assert_eq!(stalled.cleanups(), 0, "the send bound is two seconds");
    advance(SETTLE).await;
    assert_closed_1011(&stalled.drain().await, "send timeout");
    assert_eq!(stalled.cleanups(), 1);
    assert_eq!(texts(&healthy.drain().await), vec![CONFIG_EVENT]);
}

#[tokio::test(start_paused = true)]
async fn retirement_is_once_and_reconnect_is_fresh() {
    let broker = BroadcastBroker::default();
    let event = json!({"type": "config_event", "revision": 1});
    let one = batch(&[CONFIG_EVENT]);

    // Overflow and disconnect land together while a send timeout is pending.
    let mut failing = connect(&broker, 1, EntryMode::Browser, true);
    broker.publish(&event, &one);
    assert!(failing.drain().await.is_empty());
    for _ in 0..=MAX_QUEUED_BATCHES {
        broker.publish(&event, &one);
    }
    broker.disconnect(&failing.registration);
    broker.disconnect(&failing.registration);
    advance(Duration::from_secs(4)).await;
    assert_closed_1011(&failing.drain().await, "overflow before disconnect");
    assert_eq!(failing.cleanups(), 1);

    // A peer that went away fails its send, which retires it before the
    // caller's own disconnect arrives.
    let mut gone = connect(&broker, 2, EntryMode::Browser, false);
    gone.rx.close();
    broker.publish(&event, &one);
    assert!(gone.drain().await.is_empty());
    assert_eq!(gone.cleanups(), 1, "send failure retires the peer");
    broker.disconnect(&gone.registration);
    advance(Duration::from_secs(4)).await;
    assert_eq!(gone.cleanups(), 1);

    // A live handle cannot be registered twice.
    let mut direct = connect(&broker, 10, EntryMode::Direct, false);
    let mut browser = connect(&broker, 11, EntryMode::Browser, false);
    let duplicate = FakeSink {
        tx: unbounded_channel().0,
        stall: false,
        stalled: false,
    };
    assert!(
        broker
            .register(
                direct.registration.id(),
                EntryMode::Direct,
                duplicate,
                || {}
            )
            .is_err()
    );

    let wildcard = r#"{"type": "subscribe", "events": ["*"]}"#;
    let session = r#"{"type": "subscribe", "events": ["session_message"]}"#;
    assert_eq!(broker.receive(&direct.registration, wildcard), None);
    assert_eq!(broker.receive(&browser.registration, session), None);
    assert_eq!(direct.drain().await.len(), 1);
    assert_eq!(browser.drain().await.len(), 1);
    broker.disconnect(&direct.registration);
    broker.disconnect(&browser.registration);
    let missed = json!({"type": "session_message", "session_id": "s-1"});
    let missed_frame = r#"{"type": "session_message", "session_id": "s-1"}"#;
    broker.publish(&missed, &batch(&[missed_frame]));
    assert!(direct.drain().await.is_empty());
    assert_eq!((direct.cleanups(), browser.cleanups()), (1, 1));

    // The same handles reconnect with their entry mode's initial state and no
    // replay: direct starts absent, browser starts initialized and empty.
    let stale = direct.registration;
    let mut direct = connect(&broker, 10, EntryMode::Direct, false);
    let mut browser = connect(&broker, 11, EntryMode::Browser, false);
    broker.publish(&missed, &batch(&[missed_frame]));
    broker.publish(&event, &one);
    assert!(direct.drain().await.is_empty());
    assert_eq!(texts(&browser.drain().await), vec![CONFIG_EVENT]);

    // The retired registration cannot subscribe, reply to or retire the
    // connection that now holds its handle.
    assert_eq!(broker.receive(&stale, wildcard), None);
    broker.reply(&stale, batch(&[missed_frame]));
    broker.disconnect(&stale);
    assert!(
        direct.drain().await.is_empty(),
        "stale registration reached the reconnected handle"
    );
    assert_eq!(
        direct.cleanups(),
        0,
        "stale registration retired the reconnected handle"
    );
    broker.reply(&direct.registration, batch(&[CONFIG_EVENT]));
    assert_eq!(texts(&direct.drain().await), vec![CONFIG_EVENT]);
}

#[tokio::test(start_paused = true)]
async fn direct_reply_bypasses_event_filter() {
    let broker = BroadcastBroker::default();
    let event = json!({"type": "config_event", "revision": 1});
    // Absent subscriptions filter every broadcast, never a direct reply.
    let mut direct = connect(&broker, 1, EntryMode::Direct, false);
    let mut browser = connect(&broker, 2, EntryMode::Browser, false);
    let reply = r#"{"type": "session_message", "request_id": "r-1"}"#;

    broker.publish(&event, &batch(&[CONFIG_EVENT]));
    broker.reply(&direct.registration, batch(&[reply]));
    broker.reply(&browser.registration, batch(&[reply]));
    broker.publish(&event, &batch(&[CONFIG_EVENT]));
    assert_eq!(texts(&direct.drain().await), vec![reply]);
    assert_eq!(
        texts(&browser.drain().await),
        vec![CONFIG_EVENT, reply, CONFIG_EVENT]
    );

    // A rejected control is answered on the same writer; an application
    // frame is handed back unchanged and answers nothing.
    let invalid = r#"{"type": "error", "code": "ERROR", "message": "Invalid JSON format"}"#;
    assert_eq!(broker.receive(&direct.registration, "{not json"), None);
    let application = r#"{"type":"chat_message","content":"hi"}"#;
    assert_eq!(
        broker.receive(&direct.registration, application),
        Some(application)
    );
    assert_eq!(texts(&direct.drain().await), vec![invalid]);
}

#[tokio::test(start_paused = true)]
async fn typed_batches_keep_order_and_opcode() {
    let broker = BroadcastBroker::default();
    let mut client = connect(&broker, 1, EntryMode::Browser, false);
    // Neither binary payload is valid UTF-8.
    let first = Bytes::from_static(&[0xff, 0xfe, 0x00, 0x80]);
    let last = Bytes::from_static(&[0xc3, 0x28]);
    let task = r#"{"type": "task_event", "event": "created"}"#;
    let session = r#"{"type": "session_event", "event": "updated"}"#;
    let ack = r#"{"type": "subscribe_success", "events": ["session_event"]}"#;

    broker.reply(
        &client.registration,
        Batch::binary(first.clone()).expect("binary batch"),
    );
    broker.reply(&client.registration, batch(&["text reply"]));
    broker.publish(&json!({"type": "task_event"}), &batch(&[task]));
    let subscribe = r#"{"type": "subscribe", "events": ["session_event"]}"#;
    assert_eq!(broker.receive(&client.registration, subscribe), None);
    broker.publish(&json!({"type": "session_event"}), &batch(&[session]));
    broker.reply(
        &client.registration,
        Batch::binary(last.clone()).expect("binary batch"),
    );
    assert_eq!(
        client.drain().await,
        vec![
            Message::Binary(first),
            Message::text("text reply"),
            Message::text(task),
            Message::text(ack),
            Message::text(session),
            Message::Binary(last),
        ]
    );

    // Binary bytes count toward the queue bound: one half-queue batch is in
    // flight on a stalled socket, a second fits, and a third retires it.
    let mut stalled = connect(&broker, 2, EntryMode::Browser, true);
    let half = Batch::binary(Bytes::from(vec![0_u8; MAX_QUEUED_BYTES / 2 + 1])).expect("half");
    broker.reply(&stalled.registration, half.clone());
    assert!(stalled.drain().await.is_empty(), "the first batch stalls");
    broker.reply(&stalled.registration, half.clone());
    assert!(stalled.drain().await.is_empty(), "the second batch queues");
    broker.reply(&stalled.registration, half);
    assert_closed_1011(&stalled.drain().await, "binary byte overflow");
    assert_eq!(stalled.cleanups(), 1);
    assert!(
        Batch::binary(Bytes::from(vec![0_u8; MAX_QUEUED_BYTES + 1])).is_err(),
        "a binary batch larger than a whole queue is refused"
    );
}
