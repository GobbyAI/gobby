//! Daemon WebSocket event application.

use super::Workspace;
use crate::daemon::DaemonError;
use serde_json::Value;

pub(super) fn apply(ws: &mut Workspace, message: &Value) -> Result<(), DaemonError> {
    let ty = message.get("type").and_then(Value::as_str).unwrap_or("");
    match ty {
        "attention" => ws.ingest_attention(message.clone()),
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
    if let Some(pane) = ws.pane_for_attachment_mut(attachment) {
        pane.in_flight_write = None;
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
