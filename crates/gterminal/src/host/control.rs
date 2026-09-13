//! Newline-delimited JSON control protocol for the gterm host.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::io;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{unix::OwnedReadHalf, UnixStream};

use super::ledger::{fingerprint_json, LedgerDecision, OperationLedger};
use super::state::HostState;

pub const PROTOCOL_VERSION: u32 = 1;
const MAX_CONTROL_LINE: usize = 2 * 1024 * 1024;

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

async fn write_json(writer: &mut tokio::net::unix::OwnedWriteHalf, value: Value) -> io::Result<()> {
    let mut line = value.to_string();
    if line.len() >= 2 * 1024 * 1024 {
        line = json!({"ok": false, "error": "response_too_large"}).to_string();
    }
    line.push('\n');
    writer.write_all(line.as_bytes()).await?;
    writer.flush().await
}

fn with_id(mut value: Value, id: &Option<Value>) -> Value {
    if let (Some(id), Some(obj)) = (id, value.as_object_mut()) {
        obj.insert("id".to_string(), id.clone());
    }
    value
}

pub async fn handle_connection(stream: UnixStream, state: Arc<HostState>) {
    let conn_id = state.alloc_conn();
    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut request_buffer = Vec::new();
    let mut authed = false;
    let mut ledger = OperationLedger::default();
    let mut events_rx: Option<tokio::sync::mpsc::Receiver<Value>> = None;

    loop {
        tokio::select! {
            request = read_request(&mut reader, &mut request_buffer) => {
                let request = match request {
                    Ok(RequestRead::Request(request)) => request,
                    Ok(RequestRead::InvalidJson) => {
                        let _ = write_json(
                            &mut writer,
                            json!({"ok": false, "error": "invalid_json"}),
                        )
                        .await;
                        continue;
                    }
                    Ok(RequestRead::Overflow) => {
                        let _ = write_json(
                            &mut writer,
                            json!({"ok": false, "error": "control_overflow"}),
                        )
                        .await;
                        break;
                    }
                    Ok(RequestRead::Closed) | Err(_) => break,
                };
                if !authed {
                    if request.method != "hello" {
                        let _ = write_json(
                            &mut writer,
                            json!({"ok": false, "error": "unauthenticated"}),
                        )
                        .await;
                        break;
                    }
                    let presented = request.control_token.as_deref().unwrap_or("");
                    if presented != state.token.as_str() {
                        let _ = write_json(
                            &mut writer,
                            with_id(json!({"ok": false, "error": "invalid_token"}), &request.id),
                        )
                        .await;
                        continue;
                    }
                    let version_in = request.protocol_version.unwrap_or(0);
                    if version_in != PROTOCOL_VERSION {
                        let _ = write_json(
                            &mut writer,
                            with_id(
                                json!({"ok": false, "error": "unsupported_protocol"}),
                                &request.id,
                            ),
                        )
                        .await;
                        continue;
                    }
                    authed = true;
                    state.claim_control_owner(conn_id).await;
                    let _ = write_json(
                        &mut writer,
                        with_id(
                            json!({
                                "ok": true,
                                "host_epoch": state.host_epoch.as_str(),
                                "version": state.version.as_str(),
                                "protocol_version": PROTOCOL_VERSION,
                            }),
                            &request.id,
                        ),
                    )
                    .await;
                    continue;
                }

                if request.method == "spawn"
                    && state.draining.load(std::sync::atomic::Ordering::SeqCst)
                {
                    let _ = write_json(
                        &mut writer,
                        with_id(json!({"ok": false, "error": "host_draining"}), &request.id),
                    )
                    .await;
                    continue;
                }
                let mutating = matches!(
                    request.method.as_str(),
                    "spawn" | "kill" | "resize" | "write" | "write_batch"
                );
                if mutating {
                    let Some(seq) = request.operation_seq else {
                        let _ = write_json(
                            &mut writer,
                            with_id(json!({"ok": false, "error": "operation_seq_required"}), &request.id),
                        )
                        .await;
                        continue;
                    };
                    let fingerprint = fingerprint_json(&request.method, &Value::Object(request.extra.clone()));
                    match ledger.decide(seq, fingerprint) {
                        LedgerDecision::Gap => {
                            let _ = write_json(
                                &mut writer,
                                with_id(json!({"ok": false, "error": "operation_gap"}), &request.id),
                            )
                            .await;
                            continue;
                        }
                        LedgerDecision::Expired => {
                            let _ = write_json(
                                &mut writer,
                                with_id(json!({"ok": false, "error": "operation_expired"}), &request.id),
                            )
                            .await;
                            continue;
                        }
                        LedgerDecision::FingerprintMismatch => {
                            let _ = write_json(
                                &mut writer,
                                with_id(json!({"ok": false, "error": "operation_conflict"}), &request.id),
                            )
                            .await;
                            continue;
                        }
                        LedgerDecision::Replay(outcome) => {
                            let _ = write_json(&mut writer, with_id(outcome, &request.id)).await;
                            continue;
                        }
                        LedgerDecision::Execute => {
                            let outcome = dispatch(&state, conn_id, &request, &mut events_rx).await;
                            ledger.record(seq, fingerprint, outcome.clone());
                            let _ = write_json(&mut writer, with_id(outcome, &request.id)).await;
                            continue;
                        }
                    }
                }

                let outcome = dispatch(&state, conn_id, &request, &mut events_rx).await;
                let _ = write_json(&mut writer, with_id(outcome, &request.id)).await;
            }
            event = recv_event(&mut events_rx) => {
                if let Some(event) = event {
                    let _ = write_json(&mut writer, event).await;
                }
            }
        }
    }
    state.on_control_disconnect(conn_id).await;
}

async fn recv_event(rx: &mut Option<tokio::sync::mpsc::Receiver<Value>>) -> Option<Value> {
    match rx.as_mut() {
        Some(rx) => rx.recv().await,
        None => std::future::pending().await,
    }
}

async fn dispatch(
    state: &Arc<HostState>,
    conn_id: u64,
    request: &ControlRequest,
    events_rx: &mut Option<tokio::sync::mpsc::Receiver<Value>>,
) -> Value {
    match request.method.as_str() {
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
            let (ack, rx) = state.subscribe_events().await;
            *events_rx = Some(rx);
            ack
        }
        other => json!({"ok": false, "error": format!("unknown_method:{other}")}),
    }
}
