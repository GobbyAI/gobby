//! Client-side layout plans for daemon workspace tabs.

use serde_json::Value;
use tokio::sync::mpsc::UnboundedSender;

use crate::app::{ArrangeLayout, ArrangeTarget, PaneId};
use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;
use crate::Workspace;

use super::actions::activate_live_tab;
use super::daemon_ops::{op_tag, run_op};
use super::jobs::{spawn_job, JobOutcome, JobResult};
use super::workspace_actions::{daemon_pane_id, new_tab_viewport};

/// Run `ops` in order on `daemon`. The first refusal ends the chain and is
/// its result; a close refused `not_found` is done, as in `run_op`.
async fn send_ops(daemon: &LiveDaemon, ops: Vec<WorkspaceOp>) -> Result<(), DaemonError> {
    for op in ops {
        run_op(daemon, op).await?;
    }
    Ok(())
}

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

/// The target tab's index and daemon pane ids, or the notice refusing to
/// arrange it. Never falls back to another tab.
fn arrange_panes(chrome: &Chrome, target: &ArrangeTarget) -> Result<(usize, Vec<String>), Toast> {
    let Some((index, tab)) = chrome
        .tabs()
        .tabs
        .iter()
        .enumerate()
        .find(|(_, tab)| tab.id == target.tab)
    else {
        return Err(Toast::warning("Cannot arrange: that tab has closed"));
    };
    if tab.is_local() {
        return Err(Toast::warning("Cannot arrange a local tab"));
    }
    if target.pane.is_some_and(|pane| tab.slot_for(pane).is_none()) {
        return Err(Toast::warning(
            "Cannot arrange: that pane has moved or closed",
        ));
    }
    let panes: Option<Vec<_>> = tab
        .layout
        .pane_ids()
        .into_iter()
        .map(|slot| daemon_pane_id(chrome, slot))
        .collect();
    let Some(panes) = panes else {
        return Err(Toast::warning("Cannot arrange: pane mapping unavailable"));
    };
    if panes.len() < 2 {
        return Err(Toast::info("Nothing to arrange: one pane"));
    }
    Ok((index, panes))
}

/// Arrange the target tab, showing it first when it is not the active one.
/// The target is checked again once it shows, before any op goes out.
pub(super) async fn apply_arrange(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    layout: ArrangeLayout,
    target: &ArrangeTarget,
) -> Result<(), FrameError> {
    let index = match arrange_panes(chrome, target) {
        Ok((index, _)) => index,
        Err(notice) => {
            chrome.notify(notice);
            return Ok(());
        }
    };
    if index != chrome.active_index() {
        activate_live_tab(workspace, chrome, index).await?;
    }
    let panes = match arrange_panes(chrome, target) {
        Ok((_, panes)) => panes,
        Err(notice) => {
            chrome.notify(notice);
            return Ok(());
        }
    };
    let focus = chrome.focused_pane();
    let ops = plan_arrange(layout, &target.tab, &panes);
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        JobResult::Arranged {
            focus,
            result: send_ops(&daemon, ops).await,
        }
    });
    Ok(())
}

/// Move `pane` to the end of `tab`, which the move menu named when it was
/// built; the window stays on its current tab. A refusal, such as a tab of
/// another project, lands on the status line.
pub(super) fn move_pane_to_tab(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    pane: PaneId,
    tab: &str,
) {
    let slot = chrome
        .tabs()
        .tabs
        .iter()
        .find_map(|candidate| candidate.slot_for(pane));
    let Some(daemon_pane) = slot.and_then(|slot| daemon_pane_id(chrome, slot)) else {
        chrome.notify(Toast::warning("Cannot move: that pane has closed"));
        return;
    };
    if !chrome
        .tabs()
        .tabs
        .iter()
        .any(|candidate| candidate.id == tab)
    {
        chrome.notify(Toast::warning("Cannot move: that tab has closed"));
        return;
    }
    let op = WorkspaceOp::PaneMove {
        pane: daemon_pane,
        tab: tab.to_owned(),
        beside: None,
        axis: None,
        node: None,
    };
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        JobResult::Ops {
            unplaced: None,
            result: run_op(&daemon, op).await,
        }
    });
}

/// Build a grid with workspace-owned shells, so closing its panes also stops
/// their terminals. The chain runs as one job: each split starts from the pane
/// ID the previous reply returned.
pub(super) fn create_grid(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    rows: u8,
    cols: u8,
) {
    let Some(model) = workspace.workspace_model() else {
        chrome.notify(Toast::warning("Cannot create grid: workspace unavailable"));
        return;
    };
    let workspace_id = model.workspace.id.clone();
    let Some(project_id) = workspace.project_id().map(str::to_owned) else {
        chrome.notify(Toast::warning("Cannot create grid: no project selected"));
        return;
    };
    let rows = rows.clamp(1, 4) as usize;
    let cols = cols.clamp(1, 4) as usize;
    let (viewport_rows, viewport_cols) = new_tab_viewport(chrome);
    let create = WorkspaceOp::TabCreate {
        workspace: workspace_id,
        project_id: project_id.clone(),
        worktree_id: None,
        title: Some("new grid".to_owned()),
        terminal_id: None,
        cwd: None,
        rows: viewport_rows,
        cols: viewport_cols,
        node: None,
    };
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        let mut tab = None;
        let result = grid_chain(&daemon, create, rows, cols, &mut tab).await;
        JobResult::Grid {
            project: project_id,
            tab,
            result,
        }
    });
}

/// One grid's chain: the tab, its splits, then their sizes. `tab` takes the
/// new tab as soon as its reply names it, so a later refusal still focuses it.
async fn grid_chain(
    daemon: &LiveDaemon,
    create: WorkspaceOp,
    rows: usize,
    cols: usize,
    tab: &mut Option<String>,
) -> Result<(), DaemonError> {
    let reply = daemon.workspace_op(create).await?;
    let mut head = reply_id(&reply.result, "panes", "Grid creation returned no pane")?;
    *tab = Some(reply_id(
        &reply.result,
        "tabs",
        "Grid creation returned no tab",
    )?);
    let mut columns = vec![head.clone()];
    for _ in 1..cols {
        head = split_pane(daemon, &head, LayoutAxis::Horizontal).await?;
        columns.push(head.clone());
    }
    // Once vertical splits are added, a head's nearest split is vertical.
    // Size the columns first, while their nearest split is horizontal.
    let mut sizes = Vec::new();
    for (index, pane) in columns.iter().take(cols - 1).enumerate() {
        push_resize(&mut sizes, pane, 1.0 / (cols - index) as f64);
    }
    send_ops(daemon, sizes).await?;
    for column in columns {
        let mut current = column.clone();
        let mut panes = vec![column];
        for _ in 1..rows {
            current = split_pane(daemon, &current, LayoutAxis::Vertical).await?;
            panes.push(current.clone());
        }
        let mut sizes = Vec::new();
        for (index, pane) in panes.iter().take(rows - 1).enumerate() {
            push_resize(&mut sizes, pane, 1.0 / (rows - index) as f64);
        }
        send_ops(daemon, sizes).await?;
    }
    Ok(())
}

/// Split `pane` along `axis`; the reply names the new pane.
async fn split_pane(
    daemon: &LiveDaemon,
    pane: &str,
    axis: LayoutAxis,
) -> Result<String, DaemonError> {
    let reply = daemon
        .workspace_op(WorkspaceOp::PaneSplit {
            pane: pane.to_owned(),
            axis,
            terminal_id: None,
            cwd: None,
            node: None,
        })
        .await?;
    reply_id(&reply.result, "panes", "Grid creation returned no pane")
}

/// The `id` of the first `key` entry in a workspace reply.
fn reply_id(result: &Value, key: &str, missing: &str) -> Result<String, DaemonError> {
    result[key][0]["id"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| DaemonError::Protocol {
            detail: missing.to_owned(),
        })
}
