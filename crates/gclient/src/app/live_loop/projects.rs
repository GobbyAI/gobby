//! Project focus for the live loop: the outgoing project's tab set is saved
//! to its snapshot, the incoming one is restored from its own or seeded with
//! a shell (decision 7), and the client session follows the sidebar.

use std::path::PathBuf;

use crossterm::event::{KeyCode, KeyEvent};

use crate::daemon::{Daemon, DaemonError, LiveDaemon};
use crate::frame_source::FrameError;
use crate::persist::{save_session, save_snapshot, ClientSession, WorkspaceSnapshot};
use crate::ui::chrome::{attention_pane, Tab};
use crate::ui::dialogs::project::{complete_directory, expand_home, plural};
use crate::ui::dialogs::{CloseScope, CloseTarget, Dialog, RenameKind, WorktreeChoice};
use crate::ui::sidebar_rows::project_label;
use crate::ui::{Chrome, Mode};

use super::super::persistence::sidebar_snapshot;
use super::super::project_tabs::TabSet;
use super::super::sidebar_model::{ProjectEntry, WorktreeEntry};
use super::super::{PaneId, Workspace};
use super::actions::{
    activate_live_tab, open_live_rename, spawn_live_shell, spawn_live_terminal, sync_live_chrome,
    terminate_live_terminal,
};
use super::control::focus_live_pane;
use super::menu::attention_id;
use super::modal_input::{close_modal, edit_text, ModalOutcome};
use super::mouse::Placement;

/// Make `project_id` the focused project: its roster replaces the current
/// one and its tab set the tab bar. The outgoing set is saved first, so the
/// panes the roster drops are already in that project's snapshot.
pub async fn focus_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
) -> Result<(), FrameError> {
    if workspace.project_id() == Some(project_id) {
        return Ok(());
    }
    if let Some(current) = workspace.project_id() {
        chrome.project_tabs.focus(current);
    }
    workspace.persist_workspace(chrome.tabs())?;
    workspace.restore_project(project_id)?;
    workspace.fetch_roster().await?;
    workspace.attach_ready_panes().await?;
    workspace.fetch_sidebar_rows().await?;
    chrome.project_tabs.focus(project_id);
    sync_live_chrome(workspace, chrome);
    save_client_session(workspace, chrome)?;
    restore_focused(workspace, chrome).await
}

/// Show the agent behind `entry_id` (its row's `focus` item): its project
/// is focused first when it is another one, then its pane is revealed where
/// a tab shows it or split into the active tab, and takes focus.
pub async fn focus_agent(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: &str,
) -> Result<(), FrameError> {
    let Some(pane) = reveal_agent(workspace, chrome, entry_id).await? else {
        return Ok(());
    };
    focus_live_pane(workspace, pane).await
}

/// Open a fresh tab in the agent's project holding its pane, or focus the
/// tab that already shows it: a pane is never shown twice.
pub async fn open_agent_in_new_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: &str,
) -> Result<(), FrameError> {
    let Some(pane) = agent_pane(workspace, chrome, entry_id).await? else {
        return Ok(());
    };
    if !chrome.focus_pane(pane) {
        chrome.open_tab(pane, workspace.pane(pane).display_name());
    }
    focus_live_pane(workspace, pane).await
}

/// Reveal the agent's pane on the chrome the way its row's click does and
/// hand it back, so the caller decides how it takes the lease.
pub(super) async fn reveal_agent(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: &str,
) -> Result<Option<PaneId>, FrameError> {
    let Some(pane) = agent_pane(workspace, chrome, entry_id).await? else {
        return Ok(None);
    };
    chrome.reveal_pane(pane, workspace.pane(pane).display_name());
    Ok(Some(pane))
}

/// Tell the daemon the entry's prompt was seen, with the attention id the
/// roster carries; an entry without one has nothing to mark.
pub async fn mark_agent_seen(
    workspace: &mut Workspace<LiveDaemon>,
    entry_id: &str,
) -> Result<(), FrameError> {
    let Some(attention_id) = attention_id(workspace, entry_id) else {
        return Ok(());
    };
    workspace
        .daemon()
        .mark_seen(entry_id, &attention_id)
        .await?;
    Ok(())
}

/// The entry's pane once its project is the focused one: another project's
/// agent focuses that project first, whose roster attaches the pane.
async fn agent_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: &str,
) -> Result<Option<PaneId>, FrameError> {
    let project = workspace
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .map(|agent| agent.project_id.clone())
        .filter(|project| workspace.project_id() != Some(project.as_str()));
    if let Some(project) = project {
        focus_project(workspace, chrome, &project).await?;
    }
    Ok(attention_pane(workspace, entry_id))
}

/// Open `worktree_id`: focus its project, then activate the tab that
/// already shows the worktree or spawn a shell in it as a fresh tab, tagged
/// so the next click finds it. An id the sidebar no longer lists is ignored.
pub async fn open_worktree(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    worktree_id: &str,
) -> Result<(), FrameError> {
    let found = workspace.sidebar().projects.iter().find_map(|project| {
        project
            .worktrees
            .iter()
            .find(|worktree| worktree.worktree_id == worktree_id)
            .map(|worktree| (project.project_id.clone(), worktree.path.clone()))
    });
    let Some((project_id, path)) = found else {
        return Ok(());
    };
    focus_project(workspace, chrome, &project_id).await?;
    let shown = chrome
        .tabs()
        .tabs
        .iter()
        .position(|tab| tab.worktree_id.as_deref() == Some(worktree_id));
    if let Some(index) = shown {
        return activate_live_tab(workspace, chrome, index).await;
    }
    let before = chrome.tabs().tabs.len();
    let cwd = Some(path.to_string_lossy().into_owned());
    spawn_live_shell(workspace, chrome, Placement::Tab, cwd).await?;
    if chrome.tabs().tabs.len() > before {
        if let Some(tab) = chrome.active_tab_mut() {
            tab.worktree_id = Some(worktree_id.to_owned());
        }
    }
    Ok(())
}

/// Fill the focused project's tab bar when it is empty: from its snapshot
/// when one was read, else with one shell. A workspace whose snapshot store
/// was never consulted (no Gobby home) is left alone.
pub(super) async fn restore_focused(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    if let Some(project) = workspace.project_id() {
        chrome.project_tabs.focus(project);
    }
    if !chrome.tabs().tabs.is_empty() {
        return Ok(());
    }
    let Some(snapshot) = workspace.saved_snapshot() else {
        return Ok(());
    };
    *chrome.tabs_mut() = TabSet::from_snapshot(snapshot, |terminal_id| {
        workspace.pane_for_terminal(terminal_id)
    });
    if chrome.tabs().tabs.is_empty() {
        spawn_live_terminal(workspace, chrome, Placement::Tab).await?;
    }
    Ok(())
}

/// Write the focused project's snapshot when it differs from the last one
/// written; `last` carries that between calls.
pub(super) fn persist_if_changed(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
    last: &mut Option<WorkspaceSnapshot>,
) -> std::io::Result<()> {
    let Some(snapshot) = workspace.workspace_snapshot(chrome.tabs()) else {
        return Ok(());
    };
    if last.as_ref() == Some(&snapshot) {
        return Ok(());
    }
    if let Some(home) = workspace.gobby_home() {
        save_snapshot(home, &snapshot)?;
    }
    *last = Some(snapshot);
    Ok(())
}

/// Write `session.json`: the focused project and the sidebar state.
pub(super) fn save_client_session(
    workspace: &Workspace<LiveDaemon>,
    chrome: &Chrome,
) -> std::io::Result<()> {
    let Some(home) = workspace.gobby_home() else {
        return Ok(());
    };
    let session = ClientSession {
        focused_project: chrome.project_tabs.focused.clone(),
        sidebar: sidebar_snapshot(&chrome.sidebar),
    };
    save_session(home, &session).map(|_| ())
}

/// Open the new-project dialog on `~/`.
pub fn open_new_project_dialog(chrome: &mut Chrome) {
    let path = "~/".to_string();
    chrome.dialog = Some(Dialog::NewProject {
        cursor: path.chars().count(),
        path,
        error: None,
    });
    chrome.mode = Mode::ProjectDialog;
}

/// Register the checkout at `path` (with `~` expanded), then focus it; the
/// daemon's refusal shows in the dialog.
pub async fn submit_new_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    path: &str,
) -> Result<(), FrameError> {
    let expanded = expand_home(path.trim(), &home_dir());
    match workspace.daemon().init_project(&expanded).await {
        Ok(row) => {
            workspace.fetch_sidebar_rows().await?;
            close_modal(chrome);
            focus_project(workspace, chrome, &row.id).await
        }
        Err(error) => {
            set_dialog_error(chrome, daemon_reason(&error));
            Ok(())
        }
    }
}

/// Open the new-worktree dialog for `project_id`, based on its branch.
pub fn open_new_worktree_dialog<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &mut Chrome,
    project_id: &str,
) {
    let Some(project) = project_entry(workspace, project_id) else {
        return;
    };
    chrome.dialog = Some(Dialog::NewWorktree {
        project_id: project_id.to_owned(),
        branch: String::new(),
        base: project.branch.clone().unwrap_or_default(),
        cursor: 0,
        base_focused: false,
        error: None,
    });
    chrome.mode = Mode::ProjectDialog;
}

/// Create a client worktree and open a shell tab in it; the daemon's
/// refusal shows in the dialog.
pub async fn create_worktree(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
    branch: &str,
    base: Option<&str>,
) -> Result<(), FrameError> {
    match workspace
        .daemon()
        .create_worktree(project_id, branch.trim(), base)
        .await
    {
        Ok(row) => {
            workspace.fetch_sidebar_rows().await?;
            close_modal(chrome);
            open_worktree(workspace, chrome, &row.id).await
        }
        Err(error) => {
            set_dialog_error(chrome, daemon_reason(&error));
            Ok(())
        }
    }
}

/// Open the picker of `project_id`'s worktrees no tab shows.
pub fn open_open_worktree_dialog<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &mut Chrome,
    project_id: &str,
) {
    let Some(project) = project_entry(workspace, project_id) else {
        return;
    };
    let shown = |worktree_id: &str| {
        chrome.project_tabs.sets.get(project_id).is_some_and(|set| {
            set.tabs
                .iter()
                .any(|tab| tab.worktree_id.as_deref() == Some(worktree_id))
        })
    };
    let choices = project
        .worktrees
        .iter()
        .filter(|worktree| !shown(&worktree.worktree_id))
        .map(|worktree| WorktreeChoice {
            worktree_id: worktree.worktree_id.clone(),
            branch: worktree.branch.clone(),
            path: worktree.path.to_string_lossy().into_owned(),
        })
        .collect();
    chrome.dialog = Some(Dialog::OpenWorktree {
        project_id: project_id.to_owned(),
        choices,
        selected: 0,
    });
    chrome.mode = Mode::ProjectDialog;
}

/// Open the remove confirmation for `worktree_id`, counting the tabs and
/// panes that show it.
pub fn open_remove_worktree_dialog<D: Daemon>(
    workspace: &Workspace<D>,
    chrome: &mut Chrome,
    worktree_id: &str,
) {
    let Some((project_id, worktree)) = worktree_entry(workspace, worktree_id) else {
        return;
    };
    let tagged: Vec<&Tab> = tagged_tabs(chrome, &project_id, worktree_id);
    chrome.dialog = Some(Dialog::RemoveWorktree {
        worktree_id: worktree_id.to_owned(),
        branch: worktree.branch.clone(),
        path: worktree.path.to_string_lossy().into_owned(),
        tabs: tagged.len(),
        panes: tagged.iter().map(|tab| tab.slots.len()).sum(),
        error: None,
    });
    chrome.mode = Mode::ProjectDialog;
}

/// Terminate the worktree's terminals, then delete the checkout; a
/// surviving terminal keeps the checkout and says so in the dialog.
pub async fn remove_worktree(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    worktree_id: &str,
) -> Result<(), FrameError> {
    let Some((project_id, _)) = worktree_entry(workspace, worktree_id) else {
        return Ok(());
    };
    focus_project(workspace, chrome, &project_id).await?;
    let panes: Vec<PaneId> = tagged_tabs(chrome, &project_id, worktree_id)
        .iter()
        .flat_map(|tab| tab.slots.values().copied())
        .collect();
    for pane in panes {
        terminate_live_terminal(workspace, pane).await?;
    }
    sync_live_chrome(workspace, chrome);
    let live: usize = tagged_tabs(chrome, &project_id, worktree_id)
        .iter()
        .map(|tab| tab.slots.len())
        .sum();
    if live > 0 {
        set_dialog_error(chrome, format!("{} still live", plural(live, "terminal")));
        return Ok(());
    }
    match workspace.daemon().delete_worktree(worktree_id).await {
        Ok(()) => {
            workspace.fetch_sidebar_rows().await?;
            close_modal(chrome);
        }
        Err(error) => set_dialog_error(chrome, daemon_reason(&error)),
    }
    Ok(())
}

/// Ask before closing `project_id` when the prefs say so, else close it.
pub async fn close_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
) -> Result<(), FrameError> {
    let Some(project) = project_entry(workspace, project_id) else {
        return Ok(());
    };
    if !chrome.prefs.confirm_close {
        return close_project_confirmed(workspace, chrome, project_id).await;
    }
    let (tabs, panes) = chrome
        .project_tabs
        .sets
        .get(project_id)
        .map_or((0, 0), |set| {
            (
                set.tabs.len(),
                set.tabs.iter().map(|tab| tab.slots.len()).sum(),
            )
        });
    let (target, scope) = if project.worktrees.is_empty() {
        (
            CloseTarget::Project(project_id.to_owned()),
            CloseScope::Tabs { tabs, panes },
        )
    } else {
        (
            CloseTarget::WorktreeGroup(project_id.to_owned()),
            CloseScope::Group {
                workspaces: 1 + project.worktrees.len(),
                panes,
            },
        )
    };
    chrome.dialog = Some(Dialog::ConfirmClose {
        target,
        title: project_label(workspace, chrome, project_id).unwrap_or_default(),
        scope,
    });
    chrome.mode = Mode::ConfirmClose;
    Ok(())
}

/// Terminate every terminal of every tab of `project_id`; a refused kill
/// keeps its pane.
pub async fn close_project_confirmed(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
) -> Result<(), FrameError> {
    focus_project(workspace, chrome, project_id).await?;
    let panes: Vec<PaneId> = chrome
        .tabs()
        .tabs
        .iter()
        .flat_map(|tab| tab.slots.values().copied())
        .collect();
    for pane in panes {
        terminate_live_terminal(workspace, pane).await?;
    }
    sync_live_chrome(workspace, chrome);
    Ok(())
}

/// Open the rename dialog on `project_id`'s current label.
pub fn rename_project(chrome: &mut Chrome, project_id: &str, current: &str) {
    open_live_rename(
        chrome,
        RenameKind::Project(project_id.to_owned()),
        current.to_owned(),
    );
}

/// Keys of the project dialogs. New project: tab completes a directory,
/// enter hands the path to the loop and keeps the dialog for the daemon's
/// answer. New worktree: tab moves between branch and base. Open
/// worktree: up/down pick, enter opens. Remove worktree: enter/y confirms,
/// keeping the dialog for the daemon's answer.
pub fn project_dialog_key(chrome: &mut Chrome, key: &KeyEvent) -> ModalOutcome {
    let Some(dialog) = chrome.dialog.as_mut() else {
        return close_modal(chrome);
    };
    match dialog {
        Dialog::NewProject {
            path,
            cursor,
            error,
        } => match key.code {
            KeyCode::Enter => return ModalOutcome::InitProject(path.clone()),
            KeyCode::Esc => return close_modal(chrome),
            KeyCode::Tab => {
                if let Some(completed) = complete_directory(path, &home_dir()) {
                    *cursor = completed.chars().count();
                    *path = completed;
                }
            }
            _ => {
                *error = None;
                edit_text(path, cursor, key);
            }
        },
        Dialog::NewWorktree {
            project_id,
            branch,
            base,
            cursor,
            base_focused,
            error,
        } => match key.code {
            KeyCode::Enter => {
                return ModalOutcome::CreateWorktree {
                    project_id: project_id.clone(),
                    branch: branch.clone(),
                    base: (!base.is_empty()).then(|| base.clone()),
                }
            }
            KeyCode::Esc => return close_modal(chrome),
            KeyCode::Tab | KeyCode::BackTab => {
                *base_focused = !*base_focused;
                let focused = if *base_focused { &*base } else { &*branch };
                *cursor = focused.chars().count();
            }
            _ => {
                *error = None;
                let focused = if *base_focused { base } else { branch };
                edit_text(focused, cursor, key);
            }
        },
        Dialog::OpenWorktree {
            choices, selected, ..
        } => match key.code {
            KeyCode::Up | KeyCode::Char('k') => *selected = selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Char('j') => {
                *selected = (*selected + 1).min(choices.len().saturating_sub(1));
            }
            KeyCode::Enter => {
                let picked = choices
                    .get(*selected)
                    .map(|choice| choice.worktree_id.clone());
                close_modal(chrome);
                return picked.map_or(ModalOutcome::Close, ModalOutcome::OpenWorktree);
            }
            KeyCode::Esc => return close_modal(chrome),
            _ => {}
        },
        Dialog::RemoveWorktree { worktree_id, .. } => match key.code {
            KeyCode::Enter | KeyCode::Char('y') => {
                return ModalOutcome::RemoveWorktree(worktree_id.clone())
            }
            KeyCode::Esc | KeyCode::Char('n') => return close_modal(chrome),
            _ => {}
        },
        Dialog::ConfirmClose { .. } | Dialog::Rename { .. } | Dialog::Respond { .. } => {
            return close_modal(chrome)
        }
    }
    ModalOutcome::Consumed
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
}

fn project_entry<'a, D: Daemon>(
    workspace: &'a Workspace<D>,
    project_id: &str,
) -> Option<&'a ProjectEntry> {
    workspace
        .sidebar()
        .projects
        .iter()
        .find(|project| project.project_id == project_id)
}

/// The worktree row and its project's id.
fn worktree_entry<'a, D: Daemon>(
    workspace: &'a Workspace<D>,
    worktree_id: &str,
) -> Option<(String, &'a WorktreeEntry)> {
    workspace.sidebar().projects.iter().find_map(|project| {
        project
            .worktrees
            .iter()
            .find(|worktree| worktree.worktree_id == worktree_id)
            .map(|worktree| (project.project_id.clone(), worktree))
    })
}

/// The tabs of `project_id`'s set opened from `worktree_id`.
fn tagged_tabs<'a>(chrome: &'a Chrome, project_id: &str, worktree_id: &str) -> Vec<&'a Tab> {
    chrome
        .project_tabs
        .sets
        .get(project_id)
        .map(|set| {
            set.tabs
                .iter()
                .filter(|tab| tab.worktree_id.as_deref() == Some(worktree_id))
                .collect()
        })
        .unwrap_or_default()
}

/// Show `message` in whichever project dialog is open.
fn set_dialog_error(chrome: &mut Chrome, message: String) {
    match chrome.dialog.as_mut() {
        Some(
            Dialog::NewProject { error, .. }
            | Dialog::NewWorktree { error, .. }
            | Dialog::RemoveWorktree { error, .. },
        ) => *error = Some(message),
        _ => chrome.status_message = Some(message),
    }
}

/// The daemon's reason for a refusal: the `message`, `error_code`, or
/// `error` of a JSON `detail` body, else the error's own text.
fn daemon_reason(error: &DaemonError) -> String {
    let DaemonError::Protocol { detail } = error else {
        return error.to_string();
    };
    let Ok(body) = serde_json::from_str::<serde_json::Value>(detail) else {
        return detail.clone();
    };
    let detail_value = body.get("detail").unwrap_or(&body);
    ["message", "error_code", "error"]
        .iter()
        .find_map(|key| detail_value.get(key).and_then(|value| value.as_str()))
        .or_else(|| detail_value.as_str())
        .map_or_else(|| detail.clone(), str::to_owned)
}
