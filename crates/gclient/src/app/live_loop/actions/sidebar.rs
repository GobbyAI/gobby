//! Sidebar actions for the live loop: show and hide, the navigate cursor,
//! the card fold, the section filters, the session order and switching
//! projects by their place in the list.

use crate::daemon::LiveDaemon;
use crate::frame_source::FrameError;
use crate::ui::sidebar::next_machine_filter;
use crate::ui::sidebar_rows::{displayed_project_ids, project_rows, RowKind};
use crate::ui::{Action, Chrome, Mode};

use super::super::super::Workspace;
use super::super::modal_input::persist_prefs;
use super::super::projects::focus_project;

/// Apply one of the sidebar's actions; `handle_live_action` routes only
/// those here.
pub(super) fn apply_sidebar_action(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    action: Action,
) {
    match action {
        // Show or hide the pinned column (3.3 rebinds it to the overlay).
        Action::ToggleSidebar => chrome.sidebar.pinned = !chrome.sidebar.pinned,
        Action::ToggleGroup => {
            if let Some(project_id) = group_target(workspace, chrome) {
                chrome.sidebar.toggle_group(&project_id);
            }
        }
        Action::NavigateUp | Action::NavigateDown => {
            let rows_len = project_rows(workspace, chrome).len();
            if rows_len > 0 {
                let selected = chrome.sidebar.selected.min(rows_len - 1);
                chrome.sidebar.selected = if action == Action::NavigateUp {
                    selected.saturating_sub(1)
                } else {
                    (selected + 1).min(rows_len - 1)
                };
            }
            chrome.mode = Mode::Navigate;
        }
        Action::CycleMachineFilter => {
            chrome.sidebar.machine_filter = next_machine_filter(
                workspace.sidebar(),
                chrome.sidebar.machine_filter.as_deref(),
            );
        }
        Action::ToggleAgentSort => {
            chrome.prefs.agent_sort = chrome.prefs.agent_sort.toggled();
            persist_prefs(workspace.gobby_home(), chrome);
        }
        Action::ToggleProjectsFilter => {
            chrome.sidebar.all_projects = !chrome.sidebar.all_projects;
        }
        Action::ToggleSessionsScope => {
            chrome.sidebar.all_sessions = !chrome.sidebar.all_sessions;
        }
        _ => {}
    }
}

/// The project whose group `ToggleGroup` folds: the one under the navigate
/// cursor (a worktree row counts for its card), else the focused project.
fn group_target(workspace: &Workspace<LiveDaemon>, chrome: &Chrome) -> Option<String> {
    let rows = project_rows(workspace, chrome);
    let under_cursor = (chrome.mode == Mode::Navigate)
        .then(|| {
            rows[..rows.len().min(chrome.sidebar.selected + 1)]
                .iter()
                .rev()
                .find(|row| row.kind == RowKind::Project)
                .map(|row| row.id.clone())
        })
        .flatten();
    under_cursor.or_else(|| workspace.project_id().map(str::to_owned))
}

/// Focus the project an action names by its place in the sidebar's list:
/// `SwitchProject(n)` the nth, `PreviousProject` and `NextProject` the one
/// beside the focused project, wrapping.
pub(super) async fn switch_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    action: Action,
) -> Result<(), FrameError> {
    let ids = displayed_project_ids(workspace, chrome);
    let target = match action {
        Action::SwitchProject(index) => usize::from(index).checked_sub(1),
        _ if ids.is_empty() => None,
        _ => {
            let current = workspace
                .project_id()
                .and_then(|id| ids.iter().position(|candidate| candidate == id))
                .unwrap_or(0);
            let delta: isize = if action == Action::PreviousProject {
                -1
            } else {
                1
            };
            Some((current as isize + delta).rem_euclid(ids.len() as isize) as usize)
        }
    };
    match target.and_then(|index| ids.get(index)) {
        Some(project_id) => focus_project(workspace, chrome, project_id).await,
        None => Ok(()),
    }
}
