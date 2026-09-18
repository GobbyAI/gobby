//! Pane control for the live loop: focus, take/release control, and the
//! daemon writes that carry pane input.

use gobby_terminal::protocol::ClientMessage;
use serde_json::{json, Value};
use tokio::time::Instant;

use crate::daemon::{Daemon, LiveDaemon};
use crate::frame_source::{FrameError, FrameSource};
use crate::ui::Chrome;

use super::super::{ControlState, PaneId, Workspace};

/// Status shown when a key lands in a pane whose lease another viewer took.
pub const LEASE_LOST_INPUT: &str =
    "lease lost: take control (prefix+t) or take back (prefix+shift+a) to type";
/// Status shown when a key lands in a pane whose last write's outcome is
/// unknown.
pub const READ_ONLY_INPUT: &str =
    "read-only after an unconfirmed write: take control (prefix+t) to type";
/// Status shown when a key lands while a take-control request is pending.
pub const ACQUIRING_CONTROL: &str = "acquiring control: keys typed before the grant are dropped";
pub const HELD_BY_PEER: &str =
    "another viewer holds control: take control again or take back (prefix+shift+a) to type";

pub(super) async fn focus_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    if !move_live_focus(workspace, pane_id).await? {
        return Ok(());
    }
    if !workspace.pane(pane_id).is_held() {
        request_live_control(workspace, pane_id, false).await?;
    }
    Ok(())
}

/// Focus `pane_id` without taking control (alt+click): the lease still
/// follows focus away from the previous pane, and the new pane stays observed
/// until a key or the indicator takes it.
pub(super) async fn observe_live_pane(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    move_live_focus(workspace, pane_id).await.map(drop)
}

/// Move focus and release the previous pane's lease. `false` when the loop
/// is exiting or the daemon is not ready, in which case nothing moved.
async fn move_live_focus(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<bool, FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(false);
    }
    let previous = workspace.focus.replace(pane_id);
    if let Some(previous) = previous.filter(|previous| *previous != pane_id) {
        release_live_control(workspace, previous).await?;
    }
    Ok(true)
}

/// An explicit take: the indicator, the context menu, `prefix+t`,
/// `prefix+shift+a`, and a key typed into an observed pane all land here.
///
/// A pane already showing take-back has had a polite request refused or lost
/// its lease to another attachment, so an explicit take on it is a takeover.
/// The daemon refuses every non-takeover request while another attachment
/// holds the lease, so without this the documented take-back path could
/// never succeed and a held pane had no way out. Focus stays polite: see
/// `focus_live_pane`.
pub(super) async fn take_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
) -> Result<(), FrameError> {
    let takeover = workspace
        .panes
        .get(&pane_id)
        .is_some_and(|pane| pane.has_take_back());
    request_live_control(workspace, pane_id, takeover).await
}

async fn request_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    takeover: bool,
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
            "takeover": takeover,
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
    let refusal_reason = match reply.get("reason").and_then(Value::as_str) {
        Some("held") => HELD_BY_PEER.to_string(),
        Some(reason) => reason.to_string(),
        None => "control request denied".to_string(),
    };
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
        return Err(FrameError::Refused(refusal_reason));
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

/// Keys for a held pane go out at once; an observed pane takes control
/// first and delivers them once the lease is granted. A pane whose lease
/// was lost, or whose last write's outcome is unknown, refuses them until
/// control is taken explicitly (the scripted path's `read_only` refusal),
/// and keys typed while a take is already pending are dropped; the status
/// line says so in both cases.
pub(super) async fn send_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
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
    if !pane.is_live() {
        return Ok(());
    }
    let refusal = match pane.control {
        ControlState::LeaseLost => Some(LEASE_LOST_INPUT),
        ControlState::UncertainReadOnly => Some(READ_ONLY_INPUT),
        _ if pane.pending_input.is_some() => Some(ACQUIRING_CONTROL),
        _ => None,
    };
    if let Some(refusal) = refusal {
        chrome.status_message = Some(refusal.to_string());
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

/// Scroll `pane_id`'s viewport to `rows` above the live edge on whichever
/// transport carries its frames: the socket encodes `SetScrollOffset`, the
/// proxy sends `terminal_set_scroll_offset`. The offset is mirrored at once
/// so the chrome draws the new position before `ScrollOffsetApplied`
/// confirms it, as the scripted `Workspace::set_scroll_offset` does. A pane
/// without a frame source has nothing to scroll.
pub(super) async fn set_live_scroll_offset(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    rows: u32,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    let Some(source) = pane.frame_source_mut() else {
        return Ok(());
    };
    source
        .send(&ClientMessage::SetScrollOffset {
            rows_from_live_edge: rows,
        })
        .await?;
    pane.scroll_offset = rows;
    if rows == 0 {
        pane.new_output = false;
    }
    Ok(())
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
