//! Daemon WebSocket event application.

use super::{ControlState, Workspace};
use crate::daemon::DaemonError;
use serde_json::Value;

pub(super) fn apply(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let ty = message.get("type").and_then(Value::as_str).unwrap_or("");
    match ty {
        "attention" => ws.ingest_attention(message.clone()),
        "terminal_lease_lost" => apply_lease_lost(ws, message),
        "terminal_control_result" => apply_control_result(ws, message),
        "terminal_attach_result" => ws.apply_scripted_attach_result(message),
        "terminal_attachment_finalized" => apply_finalized(ws, message),
        "terminal_detach_result" => apply_detach_result(ws, message),
        "terminal_ws_fragment" => Ok(()),
        "terminal_write_outcome" => apply_write_outcome(ws, message),
        "terminal_kill_result" => apply_kill_result(ws, message),
        "terminal_event" => apply_event(ws, message),
        "terminal_resize_result" => {
            ws.note_resize_result(message);
            Ok(())
        }
        "terminal_output" | "terminal_attach_history" => Ok(()),
        _ => Ok(()),
    }
}

fn apply_kill_result(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(terminal_id) = message.get("terminal_id").and_then(Value::as_str) else {
        return Ok(());
    };
    if message.get("success").and_then(Value::as_bool) == Some(true) {
        if let Some(id) = ws.pane_for_terminal(terminal_id) {
            if let Some(pane) = ws.panes.get_mut(&id) {
                pane.terminating = true;
                pane.control = ControlState::Observe;
                pane.clear_pending_input();
            }
        }
    } else {
        ws.status_message = Some(
            message
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("terminate refused")
                .to_string(),
        );
    }
    Ok(())
}

fn apply_detach_result(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    if message.get("success").and_then(Value::as_bool) != Some(true) {
        return Ok(());
    }
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    ws.advance_scripted_detach(attachment)
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
        if gen < pane.lease_generation() {
            return Ok(());
        }
        pane.set_lease_generation(gen);
        pane.control = ControlState::LeaseLost;
        pane.take_back = true;
        pane.clear_pending_input();
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
    let host_input_granted = message.get("host_input_granted").and_then(Value::as_bool);
    let mut pending = Vec::new();
    let mut pane_id = None;
    if let Some(pane) = ws.pane_for_attachment_mut(attachment) {
        if gen < pane.lease_generation() {
            return Ok(());
        }
        pane.set_lease_generation(gen);
        pane.control = if granted {
            ControlState::Held
        } else {
            ControlState::Observe
        };
        pane.take_back = !granted;
        if granted {
            // A direct native pane the host did not grant cannot type there,
            // and gclient never falls back to daemon-mediated keys (#22573),
            // so `apply_host_grant` hands the pane back to take-back instead.
            if pane.apply_host_grant(host_input_granted) {
                pane_id = Some(pane.id);
                pending = pane.take_pending_input();
            }
        } else {
            pane.clear_pending_input();
        }
    }
    if let Some(pane_id) = pane_id {
        // Everything typed since the take-control request went out, in the
        // order it was typed (#22573).
        for input in pending {
            match input.parts() {
                (data, false) => ws.send_input(pane_id, data)?,
                (data, true) => ws.paste_to_pty(pane_id, &String::from_utf8_lossy(data))?,
            }
        }
    }
    Ok(())
}

fn apply_finalized(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let Some(attachment) = message.get("attachment_id").and_then(Value::as_str) else {
        return Ok(());
    };
    ws.retire_attachment(attachment, message.get("reason").and_then(Value::as_str));
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
    let event = message.get("event").and_then(Value::as_str).unwrap_or("");
    let terminal = message.get("terminal").unwrap_or(message);
    let terminal_id = message
        .get("terminal_id")
        .or_else(|| terminal.get("terminal_id"))
        .or_else(|| terminal.get("id"))
        .and_then(Value::as_str);
    match event {
        "created" => {
            let Some(terminal_id) = terminal_id else {
                return Ok(());
            };
            ws.pending_spawns.remove(terminal_id);
            if ws.pane_for_terminal(terminal_id).is_none() {
                let backend = terminal
                    .get("backend")
                    .and_then(Value::as_str)
                    .unwrap_or("native");
                ws.open_terminal(terminal_id, backend, "")
                    .map_err(|error| DaemonError::Protocol {
                        detail: error.to_string(),
                    })?;
            }
        }
        "exited" | "killed" | "terminated" | "orphaned" => {
            let Some(terminal_id) = terminal_id else {
                return Ok(());
            };
            ws.remove_terminal(terminal_id);
        }
        _ => {}
    }
    Ok(())
}
