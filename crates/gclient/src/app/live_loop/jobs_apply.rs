//! The one gate every job outcome passes (plan A1): settle the ledger,
//! drop what an older connection sent, then land the result.

use crate::daemon::{DaemonError, LiveDaemon, WorkspaceErrorCode};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::Workspace;
use super::jobs::{JobOutcome, JobResult, LoopJobs, OpIntent};

pub(super) fn apply_job_outcome(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    outcome: JobOutcome,
) {
    let generation = jobs.sync_generation(workspace.daemon().generation());
    if outcome.tag.generation != generation {
        return;
    }
    let follow_up = jobs.ledger.settle(&outcome.tag, generation);
    match outcome.result {
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
                jobs.focus.refused();
                chrome.notify(Toast::warning(error.reason));
            }
            Err(error) => {
                jobs.focus.refused();
                chrome.notify(Toast::error(FrameError::from(error).to_string()));
            }
        },
        JobResult::Resized { result, .. } => {
            if let Err(error) = result {
                chrome.notify(Toast::error(FrameError::from(error).to_string()));
            }
        }
    }
    jobs.start(workspace, chrome, follow_up);
}
