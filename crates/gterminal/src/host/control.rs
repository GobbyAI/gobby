//! Newline-delimited JSON control protocol for the gterm host.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

pub const PROTOCOL_VERSION: u32 = 1;

#[derive(Debug, Deserialize)]
pub struct ControlRequest {
    pub method: String,
    #[serde(default)]
    pub protocol_version: Option<u32>,
    #[serde(default)]
    pub control_token: Option<String>,
    #[serde(default)]
    pub grace_ms: Option<u64>,
}

async fn write_json(writer: &mut tokio::net::unix::OwnedWriteHalf, value: Value) -> io::Result<()> {
    let mut line = value.to_string();
    line.push('\n');
    writer.write_all(line.as_bytes()).await?;
    writer.flush().await
}

pub async fn handle_connection(
    stream: UnixStream,
    token: Arc<String>,
    host_epoch: Arc<String>,
    version: Arc<String>,
    host_pid: u32,
    draining: Arc<AtomicBool>,
    shutdown: tokio::sync::watch::Sender<bool>,
    shutdown_grace_ms: u64,
) {
    let (reader, mut writer) = stream.into_split();
    let mut lines = BufReader::new(reader).lines();
    let mut authed = false;

    while let Ok(Some(line)) = lines.next_line().await {
        let request: ControlRequest = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(_) => {
                let _ =
                    write_json(&mut writer, json!({"ok": false, "error": "invalid_json"})).await;
                continue;
            }
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
            if presented != token.as_str() {
                let _ =
                    write_json(&mut writer, json!({"ok": false, "error": "invalid_token"})).await;
                continue;
            }
            let version_in = request.protocol_version.unwrap_or(0);
            if version_in != PROTOCOL_VERSION {
                let _ = write_json(
                    &mut writer,
                    json!({"ok": false, "error": "unsupported_protocol"}),
                )
                .await;
                continue;
            }
            authed = true;
            let _ = write_json(
                &mut writer,
                json!({
                    "ok": true,
                    "host_epoch": host_epoch.as_str(),
                    "version": version.as_str(),
                    "protocol_version": PROTOCOL_VERSION,
                }),
            )
            .await;
            continue;
        }

        let response = match request.method.as_str() {
            "ping" => json!({
                "ok": true,
                "host_epoch": host_epoch.as_str(),
                "version": version.as_str(),
                "host_pid": host_pid,
            }),
            "list" => json!({
                "ok": true,
                "terminals": [],
            }),
            "host_shutdown" => {
                let _grace = request.grace_ms.unwrap_or(0).min(shutdown_grace_ms);
                draining.store(true, Ordering::SeqCst);
                let _ = shutdown.send(true);
                json!({"ok": true, "accepted": true, "draining": true})
            }
            "spawn" | "attach" if draining.load(Ordering::SeqCst) => {
                json!({"ok": false, "error": "host_draining"})
            }
            "spawn" | "attach" | "kill" | "spawn_commit" => {
                json!({"ok": false, "error": "not_implemented"})
            }
            other => json!({"ok": false, "error": format!("unknown_method:{other}")}),
        };
        let _ = write_json(&mut writer, response).await;
    }
}
