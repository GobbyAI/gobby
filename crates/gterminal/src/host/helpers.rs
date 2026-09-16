//! Shared host helpers kept out of `state.rs` for the line ceiling.

use std::path::PathBuf;
use std::time::Instant;

use serde_json::{json, Map, Value};

use super::backpressure::PushResult;
use super::state::{Attachment, CommitState, Inner, ObserverBind};
use crate::protocol::{
    FrameData, ObservationReason, ObservationState, ServerMessage, TerminalFrame, TITLE_MAX_BYTES,
};

pub(crate) fn list_rows(inner: &Inner) -> Vec<Value> {
    inner
        .terminals
        .values()
        .map(|slot| {
            let (observer_bind, reservation_id, reserve_generation) = match &slot.observer_bind {
                ObserverBind::None => ("none", None, None),
                #[cfg(feature = "vt-engine")]
                ObserverBind::Reserved {
                    reservation_id,
                    generation,
                } => ("reserved", Some(reservation_id.clone()), Some(*generation)),
                ObserverBind::Bound {
                    reservation_id,
                    generation,
                    ..
                } => ("bound", Some(reservation_id.clone()), Some(*generation)),
                ObserverBind::Entitled {
                    reservation_id,
                    generation,
                } => ("entitled", Some(reservation_id.clone()), Some(*generation)),
            };
            json!({
                "host_terminal_id": slot.host_terminal_id,
                "terminal_id": slot.identity.terminal_id,
                "spawn_key": slot.identity.spawn_key,
                "title": slot.title,
                "rows": slot.rows,
                "cols": slot.cols,
                "pgid": slot.pgid,
                "start_time": slot.start_time,
                "last_seq": slot.last_seq,
                "commit_state": match slot.commit_state {
                    CommitState::Prepared => "prepared",
                    CommitState::Committed => "committed",
                },
                "observer_bind": observer_bind,
                "reservation_id": reservation_id,
                "reserve_generation": reserve_generation,
                "observation_state": match slot.observation_state {
                    ObservationState::Live => "live",
                    ObservationState::Stale => "stale",
                    ObservationState::OrphanedObservation => "orphaned_observation",
                },
                "observation_reason": slot.observation_reason.map(ObservationReason::as_str),
                "observation_generation": slot.observation_generation,
                "tmux_history_bytes": slot.tmux_history_bytes,
            })
        })
        .collect()
}

pub(crate) fn native_entitlements(inner: &Inner) -> u32 {
    inner
        .terminals
        .values()
        .filter(|slot| !matches!(slot.observer_bind, ObserverBind::None))
        .count() as u32
        + inner
            .reservations
            .values()
            .filter(|res| !res.prepared)
            .count() as u32
}

pub(crate) fn spawn_fingerprint(
    argv: &[String],
    env: &[(String, String)],
    cwd: &PathBuf,
    dims: (u16, u16),
    reservation_id: &str,
    reserve_key: &str,
) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    argv.hash(&mut hasher);
    env.hash(&mut hasher);
    cwd.hash(&mut hasher);
    dims.hash(&mut hasher);
    reservation_id.hash(&mut hasher);
    reserve_key.hash(&mut hasher);
    hasher.finish()
}

pub(crate) fn named_key_bytes(name: &str) -> Vec<u8> {
    match name {
        "Enter" | "enter" => b"\r".to_vec(),
        "Tab" => b"\t".to_vec(),
        "Esc" => b"\x1b".to_vec(),
        "Backspace" => vec![0x7f],
        other => other.as_bytes().to_vec(),
    }
}

pub(crate) fn s(extra: &Map<String, Value>, key: &str) -> String {
    extra
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

pub(crate) fn err(code: &str) -> Value {
    json!({"ok": false, "error": code})
}

pub fn truncate_title(text: &str) -> String {
    let mut bytes = 0usize;
    let mut end = 0usize;
    for (idx, ch) in text.char_indices() {
        let n = ch.len_utf8();
        if bytes + n > TITLE_MAX_BYTES {
            break;
        }
        bytes += n;
        end = idx + n;
    }
    text[..end].to_string()
}

/// Encode `frame` for a `terminal_ansi` attachment and queue it on the
/// attachment's channel; returns whether a message was queued.
///
/// A synced attachment whose last committed frame equals `frame` sends
/// nothing. A delta that will not fit is replaced by one keyframe: queuing that
/// keyframe commits it as the encoder baseline, which resyncs the attachment,
/// so later frames are deltas against it instead of repeated full repaints.
pub(crate) fn push_terminal_ansi(
    att: &mut Attachment,
    frame: &FrameData,
    seq: u64,
    cap: usize,
) -> bool {
    if !att.desynced && att.encoder.is_current(frame) {
        return false;
    }
    let mut encoded = att.encoder.encode(frame, att.desynced);
    let bytes = std::mem::take(&mut encoded.bytes);
    let msg = ServerMessage::Terminal(TerminalFrame {
        seq,
        width: frame.width,
        height: frame.height,
        full: encoded.full,
        bytes,
    });
    if att.desynced && att.mailbox.queued_bytes() > 0 {
        queue_replacement_keyframe(att, frame, seq, cap);
        return true;
    }
    if att.desynced && att.mailbox.is_writing() {
        return true;
    }
    match att.mailbox.try_push(&msg, cap) {
        PushResult::Queued => {
            att.encoder.commit(frame.clone(), encoded);
            att.desynced = false;
            att.last_send = Instant::now();
            true
        }
        PushResult::Overflow => {
            queue_replacement_keyframe(att, frame, seq, cap);
            true
        }
        PushResult::Closed => {
            att.desynced = true;
            false
        }
    }
}

/// Queue one full repaint in place of everything the attachment still owes its
/// peer. A queued keyframe is the resync: it becomes the encoder baseline, so
/// the attachment is only left desynced when the mailbox refuses it.
fn queue_replacement_keyframe(att: &mut Attachment, frame: &FrameData, seq: u64, cap: usize) {
    let mut keyframe = att.encoder.encode(frame, true);
    let keyframe_bytes = std::mem::take(&mut keyframe.bytes);
    let keyframe_msg = ServerMessage::Terminal(TerminalFrame {
        seq,
        width: frame.width,
        height: frame.height,
        full: true,
        bytes: keyframe_bytes,
    });
    if att.mailbox.replace_with_keyframe(&keyframe_msg, cap) {
        att.encoder.commit(frame.clone(), keyframe);
        att.desynced = false;
    } else {
        att.desynced = true;
    }
    att.last_send = Instant::now();
}

#[cfg(test)]
#[path = "helpers/tests.rs"]
mod tests;
