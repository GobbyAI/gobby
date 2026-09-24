//! Project daemon workspace rows into the visible tab and pane layout.

use crate::app::ViewerState;
use crate::daemon::LiveDaemon;
use crate::ui::chrome::Tab;
use crate::ui::Chrome;
use gobby_terminal::layout;

use super::super::Workspace;

/// Show the daemon's layout for the focused project and reap what left.
/// The model is re-projected when it (or the project) moved since the last
/// sync, and a pane a placement op landed in takes the focus. Then the
/// slots whose pane left the workspace go: a local tab's as before, the
/// tab dropped once empty; a daemon tab's only unmapped, since the daemon's
/// layout keeps the slot, which renders empty until its terminal resolves.
pub fn sync_live_chrome(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let focused_pane_removed = workspace
        .focus
        .is_some_and(|pane| !workspace.panes.contains_key(&pane));
    project_live_workspace(workspace, chrome);
    let index = chrome.active_index();
    let viewer = &mut chrome.viewer;
    let set = chrome.project_tabs.set_mut();
    for tab in &mut set.tabs {
        let stale: Vec<_> = tab
            .slots
            .iter()
            .filter_map(|(slot, pane_id)| (!workspace.panes.contains_key(pane_id)).then_some(*slot))
            .collect();
        for slot in stale {
            if tab.is_local() {
                close_slot(tab, viewer, slot);
            } else {
                tab.slots.remove(&slot);
            }
        }
        if !tab.slots.is_empty() && !tab.slots.contains_key(&viewer.focus_of(tab)) {
            if let Some(slot) = tab
                .layout
                .pane_ids()
                .into_iter()
                .find(|slot| tab.slots.contains_key(slot))
            {
                viewer.focus.insert(tab.id.clone(), slot);
            }
        }
    }
    set.tabs
        .retain(|tab| !tab.is_local() || !tab.slots.is_empty());
    chrome.settle_active_index(index);
    if focused_pane_removed {
        if let Some(survivor) = chrome.focused_pane() {
            workspace.focus = Some(survivor);
            if !workspace.pane(survivor).take_back {
                workspace.request_control(survivor, false);
            }
        }
    }
}

/// Project the workspace model onto the focused project's tab bar when it
/// moved since the last projection, copy the rows' pane labels onto the
/// resolved panes, and focus the pane a pending placement landed in.
fn project_live_workspace(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    let Some(project) = workspace.project_id().map(str::to_owned) else {
        return;
    };
    chrome.focus_project(&project);
    let Some(model) = workspace.workspace_model() else {
        return;
    };
    let stamp = (project.clone(), model.generation());
    if chrome.viewer.applied.as_ref() == Some(&stamp) {
        return;
    }
    chrome.project_workspace(workspace, &project);
    chrome.viewer.applied = Some(stamp);
    let mut labels = Vec::new();
    for tab in chrome.tabs().tabs.iter().filter(|tab| !tab.is_local()) {
        for (slot, pane) in &tab.slots {
            let label = chrome
                .viewer
                .panes
                .daemon_id(*slot)
                .and_then(|pane_id| model.pane(pane_id))
                .and_then(|row| row.label.clone());
            labels.push((*pane, label));
        }
    }
    for (pane, label) in labels {
        workspace.pane_mut(pane).label = label;
    }
    let mut held = Vec::new();
    for (tab_id, pane_id) in workspace.take_placed_panes() {
        let on_bar = chrome.tabs().tabs.iter().any(|tab| tab.id == tab_id);
        let Some(slot) = chrome.viewer.panes.slot(&pane_id).filter(|_| on_bar) else {
            // Another project's tab: the placement waits for its bar while
            // the daemon still has the tab.
            if workspace
                .workspace_model()
                .is_some_and(|model| model.tab(&tab_id).is_some())
            {
                held.push((tab_id, pane_id));
            }
            continue;
        };
        chrome
            .viewer
            .active_tab
            .insert(project.clone(), tab_id.clone());
        chrome.viewer.focus.insert(tab_id, slot);
        chrome.tab_scroll_follow_active = true;
    }
    workspace.hold_placed_panes(held);
}

/// Drop `slot` from `tab`. The layout keeps its last pane (it refuses to
/// close it), so an emptied tab is left for `sync_live_chrome` to reap.
pub(super) fn close_slot(tab: &mut Tab, viewer: &mut ViewerState, slot: layout::PaneId) {
    tab.slots.remove(&slot);
    if tab.slots.is_empty() {
        return;
    }
    if let Some(next) = tab.layout.close_focused(slot) {
        if viewer.focus_of(tab) == slot {
            viewer.focus.insert(tab.id.clone(), next);
        }
    }
}
