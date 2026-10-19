//! Bounded broadcast fanout to authenticated logical connections.
//!
//! Each registered connection owns its subscription state and a FIFO of
//! batches drained by its own writer task. Subscription updates and
//! publication selection share one short state lock that is released before
//! any socket I/O, so a client's acknowledgement is queued ahead of every
//! publication its new state selects. Publishing only enqueues: each selected
//! connection either queues the batch or is retired, and a stalled socket
//! never delays another. Retirement happens once and runs the caller's
//! cleanup once; nothing is replayed to a later connection.

use std::collections::VecDeque;
use std::collections::hash_map::{self, HashMap};
use std::pin::{Pin, pin};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use bytes::Bytes;
use futures_util::{Sink, SinkExt};
use serde_json::Value;
use tokio::sync::Notify;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::{Message, Utf8Bytes};

use super::subscriptions::{EntryMode, Inbound, SubscriptionState};

/// Pending batches one connection may hold behind its in-flight batch.
pub const MAX_QUEUED_BATCHES: usize = 16;
/// Frame bytes one connection may hold queued, and the largest batch. Matches
/// gclient's aggregate assembly bound.
pub const MAX_QUEUED_BYTES: usize = 64 * 1024 * 1024;
/// Longest one frame send may stall before its client is retired.
const SEND_TIMEOUT: Duration = Duration::from_secs(2);
/// Longest the 1011 close of a retired client may take.
const CLOSE_TIMEOUT: Duration = Duration::from_secs(1);

/// One authenticated logical connection, named by the admitting caller. The
/// broker never discovers principals or admits clients itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ConnectionId(u64);

impl ConnectionId {
    pub fn new(id: u64) -> Self {
        Self(id)
    }

    pub fn get(self) -> u64 {
        self.0
    }
}

/// One frame in a batch, in the form the writer sends it.
#[derive(Debug, Clone)]
enum Frame {
    Text(Utf8Bytes),
    Binary(Bytes),
    Ping,
    Close(CloseFrame),
}

impl Frame {
    fn message(&self) -> Message {
        match self {
            Self::Text(text) => Message::Text(text.clone()),
            Self::Binary(bytes) => Message::Binary(bytes.clone()),
            Self::Ping => Message::Ping(Bytes::new()),
            Self::Close(frame) => Message::Close(Some(frame.clone())),
        }
    }
}

/// Immutable frames delivered together, in order. Clones share the frame
/// bytes, so every recipient of a publication holds the same copy.
#[derive(Debug, Clone)]
pub struct Batch {
    frames: Arc<[Frame]>,
    bytes: usize,
}

/// The batch is larger than a whole connection queue, so it could never fit.
#[derive(Debug, PartialEq, Eq)]
pub struct BatchTooLarge;

impl Batch {
    pub fn new(frames: Vec<String>) -> Result<Self, BatchTooLarge> {
        let bytes = frames.iter().map(String::len).sum();
        if bytes > MAX_QUEUED_BYTES {
            return Err(BatchTooLarge);
        }
        let frames = frames
            .into_iter()
            .map(|text| Frame::Text(Utf8Bytes::from(text)))
            .collect();
        Ok(Self { frames, bytes })
    }

    /// One text frame, bounded like a whole queue.
    pub fn text(frame: Utf8Bytes) -> Result<Self, BatchTooLarge> {
        let bytes = frame.len();
        if bytes > MAX_QUEUED_BYTES {
            return Err(BatchTooLarge);
        }
        Ok(Self::with(Frame::Text(frame), bytes))
    }

    /// One binary frame, bounded like a whole queue.
    pub fn binary(frame: Bytes) -> Result<Self, BatchTooLarge> {
        let bytes = frame.len();
        if bytes > MAX_QUEUED_BYTES {
            return Err(BatchTooLarge);
        }
        Ok(Self::with(Frame::Binary(frame), bytes))
    }

    fn single(frame: Utf8Bytes) -> Self {
        let bytes = frame.len();
        Self::with(Frame::Text(frame), bytes)
    }

    /// A keepalive ping, which costs no queue bytes.
    fn ping() -> Self {
        Self::with(Frame::Ping, 0)
    }

    /// An ordered close, which costs no queue bytes.
    fn close(frame: CloseFrame) -> Self {
        Self::with(Frame::Close(frame), 0)
    }

    fn with(frame: Frame, bytes: usize) -> Self {
        Self {
            frames: Arc::from([frame]),
            bytes,
        }
    }
}

/// The connection handle is already registered and live.
#[derive(Debug, PartialEq, Eq)]
pub struct AlreadyRegistered;

/// One registration of a connection handle. The caller drives that
/// connection through it, and it never reaches a later registration that
/// reuses the handle.
#[derive(Clone)]
pub struct Registration {
    id: ConnectionId,
    link: Arc<Link>,
}

impl Registration {
    pub fn id(&self) -> ConnectionId {
        self.id
    }
}

impl std::fmt::Debug for Registration {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registration")
            .field("id", &self.id)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone)]
enum Retirement {
    Overflow,
    SendTimeout,
    Disconnected,
    /// A close the caller chose, sent to the peer as given.
    Closed(CloseFrame),
}

impl Retirement {
    /// Close frame for a peer that may still read it; a disconnected peer
    /// gets none.
    fn close_frame(&self) -> Option<CloseFrame> {
        let (code, reason) = match self {
            Self::Overflow => (CloseCode::Error, "Broadcast queue overflow"),
            Self::SendTimeout => (CloseCode::Error, "Broadcast send timed out"),
            Self::Disconnected => return None,
            Self::Closed(frame) => return Some(frame.clone()),
        };
        Some(CloseFrame {
            code,
            reason: Utf8Bytes::from_static(reason),
        })
    }
}

#[derive(Default)]
struct Queue {
    batches: VecDeque<Batch>,
    bytes: usize,
    retirement: Option<Retirement>,
}

/// Shared by one connection's registry entry and its writer task.
#[derive(Default)]
struct Link {
    queue: Mutex<Queue>,
    ready: Notify,
    retired: Notify,
}

impl Link {
    fn lock(&self) -> MutexGuard<'_, Queue> {
        self.queue.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Queues `batch` unless it would cross either queue limit.
    fn push(&self, batch: &Batch) -> bool {
        let mut queue = self.lock();
        if queue.batches.len() >= MAX_QUEUED_BATCHES || queue.bytes + batch.bytes > MAX_QUEUED_BYTES
        {
            return false;
        }
        queue.bytes += batch.bytes;
        queue.batches.push_back(batch.clone());
        drop(queue);
        self.ready.notify_one();
        true
    }

    fn pop(&self) -> Result<Option<Batch>, Retirement> {
        let mut queue = self.lock();
        if let Some(retirement) = &queue.retirement {
            return Err(retirement.clone());
        }
        let batch = queue.batches.pop_front();
        if let Some(batch) = &batch {
            queue.bytes -= batch.bytes;
        }
        Ok(batch)
    }

    /// Records the first retirement and returns it; later calls only observe.
    fn retire(&self, reason: Retirement) -> Retirement {
        let mut queue = self.lock();
        let first = queue.retirement.get_or_insert(reason).clone();
        queue.batches.clear();
        queue.bytes = 0;
        drop(queue);
        self.retired.notify_one();
        first
    }
}

struct Entry {
    state: SubscriptionState,
    link: Arc<Link>,
}

type Entries = HashMap<ConnectionId, Entry>;

/// Retires a registered connection; an absent one is already retired.
fn retire(entries: &mut Entries, id: ConnectionId, reason: Retirement) {
    if let Some(entry) = entries.remove(&id) {
        entry.link.retire(reason);
    }
}

/// The registry entry `registration` still owns. A retired registration owns
/// none, even when a fresh connection has since reused its handle.
fn owned<'a>(entries: &'a mut Entries, registration: &Registration) -> Option<&'a mut Entry> {
    entries
        .get_mut(&registration.id)
        .filter(|entry| Arc::ptr_eq(&entry.link, &registration.link))
}

/// Queues `batch` for one connection, retiring it when the batch does not fit.
fn deliver(entries: &mut Entries, registration: &Registration, batch: &Batch) {
    if owned(entries, registration).is_some_and(|entry| !entry.link.push(batch)) {
        retire(entries, registration.id, Retirement::Overflow);
    }
}

/// Fanout to every registered connection, shared by both public entries.
#[derive(Clone, Default)]
pub struct BroadcastBroker {
    entries: Arc<Mutex<Entries>>,
}

impl BroadcastBroker {
    fn entries(&self) -> MutexGuard<'_, Entries> {
        self.entries.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Registers an authenticated connection in its entry mode's initial
    /// state and starts its writer, which must run inside a Tokio runtime.
    /// `cleanup` runs once, after the connection is retired.
    pub fn register<S>(
        &self,
        id: ConnectionId,
        entry: EntryMode,
        sink: S,
        cleanup: impl FnOnce() + Send + 'static,
    ) -> Result<Registration, AlreadyRegistered>
    where
        S: Sink<Message> + Send + 'static,
    {
        let registration = Registration {
            id,
            link: Arc::new(Link::default()),
        };
        match self.entries().entry(id) {
            hash_map::Entry::Occupied(_) => return Err(AlreadyRegistered),
            hash_map::Entry::Vacant(vacant) => {
                vacant.insert(Entry {
                    state: SubscriptionState::new(entry),
                    link: Arc::clone(&registration.link),
                });
            }
        }
        tokio::spawn(write(self.clone(), registration.clone(), sink, cleanup));
        Ok(registration)
    }

    /// Resolves one inbound text frame. A subscription control is answered
    /// on the connection's writer in the same critical section as its state
    /// change, and `None` is returned; an application frame is returned
    /// unchanged. A retired connection's frames are dropped.
    pub fn receive<'a>(&self, registration: &Registration, text: &'a str) -> Option<&'a str> {
        let mut entries = self.entries();
        let entry = owned(&mut entries, registration)?;
        let reply = match entry.state.apply_subscription_request(text) {
            Inbound::Application(frame) => return Some(frame),
            Inbound::Ack(ack) => Utf8Bytes::from(ack),
            Inbound::Rejected(error) => Utf8Bytes::from_static(error),
        };
        deliver(&mut entries, registration, &Batch::single(reply));
        None
    }

    /// Queues `batch` for every connection whose subscriptions select
    /// `filter`, the original message even when the frames are fragments.
    /// Returns once each selected connection has queued it or been retired.
    pub fn publish(&self, filter: &Value, batch: &Batch) {
        let mut entries = self.entries();
        let overflowed: Vec<ConnectionId> = entries
            .iter()
            .filter(|(_, entry)| entry.state.matches_broadcast(filter) && !entry.link.push(batch))
            .map(|(id, _)| *id)
            .collect();
        for id in overflowed {
            retire(&mut entries, id, Retirement::Overflow);
        }
    }

    /// Queues a direct reply on the connection's writer, bypassing the
    /// subscription filter.
    pub fn reply(&self, registration: &Registration, batch: Batch) {
        deliver(&mut self.entries(), registration, &batch);
    }

    /// Adds server-side subscriptions to one live connection. A retired
    /// connection is ignored.
    pub fn add_subscriptions(&self, id: ConnectionId, events: Vec<String>) {
        if let Some(entry) = self.entries().get_mut(&id) {
            entry.state.add_server_subscriptions(events);
        }
    }

    /// Queues a keepalive ping behind the connection's pending frames.
    pub fn ping(&self, registration: &Registration) {
        deliver(&mut self.entries(), registration, &Batch::ping());
    }

    /// Retires `registration` now, with a close frame that skips the queue.
    pub fn retire_with(&self, registration: &Registration, code: CloseCode, reason: &'static str) {
        let frame = CloseFrame {
            code,
            reason: Utf8Bytes::from_static(reason),
        };
        self.retire_registration(registration, Retirement::Closed(frame));
    }

    /// Ends `registration` in order: its close frame is queued behind the
    /// frames already pending, and the connection leaves the registry at once
    /// so no publication reaches it after this call.
    pub fn close(&self, registration: &Registration, frame: CloseFrame) {
        let mut entries = self.entries();
        if owned(&mut entries, registration).is_none() {
            return;
        }
        entries.remove(&registration.id);
        if !registration.link.push(&Batch::close(frame.clone())) {
            registration.link.retire(Retirement::Closed(frame));
        }
    }

    /// Registered connections.
    pub fn connection_count(&self) -> usize {
        self.entries().len()
    }

    /// Retires a connection whose peer has gone; it gets no close frame.
    pub fn disconnect(&self, registration: &Registration) {
        self.retire_registration(registration, Retirement::Disconnected);
    }

    /// Retires `registration` and returns the first recorded reason. The
    /// registry entry is removed only while this registration still owns it,
    /// so a fresh connection reusing the handle is untouched.
    fn retire_registration(&self, registration: &Registration, reason: Retirement) -> Retirement {
        let mut entries = self.entries();
        if owned(&mut entries, registration).is_some() {
            entries.remove(&registration.id);
        }
        drop(entries);
        registration.link.retire(reason)
    }
}

async fn write<S>(
    broker: BroadcastBroker,
    registration: Registration,
    sink: S,
    cleanup: impl FnOnce(),
) where
    S: Sink<Message>,
{
    let link = &registration.link;
    let mut sink = pin!(sink);
    let retirement = loop {
        match link.pop() {
            Err(retirement) => break retirement,
            Ok(Some(batch)) => {
                if let Some(retirement) =
                    send_batch(&broker, &registration, sink.as_mut(), &batch).await
                {
                    break retirement;
                }
            }
            Ok(None) => {
                tokio::select! {
                    () = link.ready.notified() => {}
                    () = link.retired.notified() => {}
                }
            }
        }
    };
    if let Some(frame) = retirement.close_frame() {
        let _ = timeout(CLOSE_TIMEOUT, sink.send(Message::Close(Some(frame)))).await;
    }
    cleanup();
}

/// Sends one batch in order and returns the retirement this writer caused.
/// A retirement from elsewhere abandons the batch and surfaces at the next
/// `pop`.
async fn send_batch<S>(
    broker: &BroadcastBroker,
    registration: &Registration,
    mut sink: Pin<&mut S>,
    batch: &Batch,
) -> Option<Retirement>
where
    S: Sink<Message>,
{
    for frame in batch.frames.iter() {
        let message = frame.message();
        let sent = tokio::select! {
            biased;
            () = registration.link.retired.notified() => return None,
            sent = timeout(SEND_TIMEOUT, sink.send(message)) => sent,
        };
        if matches!(frame, Frame::Close(_)) {
            // An ordered close ends the connection whatever the send returned.
            return Some(broker.retire_registration(registration, Retirement::Disconnected));
        }
        let failure = match sent {
            Ok(Ok(())) => continue,
            Ok(Err(_)) => Retirement::Disconnected,
            Err(_) => Retirement::SendTimeout,
        };
        return Some(broker.retire_registration(registration, failure));
    }
    None
}
