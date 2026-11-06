//! A destroy-orphans request stays pending until its inventory lands. An inventory
//! that lands from an earlier daemon generation still opens the dialog, because
//! the request number, not the generation, decides whether it applies.

use super::*;
use crate::app::live_loop::orphans::open_destroy_orphans_dialog;

#[tokio::test]
async fn a_pending_orphan_inventory_from_an_earlier_generation_still_opens_its_dialog() {
    let mock = MockDaemon::start("local-token").await;
    mock.seed_workspace("project-1", &[(&["existing"], "existing")]);
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("mock daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("initial model and roster");
    let mut chrome = Chrome::dark();
    chrome.focus_project("project-1");
    let mut jobs = LoopJobs::new(&workspace);
    // The inventory is a `terminal_list` request, answered from the seeded rows.
    mock.seed_terminal_inventory(vec![
        json!({"terminal_id": "orphan-1", "backend": "native", "state": "orphaned"}),
    ]);
    open_destroy_orphans_dialog(&workspace, &mut chrome, jobs.outcomes());
    assert_eq!(
        chrome.orphan_pending,
        Some(1),
        "the request stays pending until its inventory lands"
    );
    let outcome = stale(received(&mut jobs).await, &workspace);
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
    assert!(
        chrome.dialog.is_some(),
        "the pending request's inventory opens its dialog even after a reconnect; toasts: {:?}",
        chrome
            .alert_log
            .iter()
            .map(|toast| toast.title.clone())
            .collect::<Vec<_>>()
    );
    mock.shutdown().await;
}
