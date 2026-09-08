#![allow(dead_code)]

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use sha1::{Digest, Sha1};
use std::collections::{HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, oneshot, Notify};
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::protocol::{Message, Role};
use tokio_tungstenite::WebSocketStream;

#[derive(Debug, Clone)]
pub struct RequestRecord {
    pub method: String,
    pub target: String,
    pub authorization: Option<String>,
    pub body: Option<Value>,
}

#[derive(Debug)]
struct QueuedResponse {
    method: String,
    path_prefix: String,
    status: u16,
    body: Value,
    retry_after: Option<u64>,
    events_before_response: Vec<Value>,
    wait_for_events: bool,
}

#[derive(Debug, Clone)]
struct MockEvent {
    value: Value,
    delivered: Option<Arc<Notify>>,
}

#[derive(Debug)]
struct MockState {
    token: String,
    responses: VecDeque<QueuedResponse>,
    requests: Vec<RequestRecord>,
    spawn_refusal: Option<String>,
    spawn_events_before_reply: VecDeque<Vec<Value>>,
    suppressed_ws: HashSet<String>,
    websocket_handshakes: usize,
    websocket_failures: usize,
    websocket_gate: Option<Arc<Notify>>,
    websocket_read_gate: Option<Arc<Notify>>,
    active_websockets: usize,
    websocket_closes: usize,
    unique_attachment_ids: bool,
    next_attachment_id: u64,
    take_control_replies: VecDeque<(bool, u64, Option<String>)>,
    write_outcomes: VecDeque<(String, Option<String>)>,
    /// Reasons the next `terminal_kill` replies refuse with, in order.
    kill_refusals: VecDeque<String>,
    detach_replies: VecDeque<(bool, Option<String>)>,
    proxy_attach_refusals: VecDeque<(String, String)>,
    proxy_finalizations_before_reply: VecDeque<(String, u64, String, String)>,
    activity: Vec<String>,
}

#[derive(Debug)]
pub struct MockDaemon {
    url: String,
    state: Arc<Mutex<MockState>>,
    events: broadcast::Sender<MockEvent>,
    shutdown: Mutex<Option<oneshot::Sender<()>>>,
    task: Mutex<Option<JoinHandle<()>>>,
}

impl MockDaemon {
    pub async fn start(token: &str) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind mock daemon");
        let address = listener.local_addr().expect("mock address");
        let state = Arc::new(Mutex::new(MockState {
            token: token.to_string(),
            responses: VecDeque::new(),
            requests: Vec::new(),
            spawn_refusal: None,
            spawn_events_before_reply: VecDeque::new(),
            suppressed_ws: HashSet::new(),
            websocket_handshakes: 0,
            websocket_failures: 0,
            websocket_gate: None,
            websocket_read_gate: None,
            active_websockets: 0,
            websocket_closes: 0,
            unique_attachment_ids: false,
            next_attachment_id: 0,
            take_control_replies: VecDeque::new(),
            write_outcomes: VecDeque::new(),
            kill_refusals: VecDeque::new(),
            detach_replies: VecDeque::new(),
            proxy_attach_refusals: VecDeque::new(),
            proxy_finalizations_before_reply: VecDeque::new(),
            activity: Vec::new(),
        }));
        let (events, _) = broadcast::channel(2048);
        let (shutdown, mut shutdown_rx) = oneshot::channel();
        let server_state = Arc::clone(&state);
        let server_events = events.clone();
        let task = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    accepted = listener.accept() => {
                        let Ok((stream, _)) = accepted else { break };
                        let state = Arc::clone(&server_state);
                        let events = server_events.clone();
                        connections.spawn(async move {
                            let _ = serve_connection(stream, state, events).await;
                        });
                    }
                    completed = connections.join_next(), if !connections.is_empty() => {
                        let _ = completed;
                    }
                    _ = &mut shutdown_rx => break,
                }
            }
            connections.shutdown().await;
        });
        Self {
            url: format!("http://{address}"),
            state,
            events,
            shutdown: Mutex::new(Some(shutdown)),
            task: Mutex::new(Some(task)),
        }
    }

    pub fn url(&self) -> &str {
        &self.url
    }

    pub fn enqueue(&self, method: &str, path_prefix: &str, status: u16, body: Value) {
        self.state
            .lock()
            .expect("mock state")
            .responses
            .push_back(QueuedResponse {
                method: method.to_string(),
                path_prefix: path_prefix.to_string(),
                status,
                body,
                retry_after: None,
                events_before_response: Vec::new(),
                wait_for_events: false,
            });
    }

    pub fn enqueue_retry_after(&self, method: &str, path_prefix: &str, status: u16, seconds: u64) {
        self.state
            .lock()
            .expect("mock state")
            .responses
            .push_back(QueuedResponse {
                method: method.to_string(),
                path_prefix: path_prefix.to_string(),
                status,
                body: json!({"detail": "retry"}),
                retry_after: Some(seconds),
                events_before_response: Vec::new(),
                wait_for_events: false,
            });
    }

    pub fn enqueue_with_event(&self, method: &str, path_prefix: &str, body: Value, event: Value) {
        self.state
            .lock()
            .expect("mock state")
            .responses
            .push_back(QueuedResponse {
                method: method.to_string(),
                path_prefix: path_prefix.to_string(),
                status: 200,
                body,
                retry_after: None,
                events_before_response: vec![event],
                wait_for_events: false,
            });
    }

    pub fn enqueue_with_events(
        &self,
        method: &str,
        path_prefix: &str,
        body: Value,
        events: Vec<Value>,
    ) {
        self.state
            .lock()
            .expect("mock state")
            .responses
            .push_back(QueuedResponse {
                method: method.to_string(),
                path_prefix: path_prefix.to_string(),
                status: 200,
                body,
                retry_after: None,
                events_before_response: events,
                wait_for_events: true,
            });
    }

    pub fn set_spawn_refusal(&self, reason: &str) {
        self.state.lock().expect("mock state").spawn_refusal = Some(reason.to_string());
    }

    pub fn enqueue_spawn_events_before_reply(&self, events: Vec<Value>) {
        self.state
            .lock()
            .expect("mock state")
            .spawn_events_before_reply
            .push_back(events);
    }

    pub fn set_token(&self, token: &str) {
        self.state.lock().expect("mock state").token = token.to_string();
    }

    pub fn requests(&self) -> Vec<RequestRecord> {
        self.state.lock().expect("mock state").requests.clone()
    }

    pub fn websocket_handshakes(&self) -> usize {
        self.state.lock().expect("mock state").websocket_handshakes
    }

    pub fn active_websockets(&self) -> usize {
        self.state.lock().expect("mock state").active_websockets
    }

    pub fn websocket_closes(&self) -> usize {
        self.state.lock().expect("mock state").websocket_closes
    }

    pub fn activity(&self) -> Vec<String> {
        self.state.lock().expect("mock state").activity.clone()
    }

    pub fn use_unique_attachment_ids(&self) {
        self.state.lock().expect("mock state").unique_attachment_ids = true;
    }

    pub fn enqueue_take_control_reply(
        &self,
        granted: bool,
        lease_generation: u64,
        reason: Option<&str>,
    ) {
        self.state
            .lock()
            .expect("mock state")
            .take_control_replies
            .push_back((granted, lease_generation, reason.map(ToString::to_string)));
    }

    pub fn enqueue_write_outcome(&self, outcome: &str, reason: Option<&str>) {
        self.state
            .lock()
            .expect("mock state")
            .write_outcomes
            .push_back((outcome.to_string(), reason.map(ToString::to_string)));
    }

    /// The next `terminal_kill` replies `success: false` with `reason`.
    pub fn enqueue_kill_refusal(&self, reason: &str) {
        self.state
            .lock()
            .expect("mock state")
            .kill_refusals
            .push_back(reason.to_string());
    }

    pub fn enqueue_detach_reply(&self, success: bool, reason: Option<&str>) {
        self.state
            .lock()
            .expect("mock state")
            .detach_replies
            .push_back((success, reason.map(ToString::to_string)));
    }

    pub fn refuse_next_proxy_attach(&self, code: &str, reason: &str) {
        self.state
            .lock()
            .expect("mock state")
            .proxy_attach_refusals
            .push_back((code.to_string(), reason.to_string()));
    }

    pub fn finalize_next_proxy_attach_before_reply(
        &self,
        daemon_epoch: &str,
        seq: u64,
        code: &str,
        reason: &str,
    ) {
        self.state
            .lock()
            .expect("mock state")
            .proxy_finalizations_before_reply
            .push_back((
                daemon_epoch.to_string(),
                seq,
                code.to_string(),
                reason.to_string(),
            ));
    }

    pub fn fail_next_websocket(&self) {
        self.state.lock().expect("mock state").websocket_failures += 1;
    }

    pub fn pause_next_websocket(&self) -> Arc<Notify> {
        let gate = Arc::new(Notify::new());
        self.state.lock().expect("mock state").websocket_gate = Some(Arc::clone(&gate));
        gate
    }

    pub fn suppress_ws(&self, kind: &str) {
        self.state
            .lock()
            .expect("mock state")
            .suppressed_ws
            .insert(kind.to_string());
    }

    pub fn allow_ws(&self, kind: &str) {
        self.state
            .lock()
            .expect("mock state")
            .suppressed_ws
            .remove(kind);
    }

    pub fn drop_websockets(&self) {
        let _ = self.events.send(MockEvent {
            value: json!({"__mock_close": true}),
            delivered: None,
        });
    }

    pub async fn pause_websocket_reads(&self) -> Arc<Notify> {
        self.wait_for_websocket().await;
        let gate = Arc::new(Notify::new());
        self.state.lock().expect("mock state").websocket_read_gate = Some(Arc::clone(&gate));
        self.send_event_and_wait(json!({"__mock_pause_reads": true}))
            .await;
        gate
    }

    pub fn send_event(&self, event: Value) {
        let _ = self.events.send(MockEvent {
            value: event,
            delivered: None,
        });
    }

    pub async fn wait_for_websocket(&self) {
        for _ in 0..10_000 {
            if self.events.receiver_count() > 0 {
                return;
            }
            tokio::task::yield_now().await;
        }
        panic!("mock WebSocket event receiver was not installed");
    }

    pub async fn send_event_and_wait(&self, event: Value) {
        self.wait_for_websocket().await;
        let delivered = Arc::new(Notify::new());
        self.events
            .send(MockEvent {
                value: event,
                delivered: Some(Arc::clone(&delivered)),
            })
            .expect("mock WebSocket receiver");
        timeout(Duration::from_secs(10), delivered.notified())
            .await
            .expect("mock WebSocket event delivery");
    }

    pub async fn wait_for_no_websockets(&self) {
        timeout(Duration::from_secs(10), async {
            while self.active_websockets() != 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("mock WebSocket connections close");
    }

    pub async fn shutdown(&self) {
        if let Some(shutdown) = self.shutdown.lock().expect("mock shutdown").take() {
            let _ = shutdown.send(());
        }
        let task = self.task.lock().expect("mock task").take();
        if let Some(task) = task {
            let _ = task.await;
        }
    }
}

impl Drop for MockDaemon {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.get_mut().expect("mock shutdown").take() {
            let _ = shutdown.send(());
        }
        if let Some(task) = self.task.get_mut().expect("mock task").take() {
            task.abort();
        }
    }
}

async fn serve_connection(
    mut stream: TcpStream,
    state: Arc<Mutex<MockState>>,
    events: broadcast::Sender<MockEvent>,
) -> std::io::Result<()> {
    let mut bytes = Vec::new();
    let header_end = loop {
        if let Some(index) = find_header_end(&bytes) {
            break index;
        }
        let mut chunk = [0_u8; 2048];
        let read = stream.read(&mut chunk).await?;
        if read == 0 {
            return Ok(());
        }
        bytes.extend_from_slice(&chunk[..read]);
    };
    let headers = String::from_utf8_lossy(&bytes[..header_end]);
    let mut lines = headers.lines();
    let request_line = lines.next().unwrap_or_default();
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts.next().unwrap_or_default().to_string();
    let target = request_parts.next().unwrap_or_default().to_string();
    let mut authorization = None;
    let mut websocket_key = None;
    let mut content_length = 0;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        match name.trim().to_ascii_lowercase().as_str() {
            "authorization" => authorization = Some(value.trim().to_string()),
            "sec-websocket-key" => websocket_key = Some(value.trim().to_string()),
            "content-length" => content_length = value.trim().parse().unwrap_or(0),
            _ => {}
        }
    }
    if target.starts_with("/ws") {
        if let Some(websocket_key) = websocket_key {
            return serve_websocket(stream, state, events, authorization, websocket_key).await;
        }
    }

    let body_start = header_end + 4;
    while bytes.len() < body_start + content_length {
        let mut chunk = [0_u8; 2048];
        let read = stream.read(&mut chunk).await?;
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&chunk[..read]);
    }
    let body = if content_length == 0 {
        None
    } else {
        serde_json::from_slice(&bytes[body_start..body_start + content_length]).ok()
    };
    let expected = format!("Bearer {}", state.lock().expect("mock state").token);
    if authorization.as_deref() != Some(expected.as_str()) {
        write_response(&mut stream, 401, json!({"detail": "unauthorized"}), None).await?;
        return Ok(());
    }
    let response = {
        let mut state = state.lock().expect("mock state");
        state.requests.push(RequestRecord {
            method: method.clone(),
            target: target.clone(),
            authorization,
            body,
        });
        state.activity.push(format!("{method} {target}"));
        let position = state.responses.iter().position(|response| {
            response.method == method && target.starts_with(&response.path_prefix)
        });
        position.and_then(|position| state.responses.remove(position))
    };
    let response = response.unwrap_or_else(|| default_response(&method, &target));
    for event in response.events_before_response {
        let delivered = response.wait_for_events.then(|| Arc::new(Notify::new()));
        let _ = events.send(MockEvent {
            value: event,
            delivered: delivered.clone(),
        });
        if let Some(delivered) = delivered {
            let _ = timeout(Duration::from_secs(10), delivered.notified()).await;
        } else {
            tokio::task::yield_now().await;
        }
    }
    write_response(
        &mut stream,
        response.status,
        response.body,
        response.retry_after,
    )
    .await
}

async fn serve_websocket(
    mut stream: TcpStream,
    state: Arc<Mutex<MockState>>,
    events: broadcast::Sender<MockEvent>,
    authorization: Option<String>,
    websocket_key: String,
) -> std::io::Result<()> {
    let expected = format!("Bearer {}", state.lock().expect("mock state").token);
    if authorization.as_deref() != Some(expected.as_str()) {
        stream
            .write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            .await?;
        return Ok(());
    }
    let (fail, gate) = {
        let mut state = state.lock().expect("mock state");
        state.websocket_handshakes += 1;
        let fail = state.websocket_failures > 0;
        state.websocket_failures = state.websocket_failures.saturating_sub(1);
        (fail, state.websocket_gate.take())
    };
    if let Some(gate) = gate {
        gate.notified().await;
    }
    if fail {
        stream
            .write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            .await?;
        return Ok(());
    }
    let mut hasher = Sha1::new();
    hasher.update(websocket_key.as_bytes());
    hasher.update(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    let accept = STANDARD.encode(hasher.finalize());
    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    stream.write_all(response.as_bytes()).await?;
    let mut websocket = WebSocketStream::from_raw_socket(stream, Role::Server, None).await;
    {
        let mut state = state.lock().expect("mock state");
        state.active_websockets += 1;
        state.activity.push("WS connected".into());
    }
    let mut event_rx = events.subscribe();
    loop {
        tokio::select! {
            incoming = websocket.next() => {
                let Some(Ok(message)) = incoming else { break };
                if message.is_close() {
                    state.lock().expect("mock state").websocket_closes += 1;
                    let _ = websocket.close(None).await;
                    break;
                }
                let text = match message {
                    Message::Text(text) => text.to_string(),
                    Message::Binary(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
                    _ => continue,
                };
                let Ok(value) = serde_json::from_str::<Value>(&text) else { continue };
                {
                    let mut state = state.lock().expect("mock state");
                    state.requests.push(RequestRecord {
                        method: "WS".into(),
                        target: "/ws".into(),
                        authorization: authorization.clone(),
                        body: Some(value.clone()),
                    });
                    if let Some(kind) = value.get("type").and_then(Value::as_str) {
                        state.activity.push(format!("WS {kind}"));
                    }
                }
                let reply = websocket_reply(&state, &value);
                for event in websocket_events_before_reply(&state, &value, reply.as_ref()) {
                    websocket.send(Message::Text(event.to_string().into())).await
                        .map_err(std::io::Error::other)?;
                }
                if let Some(reply) = reply {
                    websocket.send(Message::Text(reply.to_string().into())).await
                        .map_err(std::io::Error::other)?;
                }
            }
            event = event_rx.recv() => {
                if let Ok(event) = event {
                    if event.value.get("__mock_close").and_then(Value::as_bool) == Some(true) {
                        let _ = websocket.close(None).await;
                        break;
                    }
                    if event.value.get("__mock_pause_reads").and_then(Value::as_bool) == Some(true) {
                        let gate = state.lock().expect("mock state").websocket_read_gate.take();
                        if let Some(delivered) = event.delivered {
                            delivered.notify_one();
                        }
                        if let Some(gate) = gate {
                            gate.notified().await;
                        }
                        continue;
                    }
                    websocket.send(Message::Text(event.value.to_string().into())).await
                        .map_err(std::io::Error::other)?;
                    if let Some(delivered) = event.delivered {
                        delivered.notify_one();
                    }
                }
            }
        }
    }
    state.lock().expect("mock state").active_websockets -= 1;
    Ok(())
}

fn websocket_reply(state: &Arc<Mutex<MockState>>, request: &Value) -> Option<Value> {
    let kind = request.get("type")?.as_str()?;
    if state
        .lock()
        .expect("mock state")
        .suppressed_ws
        .contains(kind)
    {
        return None;
    }
    match kind {
        "terminal_attach" => {
            let refusal = (request.get("frame_delivery").and_then(Value::as_str) == Some("proxy"))
                .then(|| {
                    state
                        .lock()
                        .expect("mock state")
                        .proxy_attach_refusals
                        .pop_front()
                })
                .flatten();
            if let Some((code, reason)) = refusal {
                return Some(json!({
                    "type": "terminal_attach_result",
                    "request_id": request.get("request_id"),
                    "terminal_id": request.get("terminal_id"),
                    "attachment_id": null,
                    "success": false,
                    "code": code,
                    "reason": reason,
                }));
            }
            let attachment_id = {
                let mut state = state.lock().expect("mock state");
                if state.unique_attachment_ids {
                    state.next_attachment_id += 1;
                    format!("attachment-{}", state.next_attachment_id)
                } else {
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".into()
                }
            };
            Some(json!({
                "type": "terminal_attach_result",
                "request_id": request.get("request_id"),
                "terminal_id": request.get("terminal_id"),
                "attachment_id": attachment_id,
                "success": true,
                "backend": "native",
                "rows": 24,
                "cols": 80,
                "lease_generation": 0,
                "direct": null,
                "frame_delivery": request.get("frame_delivery"),
            }))
        }
        "terminal_create" => {
            if let Some(reason) = state.lock().expect("mock state").spawn_refusal.clone() {
                Some(json!({
                    "type": "terminal_create_result",
                    "request_id": request.get("request_id"),
                    "success": false,
                    "terminal_id": null,
                    "backend": null,
                    "reason": reason,
                }))
            } else {
                Some(json!({
                    "type": "terminal_create_result",
                    "request_id": request.get("request_id"),
                    "success": true,
                    "terminal_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "backend": "native",
                    "reason": null,
                }))
            }
        }
        "terminal_kill" => {
            let refusal = state.lock().expect("mock state").kill_refusals.pop_front();
            Some(json!({
                "type": "terminal_kill_result",
                "request_id": request.get("request_id"),
                "terminal_id": request.get("terminal_id"),
                "success": refusal.is_none(),
                "reason": refusal,
            }))
        }
        "terminal_detach" => {
            let (success, reason) = state
                .lock()
                .expect("mock state")
                .detach_replies
                .pop_front()
                .unwrap_or((true, None));
            Some(json!({
                "type": "terminal_detach_result",
                "request_id": request.get("request_id"),
                "terminal_id": request.get("terminal_id"),
                "attachment_id": request.get("attachment_id"),
                "success": success,
                "reason": reason,
            }))
        }
        "terminal_input" | "terminal_paste" => {
            let (outcome, reason) = state
                .lock()
                .expect("mock state")
                .write_outcomes
                .pop_front()
                .unwrap_or_else(|| ("delivered".to_string(), None));
            Some(json!({
                "type": "terminal_write_outcome",
                "attachment_id": request.get("attachment_id"),
                "terminal_id": request.get("terminal_id"),
                "client_write_seq": request.get("client_write_seq"),
                "outcome": outcome,
                "reason": reason,
            }))
        }
        "terminal_take_control" => {
            let reply = state
                .lock()
                .expect("mock state")
                .take_control_replies
                .pop_front()
                .unwrap_or((true, 1, None));
            Some(json!({
                "type": "terminal_control_result",
                "attachment_id": request.get("attachment_id"),
                "granted": reply.0,
                "lease_generation": reply.1,
                "reason": reply.2,
            }))
        }
        "terminal_release_control" => Some(json!({
            "type": "terminal_control_result",
            "attachment_id": request.get("attachment_id"),
            "granted": true,
            "lease_generation": 1,
            "reason": null,
        })),
        "subscribe" => Some(json!({
            "type": "subscribe_success",
            "events": request.get("events").cloned().unwrap_or_else(|| json!([])),
        })),
        _ => None,
    }
}

fn websocket_events_before_reply(
    state: &Arc<Mutex<MockState>>,
    request: &Value,
    reply: Option<&Value>,
) -> Vec<Value> {
    let request_type = request.get("type").and_then(Value::as_str);
    let succeeded = reply
        .and_then(|value| value.get("success"))
        .and_then(Value::as_bool)
        == Some(true);
    if request_type == Some("terminal_create") && succeeded {
        return state
            .lock()
            .expect("mock state")
            .spawn_events_before_reply
            .pop_front()
            .unwrap_or_default();
    }
    if request_type != Some("terminal_attach")
        || request.get("frame_delivery").and_then(Value::as_str) != Some("proxy")
        || !succeeded
    {
        return Vec::new();
    }
    let Some((daemon_epoch, seq, code, reason)) = state
        .lock()
        .expect("mock state")
        .proxy_finalizations_before_reply
        .pop_front()
    else {
        return Vec::new();
    };
    vec![json!({
        "type": "terminal_attachment_finalized",
        "daemon_epoch": daemon_epoch,
        "seq": seq,
        "terminal_id": request.get("terminal_id"),
        "attachment_id": reply.and_then(|value| value.get("attachment_id")),
        "code": code,
        "reason": reason,
    })]
}

fn default_response(method: &str, target: &str) -> QueuedResponse {
    let body = if method == "GET" && target.starts_with("/api/terminals?") {
        json!({
            "items": [],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 0},
        })
    } else if method == "GET" && target == "/api/attention/roster" {
        json!({"epoch": "attention-1", "seq": 0, "entries": []})
    } else if method == "GET" && target == "/api/projects" {
        json!([])
    } else if method == "GET" && target.starts_with("/api/source-control/status?") {
        json!({
            "current_branch": null,
            "ahead": null,
            "behind": null,
            "repo_path": null,
            "worktree_count": 0,
        })
    } else if method == "GET" && target.starts_with("/api/source-control/worktrees?") {
        json!({"worktrees": []})
    } else if method == "GET" && target.starts_with("/api/sessions?") {
        json!({"sessions": [], "count": 0, "next_cursor": null})
    } else if method == "GET" && target.starts_with("/api/agents/runs?") {
        json!({"status": "success", "runs": [], "count": 0})
    } else {
        json!({"ok": true})
    };
    QueuedResponse {
        method: method.to_string(),
        path_prefix: target.to_string(),
        status: 200,
        body,
        retry_after: None,
        events_before_response: Vec::new(),
        wait_for_events: false,
    }
}

async fn write_response(
    stream: &mut TcpStream,
    status: u16,
    body: Value,
    retry_after: Option<u64>,
) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        401 => "Unauthorized",
        404 => "Not Found",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        _ => "Error",
    };
    let body = body.to_string();
    let retry = retry_after
        .map(|seconds| format!("Retry-After: {seconds}\r\n"))
        .unwrap_or_default();
    let response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{retry}Connection: close\r\n\r\n{body}",
        body.len(),
    );
    stream.write_all(response.as_bytes()).await
}

fn find_header_end(bytes: &[u8]) -> Option<usize> {
    bytes.windows(4).position(|window| window == b"\r\n\r\n")
}
