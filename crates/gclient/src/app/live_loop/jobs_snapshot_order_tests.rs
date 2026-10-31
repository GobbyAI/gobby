//! A delayed chain snapshot never replaces a newer installed model.

use super::*;

/// Applies a tab create past `snapshot`'s watermark, as the live loop does
/// while a chain still awaits its roster, and returns the new tab's id.
fn apply_newer_tab(workspace: &mut Workspace<LiveDaemon>, snapshot: &WorkspaceSnapshot) -> String {
    let mut tab = snapshot.tabs[0].clone();
    tab.id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee".to_owned();
    let event = crate::daemon::WorkspaceEvent {
        kind: crate::daemon::WorkspaceEventKind::TabCreated,
        workspace_id: snapshot.workspace.id.clone(),
        project_id: Some("project-1".to_owned()),
        workspace: None,
        tabs: vec![tab.clone()],
        panes: Vec::new(),
        daemon_epoch: snapshot.snapshot.daemon_epoch.clone(),
        seq: snapshot.snapshot.seq + 1,
        timestamp: String::new(),
    };
    assert!(
        workspace.apply_workspace_event(&event),
        "newer event applies"
    );
    tab.id
}

fn assert_newer_model_survives(workspace: &Workspace<LiveDaemon>, seq: u64, tab: &str) {
    let model = workspace.workspace_model().expect("model");
    assert_eq!(model.watermark.seq, seq, "watermark is not rolled back");
    assert!(model.tab(tab).is_some(), "the newer tab survives");
}

#[tokio::test]
async fn an_older_shell_snapshot_does_not_replace_a_newer_model() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    let newer_tab = apply_newer_tab(&mut workspace, &snapshot);
    let seq = snapshot.snapshot.seq + 1;
    let shell = jobs::OwnedShell {
        terminal_id: "existing".to_owned(),
        pane_id: snapshot.panes[0].id.clone(),
        draft: None,
        snapshot: Some(snapshot),
        tab_order_warning: None,
        roster: None,
    };
    chain_apply::apply_shell(&mut workspace, &mut chrome, &mut jobs, true, Ok(shell));
    assert_newer_model_survives(&workspace, seq, &newer_tab);
    mock.shutdown().await;
}

#[tokio::test]
async fn an_older_adoption_snapshot_does_not_replace_a_newer_model() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    let newer_tab = apply_newer_tab(&mut workspace, &snapshot);
    let seq = snapshot.snapshot.seq + 1;
    let old_tab = snapshot.tabs[0].id.clone();
    let adopted = jobs::AdoptedShell {
        new_tab: Some(old_tab.clone()),
        rolled_back: false,
        terminal_id: Ok("existing".to_owned()),
        snapshot: Some(snapshot),
        tab_order_warning: None,
        roster: None,
    };
    chain_apply::apply_adopted(
        &mut workspace,
        &mut chrome,
        &mut jobs,
        true,
        old_tab,
        adopted,
    );
    assert_newer_model_survives(&workspace, seq, &newer_tab);
    mock.shutdown().await;
}
