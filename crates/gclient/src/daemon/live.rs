use super::live_reader::{
    connect_socket, encode_frame_text, run_connection, Outbound, WRITE_CANCELLED, WRITE_QUEUED,
    WRITE_STARTED,
};
use super::rest::RestClient;
use super::{
    route_key, Answer, Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, KillOutcome,
    Page, ProjectRow, RosterEntry, RouteKey, RunRow, SessionRow, SourceStatus, SpawnOutcome,
    SpawnRequest, SubscribeSnapshot, TerminalRow, WorkspaceOp, WorkspaceReply, WorkspaceSnapshot,
    WorktreeRow,
};
use futures_util::future::{AbortHandle, Abortable};
use gobby_terminal::terminal_theme::ThemeDeclaration;
use reqwest::Url;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;
use tokio::sync::{broadcast, mpsc, oneshot, watch, Notify};
use tokio::task::JoinHandle;
use tokio::time::{timeout_at, Instant};
use uuid::Uuid;

pub const REQUEST_DEADLINE: Duration = Duration::from_secs(5);
pub const CONTROL_REQUEST_DEADLINE: Duration = Duration::from_secs(2);
pub const BROADCAST_CAPACITY: usize = 256;
/// Rows per WS `terminal_list` inventory page; the daemon caps at 500.
const INVENTORY_PAGE_SIZE: u16 = 200;
/// Gated event kinds the daemon delivers only to a socket that subscribed to
/// them; everything the workspace reduces over is on this list.
pub const SUBSCRIBED_EVENTS: [&str; 5] = [
    "terminal_event",
    "agent_event",
    "project_event",
    "worktree_event",
    "session_event",
];

type ReplySender = oneshot::Sender<Result<Value, DaemonError>>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CloseStage {
    ReconnectJoin,
    ReaderShutdown,
    SinkClose,
}

#[derive(Debug)]
pub(super) struct LiveState {
    pub(super) generation: Generation,
    pub(super) ready: bool,
    pub(super) last_error: Option<DaemonError>,
    pub(super) closed: bool,
    pub(super) outbound: Option<mpsc::Sender<Outbound>>,
    pub(super) requests: HashMap<String, ReplySender>,
    pub(super) writes: HashMap<(String, u64), ReplySender>,
    pub(super) controls: HashMap<String, ReplySender>,
    pub(super) control_write_states: HashMap<String, Arc<AtomicU8>>,
    pub(super) active_attachments: HashSet<String>,
    pub(super) attachment_tombstones: HashSet<String>,
    pub(super) control_tombstones: HashSet<String>,
    /// Request ids of in-flight `workspace_attach`es.
    pub(super) workspace_attaches: HashSet<String>,
    /// The workspace this connection attached; the reader drops every other
    /// workspace's `workspace_event`.
    pub(super) attached_workspace: Option<String>,
    /// The client's terminal colours, sent with every shell spawn this
    /// connection requests so the child's first OSC 10/11 queries answer
    /// with them. `None` until the app sets it; the host then decides.
    pub(super) terminal_theme: Option<ThemeDeclaration>,
    reconnect: Option<ReconnectFlight>,
}

impl Default for LiveState {
    fn default() -> Self {
        Self {
            generation: Generation(0),
            ready: false,
            last_error: None,
            closed: false,
            outbound: None,
            requests: HashMap::new(),
            writes: HashMap::new(),
            controls: HashMap::new(),
            control_write_states: HashMap::new(),
            active_attachments: HashSet::new(),
            attachment_tombstones: HashSet::new(),
            control_tombstones: HashSet::new(),
            workspace_attaches: HashSet::new(),
            attached_workspace: None,
            terminal_theme: None,
            reconnect: None,
        }
    }
}

#[derive(Debug, Clone)]
struct ReconnectFlight {
    observed: Generation,
    result_tx: watch::Sender<Option<Result<Generation, DaemonError>>>,
    result_rx: watch::Receiver<Option<Result<Generation, DaemonError>>>,
    done: Arc<Notify>,
    abort: AbortHandle,
}

#[derive(Debug)]
pub(super) struct LiveInner {
    pub(super) rest: RestClient,
    pub(super) base_url: Url,
    pub(super) token: String,
    pub(super) state: Mutex<LiveState>,
    pub(super) events: broadcast::Sender<DaemonEvent>,
    pub(super) reader: Mutex<Option<JoinHandle<()>>>,
    pub(super) closed_tx: watch::Sender<bool>,
    pub(super) reader_active: AtomicBool,
    pub(super) sink_active: AtomicBool,
    reconnect_active: AtomicBool,
    close_stall: Mutex<Option<CloseStage>>,
    owners: AtomicUsize,
}

impl LiveInner {
    pub(super) fn state(&self) -> MutexGuard<'_, LiveState> {
        self.state.lock().expect("live daemon mutex poisoned")
    }

    pub(super) fn fail_waiters(&self, error: DaemonError) {
        let mut state = self.state();
        for (_, waiter) in state.requests.drain() {
            let _ = waiter.send(Err(error.clone()));
        }
        for (_, waiter) in state.writes.drain() {
            let _ = waiter.send(Err(error.clone()));
        }
        for (_, waiter) in state.controls.drain() {
            let _ = waiter.send(Err(error.clone()));
        }
        state.control_write_states.clear();
    }

    pub(super) async fn stall_close_stage(&self, stage: CloseStage) {
        let should_stall = self
            .close_stall
            .lock()
            .expect("live daemon close-stall mutex poisoned")
            .is_some_and(|configured| configured == stage);
        if should_stall {
            std::future::pending::<()>().await;
        }
    }
}

/// Authenticated live client backed by REST and one terminal-WebSocket reader.
#[derive(Debug)]
pub struct LiveDaemon {
    pub(super) inner: Arc<LiveInner>,
}

impl LiveDaemon {
    pub fn generation(&self) -> Generation {
        self.inner.state().generation
    }

    pub fn ready(&self) -> bool {
        self.inner.state().ready
    }

    pub fn last_error(&self) -> Option<DaemonError> {
        self.inner.state().last_error.clone()
    }

    pub fn pending_counts(&self) -> (usize, usize, usize) {
        let state = self.inner.state();
        (
            state.requests.len(),
            state.writes.len(),
            state.controls.len(),
        )
    }

    pub fn control_write_started(&self, attachment_id: &str) -> bool {
        self.inner
            .state()
            .control_write_states
            .get(attachment_id)
            .is_some_and(|state| state.load(Ordering::Acquire) == WRITE_STARTED)
    }

    #[doc(hidden)]
    pub fn inject_close_stall(&self, stage: &str) {
        let stage = match stage {
            "reconnect-join" => CloseStage::ReconnectJoin,
            "reader-shutdown" => CloseStage::ReaderShutdown,
            "sink-close" => CloseStage::SinkClose,
            other => panic!("unknown live daemon close stage: {other}"),
        };
        *self
            .inner
            .close_stall
            .lock()
            .expect("live daemon close-stall mutex poisoned") = Some(stage);
    }

    #[doc(hidden)]
    pub fn shutdown_resources(&self) -> (bool, bool, bool) {
        (
            self.inner.reconnect_active.load(Ordering::Acquire),
            self.inner.reader_active.load(Ordering::Acquire),
            self.inner.sink_active.load(Ordering::Acquire),
        )
    }

    pub async fn terminal(&self, terminal_id: &str) -> Result<TerminalRow, DaemonError> {
        self.inner.rest.terminal(terminal_id).await
    }

    pub(crate) async fn attention_roster(
        &self,
    ) -> Result<(String, u64, Vec<RosterEntry>), DaemonError> {
        let roster = self.inner.rest.roster_snapshot().await?;
        Ok((roster.epoch, roster.seq, roster.entries))
    }

    fn register_waiter(
        &self,
        key: &RouteKey,
        sender: ReplySender,
        write_state: &Arc<AtomicU8>,
    ) -> Result<mpsc::Sender<Outbound>, DaemonError> {
        let mut state = self.inner.state();
        if state.closed {
            return Err(DaemonError::Unavailable { retry_after: None });
        }
        let outbound = state.outbound.clone().ok_or_else(|| {
            state
                .last_error
                .clone()
                .unwrap_or(DaemonError::Unavailable { retry_after: None })
        })?;
        match key {
            RouteKey::Request(request_id) => {
                if state.requests.contains_key(request_id) {
                    return Err(DaemonError::Protocol {
                        detail: format!("request_id already pending: {request_id}"),
                    });
                }
                state.requests.insert(request_id.clone(), sender);
            }
            RouteKey::Write(attachment_id, sequence) => {
                if state.attachment_tombstones.contains(attachment_id) {
                    return Err(DaemonError::Protocol {
                        detail: format!("attachment is finalized: {attachment_id}"),
                    });
                }
                let key = (attachment_id.clone(), *sequence);
                if state.writes.contains_key(&key) {
                    return Err(DaemonError::Protocol {
                        detail: format!("write key already pending: {}:{}", key.0, key.1),
                    });
                }
                state.writes.insert(key, sender);
            }
            RouteKey::Control(attachment_id) => {
                if state.control_tombstones.contains(attachment_id)
                    || state.attachment_tombstones.contains(attachment_id)
                {
                    return Err(DaemonError::ControlScopeIndeterminate);
                }
                if state.controls.contains_key(attachment_id) {
                    return Err(DaemonError::ControlRequestInFlight);
                }
                state.controls.insert(attachment_id.clone(), sender);
                state
                    .control_write_states
                    .insert(attachment_id.clone(), Arc::clone(write_state));
            }
        }
        Ok(outbound)
    }

    fn remove_waiter(&self, key: &RouteKey, post_write: bool) {
        let mut state = self.inner.state();
        match key {
            RouteKey::Request(request_id) => {
                state.requests.remove(request_id);
            }
            RouteKey::Write(attachment_id, sequence) => {
                state.writes.remove(&(attachment_id.clone(), *sequence));
            }
            RouteKey::Control(attachment_id) => {
                state.controls.remove(attachment_id);
                state.control_write_states.remove(attachment_id);
                if post_write {
                    state.control_tombstones.insert(attachment_id.clone());
                }
            }
        }
    }

    pub(super) async fn request(&self, message: Value) -> Result<Value, DaemonError> {
        self.request_with_deadline(message, None, None).await
    }

    /// `send`, also resolving `written` once the request is on the socket,
    /// so a later request can be ordered behind it without its reply.
    pub async fn send_marking_written(
        &self,
        message: Value,
        written: watch::Sender<bool>,
    ) -> Result<Value, DaemonError> {
        self.request_with_deadline(message, None, Some(written))
            .await
    }

    pub(super) async fn request_with_deadline(
        &self,
        message: Value,
        override_deadline: Option<Duration>,
        on_written: Option<watch::Sender<bool>>,
    ) -> Result<Value, DaemonError> {
        let raw = encode_frame_text(&message)?;
        let request = super::message_kind(&message)
            .unwrap_or("message")
            .to_string();
        let key = route_key(&message).ok_or_else(|| DaemonError::Protocol {
            detail: "request has no correlation key".into(),
        })?;
        let duration = override_deadline.unwrap_or(if matches!(key, RouteKey::Control(_)) {
            CONTROL_REQUEST_DEADLINE
        } else {
            REQUEST_DEADLINE
        });
        let deadline = Instant::now() + duration;
        let (reply_tx, reply_rx) = oneshot::channel();
        let write_state = Arc::new(AtomicU8::new(WRITE_QUEUED));
        let outbound = self.register_waiter(&key, reply_tx, &write_state)?;
        let mut guard = PendingGuard {
            daemon: self,
            key: &key,
            write_state,
            armed: true,
        };
        let (written_tx, written_rx) = oneshot::channel();
        timeout_at(
            deadline,
            outbound.send(Outbound::Message {
                raw,
                write_state: Arc::clone(&guard.write_state),
                written: written_tx,
            }),
        )
        .await
        .map_err(|_| request_timed_out(&request))?
        .map_err(|_| DaemonError::Unavailable { retry_after: None })?;
        timeout_at(deadline, written_rx)
            .await
            .map_err(|_| request_timed_out(&request))?
            .map_err(|_| DaemonError::Unavailable { retry_after: None })?;
        if let Some(on_written) = on_written {
            let _ = on_written.send(true);
        }
        let reply = timeout_at(deadline, reply_rx)
            .await
            .map_err(|_| request_timed_out(&request))?
            .map_err(|_| DaemonError::Unavailable { retry_after: None })??;
        guard.armed = false;
        Ok(reply)
    }

    async fn notification(&self, message: Value) -> Result<(), DaemonError> {
        let raw = encode_frame_text(&message)?;
        let request = super::message_kind(&message)
            .unwrap_or("message")
            .to_string();
        let outbound = {
            let state = self.inner.state();
            if state.closed {
                return Err(DaemonError::Unavailable { retry_after: None });
            }
            if message
                .get("attachment_id")
                .and_then(Value::as_str)
                .is_some_and(|attachment| state.attachment_tombstones.contains(attachment))
            {
                return Err(DaemonError::Protocol {
                    detail: "attachment is finalized".into(),
                });
            }
            state.outbound.clone().ok_or_else(|| {
                state
                    .last_error
                    .clone()
                    .unwrap_or(DaemonError::Unavailable { retry_after: None })
            })?
        };
        let deadline = Instant::now() + REQUEST_DEADLINE;
        let (written, written_rx) = oneshot::channel();
        timeout_at(
            deadline,
            outbound.send(Outbound::Message {
                raw,
                write_state: Arc::new(AtomicU8::new(WRITE_QUEUED)),
                written,
            }),
        )
        .await
        .map_err(|_| request_timed_out(&request))?
        .map_err(|_| DaemonError::Unavailable { retry_after: None })?;
        timeout_at(deadline, written_rx)
            .await
            .map_err(|_| request_timed_out(&request))?
            .map_err(|_| DaemonError::Unavailable { retry_after: None })
    }
}

impl Clone for LiveDaemon {
    fn clone(&self) -> Self {
        self.inner.owners.fetch_add(1, Ordering::Relaxed);
        Self {
            inner: Arc::clone(&self.inner),
        }
    }
}

impl Drop for LiveDaemon {
    fn drop(&mut self) {
        if self.inner.owners.fetch_sub(1, Ordering::AcqRel) != 1 {
            return;
        }
        let reader = self
            .inner
            .reader
            .lock()
            .expect("live daemon reader mutex poisoned")
            .take();
        let reconnect_abort = {
            let mut state = self.inner.state();
            state.closed = true;
            state.ready = false;
            state.outbound = None;
            state.reconnect.as_ref().map(|flight| flight.abort.clone())
        };
        self.inner.closed_tx.send_replace(true);
        self.inner
            .fail_waiters(DaemonError::Unavailable { retry_after: None });
        if let Some(abort) = reconnect_abort {
            abort.abort();
        }
        if let Some(reader) = reader {
            reader.abort();
        }
    }
}

struct PendingGuard<'a> {
    daemon: &'a LiveDaemon,
    key: &'a RouteKey,
    write_state: Arc<AtomicU8>,
    armed: bool,
}

impl Drop for PendingGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            let post_write = self
                .write_state
                .compare_exchange(
                    WRITE_QUEUED,
                    WRITE_CANCELLED,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_err_and(|state| state == WRITE_STARTED);
            self.daemon.remove_waiter(self.key, post_write);
        }
    }
}

fn required_string(value: &Value, field: &str) -> Result<String, DaemonError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| DaemonError::Protocol {
            detail: format!("missing string field {field}"),
        })
}

/// A request that outlived its deadline. Logged here because the loop turns
/// the error into one status line and nothing else records which request
/// went unanswered (#22544).
fn request_timed_out(request: &str) -> DaemonError {
    tracing::warn!(request, "daemon request timed out");
    DaemonError::timeout(request)
}
#[path = "live_connect.rs"]
mod live_connect;
