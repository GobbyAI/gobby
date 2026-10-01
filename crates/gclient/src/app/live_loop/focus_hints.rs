//! The focus hints a window leaves on its workspace, so the next window
//! opens where this one left off. Focus, zoom and the active tab stay this
//! window's; only the hint travels, as a coalesced job (plan A1).

use crate::daemon::{LiveDaemon, WorkspaceOp};
use crate::ui::Chrome;

use super::super::Workspace;
use super::jobs::{JobKey, LoopJobs, Pending};
use super::workspace_actions::{active_daemon_tab, daemon_pane_id};

/// The focus a window shows on a daemon tab: `(project, tab, pane)`.
pub type ShownFocus = (String, String, Option<String>);

/// The focus the daemon acknowledged and the focus last offered to it. A
/// hint goes out when the shown focus differs from the offered one, so a
/// hint in flight is never sent twice.
#[derive(Debug)]
pub(super) struct FocusMemo {
    acked: Option<ShownFocus>,
    offered: Option<ShownFocus>,
}

impl FocusMemo {
    /// Both start on the daemon's stored focus: the window opened on it, so
    /// nothing goes out unless it shows otherwise.
    pub(super) fn new(workspace: &Workspace<LiveDaemon>) -> Self {
        let stored = stored_focus(workspace);
        Self {
            acked: stored.clone(),
            offered: stored,
        }
    }

    pub(super) fn acked(&mut self, focus: ShownFocus) {
        self.acked = Some(focus);
    }

    /// The hint did not land: the next pass offers the shown focus again.
    pub(super) fn refused(&mut self) {
        self.offered = self.acked.clone();
    }
}

/// The focus this window shows when the active tab is the daemon's.
fn shown_focus(workspace: &Workspace<LiveDaemon>, chrome: &Chrome) -> Option<ShownFocus> {
    let project = workspace.project_id()?.to_owned();
    let tab = active_daemon_tab(chrome)?;
    let pane = daemon_pane_id(chrome, chrome.tab_focus(tab));
    Some((project, tab.id.clone(), pane))
}

/// The focus the daemon stores for the workspace, in the shape this window
/// sends, or `None` when the row names no project or no tab it still has.
fn stored_focus(workspace: &Workspace<LiveDaemon>) -> Option<ShownFocus> {
    let model = workspace.workspace_model()?;
    let project = model.workspace.focused_project_id.clone()?;
    let tab_id = model.workspace.focused_tab_id.clone()?;
    let pane = model.tab(&tab_id)?.focused_pane_id.clone();
    Some((project, tab_id, pane))
}

/// Offer the focus hints when the shown focus moved since the last offer.
pub(super) fn offer_focus_hints(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
) {
    if workspace.exit_reason().is_some()
        || !workspace.daemon_ready()
        || workspace.workspace_model().is_none()
    {
        return;
    }
    let Some(focus) = shown_focus(workspace, chrome) else {
        return;
    };
    if jobs.focus.offered.as_ref() == Some(&focus) {
        return;
    }
    jobs.focus.offered = Some(focus.clone());
    jobs.offer(
        workspace,
        chrome,
        JobKey::FocusHints,
        Pending::FocusHints(focus),
    );
}

/// The op that leaves `focus` as the workspace's hint.
pub(super) fn focus_hints_op(
    workspace: &Workspace<LiveDaemon>,
    focus: &ShownFocus,
) -> Option<WorkspaceOp> {
    let model = workspace.workspace_model()?;
    Some(WorkspaceOp::WorkspaceSetFocusHints {
        workspace: model.workspace.id.clone(),
        project_id: Some(focus.0.clone()),
        tab: Some(focus.1.clone()),
        pane: focus.2.clone(),
        node: None,
    })
}
