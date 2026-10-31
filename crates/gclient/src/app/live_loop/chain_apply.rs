//! Applies the committed answers of chained lifecycle jobs (plan A4b of
//! gclient-daemon-resilience). Each user action runs its daemon steps as one
//! job, so the loop never waits on a reply. When the answer lands it either
//! installs the result, or, when the loop has moved on, records the committed
//! mutation as an orphan that is disposed once the model says it still exists.

use crate::daemon::{
    Daemon, DaemonError, Generation, KillOutcome, LiveDaemon, SpawnOutcome, WorkspaceOp,
};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;
use tokio::sync::mpsc::UnboundedSender;

use super::super::live::relist::Relist;
use super::super::Workspace;
use super::control::focus_live_pane;
use super::daemon_ops::{op_tag, place_live_terminal, run_op};
use super::jobs::{
    spawn_job, AdoptedShell, AwaitingPane, CommittedOrphan, DisposalOutcome, JobOutcome, JobResult,
    LoopJobs, Orphan, OwnedShell, PaneAction, Settled,
};
use super::sync_live_chrome;

/// Applies a legacy spawn's reply. A reply the loop no longer waits for
/// becomes an orphan, so the terminal it committed is disposed of.
pub(super) fn apply_spawned(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    current: bool,
    then: PaneAction,
    result: Result<SpawnOutcome, DaemonError>,
    roster: Option<Result<Relist, DaemonError>>,
) {
    match result {
        Ok(SpawnOutcome::Created { terminal_id, .. }) if !current => {
            commit_orphan(jobs, Orphan::Terminal(terminal_id));
        }
        Ok(SpawnOutcome::Created { terminal_id, .. }) => {
            workspace.pending_spawns.insert(terminal_id.clone());
            install_roster(workspace, roster);
            jobs.ledger
                .awaiting_panes
                .push(AwaitingPane { terminal_id, then });
            settle_panes(jobs, workspace, chrome);
        }
        Ok(SpawnOutcome::Refused { reason }) if current => {
            chrome.notify(Toast::warning(reason));
        }
        Err(error) if current => chrome.notify(Toast::error(FrameError::from(error).to_string())),
        Ok(SpawnOutcome::Refused { .. }) | Err(_) => {}
    }
}

/// Applies an owned shell's create chain.
pub(super) fn apply_shell(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    current: bool,
    result: Result<OwnedShell, DaemonError>,
) {
    let shell = match result {
        Ok(shell) => shell,
        Err(DaemonError::Workspace(error)) if current => {
            chrome.notify(Toast::warning(error.reason));
            return;
        }
        Err(error) if current => {
            chrome.notify(Toast::error(FrameError::from(error).to_string()));
            return;
        }
        Err(_) => return,
    };
    if !current {
        commit_orphan(jobs, Orphan::Terminal(shell.terminal_id));
        return;
    }
    let terminal_id = shell.terminal_id;
    let replaced_draft = shell.draft.is_some();
    if let Some((draft_id, created_tab)) = shell.draft {
        // Find the draft by identity: another chain may have made a different
        // draft the active tab, and a draft closed meanwhile has nothing to rename.
        let project = chrome.project_tabs.key().to_owned();
        if let Some(tab) = chrome
            .project_tabs
            .set_mut()
            .tabs
            .iter_mut()
            .find(|tab| tab.id == draft_id)
        {
            tab.id = created_tab.clone();
            chrome.viewer.focus.remove(&draft_id);
            if chrome.viewer.active_tab.get(&project) == Some(&draft_id) {
                chrome.viewer.active_tab.insert(project, created_tab);
            }
        }
    }
    match shell.snapshot {
        Some(snapshot) => workspace.apply_workspace_snapshot(snapshot),
        None => workspace.expect_placement(&terminal_id),
    }
    if let Some(warning) = shell.tab_order_warning {
        chrome.notify(Toast::warning(warning));
    }
    workspace.pending_spawns.insert(terminal_id.clone());
    install_roster(workspace, shell.roster);
    let then = if replaced_draft {
        PaneAction::Focus
    } else {
        PaneAction::Attach
    };
    jobs.ledger
        .awaiting_panes
        .push(AwaitingPane { terminal_id, then });
    settle_panes(jobs, workspace, chrome);
}

/// Applies a local tab adoption chain. A chain that failed has already
/// rolled its tab back, unless the rollback itself failed and the tab is named
/// in the toast.
pub(super) fn apply_adopted(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    current: bool,
    old_tab: String,
    adopted: AdoptedShell,
) {
    let AdoptedShell {
        new_tab,
        rolled_back,
        terminal_id,
        snapshot,
        tab_order_warning,
        roster,
    } = adopted;
    let terminal_id = match terminal_id {
        Ok(terminal_id) => terminal_id,
        Err(error) => {
            match (&new_tab, rolled_back) {
                (Some(tab), false) => {
                    let orphan = Orphan::Tab(tab.clone());
                    commit_orphan(jobs, orphan.clone());
                    let outcome = match error {
                        DaemonError::Workspace(refusal) => DisposalOutcome::Refused(refusal.reason),
                        error => DisposalOutcome::Failed(error),
                    };
                    apply_disposed(workspace, chrome, jobs, orphan, outcome);
                }
                _ if current => match error {
                    DaemonError::Workspace(refusal) => {
                        chrome.notify(Toast::warning(refusal.reason));
                    }
                    error => chrome.notify(Toast::error(FrameError::from(error).to_string())),
                },
                _ => {}
            }
            if let (true, Some(snapshot)) = (current, snapshot) {
                workspace.apply_workspace_snapshot(snapshot);
            }
            return;
        }
    };
    let Some(new_tab) = new_tab else { return };
    // Resolve the initiating local view before installing/reprojecting the snapshot.
    let target = chrome.tabs().tabs.iter().position(|tab| tab.id == old_tab);
    if !current {
        commit_orphan(jobs, Orphan::Tab(new_tab));
        return;
    }
    if target.is_none() {
        if let Some(snapshot) = snapshot {
            workspace.apply_workspace_snapshot(snapshot);
        }
        commit_orphan(jobs, Orphan::Tab(new_tab));
        return;
    }
    let was_active = chrome.active_tab().is_some_and(|tab| tab.id == old_tab);
    if let Some(tab) = target.and_then(|index| chrome.tabs_mut().tabs.get_mut(index)) {
        tab.id = new_tab.clone();
    }
    if let Some(snapshot) = snapshot {
        workspace.apply_workspace_snapshot(snapshot);
    }
    let was_zoomed = chrome.viewer.zoomed.remove(&old_tab);
    chrome.viewer.focus.remove(&old_tab);
    if was_zoomed {
        chrome.viewer.zoomed.insert(new_tab.clone());
    }
    if was_active {
        chrome
            .viewer
            .active_tab
            .insert(chrome.project_tabs.key().to_owned(), new_tab);
    }
    if let Some(warning) = tab_order_warning {
        chrome.notify(Toast::warning(warning));
    }
    workspace.pending_spawns.insert(terminal_id.clone());
    install_roster(workspace, roster);
    jobs.ledger.awaiting_panes.push(AwaitingPane {
        terminal_id,
        then: if was_active {
            PaneAction::Focus
        } else {
            PaneAction::Attach
        },
    });
    settle_panes(jobs, workspace, chrome);
}

/// Records how a disposal settled. A refusal or a lost answer waits for the
/// model to move before it is tried again.
pub(super) fn apply_disposed(
    workspace: &Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
    orphan: Orphan,
    outcome: DisposalOutcome,
) {
    let Some(index) = jobs
        .ledger
        .committed_orphans
        .iter()
        .position(|entry| entry.orphan == orphan)
    else {
        return;
    };
    let label = match &orphan {
        Orphan::Terminal(id) => format!("Terminal {id}"),
        Orphan::Tab(tab) => format!("Tab {tab}"),
    };
    let lost = match outcome {
        DisposalOutcome::Done => {
            jobs.ledger.committed_orphans.remove(index);
            return;
        }
        DisposalOutcome::Refused(reason) => {
            chrome.notify(Toast::warning(format!("{label} was not closed: {reason}")));
            false
        }
        DisposalOutcome::Failed(error) => {
            chrome.notify(Toast::error(format!(
                "{label} was not closed: {}",
                FrameError::from(error)
            )));
            true
        }
    };
    let entry = &mut jobs.ledger.committed_orphans[index];
    entry.in_flight = false;
    entry.settled = Some(Settled {
        model_install: model_install(workspace),
        lost,
    });
}

/// Places or focuses committed terminals whose panes the roster has shown,
/// and keeps the rest while the roster still tracks them.
pub(super) fn settle_panes(
    jobs: &mut LoopJobs,
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) {
    let outcomes = jobs.outcomes().clone();
    let mut waiting = Vec::new();
    for entry in std::mem::take(&mut jobs.ledger.awaiting_panes) {
        match workspace.pane_for_terminal(&entry.terminal_id) {
            Some(pane) => {
                // A settled pane attaches on the next tick, as `apply_open` marks
                // an opened one; an attached pane at this generation is not due.
                workspace.pane_mut(pane).attach_retry_at = Some(tokio::time::Instant::now());
                match entry.then {
                    PaneAction::Place {
                        placement,
                        worktree_id,
                    } => place_live_terminal(
                        workspace,
                        chrome,
                        &outcomes,
                        placement,
                        &entry.terminal_id,
                        worktree_id,
                        None,
                    ),
                    PaneAction::Focus => {
                        chrome.focus_pane(pane);
                        focus_live_pane(workspace, pane);
                    }
                    PaneAction::Attach => {}
                }
            }
            None if workspace.pending_spawns.contains(&entry.terminal_id) => waiting.push(entry),
            None => {}
        }
    }
    jobs.ledger.awaiting_panes = waiting;
    sync_live_chrome(workspace, chrome);
}

/// Decides, against the current model, which committed orphans still need a
/// disposal, and starts those. A settled orphan waits for the model to move.
pub(super) fn evaluate(jobs: &mut LoopJobs, workspace: &Workspace<LiveDaemon>) {
    // Without an authoritative model nothing can be judged absent.
    let Some(model) = workspace.workspace_model() else {
        return;
    };
    if !workspace.daemon_ready() || model.installed_generation != workspace.daemon().generation() {
        return;
    }
    let install = model_install(workspace);
    let daemon = workspace.daemon().clone();
    let outcomes = jobs.outcomes().clone();
    jobs.ledger.committed_orphans.retain_mut(|entry| {
        if entry.in_flight {
            return true;
        }
        if entry
            .settled
            .as_ref()
            .is_some_and(|settled| settled.model_install == install)
        {
            return true;
        }
        let settled_lost = entry.settled.as_ref().is_some_and(|settled| settled.lost);
        let needed = match &entry.orphan {
            // The daemon still holds the tab while the model does.
            Orphan::Tab(tab) => model.tab(tab).is_some(),
            // A terminal the model places is the current state. After a lost
            // answer, one the roster no longer lists is already gone.
            Orphan::Terminal(id) => {
                !model
                    .panes()
                    .any(|pane| pane.terminal_id.as_deref() == Some(id))
                    && !(settled_lost && !workspace.roster_ids.contains(id))
            }
        };
        if !needed {
            return false;
        }
        entry.in_flight = true;
        spawn_disposal(&daemon, &outcomes, entry.orphan.clone());
        true
    });
}

fn commit_orphan(jobs: &mut LoopJobs, orphan: Orphan) {
    jobs.ledger.committed_orphans.push(CommittedOrphan {
        orphan,
        in_flight: false,
        settled: None,
    });
}

/// Installs a roster read taken after a create. A read the daemon refused or
/// that a newer roster overtook is asked for again.
fn install_roster(
    workspace: &mut Workspace<LiveDaemon>,
    roster: Option<Result<Relist, DaemonError>>,
) {
    match roster {
        Some(Ok(relist)) => {
            workspace.apply_relist(relist);
        }
        Some(Err(error)) => {
            tracing::warn!(%error, "roster read after a create failed; retrying");
            workspace.request_relist();
        }
        None => workspace.request_relist(),
    }
}

fn model_install(workspace: &Workspace<LiveDaemon>) -> Option<(Generation, u64)> {
    workspace
        .workspace_model()
        .map(|model| (model.installed_generation, model.install))
}

fn spawn_disposal(daemon: &LiveDaemon, outcomes: &UnboundedSender<JobOutcome>, orphan: Orphan) {
    let tag = op_tag(daemon);
    let daemon = daemon.clone();
    spawn_job(outcomes, tag, async move {
        let outcome = match &orphan {
            Orphan::Terminal(id) => match daemon.terminate(id).await {
                Ok(KillOutcome::Killed { .. }) => DisposalOutcome::Done,
                Ok(KillOutcome::Refused { .. }) => {
                    DisposalOutcome::Refused("the daemon kept the terminal".into())
                }
                Err(error) => DisposalOutcome::Failed(error),
            },
            Orphan::Tab(tab) => {
                let close = WorkspaceOp::TabClose {
                    tab: tab.clone(),
                    node: None,
                };
                match run_op(&daemon, close).await {
                    Ok(()) => DisposalOutcome::Done,
                    Err(DaemonError::Workspace(error)) => DisposalOutcome::Refused(error.reason),
                    Err(error) => DisposalOutcome::Failed(error),
                }
            }
        };
        JobResult::Disposed { orphan, outcome }
    });
}
