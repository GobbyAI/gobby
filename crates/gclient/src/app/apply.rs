//! Daemon WebSocket event application.

use super::{ControlState, Workspace};
use crate::daemon::DaemonError;
use base64::Engine;
use serde_json::Value;

pub(super) fn apply(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let ty = message.get("type").and_then(Value::as_str).unwrap_or("");
    match ty {
        "attention" => ws.ingest_attention(message.clone()),
        "terminal_lease_lost" => apply_lease_lost(ws, message),
        "terminal_control_result" => apply_control_result(ws, message),
        "terminal_attachment_finalized" => apply_finalized(ws, message),
        "terminal_ws_fragment" => apply_fragment(ws, message),
        "terminal_write_outcome" => apply_write_outcome(ws, message),
        "terminal_event" => apply_event(ws, message),
        "terminal_output" | "terminal_attach_history" => Ok(()),
        _ => Ok(()),
    }
}

fn apply_lease_lost(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    let gen = message
        .get("lease_generation")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if let Some(pane) = ws.pane_for_attachment_mut(attachment) {
        if gen < pane.lease_generation {
            return Ok(());
        }
        pane.lease_generation = gen;
        pane.control = ControlState::LeaseLost;
        pane.take_back = true;
    }
    Ok(())
}

fn apply_control_result(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    let gen = message
        .get("lease_generation")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let granted = message
        .get("granted")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if let Some(pane) = ws.pane_for_attachment_mut(attachment) {
        if gen < pane.lease_generation {
            return Ok(());
        }
        pane.lease_generation = gen;
        pane.control = if granted {
            ControlState::Held
        } else {
            ControlState::Observe
        };
        pane.take_back = !granted;
    }
    Ok(())
}

fn apply_finalized(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    if let Some(pane) = ws.pane_for_attachment_mut(attachment) {
        pane.live = false;
        pane.fragment = None;
        pane.control = ControlState::Observe;
    }
    Ok(())
}

fn apply_fragment(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    let Some(pane) = ws.pane_for_attachment_mut(attachment) else {
        return Ok(());
    };
    if !pane.live {
        return Ok(());
    }
    let index = message
        .get("fragment_index")
        .and_then(Value::as_u64)
        .unwrap_or(0) as u32;
    let payload = message.get("payload").and_then(Value::as_str).unwrap_or("");
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(payload)
        .unwrap_or_default();
    let acc = pane.fragment.get_or_insert_with(Default::default);
    acc.parts.insert(index, bytes);
    if message.get("more").and_then(Value::as_bool) == Some(false) {
        pane.fragment = None;
        pane.frames_rendered += 1;
    }
    Ok(())
}

fn apply_write_outcome(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    let outcome = message.get("outcome").and_then(Value::as_str).unwrap_or("");
    let reason = message.get("reason").and_then(Value::as_str).unwrap_or("");
    if let Some(pane) = ws.pane_for_attachment_mut(attachment) {
        pane.in_flight_write = None;
        match outcome {
            "delivered" => {
                if pane.control != ControlState::LeaseLost {
                    pane.control = ControlState::Held;
                }
            }
            "indeterminate" => {
                pane.control = ControlState::UncertainReadOnly;
            }
            "refused"
                if matches!(
                    reason,
                    "write_seq_conflict" | "write_seq_expired" | "write_seq_capacity"
                ) => {}
            "refused" => {
                pane.control = ControlState::Observe;
            }
            _ => {}
        }
    }
    Ok(())
}

fn apply_event(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    if message.get("event").and_then(Value::as_str) == Some("exited") {
        if let Some(terminal_id) = message.get("terminal_id").and_then(Value::as_str) {
            ws.remove_terminal(terminal_id);
        }
    }
    Ok(())
}
