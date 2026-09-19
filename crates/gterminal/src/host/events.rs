//! Bounded, replayable control-plane lifecycle events.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use serde_json::{json, Value};
use tokio::sync::{mpsc, Mutex};

use super::backpressure;
use crate::protocol::EVENT_QUEUE_ENTRIES;

struct EventSubscriber {
    tx: mpsc::Sender<Value>,
    queued_bytes: Arc<AtomicUsize>,
}

struct EventState {
    seq: u64,
    ring: VecDeque<(u64, usize, Value)>,
    ring_bytes: usize,
    subscribers: Vec<EventSubscriber>,
}

#[derive(Clone)]
pub(crate) struct HostEvents {
    epoch: String,
    event_queue_bytes: usize,
    state: Arc<Mutex<EventState>>,
}

/// One accepted `Input`/`Paste` from a granted frame stream.
pub(crate) struct InputActivity {
    pub terminal_id: String,
    pub host_terminal_id: String,
    /// The daemon attachment id that held the grant.
    pub attachment_id: String,
    /// `"input"` or `"paste"`.
    pub kind: &'static str,
    pub bytes: usize,
    /// `"esc"` or `"ctrl_c"` when the whole `Input` payload was that one byte.
    pub interrupt: Option<&'static str>,
}

pub(crate) struct EventReceiver {
    rx: mpsc::Receiver<Value>,
    queued_bytes: Arc<AtomicUsize>,
}

impl EventReceiver {
    pub async fn recv(&mut self) -> Option<Value> {
        let value = self.rx.recv().await?;
        self.queued_bytes
            .fetch_sub(encoded_len(&value), Ordering::AcqRel);
        Some(value)
    }
}

impl HostEvents {
    pub fn new(epoch: String, event_queue_bytes: usize) -> Self {
        Self {
            epoch,
            event_queue_bytes,
            state: Arc::new(Mutex::new(EventState {
                seq: 0,
                ring: VecDeque::new(),
                ring_bytes: 0,
                subscribers: Vec::new(),
            })),
        }
    }

    pub async fn cursor(&self) -> (String, u64) {
        let state = self.state.lock().await;
        (self.epoch.clone(), state.seq)
    }

    pub async fn subscribe(&self, since: Option<u64>) -> (Value, EventReceiver) {
        let (tx, rx) = mpsc::channel(EVENT_QUEUE_ENTRIES);
        let queued_bytes = Arc::new(AtomicUsize::new(0));
        let mut state = self.state.lock().await;
        let gap = since.is_some_and(|cursor| !cursor_is_replayable(&state, cursor));
        if let Some(cursor) = since.filter(|_| !gap) {
            for (_, _, event) in state.ring.iter().filter(|(seq, _, _)| *seq > cursor) {
                if !queue_event(&tx, &queued_bytes, event.clone(), self.event_queue_bytes) {
                    break;
                }
            }
        }
        state.subscribers.push(EventSubscriber {
            tx,
            queued_bytes: queued_bytes.clone(),
        });
        let ack = json!({
            "ok": true,
            "subscribed": true,
            "epoch": self.epoch,
            "seq": state.seq,
            "gap": gap,
        });
        drop(state);
        (ack, EventReceiver { rx, queued_bytes })
    }

    /// Reports one accepted frame-stream write without its payload.
    pub async fn emit_input_activity(&self, activity: InputActivity) {
        self.emit(json!({
            "event": "input_activity",
            "terminal_id": activity.terminal_id,
            "host_terminal_id": activity.host_terminal_id,
            "attachment_id": activity.attachment_id,
            "kind": activity.kind,
            "bytes": activity.bytes,
            "interrupt": activity.interrupt,
        }))
        .await;
    }

    #[cfg(feature = "vt-engine")]
    pub async fn emit_terminal_exited(
        &self,
        terminal_id: String,
        host_terminal_id: String,
        exit_code: Option<u32>,
    ) {
        self.emit(json!({
            "event": "terminal_exited",
            "terminal_id": terminal_id,
            "host_terminal_id": host_terminal_id,
            "exit_code": exit_code,
        }))
        .await;
    }

    async fn emit(&self, mut event: Value) {
        let mut state = self.state.lock().await;
        state.seq = state.seq.saturating_add(1);
        let seq = state.seq;
        if let Some(object) = event.as_object_mut() {
            object.insert("epoch".into(), Value::String(self.epoch.clone()));
            object.insert("seq".into(), Value::from(seq));
        }
        let bytes = encoded_len(&event);
        state.ring.push_back((seq, bytes, event.clone()));
        state.ring_bytes += bytes;
        while state.ring.len() > EVENT_QUEUE_ENTRIES || state.ring_bytes > self.event_queue_bytes {
            if let Some((_, removed, _)) = state.ring.pop_front() {
                state.ring_bytes = state.ring_bytes.saturating_sub(removed);
            }
        }
        state.subscribers.retain(|subscriber| {
            queue_event(
                &subscriber.tx,
                &subscriber.queued_bytes,
                event.clone(),
                self.event_queue_bytes,
            )
        });
    }
}

fn cursor_is_replayable(state: &EventState, cursor: u64) -> bool {
    if cursor > state.seq {
        return false;
    }
    if cursor == state.seq {
        return true;
    }
    state
        .ring
        .front()
        .is_some_and(|(oldest, _, _)| cursor.saturating_add(1) >= *oldest)
}

fn queue_event(
    tx: &mpsc::Sender<Value>,
    queued_bytes: &AtomicUsize,
    event: Value,
    cap: usize,
) -> bool {
    backpressure::queue_event(tx, queued_bytes, event, cap, encoded_len)
}

fn encoded_len(value: &Value) -> usize {
    value.to_string().len() + 1
}
