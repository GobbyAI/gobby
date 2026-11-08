//! Pane control for the live loop: focus, take/release control, and the
//! daemon writes that carry pane input.

use std::io;

use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;
use tokio::sync::watch;
use tokio::time::Instant;

use crate::daemon::LiveDaemon;
use crate::frame_source::{FrameError, PaneFrameSource};
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::pane::PendingInput;
use super::super::{
    AttachState, ControlOutcome, ControlState, Pane, PaneId, Workspace, HOST_GRANT_UNAVAILABLE,
};
use super::jobs::{JobKey, JobOutcome, LoopJobs, Pending};
use super::outage_grant::{GrantOutcome, CONTROL_UNREACHABLE};
use super::writer::PaneWriter;

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
/// Status shown when a key lands in a native pane on the daemon's proxy. Its
/// keys belong on its host stream, so the pane reattaches there and this key
/// is dropped (#25653).
pub const PROXY_NATIVE_INPUT: &str =
    "this terminal's input runs on its host stream; reattaching it, so the key was not sent";
/// The gterm refusal for a grant request that did not displace the pane's peer
/// (`takeover: false`).
const PEER_HELD_GRANT: &str = "input_held_by_peer";
/// Status shown when input reaches a pane whose release is still on its way.
pub const FOCUS_MOVED_INPUT: &str = "Focus moved; input not sent.";
/// Reported when typed input left gclient but its delivery is unknown.
pub(super) const UNCONFIRMED_INPUT: &str = "Input delivery unconfirmed.";
/// Status shown when a key cannot reach its pane while the daemon is away.
pub(super) const DAEMON_DOWN_INPUT: &str = "daemon is down; input not sent";
/// Status shown when a scroll cannot reach a proxied pane while the daemon is away.
pub(super) const DAEMON_DOWN_SCROLL: &str = "daemon is down; scroll not sent";

pub(super) fn focus_live_pane(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    if !move_live_focus(workspace, pane_id) {
        return;
    }
    if !workspace.pane(pane_id).is_held() && !workspace.pane(pane_id).take_back {
        // Focus may claim a free pane, but must never interrupt a peer holder.
        workspace.request_control(pane_id, false);
    }
}

/// Focus `pane_id` without taking control (alt+click): the lease still
/// follows focus away from the previous pane, and the new pane stays observed
/// until a key or the indicator takes it.
pub(super) fn observe_live_pane(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    move_live_focus(workspace, pane_id);
}

/// Move focus and release the previous pane's lease. `false` only when the
/// loop is exiting, in which case nothing moved.
fn move_live_focus(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) -> bool {
    if workspace.exit_reason().is_some() {
        return false;
    }
    let previous = workspace.focus.replace(pane_id);
    if let Some(previous) = previous.filter(|previous| *previous != pane_id) {
        release_live_control(workspace, previous);
    }
    true
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

// Writes queued keys in order. A key whose write fails reports its own error,
// and the keys after it are still written, so no failure drops keys silently.
fn flush_pending_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &LoopJobs,
    pane_id: PaneId,
    pending: Vec<PendingInput>,
) {
    // Keys queued before this pane resolved to the daemon's proxy are not
    // relayed through the daemon any more (B2): the pane reattaches to its host,
    // and the keys are dropped visibly.
    if !pending.is_empty() && workspace.pane(pane_id).native_proxy() {
        chrome.notify(Toast::warning(PROXY_NATIVE_INPUT));
        workspace.pane_mut(pane_id).promote_now();
        return;
    }
    for input in pending {
        let (data, paste) = input.parts();
        if let Err(error) = send_live_write(workspace, jobs, pane_id, data, paste) {
            chrome.notify(Toast::error(error.to_string()));
        }
    }
}

/// Applies the reply to a control request the loop started beside it. Every
/// key typed since the click is still queued, so a grant flushes them in the
/// order they were typed instead of crediting the first one and losing the
/// rest (#22573).
pub(super) fn apply_control_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &LoopJobs,
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
            retire_live_control(workspace, jobs.outcomes(), pane_id);
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
        // A grant follows this pane's own release, which the take waited on.
        if granted {
            pane.release_done = None;
        }
        // A host grant this pane already holds is the host's decision. The
        // daemon's answer to the take does not revoke it, and the keys queued
        // for it go to the host now (ruling b59e4ce9).
        let host_granted = pane.direct_input();
        if !granted {
            chrome.notify(Toast::warning(refusal_reason));
            if !host_granted {
                pane.clear_pending_input();
                return;
            }
        }
        if granted && !host_granted && !pane.apply_host_grant(host_input_granted) {
            // The lease is ours and the host grant is not, so there is
            // nowhere to type. This is the terminal being gone for input
            // rather than an ordinary pane, and take-back is the only thing
            // that can recover it (#22573).
            chrome.notify(Toast::error(HOST_GRANT_UNAVAILABLE));
            return;
        }
        pane.take_pending_input()
    };
    flush_pending_input(workspace, chrome, jobs, pane_id, pending);
}

pub(super) fn retire_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) {
    let Some((terminal_id, attachment_id, _)) =
        workspace.retire_indeterminate_control(pane_id, Instant::now())
    else {
        return;
    };
    // The detach goes out behind whatever the pane already queued.
    let message = json!({
        "type": "terminal_detach",
        "request_id": uuid::Uuid::new_v4().to_string(),
        "terminal_id": terminal_id,
        "attachment_id": attachment_id,
    });
    let pane = pane_writer(workspace, outcomes, pane_id);
    let writer = pane.writer.as_ref().expect("writer spawned");
    // Only a writer whose loop already exited refuses it, and that loop is
    // the one retiring.
    let _ = writer.enqueue_barrier(Some(message), None, None, attachment_id);
}

/// Give up `pane_id`'s lease. This only marks the release; the loop sends it
/// through the pane's writer, behind every write the pane already accepted,
/// and every later take waits for it (plan A2).
pub(super) fn release_live_control(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    if workspace.exit_reason().is_some() {
        return;
    }
    let daemon_ready = workspace.daemon_ready();
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    // A pane whose grant is still in flight holds a lease the daemon is about
    // to give it, so leaving without releasing would leak it. The release verb
    // is idempotent, which is what makes this safe to send either way.
    if !pane.is_live() || !(pane.is_held() || pane.control_request.is_some()) {
        return;
    }
    // With the daemon away there is no release to send. The pane lets go here,
    // and the host keeps its grant until the next grant replaces it (#24815).
    if daemon_ready {
        pane.release_pending = true;
    }
    pane.control = ControlState::Observe;
    pane.take_back = false;
    // A grant still in flight for a pane we just left must not move it.
    pane.control_request = None;
    // The queue bridges the gap between asking for a grant and getting it. This
    // pane gave the grant up instead, so the gap closed: replaying those keys
    // whenever it next wins control would run a command long after it was
    // typed, which is worse than the words never landing (#22573).
    pane.clear_pending_input();
}

/// Send the releases focus moves marked, each behind its pane's accepted
/// writes and its own take, then start the recorded control request, which
/// waits for every one of them before it writes the take. A release that
/// could not be queued is never read as sent, so no take follows it.
pub(super) fn start_live_control(
    workspace: &mut Workspace<LiveDaemon>,
    jobs: &LoopJobs,
    control: &UnboundedSender<ControlOutcome>,
) {
    let releasing: Vec<PaneId> = workspace
        .panes
        .iter()
        .filter(|(_, pane)| pane.release_pending)
        .map(|(pane_id, _)| *pane_id)
        .collect();
    let mut queued = true;
    for pane_id in releasing {
        queued &= enqueue_release(workspace, jobs.outcomes(), pane_id).is_some();
    }
    if queued {
        workspace.start_control_request(control);
    }
}

/// Queue a pane's pending release behind its accepted writes and its take.
/// Every release gets its own barrier, since a take may have gone out after
/// the last one. `None` leaves the release pending.
fn enqueue_release(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) -> Option<watch::Receiver<bool>> {
    let pane = pane_writer(workspace, outcomes, pane_id);
    pane.release_pending = false;
    let message = json!({
        "type": "terminal_release_control",
        "terminal_id": pane.terminal_id,
        "attachment_id": pane.attachment_id(),
    });
    let after = pane.take_done.take();
    // A direct pane typed on its frame socket: the release follows the local
    // flush of those bytes, which is all gclient can order (A2.11).
    let flushed = pane
        .frame_source_mut()
        .and_then(PaneFrameSource::take_write_receipt);
    let attachment = pane.attachment_id().to_owned();
    let writer = pane.writer.as_ref().expect("writer spawned");
    let done = writer.enqueue_barrier(Some(message), after, flushed, attachment);
    pane.release_pending = done.is_none();
    pane.release_done.clone_from(&done);
    done
}

/// What a close of this pane waits on: its pending release, which follows
/// every write accepted before it, or else a barrier behind those writes and
/// a direct pane's last frame write. A release already queued is reused,
/// since nothing can be typed on a pane that let go.
pub(super) fn close_barrier(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) -> Option<watch::Receiver<bool>> {
    let generation = workspace.daemon().generation();
    let pane = workspace.panes.get_mut(&pane_id)?;
    if pane.release_pending {
        return enqueue_release(workspace, outcomes, pane_id);
    }
    if let Some(release) = pane.pending_release() {
        return Some(release);
    }
    let flushed = pane
        .frame_source_mut()
        .and_then(PaneFrameSource::take_write_receipt);
    let serving = pane
        .writer
        .as_ref()
        .is_some_and(|writer| writer.serves(generation));
    if flushed.is_none() && !serving {
        return None;
    }
    let pane = pane_writer(workspace, outcomes, pane_id);
    let attachment = pane.attachment_id().to_owned();
    let writer = pane.writer.as_ref().expect("writer spawned");
    writer.enqueue_barrier(None, None, flushed, attachment)
}

/// Keys for a held pane go out at once; an observed pane takes control
/// first and delivers them once the lease is granted. A pane whose lease
/// was lost, or whose last write's outcome is unknown, refuses them until
/// control is taken explicitly (the scripted path's `read_only` refusal).
/// Keys typed while a take is pending are queued and flushed once control
/// is granted.
pub(super) fn send_live_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) -> Result<(), FrameError> {
    if workspace.exit_reason().is_some() {
        return Ok(());
    }
    // A key the host granted types onto the host's stream without asking the
    // daemon, which may be away or slow (ruling b59e4ce9).
    if types_on_host(workspace, pane_id) {
        return send_live_write(workspace, jobs, pane_id, data, paste);
    }
    // An ungranted native pane asks the host for its grant on the stream it
    // holds. The daemon's take runs beside that request, not in front of it.
    let pane = workspace.pane(pane_id);
    if pane.direct_native() && pane.is_live() {
        request_host_grant(workspace, chrome, jobs, pane_id, data, paste);
        return Ok(());
    }
    if !workspace.daemon_ready() {
        refuse_during_outage(workspace, pane_id);
        return Ok(());
    }
    // A lost lease and an unknown write outcome say what is wrong before the
    // key goes anywhere else (#22573).
    let acquiring = workspace.awaiting_control(pane_id);
    if let Some(refusal) = control_refusal(&workspace.pane(pane_id).control, acquiring) {
        chrome.notify(Toast::warning(refusal));
        return Ok(());
    }
    // A native pane on the daemon's proxy has no host stream of its own here.
    // The daemon no longer relays its keys: the pane reattaches to its host,
    // and this key is dropped visibly.
    if workspace.pane(pane_id).native_proxy() {
        chrome.notify(Toast::warning(PROXY_NATIVE_INPUT));
        workspace.pane_mut(pane_id).promote_now();
        return Ok(());
    }
    let pane = workspace.pane(pane_id);
    if pane.writable() && (!pane.direct_native() || pane.direct_input()) {
        return send_live_write(workspace, jobs, pane_id, data, paste);
    }
    let pane = workspace.pane(pane_id);
    // A key typed while the pane re-attaches, or while a recovery detaches it
    // to attach again, is its ask for control, as it would be a moment later
    // on the live attachment; the take waits until the new attachment is
    // live, and a failed attach clears the queue.
    if !pane.is_live()
        && !pane.fallback_in_flight
        && !matches!(pane.attach_state(), AttachState::Attaching { .. })
    {
        return Ok(());
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if !pane.queue_input(data, paste) {
        chrome.notify(Toast::warning(INPUT_QUEUE_FULL));
        return Ok(());
    }
    // Focus already asked for this grant; this covers the pane that gained
    // focus before the daemon was ready to be asked.
    workspace.request_control(pane_id, true);
    Ok(())
}

/// Whether a key goes straight onto the pane's host stream. The host's grant
/// decides. The daemon's lease only refuses when it reports the lease lost or
/// unconfirmed, since those wait on a person (#22573). A host-local recovery
/// keeps typing whatever the lease says (#23076).
fn types_on_host(workspace: &Workspace<LiveDaemon>, pane_id: PaneId) -> bool {
    let pane = workspace.pane(pane_id);
    pane.direct_input()
        && (workspace.host_recovered.contains(&pane_id)
            || (pane.is_live()
                && !pane.is_terminating()
                && !matches!(
                    pane.control,
                    ControlState::LeaseLost | ControlState::UncertainReadOnly
                )))
}

/// The refusal a key gets while the daemon's lease is lost or its write outcome
/// unknown. Both wait on a person, so typing into them says what is wrong
/// instead of queueing. Once that person has asked for control, the decision is
/// made and the keys they type belong in the queue like any other (#22573).
fn control_refusal(control: &ControlState, acquiring: bool) -> Option<&'static str> {
    match control {
        ControlState::LeaseLost if !acquiring => Some(LEASE_LOST_INPUT),
        ControlState::UncertainReadOnly if !acquiring => Some(READ_ONLY_INPUT),
        _ => None,
    }
}

/// Asks the host for the grant an ungranted native pane types on, and queues
/// the key until the host answers. Nothing here waits on the daemon. A host
/// that refuses because a peer holds the grant drops the key visibly
/// (`apply_outage_grant`), and only this keypress asks without displacing one
/// (#25653).
fn request_host_grant(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) {
    let acquiring = workspace.awaiting_control(pane_id);
    // A lost lease and an unknown write outcome wait on a person, as they do
    // with the daemon (#22573).
    if let Some(refusal) = control_refusal(&workspace.pane(pane_id).control, acquiring) {
        chrome.notify(Toast::warning(refusal));
        return;
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if !pane.queue_input(data, paste) {
        chrome.notify(Toast::warning(INPUT_QUEUE_FULL));
        return;
    }
    // The daemon's take runs beside the host's grant, not in front of it. A
    // keypress never displaces a peer, so the take does not takeover (#25653).
    if workspace.daemon_ready() {
        workspace.request_control(pane_id, false);
    }
    jobs.offer(
        workspace,
        chrome,
        JobKey::HostGrant(pane_id),
        Pending::HostGrant(pane_id),
    );
}

/// Input typed while the daemon is away and the pane has no live host stream
/// to type on. Nothing is sent, and the status says so (#24815).
fn refuse_during_outage(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    if let Some(pane) = workspace.panes.get_mut(&pane_id) {
        pane.status_message = Some(DAEMON_DOWN_INPUT.to_string());
    }
}

/// Clears each pane whose revoke landed. A failed revoke leaves its pane as it
/// was, and the next grant retries it.
pub(super) fn drop_revoked_grants(
    workspace: &mut Workspace<LiveDaemon>,
    revoked: Vec<(PaneId, String, io::Result<()>)>,
) {
    for (other, other_attachment, result) in revoked {
        if result.is_err() {
            continue;
        }
        if let Some(pane) = workspace.panes.get_mut(&other) {
            if pane.attachment_id() == other_attachment {
                pane.drop_host_grant();
            }
        }
    }
}

/// Applies an outage grant's answer. A granted pane types its queued keys. A
/// refused or unreachable grant says why, and sends nothing.
pub(super) fn apply_outage_grant(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &LoopJobs,
    pane_id: PaneId,
    attachment: &str,
    lease: u64,
    grant: io::Result<GrantOutcome>,
) {
    let Some(pane) = workspace.panes.get_mut(&pane_id) else {
        return;
    };
    // The pane has another attachment now, so this grant names nothing it holds.
    if pane.attachment_id() != attachment {
        return;
    }
    // A grant asked under an older lease than the pane holds now was answered
    // after the daemon's take granted the pane. That take is the newer answer,
    // so a refusal to the older grant does not take the lease back (#25790).
    let superseded = pane.lease_generation() > lease;
    match grant {
        Ok(GrantOutcome::Granted) => {
            pane.restore_host_control();
            let pending = pane.take_pending_input();
            flush_pending_input(workspace, chrome, jobs, pane_id, pending);
        }
        // A peer holds the host's grant. The key is dropped visibly, and the
        // pane keeps its daemon control: nothing here displaces the peer
        // (#25653).
        Ok(GrantOutcome::Refused(code)) if code == PEER_HELD_GRANT => {
            pane.clear_pending_input();
            chrome.notify(Toast::warning(HELD_BY_PEER));
        }
        // A live pane never draws its status, so each dropped key is reported
        // by a toast as well.
        Ok(GrantOutcome::Refused(_)) if superseded => {}
        Ok(GrantOutcome::Refused(_)) => {
            if !pane.apply_host_grant(Some(false)) {
                chrome.notify(Toast::error(HOST_GRANT_UNAVAILABLE));
            }
        }
        Err(_) if superseded => {}
        Err(_) => {
            pane.apply_host_grant(Some(false));
            pane.status_message = Some(CONTROL_UNREACHABLE.to_string());
            chrome.notify(Toast::error(CONTROL_UNREACHABLE));
        }
    }
}

/// A forwarded mouse report. Unlike a keystroke it never asks for control: an
/// alt+clicked pane was deliberately left observing, and a pointer report must
/// not take its lease back behind the person who did that. It does queue behind
/// a grant already in flight, so the click that asked for that grant does not
/// lose the report it produced (#22573). A report that overruns the queue is
/// dropped without a toast, because pointer motion would make a storm of them.
pub(super) fn send_live_report(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &LoopJobs,
    pane_id: PaneId,
    data: &[u8],
) -> Result<(), FrameError> {
    // A wheel's arrow keys and pointer reports are input too. A native pane on
    // the daemon's proxy refuses them on the stream like a key (ruling
    // b59e4ce9), so they never reach the daemon one report at a time.
    if workspace.pane(pane_id).native_proxy() {
        chrome.notify(Toast::warning(PROXY_NATIVE_INPUT));
        workspace.pane_mut(pane_id).promote_now();
        return Ok(());
    }
    if workspace.pane(pane_id).writable() {
        return send_live_write(workspace, jobs, pane_id, data, false);
    }
    if !workspace.awaiting_control(pane_id) {
        return Ok(());
    }
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    pane.queue_input(data, false);
    Ok(())
}

/// The pane, with a writer serving the current connection.
fn pane_writer<'a>(
    workspace: &'a mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) -> &'a mut Pane {
    let generation = workspace.daemon().generation();
    let writer = (!workspace
        .pane(pane_id)
        .writer
        .as_ref()
        .is_some_and(|writer| writer.serves(generation)))
    .then(|| PaneWriter::spawn(workspace.daemon(), outcomes, pane_id));
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if writer.is_some() {
        pane.writer = writer;
    }
    pane
}

/// Hand one write to the pane's writer, which sends it after the writes
/// typed before it. Nothing here waits on the daemon.
pub(super) fn send_live_write(
    workspace: &mut Workspace<LiveDaemon>,
    jobs: &LoopJobs,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) -> Result<(), FrameError> {
    let generation = workspace.daemon().generation();
    // A direct native pane types on its own frame socket: no write
    // sequence, no in-flight write, no daemon round trip per key (#22573).
    // A pane restored by a host-local reconnect keeps typing even after a
    // later daemon outage cleared its lease: the carried grant lives at
    // the host, which enforces it, and the daemon was never part of that
    // reconnect (#23076).
    let direct = types_on_host(workspace, pane_id);
    let pane = workspace.pane(pane_id);
    if !direct && !pane.writable() {
        if pane.release_pending || pane.pending_release().is_some() {
            let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
            pane.status_message = Some(FOCUS_MOVED_INPUT.to_string());
        }
        return Ok(());
    }
    if direct {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        return pane.send_host_input(data, paste);
    }
    let pane = pane_writer(workspace, jobs.outcomes(), pane_id);
    let client_write_seq = pane.client_write_seq + 1;
    let mut body = json!({
        "type": if paste { "terminal_paste" } else { "terminal_input" },
        "terminal_id": pane.terminal_id,
        "attachment_id": pane.attachment_id(),
        "client_write_seq": client_write_seq,
    });
    body[if paste { "text" } else { "data" }] = json!(String::from_utf8_lossy(data));
    let backlog = FrameError::Backpressure.to_string();
    let writer = pane.writer.as_ref().expect("writer spawned");
    match writer.enqueue(generation, client_write_seq, body, data.len()) {
        Ok(true) => {
            pane.client_write_seq = client_write_seq;
            pane.in_flight_write = Some(client_write_seq);
            if pane.status_message.as_deref() == Some(backlog.as_str()) {
                pane.status_message = None;
            }
        }
        Ok(false) => {}
        Err(error) => pane.status_message = Some(error.to_string()),
    }
    Ok(())
}

/// Land one write's outcome from the pane writer. A write whose reply never
/// came leaves the pane read-only until control is taken again.
pub(super) fn apply_write_result(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    bytes: usize,
    reply: &Value,
) {
    if reply.get("outcome").and_then(Value::as_str) == Some("refused") {
        let reason = reply
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        chrome.notify(
            Toast::warning(format!("Input refused: {reason}.")).with_body(format!("{bytes} bytes")),
        );
    }
    apply_live_write_outcome(workspace, reply);
}

/// A write whose send failed may or may not have reached the terminal, so
/// the pane stops typing until control is taken again. Only the connection
/// it was typed on still holds that pane state, and a pane that already let
/// go keeps observing: its next take is the one a person asks for anyway.
pub(super) fn apply_write_unconfirmed(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    pane_id: PaneId,
    current: bool,
) {
    if current {
        if let Some(pane) = workspace.panes.get_mut(&pane_id) {
            pane.in_flight_write = None;
            if !pane.let_go() {
                pane.control = ControlState::UncertainReadOnly;
            }
        }
    }
    chrome.notify(Toast::warning(UNCONFIRMED_INPUT));
}

/// Writes dropped unsent behind an unconfirmed write or a connection change.
pub(super) fn apply_write_abandoned(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    pane_id: PaneId,
    messages: usize,
    bytes: usize,
    current: bool,
) {
    if current {
        if let Some(pane) = workspace.panes.get_mut(&pane_id) {
            pane.in_flight_write = None;
        }
    }
    chrome.notify(
        Toast::warning(format!("Input not sent: {bytes} bytes."))
            .with_body(format!("{messages} writes")),
    );
}

/// Scroll `pane_id`'s viewport to `rows` above the live edge on whichever
/// transport carries its frames: the socket encodes `SetScrollOffset`, the
/// proxy sends `terminal_set_scroll_offset`. The offset is mirrored at once
/// so the chrome draws the new position before `ScrollOffsetApplied`
/// confirms it, as the scripted `Workspace::set_scroll_offset` does. A pane
/// without a frame source has nothing to scroll.
pub(super) fn set_live_scroll_offset(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    pane_id: PaneId,
    rows: u32,
) {
    if workspace.exit_reason().is_some() {
        return;
    }
    // A host socket takes a scroll without the daemon. Any other pane needs the
    // daemon, so while it is away the pane says so (#24815).
    let daemon_ready = workspace.daemon_ready();
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    let on_host = matches!(
        pane.frame_source(),
        Some(source) if !matches!(source, PaneFrameSource::Proxy(_))
    );
    if !on_host && !daemon_ready {
        pane.status_message = Some(DAEMON_DOWN_SCROLL.to_string());
        return;
    }
    if pane.frame_source().is_none() {
        return;
    }
    if pane.scroll_offset != rows {
        pane.scrolled_at = Some(std::time::Instant::now());
    }
    pane.scroll_offset = rows;
    if rows == 0 {
        pane.new_output = false;
    }
    jobs.offer(
        workspace,
        chrome,
        JobKey::Scroll(pane_id),
        Pending::Scroll(pane_id, rows),
    );
}

pub(super) fn apply_live_write_outcome(workspace: &mut Workspace<LiveDaemon>, message: &Value) {
    let Some(attachment_id) = message.get("attachment_id").and_then(Value::as_str) else {
        return;
    };
    let outcome = message.get("outcome").and_then(Value::as_str).unwrap_or("");
    let reason = message.get("reason").and_then(Value::as_str).unwrap_or("");
    let seq = message.get("client_write_seq").and_then(Value::as_u64);
    if let Some(pane) = workspace.pane_for_attachment_mut(attachment_id) {
        // Writes queued behind this one are still in flight.
        if seq.is_none_or(|seq| pane.in_flight_write == Some(seq)) {
            pane.in_flight_write = None;
        }
        match outcome {
            // A write queued before a release or observe may be confirmed
            // after it; that confirmation never hands the lease back.
            "delivered"
                if !matches!(
                    pane.control,
                    ControlState::LeaseLost | ControlState::Observe
                ) =>
            {
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

#[cfg(test)]
#[path = "control/tests.rs"]
mod tests;
