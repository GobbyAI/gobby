//! Newline-delimited JSON control protocol for the gterm host.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use std::io;
use std::sync::{Arc, Mutex as StdMutex};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::net::{unix::OwnedReadHalf, UnixStream};
use tokio::sync::{mpsc, OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;

use super::events::EventReceiver;
use super::ledger::{fingerprint_json, LedgerDecision, OperationLedger};
use super::state::HostState;

pub const PROTOCOL_VERSION: u32 = 1;
const MAX_CONTROL_LINE: usize = 2 * 1024 * 1024;
const MAX_INFLIGHT_PER_CONNECTION: usize = 64;

#[derive(Debug, Deserialize)]
pub struct ControlRequest {
    pub method: String,
    #[serde(default)]
    pub id: Option<Value>,
    #[serde(default)]
    pub protocol_version: Option<u32>,
    #[serde(default)]
    pub control_token: Option<String>,
    #[serde(default)]
    pub grace_ms: Option<u64>,
    #[serde(default)]
    pub operation_seq: Option<u64>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

enum RequestRead {
    Request(ControlRequest),
    InvalidJson,
    Overflow,
    Closed,
}

async fn read_request(
    reader: &mut BufReader<OwnedReadHalf>,
    buffer: &mut Vec<u8>,
) -> io::Result<RequestRead> {
    if buffer.len() > MAX_CONTROL_LINE {
        buffer.clear();
        return Ok(RequestRead::Overflow);
    }
    let remaining = MAX_CONTROL_LINE + 1 - buffer.len();
    let read = (&mut *reader)
        .take(remaining as u64)
        .read_until(b'\n', buffer)
        .await?;
    if buffer.len() > MAX_CONTROL_LINE {
        buffer.clear();
        return Ok(RequestRead::Overflow);
    }
    if read == 0 && buffer.is_empty() {
        return Ok(RequestRead::Closed);
    }
    let mut line = std::mem::take(buffer);
    if line.last() == Some(&b'\n') {
        line.pop();
        if line.last() == Some(&b'\r') {
            line.pop();
        }
    }
    match serde_json::from_slice(&line) {
        Ok(request) => Ok(RequestRead::Request(request)),
        Err(_) => Ok(RequestRead::InvalidJson),
    }
}

fn with_id(mut value: Value, id: &Option<Value>) -> Value {
    if let (Some(id), Some(obj)) = (id, value.as_object_mut()) {
        obj.insert("id".to_string(), id.clone());
    }
    value
}

pub async fn handle_connection(stream: UnixStream, state: Arc<HostState>) {
    let conn_id = state.alloc_conn();
    let (reader, writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut request_buffer = Vec::new();
    let mut authed = false;
    let in_flight = Arc::new(StdMutex::new(HashSet::<String>::new()));
    let permits = Arc::new(Semaphore::new(MAX_INFLIGHT_PER_CONNECTION));
    let outbound_cap = state.config.control_queue_entries.max(1) as usize;
    let deadline = state.config.control_deadline();
    let (outbound_tx, outbound_rx) = mpsc::channel::<Value>(outbound_cap);
    let writer_task = tokio::spawn(async move {
        let _ = super::backpressure::write_outbound(writer, outbound_rx, deadline).await;
    });
    let event_tasks = Arc::new(StdMutex::new(Vec::new()));
    let mut dispatch_tasks = JoinSet::new();
    let (ordered_tx, mut ordered_rx) = mpsc::channel::<QueuedRequest>(MAX_INFLIGHT_PER_CONNECTION);
    let ordered_state = state.clone();
    let ordered_in_flight = in_flight.clone();
    let ordered_outbound = outbound_tx.clone();
    let ordered_task = tokio::spawn(async move {
        let mut ledger = OperationLedger::default();
        while let Some(QueuedRequest {
            request,
            _permit,
            id_key,
        }) = ordered_rx.recv().await
        {
            let result = dispatch_ordered(&ordered_state, conn_id, &request, &mut ledger).await;
            let _ = ordered_outbound
                .send(with_id(result.response, &request.id))
                .await;
            ordered_in_flight
                .lock()
                .expect("in-flight request lock poisoned")
                .remove(&id_key);
        }
    });

    loop {
        let request = match read_request(&mut reader, &mut request_buffer).await {
            Ok(RequestRead::Request(request)) => request,
            Ok(RequestRead::InvalidJson) => {
                let _ = outbound_tx
                    .send(json!({"ok": false, "error": "invalid_json"}))
                    .await;
                continue;
            }
            Ok(RequestRead::Overflow) => {
                let _ = outbound_tx
                    .send(json!({"ok": false, "error": "control_overflow"}))
                    .await;
                break;
            }
            Ok(RequestRead::Closed) | Err(_) => break,
        };
        let Some(id) = request.id.clone() else {
            let _ = outbound_tx
                .send(json!({"ok": false, "error": "missing_id"}))
                .await;
            continue;
        };
        if !authed {
            if request.method != "hello" {
                let _ = outbound_tx
                    .send(with_id(
                        json!({"ok": false, "error": "unauthenticated"}),
                        &request.id,
                    ))
                    .await;
                break;
            }
            let presented = request.control_token.as_deref().unwrap_or("");
            if presented != state.token.as_str() {
                let _ = outbound_tx
                    .send(with_id(
                        json!({"ok": false, "error": "invalid_token"}),
                        &request.id,
                    ))
                    .await;
                continue;
            }
            let version_in = request.protocol_version.unwrap_or(0);
            if version_in != PROTOCOL_VERSION {
                let _ = outbound_tx
                    .send(with_id(
                        json!({"ok": false, "error": "unsupported_protocol"}),
                        &request.id,
                    ))
                    .await;
                continue;
            }
            authed = true;
            state.claim_control_owner(conn_id).await;
            let _ = outbound_tx
                .send(with_id(
                    json!({
                        "ok": true,
                        "host_epoch": state.host_epoch.as_str(),
                        "version": state.version.as_str(),
                        "protocol_version": PROTOCOL_VERSION,
                    }),
                    &request.id,
                ))
                .await;
            continue;
        }

        let id_key = id.to_string();
        let duplicate = in_flight
            .lock()
            .expect("in-flight request lock poisoned")
            .contains(&id_key);
        if duplicate {
            let _ = outbound_tx
                .send(with_id(
                    json!({"ok": false, "error": "duplicate_id"}),
                    &request.id,
                ))
                .await;
            continue;
        }
        let Ok(permit) = permits.clone().try_acquire_owned() else {
            let _ = outbound_tx
                .send(with_id(
                    json!({"ok": false, "error": "too_many_inflight"}),
                    &request.id,
                ))
                .await;
            continue;
        };
        in_flight
            .lock()
            .expect("in-flight request lock poisoned")
            .insert(id_key.clone());
        if is_mutating(&request.method) {
            if ordered_tx
                .send(QueuedRequest {
                    request,
                    _permit: permit,
                    id_key: id_key.clone(),
                })
                .await
                .is_err()
            {
                in_flight
                    .lock()
                    .expect("in-flight request lock poisoned")
                    .remove(&id_key);
            }
            continue;
        }
        let task_state = state.clone();
        let task_in_flight = in_flight.clone();
        let task_outbound = outbound_tx.clone();
        let task_event_tasks = event_tasks.clone();
        dispatch_tasks.spawn(async move {
            let _permit = permit;
            let DispatchResult { response, events } =
                dispatch(&task_state, conn_id, &request).await;
            if let Some(events) = events {
                let event_outbound = task_outbound.clone();
                let event_task = tokio::spawn(recv_event(events, event_outbound));
                task_event_tasks
                    .lock()
                    .expect("event task lock poisoned")
                    .push(event_task);
            }
            let _ = task_outbound.send(with_id(response, &request.id)).await;
            task_in_flight
                .lock()
                .expect("in-flight request lock poisoned")
                .remove(&id_key);
        });
        while dispatch_tasks.try_join_next().is_some() {}
    }
    dispatch_tasks.abort_all();
    while dispatch_tasks.join_next().await.is_some() {}
    drop(ordered_tx);
    ordered_task.abort();
    let _ = ordered_task.await;
    for task in event_tasks
        .lock()
        .expect("event task lock poisoned")
        .drain(..)
    {
        task.abort();
    }
    state.on_control_disconnect(conn_id).await;
    drop(outbound_tx);
    let _ = writer_task.await;
}

async fn recv_event(mut rx: EventReceiver, outbound: mpsc::Sender<Value>) {
    while let Some(event) = rx.recv().await {
        if outbound.send(event).await.is_err() {
            break;
        }
    }
}

struct DispatchResult {
    response: Value,
    events: Option<EventReceiver>,
}

struct QueuedRequest {
    request: ControlRequest,
    _permit: OwnedSemaphorePermit,
    id_key: String,
}

fn is_mutating(method: &str) -> bool {
    matches!(
        method,
        "spawn" | "kill" | "resize" | "write" | "write_batch"
    )
}

async fn dispatch_ordered(
    state: &Arc<HostState>,
    conn_id: u64,
    request: &ControlRequest,
    ledger: &mut OperationLedger,
) -> DispatchResult {
    if request.method == "spawn" && state.draining.load(std::sync::atomic::Ordering::SeqCst) {
        return DispatchResult {
            response: json!({"ok": false, "error": "host_draining"}),
            events: None,
        };
    }
    let Some(seq) = request.operation_seq else {
        return DispatchResult {
            response: json!({"ok": false, "error": "operation_seq_required"}),
            events: None,
        };
    };
    let fingerprint = fingerprint_json(&request.method, &Value::Object(request.extra.clone()));
    match ledger.decide(seq, fingerprint) {
        LedgerDecision::Gap => DispatchResult {
            response: json!({"ok": false, "error": "operation_gap"}),
            events: None,
        },
        LedgerDecision::Expired => DispatchResult {
            response: json!({"ok": false, "error": "operation_expired"}),
            events: None,
        },
        LedgerDecision::FingerprintMismatch => DispatchResult {
            response: json!({"ok": false, "error": "operation_conflict"}),
            events: None,
        },
        LedgerDecision::Replay(outcome) => DispatchResult {
            response: outcome,
            events: None,
        },
        LedgerDecision::Execute => {
            let result = dispatch(state, conn_id, request).await;
            ledger.record(seq, fingerprint, result.response.clone());
            result
        }
    }
}

async fn dispatch(
    state: &Arc<HostState>,
    conn_id: u64,
    request: &ControlRequest,
) -> DispatchResult {
    let response = match request.method.as_str() {
        "ping" => state.ping_json().await,
        "list" => {
            state.expire_prepared().await;
            state.list_json().await
        }
        "host_shutdown" => {
            state.begin_shutdown(request.grace_ms.unwrap_or(0));
            json!({"ok": true, "accepted": true, "draining": true})
        }
        "spawn" if state.draining.load(std::sync::atomic::Ordering::SeqCst) => {
            json!({"ok": false, "error": "host_draining"})
        }
        "reserve_observer" => state.reserve_observer(conn_id, &request.extra).await,
        "release_observer" => state.release_observer(&request.extra).await,
        "spawn" => state.spawn(conn_id, &request.extra).await,
        "spawn_commit" => state.spawn_commit(&request.extra).await,
        "kill" => state.kill(&request.extra).await,
        "resize" => state.resize(&request.extra).await,
        "write" => state.write(&request.extra).await,
        "write_batch" => state.write_batch(&request.extra).await,
        "snapshot" => state.snapshot(&request.extra).await,
        "subscribe_events" => {
            let (ack, rx) = state
                .subscribe_events(request.extra.get("since").and_then(Value::as_u64))
                .await;
            return DispatchResult {
                response: ack,
                events: Some(rx),
            };
        }
        other => json!({"ok": false, "error": format!("unknown_method:{other}")}),
    };
    DispatchResult {
        response,
        events: None,
    }
}
