//! Split placement depends on the focused daemon pane. A split with no
//! resolved focus is refused; it never becomes a new tab.

use crate::app::live_loop::actions::live_layout_area;
use crate::app::live_loop::daemon_ops::place_live_terminal;
use crate::app::live_loop::projection::sync_live_chrome;
use crate::app::live_loop::workspace_actions::placement_op;
use crate::daemon::WorkspaceOp;
use crate::ui::pane_layout::{lone_pane_inner_rect, split_terminal_rect};
use ratatui::layout::Direction;

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

/// A gclient-opened tab starts at the rect its pane is drawn in, so the daemon
/// never holds the 80x24 default for a pane the user sees at another size.
#[tokio::test]
async fn a_new_tab_opens_at_the_viewport_of_its_layout() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);

    let inner = lone_pane_inner_rect(live_layout_area(&chrome), chrome.prefs.pane_scrollbars);
    assert_ne!(
        inner.height, inner.width,
        "a square viewport would hide swapped rows and cols"
    );
    let op = placement_op(&workspace, &chrome, Placement::Tab, None, None, None, None);
    let Some(WorkspaceOp::TabCreate { rows, cols, .. }) = op else {
        panic!("a tab placement opens a tab");
    };
    assert_eq!(
        (rows, cols),
        (Some(inner.height), Some(inner.width)),
        "the tab opens at the size its pane is drawn in"
    );
    mock.shutdown().await;
}

/// A split opens at the rect the new pane takes along the split axis.
#[tokio::test]
async fn a_split_opens_at_the_viewport_of_its_layout() {
    let mock = MockDaemon::start("local-token").await;
    let (mut workspace, mut chrome, _) = fixture(&mock).await;
    project_with_daemon_tab_active(&mut workspace, &mut chrome);

    let tab = chrome.active_tab().expect("the daemon tab is active");
    let split_rect = split_terminal_rect(
        &tab.layout,
        chrome.tab_focus(tab),
        Direction::Horizontal,
        live_layout_area(&chrome),
        &chrome.prefs,
    )
    .expect("the focused pane splits");
    assert_ne!(
        split_rect.height, split_rect.width,
        "a square viewport would hide swapped rows and cols"
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
    let Some(WorkspaceOp::PaneSplit { rows, cols, .. }) = op else {
        panic!("a split of a resolved pane is a pane.split");
    };
    assert_eq!(
        (rows, cols),
        (Some(split_rect.height), Some(split_rect.width)),
        "the split opens at the size its pane is drawn in"
    );
    mock.shutdown().await;
}
