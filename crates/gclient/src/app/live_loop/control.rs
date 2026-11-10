//! Pane input for the live loop: focus, and the writes that carry a pane's
//! keys. Every attached pane is writable. A native pane types straight onto
//! its host stream; a daemon-bound pane types through the daemon's writer.

use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;
use tokio::sync::watch;

use crate::app::apply::refused_write_toast;
use crate::daemon::LiveDaemon;
use crate::frame_source::{FrameError, FrameSource, PaneFrameSource, Transport};
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::pane::PendingInput;
use super::super::{AttachState, Pane, PaneId, Workspace};
use super::jobs::{JobKey, JobOutcome, LoopJobs, Pending};
use super::writer::PaneWriter;

/// Status shown when a key lands in a native pane on the daemon's proxy that
/// cannot promote back to its host stream, so there is no stream to hold the
/// key for. The pane reattaches there and this key is dropped (#25653).
pub const PROXY_NATIVE_INPUT: &str =
    "this terminal's input runs on its host stream; reattaching it, so the key was not sent";
/// Status shown when a key is typed into a native pane whose close is in flight.
/// The key stays off the daemon, and the close does not carry it (#25653).
pub const CLOSING_NATIVE_INPUT: &str = "this pane is closing; the key was not sent";
/// Status shown when so much was typed while a pane was reattaching that the
/// queue holding it filled.
pub const INPUT_QUEUE_FULL: &str =
    "too much typed while the terminal reattached; the rest was dropped";
/// Status shown when a key cannot reach its pane while the daemon is away.
pub(super) const DAEMON_DOWN_INPUT: &str = "daemon is down; input not sent";
/// Status shown when a scroll cannot reach a proxied pane while the daemon is away.
pub(super) const DAEMON_DOWN_SCROLL: &str = "daemon is down; scroll not sent";

/// Moves focus to `pane_id`. Focus never sends anything to the daemon or the host.
pub(super) fn focus_live_pane(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    if workspace.exit_reason().is_none() {
        workspace.focus.replace(pane_id);
    }
}

/// Sends queued keys to a pane that became live. Keys wait only while their
/// attachment is coming up; the loop calls this each turn.
pub(super) fn flush_attached_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &LoopJobs,
) {
    // A native pane on the proxy keeps its held keys until its direct promotion
    // lands; they then go out on the host stream.
    let ready: Vec<PaneId> = workspace
        .panes
        .iter()
        .filter(|(_, pane)| {
            pane.writable() && !pane.native_proxy() && !pane.pending_input.is_empty()
        })
        .map(|(id, _)| *id)
        .collect();
    for pane_id in ready {
        let pending = workspace
            .panes
            .get_mut(&pane_id)
            .map(Pane::take_pending_input)
            .unwrap_or_default();
        flush_pending_input(workspace, chrome, jobs, pane_id, pending);
    }
}

fn flush_pending_input(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &LoopJobs,
    pane_id: PaneId,
    pending: Vec<PendingInput>,
) {
    for input in pending {
        let (data, paste) = input.parts();
        if let Err(error) = send_live_write(workspace, jobs, pane_id, data, paste) {
            chrome.notify(Toast::error(error.to_string()));
        }
    }
}

/// Whether the pane's attachment is still coming up, so keys typed now belong
/// in its queue rather than on a stream that does not exist yet.
fn attaching(pane: &Pane) -> bool {
    pane.fallback_in_flight || matches!(pane.attach_state(), AttachState::Attaching { .. })
}

fn queue_while_attaching(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) {
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if !pane.queue_input(data, paste) {
        chrome.notify(Toast::warning(INPUT_QUEUE_FULL));
    }
}

/// A native pane on the daemon's proxy holds its keys only when a direct
/// promotion can land: a direct locator, direct frames allowed, and a daemon
/// to promote through. Otherwise its keys are refused as before.
fn held_for_promotion(workspace: &Workspace<LiveDaemon>, pane_id: PaneId) -> bool {
    workspace.pane(pane_id).direct_available
        && workspace.daemon_ready()
        && workspace.frame_delivery.allows(Transport::Direct)
}

/// Holds a key typed into a native pane on the proxy until its direct promotion
/// lands. The promotion is asked for now, not when the proxy interval runs out.
/// Only the first held key asks, so keys typed during a retry's backoff do not
/// restart it.
fn queue_for_promotion(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) {
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    let first = pane.pending_input.is_empty();
    if !pane.queue_input(data, paste) {
        chrome.notify(Toast::warning(INPUT_QUEUE_FULL));
        return;
    }
    if first {
        pane.promote_now();
    }
}

/// Sends a key typed into a pane. A native pane on its host stream types
/// straight there, with or without the daemon. A daemon-bound pane needs the
/// daemon, so it waits on the daemon's writer. A pane still attaching keeps the
/// key until its attachment is live.
// INVARIANT (#25653): native keystrokes and paste never wait for the daemon,
// a lease, or attachment metadata. A present host stream always wins.
// Native keystrokes and their terminal echo must NEVER depend on the daemon:
// not its WebSocket writer, response latency, database, leases, or readiness.
// Once a local native stream is attached, send bytes directly to that host.
// Poll daemon registration, focus, geometry, activity, and recovery beside the
// input path; none may become a prerequisite for a key or its echoed frame.
// A missing native stream must refuse explicitly, never fall back to daemon
// input. The daemon enhances the session; it does not carry native typing.
//
// Guard: loop_liveness::
// keystroke_latency_is_the_same_whether_the_daemon_is_healthy_slowed_or_absent
// holds or withholds daemon replies and requires bounded host writes with zero
// daemon input. loop_liveness::keystroke_latency_by_pane_count also checks the
// host echo under load. Keep those tests sensitive to this dependency: startup
// setup tolerance must never relax their keystroke or echo bounds.
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
    if workspace.pane(pane_id).closing_native() {
        chrome.notify(Toast::warning(CLOSING_NATIVE_INPUT));
        return Ok(());
    }
    if types_on_host(workspace.pane(pane_id)) {
        return send_live_write(workspace, jobs, pane_id, data, paste);
    }
    if workspace.pane(pane_id).native_proxy() {
        if held_for_promotion(workspace, pane_id) {
            queue_for_promotion(workspace, chrome, pane_id, data, paste);
        } else {
            chrome.notify(Toast::warning(PROXY_NATIVE_INPUT));
            workspace.pane_mut(pane_id).promote_now();
        }
        return Ok(());
    }
    let pane = workspace.pane(pane_id);
    if pane.accepts_input() && workspace.daemon_ready() {
        return send_live_write(workspace, jobs, pane_id, data, paste);
    }
    if !workspace.daemon_ready() {
        refuse_during_outage(workspace, pane_id);
        return Ok(());
    }
    if attaching(workspace.pane(pane_id)) {
        queue_while_attaching(workspace, chrome, pane_id, data, paste);
    }
    Ok(())
}

/// Input typed while the daemon is away and the pane has no live host stream
/// to type on. Nothing is sent, and the status says so (#24815).
fn refuse_during_outage(workspace: &mut Workspace<LiveDaemon>, pane_id: PaneId) {
    if let Some(pane) = workspace.panes.get_mut(&pane_id) {
        pane.status_message = Some(DAEMON_DOWN_INPUT.to_string());
    }
}

/// Closes the pane's writer queue behind its accepted writes, or a barrier
/// behind them and a direct pane's last frame write.
pub(super) fn close_barrier(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) -> Option<watch::Receiver<bool>> {
    let generation = workspace.daemon().generation();
    let pane = workspace.panes.get_mut(&pane_id)?;
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

/// Detaches the pane's attachment behind every write it accepted, so a pane
/// that leaves the view never stays attached. An external or adopted terminal
/// outlives its pane; this detach is what stops the stream and frees the
/// attachment. `None` when there is nothing to detach: the daemon is away, or
/// the pane is not live, and its attachment died with the socket.
pub(super) fn detach_barrier(
    workspace: &mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) -> Option<watch::Receiver<bool>> {
    if workspace.exit_reason().is_some() || !workspace.daemon_ready() {
        return None;
    }
    let pane = workspace.panes.get_mut(&pane_id)?;
    if !pane.is_live() {
        return None;
    }
    let flushed = pane
        .frame_source_mut()
        .and_then(PaneFrameSource::take_write_receipt);
    let attachment = pane.begin_closing_detach()?;
    let pane = pane_writer(workspace, outcomes, pane_id);
    let message = json!({
        "type": "terminal_detach",
        "request_id": uuid::Uuid::new_v4().to_string(),
        "terminal_id": pane.terminal_id,
        "attachment_id": attachment,
    });
    let writer = pane.writer.as_ref().expect("writer spawned");
    writer.enqueue_barrier(Some(message), None, flushed, attachment)
}

/// A forwarded mouse report. It queues behind a pane that is still attaching,
/// so the report of a click is not lost. A report that overruns the queue is
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
    if attaching(workspace.pane(pane_id)) {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        pane.queue_input(data, false);
    }
    Ok(())
}

/// The pane, with a writer serving the current connection.
fn pane_writer<'a>(
    workspace: &'a mut Workspace<LiveDaemon>,
    outcomes: &UnboundedSender<JobOutcome>,
    pane_id: PaneId,
) -> &'a mut Pane {
    let generation = workspace.daemon().generation();
    let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
    if !pane
        .writer
        .as_ref()
        .is_some_and(|writer| writer.serves(generation))
    {
        pane.writer = Some(PaneWriter::spawn(
            &workspace.daemon,
            outcomes,
            pane_id,
            pane.writer_ready.take(),
        ));
    }
    pane
}

// The host socket, not daemon readiness, a lease, or an attachment-state flag,
// determines native input routing (#25653). This predicate cannot access the
// daemon. Explicit local close/kill still stops further input.
fn types_on_host(pane: &Pane) -> bool {
    pane.backend.is_native()
        && !pane.closing_native()
        && !pane.is_terminating()
        && pane
            .frame_source()
            .is_some_and(|source| source.transport() == Transport::Direct)
}

// INVARIANT (#25653): a native host stream has no daemon writer fallback.
// Enqueue locally and return; only legacy tmux writes use the daemon worker.
/// Hand one write to the pane's path without waiting on the daemon.
pub(super) fn send_live_write(
    workspace: &mut Workspace<LiveDaemon>,
    jobs: &LoopJobs,
    pane_id: PaneId,
    data: &[u8],
    paste: bool,
) -> Result<(), FrameError> {
    if types_on_host(workspace.pane(pane_id)) {
        let pane = workspace.panes.get_mut(&pane_id).expect("pane exists");
        return pane.send_host_input(data, paste);
    }
    if workspace.pane(pane_id).backend.is_native() {
        return Err(FrameError::Protocol(
            "native host input stream unavailable".into(),
        ));
    }
    if !workspace.pane(pane_id).accepts_input() {
        return Ok(());
    }
    let generation = workspace.daemon().generation();
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

/// Land one write's outcome from the pane writer.
pub(super) fn apply_write_result(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    bytes: usize,
    reply: &Value,
) {
    if let Some(toast) = refused_write_toast(reply) {
        chrome.notify(toast.with_body(format!("{bytes} bytes")));
    }
    apply_live_write_outcome(workspace, reply);
}

/// A write whose send failed may or may not have reached the terminal. The
/// pane keeps typing; only the in-flight marker of that write is cleared.
pub(super) fn apply_write_unconfirmed(
    workspace: &mut Workspace<LiveDaemon>,
    pane_id: PaneId,
    current: bool,
) {
    if current {
        if let Some(pane) = workspace.panes.get_mut(&pane_id) {
            pane.in_flight_write = None;
        }
    }
}

/// Writes dropped unsent behind a connection change.
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
    let seq = message.get("client_write_seq").and_then(Value::as_u64);
    if let Some(pane) = workspace.pane_for_attachment_mut(attachment_id) {
        // Writes queued behind this one are still in flight.
        if seq.is_none_or(|seq| pane.in_flight_write == Some(seq)) {
            pane.in_flight_write = None;
        }
    }
}

#[cfg(test)]
#[path = "control/tests.rs"]
mod tests;
