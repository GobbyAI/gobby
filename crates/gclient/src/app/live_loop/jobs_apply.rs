//! The one gate every job outcome passes (plan A1): settle the ledger,
//! drop what an older connection sent, then land the result.

use std::time::Duration;

use crate::daemon::{DaemonError, Generation, LiveDaemon, TerminalRow, WorkspaceErrorCode};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::attention::apply_response;
use super::super::{PaneId, Workspace};
use super::control::{
    apply_outage_grant, apply_write_abandoned, apply_write_result, apply_write_unconfirmed,
};
use super::daemon_ops::{apply_kills, apply_ops};
use super::jobs::{JobOutcome, JobResult, LoopJobs, OpIntent};

pub(super) fn apply_job_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    outcome: JobOutcome,
) {
    let generation = jobs.sync_generation(workspace.daemon().generation());
    let current = outcome.tag.generation == generation;
    // Lost input is reported whichever connection it was typed on, and a
    // kill's pane bookkeeping is local.
    let result = match outcome.result {
        JobResult::WriteUnconfirmed { pane, attachment } => {
            let current = current && on_attachment(workspace, pane, &attachment);
            return apply_write_unconfirmed(workspace, chrome, pane, current);
        }
        JobResult::WriteAbandoned {
            pane,
            attachment,
            messages,
            bytes,
        } => {
            let current = current && on_attachment(workspace, pane, &attachment);
            return apply_write_abandoned(workspace, chrome, pane, messages, bytes, current);
        }
        JobResult::Killed {
            killed,
            kept,
            result,
        } => return apply_kills(workspace, chrome, killed, kept, result),
        // A host grant is real whichever generation asked for it, so it lands.
        result @ JobResult::HostGrant { .. } => result,
        JobResult::Opened {
            terminal_id,
            elapsed,
            ..
        } if !current => {
            // An earlier connection's read cannot install into this one; the
            // terminal's next workspace event opens it again.
            tracing::debug!(
                terminal_id = %terminal_id,
                generation = outcome.tag.generation.0,
                elapsed_ms = u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
                "dropped an unresolved terminal open from an earlier daemon generation"
            );
            return;
        }
        _ if !current => return,
        result => result,
    };
    let follow_up = jobs.ledger.settle(&outcome.tag, generation);
    match result {
        JobResult::WorkspaceOp {
            intent: OpIntent::FocusHints(focus),
            result,
        } => match result {
            // A hint naming a row the daemon already reaped is done.
            Ok(()) => jobs.focus.acked(focus),
            Err(DaemonError::Workspace(error)) if error.code == WorkspaceErrorCode::NotFound => {
                jobs.focus.acked(focus)
            }
            Err(DaemonError::Workspace(error)) => {
                // A coalesced successor already owns the offered memo.
                if follow_up.is_none() {
                    jobs.focus.refused();
                }
                chrome.notify(Toast::warning(error.reason));
            }
            Err(error) => {
                if follow_up.is_none() {
                    jobs.focus.refused();
                }
                chrome.notify(Toast::error(FrameError::from(error).to_string()));
            }
        },
        JobResult::Resized { result, .. } | JobResult::Scrolled { result } => {
            if let Err(error) = result {
                chrome.notify(Toast::error(FrameError::from(error).to_string()));
            }
        }
        JobResult::Write { bytes, reply, .. } => {
            apply_write_result(workspace, chrome, bytes, &reply)
        }
        JobResult::WriteUnconfirmed { .. }
        | JobResult::WriteAbandoned { .. }
        | JobResult::Killed { .. } => {}
        JobResult::HostGrant {
            pane,
            attachment,
            grant,
            revoked,
        } => apply_outage_grant(workspace, chrome, jobs, pane, &attachment, grant, revoked),
        JobResult::Responded { pending, result } => {
            apply_response(workspace, chrome, pending, result);
        }
        JobResult::Ops { unplaced, result } => apply_ops(workspace, chrome, unplaced, result),
        JobResult::Opened {
            terminal_id,
            elapsed,
            result,
        } => apply_open(workspace, generation, &terminal_id, elapsed, result),
        JobResult::Roster {
            project,
            elapsed,
            result,
        } => match result {
            Ok(relist) => {
                if workspace.apply_relist(relist) {
                    workspace.roster_landed = true;
                }
            }
            Err(error) => {
                tracing::warn!(
                    operation = "roster",
                    project = %project,
                    generation = generation.0,
                    error_class = %error.code(),
                    elapsed_ms = u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
                    %error,
                    "roster refresh failed; retrying with the focused relist"
                );
                workspace.request_relist();
            }
        },
        JobResult::Attention { elapsed, result } => match result {
            Ok(roster) => {
                // A snapshot older than an attention event already applied is
                // asked for again, rather than undoing that event.
                if !workspace.install_attention_guarded(roster) {
                    workspace.pending_refetch.attention = true;
                }
            }
            Err(error) => {
                tracing::warn!(
                    operation = "attention roster",
                    generation = generation.0,
                    error_class = %error.code(),
                    elapsed_ms = u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
                    %error,
                    "attention refresh failed"
                );
                // The sidebar's own refetch reports the failure and its banner.
                workspace.pending_sidebar.roster = true;
            }
        },
    }
    jobs.start(workspace, chrome, follow_up);
}

/// Installs the row a current read returned, but only while the terminal is
/// still unresolved: a read that outlived its pane or its focus installs
/// nothing. A failed read is a WARN carrying what it tried and how long it
/// ran; nothing retries it here, and the terminal's next workspace event
/// opens it again.
fn apply_open(
    workspace: &mut Workspace<LiveDaemon>,
    generation: Generation,
    terminal_id: &str,
    elapsed: Duration,
    result: Result<TerminalRow, DaemonError>,
) {
    if !workspace.is_unresolved_terminal(terminal_id) {
        tracing::debug!(
            terminal_id = %terminal_id,
            generation = generation.0,
            "dropped an unresolved terminal open whose terminal is no longer unresolved"
        );
        return;
    }
    match result {
        Ok(row) => {
            workspace.install_unresolved_rows(vec![row]);
            // The read cannot await the attach, so the render tick's
            // `start_due_attaches` picks the new pane up.
            if let Some(pane) = workspace.pane_for_terminal(terminal_id) {
                workspace.pane_mut(pane).attach_retry_at = Some(tokio::time::Instant::now());
            }
        }
        Err(error) => tracing::warn!(
            terminal_id = %terminal_id,
            generation = generation.0,
            error_class = %error.code(),
            elapsed_ms = u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX),
            "unresolved terminal open failed: {error}"
        ),
    }
}

/// Whether `pane` is still on the attachment a write was typed for. A
/// re-attach keeps the connection, so a late outcome for the attachment it
/// replaced must not undo the reset the new one got (R6 F2).
fn on_attachment(workspace: &Workspace<LiveDaemon>, pane: PaneId, attachment: &str) -> bool {
    workspace
        .panes
        .get(&pane)
        .is_some_and(|pane| pane.attachment_id() == attachment)
}
