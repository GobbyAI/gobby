//! Items under the seven persistent menu-bar titles.

use crate::app::ControlState;
use crate::ui::menu_bar::MenuBarMenu;
use crate::ui::{Action, Chrome, WorkspaceView};

use super::menu::{
    agents_view_items, blocked_entry, enabled_if, item, passthrough_label, MenuAction, MenuItem,
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
            act("settings", Action::Settings),
            act("reload config", Action::ReloadConfig),
            act("quit", Action::Quit),
        ],
        MenuBarMenu::File => vec![
            act("new terminal", Action::NewTerminal),
            act("new tab", Action::NewTab),
            act("new workspace…", Action::NewProject),
            enabled_if(
                act("rename tab", Action::RenameTab),
                chrome.focused_pane().is_some(),
            ),
            enabled_if(
                act("close tab", Action::CloseTab),
                chrome.focused_pane().is_some(),
            ),
            item("destroy orphaned terminals…", MenuAction::DestroyOrphans),
            enabled_if(act("detach", Action::Detach), held),
        ],
        MenuBarMenu::Edit => edit_items(ws, chrome),
        MenuBarMenu::View => {
            let mut items = agents_view_items(chrome);
            items.push(act(
                if chrome.sidebar.all_projects {
                    "all projects"
                } else {
                    "working projects"
                },
                Action::ToggleProjectsFilter,
            ));
            items.push(act("show sidebar", Action::ToggleSidebar));
            items.push(item("pin sidebar", MenuAction::PinSidebar));
            items
        }
        MenuBarMenu::Window => vec![
            enabled_if(
                act("split right", Action::SplitVertical),
                chrome.focused_pane().is_some(),
            ),
            enabled_if(
                act("split down", Action::SplitHorizontal),
                chrome.focused_pane().is_some(),
            ),
            enabled_if(
                act(
                    if chrome.is_zoomed() { "unzoom" } else { "zoom" },
                    Action::Zoom,
                ),
                chrome.focused_pane().is_some(),
            ),
            enabled_if(
                act("close pane", Action::ClosePane),
                chrome.focused_pane().is_some(),
            ),
            enabled_if(
                act("resize mode", Action::ResizeMode),
                chrome.focused_pane().is_some(),
            ),
        ],
        MenuBarMenu::Agent => {
            let entry = focused.and_then(|pane| blocked_entry(ws, pane));
            let has_entry = entry.is_some();
            let attention = ws.attention_entry_ids();
            let can_step = attention.len() > 1
                || attention
                    .first()
                    .is_some_and(|only| entry.as_ref() != Some(only));
            vec![
                enabled_if(act("respond", Action::Respond), has_entry),
                enabled_if(
                    item("mark seen", MenuAction::MarkSeen(entry.unwrap_or_default())),
                    has_entry,
                ),
                enabled_if(
                    act("take control", Action::TakeControl),
                    focused.is_some() && !held,
                ),
                enabled_if(act("release control", Action::ReleaseControl), held),
                enabled_if(
                    act("take back", Action::TakeBack),
                    focused.is_some() && !held,
                ),
                enabled_if(act("detach", Action::Detach), held),
                enabled_if(
                    act("open alert target", Action::OpenNotificationTarget),
                    chrome.latest_alert_target().is_some(),
                ),
                enabled_if(act("next attention", Action::NextAttention), can_step),
                enabled_if(
                    act("previous attention", Action::PreviousAttention),
                    can_step,
                ),
            ]
        }
        MenuBarMenu::Help => vec![
            act("keys", Action::Help),
            item("alerts…", MenuAction::ShowAlerts),
        ],
    }
}

/// The pane items, applied to the focused pane; shown disabled while no
/// pane has focus.
fn edit_items<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<MenuItem> {
    let focused = chrome.focused_pane();
    let mut items: Vec<MenuItem> = [
        ("copy mode", Action::CopyMode),
        ("rename pane", Action::RenamePane),
        ("rename tab", Action::RenameTab),
        ("rename terminal", Action::RenameTerminal),
    ]
    .into_iter()
    .map(|(label, action)| enabled_if(item(label, MenuAction::Act(action)), focused.is_some()))
    .collect();
    if let Some(pane) = focused {
        let state = ws.pane(pane);
        if state.label.is_some() {
            items.push(item("clear pane name", MenuAction::ClearPaneName(pane)));
        }
        items.push(item(
            passthrough_label(state),
            MenuAction::TogglePassthrough(pane),
        ));
    }
    items
}
