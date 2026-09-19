//! Semantic-frame relay over the daemon terminal WebSocket.

use std::collections::VecDeque;
use std::io::Cursor;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use gobby_terminal::protocol::{read_message, ClientMessage, ServerMessage, MAX_FRAME_SIZE};
use serde_json::{json, Value};
use tokio::sync::broadcast::error::TryRecvError;
use uuid::Uuid;

use super::{FrameError, FrameSource, Transport};
use crate::daemon::{Daemon, DaemonEvent, EventReceiver, LiveDaemon};

#[derive(Debug)]
pub struct ProxyFrameSource {
    daemon: LiveDaemon,
    terminal_id: String,
    attachment_id: String,
    receiver: EventReceiver,
    buffered: VecDeque<ServerMessage>,
}

impl ProxyFrameSource {
    pub async fn attach(
        daemon: LiveDaemon,
        terminal_id: impl Into<String>,
    ) -> Result<Self, FrameError> {
        let terminal_id = terminal_id.into();
        // Install the receiver before writing terminal_attach so events emitted
        // immediately before its result cannot pass the source unnoticed.
        let (_, receiver) = daemon.subscribe();
        let reply = daemon
            .send(json!({
                "type": "terminal_attach",
                "request_id": Uuid::new_v4().to_string(),
                "terminal_id": terminal_id,
                "frame_delivery": "proxy",
                "encoding": "semantic_frame",
            }))
            .await?;
        if reply.get("success").and_then(Value::as_bool) != Some(true) {
            return Err(FrameError::Protocol(
                reply
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("terminal attach refused")
                    .to_string(),
            ));
        }
        let attachment_id = reply
            .get("attachment_id")
            .and_then(Value::as_str)
            .ok_or_else(|| FrameError::Protocol("attach result omitted attachment_id".into()))?
            .to_string();
        Self::from_attachment(daemon, terminal_id, attachment_id, receiver)
    }

    pub fn from_attachment(
        daemon: LiveDaemon,
        terminal_id: impl Into<String>,
        attachment_id: impl Into<String>,
        receiver: EventReceiver,
    ) -> Result<Self, FrameError> {
        let mut source = Self {
            daemon,
            terminal_id: terminal_id.into(),
            attachment_id: attachment_id.into(),
            receiver,
            buffered: VecDeque::new(),
        };
        source.drain_initial()?;
        Ok(source)
    }

    pub fn attachment_id(&self) -> &str {
        &self.attachment_id
    }

    #[doc(hidden)]
    pub fn daemon(&self) -> &LiveDaemon {
        &self.daemon
    }

    fn drain_initial(&mut self) -> Result<(), FrameError> {
        loop {
            match self.receiver.try_recv() {
                Ok(event) => {
                    if let Some(message) = self.map_event(event)? {
                        self.buffered.push_back(message);
                    }
                }
                Err(TryRecvError::Empty) => return Ok(()),
                Err(TryRecvError::Closed) => return Err(FrameError::Eof),
                Err(TryRecvError::Lagged(_)) => return Err(FrameError::Lag),
            }
        }
    }

    fn map_event(&self, event: DaemonEvent) -> Result<Option<ServerMessage>, FrameError> {
        match event {
            DaemonEvent::Frame(payload) if self.matches_attachment(&payload) => {
                decode_frame_payload(&payload).map(Some)
            }
            DaemonEvent::AttachHistory(payload) if self.matches_attachment(&payload) => {
                Ok(Some(ServerMessage::AttachHistory {
                    text: payload
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                    truncated: payload
                        .get("truncated")
                        .and_then(Value::as_bool)
                        .unwrap_or(false),
                    dropped_bytes: payload
                        .get("dropped_bytes")
                        .and_then(Value::as_u64)
                        .unwrap_or_default(),
                    total_bytes: payload
                        .get("total_bytes")
                        .and_then(Value::as_u64)
                        .unwrap_or_default(),
                }))
            }
            DaemonEvent::ScrollOffsetApplied(payload) if self.matches_attachment(&payload) => {
                Ok(Some(ServerMessage::ScrollOffsetApplied {
                    applied_rows: json_u32(&payload, "applied_rows")?,
                    max_rows: json_u32(&payload, "max_rows")?,
                }))
            }
            DaemonEvent::AttachmentFinalized {
                attachment_id,
                payload,
                ..
            } if attachment_id == self.attachment_id => Err(FrameError::Finalized {
                code: payload
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or("attachment_finalized")
                    .to_string(),
                reason: payload
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("attachment finalized")
                    .to_string(),
            }),
            DaemonEvent::Lagged => Err(FrameError::Lag),
            DaemonEvent::Disconnected { error, .. } => Err(FrameError::Protocol(error.to_string())),
            _ => Ok(None),
        }
    }

    fn matches_attachment(&self, payload: &Value) -> bool {
        payload.get("attachment_id").and_then(Value::as_str) == Some(self.attachment_id.as_str())
    }
}

impl FrameSource for ProxyFrameSource {
    async fn send(&mut self, message: &ClientMessage) -> Result<(), FrameError> {
        let body = match message {
            ClientMessage::SetViewport { rows, cols } => json!({
                "type": "terminal_set_viewport",
                "terminal_id": self.terminal_id,
                "attachment_id": self.attachment_id,
                "rows": rows,
                "cols": cols,
            }),
            ClientMessage::SetScrollOffset {
                rows_from_live_edge,
            } => json!({
                "type": "terminal_set_scroll_offset",
                "terminal_id": self.terminal_id,
                "attachment_id": self.attachment_id,
                "rows_from_live_edge": rows_from_live_edge,
            }),
            ClientMessage::Detach => json!({
                "type": "terminal_detach",
                "request_id": Uuid::new_v4().to_string(),
                "terminal_id": self.terminal_id,
                "attachment_id": self.attachment_id,
            }),
            _ => {
                return Err(FrameError::Protocol(
                    "message is not valid after frame attachment".into(),
                ))
            }
        };
        self.daemon.notify(body).await.map_err(FrameError::from)
    }

    /// A proxied pane has no socket to the host, so its keystrokes stay on the
    /// daemon's `terminal_input` path (#22573); nothing routes host input here.
    fn send_input(&mut self, _message: &ClientMessage) -> Result<(), FrameError> {
        Err(FrameError::Protocol(
            "proxied panes type through the daemon".into(),
        ))
    }

    async fn recv(&mut self) -> Result<ServerMessage, FrameError> {
        if let Some(message) = self.buffered.pop_front() {
            return Ok(message);
        }
        loop {
            let event = self.receiver.recv().await.map_err(|error| match error {
                tokio::sync::broadcast::error::RecvError::Closed => FrameError::Eof,
                tokio::sync::broadcast::error::RecvError::Lagged(_) => FrameError::Lag,
            })?;
            if let Some(message) = self.map_event(event)? {
                return Ok(message);
            }
        }
    }

    fn transport(&self) -> Transport {
        Transport::Proxy
    }
}

fn decode_frame_payload(payload: &Value) -> Result<ServerMessage, FrameError> {
    if payload.get("encoding").and_then(Value::as_str) != Some("bincode-b64") {
        return Err(FrameError::Protocol(
            "terminal_frame did not use bincode-b64".into(),
        ));
    }
    let encoded = payload
        .get("payload")
        .and_then(Value::as_str)
        .ok_or_else(|| FrameError::Protocol("terminal_frame omitted payload".into()))?;
    let raw = STANDARD
        .decode(encoded)
        .map_err(|error| FrameError::Protocol(error.to_string()))?;
    if raw.len() > MAX_FRAME_SIZE {
        return Err(FrameError::Protocol("terminal_frame was oversized".into()));
    }

    let is_framed = raw
        .get(..4)
        .and_then(|prefix| <[u8; 4]>::try_from(prefix).ok())
        .is_some_and(|prefix| u32::from_le_bytes(prefix) as usize == raw.len() - 4);
    let framed = if is_framed {
        raw
    } else {
        let mut framed = Vec::with_capacity(raw.len() + 4);
        framed.extend_from_slice(&(raw.len() as u32).to_le_bytes());
        framed.extend_from_slice(&raw);
        framed
    };
    read_message(&mut Cursor::new(framed), MAX_FRAME_SIZE).map_err(FrameError::from)
}

fn json_u32(payload: &Value, field: &str) -> Result<u32, FrameError> {
    let value = payload
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| FrameError::Protocol(format!("event omitted {field}")))?;
    u32::try_from(value).map_err(|_| FrameError::Protocol(format!("event {field} exceeded u32")))
}
