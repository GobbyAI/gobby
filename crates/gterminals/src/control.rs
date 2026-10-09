//! Control-socket client: newline-delimited JSON requests and replies, plus
//! the host event stream that shares the connection after `subscribe_events`.

use std::collections::{BTreeMap, VecDeque};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;

use serde::Serialize;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_CONTROL_LINE: usize = 2 * 1024 * 1024;
pub const EVENT_QUEUE_ENTRIES: usize = 256;
pub const EVENT_QUEUE_BYTES: usize = 256 * 1024;

#[derive(Debug, thiserror::Error)]
pub enum ControlError {
    #[error("control connection is not authenticated")]
    NotAuthenticated,
    #[error("host protocol {host:?} is unsupported")]
    UnsupportedProtocol { host: Option<u32> },
    #[error("reply id {actual} does not match request id {expected}")]
    UnexpectedResponseId { expected: String, actual: String },
    #[error("host refused: {error}")]
    Host {
        error: String,
        code: Option<String>,
        detail: Option<String>,
    },
    #[error("host epoch changed: expected {expected}, got {actual}")]
    EpochChanged { expected: String, actual: String },
    #[error("malformed host line: {0}")]
    Malformed(String),
    #[error("control line exceeds the control line ceiling")]
    LineTooLong,
    #[error("event buffer overflow")]
    EventOverflow,
    #[error("control connection closed")]
    Closed,
    #[error(transparent)]
    Event(#[from] EventError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EventError {
    #[error("event epoch changed: expected {expected}, got {actual}")]
    EpochChanged { expected: String, actual: String },
    #[error("event seq {seq} is not after {last}")]
    NonMonotonic { last: u64, seq: u64 },
    #[error("events after {cursor} in {epoch} are not replayable; reconcile required")]
    ReconcileRequired { epoch: String, cursor: u64 },
    #[error("unknown host event {0:?}")]
    UnknownEvent(String),
    #[error("malformed host event: {0}")]
    Malformed(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Hello {
    pub host_epoch: String,
    pub version: String,
    pub protocol_version: u32,
    pub capabilities: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Ping {
    pub host_epoch: String,
    pub version: String,
    pub host_pid: u32,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Inventory {
    pub epoch: String,
    pub seq: u64,
    pub terminals: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SpawnRequest {
    pub terminal_id: String,
    pub spawn_key: String,
    pub argv: Vec<String>,
    pub cwd: String,
    pub env: BTreeMap<String, String>,
    pub cols: u16,
    pub rows: u16,
    pub commit_deadline_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reservation_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reserve_key: Option<String>,
}

/// The host's `spawn_prepared` reply: the child exists but is gated until
/// `spawn_commit`, or killed by `spawn_abort`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SpawnPrepared {
    pub host_terminal_id: String,
    pub terminal_id: String,
    pub spawn_key: String,
    pub pgid: i64,
    pub start_time: f64,
    pub reservation_id: Option<String>,
    pub reserve_generation: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteKind {
    Text,
    Key,
    Paste,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteRequest {
    pub host_terminal_id: String,
    pub kind: WriteKind,
    pub data: Vec<u8>,
    pub submit: Option<bool>,
}

/// The `subscribe_events` acknowledgement.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Subscription {
    pub epoch: String,
    pub seq: u64,
    pub gap: bool,
}

impl Subscription {
    pub fn new(epoch: impl Into<String>, seq: u64, gap: bool) -> Self {
        Self {
            epoch: epoch.into(),
            seq,
            gap,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputKind {
    Input,
    Paste,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Interrupt {
    Esc,
    CtrlC,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostEventKind {
    TerminalExited {
        terminal_id: String,
        host_terminal_id: String,
        exit_code: Option<i32>,
    },
    InputActivity {
        terminal_id: String,
        host_terminal_id: String,
        attachment_id: String,
        kind: InputKind,
        bytes: u64,
        interrupt: Option<Interrupt>,
        /// The write reached the child as Enter, submitting its composer.
        submit: bool,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostEvent {
    pub epoch: String,
    pub seq: u64,
    pub kind: HostEventKind,
}

impl HostEvent {
    /// Decode one host event line; unknown events are refused, never skipped.
    pub fn from_value(value: &Value) -> Result<Self, EventError> {
        let event = event_str(value, "event")?;
        let kind = match event.as_str() {
            "terminal_exited" => HostEventKind::TerminalExited {
                terminal_id: event_str(value, "terminal_id")?,
                host_terminal_id: event_str(value, "host_terminal_id")?,
                exit_code: match value.get("exit_code") {
                    None | Some(Value::Null) => None,
                    Some(code) => Some(
                        code.as_i64()
                            .and_then(|code| i32::try_from(code).ok())
                            .ok_or_else(|| EventError::Malformed("exit_code".into()))?,
                    ),
                },
            },
            "input_activity" => HostEventKind::InputActivity {
                terminal_id: event_str(value, "terminal_id")?,
                host_terminal_id: event_str(value, "host_terminal_id")?,
                attachment_id: event_str(value, "attachment_id")?,
                kind: match event_str(value, "kind")?.as_str() {
                    "input" => InputKind::Input,
                    "paste" => InputKind::Paste,
                    _ => return Err(EventError::Malformed("kind".into())),
                },
                bytes: event_u64(value, "bytes")?,
                interrupt: match value.get("interrupt") {
                    None | Some(Value::Null) => None,
                    Some(Value::String(name)) if name == "esc" => Some(Interrupt::Esc),
                    Some(Value::String(name)) if name == "ctrl_c" => Some(Interrupt::CtrlC),
                    Some(_) => return Err(EventError::Malformed("interrupt".into())),
                },
                submit: value
                    .get("submit")
                    .and_then(Value::as_bool)
                    .ok_or_else(|| EventError::Malformed("submit".into()))?,
            },
            _ => return Err(EventError::UnknownEvent(event)),
        };
        Ok(Self {
            epoch: event_str(value, "epoch")?,
            seq: event_u64(value, "seq")?,
            kind,
        })
    }
}

fn event_str(value: &Value, field: &str) -> Result<String, EventError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| EventError::Malformed(field.into()))
}

fn event_u64(value: &Value, field: &str) -> Result<u64, EventError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| EventError::Malformed(field.into()))
}

/// Per-epoch event cursor. `accept` admits only the next contiguous sequence;
/// `commit` records what the caller has durably applied, and a resume replays
/// from the committed cursor. Every rejection leaves the cursor unchanged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventCursor {
    epoch: String,
    delivered: u64,
    committed: u64,
}

impl EventCursor {
    pub fn start(subscription: &Subscription) -> Self {
        Self {
            epoch: subscription.epoch.clone(),
            delivered: subscription.seq,
            committed: subscription.seq,
        }
    }

    /// The cursor to pass as `since` when resubscribing.
    pub fn since(&self) -> u64 {
        self.committed
    }

    pub fn accept(&mut self, event: &HostEvent) -> Result<(), EventError> {
        if event.epoch != self.epoch {
            return Err(EventError::EpochChanged {
                expected: self.epoch.clone(),
                actual: event.epoch.clone(),
            });
        }
        if event.seq <= self.delivered {
            return Err(EventError::NonMonotonic {
                last: self.delivered,
                seq: event.seq,
            });
        }
        if event.seq != self.delivered + 1 {
            return Err(self.reconcile());
        }
        self.delivered = event.seq;
        Ok(())
    }

    pub fn commit(&mut self, seq: u64) -> Result<(), EventError> {
        if seq > self.delivered {
            return Err(EventError::NonMonotonic {
                last: self.delivered,
                seq,
            });
        }
        if seq < self.committed {
            return Err(EventError::NonMonotonic {
                last: self.committed,
                seq,
            });
        }
        self.committed = seq;
        Ok(())
    }

    /// Adopt a resubscription made with `since()`. A changed epoch or a host
    /// that cannot replay from the cursor requires reconciliation. Resubscribe
    /// on a fresh connection: see [`ControlClient::subscribe_events`].
    pub fn resume(&mut self, subscription: &Subscription) -> Result<(), EventError> {
        if subscription.epoch != self.epoch {
            return Err(EventError::EpochChanged {
                expected: self.epoch.clone(),
                actual: subscription.epoch.clone(),
            });
        }
        if subscription.gap || subscription.seq < self.committed {
            return Err(self.reconcile());
        }
        self.delivered = self.committed;
        Ok(())
    }

    fn reconcile(&self) -> EventError {
        EventError::ReconcileRequired {
            epoch: self.epoch.clone(),
            cursor: self.committed,
        }
    }
}

/// Host errors that leave the connection's `operation_seq` unconsumed.
const UNCONSUMED_SEQ_ERRORS: &[&str] =
    &["operation_gap", "operation_seq_required", "host_draining"];

/// A control-socket client over any byte stream. Requests are issued one at
/// a time, so state-changing verbs carry this connection's monotonic
/// `operation_seq` in order. Event lines that arrive while a reply is awaited
/// are buffered within gterm's event queue ceilings.
///
/// Not cancellation-safe: dropping an in-flight request future can leave a
/// partial line, an unread reply, or `operation_seq` out of step with the
/// host's ledger. Discard the client after a cancelled request or a ledger
/// error (`operation_expired`, `operation_conflict`). One known divergence:
/// the host can return `host_draining` for `spawn` after it has recorded the
/// seq; this client treats that error as unconsumed, so its next state-changing
/// request reuses the seq and gets `operation_conflict` once.
#[derive(Debug)]
pub struct ControlClient<S> {
    stream: BufReader<S>,
    next_id: u64,
    next_seq: u64,
    host_epoch: Option<String>,
    events: VecDeque<Value>,
    event_bytes: usize,
}

impl<S: AsyncRead + AsyncWrite + Unpin> ControlClient<S> {
    pub fn new(stream: S) -> Self {
        Self {
            stream: BufReader::new(stream),
            next_id: 1,
            next_seq: 1,
            host_epoch: None,
            events: VecDeque::new(),
            event_bytes: 0,
        }
    }

    pub async fn hello(&mut self, control_token: &str) -> Result<Hello, ControlError> {
        let reply = self
            .roundtrip(
                "hello",
                json!({"protocol_version": PROTOCOL_VERSION, "control_token": control_token}),
            )
            .await?;
        let version = reply
            .get("protocol_version")
            .and_then(Value::as_u64)
            .map(|version| u32::try_from(version).unwrap_or(u32::MAX));
        if version != Some(PROTOCOL_VERSION) {
            return Err(ControlError::UnsupportedProtocol { host: version });
        }
        let hello = Hello {
            host_epoch: reply_str(&reply, "host_epoch")?,
            version: reply_str(&reply, "version")?,
            protocol_version: PROTOCOL_VERSION,
            capabilities: reply
                .get("capabilities")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default(),
        };
        self.host_epoch = Some(hello.host_epoch.clone());
        Ok(hello)
    }

    pub async fn ping(&mut self) -> Result<Ping, ControlError> {
        let reply = self.request("ping", json!({})).await?;
        let host_epoch = reply_str(&reply, "host_epoch")?;
        self.require_epoch(&host_epoch)?;
        Ok(Ping {
            host_epoch,
            version: reply_str(&reply, "version")?,
            host_pid: reply_u64(&reply, "host_pid")?
                .try_into()
                .map_err(|_| ControlError::Malformed("host_pid".into()))?,
        })
    }

    pub async fn list(&mut self) -> Result<Inventory, ControlError> {
        let reply = self.request("list", json!({})).await?;
        let epoch = reply_str(&reply, "epoch")?;
        self.require_epoch(&epoch)?;
        Ok(Inventory {
            epoch,
            seq: reply_u64(&reply, "seq")?,
            terminals: reply
                .get("terminals")
                .and_then(Value::as_array)
                .cloned()
                .ok_or_else(|| ControlError::Malformed("terminals".into()))?,
        })
    }

    pub async fn spawn(&mut self, request: &SpawnRequest) -> Result<SpawnPrepared, ControlError> {
        let fields = serde_json::to_value(request)
            .map_err(|err| ControlError::Malformed(err.to_string()))?;
        let reply = self.mutating("spawn", fields).await?;
        Ok(SpawnPrepared {
            host_terminal_id: reply_str(&reply, "host_terminal_id")?,
            terminal_id: reply_str(&reply, "terminal_id")?,
            spawn_key: reply_str(&reply, "spawn_key")?,
            pgid: reply
                .get("pgid")
                .and_then(Value::as_i64)
                .ok_or_else(|| ControlError::Malformed("pgid".into()))?,
            start_time: reply
                .get("start_time")
                .and_then(Value::as_f64)
                .ok_or_else(|| ControlError::Malformed("start_time".into()))?,
            reservation_id: reply
                .get("reservation_id")
                .and_then(Value::as_str)
                .map(str::to_owned),
            reserve_generation: reply.get("reserve_generation").and_then(Value::as_u64),
        })
    }

    pub async fn spawn_commit(
        &mut self,
        terminal_id: &str,
        spawn_key: &str,
        commit_deadline_ms: Option<u64>,
    ) -> Result<(), ControlError> {
        let mut fields = json!({"terminal_id": terminal_id, "spawn_key": spawn_key});
        if let Some(deadline) = commit_deadline_ms {
            fields["commit_deadline_ms"] = json!(deadline);
        }
        self.request("spawn_commit", fields).await.map(drop)
    }

    /// gterm has no abort verb: a prepared child is aborted by killing its
    /// host terminal, exactly as the Python runtime does.
    pub async fn spawn_abort(
        &mut self,
        prepared: &SpawnPrepared,
        grace_ms: u64,
    ) -> Result<(), ControlError> {
        self.kill(&prepared.host_terminal_id, grace_ms).await
    }

    pub async fn kill(
        &mut self,
        host_terminal_id: &str,
        grace_ms: u64,
    ) -> Result<(), ControlError> {
        self.mutating(
            "kill",
            json!({"host_terminal_id": host_terminal_id, "grace_ms": grace_ms}),
        )
        .await
        .map(drop)
    }

    pub async fn resize(
        &mut self,
        host_terminal_id: &str,
        rows: u16,
        cols: u16,
    ) -> Result<(), ControlError> {
        self.mutating(
            "resize",
            json!({"host_terminal_id": host_terminal_id, "rows": rows, "cols": cols}),
        )
        .await
        .map(drop)
    }

    pub async fn write(&mut self, request: &WriteRequest) -> Result<Value, ControlError> {
        let kind = match request.kind {
            WriteKind::Text => "text",
            WriteKind::Key => "key",
            WriteKind::Paste => "paste",
        };
        let mut fields = json!({
            "host_terminal_id": request.host_terminal_id,
            "kind": kind,
            "encoding": "utf8-b64",
            "data": BASE64.encode(&request.data),
        });
        if let Some(submit) = request.submit {
            fields["submit"] = json!(submit);
        }
        self.mutating("write", fields).await
    }

    pub async fn reserve_observer(
        &mut self,
        terminal_id: &str,
        reserve_key: &str,
    ) -> Result<Value, ControlError> {
        self.request(
            "reserve_observer",
            json!({"terminal_id": terminal_id, "reserve_key": reserve_key}),
        )
        .await
    }

    pub async fn release_observer(
        &mut self,
        reservation_id: &str,
        reserve_key: &str,
    ) -> Result<Value, ControlError> {
        self.request(
            "release_observer",
            json!({"reservation_id": reservation_id, "reserve_key": reserve_key}),
        )
        .await
    }

    /// Subscribe this connection to host events, replaying after `since`.
    /// Subscribe once per connection. Each call adds another host-side
    /// subscriber on this connection, so a second subscribe double-delivers
    /// every later event and its replay collides with events already
    /// buffered here; resume with [`EventCursor::resume`] on a fresh client.
    pub async fn subscribe_events(
        &mut self,
        since: Option<u64>,
    ) -> Result<Subscription, ControlError> {
        let fields = since.map_or_else(|| json!({}), |since| json!({"since": since}));
        let reply = self.request("subscribe_events", fields).await?;
        let epoch = reply_str(&reply, "epoch")?;
        self.require_epoch(&epoch)?;
        Ok(Subscription {
            epoch,
            seq: reply_u64(&reply, "seq")?,
            gap: reply.get("gap").and_then(Value::as_bool).unwrap_or(false),
        })
    }

    /// The next event, in arrival order. A reply with no request pending is
    /// a protocol violation.
    pub async fn next_event(&mut self) -> Result<HostEvent, ControlError> {
        let value = match self.events.pop_front() {
            Some(value) => {
                self.event_bytes -= encoded_len(&value);
                value
            }
            None => {
                let value = self.read_line().await?;
                if !is_event(&value) {
                    return Err(ControlError::Malformed("reply without a request".into()));
                }
                value
            }
        };
        Ok(HostEvent::from_value(&value)?)
    }

    fn require_epoch(&self, epoch: &str) -> Result<(), ControlError> {
        match &self.host_epoch {
            Some(expected) if expected != epoch => Err(ControlError::EpochChanged {
                expected: expected.clone(),
                actual: epoch.to_owned(),
            }),
            _ => Ok(()),
        }
    }

    async fn mutating(&mut self, method: &str, mut fields: Value) -> Result<Value, ControlError> {
        let seq = self.next_seq;
        fields["operation_seq"] = json!(seq);
        let result = self.request(method, fields).await;
        let consumed = match &result {
            Ok(_) => true,
            Err(ControlError::Host { error, .. }) => {
                !UNCONSUMED_SEQ_ERRORS.contains(&error.as_str())
            }
            Err(_) => false,
        };
        if consumed {
            self.next_seq = seq + 1;
        }
        result
    }

    async fn request(&mut self, method: &str, fields: Value) -> Result<Value, ControlError> {
        if self.host_epoch.is_none() {
            return Err(ControlError::NotAuthenticated);
        }
        self.roundtrip(method, fields).await
    }

    async fn roundtrip(&mut self, method: &str, mut fields: Value) -> Result<Value, ControlError> {
        let id = self.next_id.to_string();
        self.next_id += 1;
        let object = fields
            .as_object_mut()
            .ok_or_else(|| ControlError::Malformed("request fields".into()))?;
        object.insert("id".into(), json!(id));
        object.insert("method".into(), json!(method));
        let mut line =
            serde_json::to_vec(&fields).map_err(|err| ControlError::Malformed(err.to_string()))?;
        line.push(b'\n');
        if line.len() > MAX_CONTROL_LINE {
            return Err(ControlError::LineTooLong);
        }
        self.stream.write_all(&line).await?;
        self.stream.flush().await?;
        loop {
            let reply = self.read_line().await?;
            if is_event(&reply) {
                self.buffer_event(reply)?;
                continue;
            }
            match reply.get("id") {
                Some(Value::String(actual)) if *actual == id => {}
                Some(actual) => {
                    return Err(ControlError::UnexpectedResponseId {
                        expected: id,
                        actual: actual.to_string(),
                    });
                }
                // gterm sends id-less errors only for unparseable lines.
                None => {
                    raise_for_reply(&reply)?;
                    return Err(ControlError::Malformed("reply without id".into()));
                }
            }
            raise_for_reply(&reply)?;
            return Ok(reply);
        }
    }

    fn buffer_event(&mut self, event: Value) -> Result<(), ControlError> {
        let bytes = encoded_len(&event);
        if self.events.len() >= EVENT_QUEUE_ENTRIES || self.event_bytes + bytes > EVENT_QUEUE_BYTES
        {
            return Err(ControlError::EventOverflow);
        }
        self.event_bytes += bytes;
        self.events.push_back(event);
        Ok(())
    }

    async fn read_line(&mut self) -> Result<Value, ControlError> {
        let mut line = Vec::new();
        let limit = MAX_CONTROL_LINE as u64 + 1;
        let read = (&mut self.stream)
            .take(limit)
            .read_until(b'\n', &mut line)
            .await?;
        if read == 0 {
            return Err(ControlError::Closed);
        }
        // gterm counts the newline toward MAX_CONTROL_LINE.
        if line.len() > MAX_CONTROL_LINE {
            return Err(ControlError::LineTooLong);
        }
        if line.last() != Some(&b'\n') {
            return Err(ControlError::Closed);
        }
        serde_json::from_slice(&line).map_err(|err| ControlError::Malformed(err.to_string()))
    }
}

fn is_event(value: &Value) -> bool {
    value.get("event").is_some() && value.get("id").is_none()
}

fn encoded_len(value: &Value) -> usize {
    value.to_string().len()
}

fn raise_for_reply(reply: &Value) -> Result<(), ControlError> {
    if reply.get("ok") != Some(&Value::Bool(false)) {
        return Ok(());
    }
    let text = |field: &str| reply.get(field).and_then(Value::as_str).map(str::to_owned);
    Err(ControlError::Host {
        error: text("error").unwrap_or_else(|| "error".into()),
        code: text("code"),
        detail: text("detail"),
    })
}

fn reply_str(reply: &Value, field: &str) -> Result<String, ControlError> {
    reply
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| ControlError::Malformed(field.into()))
}

fn reply_u64(reply: &Value, field: &str) -> Result<u64, ControlError> {
    reply
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| ControlError::Malformed(field.into()))
}
