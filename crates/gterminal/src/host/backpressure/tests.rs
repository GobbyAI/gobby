use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::AsyncWrite;
use tokio::sync::mpsc;
use tokio::time::timeout;

use super::{
    encoded_message_bytes, enqueue_control, write_outbound, ControlClose, ControlQueue,
    FrameMailbox, PushResult,
};
use crate::host::config::HostConfig;
use crate::host::events::HostEvents;
use crate::protocol::{ServerMessage, DELTA_QUEUE_BYTES, MAX_DELTA_QUEUE_BYTES, MAX_FRAME_SIZE};

struct PendingWriter;

impl AsyncWrite for PendingWriter {
    fn poll_write(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        _buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Poll::Pending
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
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

    let mut queue = ControlQueue::new(4, Duration::from_millis(20));
    queue.push(json!({"id": 1, "ok": true})).expect("first");
    queue.push(json!({"id": 2, "ok": true})).expect("second");
    queue.push(json!({"id": 3, "ok": true})).expect("third");
    let first = queue.pop().expect("pop first");
    let second = queue.pop().expect("pop second");
    let third = queue.pop().expect("pop third");
    assert_eq!(first["id"], 1);
    assert_eq!(second["id"], 2);
    assert_eq!(third["id"], 3);
    assert_ne!(first, second);
    assert_ne!(second, third);

    let (tx, rx) = mpsc::channel(4);
    tx.try_send(json!({"ok": true, "n": 1})).expect("enqueue");
    let close = timeout(
        Duration::from_millis(100),
        write_outbound(PendingWriter, rx, Duration::from_millis(20)),
    )
    .await
    .expect("writer finishes at the test-local deadline");
    assert_eq!(close, ControlClose::Deadline);

    let events = HostEvents::new("epoch".into(), 256);
    let (_ack, mut rx) = events.subscribe(None).await;
    for seq in 0..300u32 {
        events
            .emit_terminal_exited("term".into(), "host-term".into(), Some(seq))
            .await;
    }
    let mut saw_overflow = false;
    while let Some(event) = rx.recv().await {
        if event.get("error").and_then(Value::as_str) == Some("event_overflow") {
            saw_overflow = true;
            break;
        }
    }
    assert!(
        saw_overflow,
        "an overflowing event subscriber must receive event_overflow"
    );
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
    let mut test_local = HostConfig::default();
    test_local.delta_queue_bytes = 4096;
    test_local
        .validate()
        .expect("4096 admits an 8x24 semantic keyframe and remains a valid test-local cap");
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
