//! Adopt a local tab into the daemon workspace before splitting it.

use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceErrorCode, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;
use gobby_terminal::layout;
use ratatui::layout::Direction;
use std::collections::{HashMap, HashSet};

use super::super::Workspace;
use super::mouse::Placement;

type LocalSplit = (layout::PaneId, layout::PaneId, LayoutAxis, f64);

fn first_local_slot(node: &layout::Node) -> layout::PaneId {
    match node {
        layout::Node::Pane(slot) => *slot,
        layout::Node::Split { first, .. } => first_local_slot(first),
    }
}

fn local_split_steps(node: &layout::Node, steps: &mut Vec<LocalSplit>) {
    if let layout::Node::Split {
        direction,
        ratio,
        first,
        second,
    } = node
    {
        let axis = match direction {
            Direction::Horizontal => LayoutAxis::Horizontal,
            Direction::Vertical => LayoutAxis::Vertical,
        };
        steps.push((
            first_local_slot(first),
            first_local_slot(second),
            axis,
            f64::from(*ratio),
        ));
        local_split_steps(first, steps);
        local_split_steps(second, steps);
    }
}

async fn rollback_adopted_tab(
    workspace: &mut Workspace<LiveDaemon>,
    workspace_id: &str,
    tab_id: String,
) -> Result<(), FrameError> {
    match workspace
        .daemon()
        .workspace_op(WorkspaceOp::TabClose {
            tab: tab_id,
            node: None,
        })
        .await
    {
        Ok(_) => {}
        Err(DaemonError::Workspace(error)) if error.code == WorkspaceErrorCode::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let node = workspace.attach_target().node.clone();
    let snapshot = workspace
        .daemon()
        .workspace_snapshot(node.as_deref(), workspace_id)
        .await?;
    workspace.apply_workspace_snapshot(snapshot);
    Ok(())
}

struct LocalAdoption {
    old_tab: String,
    new_tab: String,
    focused_pane: String,
    workspace_id: String,
    next_position: Option<u32>,
}

async fn adopt_local_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<Option<LocalAdoption>, FrameError> {
    let tab = chrome
        .active_tab()
        .expect("local tab selected for adoption");
    let old_tab = tab.id.clone();
    let title = (!tab.title.is_empty()).then(|| tab.title.clone());
    let worktree_id = tab.worktree_id.clone();
    let focused_slot = chrome.tab_focus(tab);
    let first_slot = first_local_slot(tab.layout.root());
    let mut steps = Vec::new();
    local_split_steps(tab.layout.root(), &mut steps);
    let mut terminals = HashMap::new();
    let mut seen = HashSet::new();
    for slot in tab.layout.pane_ids() {
        let terminal_id = tab
            .slots
            .get(&slot)
            .and_then(|pane| workspace.panes.get(pane))
            .map(|pane| pane.terminal_id.clone());
        let Some(terminal_id) = terminal_id else {
            chrome.notify(Toast::warning(
                "A local pane disappeared; reopen the tab to split it",
            ));
            return Ok(None);
        };
        if !seen.insert(terminal_id.clone()) {
            chrome.notify(Toast::warning(
                "The same terminal appears twice in this tab",
            ));
            return Ok(None);
        }
        terminals.insert(slot, terminal_id);
    }
    let workspace_id = workspace
        .workspace_model()
        .map(|model| model.workspace.id.clone())
        .ok_or_else(|| FrameError::Protocol("workspace vanished during tab adoption".into()))?;
    let project_id = workspace
        .project_id()
        .ok_or_else(|| FrameError::Protocol("project vanished during tab adoption".into()))?
        .to_owned();
    let next_position = chrome
        .tabs()
        .tabs
        .iter()
        .skip(chrome.active_index() + 1)
        .find(|tab| !tab.is_local())
        .and_then(|tab| workspace.workspace_model()?.tab(&tab.id))
        .map(|row| row.position);
    let first_terminal = terminals[&first_slot].clone();
    let reply = match workspace
        .daemon()
        .workspace_op(WorkspaceOp::TabCreate {
            workspace: workspace_id.clone(),
            project_id,
            worktree_id,
            title,
            terminal_id: Some(first_terminal),
            cwd: None,
            node: None,
        })
        .await
    {
        Ok(reply) => reply,
        Err(DaemonError::Workspace(error)) => {
            chrome.notify(Toast::warning(error.reason));
            return Ok(None);
        }
        Err(error) => return Err(error.into()),
    };
    let new_tab = reply.result["tabs"][0]["id"]
        .as_str()
        .ok_or_else(|| FrameError::Protocol("adopted tab.create omitted tab id".into()))?
        .to_owned();
    let built: Result<String, DaemonError> = async {
        let first_pane = reply.result["panes"][0]["id"]
            .as_str()
            .ok_or_else(|| DaemonError::Protocol {
                detail: "adopted tab.create omitted pane id".into(),
            })?
            .to_owned();
        let mut pane_ids = HashMap::from([(first_slot, first_pane)]);
        for (base, added, axis, ratio) in steps {
            let pane = pane_ids.get(&base).ok_or_else(|| DaemonError::Protocol {
                detail: "local split lost its base pane".into(),
            })?;
            let reply = workspace
                .daemon()
                .workspace_op(WorkspaceOp::PaneSplit {
                    pane: pane.clone(),
                    axis,
                    terminal_id: Some(terminals[&added].clone()),
                    cwd: None,
                    node: None,
                })
                .await?;
            let new_pane = reply.result["panes"][0]["id"]
                .as_str()
                .ok_or_else(|| DaemonError::Protocol {
                    detail: "adopted pane.split omitted pane id".into(),
                })?
                .to_owned();
            if (ratio - 0.5).abs() > f64::EPSILON {
                workspace
                    .daemon()
                    .workspace_op(WorkspaceOp::PaneResize {
                        pane: pane.clone(),
                        ratio,
                        node: None,
                    })
                    .await?;
            }
            pane_ids.insert(added, new_pane);
        }
        pane_ids
            .get(&focused_slot)
            .cloned()
            .ok_or_else(|| DaemonError::Protocol {
                detail: "local tab lost its focused pane".into(),
            })
    }
    .await;
    let focused_pane = match built {
        Ok(pane) => pane,
        Err(error) => {
            rollback_adopted_tab(workspace, &workspace_id, new_tab).await?;
            if let DaemonError::Workspace(ref refusal) = error {
                chrome.notify(Toast::warning(refusal.reason.clone()));
                return Ok(None);
            }
            return Err(error.into());
        }
    };
    Ok(Some(LocalAdoption {
        old_tab,
        new_tab,
        focused_pane,
        workspace_id,
        next_position,
    }))
}

pub(super) async fn spawn_in_adopted_local_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    placement: Placement,
    cwd: Option<String>,
) -> Result<bool, FrameError> {
    let Some(adopted) = adopt_local_tab(workspace, chrome).await? else {
        return Ok(true);
    };
    let axis = match placement {
        Placement::SplitRight => LayoutAxis::Horizontal,
        Placement::SplitDown => LayoutAxis::Vertical,
        Placement::Tab => unreachable!("a local tab migration only serves splits"),
    };
    let spawned: Result<String, DaemonError> = async {
        let reply = workspace
            .daemon()
            .workspace_op(WorkspaceOp::PaneSplit {
                pane: adopted.focused_pane.clone(),
                axis,
                terminal_id: None,
                cwd,
                node: None,
            })
            .await?;
        reply.result["panes"][0]["terminal_id"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| DaemonError::Protocol {
                detail: "owned pane.split omitted terminal id".into(),
            })
    }
    .await;
    let terminal_id = match spawned {
        Ok(terminal_id) => terminal_id,
        Err(error) => {
            rollback_adopted_tab(workspace, &adopted.workspace_id, adopted.new_tab).await?;
            if let DaemonError::Workspace(ref refusal) = error {
                chrome.notify(Toast::warning(refusal.reason.clone()));
                return Ok(true);
            }
            return Err(error.into());
        }
    };
    if let Some(position) = adopted.next_position {
        if let Err(error) = workspace
            .daemon()
            .workspace_op(WorkspaceOp::TabMove {
                tab: adopted.new_tab.clone(),
                position,
                workspace: None,
                node: None,
            })
            .await
        {
            chrome.notify(Toast::warning(format!(
                "Shell opened, but tab order could not be saved: {error}"
            )));
        }
    }
    let node = workspace.attach_target().node.clone();
    let snapshot = match workspace
        .daemon()
        .workspace_snapshot(node.as_deref(), &adopted.workspace_id)
        .await
    {
        Ok(snapshot) => snapshot,
        Err(error) => {
            rollback_adopted_tab(workspace, &adopted.workspace_id, adopted.new_tab).await?;
            return Err(error.into());
        }
    };
    workspace.apply_workspace_snapshot(snapshot);
    let was_zoomed = chrome.viewer.zoomed.remove(&adopted.old_tab);
    let tab = chrome
        .active_tab_mut()
        .ok_or_else(|| FrameError::Protocol("local tab disappeared during adoption".into()))?;
    tab.id = adopted.new_tab.clone();
    chrome.viewer.focus.remove(&adopted.old_tab);
    if was_zoomed {
        chrome.viewer.zoomed.insert(adopted.new_tab.clone());
    }
    chrome
        .viewer
        .active_tab
        .insert(chrome.project_tabs.key().to_owned(), adopted.new_tab);
    workspace.pending_spawns.insert(terminal_id.clone());
    workspace.fetch_roster().await?;
    workspace.attach_ready_panes().await?;
    super::sync_live_chrome(workspace, chrome);
    if let Some(pane) = workspace.pane_for_terminal(&terminal_id) {
        chrome.focus_pane(pane);
        super::control::focus_live_pane(workspace, pane);
    }
    Ok(true)
}
