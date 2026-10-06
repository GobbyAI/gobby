//! The one gate every job outcome passes (plan A1): settle the ledger,
//! drop what an older connection sent, then land the result.

use crate::daemon::{DaemonError, LiveDaemon, WorkspaceErrorCode};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::attention::apply_response;
use super::super::{PaneId, Workspace};
use super::control::{apply_write_abandoned, apply_write_result, apply_write_unconfirmed};
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
        JobResult::Responded { pending, result } => {
            apply_response(workspace, chrome, pending, result);
        }
        JobResult::Ops { unplaced, result } => apply_ops(workspace, chrome, unplaced, result),
    }
    jobs.start(workspace, chrome, follow_up);
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
