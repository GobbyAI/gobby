//! Pane control for the live loop: focus, take/release control, and the
//! daemon writes that carry pane input.

use gobby_terminal::protocol::ClientMessage;
use serde_json::{json, Value};
use tokio::time::Instant;

use crate::daemon::{Daemon, LiveDaemon};
use crate::frame_source::{FrameError, FrameSource};
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::{ControlOutcome, ControlState, PaneId, Workspace, HOST_GRANT_UNAVAILABLE};

/// Status shown when a key lands in a pane whose lease another viewer took.
pub const LEASE_LOST_INPUT: &str =
    "lease lost: take control (prefix+t) or take back (prefix+shift+a) to type";
/// Status shown when a key lands in a pane whose last write's outcome is
/// unknown.
pub const READ_ONLY_INPUT: &str =
    "read-only after an unconfirmed write: take control (prefix+t) to type";
/// Status shown when so much was typed while a grant was in flight that the
/// queue holding it filled. Only a daemon that stopped answering gets here.
pub const INPUT_QUEUE_FULL: &str = "too much typed while acquiring control; the rest was dropped";
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
        // Focus is the whole gesture. Clicking a pane is a person saying they
        // want to type in it, so it takes the input grant over rather than
        // asking and offering a second button when someone else holds it
        // (#22573). The request runs beside the loop, so the click itself
        // never waits on the daemon.
        workspace.request_control(pane_id, true);
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
pub(super) fn take_live_control(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    workspace.request_control(pane_id, true);
}

/// Applies the reply to a control request the loop started beside it. Every
/// key typed since the click is still queued, so a grant flushes them in the
/// order they were typed instead of crediting the first one and losing the
/// rest (#22573).
pub(super) async fn apply_control_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcome: ControlOutcome,
) {
    let (pane_id, request) = (outcome.pane_id, outcome.request);
    // Focus moved while this was in flight, so the pane no longer wants it.
    let current = workspace
        .panes
        .get(&pane_id)
        .and_then(|pane| pane.control_request);
    if current != Some(request) {
        return;
    }
    if let Some(pane) = workspace.panes.get_mut(&pane_id) {
        pane.control_request = None;
    }
    let reply = match outcome.reply {
        Ok(reply) => reply,
        Err(error) => {
            retire_live_control(workspace, pane_id).await;
            chrome.notify(Toast::error(error.to_string()));
            return;
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
    let host_input_granted = reply.get("host_input_granted").and_then(Value::as_bool);
    let refusal_reason = match reply.get("reason").and_then(Value::as_str) {
        Some("held") => HELD_BY_PEER.to_string(),
        Some(reason) => reason.to_string(),
        None => "control request denied".to_string(),
    };
    let pending = {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        if generation < pane.lease_generation() {
            return;
        }
        pane.set_lease_generation(generation);
        pane.control = if granted {
            ControlState::Held
        } else {
            ControlState::Observe
        };
        pane.take_back = !granted;
        if !granted {
            pane.clear_pending_input();
            chrome.notify(Toast::warning(refusal_reason));
            return;
        }
        if !pane.apply_host_grant(host_input_granted) {
            // The lease is ours and the host grant is not, so there is
            // nowhere to type. This is the terminal being gone for input
            // rather than an ordinary pane, and take-back is the only thing
            // that can recover it (#22573).
            chrome.notify(Toast::error(HOST_GRANT_UNAVAILABLE));
            return;
        }
        pane.take_pending_input()
    };
    for input in pending {
        let (data, paste) = input.parts();
        if let Err(error) = send_live_write(workspace, pane_id, data, paste).await {
            chrome.notify(Toast::error(error.to_string()));
            break;
        }
    }
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
    // A pane whose grant is still in flight holds a lease the daemon is about
    // to give it, so leaving without releasing would leak it. The release verb
    // is idempotent, which is what makes this safe to send either way.
    if !pane.is_live() || !(pane.is_held() || pane.control_request.is_some()) {
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
    // A grant still in flight for a pane we just left must not move it.
    pane.control_request = None;
    // The queue bridges the gap between asking for a grant and getting it. This
    // pane gave the grant up instead, so the gap closed: replaying those keys
    // whenever it next wins control would run a command long after it was
    // typed, which is worse than the words never landing (#22573).
    pane.clear_pending_input();
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
    paste: bool,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return Ok(());
    }
    if workspace.pane(pane_id).writable() {
        return send_live_write(workspace, pane_id, data, paste).await;
    }
    if !workspace.pane(pane_id).is_live() {
        return Ok(());
    }
    // A lost lease and an unknown write outcome both wait on a person, so
    // typing into them says what is wrong instead of queueing. Once that
    // person has asked for control, the decision is made and the keys they
    // type next belong in the queue like any other (#22573).
    let acquiring = workspace.awaiting_control(pane_id);
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    let refusal = match pane.control {
        ControlState::LeaseLost if !acquiring => Some(LEASE_LOST_INPUT),
        ControlState::UncertainReadOnly if !acquiring => Some(READ_ONLY_INPUT),
        _ => None,
    };
    if let Some(refusal) = refusal {
        chrome.notify(Toast::warning(refusal));
        return Ok(());
    }
    if !pane.queue_input(data, paste) {
        chrome.notify(Toast::warning(INPUT_QUEUE_FULL));
        return Ok(());
    }
    // Focus already asked for this grant; this covers the pane that gained
    // focus before the daemon was ready to be asked.
    workspace.request_control(pane_id, true);
    Ok(())
}

/// A forwarded mouse report. Unlike a keystroke it never asks for control: an
/// alt+clicked pane was deliberately left observing, and a pointer report must
/// not take its lease back behind the person who did that. It does queue behind
/// a grant already in flight, so the click that asked for that grant does not
/// lose the report it produced (#22573). A report that overruns the queue is
/// dropped without a toast, because pointer motion would make a storm of them.
pub(super) async fn send_live_report(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    data: &[u8],
) -> Result<(), FrameError> {
    if workspace.pane(pane_id).writable() {
        return send_live_write(workspace, pane_id, data, false).await;
    }
    if !workspace.awaiting_control(pane_id) {
        return Ok(());
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    pane.queue_input(data, false);
    Ok(())
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
        // A direct native pane types on its own frame socket: no write
        // sequence, no in-flight write, no daemon round trip per key (#22573).
        if pane.direct_input() {
            return pane.send_host_input(data, paste);
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
