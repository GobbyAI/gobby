//! Resolve history identity briefly; capture only on host-owned blocking work.
use super::helpers::{err, s};
use super::state::HostState;
use crate::protocol::{SnapshotMode, SNAPSHOT_DEFAULT_MAX_BYTES, SNAPSHOT_DEFAULT_MAX_LINES};
use serde_json::{json, Map, Value};
use std::time::Instant;

pub(crate) fn trim_to_char_boundary(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return text.to_string();
    }
    let mut start = text.len() - max_bytes;
    while start < text.len() && !text.is_char_boundary(start) {
        start += 1;
    }
    text[start..].to_string()
}

fn invalid_snapshot_mode() -> Value {
    json!({
        "ok": false,
        "error": "invalid_mode",
        "valid_modes": SnapshotMode::VALID,
    })
}

pub(super) fn snapshot_reply(mode: SnapshotMode, body: SnapshotBody) -> Value {
    json!({
        "ok": true,
        "mode": mode.as_wire(),
        "text": body.text,
        "truncated": body.truncated,
        "dropped_bytes": body.dropped_bytes,
        "total_bytes": body.total_bytes,
    })
}

pub(super) fn snapshot_mode(extra: &Map<String, Value>) -> Result<SnapshotMode, Value> {
    match extra.get("mode") {
        None => Ok(SnapshotMode::Text),
        Some(Value::String(value)) => {
            SnapshotMode::from_wire(value).ok_or_else(invalid_snapshot_mode)
        }
        Some(_) => Err(invalid_snapshot_mode()),
    }
}

#[derive(Clone, Debug)]
pub(super) struct SnapshotBody {
    pub(super) text: String,
    pub(super) truncated: bool,
    pub(super) dropped_bytes: Option<u64>,
    pub(super) total_bytes: Option<u64>,
}

pub(super) fn truncate_snapshot(text: &str, max_lines: usize, max_bytes: usize) -> SnapshotBody {
    let total_bytes = text.len() as u64;
    let mut truncated = false;
    let mut dropped_bytes = 0u64;
    let mut lines: Vec<&str> = text.lines().collect();
    if lines.len() > max_lines {
        dropped_bytes = lines[..lines.len() - max_lines]
            .iter()
            .map(|line| line.len() as u64 + 1)
            .sum::<u64>()
            .min(total_bytes);
        lines = lines[lines.len() - max_lines..].to_vec();
        truncated = true;
    }
    let mut joined = lines.join("\n");
    if joined.len() > max_bytes {
        let before = joined.len();
        joined = trim_to_char_boundary(&joined, max_bytes);
        dropped_bytes += (before - joined.len()) as u64;
        truncated = true;
    }
    SnapshotBody {
        text: joined,
        truncated,
        dropped_bytes: Some(dropped_bytes),
        total_bytes: Some(total_bytes),
    }
}

impl HostState {
    pub async fn snapshot(&self, extra: &Map<String, Value>) -> Value {
        let mode = match snapshot_mode(extra) {
            Ok(mode) => mode,
            Err(refusal) => return refusal,
        };
        let host_id = s(extra, "host_terminal_id");
        let max_bytes = extra
            .get("max_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(SNAPSHOT_DEFAULT_MAX_BYTES as u64) as usize;
        let max_lines = extra
            .get("max_lines")
            .and_then(Value::as_u64)
            .unwrap_or(SNAPSHOT_DEFAULT_MAX_LINES as u64) as usize;
        #[cfg(feature = "vt-engine")]
        let capture;
        #[cfg(feature = "vt-engine")]
        let terminal;
        {
            let inner = self.inner.lock().await;
            let Some(identity) = inner.by_host_id.get(&host_id) else {
                let tail = inner
                    .exit_tails
                    .get_snapshot(&host_id, Instant::now())
                    .cloned();
                drop(inner);
                return match tail {
                    Some(Ok(tail)) if mode == SnapshotMode::Text => {
                        let mut body = truncate_snapshot(&tail.text, max_lines, max_bytes);
                        body.truncated |= tail.truncated;
                        body.total_bytes = tail.total_bytes;
                        body.dropped_bytes = tail
                            .dropped_bytes
                            .zip(body.dropped_bytes)
                            .map(|(prior, extra)| prior.saturating_add(extra));
                        snapshot_reply(mode, body)
                    }
                    Some(Err(code)) => err(code),
                    _ => err("not_found"),
                };
            };
            let Some(slot) = inner.terminals.get(identity) else {
                return err("not_found");
            };
            if slot.locator.is_some() {
                return err("not_native");
            }
            #[cfg(feature = "vt-engine")]
            {
                let Some(child) = slot.child.as_ref() else {
                    return snapshot_reply(mode, truncate_snapshot("", max_lines, max_bytes));
                };
                terminal = child.runtime.terminal_handle();
                capture = match terminal.begin_history_capture() {
                    Ok(capture) => capture,
                    Err(code) => return err(code),
                };
            }
        }
        #[cfg(feature = "vt-engine")]
        {
            let result =
                tokio::task::spawn_blocking(move || capture.read(mode, max_lines, max_bytes)).await;
            let inner = self.inner.lock().await;
            let alive = inner
                .by_host_id
                .get(&host_id)
                .and_then(|id| inner.terminals.get(id))
                .and_then(|slot| slot.child.as_ref())
                .is_some_and(|child| {
                    std::sync::Arc::ptr_eq(&terminal, &child.runtime.terminal_handle())
                });
            drop(inner);
            if !alive {
                return err("terminal_gone");
            }
            match result {
                Ok(Ok(body)) => json!({"ok":true, "mode":mode.as_wire(), "text":body.text,
                    "truncated":body.truncated, "total_bytes":body.total_bytes,
                    "dropped_bytes":body.dropped_bytes}),
                Ok(Err(code)) => err(code),
                Err(_) => err("capture_failed"),
            }
        }
        #[cfg(not(feature = "vt-engine"))]
        snapshot_reply(mode, truncate_snapshot("", max_lines, max_bytes))
    }
}
