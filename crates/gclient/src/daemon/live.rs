use super::live_reader::{
    connect_socket, run_connection, Outbound, WRITE_CANCELLED, WRITE_QUEUED, WRITE_STARTED,
};
use super::rest::RestClient;
use super::{
    route_key, Answer, Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, KillOutcome,
    Page, ProjectRow, RosterEntry, RouteKey, RunRow, SessionRow, SourceStatus, SpawnOutcome,
    SpawnRequest, SubscribeSnapshot, TerminalRow, WorkspaceOp, WorkspaceReply, WorkspaceSnapshot,
    WorktreeRow,
};
use futures_util::future::{AbortHandle, Abortable};
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
    pub async fn new(
        base_url: impl AsRef<str>,
        token: impl Into<String>,
    ) -> Result<Self, DaemonError> {
        let base_url = Url::parse(base_url.as_ref()).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })?;
        let token = token.into();
        let rest = RestClient::new(base_url.clone(), token.clone())?;
        let (events, _) = broadcast::channel(BROADCAST_CAPACITY);
        let (closed_tx, _) = watch::channel(false);
        let daemon = Self {
            inner: Arc::new(LiveInner {
                rest,
                base_url,
                token,
                state: Mutex::new(LiveState::default()),
                events,
                reader: Mutex::new(None),
                closed_tx,
                reader_active: AtomicBool::new(false),
                sink_active: AtomicBool::new(false),
                reconnect_active: AtomicBool::new(false),
                close_stall: Mutex::new(None),
                owners: AtomicUsize::new(1),
            }),
        };
        daemon.open_connection(Generation(0)).await?;
        Ok(daemon)
    }

    pub async fn connect(
        base_url: impl AsRef<str>,
        token: impl Into<String>,
    ) -> Result<Self, DaemonError> {
        Self::new(base_url, token).await
    }

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

    async fn open_connection(&self, observed: Generation) -> Result<Generation, DaemonError> {
        let socket = connect_socket(
            &self.inner.base_url,
            &self.inner.token,
            self.inner.closed_tx.subscribe(),
        )
        .await?;
        // The reader guard lives in this block: it must not be held across
        // the subscribe await below.
        let generation = {
            let mut reader = self
                .inner
                .reader
                .lock()
                .expect("live daemon reader mutex poisoned");
            let (outbound, receiver) = mpsc::channel(256);
            let generation = {
                let mut state = self.inner.state();
                if state.closed {
                    return Err(DaemonError::Unavailable { retry_after: None });
                }
                if state.generation != observed {
                    return Ok(state.generation);
                }
                state.generation.0 += 1;
                state.ready = true;
                state.last_error = None;
                state.outbound = Some(outbound);
                state.generation
            };
            let inner = Arc::clone(&self.inner);
            let handle = tokio::spawn(async move {
                run_connection(inner, generation, socket, receiver).await;
            });
            *reader = Some(handle);
            generation
        };
        self.subscribe_events().await?;
        Ok(generation)
    }

    /// Ask for the gated event kinds and wait for the daemon's confirmation.
    /// The daemon delivers those kinds only to a socket that subscribed, so
    /// a connection that skipped this would reduce over silence; waiting for
    /// `subscribe_success` keeps the subscription ahead of the first fetch.
    async fn subscribe_events(&self) -> Result<(), DaemonError> {
        let mut events = self.inner.events.subscribe();
        self.notification(json!({"type": "subscribe", "events": SUBSCRIBED_EVENTS}))
            .await?;
        let deadline = Instant::now() + REQUEST_DEADLINE;
        loop {
            let event = timeout_at(deadline, events.recv())
                .await
                .map_err(|_| request_timed_out("subscribe"))?;
            match event {
                Ok(DaemonEvent::Message(message))
                    if super::message_kind(&message) == Some("subscribe_success") =>
                {
                    return Ok(());
                }
                Ok(DaemonEvent::Disconnected { error, .. }) => return Err(error),
                Ok(_) | Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => {
                    return Err(DaemonError::Unavailable { retry_after: None });
                }
            }
        }
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
        super::encode_message(&message).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })?;
        let request = super::message_kind(&message)
            .unwrap_or("message")
            .to_string();
        let key = route_key(&message).ok_or_else(|| DaemonError::Protocol {
            detail: "request has no correlation key".into(),
        })?;
        let duration = if matches!(key, RouteKey::Control(_)) {
            CONTROL_REQUEST_DEADLINE
        } else {
            REQUEST_DEADLINE
        };
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
                value: message,
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
        let reply = timeout_at(deadline, reply_rx)
            .await
            .map_err(|_| request_timed_out(&request))?
            .map_err(|_| DaemonError::Unavailable { retry_after: None })??;
        guard.armed = false;
        Ok(reply)
    }

    async fn notification(&self, message: Value) -> Result<(), DaemonError> {
        super::encode_message(&message).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })?;
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
                value: message,
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

impl Daemon for LiveDaemon {
    async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        self.inner.rest.list_terminals(project, cursor).await
    }

    async fn inventory_page(
        &self,
        states: &[&str],
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        let reply = self
            .request(json!({
                "type": "terminal_list",
                "request_id": Uuid::new_v4().to_string(),
                "states": states,
                "limit": INVENTORY_PAGE_SIZE,
                "cursor": cursor,
            }))
            .await?;
        serde_json::from_value(reply).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })
    }

    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError> {
        self.inner.rest.roster().await
    }

    async fn projects(&self) -> Result<Vec<ProjectRow>, DaemonError> {
        self.inner.rest.projects().await
    }

    async fn source_status(&self, project: &str) -> Result<SourceStatus, DaemonError> {
        self.inner.rest.source_status(project).await
    }

    async fn worktrees(&self, project: &str) -> Result<Vec<WorktreeRow>, DaemonError> {
        self.inner.rest.worktrees(project).await
    }

    async fn init_project(&self, path: &str) -> Result<ProjectRow, DaemonError> {
        self.inner.rest.init_project(path).await
    }

    async fn create_worktree(
        &self,
        project: &str,
        branch: &str,
        base: Option<&str>,
    ) -> Result<WorktreeRow, DaemonError> {
        self.inner.rest.create_worktree(project, branch, base).await
    }

    async fn delete_worktree(&self, worktree_id: &str) -> Result<(), DaemonError> {
        self.inner.rest.delete_worktree(worktree_id).await
    }

    async fn sessions(&self, project: &str) -> Result<Vec<SessionRow>, DaemonError> {
        self.inner.rest.sessions(project).await
    }

    async fn agent_runs(&self, project: &str) -> Result<Vec<RunRow>, DaemonError> {
        self.inner.rest.agent_runs(project).await
    }

    async fn respond(
        &self,
        entry: &str,
        attention_id: &str,
        answer: &Answer,
    ) -> Result<(), DaemonError> {
        self.inner.rest.respond(entry, attention_id, answer).await
    }

    async fn mark_seen(&self, entry: &str, attention_id: &str) -> Result<(), DaemonError> {
        self.inner.rest.mark_seen(entry, attention_id).await
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnOutcome, DaemonError> {
        let mut message = json!({
            "type": "terminal_create",
            "request_id": Uuid::new_v4().to_string(),
            "rows": request.rows,
            "cols": request.cols,
            "command": request.command,
        });
        if let Some(cwd) = request.cwd {
            message["cwd"] = Value::String(cwd);
        }
        if let Some(project_id) = request.project_id {
            message["project_id"] = Value::String(project_id);
        }
        let reply = self.request(message).await?;
        if reply.get("success").and_then(Value::as_bool) != Some(true) {
            return Ok(SpawnOutcome::Refused {
                reason: reply
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("refused")
                    .to_string(),
            });
        }
        Ok(SpawnOutcome::Created {
            terminal_id: required_string(&reply, "terminal_id")?,
            backend: required_string(&reply, "backend")?,
        })
    }

    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError> {
        let reply = self
            .request(json!({
                "type": "terminal_kill",
                "request_id": Uuid::new_v4().to_string(),
                "terminal_id": terminal_id,
            }))
            .await?;
        if reply.get("success").and_then(Value::as_bool) == Some(true) {
            Ok(KillOutcome::Killed {
                terminal_id: terminal_id.to_string(),
            })
        } else {
            Ok(KillOutcome::Refused {
                terminal_id: terminal_id.to_string(),
            })
        }
    }

    fn subscribe(&self) -> (SubscribeSnapshot, EventReceiver) {
        let receiver = self.inner.events.subscribe().into();
        let state = self.inner.state();
        (
            SubscribeSnapshot {
                generation: state.generation,
                ready: state.ready,
                last_error: state.last_error.clone(),
            },
            receiver,
        )
    }

    async fn send(&self, message: Value) -> Result<Value, DaemonError> {
        self.request(message).await
    }

    async fn notify(&self, message: Value) -> Result<(), DaemonError> {
        self.notification(message).await
    }

    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError> {
        let (mut joiner, owner, observed) = {
            let mut state = self.inner.state();
            if state.closed {
                return Err(DaemonError::Unavailable { retry_after: None });
            }
            if state.generation != observed && state.ready {
                return Ok(state.generation);
            }
            // A stale caller with no ready connection and no flight still needs
            // a connection: a handshake that failed after rolling the generation
            // forward would otherwise strand every later attempt on the cached
            // error (#22002).
            let observed = state.generation;
            if let Some(flight) = &state.reconnect {
                (Some(flight.result_rx.clone()), None, observed)
            } else {
                let (result_tx, result_rx) = watch::channel(None);
                let done = Arc::new(Notify::new());
                let (abort, registration) = AbortHandle::new_pair();
                state.reconnect = Some(ReconnectFlight {
                    observed,
                    result_tx: result_tx.clone(),
                    result_rx,
                    done: Arc::clone(&done),
                    abort,
                });
                (None, Some((result_tx, done, registration)), observed)
            }
        };
        if let Some(receiver) = joiner.as_ref() {
            if let Some(result) = receiver.borrow().clone() {
                return result;
            }
        }
        if let Some(receiver) = joiner.as_mut() {
            receiver
                .changed()
                .await
                .map_err(|_| DaemonError::Unavailable { retry_after: None })?;
            return receiver
                .borrow()
                .clone()
                .unwrap_or(Err(DaemonError::Unavailable { retry_after: None }));
        }
        let (result_tx, done, registration) = owner.expect("reconnect owner or joiner");
        self.inner.reconnect_active.store(true, Ordering::Release);
        let mut owner_guard = ReconnectOwnerGuard {
            daemon: self,
            observed,
            result_tx: result_tx.clone(),
            done: Arc::clone(&done),
            armed: true,
        };
        let mut result = Abortable::new(self.open_connection(observed), registration)
            .await
            .unwrap_or(Err(DaemonError::Unavailable { retry_after: None }));
        {
            let mut state = self.inner.state();
            if state.closed {
                result = Err(DaemonError::Unavailable { retry_after: None });
            }
            if result.is_err() {
                state.ready = false;
                state.last_error = result.as_ref().err().cloned();
            }
            result_tx.send_replace(Some(result.clone()));
            if state
                .reconnect
                .as_ref()
                .is_some_and(|flight| flight.observed == observed)
            {
                state.reconnect = None;
            }
        }
        done.notify_one();
        self.inner.reconnect_active.store(false, Ordering::Release);
        owner_guard.armed = false;
        result
    }

    async fn close(&self, deadline: Instant) -> Result<(), DaemonError> {
        let (outbound, reconnect, reader) = {
            let mut reader_slot = self
                .inner
                .reader
                .lock()
                .expect("live daemon reader mutex poisoned");
            let mut state = self.inner.state();
            if state.closed {
                return Ok(());
            }
            state.closed = true;
            state.ready = false;
            let reconnect = state.reconnect.as_ref().map(|flight| {
                flight
                    .result_tx
                    .send_replace(Some(Err(DaemonError::Unavailable { retry_after: None })));
                (Arc::clone(&flight.done), flight.abort.clone())
            });
            let reader = reader_slot.take();
            (state.outbound.take(), reconnect, reader)
        };
        let mut resources = CloseResources {
            inner: Arc::clone(&self.inner),
            reader,
            reconnect_abort: reconnect.as_ref().map(|(_, abort)| abort.clone()),
            armed: true,
        };
        self.inner
            .fail_waiters(DaemonError::Unavailable { retry_after: None });
        if deadline <= Instant::now() {
            return Err(DaemonError::timeout("close"));
        }
        let close_result = if let Some(outbound) = outbound {
            let (done, done_rx) = oneshot::channel();
            match timeout_at(deadline, outbound.send(Outbound::Close { done })).await {
                Err(_) => Err(DaemonError::timeout("close")),
                Ok(Err(_)) => Ok(()),
                Ok(Ok(())) => match timeout_at(deadline, done_rx).await {
                    Err(_) => Err(DaemonError::timeout("close")),
                    Ok(_) => Ok(()),
                },
            }
        } else {
            Ok(())
        };
        close_result?;
        if let Some(handle) = resources.reader.as_mut() {
            if timeout_at(deadline, &mut *handle).await.is_err() {
                return Err(DaemonError::timeout("close"));
            }
        }
        if let Some((done, abort)) = reconnect {
            self.inner
                .stall_close_stage(CloseStage::ReconnectJoin)
                .await;
            abort.abort();
            timeout_at(deadline, done.notified())
                .await
                .map_err(|_| DaemonError::timeout("close"))?;
        }
        self.inner.closed_tx.send_replace(true);
        resources.armed = false;
        Ok(())
    }

    async fn attach_workspace(
        &self,
        node: Option<&str>,
        workspace: Option<&str>,
    ) -> Result<WorkspaceSnapshot, DaemonError> {
        self.attach_workspace_live(node, workspace).await
    }

    async fn workspace_op(&self, op: WorkspaceOp) -> Result<WorkspaceReply, DaemonError> {
        self.workspace_op_live(op).await
    }
}

struct CloseResources {
    inner: Arc<LiveInner>,
    reader: Option<JoinHandle<()>>,
    reconnect_abort: Option<AbortHandle>,
    armed: bool,
}

impl Drop for CloseResources {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.inner.closed_tx.send_replace(true);
        if let Some(abort) = self.reconnect_abort.take() {
            abort.abort();
        }
        if let Some(reader) = self.reader.take() {
            reader.abort();
        }
    }
}

struct ReconnectOwnerGuard<'a> {
    daemon: &'a LiveDaemon,
    observed: Generation,
    result_tx: watch::Sender<Option<Result<Generation, DaemonError>>>,
    done: Arc<Notify>,
    armed: bool,
}

impl Drop for ReconnectOwnerGuard<'_> {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.daemon
            .inner
            .reconnect_active
            .store(false, Ordering::Release);
        let error = DaemonError::Unavailable { retry_after: None };
        {
            let mut state = self.daemon.inner.state();
            state.ready = false;
            state.last_error = Some(error.clone());
            if state
                .reconnect
                .as_ref()
                .is_some_and(|flight| flight.observed == self.observed)
            {
                state.reconnect = None;
            }
        }
        self.result_tx.send_replace(Some(Err(error)));
        self.done.notify_one();
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
