//! Network stages run as futures polled beside input and rendering.

use std::future::Future;
use std::pin::Pin;
use std::time::Instant;

use crate::app::startup_stages::{StageState, StartupStage};
use crate::daemon::{
    Daemon, DaemonError, Generation, LiveDaemon, RosterEntry, TerminalRow, WorkspaceSnapshot,
};
use crate::startup::AttachTarget;
use crate::ui::Chrome;

use super::{Relist, Workspace};

pub(super) enum StartupAnswer {
    Health(Option<String>),
    Attached(WorkspaceSnapshot),
    Roster(Relist, (String, u64, Vec<RosterEntry>), Vec<TerminalRow>),
}

pub(super) type StartupFuture = Pin<Box<dyn Future<Output = Result<StartupAnswer, DaemonError>>>>;

pub(super) fn connect(daemon: LiveDaemon) -> StartupFuture {
    Box::pin(async move {
        daemon.reconnect(Generation(0)).await?;
        let version = daemon.config_version().await?;
        Ok(StartupAnswer::Health(version))
    })
}

pub(super) fn attach(daemon: LiveDaemon, target: AttachTarget) -> StartupFuture {
    Box::pin(async move {
        let project = target
            .workspace
            .is_none()
            .then_some(target.project_id.as_deref())
            .flatten();
        daemon
            .attach_workspace(target.node.as_deref(), target.workspace.as_deref(), project)
            .await
            .map(StartupAnswer::Attached)
    })
}

pub(super) fn roster(workspace: &mut Workspace<LiveDaemon>) -> StartupFuture {
    let relist = workspace.start_initial_relist();
    let unresolved = workspace.unresolved_terminal_ids();
    let daemon = workspace.daemon().clone();
    Box::pin(async move {
        let (rows, attention) = tokio::try_join!(relist, daemon.attention_roster())?;
        let mut found = Vec::new();
        for terminal_id in unresolved {
            if let Ok(row) = daemon.terminal(&terminal_id).await {
                found.push(row);
            }
        }
        Ok(StartupAnswer::Roster(rows, attention, found))
    })
}

pub(super) fn mark_running(chrome: &mut Chrome, stage: StartupStage) {
    let now = Instant::now();
    chrome.connection.now = now;
    if let Some(stages) = &mut chrome.connection.stages {
        stages.mark_running(stage, now);
    }
}

pub(super) fn mark_done(chrome: &mut Chrome, stage: StartupStage) {
    let now = Instant::now();
    chrome.connection.now = now;
    let Some(stages) = &mut chrome.connection.stages else {
        return;
    };
    if !matches!(stages.state(stage), StageState::Running { .. }) {
        return;
    }
    stages.mark_done(stage, now);
    let took_ms = stages.elapsed(stage).unwrap_or_default().as_millis() as u64;
    tracing::info!(
        stage = stage.label(),
        took_ms,
        "gclient startup stage complete"
    );
    if stages.finished() {
        tracing::info!(summary = %stages.summary(), "gclient startup complete");
    }
}
