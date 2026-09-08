use super::live::{CloseStage, LiveInner, LiveState};
use super::{decode_message, encode_message, message_kind, route_key, DaemonError, DaemonEvent};
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use reqwest::Url;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot, watch};
use tokio::time::{interval, timeout, Instant, MissedTickBehavior};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::header::{HeaderValue, AUTHORIZATION};
use tokio_tungstenite::tungstenite::protocol::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

const FRAGMENT_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_ASSEMBLY_BYTES: usize = 16 * 1024 * 1024;
const MAX_AGGREGATE_BYTES: usize = 64 * 1024 * 1024;
pub(super) const WRITE_QUEUED: u8 = 0;
pub(super) const WRITE_STARTED: u8 = 1;
pub(super) const WRITE_CANCELLED: u8 = 2;

pub(super) type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Debug)]
pub(super) enum Outbound {
    Message {
        value: Value,
        write_state: Arc<AtomicU8>,
        written: oneshot::Sender<()>,
    },
    Close {
        done: oneshot::Sender<()>,
    },
}

struct ConnectionResources {
    inner: Arc<LiveInner>,
    sink_active: bool,
}

impl ConnectionResources {
    fn new(inner: Arc<LiveInner>) -> Self {
        inner.reader_active.store(true, Ordering::Release);
        inner.sink_active.store(true, Ordering::Release);
        Self {
            inner,
            sink_active: true,
        }
    }

    fn sink_closed(&mut self) {
        self.inner.sink_active.store(false, Ordering::Release);
        self.sink_active = false;
    }
}

impl Drop for ConnectionResources {
    fn drop(&mut self) {
        if self.sink_active {
            self.inner.sink_active.store(false, Ordering::Release);
        }
        self.inner.reader_active.store(false, Ordering::Release);
    }
}

pub(super) async fn connect_socket(
    base_url: &Url,
    token: &str,
    mut closed: watch::Receiver<bool>,
) -> Result<Socket, DaemonError> {
    if *closed.borrow() {
        return Err(DaemonError::Unavailable { retry_after: None });
    }
    let mut url = base_url.clone();
    let scheme = match url.scheme() {
        "http" => "ws",
        "https" => "wss",
        "ws" | "wss" => url.scheme(),
        other => {
            return Err(DaemonError::Protocol {
                detail: format!("unsupported daemon URL scheme: {other}"),
            });
        }
    };
    let scheme = scheme.to_string();
    url.set_scheme(&scheme)
        .map_err(|()| DaemonError::Protocol {
            detail: "failed to create daemon WebSocket URL".into(),
        })?;
    url.set_query(None);
    url.set_fragment(None);
    url.set_path("/ws");

    let mut request = url.as_str().into_client_request().map_err(protocol_error)?;
    let authorization =
        HeaderValue::from_str(&format!("Bearer {token}")).map_err(protocol_error)?;
    request.headers_mut().insert(AUTHORIZATION, authorization);
    let connection = timeout(
        super::REQUEST_DEADLINE,
        tokio_tungstenite::connect_async(request),
    );
    let (socket, _) = tokio::select! {
        result = connection => result
            .map_err(|_| DaemonError::Timeout)?
            .map_err(|error| match error {
                tokio_tungstenite::tungstenite::Error::Http(response)
                    if matches!(response.status().as_u16(), 401 | 403) =>
                {
                    DaemonError::Unauthorized
                }
                tokio_tungstenite::tungstenite::Error::Http(response)
                    if response.status().is_client_error() =>
                {
                    DaemonError::Protocol {
                        detail: format!("WebSocket handshake returned {}", response.status()),
                    }
                }
                _ => DaemonError::Unavailable { retry_after: None },
            })?,
        _ = closed.changed() => return Err(DaemonError::Unavailable { retry_after: None }),
    };
    Ok(socket)
}

pub(super) async fn run_connection(
    inner: Arc<LiveInner>,
    generation: super::Generation,
    socket: Socket,
    mut outbound: mpsc::Receiver<Outbound>,
) {
    let mut resources = ConnectionResources::new(Arc::clone(&inner));
    let (mut sink, mut stream) = socket.split();
    let mut fragments = FragmentAssembler::default();
    let mut sweep = interval(Duration::from_millis(100));
    sweep.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let disconnect_error = loop {
        tokio::select! {
            command = outbound.recv() => {
                match command {
                    Some(Outbound::Message { value, write_state, written }) => {
                        let raw = match encode_message(&value)
                            .and_then(|bytes| String::from_utf8(bytes)
                                .map_err(|error| super::WsCodecError::Json(
                                    serde_json::Error::io(std::io::Error::new(
                                        std::io::ErrorKind::InvalidData,
                                        error,
                                    )),
                                ))) {
                            Ok(raw) => raw,
                            Err(error) => break protocol_error(error),
                        };
                        if write_state
                            .compare_exchange(
                                WRITE_QUEUED,
                                WRITE_STARTED,
                                Ordering::AcqRel,
                                Ordering::Acquire,
                            )
                            .is_err()
                        {
                            let _ = written.send(());
                            continue;
                        }
                        if !matches!(
                            timeout(
                                super::REQUEST_DEADLINE,
                                sink.send(Message::Text(raw.into())),
                            )
                            .await,
                            Ok(Ok(()))
                        ) {
                            break DaemonError::Unavailable { retry_after: None };
                        }
                        let _ = written.send(());
                    }
                    Some(Outbound::Close { done }) => {
                        inner.stall_close_stage(CloseStage::SinkClose).await;
                        let _ = timeout(super::REQUEST_DEADLINE, sink.close()).await;
                        resources.sink_closed();
                        let _ = done.send(());
                        inner
                            .stall_close_stage(CloseStage::ReaderShutdown)
                            .await;
                        return;
                    }
                    None => break DaemonError::Unavailable { retry_after: None },
                }
            }
            incoming = stream.next() => {
                let message = match incoming {
                    Some(Ok(message)) => message,
                    Some(Err(_)) | None => break DaemonError::Unavailable { retry_after: None },
                };
                let value = match decode_frame(message) {
                    Ok(Some(value)) => value,
                    Ok(None) => continue,
                    Err(error) => break error,
                };
                if matches!(
                    message_kind(&value),
                    Some("terminal_detach_result" | "terminal_attachment_finalized")
                ) {
                    if let Some(attachment) = value.get("attachment_id").and_then(Value::as_str) {
                        fragments.drop_attachment(attachment);
                    }
                }
                let value = if message_kind(&value) == Some("terminal_ws_fragment") {
                    if value
                        .get("attachment_id")
                        .and_then(Value::as_str)
                        .is_some_and(|attachment| is_tombstoned(&inner, attachment))
                    {
                        continue;
                    }
                    match fragments.push(&value) {
                        Ok(Some(value)) => value,
                        Ok(None) => continue,
                        Err(error) => break error,
                    }
                } else {
                    value
                };
                handle_inbound(&inner, value);
            }
            _ = sweep.tick() => {
                fragments.expire(Instant::now());
            }
        }
    };
    disconnect(&inner, generation, disconnect_error);
}

fn decode_frame(message: Message) -> Result<Option<Value>, DaemonError> {
    match message {
        Message::Text(text) => decode_message(text.as_bytes())
            .map(Some)
            .map_err(protocol_error),
        Message::Binary(bytes) => decode_message(&bytes).map(Some).map_err(protocol_error),
        Message::Close(frame) => {
            if frame
                .as_ref()
                .is_some_and(|frame| u16::from(frame.code) == 4401)
            {
                Err(DaemonError::Unauthorized)
            } else {
                Err(DaemonError::Unavailable { retry_after: None })
            }
        }
        Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => Ok(None),
    }
}

fn handle_inbound(inner: &LiveInner, value: Value) {
    let kind = message_kind(&value).unwrap_or_default();
    let attachment = value
        .get("attachment_id")
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut reply = None;
    let mut fenced_writes = Vec::new();
    let ignored = {
        let mut state = inner.state();
        if kind == "terminal_attach_result"
            && value.get("success").and_then(Value::as_bool) == Some(true)
        {
            if let Some(attachment) = attachment.as_ref() {
                state.attachment_tombstones.remove(attachment);
                state.control_tombstones.remove(attachment);
                state.active_attachments.insert(attachment.clone());
            }
        }
        if matches!(
            kind,
            "terminal_detach_result" | "terminal_attachment_finalized"
        ) {
            if let Some(attachment) = attachment.as_ref() {
                state.active_attachments.remove(attachment);
                state.attachment_tombstones.insert(attachment.clone());
                state.control_tombstones.remove(attachment);
                let keys: Vec<_> = state
                    .writes
                    .keys()
                    .filter(|(id, _)| id == attachment)
                    .cloned()
                    .collect();
                for key in keys {
                    if let Some(waiter) = state.writes.remove(&key) {
                        fenced_writes.push(waiter);
                    }
                }
            }
        }
        let ignored = attachment
            .as_ref()
            .is_some_and(|id| state.attachment_tombstones.contains(id))
            && !matches!(
                kind,
                "terminal_attach_result"
                    | "terminal_detach_result"
                    | "terminal_attachment_finalized"
            );
        if !ignored {
            if let Some(key) = route_key(&value) {
                reply = match key {
                    super::RouteKey::Request(id) => state.requests.remove(&id),
                    super::RouteKey::Write(id, sequence) => state.writes.remove(&(id, sequence)),
                    super::RouteKey::Control(id) => state.controls.remove(&id),
                };
            }
        }
        ignored
    };
    if ignored {
        return;
    }
    for waiter in fenced_writes {
        let _ = waiter.send(Err(DaemonError::ControlScopeIndeterminate));
    }
    if let Some(reply) = reply {
        if kind == "terminal_attachment_finalized" {
            let _ = reply.send(Err(DaemonError::ControlScopeIndeterminate));
        } else if kind == "terminal_error" {
            let detail = value
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("terminal_error")
                .to_string();
            let _ = reply.send(Err(DaemonError::Protocol { detail }));
        } else {
            let _ = reply.send(Ok(value.clone()));
        }
    }
    if let Some(event) = daemon_event(value) {
        let _ = inner.events.send(event);
    }
}

fn is_tombstoned(inner: &LiveInner, attachment: &str) -> bool {
    inner.state().attachment_tombstones.contains(attachment)
}

fn daemon_event(value: Value) -> Option<DaemonEvent> {
    let epoch = || {
        value
            .get("daemon_epoch")
            .or_else(|| value.get("epoch"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    let sequence = || value.get("seq").and_then(Value::as_u64).unwrap_or(0);
    match message_kind(&value)? {
        "terminal_event" => Some(DaemonEvent::Terminal {
            daemon_epoch: epoch(),
            seq: sequence(),
            payload: value,
        }),
        "terminal_lease_lost" => Some(DaemonEvent::LeaseLost {
            daemon_epoch: epoch(),
            seq: sequence(),
            payload: value,
        }),
        "terminal_attachment_finalized" => Some(DaemonEvent::AttachmentFinalized {
            daemon_epoch: epoch(),
            seq: sequence(),
            attachment_id: value
                .get("attachment_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            payload: value,
        }),
        "terminal_output" => Some(DaemonEvent::Output(value)),
        "terminal_frame" => Some(DaemonEvent::Frame(value)),
        "terminal_attach_history" => Some(DaemonEvent::AttachHistory(value)),
        "terminal_scroll_offset_applied" => Some(DaemonEvent::ScrollOffsetApplied(value)),
        "terminal_resize_result" => Some(DaemonEvent::Message(value)),
        "agent_event"
            if matches!(
                value.get("event").and_then(Value::as_str),
                Some("attention_changed" | "attention_metadata_changed")
            ) =>
        {
            Some(DaemonEvent::Attention {
                epoch: epoch(),
                seq: sequence(),
                payload: value,
            })
        }
        kind if !kind.ends_with("_result") && kind != "terminal_write_outcome" => {
            Some(DaemonEvent::Message(value))
        }
        _ => None,
    }
}

fn disconnect(inner: &LiveInner, generation: super::Generation, error: DaemonError) {
    let should_publish = {
        let mut state = inner.state();
        if state.closed || state.generation != generation || !state.ready {
            false
        } else {
            state.ready = false;
            state.last_error = Some(error.clone());
            state.outbound = None;
            let attachments: Vec<_> = state.active_attachments.drain().collect();
            state
                .attachment_tombstones
                .extend(attachments.iter().cloned());
            state.control_tombstones.extend(attachments);
            fail_waiters(&mut state, error.clone());
            true
        }
    };
    if should_publish {
        let _ = inner
            .events
            .send(DaemonEvent::Disconnected { generation, error });
    }
}

fn fail_waiters(state: &mut LiveState, error: DaemonError) {
    for (_, waiter) in state.requests.drain() {
        let _ = waiter.send(Err(error.clone()));
    }
    for (_, waiter) in state.writes.drain() {
        let _ = waiter.send(Err(error.clone()));
    }
    for (_, waiter) in state.controls.drain() {
        let _ = waiter.send(Err(error.clone()));
    }
}

fn protocol_error(error: impl std::fmt::Display) -> DaemonError {
    DaemonError::Protocol {
        detail: error.to_string(),
    }
}

#[derive(Debug)]
struct Assembly {
    message_seq: u64,
    next_index: u64,
    event: String,
    deadline: Instant,
    bytes: Vec<u8>,
}

#[derive(Debug, Default)]
struct FragmentAssembler {
    by_attachment: HashMap<String, Assembly>,
    aggregate_bytes: usize,
}

impl FragmentAssembler {
    fn push(&mut self, fragment: &Value) -> Result<Option<Value>, DaemonError> {
        self.expire(Instant::now());
        let attachment = required_str(fragment, "attachment_id")?;
        let message_seq = required_u64(fragment, "message_seq")?;
        let index = required_u64(fragment, "fragment_index")?;
        let event = required_str(fragment, "event")?;
        if fragment.get("encoding").and_then(Value::as_str) != Some("utf8-b64") {
            return Err(DaemonError::Protocol {
                detail: "unsupported terminal fragment encoding".into(),
            });
        }
        let encoded = required_str(fragment, "payload")?;
        if encoded.len() > (MAX_ASSEMBLY_BYTES * 4 / 3) + 4 {
            return Err(DaemonError::Protocol {
                detail: "terminal fragment capacity exceeded".into(),
            });
        }
        let payload = STANDARD.decode(encoded).map_err(protocol_error)?;
        let assembly = self
            .by_attachment
            .entry(attachment.to_string())
            .or_insert_with(|| Assembly {
                message_seq,
                next_index: 0,
                event: event.to_string(),
                deadline: Instant::now() + FRAGMENT_TIMEOUT,
                bytes: Vec::new(),
            });
        if assembly.message_seq != message_seq || assembly.event != event {
            return Err(DaemonError::Protocol {
                detail: "invalid terminal fragment sequence".into(),
            });
        }
        if index < assembly.next_index {
            return Ok(None);
        }
        if index > assembly.next_index {
            return Err(DaemonError::Protocol {
                detail: "invalid terminal fragment sequence".into(),
            });
        }
        if assembly.bytes.len() + payload.len() > MAX_ASSEMBLY_BYTES
            || self.aggregate_bytes + payload.len() > MAX_AGGREGATE_BYTES
        {
            return Err(DaemonError::Protocol {
                detail: "terminal fragment capacity exceeded".into(),
            });
        }
        assembly.bytes.extend_from_slice(&payload);
        assembly.next_index += 1;
        self.aggregate_bytes += payload.len();
        if fragment.get("more").and_then(Value::as_bool) != Some(false) {
            return Ok(None);
        }
        let assembly =
            self.by_attachment
                .remove(attachment)
                .ok_or_else(|| DaemonError::Protocol {
                    detail: "terminal fragment assembly disappeared".into(),
                })?;
        self.aggregate_bytes -= assembly.bytes.len();
        let value = decode_message(&assembly.bytes).map_err(protocol_error)?;
        if message_kind(&value) != Some(assembly.event.as_str()) {
            return Err(DaemonError::Protocol {
                detail: "terminal fragment event mismatch".into(),
            });
        }
        Ok(Some(value))
    }

    fn drop_attachment(&mut self, attachment: &str) {
        if let Some(assembly) = self.by_attachment.remove(attachment) {
            self.aggregate_bytes -= assembly.bytes.len();
        }
    }

    fn expire(&mut self, now: Instant) {
        let expired: Vec<_> = self
            .by_attachment
            .iter()
            .filter(|(_, assembly)| assembly.deadline <= now)
            .map(|(attachment, _)| attachment.clone())
            .collect();
        for attachment in &expired {
            if let Some(assembly) = self.by_attachment.remove(attachment) {
                self.aggregate_bytes -= assembly.bytes.len();
            }
        }
    }
}

fn required_str<'a>(value: &'a Value, field: &str) -> Result<&'a str, DaemonError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| DaemonError::Protocol {
            detail: format!("missing string field {field}"),
        })
}

fn required_u64(value: &Value, field: &str) -> Result<u64, DaemonError> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| DaemonError::Protocol {
            detail: format!("missing integer field {field}"),
        })
}
