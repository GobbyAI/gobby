//! Per-observer byte accounting, lag close, and control/event caps.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::io::{AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, Notify};
use tokio::time::timeout;

use crate::protocol::{write_message, ServerMessage};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlClose {
    Deadline,
    Overflow,
    Disconnected,
}

struct ControlEntry {
    value: Value,
    queued_at: Instant,
}

pub struct ControlQueue {
    cap: usize,
    deadline: Duration,
    entries: VecDeque<ControlEntry>,
}

impl ControlQueue {
    pub fn new(cap: usize, deadline: Duration) -> Self {
        Self {
            cap,
            deadline,
            entries: VecDeque::new(),
        }
    }

    pub fn push(&mut self, value: Value) -> Result<(), ControlClose> {
        if self.entries.len() >= self.cap {
            return Err(ControlClose::Overflow);
        }
        self.entries.push_back(ControlEntry {
            value,
            queued_at: Instant::now(),
        });
        Ok(())
    }

    pub fn pop(&mut self) -> Option<Value> {
        self.entries.pop_front().map(|entry| entry.value)
    }

    pub fn deadline_exceeded(&self) -> bool {
        self.entries
            .front()
            .is_some_and(|entry| entry.queued_at.elapsed() >= self.deadline)
    }
}

pub async fn write_outbound<W: AsyncWrite + Unpin>(
    mut writer: W,
    mut rx: mpsc::Receiver<Value>,
    deadline: Duration,
) -> ControlClose {
    while let Some(value) = rx.recv().await {
        let mut line = value.to_string();
        if line.len() >= 2 * 1024 * 1024 {
            let id = value.get("id").cloned();
            let mut too_large = serde_json::json!({"ok": false, "error": "response_too_large"});
            if let (Some(id), Some(object)) = (id, too_large.as_object_mut()) {
                object.insert("id".into(), id);
            }
            line = too_large.to_string();
        }
        line.push('\n');
        match timeout(deadline, writer.write_all(line.as_bytes())).await {
            Ok(Ok(())) => {
                if writer.flush().await.is_err() {
                    return ControlClose::Overflow;
                }
            }
            Ok(Err(_)) => return ControlClose::Overflow,
            Err(_) => {
                let _ = timeout(
                    Duration::from_millis(20),
                    writer.write_all(b"{\"ok\":false,\"error\":\"control_deadline\"}\n"),
                )
                .await;
                return ControlClose::Deadline;
            }
        }
    }
    ControlClose::Disconnected
}

pub fn enqueue_control(tx: &mpsc::Sender<Value>, value: Value) -> Result<(), ControlClose> {
    tx.try_send(value).map_err(|err| match err {
        mpsc::error::TrySendError::Full(_) => ControlClose::Overflow,
        mpsc::error::TrySendError::Closed(_) => ControlClose::Disconnected,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushResult {
    Queued,
    Overflow,
    Closed,
}

struct FrameInner {
    items: VecDeque<(usize, ServerMessage)>,
    queued_bytes: usize,
    last_drain: Instant,
    closed: bool,
    writing: bool,
}

struct FrameShared {
    inner: Mutex<FrameInner>,
    notify: Notify,
}

#[derive(Clone)]
pub struct FrameMailbox {
    shared: Arc<FrameShared>,
}

impl FrameMailbox {
    pub fn new() -> Self {
        Self {
            shared: Arc::new(FrameShared {
                inner: Mutex::new(FrameInner {
                    items: VecDeque::new(),
                    queued_bytes: 0,
                    last_drain: Instant::now(),
                    closed: false,
                    writing: false,
                }),
                notify: Notify::new(),
            }),
        }
    }

    pub fn queued_bytes(&self) -> usize {
        self.lock().queued_bytes
    }

    pub fn try_push(&self, msg: &ServerMessage, cap: usize) -> PushResult {
        let bytes = encoded_message_bytes(msg);
        let mut inner = self.lock();
        if inner.closed {
            return PushResult::Closed;
        }
        if inner.queued_bytes.saturating_add(bytes) > cap {
            return PushResult::Overflow;
        }
        inner.queued_bytes = inner.queued_bytes.saturating_add(bytes);
        inner.items.push_back((bytes, msg.clone()));
        drop(inner);
        self.shared.notify.notify_waiters();
        PushResult::Queued
    }

    pub fn replace_with_keyframe(&self, msg: &ServerMessage, cap: usize) {
        let bytes = encoded_message_bytes(msg);
        let mut inner = self.lock();
        if inner.closed {
            return;
        }
        if bytes > cap {
            return;
        }
        inner.items.clear();
        inner.queued_bytes = bytes;
        inner.items.push_back((bytes, msg.clone()));
        drop(inner);
        self.shared.notify.notify_waiters();
    }

    pub fn force_push(&self, msg: ServerMessage, cap: usize) {
        let bytes = encoded_message_bytes(&msg);
        let mut inner = self.lock();
        if inner.closed {
            return;
        }
        if bytes > cap {
            return;
        }
        while inner.queued_bytes.saturating_add(bytes) > cap {
            let Some((dropped, _)) = inner.items.pop_front() else {
                break;
            };
            inner.queued_bytes = inner.queued_bytes.saturating_sub(dropped);
        }
        if inner.queued_bytes.saturating_add(bytes) > cap {
            return;
        }
        inner.queued_bytes = inner.queued_bytes.saturating_add(bytes);
        inner.items.push_back((bytes, msg));
        drop(inner);
        self.shared.notify.notify_waiters();
    }

    pub fn try_pop(&self) -> Option<ServerMessage> {
        let mut inner = self.lock();
        let (bytes, msg) = inner.items.pop_front()?;
        inner.queued_bytes = inner.queued_bytes.saturating_sub(bytes);
        inner.writing = true;
        Some(msg)
    }

    pub async fn recv(&self) -> Option<ServerMessage> {
        loop {
            let notified = self.shared.notify.notified();
            if let Some(msg) = self.try_pop() {
                return Some(msg);
            }
            if self.lock().closed {
                return None;
            }
            notified.await;
        }
    }

    pub fn note_drain(&self) {
        let mut inner = self.lock();
        inner.writing = false;
        inner.last_drain = Instant::now();
    }

    pub fn is_lagged(&self, timeout: Duration) -> bool {
        let inner = self.lock();
        !inner.closed
            && (inner.queued_bytes > 0 || inner.writing)
            && inner.last_drain.elapsed() >= timeout
    }

    pub fn close_with(&self, msg: ServerMessage, cap: usize) {
        let bytes = encoded_message_bytes(&msg);
        let mut inner = self.lock();
        inner.items.clear();
        if bytes <= cap {
            inner.queued_bytes = bytes;
            inner.items.push_back((bytes, msg));
        } else {
            inner.queued_bytes = 0;
        }
        inner.closed = true;
        inner.writing = false;
        drop(inner);
        self.shared.notify.notify_waiters();
    }

    pub fn close(&self) {
        let mut inner = self.lock();
        inner.closed = true;
        drop(inner);
        self.shared.notify.notify_waiters();
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, FrameInner> {
        self.shared
            .inner
            .lock()
            .unwrap_or_else(|err| err.into_inner())
    }
}

pub fn encoded_message_bytes(msg: &ServerMessage) -> usize {
    let mut buf = Vec::new();
    write_message(&mut buf, msg)
        .map(|()| buf.len())
        .unwrap_or(0)
}

pub fn queue_event(
    tx: &mpsc::Sender<Value>,
    queued_bytes: &std::sync::atomic::AtomicUsize,
    event: Value,
    cap: usize,
    encoded_len: impl Fn(&Value) -> usize,
) -> bool {
    use std::sync::atomic::Ordering;
    let bytes = encoded_len(&event);
    let previous = queued_bytes.fetch_add(bytes, Ordering::AcqRel);
    if previous.saturating_add(bytes) > cap {
        queued_bytes.fetch_sub(bytes, Ordering::AcqRel);
        let overflow = serde_json::json!({"ok": false, "error": "event_overflow"});
        let overflow_bytes = encoded_len(&overflow);
        queued_bytes.fetch_add(overflow_bytes, Ordering::AcqRel);
        if tx.try_send(overflow).is_err() {
            queued_bytes.fetch_sub(overflow_bytes, Ordering::AcqRel);
        }
        return false;
    }
    if tx.try_send(event).is_err() {
        queued_bytes.fetch_sub(bytes, Ordering::AcqRel);
        return false;
    }
    true
}

#[cfg(test)]
#[path = "backpressure/tests.rs"]
mod tests;
