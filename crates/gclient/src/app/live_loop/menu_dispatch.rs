//! Live dispatch for context and menu-bar actions.

use crate::daemon::LiveDaemon;
use crate::frame_source::FrameError;
use crate::ui::chrome::attention_pane;
use crate::ui::dialogs::{CloseScope, CloseTarget, Dialog};
use crate::ui::sidebar_rows::project_label;
use crate::ui::{Action, Chrome, Mode};

use super::super::attention::open_response_dialog;
use super::super::Workspace;
use super::actions::{activate_live_tab, handle_live_action};
use super::control::{focus_live_pane, observe_live_pane, release_live_control, take_live_control};
use super::menu::{apply_local_menu_action, ContextMenuKind, MenuAction};
use super::modal_input::open_alerts_dialog;
use super::orphans::{agent_orphan, destroy_orphans, open_destroy_orphans_dialog};
use super::projects::{
    close_live_terminal, close_project, focus_agent, focus_project, mark_agent_seen,
    open_agent_in_new_tab, open_new_worktree_dialog, open_open_worktree_dialog,
    open_remove_worktree_dialog, open_worktree, rename_project, reveal_agent,
};
use super::workspace_actions::apply_daemon_menu_action;

/// A context menu item. A keymap action runs as its chord would once the
/// menu's pane or tab is the focused one (the pane is observed, so the lease
/// stays the action's decision); `respond` focuses the entry's pane and opens
/// its dialog; the project and worktree row items run the `projects` flows,
/// the agent row items its agent helpers; the chrome-only items go through
/// `apply_local_menu_action`.
pub async fn apply_live_menu_action(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    kind: ContextMenuKind,
    action: MenuAction,
) -> Result<bool, FrameError> {
    match action {
        MenuAction::NewGrid { rows, cols } => {
            super::arrange::create_grid(workspace, chrome, rows, cols).await?;
        }
        MenuAction::OpenNewGrid => {
            chrome.dialog = Some(Dialog::NewGrid { rows: 2, cols: 2 });
            chrome.mode = Mode::ProjectDialog;
        }
        MenuAction::Arrange { layout, target } => {
            super::arrange::apply_arrange(workspace, chrome, layout, &target).await?;
        }
        MenuAction::Act(action) => {
            focus_menu_target(workspace, chrome, &kind).await?;
            if action == Action::Quit {
                return Ok(true);
            }
            handle_live_action(workspace, chrome, action).await?;
        }
        MenuAction::Respond(entry_id) => {
            if let Some(pane) = attention_pane(workspace, &entry_id) {
                chrome.focus_pane(pane);
                focus_live_pane(workspace, pane).await?;
            }
            open_response_dialog(workspace, chrome, Some(&entry_id)).await?;
        }
        MenuAction::FocusProject(project_id) => {
            focus_project(workspace, chrome, &project_id).await?;
        }
        MenuAction::OpenWorktreeTab(worktree_id) => {
            open_worktree(workspace, chrome, &worktree_id).await?;
        }
        MenuAction::NewWorktree(project_id) => {
            open_new_worktree_dialog(workspace, chrome, &project_id);
        }
        MenuAction::OpenWorktree(project_id) => {
            open_open_worktree_dialog(workspace, chrome, &project_id);
        }
        MenuAction::RemoveWorktree(worktree_id) => {
            open_remove_worktree_dialog(workspace, chrome, &worktree_id);
        }
        MenuAction::CloseProject(project_id) => {
            close_project(workspace, chrome, &project_id).await?;
        }
        MenuAction::RenameProject(project_id) => {
            let current = project_label(workspace, chrome, &project_id).unwrap_or_default();
            rename_project(chrome, &project_id, &current);
        }
        MenuAction::FocusAgent(entry_id) => focus_agent(workspace, chrome, &entry_id).await?,
        MenuAction::OpenAgentInNewTab(entry_id) => {
            open_agent_in_new_tab(workspace, chrome, &entry_id).await?;
        }
        MenuAction::MarkSeen(entry_id) => mark_agent_seen(workspace, &entry_id).await?,
        MenuAction::ShowAlerts => open_alerts_dialog(chrome),
        MenuAction::ShowDaemon => open_daemon_dialog(workspace, chrome),
        MenuAction::ShowAbout => open_about_dialog(chrome),
        MenuAction::DestroyOrphans => open_destroy_orphans_dialog(workspace, chrome).await,
        MenuAction::DestroyTerminal(terminal_id) => {
            let target = agent_orphan(workspace, &terminal_id);
            destroy_orphans(workspace, chrome, vec![target]).await?;
        }
        MenuAction::CloseTerminal(pane) => {
            if !workspace.panes.contains_key(&pane) {
                return Ok(false);
            }
            if chrome.prefs.confirm_close {
                chrome.dialog = Some(Dialog::ConfirmClose {
                    target: CloseTarget::Terminal(pane),
                    title: workspace.pane(pane).display_name().to_owned(),
                    scope: CloseScope::Panes(1),
                });
                chrome.mode = Mode::ConfirmClose;
            } else {
                close_live_terminal(workspace, chrome, pane).await?;
            }
        }
        MenuAction::TakeControl(pane) => {
            focus_menu_target(workspace, chrome, &kind).await?;
            take_live_control(workspace, pane);
        }
        MenuAction::ReleaseControl(pane) => {
            focus_menu_target(workspace, chrome, &kind).await?;
            release_live_control(workspace, pane).await?;
        }
        _ => {
            if !apply_daemon_menu_action(workspace, chrome, &action).await? {
                apply_local_menu_action(workspace, chrome, &action);
            }
        }
    }
    Ok(false)
}

fn open_daemon_dialog(workspace: &Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let stages = chrome.connection.stages.as_ref();
    let health = if let Some(stage) = stages.and_then(|stages| stages.running()) {
        format!("connecting · {}", stage.label())
    } else if workspace.daemon_ready() {
        "ok".to_owned()
    } else {
        format!(
            "unreachable: {}",
            workspace
                .daemon_error()
                .map(ToString::to_string)
                .unwrap_or_else(|| "daemon unavailable".to_owned())
        )
    };
    chrome.dialog = Some(Dialog::Daemon {
        url: chrome.connection.url.clone(),
        gclient_version: env!("CARGO_PKG_VERSION").to_owned(),
        daemon_version: chrome.connection.daemon_version.clone(),
        health,
        last_roster_refresh: workspace.roster_refresh_age(),
        stages: stages
            .filter(|stages| stages.finished())
            .map(|stages| stages.summary()),
    });
    chrome.mode = Mode::ProjectDialog;
}

fn open_about_dialog(chrome: &mut Chrome) {
    let machine = crate::ui::sidebar::local_hostname()
        .or_else(|| {
            (!chrome.connection.machine.is_empty())
                .then(|| crate::app::short_terminal_id(&chrome.connection.machine))
        })
        .unwrap_or("unknown")
        .to_owned();
    chrome.dialog = Some(Dialog::About {
        url: chrome.connection.url.clone(),
        gclient_version: env!("CARGO_PKG_VERSION").to_owned(),
        daemon_version: chrome.connection.daemon_version.clone(),
        machine,
    });
    chrome.mode = Mode::ProjectDialog;
}

/// Make the menu's pane or tab the one keymap actions act on: a worktree
/// row's is the tab opened from it, an agent row's the pane its entry maps
/// to, revealed and observed so the lease stays the action's decision.
async fn focus_menu_target(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    kind: &ContextMenuKind,
) -> Result<(), FrameError> {
    match kind {
        ContextMenuKind::Pane(pane) if chrome.focused_pane() != Some(*pane) => {
            chrome.focus_pane(*pane);
            observe_live_pane(workspace, *pane).await?;
        }
        ContextMenuKind::Tab(index) if *index != chrome.active_index() => {
            activate_live_tab(workspace, chrome, *index).await?;
        }
        ContextMenuKind::Worktree(worktree_id) => {
            open_worktree(workspace, chrome, worktree_id).await?;
        }
        ContextMenuKind::Agent(entry_id) => {
            if let Some(pane) = reveal_agent(workspace, chrome, entry_id).await? {
                observe_live_pane(workspace, pane).await?;
            }
        }
        ContextMenuKind::MenuBar(_) => {}
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn about_names_the_local_machine_like_the_sidebar() {
        let mut chrome = Chrome::dark();
        chrome.connection.machine = "12345678-aaaa-bbbb-cccc-ddddeeeeffff".to_owned();
        open_about_dialog(&mut chrome);
        let Some(Dialog::About { machine, .. }) = &chrome.dialog else {
            panic!("about dialog");
        };
        let expected = crate::ui::sidebar::local_hostname().unwrap_or("12345678");
        assert_eq!(machine, expected);
    }
}
