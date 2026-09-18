//! Project focus for the live loop: the outgoing project's tab set is parked
//! in the chrome, the incoming one is shown or seeded with a shell
//! (decision 7); the daemon's workspace rows remember the focus.

use std::path::PathBuf;

use crossterm::event::{KeyCode, KeyEvent};

use crate::daemon::{Daemon, DaemonError, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::chrome::{attention_pane, Tab};
use crate::ui::dialogs::project::{complete_directory, expand_home, plural};
use crate::ui::dialogs::{CloseScope, CloseTarget, Dialog, OrphanRow, RenameKind, WorktreeChoice};
use crate::ui::sidebar::TERMINAL_ROW;
use crate::ui::sidebar_rows::project_label;
use crate::ui::{Chrome, Mode};

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
use super::workspace_actions::{place_live_terminal, send_workspace_op};

/// Make `project_id` the focused project: its roster replaces the current
/// one and its tab set the tab bar; the outgoing set stays parked in the
/// chrome.
pub async fn focus_project(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    project_id: &str,
) -> Result<(), FrameError> {
    if workspace.project_id() == Some(project_id) {
        return Ok(());
    }
    if let Some(current) = workspace.project_id() {
        chrome.focus_project(current);
    }
    workspace.select_project(project_id);
    workspace.fetch_roster().await?;
    workspace.attach_ready_panes().await?;
    // The new project's sessions and runs arrive from a background
    // refetch; the switch itself never waits on git status.
    workspace.request_focused_sessions();
    chrome.focus_project(project_id);
    chrome.sidebar.expanded_project = Some(project_id.to_owned());
    sync_live_chrome(workspace, chrome);
    restore_focused(workspace, chrome).await
}

/// Show the agent behind `entry_id` (its row's `focus` item, and its row's
/// click): its project is focused first when it is another one, then its
/// pane is revealed on the tab already showing it, or in a new tab when
/// none does, and takes control the way any explicit activation does.
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
        let terminal_id = workspace.pane(pane).terminal_id.clone();
        place_live_terminal(workspace, chrome, Placement::Tab, &terminal_id, None).await?;
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
    if !chrome.focus_pane(pane) {
        let terminal_id = workspace.pane(pane).terminal_id.clone();
        place_live_terminal(workspace, chrome, Placement::Tab, &terminal_id, None).await?;
    }
    Ok(Some(pane))
}

/// Reveal and focus a terminal picked from the navigator. The pane is
/// attached on demand when the daemon row arrived outside reconciliation.
pub(super) async fn focus_terminal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    terminal_id: &str,
) -> Result<(), FrameError> {
    let pane = terminal_pane(workspace, terminal_id).await?;
    if !chrome.focus_pane(pane) {
        place_live_terminal(workspace, chrome, Placement::SplitRight, terminal_id, None).await?;
    }
    focus_live_pane(workspace, pane).await
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
///
/// A sidebar row is either a session the roster joined to an agent entry or a
/// bare terminal keyed `terminal:<terminal_id>` (`bare_terminals`). Only the
/// first kind appears in `sidebar().agents`, so resolving through that list
/// alone answered `None` for every bare row — and each caller reads `None` as
/// "nothing to do" and returns `Ok(())`. That one miss is why clicking such a
/// row did nothing, why "open in new tab" did nothing, and why a right-click
/// close fell through to whatever pane happened to be focused: `focus_menu_target`
/// retargets through here before the action runs. A bare row carries its
/// terminal in its own id and belongs to the focused project already, so the
/// id is the whole answer.
async fn agent_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    entry_id: &str,
) -> Result<Option<PaneId>, FrameError> {
    let agent = workspace
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .map(|agent| (agent.project_id.clone(), agent.terminal_id.clone()));
    let terminal_id = match agent {
        Some((project, terminal_id)) => {
            if workspace.project_id() != Some(project.as_str()) {
                focus_project(workspace, chrome, &project).await?;
            }
            terminal_id
        }
        None => match entry_id.strip_prefix(TERMINAL_ROW) {
            Some(terminal_id) => terminal_id.to_string(),
            None => return Ok(None),
        },
    };
    if let Some(pane) = attention_pane(workspace, entry_id) {
        return Ok(Some(pane));
    }
    Ok(Some(terminal_pane(workspace, &terminal_id).await?))
}

async fn terminal_pane(
    workspace: &mut Workspace<LiveDaemon>,
    terminal_id: &str,
) -> Result<PaneId, FrameError> {
    match workspace.pane_for_terminal(terminal_id) {
        Some(pane) => Ok(pane),
        None => workspace.open_live_terminal(terminal_id).await,
    }
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
    let worktree = Some(worktree_id.to_owned());
    spawn_live_shell(workspace, chrome, Placement::Tab, cwd, worktree).await?;
    if chrome.tabs().tabs.len() > before {
        if let Some(tab) = chrome.active_tab_mut() {
            tab.worktree_id = Some(worktree_id.to_owned());
        }
    }
    Ok(())
}

/// Fill the focused project's tab bar when it is empty. The daemon's tabs
/// were projected by the chrome sync, so an empty bar means the workspace
/// has none for the project, and one shell opens its first tab. A window
/// without a Gobby home (only the loop tests run that way) seeds nothing.
pub(super) async fn restore_focused(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    if let Some(project) = workspace.project_id() {
        chrome.focus_project(project);
    }
    // A window inside a gclient pane shows what the outer window opens and
    // never seeds a shell of its own.
    if workspace.in_pane() || !chrome.tabs().tabs.is_empty() || workspace.gobby_home().is_none() {
        return Ok(());
    }
    spawn_live_terminal(workspace, chrome, Placement::Tab).await
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
            branch: worktree.branch.clone().unwrap_or_else(|| "~".to_string()),
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
        branch: worktree.branch.clone().unwrap_or_else(|| "~".to_string()),
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
    // The worktree's terminals are gone: its daemon tabs go with them.
    let tabs: Vec<String> = tagged_tabs(chrome, &project_id, worktree_id)
        .iter()
        .filter(|tab| !tab.is_local())
        .map(|tab| tab.id.clone())
        .collect();
    for tab in tabs {
        send_workspace_op(workspace, chrome, WorkspaceOp::TabClose { tab, node: None }).await?;
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
        Dialog::DestroyOrphans {
            rows,
            checked,
            selected,
        } => match key.code {
            KeyCode::Up | KeyCode::Char('k') => *selected = selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Char('j') => {
                *selected = (*selected + 1).min(rows.len().saturating_sub(1));
            }
            KeyCode::Char(' ') => {
                if let Some(flag) = checked.get_mut(*selected) {
                    *flag = !*flag;
                }
            }
            KeyCode::Char('a') => {
                let all = checked.iter().all(|flag| *flag);
                checked.iter_mut().for_each(|flag| *flag = !all);
            }
            KeyCode::Enter => {
                let picked: Vec<OrphanRow> = rows
                    .iter()
                    .zip(checked.iter())
                    .filter(|(_, flag)| **flag)
                    .map(|(row, _)| row.clone())
                    .collect();
                close_modal(chrome);
                return if picked.is_empty() {
                    ModalOutcome::Close
                } else {
                    ModalOutcome::DestroyOrphans(picked)
                };
            }
            KeyCode::Esc => return close_modal(chrome),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    fn orphan(terminal_id: &str) -> OrphanRow {
        OrphanRow {
            terminal_id: terminal_id.to_string(),
            backend: "tmux".to_string(),
            name: terminal_id.to_string(),
            owner: None,
            last_seen: None,
        }
    }

    fn press(chrome: &mut Chrome, code: KeyCode) -> ModalOutcome {
        project_dialog_key(chrome, &KeyEvent::new(code, KeyModifiers::NONE))
    }

    fn checked(chrome: &Chrome) -> (Vec<bool>, usize) {
        match &chrome.dialog {
            Some(Dialog::DestroyOrphans {
                checked, selected, ..
            }) => (checked.clone(), *selected),
            other => panic!("destroy-orphans dialog expected, got {other:?}"),
        }
    }

    #[test]
    fn destroy_orphans_dialog_toggles_and_confirms_checked_rows() {
        let mut chrome = Chrome::dark();
        chrome.dialog = Some(Dialog::DestroyOrphans {
            rows: vec![orphan("alpha"), orphan("beta"), orphan("gamma")],
            checked: vec![true; 3],
            selected: 0,
        });
        chrome.mode = Mode::ProjectDialog;

        assert_eq!(
            press(&mut chrome, KeyCode::Char('j')),
            ModalOutcome::Consumed
        );
        assert_eq!(
            press(&mut chrome, KeyCode::Char(' ')),
            ModalOutcome::Consumed
        );
        assert_eq!(checked(&chrome), (vec![true, false, true], 1));
        // `a` fills a partial set, then clears a full one.
        press(&mut chrome, KeyCode::Char('a'));
        assert_eq!(checked(&chrome).0, [true, true, true]);
        press(&mut chrome, KeyCode::Char('a'));
        assert_eq!(checked(&chrome).0, [false, false, false]);
        press(&mut chrome, KeyCode::Down);
        press(&mut chrome, KeyCode::Down);
        press(&mut chrome, KeyCode::Down);
        assert_eq!(checked(&chrome).1, 2, "selection stops at the last row");
        press(&mut chrome, KeyCode::Char(' '));
        press(&mut chrome, KeyCode::Char('k'));
        press(&mut chrome, KeyCode::Char(' '));
        assert_eq!(checked(&chrome), (vec![false, true, true], 1));

        assert_eq!(
            press(&mut chrome, KeyCode::Enter),
            ModalOutcome::DestroyOrphans(vec![orphan("beta"), orphan("gamma")])
        );
        assert_eq!(
            (chrome.mode, chrome.dialog.is_none()),
            (Mode::Terminal, true)
        );

        // Nothing checked confirms as a plain close; esc always closes.
        chrome.dialog = Some(Dialog::DestroyOrphans {
            rows: vec![orphan("alpha")],
            checked: vec![false],
            selected: 0,
        });
        chrome.mode = Mode::ProjectDialog;
        assert_eq!(press(&mut chrome, KeyCode::Enter), ModalOutcome::Close);
        chrome.dialog = Some(Dialog::DestroyOrphans {
            rows: vec![orphan("alpha")],
            checked: vec![true],
            selected: 0,
        });
        chrome.mode = Mode::ProjectDialog;
        assert_eq!(press(&mut chrome, KeyCode::Esc), ModalOutcome::Close);
        assert!(chrome.dialog.is_none());
    }
}
