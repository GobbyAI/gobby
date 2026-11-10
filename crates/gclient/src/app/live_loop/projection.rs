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
    workspace.present_roster_warnings(chrome);
    let focused_pane_removed = workspace
        .focus
        .is_some_and(|pane| !workspace.panes.contains_key(&pane));
    // Projection drops populated slots whose client pane has been reaped.
    // Remember them so their lingering model rows cannot look newly pending.
    let reaped_slots: Vec<_> = chrome
        .tabs()
        .tabs
        .iter()
        .flat_map(|tab| &tab.slots)
        .filter_map(|(slot, pane)| (!workspace.panes.contains_key(pane)).then_some(*slot))
        .collect();
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
        // A new terminal keeps its layout slot while resolving. A reaped
        // terminal or a pane now owned by another tab cannot retain focus.
        let focus = viewer.focus_of(tab);
        let pending =
            !reaped_slots.contains(&focus) && awaiting_pane(workspace, viewer, tab, focus);
        if !tab.slots.is_empty() && !tab.slots.contains_key(&focus) && !pending {
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
    set.tabs.retain(|tab| {
        !tab.is_local()
            || !tab.slots.is_empty()
            || tab
                .id
                .starts_with(super::super::viewer_state::EMPTY_LOCAL_TAB_PREFIX)
    });
    chrome.settle_active_index(index);
    if focused_pane_removed {
        if let Some(survivor) = chrome.focused_pane() {
            workspace.focus = Some(survivor);
        }
    }
}

/// Whether `slot` holds a daemon pane of `tab` still waiting on its terminal:
/// a reserved pane has none until its spawn binds one, and a bound terminal has
/// none until it resolves. Its focus is the daemon's or a placement's, not
/// stale, so projection keeps it. A pane the daemon now places in another tab
/// is not this tab's to wait on. A terminal whose open failed is not waited on
/// either: no retry is pending, so the focus falls back as for a vanished pane.
fn awaiting_pane(
    workspace: &Workspace<LiveDaemon>,
    viewer: &ViewerState,
    tab: &Tab,
    slot: layout::PaneId,
) -> bool {
    if !tab.layout.pane_ids().contains(&slot) {
        return false;
    }
    let Some(model) = workspace.workspace_model() else {
        return false;
    };
    let Some(pane) = viewer
        .panes
        .daemon_id(slot)
        .and_then(|pane_id| model.pane(pane_id))
    else {
        return false;
    };
    if pane.tab_id != tab.id {
        return false;
    }
    let Some(terminal_id) = pane.terminal_id.as_deref() else {
        return true;
    };
    !workspace.open_failed(terminal_id)
}

/// Project the workspace model onto the focused project's tab bar when it
/// moved since the last projection, copy the rows' pane labels onto the
/// resolved panes, and focus the pane a pending placement landed in.
fn project_live_workspace(workspace: &mut Workspace<LiveDaemon>, chrome: &mut Chrome) {
    // A `workspace.select` shows its tab, on the pane its row now names: the
    // window's own focus memo for that tab gives way to the stored one.
    if let Some((project, tab_id)) = workspace.take_requested_focus() {
        if workspace.project_id() != Some(project.as_str()) {
            workspace.select_project(project.clone());
        }
        chrome.viewer.focus.remove(&tab_id);
        chrome.viewer.active_tab.insert(project, tab_id);
        chrome.viewer.applied = None;
        chrome.tab_scroll_follow_active = true;
    }
    let Some(project) = workspace.project_id().map(str::to_owned) else {
        return;
    };
    chrome.focus_project(&project);
    workspace.forget_vanished_open_failures();
    let Some(model) = workspace.workspace_model() else {
        return;
    };
    let stamp = (project.clone(), model.generation());
    // Opening a pane does not bump the generation, so a terminal that had no
    // pane at the last projection forces another one once it resolves (#22972).
    let resolved = chrome
        .viewer
        .unresolved
        .iter()
        .any(|terminal_id| workspace.pane_for_terminal(terminal_id).is_some());
    // A placement recorded after its event was projected still needs its
    // focus; one held for another project's bar waits for that bar.
    let placed_here = workspace
        .placed_panes
        .iter()
        .any(|(tab_id, _)| chrome.tabs().tabs.iter().any(|tab| tab.id == *tab_id));
    if chrome.viewer.applied.as_ref() == Some(&stamp) && !resolved && !placed_here {
        return;
    }
    chrome.viewer.unresolved = chrome.project_workspace(workspace, &project);
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
        // Placement replies can outlive a move. Follow the pane's current
        // owner rather than focusing its slot in the reply's former tab.
        let Some(tab_id) = workspace
            .workspace_model()
            .and_then(|model| model.pane(&pane_id))
            .map(|pane| pane.tab_id.clone())
        else {
            held.push((tab_id, pane_id));
            continue;
        };
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

#[cfg(test)]
#[path = "projection/tests.rs"]
mod tests;
