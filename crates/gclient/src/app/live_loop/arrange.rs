//! Client-side layout plans for daemon workspace tabs.

use crate::app::ArrangeLayout;
use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;
use crate::Workspace;

use super::control::focus_live_pane;
use super::workspace_actions::{active_daemon_tab, daemon_pane_id, send_workspace_op};

fn push_move(ops: &mut Vec<WorkspaceOp>, tab_id: &str, pane: &str, beside: &str, axis: LayoutAxis) {
    ops.push(WorkspaceOp::PaneMove {
        pane: pane.to_owned(),
        tab: tab_id.to_owned(),
        beside: Some(beside.to_owned()),
        axis: Some(axis),
        node: None,
    });
}

fn push_resize(ops: &mut Vec<WorkspaceOp>, pane: &str, ratio: f64) {
    ops.push(WorkspaceOp::PaneResize {
        pane: pane.to_owned(),
        ratio,
        node: None,
    });
}

pub fn plan_arrange(layout: ArrangeLayout, tab_id: &str, panes: &[String]) -> Vec<WorkspaceOp> {
    if panes.len() < 2 {
        return Vec::new();
    }
    let mut ops = Vec::new();
    match layout {
        ArrangeLayout::EvenHorizontal | ArrangeLayout::EvenVertical => {
            let axis = if layout == ArrangeLayout::EvenHorizontal {
                LayoutAxis::Horizontal
            } else {
                LayoutAxis::Vertical
            };
            for index in 1..panes.len() {
                push_move(&mut ops, tab_id, &panes[index], &panes[index - 1], axis);
            }
            for index in 0..panes.len() - 1 {
                push_resize(&mut ops, &panes[index], 1.0 / (panes.len() - index) as f64);
            }
        }
        ArrangeLayout::MainHorizontal | ArrangeLayout::MainVertical => {
            let (main_axis, minor_axis) = if layout == ArrangeLayout::MainVertical {
                (LayoutAxis::Horizontal, LayoutAxis::Vertical)
            } else {
                (LayoutAxis::Vertical, LayoutAxis::Horizontal)
            };
            push_move(&mut ops, tab_id, &panes[1], &panes[0], main_axis);
            for index in 2..panes.len() {
                push_move(
                    &mut ops,
                    tab_id,
                    &panes[index],
                    &panes[index - 1],
                    minor_axis,
                );
            }
            push_resize(&mut ops, &panes[0], 0.5);
            for index in 1..panes.len() - 1 {
                push_resize(&mut ops, &panes[index], 1.0 / (panes.len() - index) as f64);
            }
        }
        ArrangeLayout::Tiled => {
            let root = panes.len().isqrt();
            let rows = root + usize::from(root * root < panes.len());
            let cols = panes.len().div_ceil(rows);
            let groups: Vec<&[String]> = panes.chunks(cols).collect();
            for row in 1..groups.len() {
                push_move(
                    &mut ops,
                    tab_id,
                    &groups[row][0],
                    &groups[row - 1][0],
                    LayoutAxis::Vertical,
                );
            }
            // A resize targets the split directly holding the named pane. Set row
            // heights before horizontal members give each head a nearer split.
            for row in 0..groups.len() - 1 {
                push_resize(&mut ops, &groups[row][0], 1.0 / (groups.len() - row) as f64);
            }
            for group in &groups {
                for column in 1..group.len() {
                    push_move(
                        &mut ops,
                        tab_id,
                        &group[column],
                        &group[column - 1],
                        LayoutAxis::Horizontal,
                    );
                }
            }
            for group in &groups {
                for column in 0..group.len() - 1 {
                    push_resize(
                        &mut ops,
                        &group[column],
                        1.0 / (group.len() - column) as f64,
                    );
                }
            }
        }
    }
    ops
}

pub(super) async fn apply_arrange(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    layout: ArrangeLayout,
) -> Result<(), FrameError> {
    let Some(tab) = active_daemon_tab(chrome) else {
        chrome.notify(Toast::warning("Cannot arrange a local tab"));
        return Ok(());
    };
    let tab_id = tab.id.clone();
    let panes: Option<Vec<_>> = tab
        .layout
        .pane_ids()
        .into_iter()
        .map(|slot| daemon_pane_id(chrome, slot))
        .collect();
    let Some(panes) = panes else {
        chrome.notify(Toast::warning("Cannot arrange: pane mapping unavailable"));
        return Ok(());
    };
    if panes.len() < 2 {
        chrome.notify(Toast::info("Nothing to arrange: one pane"));
        return Ok(());
    }
    let focused = chrome.focused_pane();
    for op in plan_arrange(layout, &tab_id, &panes) {
        if !send_workspace_op(workspace, chrome, op).await? {
            return Ok(());
        }
    }
    if let Some(pane) = focused {
        chrome.focus_pane(pane);
        focus_live_pane(workspace, pane).await?;
    }
    Ok(())
}

/// Build a grid with workspace-owned shells, so closing its panes also stops
/// their terminals. The daemon returns each new pane ID for the next split.
pub(super) async fn create_grid(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    rows: u8,
    cols: u8,
) -> Result<(), FrameError> {
    let Some(model) = workspace.workspace_model() else {
        chrome.notify(Toast::warning("Cannot create grid: workspace unavailable"));
        return Ok(());
    };
    let workspace_id = model.workspace.id.clone();
    let Some(project_id) = workspace.project_id().map(str::to_owned) else {
        chrome.notify(Toast::warning("Cannot create grid: no project selected"));
        return Ok(());
    };
    let rows = rows.clamp(1, 4) as usize;
    let cols = cols.clamp(1, 4) as usize;
    let Some(mut head) = create_owned_pane(
        workspace,
        chrome,
        WorkspaceOp::TabCreate {
            workspace: workspace_id,
            project_id,
            worktree_id: None,
            title: Some("new grid".to_owned()),
            terminal_id: None,
            node: None,
        },
        true,
    )
    .await?
    else {
        return Ok(());
    };
    let mut columns = vec![head.clone()];
    for _ in 1..cols {
        let Some(created) = create_owned_pane(
            workspace,
            chrome,
            WorkspaceOp::PaneSplit {
                pane: head,
                axis: LayoutAxis::Horizontal,
                terminal_id: None,
                node: None,
            },
            false,
        )
        .await?
        else {
            return Ok(());
        };
        head = created.clone();
        columns.push(created);
    }
    // Once vertical splits are added, a head's nearest split is vertical.
    // Size the columns first, while their nearest split is horizontal.
    for (index, pane) in columns.iter().take(cols - 1).enumerate() {
        if !send_workspace_op(
            workspace,
            chrome,
            WorkspaceOp::PaneResize {
                pane: pane.clone(),
                ratio: 1.0 / (cols - index) as f64,
                node: None,
            },
        )
        .await?
        {
            return Ok(());
        }
    }
    for column in columns {
        let mut current = column.clone();
        let mut panes = vec![column];
        for _ in 1..rows {
            let Some(created) = create_owned_pane(
                workspace,
                chrome,
                WorkspaceOp::PaneSplit {
                    pane: current,
                    axis: LayoutAxis::Vertical,
                    terminal_id: None,
                    node: None,
                },
                false,
            )
            .await?
            else {
                return Ok(());
            };
            current = created.clone();
            panes.push(created);
        }
        for (index, pane) in panes.iter().take(rows - 1).enumerate() {
            if !send_workspace_op(
                workspace,
                chrome,
                WorkspaceOp::PaneResize {
                    pane: pane.clone(),
                    ratio: 1.0 / (rows - index) as f64,
                    node: None,
                },
            )
            .await?
            {
                return Ok(());
            }
        }
    }
    Ok(())
}

async fn create_owned_pane(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    op: WorkspaceOp,
    focus: bool,
) -> Result<Option<String>, FrameError> {
    match workspace.daemon().workspace_op(op).await {
        Ok(reply) => {
            let pane = reply.result["panes"]
                .as_array()
                .and_then(|panes| panes.first());
            let pane_id = pane.and_then(|pane| pane["id"].as_str());
            let Some(pane_id) = pane_id else {
                chrome.notify(Toast::warning("Grid creation returned no pane"));
                return Ok(None);
            };
            if focus {
                let tab_id = reply.result["tabs"]
                    .as_array()
                    .and_then(|tabs| tabs.first())
                    .and_then(|tab| tab["id"].as_str());
                let (Some(project_id), Some(tab_id)) = (workspace.project_id(), tab_id) else {
                    chrome.notify(Toast::warning("Grid creation returned no tab"));
                    return Ok(None);
                };
                // Projection runs after this menu action, when the tab event arrives.
                chrome
                    .viewer
                    .active_tab
                    .insert(project_id.to_owned(), tab_id.to_owned());
            }
            Ok(Some(pane_id.to_owned()))
        }
        Err(DaemonError::Workspace(error)) => {
            chrome.notify(Toast::warning(error.reason));
            Ok(None)
        }
        Err(error) => Err(error.into()),
    }
}
