//! A pane a placement lands in keeps its focus while its terminal resolves.
//! Shares the apply-path fixture of the parent `jobs_apply_tests` module.

use super::*;

#[tokio::test]
async fn a_placed_pane_keeps_focus_while_its_terminal_resolves() {
    const NEW: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, mut snapshot) = fixture(&mock).await;
    let mut jobs = LoopJobs::new(&workspace);
    // The daemon split the shown tab: the new pane is in the model before its
    // terminal resolves into a pane.
    let existing = snapshot.panes[0].id.clone();
    snapshot.tabs[0].layout = crate::daemon::LayoutNode::Split {
        axis: crate::daemon::LayoutAxis::Horizontal,
        ratio: 0.5,
        children: Box::new([
            crate::daemon::LayoutNode::Pane { pane_id: existing },
            crate::daemon::LayoutNode::Pane {
                pane_id: "mock-pane-new".to_owned(),
            },
        ]),
    };
    let mut placed = snapshot.panes[0].clone();
    placed.id = "mock-pane-new".to_owned();
    placed.reference = 2;
    placed.terminal_id = Some(NEW.to_owned());
    placed.owns_terminal = true;
    placed.label = None;
    snapshot.panes.push(placed);
    workspace.apply_workspace_snapshot(snapshot);
    let shell = jobs::OwnedShell {
        terminal_id: NEW.to_owned(),
        pane_id: "mock-pane-new".to_owned(),
        draft: None,
        snapshot: None,
        tab_order_warning: None,
        roster: None,
    };
    chain_apply::apply_shell(&mut workspace, &mut chrome, &mut jobs, true, Ok(shell));
    mock.enqueue(
        "GET",
        &format!("/api/terminals/{NEW}"),
        200,
        json!({"terminal_id": NEW, "backend": "native", "state": "live"}),
    );
    workspace
        .open_live_terminal_unattached(NEW)
        .await
        .expect("the placed terminal resolves");
    crate::app::sync_live_chrome(&mut workspace, &mut chrome);
    assert_eq!(
        chrome.focused_pane(),
        workspace.pane_for_terminal(NEW),
        "the placed pane takes focus once it resolves"
    );
    mock.shutdown().await;
}
