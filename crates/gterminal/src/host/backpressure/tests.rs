use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::AsyncWrite;
use tokio::sync::mpsc;
use tokio::time::timeout;

use super::{write_outbound, ControlClose, ControlQueue};
use crate::host::config::HostConfig;
use crate::host::events::HostEvents;

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
    assert!(
        !queue.deadline_exceeded(),
        "fresh control entries have not exceeded the deadline"
    );
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
