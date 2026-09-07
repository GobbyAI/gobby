//! Pane control for the live loop: focus, take/release control, and the
//! daemon writes that carry pane input.

use serde_json::{json, Value};
use tokio::time::Instant;

use crate::daemon::{Daemon, DaemonError, LiveDaemon};
use crate::frame_source::FrameError;

use super::super::{ControlState, PaneId, Workspace};

pub(super) async fn focus_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let previous = workspace.focus.replace(pane_id);
    if let Some(previous) = previous.filter(|previous| *previous != pane_id) {
        release_live_control(workspace, previous).await?;
    }
    if !workspace.pane(pane_id).is_held() {
        take_live_control(workspace, pane_id).await?;
    }
    Ok(())
}

pub(super) async fn take_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let pane = workspace.pane(pane_id);
    if !pane.is_live() {
        return Ok(());
    }
    let attachment_id = pane.attachment_id().to_string();
    let terminal_id = pane.terminal_id.clone();
    let reply = match workspace
        .daemon()
        .send(json!({
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
            "takeover": false,
        }))
        .await
    {
        Ok(reply) => reply,
        Err(error) => {
            retire_live_control(workspace, pane_id).await;
            return Err(FrameError::from(error));
        }
    };
    let generation = reply
        .get("lease_generation")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let granted = reply
        .get("granted")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let refusal_reason = reply
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("control request denied")
        .to_string();
    let pending = {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        if generation < pane.lease_generation() {
            return Ok(());
        }
        pane.set_lease_generation(generation);
        pane.control = if granted {
            ControlState::Held
        } else {
            ControlState::Observe
        };
        pane.take_back = !granted;
        if granted {
            pane.pending_input.take()
        } else {
            pane.pending_input = None;
            None
        }
    };
    if let Some(data) = pending {
        send_live_write(workspace, pane_id, &data, false).await?;
    }
    if !granted {
        return Err(FrameError::from(DaemonError::Protocol {
            detail: refusal_reason,
        }));
    }
    Ok(())
}

pub(super) async fn retire_live_control(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    let Some((terminal_id, attachment_id, _)) =
        workspace.retire_indeterminate_control(pane_id, Instant::now())
    else {
        return;
    };
    let _ = workspace
        .daemon()
        .notify(json!({
            "type": "terminal_detach",
            "request_id": uuid::Uuid::new_v4().to_string(),
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
        }))
        .await;
}

pub(super) async fn release_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let pane = workspace.pane(pane_id);
    if !pane.is_live() || !pane.is_held() {
        return Ok(());
    }
    let message = json!({
        "type": "terminal_release_control",
        "terminal_id": pane.terminal_id,
        "attachment_id": pane.attachment_id(),
    });
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    pane.control = ControlState::Observe;
    pane.take_back = false;
    workspace
        .daemon()
        .notify(message)
        .await
        .map_err(FrameError::from)
}

pub(super) async fn send_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    data: &[u8],
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    if workspace.pane(pane_id).writable() {
        return send_live_write(workspace, pane_id, data, false).await;
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if !pane.is_live() || pane.pending_input.is_some() {
        return Ok(());
    }
    pane.pending_input = Some(data.to_vec());
    take_live_control(workspace, pane_id).await
}

pub(super) async fn send_live_write(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) -> Result<(), FrameError> {
    let message = {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        if !pane.writable() {
            return Ok(());
        }
        pane.client_write_seq += 1;
        pane.in_flight_write = Some(pane.client_write_seq);
        let mut message = json!({
            "type": if paste { "terminal_paste" } else { "terminal_input" },
            "terminal_id": pane.terminal_id,
            "attachment_id": pane.attachment_id(),
            "client_write_seq": pane.client_write_seq,
        });
        message[if paste { "text" } else { "data" }] = json!(String::from_utf8_lossy(data));
        message
    };
    match workspace.daemon().send(message).await {
        Ok(reply) => {
            apply_live_write_outcome(workspace, &reply);
            Ok(())
        }
        Err(error) => {
            let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
            pane.in_flight_write = None;
            pane.control = ControlState::UncertainReadOnly;
            Err(FrameError::from(error))
        }
    }
}

pub(super) fn apply_live_write_outcome(workspace: &mut Workspace<LiveDaemon>, message: &Value) {
    let Some(attachment_id) = message.get("attachment_id").and_then(Value::as_str) else {
        return;
    };
    let outcome = message.get("outcome").and_then(Value::as_str).unwrap_or("");
    let reason = message.get("reason").and_then(Value::as_str).unwrap_or("");
    if let Some(pane) = workspace.pane_for_attachment_mut(attachment_id) {
        pane.in_flight_write = None;
        match outcome {
            "delivered" if pane.control != ControlState::LeaseLost => {
                pane.control = ControlState::Held;
            }
            "indeterminate" => pane.control = ControlState::UncertainReadOnly,
            "refused"
                if !matches!(
                    reason,
                    "write_seq_conflict" | "write_seq_expired" | "write_seq_capacity"
                ) =>
            {
                pane.control = ControlState::Observe;
            }
            _ => {}
        }
    }
}
