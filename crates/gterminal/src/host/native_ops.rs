//! Native-terminal resource lifecycle and frame production.

use std::io;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{Map, Value, json};

use super::helpers::{err, native_entitlements, push_terminal_ansi, s, truncate_title};
use super::state::{CommitState, HostState, Identity, Reservation};
use crate::protocol::{
    DELTA_LAG_TIMEOUT_MS, RenderEncoding, SNAPSHOT_DEFAULT_MAX_BYTES, SNAPSHOT_DEFAULT_MAX_LINES,
    ServerMessage, validate_dimensions,
};

#[derive(Debug)]
pub(crate) enum KillGroupError {
    InvalidPgid,
    Io(io::Error),
}

pub(crate) fn kill_group(pgid: i32, signal: i32) -> Result<(), KillGroupError> {
    if pgid <= 0 {
        return Err(KillGroupError::InvalidPgid);
    }
    // SAFETY: positive process groups are required above; libc reports any OS error.
    if unsafe { libc::killpg(pgid, signal) } == 0 {
        Ok(())
    } else {
        Err(KillGroupError::Io(io::Error::last_os_error()))
    }
}

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

fn targets_tmux(inner: &super::state::Inner, extra: &Map<String, Value>) -> bool {
    if let Some(host_id) = extra.get("host_terminal_id").and_then(Value::as_str) {
        if inner
            .by_host_id
            .get(host_id)
            .and_then(|identity| inner.terminals.get(identity))
            .is_some_and(|slot| slot.locator.is_some())
        {
            return true;
        }
    }
    if let Some(terminal_id) = extra.get("terminal_id").and_then(Value::as_str) {
        return inner.terminals.values().any(|slot| {
            slot.locator.is_some()
                && (slot.identity.terminal_id == terminal_id
                    || slot.host_terminal_id == terminal_id)
        });
    }
    false
}

impl HostState {
    pub async fn reserve_observer(&self, conn_id: u64, extra: &Map<String, Value>) -> Value {
        let terminal_id = extra
            .get("terminal_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let reserve_key = extra
            .get("reserve_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if terminal_id.is_empty() || reserve_key.is_empty() {
            return err("invalid_request");
        }
        let mut inner = self.inner.lock().await;
        if targets_tmux(&inner, extra) {
            return err("not_native");
        }
        if let Some(existing) = inner.reservations.values().find(|res| {
            res.conn_id == conn_id && res.terminal_id == terminal_id && res.key == reserve_key
        }) {
            return json!({
                "ok": true,
                "reservation_id": existing.id,
                "reserve_key": existing.key,
                "reserve_generation": existing.generation,
            });
        }
        if native_entitlements(&inner) >= self.config.native_entitlement_ceiling() {
            return err("capacity");
        }
        let reservation_id = format!("rsv-{}", uuid::Uuid::new_v4());
        inner.reservations.insert(
            reservation_id.clone(),
            Reservation {
                id: reservation_id.clone(),
                key: reserve_key.clone(),
                generation: 1,
                terminal_id,
                conn_id,
                identity: None,
                prepared: false,
            },
        );
        json!({
            "ok": true,
            "reservation_id": reservation_id,
            "reserve_key": reserve_key,
            "reserve_generation": 1,
        })
    }

    pub async fn release_observer(&self, extra: &Map<String, Value>) -> Value {
        let reservation_id = extra
            .get("reservation_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let reserve_key = extra
            .get("reserve_key")
            .and_then(Value::as_str)
            .unwrap_or("");
        let mut inner = self.inner.lock().await;
        if targets_tmux(&inner, extra) {
            return err("not_native");
        }
        if let Some(res) = inner.reservations.get(reservation_id) {
            if res.prepared || (res.key != reserve_key && !reserve_key.is_empty()) {
                return json!({"ok": true, "released": false});
            }
            inner.reservations.remove(reservation_id);
        }
        json!({"ok": true, "released": true})
    }

    pub async fn spawn_commit(&self, extra: &Map<String, Value>) -> Value {
        let identity = Identity {
            terminal_id: s(extra, "terminal_id"),
            spawn_key: s(extra, "spawn_key"),
        };
        let mut inner = self.inner.lock().await;
        let Some(slot) = inner.terminals.get_mut(&identity) else {
            return err("not_found");
        };
        if slot.commit_state == CommitState::Committed {
            return json!({
                "ok": true,
                "host_terminal_id": slot.host_terminal_id,
                "commit_state": "committed",
            });
        }
        #[cfg(feature = "vt-engine")]
        if let Some(child) = slot.child.as_mut() {
            if child.commit().is_err() {
                return err("commit_failed");
            }
        }
        slot.commit_state = CommitState::Committed;
        slot.commit_deadline = None;
        json!({
            "ok": true,
            "host_terminal_id": slot.host_terminal_id,
            "commit_state": "committed",
        })
    }

    pub async fn kill(&self, extra: &Map<String, Value>) -> Value {
        let host_terminal_id = s(extra, "host_terminal_id");
        let grace_ms = extra.get("grace_ms").and_then(Value::as_u64).unwrap_or(100);
        let mut inner = self.inner.lock().await;
        let Some(identity) = inner.by_host_id.get(&host_terminal_id).cloned() else {
            return json!({"ok": true, "killed": false});
        };
        if inner
            .terminals
            .get(&identity)
            .is_some_and(|slot| slot.locator.is_some())
        {
            return err("not_native");
        }
        if let Some(slot) = inner.terminals.remove(&identity) {
            inner.by_host_id.remove(&host_terminal_id);
            inner.reservations.remove(&slot.reservation_id);
            let _ = kill_group(slot.pgid, libc::SIGTERM);
            let pgid = slot.pgid;
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(grace_ms)).await;
                let _ = kill_group(pgid, libc::SIGKILL);
            });
        }
        json!({"ok": true, "killed": true})
    }

    pub async fn resize(&self, extra: &Map<String, Value>) -> Value {
        let host_terminal_id = s(extra, "host_terminal_id");
        let rows = extra.get("rows").and_then(Value::as_i64).unwrap_or(0);
        let cols = extra.get("cols").and_then(Value::as_i64).unwrap_or(0);
        let dims = match validate_dimensions(rows, cols) {
            Ok(dims) => dims,
            Err(e) => return err(e.code()),
        };
        let mut inner = self.inner.lock().await;
        let Some(identity) = inner.by_host_id.get(&host_terminal_id).cloned() else {
            return err("not_found");
        };
        let Some(slot) = inner.terminals.get_mut(&identity) else {
            return err("not_found");
        };
        if slot.locator.is_some() {
            return err("not_native");
        }
        #[cfg(feature = "vt-engine")]
        if let Some(child) = slot.child.as_ref() {
            child.runtime.resize(dims.0, dims.1, 0, 0);
        }
        slot.rows = dims.0;
        slot.cols = dims.1;
        slot.last_seq += 1;
        json!({"ok": true, "rows": dims.0, "cols": dims.1})
    }

    pub async fn snapshot(&self, extra: &Map<String, Value>) -> Value {
        let host_terminal_id = s(extra, "host_terminal_id");
        let max_bytes = extra
            .get("max_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(SNAPSHOT_DEFAULT_MAX_BYTES as u64) as usize;
        let max_lines = extra
            .get("max_lines")
            .and_then(Value::as_u64)
            .unwrap_or(SNAPSHOT_DEFAULT_MAX_LINES as u64) as usize;
        let inner = self.inner.lock().await;
        let Some(identity) = inner.by_host_id.get(&host_terminal_id).cloned() else {
            return err("not_found");
        };
        let Some(slot) = inner.terminals.get(&identity) else {
            return err("not_found");
        };
        if slot.locator.is_some() {
            return err("not_native");
        }
        #[cfg(feature = "vt-engine")]
        let text = if let Some(child) = slot.child.as_ref() {
            let history = child.runtime.snapshot_history().unwrap_or_default();
            if history.is_empty() {
                child.runtime.visible_text()
            } else {
                history
            }
        } else {
            String::new()
        };
        #[cfg(not(feature = "vt-engine"))]
        let text = String::new();
        let total_bytes = text.len() as u64;
        let mut truncated = false;
        let mut dropped_bytes = 0u64;
        let mut lines: Vec<&str> = text.lines().collect();
        if lines.len() > max_lines {
            dropped_bytes += lines[..lines.len() - max_lines]
                .iter()
                .map(|line| line.len() as u64 + 1)
                .sum::<u64>();
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
        json!({
            "ok": true,
            "text": joined,
            "truncated": truncated,
            "dropped_bytes": dropped_bytes,
            "total_bytes": total_bytes,
        })
    }

    pub async fn expire_prepared(&self) {
        let mut inner = self.inner.lock().await;
        let now = Instant::now();
        let expired: Vec<Identity> = inner
            .terminals
            .iter()
            .filter(|(_, slot)| {
                slot.commit_state == CommitState::Prepared
                    && slot.commit_deadline.is_some_and(|deadline| deadline <= now)
            })
            .map(|(identity, _)| identity.clone())
            .collect();
        for identity in expired {
            if let Some(slot) = inner.terminals.remove(&identity) {
                inner.by_host_id.remove(&slot.host_terminal_id);
                inner.reservations.remove(&slot.reservation_id);
                let _ = kill_group(slot.pgid, libc::SIGKILL);
            }
        }
    }

    pub async fn broadcast_frames(self: &Arc<Self>) {
        let mut inner = self.inner.lock().await;
        let ids: Vec<u64> = inner.attachments.keys().copied().collect();
        for id in ids {
            let (host_id, rows, cols, scroll, encoding) = {
                let Some(att) = inner.attachments.get(&id) else {
                    continue;
                };
                (
                    att.host_terminal_id.clone(),
                    att.rows,
                    att.cols,
                    att.scroll,
                    att.encoding,
                )
            };
            let Some(identity) = inner.by_host_id.get(&host_id).cloned() else {
                continue;
            };
            if inner
                .terminals
                .get(&identity)
                .is_some_and(|slot| slot.locator.is_some())
            {
                continue;
            }
            #[cfg(feature = "vt-engine")]
            let (frame, seq) = {
                let Some(slot) = inner.terminals.get_mut(&identity) else {
                    continue;
                };
                let Some(child) = slot.child.as_mut() else {
                    continue;
                };
                if scroll > 0 {
                    child.runtime.set_scroll_offset_from_bottom(scroll as usize);
                } else {
                    child.runtime.scroll_reset();
                }
                let frame = child.runtime.frame_data(cols, rows);
                if scroll > 0 {
                    child.runtime.scroll_reset();
                }
                let title = child.runtime.osc_title();
                if slot.title != title {
                    slot.title = truncate_title(&title);
                    slot.last_seq += 1;
                }
                (frame, slot.last_seq)
            };
            #[cfg(not(feature = "vt-engine"))]
            let (frame, seq) = {
                let _ = scroll;
                (
                    crate::protocol::FrameData {
                        cells: Vec::new(),
                        width: cols,
                        height: rows,
                        cursor: None,
                        hyperlinks: Vec::new(),
                        graphics: Vec::new(),
                        modes: crate::protocol::PaneModes::default(),
                    },
                    inner
                        .terminals
                        .get(&identity)
                        .map_or(0, |slot| slot.last_seq),
                )
            };
            if let Some(att) = inner.attachments.get_mut(&id) {
                let sent = match encoding {
                    RenderEncoding::SemanticFrame => {
                        match att.tx.try_send(ServerMessage::Frame(frame)) {
                            Ok(()) => {
                                att.last_send = Instant::now();
                                att.desynced = false;
                                true
                            }
                            Err(_) => {
                                att.desynced = true;
                                false
                            }
                        }
                    }
                    RenderEncoding::TerminalAnsi => push_terminal_ansi(att, &frame, seq),
                };
                if sent {
                    att.delta_len = att.delta_len.saturating_add(1);
                }
            }
        }
    }

    pub fn lag_timeout(&self) -> Duration {
        Duration::from_millis(DELTA_LAG_TIMEOUT_MS)
    }
}

#[cfg(test)]
mod tests;
