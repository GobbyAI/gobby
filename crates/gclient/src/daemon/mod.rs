//! Daemon transport boundary used by the workspace and its scripted test double.

mod live;
mod live_reader;
mod rest;
mod ws;

pub use live::{LiveDaemon, BROADCAST_CAPACITY, CONTROL_REQUEST_DEADLINE, REQUEST_DEADLINE};
pub use ws::{
    decode_message, encode_message, message_kind, route_key, RouteKey, WsCodecError, GOLDEN_NAMES,
    TERMINAL_WS_SAFE_INTEGER_MAX,
};

use crate::copy_mode::{paste_payload, PASTE_MAX_BYTES};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, VecDeque};
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;
use thiserror::Error;
use tokio::sync::broadcast;
use tokio::time::Instant;

pub type WsMessage = Value;
pub type WsReply = Value;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum DaemonError {
    #[error("daemon authorization failed")]
    Unauthorized,
    #[error("daemon resource was not found")]
    NotFound,
    #[error("daemon unavailable")]
    Unavailable { retry_after: Option<Duration> },
    #[error("daemon protocol error: {detail}")]
    Protocol { detail: String },
    #[error("a control request is already in flight for this attachment")]
    ControlRequestInFlight,
    #[error("the attachment's control scope has an indeterminate result")]
    ControlScopeIndeterminate,
    #[error("daemon request timed out")]
    Timeout,
}

impl DaemonError {
    /// Compatibility constructor retained for the scripted reducer surface.
    pub fn new(status: u16, code: impl Into<String>, message: impl Into<String>) -> Self {
        let code = code.into();
        let message = message.into();
        match status {
            401 | 403 if code != "paste_too_large" => Self::Unauthorized,
            404 => Self::NotFound,
            500..=599 => Self::Unavailable { retry_after: None },
            _ => Self::Protocol {
                detail: format!("status={status}; code={code}; {message}"),
            },
        }
    }

    pub fn status(&self) -> u16 {
        match self {
            Self::Unauthorized => 401,
            Self::NotFound => 404,
            Self::Unavailable { .. } => 503,
            Self::ControlRequestInFlight | Self::ControlScopeIndeterminate => 409,
            Self::Timeout => 408,
            Self::Protocol { detail } => detail
                .strip_prefix("status=")
                .and_then(|rest| rest.split(';').next())
                .and_then(|raw| raw.parse().ok())
                .unwrap_or(400),
        }
    }

    pub fn code(&self) -> &str {
        match self {
            Self::Unauthorized => "unauthorized",
            Self::NotFound => "not_found",
            Self::Unavailable { .. } => "unavailable",
            Self::Protocol { detail } if detail.contains("paste_too_large") => "paste_too_large",
            Self::Protocol { .. } => "protocol",
            Self::ControlRequestInFlight => "control_request_in_flight",
            Self::ControlScopeIndeterminate => "control_scope_indeterminate",
            Self::Timeout => "timeout",
        }
    }

    pub(crate) fn unavailable() -> Self {
        Self::Unavailable { retry_after: None }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Generation(pub u64);

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    pub daemon_epoch: String,
    #[serde(deserialize_with = "ws::deserialize_safe_u64")]
    pub seq: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Page<T> {
    pub items: Vec<T>,
    #[serde(default)]
    pub next_cursor: Option<String>,
    #[serde(default)]
    pub snapshot: Option<Snapshot>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct TerminalRow {
    #[serde(alias = "id")]
    pub terminal_id: String,
    #[serde(flatten)]
    pub fields: BTreeMap<String, Value>,
}

impl TerminalRow {
    pub fn id(&self) -> &str {
        &self.terminal_id
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct RosterEntry {
    pub entry_id: String,
    #[serde(flatten)]
    pub fields: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Answer {
    pub fingerprint: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub option: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub key: Option<String>,
}

impl Answer {
    pub fn option(fingerprint: impl Into<String>, option: u64) -> Self {
        Self {
            fingerprint: fingerprint.into(),
            option: Some(option),
            ..Self::default()
        }
    }

    pub fn text(fingerprint: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            fingerprint: fingerprint.into(),
            text: Some(text.into()),
            ..Self::default()
        }
    }

    pub(crate) fn response_body(&self, attention_id: &str) -> Value {
        let mut answer = Map::new();
        if let Some(option) = self.option {
            answer.insert("option".into(), option.into());
        }
        if let Some(text) = &self.text {
            answer.insert("text".into(), text.clone().into());
        }
        if let Some(key) = &self.key {
            answer.insert("key".into(), key.clone().into());
        }
        json!({
            "attention_id": attention_id,
            "fingerprint": self.fingerprint,
            "answer": answer,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SpawnRequest {
    #[serde(default = "default_rows")]
    pub rows: u16,
    #[serde(default = "default_cols")]
    pub cols: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(default = "default_command")]
    pub command: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
}

const fn default_rows() -> u16 {
    24
}

const fn default_cols() -> u16 {
    80
}

fn default_command() -> Vec<String> {
    vec!["zsh".into()]
}

impl Default for SpawnRequest {
    fn default() -> Self {
        Self {
            rows: default_rows(),
            cols: default_cols(),
            cwd: None,
            command: default_command(),
            project_id: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpawnOutcome {
    Created {
        terminal_id: String,
        backend: String,
    },
    Refused {
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KillOutcome {
    Killed { terminal_id: String },
    Refused { terminal_id: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscribeSnapshot {
    pub generation: Generation,
    pub ready: bool,
    pub last_error: Option<DaemonError>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum DaemonEvent {
    Terminal {
        daemon_epoch: String,
        seq: u64,
        payload: Value,
    },
    LeaseLost {
        daemon_epoch: String,
        seq: u64,
        payload: Value,
    },
    AttachmentFinalized {
        daemon_epoch: String,
        seq: u64,
        attachment_id: String,
        payload: Value,
    },
    Output(Value),
    Frame(Value),
    AttachHistory(Value),
    ScrollOffsetApplied(Value),
    Attention {
        epoch: String,
        seq: u64,
        payload: Value,
    },
    Message(Value),
    Disconnected {
        generation: Generation,
        error: DaemonError,
    },
    Lagged,
}

// Static dispatch (`Workspace<D>`) is intentional; implementations return Send futures.
#[allow(async_fn_in_trait)]
pub trait Daemon: Send + Sync {
    async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError>;
    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError>;
    async fn respond(
        &self,
        entry: &str,
        attention_id: &str,
        answer: &Answer,
    ) -> Result<(), DaemonError>;
    async fn mark_seen(&self, entry: &str, attention_id: &str) -> Result<(), DaemonError>;
    async fn spawn(&self, req: SpawnRequest) -> Result<SpawnOutcome, DaemonError>;
    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError>;
    fn subscribe(&self) -> (SubscribeSnapshot, broadcast::Receiver<DaemonEvent>);
    async fn send(&self, msg: WsMessage) -> Result<WsReply, DaemonError>;
    async fn notify(&self, msg: WsMessage) -> Result<(), DaemonError>;
    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError>;
    async fn close(&self, deadline: Instant) -> Result<(), DaemonError>;
}

#[derive(Debug)]
struct ScriptedState {
    rest: Vec<String>,
    ws_out: Vec<Value>,
    attention_inbox: VecDeque<Value>,
    roster: Value,
    terminal_pages: VecDeque<Value>,
    live_terminals: Vec<String>,
    spawn_response: Value,
    last_spawn_body: Value,
    respond_status: u16,
    respond_code: String,
    reachable: bool,
    ws_connected: bool,
    closed: bool,
    generation: Generation,
    pty_mutations: u32,
    last_pty_write: Option<String>,
    last_paste_seq: Option<u64>,
}

/// Deterministic in-memory daemon used by reducer tests.
#[derive(Debug)]
pub struct ScriptedDaemon {
    state: Mutex<ScriptedState>,
    events: broadcast::Sender<DaemonEvent>,
}

impl Default for ScriptedDaemon {
    fn default() -> Self {
        Self::new()
    }
}

impl ScriptedDaemon {
    pub fn new() -> Self {
        let (events, _) = broadcast::channel(BROADCAST_CAPACITY);
        Self {
            state: Mutex::new(ScriptedState {
                rest: Vec::new(),
                ws_out: Vec::new(),
                attention_inbox: VecDeque::new(),
                roster: json!({"epoch": "e0", "seq": 0, "entries": []}),
                terminal_pages: VecDeque::new(),
                live_terminals: Vec::new(),
                spawn_response: json!({"success": true}),
                last_spawn_body: Value::Null,
                respond_status: 200,
                respond_code: String::new(),
                reachable: true,
                ws_connected: true,
                closed: false,
                generation: Generation(1),
                pty_mutations: 0,
                last_pty_write: None,
                last_paste_seq: None,
            }),
            events,
        }
    }

    fn state(&self) -> MutexGuard<'_, ScriptedState> {
        self.state.lock().expect("scripted daemon mutex poisoned")
    }

    pub fn rest_paths(&self) -> Vec<String> {
        self.state().rest.clone()
    }

    pub fn ws_sent(&self) -> Vec<Value> {
        self.state().ws_out.clone()
    }

    pub fn ws_sent_types(&self) -> Vec<String> {
        self.state()
            .ws_out
            .iter()
            .filter_map(|message| message.get("type")?.as_str().map(str::to_string))
            .collect()
    }

    pub fn push_attention_event(&self, event: Value) {
        self.state().attention_inbox.push_back(event.clone());
        let _ = self.events.send(DaemonEvent::Message(event));
    }

    pub fn take_attention_events(&self) -> Vec<Value> {
        self.state().attention_inbox.drain(..).collect()
    }

    pub fn set_roster(&self, roster: Value) {
        self.state().roster = roster;
    }

    pub fn roster_value(&self) -> Value {
        self.state().roster.clone()
    }

    pub fn set_terminal_pages(&self, pages: Vec<Value>) {
        self.state().terminal_pages = pages.into();
    }

    pub fn set_live_terminals(&self, ids: Vec<String>) {
        self.state().live_terminals = ids;
    }

    pub fn live_terminals(&self) -> Vec<String> {
        self.state().live_terminals.clone()
    }

    pub fn set_spawn_response(&self, body: Value) {
        self.state().spawn_response = body;
    }

    pub fn last_spawn_body(&self) -> Value {
        self.state().last_spawn_body.clone()
    }

    pub fn set_respond_status(&self, status: u16, code: impl Into<String>) {
        let mut state = self.state();
        state.respond_status = status;
        state.respond_code = code.into();
    }

    pub fn set_reachable(&self, reachable: bool) {
        self.state().reachable = reachable;
    }

    pub fn reachable(&self) -> bool {
        self.state().reachable
    }

    pub fn set_ws_connected(&self, connected: bool) {
        self.state().ws_connected = connected;
    }

    pub fn ws_connected(&self) -> bool {
        self.state().ws_connected
    }

    pub fn pty_mutation_count(&self) -> u32 {
        self.state().pty_mutations
    }

    pub fn last_pty_write(&self) -> Option<String> {
        self.state().last_pty_write.clone()
    }

    pub fn last_paste_seq(&self) -> Option<u64> {
        self.state().last_paste_seq
    }

    pub fn subscribe_scripted(&self) {
        let mut state = self.state();
        state.rest.push("WS subscribe".into());
        if !state.closed {
            state.ws_connected = true;
        }
    }

    pub fn subscribe(&self) {
        self.subscribe_scripted();
    }

    pub fn fetch_attention_roster(&self) -> Result<Value, DaemonError> {
        let mut state = self.state();
        state.rest.push("GET /api/attention/roster".into());
        if !state.reachable {
            return Err(DaemonError::unavailable());
        }
        Ok(state.roster.clone())
    }

    pub fn fetch_terminals_page(
        &self,
        project_id: &str,
        cursor: Option<&str>,
    ) -> Result<Value, DaemonError> {
        let mut state = self.state();
        let mut path = format!("GET /api/terminals?project_id={project_id}&states=pending,live");
        if let Some(cursor) = cursor {
            path.push_str("&cursor=");
            path.push_str(cursor);
        }
        state.rest.push(path);
        if !state.reachable {
            return Err(DaemonError::unavailable());
        }
        Ok(state
            .terminal_pages
            .pop_front()
            .unwrap_or_else(|| json!({"items": [], "next_cursor": null})))
    }

    pub fn respond_scripted(&self, entry_id: &str, _body: Value) -> Result<Value, DaemonError> {
        let mut state = self.state();
        state
            .rest
            .push(format!("POST /api/attention/{entry_id}/respond"));
        if state.respond_status != 200 {
            return Err(DaemonError::new(
                state.respond_status,
                state.respond_code.clone(),
                state.respond_code.clone(),
            ));
        }
        Ok(json!({"ok": true}))
    }

    pub fn respond(&self, entry_id: &str, body: Value) -> Result<Value, DaemonError> {
        self.respond_scripted(entry_id, body)
    }

    pub fn spawn_agent(&self, body: Value) -> Result<Value, DaemonError> {
        let mut state = self.state();
        state.rest.push("POST /api/agents/spawn".into());
        state.last_spawn_body = body;
        Ok(state.spawn_response.clone())
    }

    pub fn send_ws(&self, message: Value) -> Result<(), DaemonError> {
        let mut state = self.state();
        if state.closed || !state.ws_connected || !state.reachable {
            return Err(DaemonError::unavailable());
        }
        encode_message(&message).map_err(|error| DaemonError::Protocol {
            detail: error.to_string(),
        })?;
        match message.get("type").and_then(Value::as_str) {
            Some("terminal_input") => {
                if let Some(data) = message.get("data").and_then(Value::as_str) {
                    state.last_pty_write = Some(data.to_string());
                    state.pty_mutations += 1;
                }
            }
            Some("terminal_paste") => {
                let text = message.get("text").and_then(Value::as_str).unwrap_or("");
                if text.len() > PASTE_MAX_BYTES {
                    return Err(DaemonError::Protocol {
                        detail: "paste_too_large".into(),
                    });
                }
                state.last_paste_seq = message.get("client_write_seq").and_then(Value::as_u64);
                state.last_pty_write = Some(paste_payload(text, true));
                state.pty_mutations += 1;
            }
            _ => {}
        }
        state.ws_out.push(message);
        Ok(())
    }
}

impl Daemon for ScriptedDaemon {
    async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        serde_json::from_value(self.fetch_terminals_page(project, cursor)?).map_err(|error| {
            DaemonError::Protocol {
                detail: error.to_string(),
            }
        })
    }

    async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError> {
        let roster = self.fetch_attention_roster()?;
        serde_json::from_value(roster.get("entries").cloned().unwrap_or_else(|| json!([]))).map_err(
            |error| DaemonError::Protocol {
                detail: error.to_string(),
            },
        )
    }

    async fn respond(
        &self,
        entry: &str,
        attention_id: &str,
        answer: &Answer,
    ) -> Result<(), DaemonError> {
        self.respond_scripted(entry, answer.response_body(attention_id))?;
        Ok(())
    }

    async fn mark_seen(&self, entry: &str, attention_id: &str) -> Result<(), DaemonError> {
        self.state()
            .rest
            .push(format!("POST /api/attention/{entry}/seen:{attention_id}"));
        Ok(())
    }

    async fn spawn(&self, request: SpawnRequest) -> Result<SpawnOutcome, DaemonError> {
        let result = self.spawn_agent(serde_json::to_value(request).map_err(|error| {
            DaemonError::Protocol {
                detail: error.to_string(),
            }
        })?)?;
        if result.get("success").and_then(Value::as_bool) == Some(false) {
            return Ok(SpawnOutcome::Refused {
                reason: result
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("refused")
                    .to_string(),
            });
        }
        Ok(SpawnOutcome::Created {
            terminal_id: result
                .get("terminal_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            backend: result
                .get("backend")
                .and_then(Value::as_str)
                .unwrap_or("native")
                .to_string(),
        })
    }

    async fn terminate(&self, terminal_id: &str) -> Result<KillOutcome, DaemonError> {
        self.send_ws(json!({"type": "terminal_kill", "terminal_id": terminal_id}))?;
        Ok(KillOutcome::Killed {
            terminal_id: terminal_id.to_string(),
        })
    }

    fn subscribe(&self) -> (SubscribeSnapshot, broadcast::Receiver<DaemonEvent>) {
        self.subscribe_scripted();
        let state = self.state();
        (
            SubscribeSnapshot {
                generation: state.generation,
                ready: state.ws_connected,
                last_error: state.closed.then(DaemonError::unavailable),
            },
            self.events.subscribe(),
        )
    }

    async fn send(&self, message: Value) -> Result<Value, DaemonError> {
        self.send_ws(message)?;
        Ok(json!({"ok": true}))
    }

    async fn notify(&self, message: Value) -> Result<(), DaemonError> {
        self.send_ws(message)
    }

    async fn reconnect(&self, observed: Generation) -> Result<Generation, DaemonError> {
        let mut state = self.state();
        if state.closed {
            return Err(DaemonError::unavailable());
        }
        if state.generation == observed || !state.ws_connected {
            state.generation.0 += 1;
        }
        state.ws_connected = true;
        Ok(state.generation)
    }

    async fn close(&self, _deadline: Instant) -> Result<(), DaemonError> {
        let mut state = self.state();
        state.closed = true;
        state.ws_connected = false;
        Ok(())
    }
}
