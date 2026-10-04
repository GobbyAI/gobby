#![allow(dead_code)]

mod workspace;

pub mod host;

#[allow(unused_imports)]
pub use host::{live_workspace_on_direct_host, DirectHost};
pub use workspace::WorkspaceSim;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use sha1::{Digest, Sha1};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, oneshot, Notify};
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, Message, Role};
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
    /// The reply waits for this before it is written: a daemon that is slow
    /// to answer, without a real sleep in the test.
    hold: Option<Arc<Notify>>,
}

#[derive(Debug, Clone)]
struct MockEvent {
    value: Value,
    delivered: Option<Arc<Notify>>,
}

/// A websocket request the mock answers only once `release` is notified.
/// The connection keeps serving everything else meanwhile, so the request
/// stays pending while events and frames flow.
struct WsHold {
    kind: String,
    filter: Box<dyn Fn(&Value) -> bool + Send>,
    release: Arc<Notify>,
}

impl std::fmt::Debug for WsHold {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WsHold")
            .field("kind", &self.kind)
            .finish_non_exhaustive()
    }
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
    ws_holds: Vec<WsHold>,
    /// Replies actually written, per request `type`.
    ws_replies: HashMap<String, usize>,
    active_websockets: usize,
    websocket_closes: usize,
    unique_attachment_ids: bool,
    next_attachment_id: u64,
    /// When set, a closed websocket finalizes every attachment it was issued,
    /// as the daemon's `finalize_websocket` does, and `terminal_take_control`
    /// refuses a finalized id as `stale_attachment` (#23419).
    finalize_attachments_on_close: bool,
    live_attachments: HashSet<String>,
    attach_lease_holders: Vec<(String, Value)>,
    attach_backends: Vec<(String, String)>,
    /// `(granted, lease_generation, reason, host_input_granted)`. The last
    /// field is the terminal host's input grant, which a direct native pane
    /// needs before it may type on its own frame socket (#22573).
    take_control_replies: VecDeque<(bool, u64, Option<String>, Option<bool>)>,
    write_outcomes: VecDeque<(String, Option<String>)>,
    /// Reasons the next `terminal_kill` replies refuse with, in order.
    kill_refusals: VecDeque<String>,
    /// `(code, reason)` the next `workspace_op` replies refuse with, in order.
    workspace_refusals: VecDeque<(String, String)>,
    /// `(op, occurrence, code, reason)` for a specific workspace mutation.
    workspace_refusals_at: Vec<(String, usize, String, String)>,
    snapshot_refusal_after_ops: Option<(usize, String)>,
    workspace_results: VecDeque<Value>,
    workspace_requests: Vec<Value>,
    detach_replies: VecDeque<(bool, Option<String>)>,
    proxy_attach_refusals: VecDeque<(String, String)>,
    /// The `direct` locator every `frame_delivery: "direct"` attach answers
    /// with. `None` omits it, which is how a daemon says the host has no
    /// socket to offer and gclient falls back to a proxy attachment.
    direct_attach_locator: Option<Value>,
    proxy_finalizations_before_reply: VecDeque<(String, u64, String, String)>,
    activity: Vec<String>,
    /// The daemon workspace `workspace_attach` serves and `workspace_op` moves.
    workspace: WorkspaceSim,
    /// Events the last `workspace_op` published, sent before its reply.
    pending_workspace_events: VecDeque<Value>,
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
        Self::start_at(token, "127.0.0.1:0").await
    }

    /// `start` on a chosen address: the daemon coming back on the port a
    /// client already holds the URL of.
    pub async fn start_at(token: &str, address: &str) -> Self {
        let listener = TcpListener::bind(address).await.expect("bind mock daemon");
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
            ws_holds: Vec::new(),
            ws_replies: HashMap::new(),
            active_websockets: 0,
            websocket_closes: 0,
            unique_attachment_ids: false,
            next_attachment_id: 0,
            finalize_attachments_on_close: false,
            live_attachments: HashSet::new(),
            attach_lease_holders: Vec::new(),
            attach_backends: Vec::new(),
            take_control_replies: VecDeque::new(),
            write_outcomes: VecDeque::new(),
            kill_refusals: VecDeque::new(),
            workspace_refusals: VecDeque::new(),
            workspace_refusals_at: Vec::new(),
            snapshot_refusal_after_ops: None,
            workspace_results: VecDeque::new(),
            workspace_requests: Vec::new(),
            detach_replies: VecDeque::new(),
            proxy_attach_refusals: VecDeque::new(),
            direct_attach_locator: None,
            proxy_finalizations_before_reply: VecDeque::new(),
            workspace: WorkspaceSim::from_fixture(),
            pending_workspace_events: VecDeque::new(),
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
                        // The daemon's asyncio transports disable Nagle too.
                        let _ = stream.set_nodelay(true);
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
                hold: None,
            });
    }

    /// Queue a reply the mock writes only once the returned notify fires.
    pub fn enqueue_held(
        &self,
        method: &str,
        path_prefix: &str,
        status: u16,
        body: Value,
    ) -> Arc<Notify> {
        let hold = Arc::new(Notify::new());
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
                hold: Some(hold.clone()),
            });
        hold
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
                hold: None,
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
                hold: None,
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
                hold: None,
            });
    }

    pub fn set_spawn_refusal(&self, reason: &str) {
        self.state.lock().expect("mock state").spawn_refusal = Some(reason.to_string());
    }

    /// Seed the daemon workspace with `project`'s tabs (`WorkspaceSim::seed`).
    pub fn seed_workspace(
        &self,
        project: &str,
        tabs: &[(&[&str], &str)],
    ) -> Vec<(String, Vec<String>)> {
        self.state
            .lock()
            .expect("mock state")
            .workspace
            .seed(project, tabs)
    }

    pub fn queue_owned_terminal_id(&self, terminal_id: &str) {
        self.state
            .lock()
            .expect("mock state")
            .workspace
            .queue_owned_terminal_id(terminal_id);
    }

    pub fn seed_other_workspace(&self, project: &str, tabs: &[(&[&str], &str)]) -> String {
        self.state
            .lock()
            .expect("mock state")
            .workspace
            .seed_other_workspace(project, tabs)
    }

    /// The tab id of `terminal_id` on the current or a parked workspace.
    pub fn tab_for_terminal(&self, terminal_id: &str) -> Option<String> {
        self.state
            .lock()
            .expect("mock state")
            .workspace
            .tab_for_terminal(terminal_id)
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

    /// Model the daemon finalizing a closed socket's attachments: a later
    /// `terminal_take_control` naming one is refused `stale_attachment`.
    pub fn finalize_attachments_on_close(&self) {
        self.state
            .lock()
            .expect("mock state")
            .finalize_attachments_on_close = true;
    }

    pub fn set_attach_backend(&self, terminal_id: &str, backend: &str) {
        self.state
            .lock()
            .expect("mock state")
            .attach_backends
            .push((terminal_id.to_string(), backend.to_string()));
    }

    pub fn set_attach_lease_holder(&self, terminal_id: &str, holder: Value) {
        self.state
            .lock()
            .expect("mock state")
            .attach_lease_holders
            .push((terminal_id.to_string(), holder));
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
            .push_back((
                granted,
                lease_generation,
                reason.map(ToString::to_string),
                Some(granted),
            ));
    }

    /// A grant the terminal host never matched: the daemon hands out the
    /// writer lease and omits `host_input_granted`. A direct native pane must
    /// refuse to type rather than fall back to the daemon (#22573).
    pub fn enqueue_take_control_reply_without_host_grant(&self, lease_generation: u64) {
        self.state
            .lock()
            .expect("mock state")
            .take_control_replies
            .push_back((true, lease_generation, None, None));
    }

    /// Answer every direct attach with `locator`, so the client connects a real
    /// frame socket and the pane lands on `Transport::Direct`.
    pub fn serve_direct_attach(&self, locator: Value) {
        self.state.lock().expect("mock state").direct_attach_locator = Some(locator);
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

    /// The next `workspace_op` replies `workspace_error` with `code` and
    /// `reason` instead of moving the workspace.
    pub fn enqueue_workspace_refusal(&self, code: &str, reason: &str) {
        self.state
            .lock()
            .expect("mock state")
            .workspace_refusals
            .push_back((code.to_string(), reason.to_string()));
    }

    pub fn refuse_workspace_op_at(&self, op: &str, occurrence: usize, code: &str, reason: &str) {
        self.state
            .lock()
            .expect("mock state")
            .workspace_refusals_at
            .push((op.into(), occurrence, code.into(), reason.into()));
    }

    pub fn refuse_snapshot_after_ops(&self, count: usize, reason: &str) {
        self.state
            .lock()
            .expect("mock state")
            .snapshot_refusal_after_ops = Some((count, reason.into()));
    }

    pub fn enqueue_workspace_result(&self, result: Value) {
        self.state
            .lock()
            .expect("mock state")
            .workspace_results
            .push_back(result);
    }

    pub fn workspace_requests(&self) -> Vec<Value> {
        self.state
            .lock()
            .expect("mock state")
            .workspace_requests
            .clone()
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

    /// Hold the reply to the `workspace_attach` request until released.
    pub fn hold_attach(&self) -> Arc<Notify> {
        self.hold_ws("workspace_attach", |_| true)
    }

    /// Hold the reply (and the events sent before it) to the next websocket
    /// request of `kind` that `filter` accepts, until the returned notify is
    /// released. The hold is one-shot: the first matching request takes it.
    pub fn hold_ws(
        &self,
        kind: &str,
        filter: impl Fn(&Value) -> bool + Send + 'static,
    ) -> Arc<Notify> {
        let release = Arc::new(Notify::new());
        self.state
            .lock()
            .expect("mock state")
            .ws_holds
            .push(WsHold {
                kind: kind.to_string(),
                filter: Box::new(filter),
                release: Arc::clone(&release),
            });
        release
    }

    /// How many replies to websocket requests of `kind` were written.
    pub fn replies(&self, kind: &str) -> usize {
        self.state
            .lock()
            .expect("mock state")
            .ws_replies
            .get(kind)
            .copied()
            .unwrap_or_default()
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

    /// Close every live websocket the way the daemon does on stop/restart:
    /// close code 1001 with the "Server shutting down" reason (#22002).
    pub fn close_websockets_going_away(&self) {
        let _ = self.events.send(MockEvent {
            value: json!({"__mock_close": true, "code": 1001}),
            delivered: None,
        });
    }

    /// Close every live websocket the way uvicorn does when the daemon's HTTP
    /// server (which serves `/ws`) shuts down: close code 1012 "service
    /// restart" (#22002).
    pub fn close_websockets_service_restart(&self) {
        let _ = self.events.send(MockEvent {
            value: json!({"__mock_close": true, "code": 1012}),
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
    if let Some(hold) = response.hold {
        hold.notified().await;
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
    let (released_tx, mut released_rx) =
        tokio::sync::mpsc::unbounded_channel::<(String, Vec<Value>, Option<Value>)>();
    loop {
        tokio::select! {
            Some((kind, events, reply)) = released_rx.recv() => {
                write_reply(&mut websocket, &state, &kind, events, reply).await?;
            }
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
                let before = websocket_events_before_reply(&state, &value, reply.as_ref());
                let kind = value.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
                let hold = reply.as_ref().and_then(|_| take_ws_hold(&state, &kind, &value));
                if let Some(release) = hold {
                    let released = released_tx.clone();
                    tokio::spawn(async move {
                        release.notified().await;
                        let _ = released.send((kind, before, reply));
                    });
                } else {
                    write_reply(&mut websocket, &state, &kind, before, reply).await?;
                }
            }
            event = event_rx.recv() => {
                if let Ok(event) = event {
                    if event.value.get("__mock_close").and_then(Value::as_bool) == Some(true) {
                        let frame = event
                            .value
                            .get("code")
                            .and_then(Value::as_u64)
                            .map(|code| CloseFrame {
                                code: CloseCode::from(code as u16),
                                reason: "Server shutting down".into(),
                            });
                        let _ = websocket.close(frame).await;
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
    {
        let mut state = state.lock().expect("mock state");
        state.active_websockets -= 1;
        if state.finalize_attachments_on_close {
            state.live_attachments.clear();
        }
    }
    Ok(())
}

/// Take the first hold that matches `request`, if any.
fn take_ws_hold(state: &Mutex<MockState>, kind: &str, request: &Value) -> Option<Arc<Notify>> {
    let mut state = state.lock().expect("mock state");
    let index = state
        .ws_holds
        .iter()
        .position(|hold| hold.kind == kind && (hold.filter)(request))?;
    Some(state.ws_holds.remove(index).release)
}

/// Write the events a request publishes, then its reply, counting the reply.
async fn write_reply(
    websocket: &mut WebSocketStream<TcpStream>,
    state: &Mutex<MockState>,
    kind: &str,
    events: Vec<Value>,
    reply: Option<Value>,
) -> std::io::Result<()> {
    for event in events {
        websocket
            .send(Message::Text(event.to_string().into()))
            .await
            .map_err(std::io::Error::other)?;
    }
    if let Some(reply) = reply {
        websocket
            .send(Message::Text(reply.to_string().into()))
            .await
            .map_err(std::io::Error::other)?;
        *state
            .lock()
            .expect("mock state")
            .ws_replies
            .entry(kind.to_string())
            .or_default() += 1;
    }
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
            let direct = (request.get("frame_delivery").and_then(Value::as_str) == Some("direct"))
                .then(|| {
                    state
                        .lock()
                        .expect("mock state")
                        .direct_attach_locator
                        .clone()
                })
                .flatten();
            let attachment_id = {
                let mut state = state.lock().expect("mock state");
                let attachment_id: String = if state.unique_attachment_ids {
                    state.next_attachment_id += 1;
                    format!("attachment-{}", state.next_attachment_id)
                } else {
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".into()
                };
                state.live_attachments.insert(attachment_id.clone());
                attachment_id
            };
            let lease_holder = state
                .lock()
                .expect("mock state")
                .attach_lease_holders
                .iter()
                .find(|(id, _)| {
                    Some(id.as_str()) == request.get("terminal_id").and_then(Value::as_str)
                })
                .map(|(_, holder)| holder.clone());
            let backend = state
                .lock()
                .expect("mock state")
                .attach_backends
                .iter()
                .find(|(id, _)| {
                    Some(id.as_str()) == request.get("terminal_id").and_then(Value::as_str)
                })
                .map_or("native", |(_, backend)| backend.as_str())
                .to_string();
            Some(json!({
                "type": "terminal_attach_result",
                "request_id": request.get("request_id"),
                "terminal_id": request.get("terminal_id"),
                "attachment_id": attachment_id,
                "success": true,
                "backend": backend,
                "rows": 24,
                "cols": 80,
                "lease_generation": 0,
                "lease_holder": lease_holder,
                "direct": direct,
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
            let mut state = state.lock().expect("mock state");
            let refusal = state.kill_refusals.pop_front();
            if refusal.is_none() {
                if let Some(terminal_id) = request.get("terminal_id").and_then(Value::as_str) {
                    let events = state.workspace.reap_terminal(terminal_id);
                    state.pending_workspace_events.extend(events);
                }
            }
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
            let reply = {
                let mut state = state.lock().expect("mock state");
                let finalized = state.finalize_attachments_on_close
                    && !request
                        .get("attachment_id")
                        .and_then(Value::as_str)
                        .is_some_and(|id| state.live_attachments.contains(id));
                if finalized {
                    (false, 0, Some("stale_attachment".to_string()), None)
                } else {
                    state
                        .take_control_replies
                        .pop_front()
                        .unwrap_or((true, 1, None, Some(true)))
                }
            };
            Some(json!({
                "type": "terminal_control_result",
                "attachment_id": request.get("attachment_id"),
                "granted": reply.0,
                "lease_generation": reply.1,
                "reason": reply.2,
                "host_input_granted": reply.3,
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
        "workspace_attach" => {
            let mut state = state.lock().expect("mock state");
            if let Some(workspace) = request.get("workspace").and_then(Value::as_str) {
                state.workspace.select_workspace(workspace);
            } else {
                if let Some(project_id) = request.get("project_id").and_then(Value::as_str) {
                    state.workspace.open_project(project_id);
                }
            }
            Some(state.workspace.attach_reply(request.get("request_id")))
        }
        "workspace_snapshot" => {
            let mut state = state.lock().expect("mock state");
            if state
                .snapshot_refusal_after_ops
                .as_ref()
                .is_some_and(|(count, _)| state.workspace_requests.len() >= *count)
            {
                let (_, reason) = state
                    .snapshot_refusal_after_ops
                    .take()
                    .expect("snapshot refusal");
                return Some(json!({
                    "type": "workspace_error",
                    "request_id": request.get("request_id"),
                    "code": "invalid_op",
                    "reason": reason,
                }));
            }
            state.workspace.snapshot_reply_for(
                request.get("workspace")?.as_str()?,
                request.get("request_id"),
            )
        }
        "workspace_op" => {
            let mut state = state.lock().expect("mock state");
            state.workspace_requests.push(request.clone());
            if request.get("op").and_then(Value::as_str) == Some("workspace.list") {
                return Some(json!({
                    "type": "workspace_op",
                    "request_id": request.get("request_id"),
                    "op": "workspace.list",
                    "result": state.workspace.list(),
                }));
            }
            let op = request
                .get("op")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let occurrence = state
                .workspace_requests
                .iter()
                .filter(|request| request.get("op").and_then(Value::as_str) == Some(op))
                .count();
            if let Some(index) = state
                .workspace_refusals_at
                .iter()
                .position(|(target, nth, _, _)| target == op && *nth == occurrence)
            {
                let (_, _, code, reason) = state.workspace_refusals_at.remove(index);
                return Some(json!({
                    "type": "workspace_error",
                    "request_id": request.get("request_id"),
                    "code": code,
                    "reason": reason,
                }));
            }
            if let Some((code, reason)) = state.workspace_refusals.pop_front() {
                return Some(json!({
                    "type": "workspace_error",
                    "request_id": request.get("request_id"),
                    "code": code,
                    "reason": reason,
                }));
            }
            if matches!(
                request.get("op").and_then(Value::as_str),
                Some("tab.create" | "pane.split")
            ) {
                if let Some(terminal_id) = request.get("terminal_id").and_then(Value::as_str) {
                    if let Some(held_ref) = state.workspace.held_ref_for_terminal(terminal_id) {
                        return Some(json!({
                            "type": "workspace_error",
                            "request_id": request.get("request_id"),
                            "code": "busy",
                            "reason": format!("Terminal {terminal_id} is held by pane {held_ref}"),
                        }));
                    }
                }
            }
            if !state.workspace.knows(request) {
                let id = ["pane", "tab"]
                    .iter()
                    .find_map(|key| request.get(*key).and_then(Value::as_str))
                    .unwrap_or_default();
                return Some(json!({
                    "type": "workspace_error",
                    "request_id": request.get("request_id"),
                    "code": "not_found",
                    "reason": format!("No workspace, tab, or pane has id {id}"),
                }));
            }
            let events = state.workspace.apply(request);
            let result = state.workspace_results.pop_front().or_else(|| {
                match request.get("op").and_then(Value::as_str) {
                    Some("tab.create" | "pane.split") => events
                        .first()
                        .map(|event| json!({"tabs": event["tabs"], "panes": event["panes"]})),
                    Some("pane.read") => Some(json!({"text": "mock pane output"})),
                    Some("pane.wait_for_output") => {
                        Some(json!({"matched": true, "reason": "matched"}))
                    }
                    _ => None,
                }
            });
            state.pending_workspace_events.extend(events);
            Some(json!({
                "type": "workspace_op",
                "request_id": request.get("request_id"),
                "op": request.get("op"),
                "result": result,
            }))
        }
        _ => None,
    }
}

fn websocket_events_before_reply(
    state: &Arc<Mutex<MockState>>,
    request: &Value,
    reply: Option<&Value>,
) -> Vec<Value> {
    let request_type = request.get("type").and_then(Value::as_str);
    if matches!(request_type, Some("workspace_op" | "terminal_kill")) {
        return state
            .lock()
            .expect("mock state")
            .pending_workspace_events
            .drain(..)
            .collect();
    }
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
    } else if method == "GET" && target == "/api/admin/config" {
        json!({"status": "success", "config": {"server": {"version": "0.5.0"}}})
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
        hold: None,
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
