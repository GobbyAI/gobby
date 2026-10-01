//! Items under the seven persistent menu-bar titles.

use crate::app::ControlState;
use crate::ui::menu_bar::MenuBarMenu;
use crate::ui::{Action, Chrome, WorkspaceView};

use super::menu::{
    arrange_row, blocked_entry, enabled_if, item, passthrough_label, theme_row_label, toggle,
    ArrangeTarget, MenuAction, MenuItem, Submenu,
};

pub fn menu_bar_items<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    menu: MenuBarMenu,
) -> Vec<MenuItem> {
    let act = |label: &'static str, action: Action| item(label, MenuAction::Act(action));
    let focused = chrome.focused_pane();
    let held = focused.is_some_and(|pane| ws.pane(pane).control == ControlState::Held);
    match menu {
        MenuBarMenu::Gobby => vec![
            act("Settings", Action::Settings),
            act("Reload config", Action::ReloadConfig),
            act("Quit", Action::Quit),
        ],
        MenuBarMenu::File => vec![
            act("New terminal", Action::NewTerminal),
            act("New tab", Action::NewTab),
            act("New workspace…", Action::NewProject),
            enabled_if(
                act("Rename tab", Action::RenameTab),
                chrome.focused_pane().is_some(),
            ),
            enabled_if(
                act("Close tab", Action::CloseTab),
                chrome.focused_pane().is_some(),
            ),
            item("Destroy orphaned terminals…", MenuAction::DestroyOrphans),
            enabled_if(act("Detach", Action::Detach), held),
        ],
        MenuBarMenu::Edit => edit_items(ws, chrome),
        // Each section's options live under Sidebar ▸ in a submenu of their
        // own; the state legend is Help › Keys.
        MenuBarMenu::View => vec![
            item(
                theme_row_label(chrome),
                MenuAction::OpenSubmenu(Submenu::Theme),
            ),
            toggle(
                ("✓ Monochrome", "  Monochrome"),
                chrome.prefs.monochrome,
                MenuAction::ToggleMonochrome,
            ),
            item("  Sidebar ▸", MenuAction::OpenSubmenu(Submenu::Sidebar)),
        ],
        MenuBarMenu::Window => {
            let mut items = vec![
                enabled_if(
                    act("Split right", Action::SplitVertical),
                    chrome.focused_pane().is_some(),
                ),
                enabled_if(
                    act("Split down", Action::SplitHorizontal),
                    chrome.focused_pane().is_some(),
                ),
                enabled_if(
                    act(
                        if chrome.is_zoomed() { "Unzoom" } else { "Zoom" },
                        Action::Zoom,
                    ),
                    chrome.focused_pane().is_some(),
                ),
                enabled_if(
                    act("Close pane", Action::ClosePane),
                    chrome.focused_pane().is_some(),
                ),
                enabled_if(
                    act("Resize mode", Action::ResizeMode),
                    chrome.focused_pane().is_some(),
                ),
            ];
            items.push(arrange_row(chrome.active_tab().map(|tab| ArrangeTarget {
                tab: tab.id.clone(),
                pane: None,
            })));
            items
        }
        MenuBarMenu::Agent => {
            let entry = focused.and_then(|pane| blocked_entry(ws, pane));
            let has_entry = entry.is_some();
            let attention = ws.attention_entry_ids();
            let can_step = attention.len() > 1
                || attention
                    .first()
                    .is_some_and(|only| entry.as_ref() != Some(only));
            vec![
                enabled_if(act("Respond", Action::Respond), has_entry),
                enabled_if(
                    item("Mark seen", MenuAction::MarkSeen(entry.unwrap_or_default())),
                    has_entry,
                ),
                enabled_if(
                    act("Take control", Action::TakeControl),
                    focused.is_some() && !held,
                ),
                enabled_if(act("Release control", Action::ReleaseControl), held),
                enabled_if(
                    act("Take back", Action::TakeBack),
                    focused.is_some() && !held,
                ),
                enabled_if(act("Detach", Action::Detach), held),
                enabled_if(
                    act("Open alert target", Action::OpenNotificationTarget),
                    chrome.latest_alert_target().is_some(),
                ),
                enabled_if(act("Next attention", Action::NextAttention), can_step),
                enabled_if(
                    act("Previous attention", Action::PreviousAttention),
                    can_step,
                ),
            ]
        }
        MenuBarMenu::Help => vec![
            act("Keys", Action::Help),
            item("Alerts…", MenuAction::ShowAlerts),
            item("Daemon", MenuAction::ShowDaemon),
            item("About Gobby", MenuAction::ShowAbout),
        ],
    }
}

/// The pane items, applied to the focused pane; shown disabled while no
/// pane has focus.
fn edit_items<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<MenuItem> {
    let focused = chrome.focused_pane();
    let mut items: Vec<MenuItem> = [
        ("Copy mode", Action::CopyMode),
        ("Rename pane", Action::RenamePane),
        ("Rename tab", Action::RenameTab),
        ("Rename terminal", Action::RenameTerminal),
    ]
    .into_iter()
    .map(|(label, action)| enabled_if(item(label, MenuAction::Act(action)), focused.is_some()))
    .collect();
    if let Some(pane) = focused {
        let state = ws.pane(pane);
        if state.label.is_some() {
            items.push(item("Clear pane name", MenuAction::ClearPaneName(pane)));
        }
        items.push(item(
            passthrough_label(state),
            MenuAction::TogglePassthrough(pane),
        ));
    }
    items
}
