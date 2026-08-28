//! Daemon REST/WS data plane. Tests drive a scripted transport.

mod ws;

pub use ws::{
    decode_message, encode_message, WsCodecError, GOLDEN_NAMES, TERMINAL_WS_SAFE_INTEGER_MAX,
};

use crate::copy_mode::{paste_payload, PASTE_MAX_BYTES};
use serde_json::{json, Value};
use std::collections::VecDeque;
use thiserror::Error;

#[derive(Debug, Error)]
pub struct DaemonError {
    status: u16,
    code: String,
    message: String,
}

impl DaemonError {
    pub fn new(status: u16, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
        }
    }

    pub fn status(&self) -> u16 {
        self.status
    }

    pub fn code(&self) -> &str {
        &self.code
    }
}

impl std::fmt::Display for DaemonError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

#[derive(Debug, Default)]
pub struct ScriptedDaemon {
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
    pty_mutations: u32,
    last_pty_write: Option<String>,
    last_paste_seq: Option<u64>,
}

impl ScriptedDaemon {
    pub fn new() -> Self {
        Self {
            roster: json!({"epoch": "e0", "seq": 0, "entries": []}),
            spawn_response: json!({"success": true}),
            respond_status: 200,
            reachable: true,
            ws_connected: true,
            ..Self::default()
        }
    }

    pub fn rest_paths(&self) -> &[String] {
        &self.rest
    }

    pub fn ws_sent(&self) -> Vec<Value> {
        self.ws_out.clone()
    }

    pub fn ws_sent_types(&self) -> Vec<String> {
        self.ws_out
            .iter()
            .filter_map(|m| m.get("type")?.as_str().map(str::to_string))
            .collect()
    }

    pub fn push_attention_event(&mut self, event: Value) {
        self.attention_inbox.push_back(event);
    }

    pub fn take_attention_events(&mut self) -> Vec<Value> {
        self.attention_inbox.drain(..).collect()
    }

    pub fn set_roster(&mut self, roster: Value) {
        self.roster = roster;
    }

    pub fn roster(&self) -> &Value {
        &self.roster
    }

    pub fn set_terminal_pages(&mut self, pages: Vec<Value>) {
        self.terminal_pages = pages.into();
    }

    pub fn set_live_terminals(&mut self, ids: Vec<String>) {
        self.live_terminals = ids;
    }

    pub fn live_terminals(&self) -> &[String] {
        &self.live_terminals
    }

    pub fn set_spawn_response(&mut self, body: Value) {
        self.spawn_response = body;
    }

    pub fn last_spawn_body(&self) -> &Value {
        &self.last_spawn_body
    }

    pub fn set_respond_status(&mut self, status: u16, code: impl Into<String>) {
        self.respond_status = status;
        self.respond_code = code.into();
    }

    pub fn set_reachable(&mut self, reachable: bool) {
        self.reachable = reachable;
    }

    pub fn reachable(&self) -> bool {
        self.reachable
    }

    pub fn set_ws_connected(&mut self, connected: bool) {
        self.ws_connected = connected;
    }

    pub fn ws_connected(&self) -> bool {
        self.ws_connected
    }

    pub fn pty_mutation_count(&self) -> u32 {
        self.pty_mutations
    }

    pub fn last_pty_write(&self) -> Option<&str> {
        self.last_pty_write.as_deref()
    }

    pub fn last_paste_seq(&self) -> Option<u64> {
        self.last_paste_seq
    }

    pub fn subscribe(&mut self) {
        self.rest.push("WS subscribe".into());
        self.ws_connected = true;
    }

    pub fn fetch_attention_roster(&mut self) -> Result<Value, DaemonError> {
        self.rest.push("GET /api/attention/roster".into());
        if !self.reachable {
            return Err(DaemonError::new(503, "unreachable", "daemon unreachable"));
        }
        Ok(self.roster.clone())
    }

    pub fn fetch_terminals_page(
        &mut self,
        project_id: &str,
        cursor: Option<&str>,
    ) -> Result<Value, DaemonError> {
        let mut path = format!("GET /api/terminals?project_id={project_id}&states=pending%7Clive");
        if let Some(cursor) = cursor {
            path.push_str("&cursor=");
            path.push_str(cursor);
        }
        self.rest.push(path);
        if !self.reachable {
            return Err(DaemonError::new(503, "unreachable", "daemon unreachable"));
        }
        Ok(self
            .terminal_pages
            .pop_front()
            .unwrap_or_else(|| json!({"items": [], "next_cursor": null})))
    }

    pub fn respond(&mut self, entry_id: &str, _body: Value) -> Result<Value, DaemonError> {
        self.rest
            .push(format!("POST /api/attention/{entry_id}/respond"));
        if self.respond_status != 200 {
            return Err(DaemonError::new(
                self.respond_status,
                self.respond_code.clone(),
                self.respond_code.clone(),
            ));
        }
        Ok(json!({"ok": true}))
    }

    pub fn spawn_agent(&mut self, body: Value) -> Result<Value, DaemonError> {
        self.rest.push("POST /api/agents/spawn".into());
        self.last_spawn_body = body;
        Ok(self.spawn_response.clone())
    }

    pub fn send_ws(&mut self, message: Value) -> Result<(), DaemonError> {
        if !self.ws_connected || !self.reachable {
            return Err(DaemonError::new(503, "unreachable", "daemon unreachable"));
        }
        encode_message(&message).map_err(|err| DaemonError::new(400, "ws", err.to_string()))?;
        match message.get("type").and_then(Value::as_str) {
            Some("terminal_input") => {
                if let Some(data) = message.get("data").and_then(Value::as_str) {
                    self.last_pty_write = Some(data.to_string());
                    self.pty_mutations += 1;
                }
            }
            Some("terminal_paste") => {
                let text = message.get("text").and_then(Value::as_str).unwrap_or("");
                if text.len() > PASTE_MAX_BYTES {
                    return Err(DaemonError::new(400, "paste_too_large", "paste_too_large"));
                }
                self.last_paste_seq = message.get("client_write_seq").and_then(Value::as_u64);
                let payload = paste_payload(text, true);
                self.last_pty_write = Some(payload);
                self.pty_mutations += 1;
            }
            _ => {}
        }
        self.ws_out.push(message);
        Ok(())
    }
}
