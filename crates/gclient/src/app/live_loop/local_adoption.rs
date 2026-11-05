//! Local tab adoption: a tab the daemon does not know yet becomes a daemon tab.
//! The adoption and the split for the new shell run as one chained job (plan
//! A4b of gclient-daemon-resilience). A failed step closes the tab it made.

use std::collections::{HashMap, HashSet};
use std::future::Future;

use gobby_terminal::layout;
use ratatui::layout::Direction;
use tokio::sync::mpsc::UnboundedSender;

use crate::daemon::{Daemon, DaemonError, LayoutAxis, LiveDaemon, WorkspaceErrorCode, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::live::relist::Relist;
use super::super::Workspace;
use super::daemon_ops::op_tag;
use super::jobs::{spawn_job, AdoptedShell, JobOutcome, JobResult};
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

/// A local tab's layout and terminals, read before its adoption job starts.
struct LocalPlan {
    old_tab: String,
    title: Option<String>,
    worktree_id: Option<String>,
    focused_slot: layout::PaneId,
    first_slot: layout::PaneId,
    steps: Vec<LocalSplit>,
    terminals: HashMap<layout::PaneId, String>,
    workspace_id: String,
    project_id: String,
    next_position: Option<u32>,
}

/// `None` when the tab cannot be adopted; the reason is already a toast.
fn plan_local_adoption(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<Option<LocalPlan>, FrameError> {
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
    Ok(Some(LocalPlan {
        old_tab,
        title,
        worktree_id,
        focused_slot,
        first_slot,
        steps,
        terminals,
        workspace_id,
        project_id,
        next_position,
    }))
}

/// Starts the adoption of the active local tab as one chained job, with the
/// split for the new shell. Returns once the first step is queued.
pub(super) fn spawn_in_adopted_local_tab(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    outcomes: &UnboundedSender<JobOutcome>,
    placement: Placement,
    cwd: Option<String>,
) -> bool {
    if chrome
        .active_tab()
        .is_some_and(|tab| chrome.viewer.adopting.contains(&tab.id))
    {
        chrome.notify(Toast::warning(
            "This tab is still opening; split again once it settles",
        ));
        return true;
    }
    let plan = match plan_local_adoption(workspace, chrome) {
        Ok(Some(plan)) => plan,
        Ok(None) => return true,
        Err(error) => {
            chrome.notify(Toast::error(error.to_string()));
            return true;
        }
    };
    chrome.viewer.adopting.insert(plan.old_tab.clone());
    let axis = match placement {
        Placement::SplitRight => LayoutAxis::Horizontal,
        Placement::SplitDown => LayoutAxis::Vertical,
        Placement::Tab => unreachable!("a local tab migration only serves splits"),
    };
    let project = Some(plan.project_id.clone());
    let old_tab = plan.old_tab.clone();
    let roster = workspace.issue_roster_fetch(plan.project_id.clone());
    let node = workspace.attach_target().node.clone();
    let daemon = workspace.daemon().clone();
    spawn_job(outcomes, op_tag(&daemon), async move {
        // The chain runs in its own task. A panic in it would otherwise leave
        // the old tab adopting for good, so it reports as a failed adoption.
        let chain_daemon = daemon.clone();
        let chain =
            tokio::spawn(
                async move { adopt_chain(&chain_daemon, node, plan, axis, cwd, roster).await },
            );
        let adopted = chain.await.unwrap_or_else(|_| AdoptedShell {
            new_tab: None,
            rolled_back: false,
            terminal_id: Err(DaemonError::Protocol {
                detail: "local tab adoption panicked".into(),
            }),
            snapshot: None,
            tab_order_warning: None,
            roster: None,
        });
        JobResult::Adopted {
            project,
            old_tab,
            adopted,
        }
    });
    true
}

/// Creates the adopted tab, rebuilds the local layout in it, splits the
/// focused pane for the new shell, and reads the result back.
async fn adopt_chain(
    daemon: &LiveDaemon,
    node: Option<String>,
    plan: LocalPlan,
    axis: LayoutAxis,
    cwd: Option<String>,
    roster: impl Future<Output = Result<Relist, DaemonError>>,
) -> AdoptedShell {
    let LocalPlan {
        title,
        worktree_id,
        focused_slot,
        first_slot,
        steps,
        terminals,
        workspace_id,
        project_id,
        next_position,
        ..
    } = plan;
    let first_terminal = terminals[&first_slot].clone();
    let reply = match daemon
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
        Err(error) => return refused(error),
    };
    let Some(new_tab) = reply.result["tabs"][0]["id"].as_str().map(str::to_owned) else {
        return refused(DaemonError::Protocol {
            detail: "adopted tab.create omitted tab id".into(),
        });
    };
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
            let reply = daemon
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
                daemon
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
        Err(error) => return rollback(daemon, &node, &workspace_id, new_tab, error).await,
    };
    let split = daemon
        .workspace_op(WorkspaceOp::PaneSplit {
            pane: focused_pane,
            axis,
            terminal_id: None,
            cwd,
            node: None,
        })
        .await
        .and_then(|reply| {
            reply.result["panes"][0]["terminal_id"]
                .as_str()
                .map(str::to_owned)
                .ok_or_else(|| DaemonError::Protocol {
                    detail: "owned pane.split omitted terminal id".into(),
                })
        });
    let terminal_id = match split {
        Ok(terminal_id) => terminal_id,
        Err(error) => return rollback(daemon, &node, &workspace_id, new_tab, error).await,
    };
    let mut tab_order_warning = None;
    if let Some(position) = next_position {
        let tab_move = WorkspaceOp::TabMove {
            tab: new_tab.clone(),
            position,
            workspace: None,
            node: None,
        };
        if let Err(error) = daemon.workspace_op(tab_move).await {
            tab_order_warning = Some(format!(
                "Shell opened, but tab order could not be saved: {error}"
            ));
        }
    }
    let snapshot = match daemon
        .workspace_snapshot(node.as_deref(), &workspace_id)
        .await
    {
        Ok(snapshot) => snapshot,
        Err(error) => return rollback(daemon, &node, &workspace_id, new_tab, error).await,
    };
    AdoptedShell {
        new_tab: Some(new_tab),
        rolled_back: false,
        terminal_id: Ok(terminal_id),
        snapshot: Some(snapshot),
        tab_order_warning,
        roster: Some(roster.await),
    }
}

/// A step the daemon refused before any tab existed.
fn refused(error: DaemonError) -> AdoptedShell {
    AdoptedShell {
        new_tab: None,
        rolled_back: true,
        terminal_id: Err(error),
        snapshot: None,
        tab_order_warning: None,
        roster: None,
    }
}

/// Closes the tab a failed chain created. A failed close leaves the tab in
/// `new_tab`, named by the toast; a closed tab comes back with a fresh snapshot.
async fn rollback(
    daemon: &LiveDaemon,
    node: &Option<String>,
    workspace_id: &str,
    tab: String,
    error: DaemonError,
) -> AdoptedShell {
    let close = WorkspaceOp::TabClose {
        tab: tab.clone(),
        node: None,
    };
    match daemon.workspace_op(close).await {
        Ok(_) => {}
        Err(DaemonError::Workspace(refusal)) if refusal.code == WorkspaceErrorCode::NotFound => {}
        Err(close_error) => {
            return AdoptedShell {
                new_tab: Some(tab),
                rolled_back: false,
                terminal_id: Err(close_error),
                snapshot: None,
                tab_order_warning: None,
                roster: None,
            };
        }
    }
    AdoptedShell {
        snapshot: daemon
            .workspace_snapshot(node.as_deref(), workspace_id)
            .await
            .ok(),
        ..refused(error)
    }
}
