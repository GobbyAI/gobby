//! Drive the production project job transitions in direct integration fixtures.

use gobby_client::app::{
    apply_project_attachment, dispatch_project_navigation, resume_project_navigation,
    sync_live_chrome, Workspace,
};
use gobby_client::daemon::LiveDaemon;
use gobby_client::ui::Chrome;
use tokio::sync::mpsc;

pub async fn settle_navigation(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    apply_navigation(workspace, chrome).await;
    workspace
        .flush_sidebar_refetches()
        .await
        .expect("project sidebar");
    workspace.fetch_roster().await.expect("project roster");
    sync_live_chrome(workspace, chrome);
}

pub async fn apply_navigation(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let (sender, mut receiver) = mpsc::unbounded_channel();
    while dispatch_project_navigation(workspace, &sender) {
        let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), receiver.recv())
            .await
            .expect("project job finishes")
            .expect("project job reports");
        let current = outcome.tag.generation == workspace.daemon().generation();
        match outcome.result {
            gobby_client::app::JobResult::ProjectAttached(attachment) => {
                apply_project_attachment(workspace, chrome, current, attachment);
                resume_project_navigation(workspace, chrome, &sender)
                    .await
                    .expect("resume project action");
            }
            other => panic!("unexpected project job: {other:?}"),
        }
    }
}

pub async fn settle_removal(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    receiver: &mut mpsc::UnboundedReceiver<gobby_client::app::JobOutcome>,
) {
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(10), receiver.recv())
        .await
        .expect("removal finishes")
        .expect("removal reports");
    let current = outcome.tag.generation == workspace.daemon().generation();
    match outcome.result {
        gobby_client::app::JobResult::WorktreeRemoved(removal) => {
            gobby_client::app::apply_worktree_removal(workspace, chrome, current, removal);
        }
        other => panic!("unexpected removal job: {other:?}"),
    }
    workspace
        .drain_live_events()
        .await
        .expect("removal layout events");
    sync_live_chrome(workspace, chrome);
}
