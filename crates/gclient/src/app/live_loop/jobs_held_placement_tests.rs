//! A held placement follows its pane: it waits while the model shows the pane on
//! its tab and is dropped once the pane leaves the model.

use super::*;
use crate::app::sync_live_chrome;

#[tokio::test]
async fn a_held_placement_is_dropped_once_its_pane_leaves_the_model() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, snapshot) = fixture(&mock).await;
    let tab = snapshot.tabs[0].id.clone();
    // The model shows this pane on the tab, but its terminal has not landed, so
    // no slot for it is in the tab yet: its placement has to wait.
    let mut pending = snapshot.panes[0].clone();
    pending.id = "pane-pending".into();
    pending.reference = 3;
    pending.terminal_id = Some("55555555-5555-4555-8555-555555555555".into());
    let pane_event = |kind, seq| crate::daemon::WorkspaceEvent {
        kind,
        workspace_id: snapshot.workspace.id.clone(),
        project_id: Some("project-1".to_owned()),
        workspace: None,
        tabs: vec![snapshot.tabs[0].clone()],
        panes: vec![pending.clone()],
        daemon_epoch: snapshot.snapshot.daemon_epoch.clone(),
        seq,
        timestamp: String::new(),
    };
    let seq = snapshot.snapshot.seq;
    assert!(
        workspace.apply_workspace_event(&pane_event(
            crate::daemon::WorkspaceEventKind::PaneAdded,
            seq + 1
        )),
        "the pane added event applies"
    );
    workspace
        .placed_panes
        .push((tab.clone(), pending.id.clone()));

    sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        workspace.placed_panes,
        vec![(tab.clone(), "pane-pending".to_owned())],
        "a pane the model still shows on its tab keeps its placement held"
    );

    assert!(
        workspace.apply_workspace_event(&pane_event(
            crate::daemon::WorkspaceEventKind::PaneRemoved,
            seq + 2
        )),
        "the pane removed event applies"
    );
    sync_live_chrome(&mut workspace, &mut chrome);
    assert!(
        workspace.placed_panes.is_empty(),
        "a pane that left the model drops its placement, so later syncs stop re-projecting for it"
    );
    assert!(
        workspace
            .workspace_model()
            .is_some_and(|model| model.tab(&tab).is_some()),
        "the tab survives its pane"
    );
    mock.shutdown().await;
}
