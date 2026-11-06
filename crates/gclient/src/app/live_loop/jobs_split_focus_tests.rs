//! Split placement depends on the focused daemon pane. A split with no
//! resolved focus is refused; it never becomes a new tab.

use crate::app::live_loop::daemon_ops::place_live_terminal;
use crate::app::live_loop::projection::sync_live_chrome;
use crate::app::live_loop::workspace_actions::placement_op;
use crate::daemon::WorkspaceOp;

use super::*;

/// Project the workspace, then make its daemon tab the active one.
fn project_with_daemon_tab_active(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    sync_live_chrome(workspace, chrome);
    let daemon_tab = chrome
        .tabs()
        .tabs
        .iter()
        .find(|tab| !tab.is_local())
        .map(|tab| tab.id.clone())
        .expect("the snapshot's daemon tab is projected");
    chrome
        .viewer
        .active_tab
        .insert("project-1".to_owned(), daemon_tab);
}

#[tokio::test]
async fn a_split_whose_focused_pane_is_unresolved_is_refused_not_turned_into_a_tab() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);
    assert!(
        chrome.active_tab().is_some_and(|tab| !tab.is_local()),
        "the project's daemon tab is active"
    );
    // A pane opened into the daemon tab takes focus before the daemon has
    // named it, so the focused slot resolves to no daemon pane.
    let pane = workspace
        .pane_for_terminal("existing")
        .expect("existing pane");
    chrome.open_pane(pane, "unresolved");
    let focus = chrome.tab_focus(chrome.active_tab().expect("active tab"));
    assert!(
        chrome.viewer.panes.daemon_id(focus).is_none(),
        "the focused slot has no daemon pane"
    );

    let op = placement_op(
        &workspace,
        &chrome,
        Placement::SplitRight,
        None,
        None,
        None,
        None,
    );
    assert!(
        !matches!(op, Some(WorkspaceOp::TabCreate { .. })),
        "a split with no resolved focused pane must not become tab.create"
    );
    assert!(op.is_none(), "the split is refused instead");
    mock.shutdown().await;
}

#[tokio::test]
async fn a_split_with_a_resolved_focused_pane_still_splits_it() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);
    assert!(
        chrome.active_tab().is_some_and(|tab| !tab.is_local()),
        "the project's daemon tab is active"
    );

    let op = placement_op(
        &workspace,
        &chrome,
        Placement::SplitRight,
        None,
        None,
        None,
        None,
    );
    assert!(
        matches!(op, Some(WorkspaceOp::PaneSplit { .. })),
        "a split whose focused pane resolves is a pane.split"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_split_chord_with_an_unresolved_focus_says_to_wait() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);
    let pane = workspace
        .pane_for_terminal("existing")
        .expect("existing pane");
    chrome.open_pane(pane, "unresolved");
    let jobs = LoopJobs::new(&workspace);

    spawn_live_shell(
        &mut workspace,
        &mut chrome,
        jobs.outcomes(),
        Placement::SplitRight,
        None,
        None,
    )
    .await
    .expect("the chord is handled");

    assert_eq!(
        chrome.alert_log.last().map(|toast| toast.title.as_str()),
        Some("Wait for the focused pane before splitting"),
        "the refusal names the wait, not a missing project"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_refused_split_placement_adds_no_pane_to_the_daemon_tab_locally() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);
    let pane = workspace
        .pane_for_terminal("existing")
        .expect("existing pane");
    chrome.open_pane(pane, "unresolved");
    let slots = chrome.active_tab().expect("active tab").slots.len();
    let jobs = LoopJobs::new(&workspace);

    place_live_terminal(
        &mut workspace,
        &mut chrome,
        jobs.outcomes(),
        Placement::SplitRight,
        "existing",
        None,
        None,
    );

    assert_eq!(
        chrome.active_tab().expect("active tab").slots.len(),
        slots,
        "a daemon tab changes only through the daemon, never the chrome alone"
    );
    mock.shutdown().await;
}

#[tokio::test]
async fn a_refused_split_placement_says_to_wait() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);
    let pane = workspace
        .pane_for_terminal("existing")
        .expect("existing pane");
    chrome.open_pane(pane, "unresolved");
    let jobs = LoopJobs::new(&workspace);

    place_live_terminal(
        &mut workspace,
        &mut chrome,
        jobs.outcomes(),
        Placement::SplitRight,
        "existing",
        None,
        None,
    );

    assert_eq!(
        chrome.alert_log.last().map(|toast| toast.title.as_str()),
        Some("Wait for the focused pane before splitting"),
        "a refused placement says why, since the user sees no pane appear"
    );
    mock.shutdown().await;
}
