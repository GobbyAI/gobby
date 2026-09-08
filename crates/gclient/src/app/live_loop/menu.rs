//! Context menu state and item lists.
//!
//! A right-click on a pane, a tab or empty chrome opens a menu; the sidebar
//! rows (project, worktree, agent) reuse the same state once the projects
//! sidebar lands (plan 5.3). This module is pure: [`build_menu`] reads the
//! workspace and chrome to decide which items apply, [`menu_hit`] maps a
//! screen cell to an item, and the routers in `mouse` and `modal_input` own
//! the open, select, activate and close transitions. The loops dispatch the
//! chosen [`MenuAction`]; the items both loops apply the same way live in
//! [`apply_local_menu_action`].

use ratatui::layout::{Margin, Position, Rect};

use crate::app::{ControlState, PaneId};
use crate::daemon::Daemon;
use crate::ui::chrome::attention_pane;
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::super::Workspace;

/// What the menu was opened on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContextMenuKind {
    Pane(PaneId),
    Tab(usize),
    Project(String),
    Worktree(String),
    Agent(String),
    Global,
}

/// What an item does when activated.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MenuAction {
    /// A keymap action, dispatched exactly as its chord would be once the
    /// menu's pane or tab is the focused one.
    Act(Action),
    /// Exchange the menu's pane with the focused one.
    SwapWithFocused(PaneId),
    /// Drop the name the user gave the pane so it shows the daemon's again.
    ClearPaneName(PaneId),
    /// Flip `Pane::right_click_passthrough`.
    TogglePassthrough(PaneId),
    FocusProject(String),
    OpenWorktreeTab(String),
    NewWorktree(String),
    OpenWorktree(String),
    RemoveWorktree(String),
    ToggleGroup(String),
    CloseProject(String),
    RenameProject(String),
    FocusAgent(String),
    OpenAgentInNewTab(String),
    /// Open the respond dialog for this attention entry.
    Respond(String),
    MarkSeen(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MenuItem {
    pub label: &'static str,
    pub action: MenuAction,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextMenuState {
    pub kind: ContextMenuKind,
    pub anchor: (u16, u16),
    pub items: Vec<MenuItem>,
    pub selected: usize,
    /// Screen row of each item as last drawn; seeded from the anchor so the
    /// pointer finds rows before the first render.
    pub item_rects: Vec<Rect>,
}

/// Narrowest popup, in columns, so short menus still read as a panel.
pub const MENU_MIN_WIDTH: u16 = 14;

/// The items for `kind` in the workspace's and chrome's current state.
pub fn build_menu<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    kind: ContextMenuKind,
    anchor: (u16, u16),
) -> ContextMenuState {
    let items = match &kind {
        ContextMenuKind::Pane(pane) => pane_items(ws, chrome, *pane),
        ContextMenuKind::Tab(_) => tab_items(),
        ContextMenuKind::Global => global_items(),
        // Sidebar rows get their items with the projects sidebar (plan 5.3).
        ContextMenuKind::Project(_) | ContextMenuKind::Worktree(_) | ContextMenuKind::Agent(_) => {
            Vec::new()
        }
    };
    let item_rects = item_rects(menu_rect(anchor, &items), items.len());
    ContextMenuState {
        kind,
        anchor,
        items,
        selected: 0,
        item_rects,
    }
}

/// The popup a menu of `items` occupies at `anchor`: `longest label + 4`
/// wide (floor [`MENU_MIN_WIDTH`]) and `items + 2` tall, before the renderer
/// clamps it to the frame.
pub fn menu_rect(anchor: (u16, u16), items: &[MenuItem]) -> Rect {
    let longest = items
        .iter()
        .map(|item| item.label.chars().count())
        .max()
        .unwrap_or(0);
    let width = u16::try_from(longest + 4).unwrap_or(u16::MAX);
    let height = u16::try_from(items.len() + 2).unwrap_or(u16::MAX);
    Rect::new(anchor.0, anchor.1, width.max(MENU_MIN_WIDTH), height)
}

/// One row per item inside the popup's border.
pub fn item_rects(menu: Rect, count: usize) -> Vec<Rect> {
    menu.inner(Margin::new(1, 1)).rows().take(count).collect()
}

/// The item drawn at a screen cell.
pub fn menu_hit(state: &ContextMenuState, column: u16, row: u16) -> Option<usize> {
    let at = Position::new(column, row);
    state.item_rects.iter().position(|rect| rect.contains(at))
}

/// Open the menu for `kind` at `anchor`.
pub fn open_menu<W: WorkspaceView>(
    ws: &W,
    chrome: &mut Chrome,
    kind: ContextMenuKind,
    anchor: (u16, u16),
) {
    chrome.menu = Some(build_menu(ws, chrome, kind, anchor));
    chrome.mode = Mode::ContextMenu;
}

/// Drop the menu and return the chrome to the terminal.
pub fn close_menu(chrome: &mut Chrome) {
    chrome.menu = None;
    chrome.mode = Mode::Terminal;
}

/// Close the menu and hand back the selected item's action, when the item
/// is enabled.
pub fn activate_menu(chrome: &mut Chrome) -> Option<(ContextMenuKind, MenuAction)> {
    let menu = chrome.menu.take();
    chrome.mode = Mode::Terminal;
    let menu = menu?;
    let item = menu.items.into_iter().nth(menu.selected)?;
    item.enabled.then_some((menu.kind, item.action))
}

/// Apply the items both loops handle on chrome and workspace state alone;
/// `true` when `action` was one of them.
pub fn apply_local_menu_action<D: Daemon>(
    workspace: &mut Workspace<D>,
    chrome: &mut Chrome,
    action: &MenuAction,
) -> bool {
    match action {
        MenuAction::SwapWithFocused(pane) => swap_with_focused(chrome, *pane),
        MenuAction::ClearPaneName(pane) => {
            if let Some(pane) = workspace.panes.get_mut(pane) {
                pane.label = None;
            }
        }
        MenuAction::TogglePassthrough(pane) => {
            if let Some(pane) = workspace.panes.get_mut(pane) {
                pane.right_click_passthrough = !pane.right_click_passthrough;
            }
        }
        MenuAction::ToggleGroup(project_id) => {
            let folded = &mut chrome.sidebar.collapsed_projects;
            if !folded.remove(project_id) {
                folded.insert(project_id.clone());
            }
        }
        _ => return false,
    }
    true
}

/// Exchange `pane`'s slot with the focused slot of the active tab.
fn swap_with_focused(chrome: &mut Chrome, pane: PaneId) {
    if let Some(tab) = chrome.active_tab_mut() {
        let focused = tab.layout.focused();
        if let Some(slot) = tab.slot_for(pane).filter(|slot| *slot != focused) {
            tab.layout.swap_panes(focused, slot);
        }
    }
}

/// herdr `ContextMenu::items` for a pane, in its order: rename, clear the
/// label it has, swap with the focused pane when it is another pane, the
/// splits, zoom or unzoom by the active tab, the lease item for its control
/// state, respond while an attention entry blocks on it, copy mode, the
/// right-click passthrough flip, close.
fn pane_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, pane: PaneId) -> Vec<MenuItem> {
    let state = ws.pane(pane);
    let zoomed = chrome.active_tab().is_some_and(|tab| tab.zoomed);
    let mut items = vec![item("rename pane", MenuAction::Act(Action::RenamePane))];
    if state.label.is_some() {
        items.push(item("clear pane name", MenuAction::ClearPaneName(pane)));
    }
    if chrome.focused_pane() != Some(pane) {
        items.push(item(
            "swap with focused pane",
            MenuAction::SwapWithFocused(pane),
        ));
    }
    items.extend([
        item("split right", MenuAction::Act(Action::SplitVertical)),
        item("split down", MenuAction::Act(Action::SplitHorizontal)),
        item(
            if zoomed { "unzoom" } else { "zoom" },
            MenuAction::Act(Action::Zoom),
        ),
        control_item(state),
    ]);
    if let Some(entry_id) = blocked_entry(ws, pane) {
        items.push(item("respond", MenuAction::Respond(entry_id)));
    }
    let passthrough = if state.right_click_passthrough {
        "use gclient menu"
    } else {
        "send right-clicks to pane"
    };
    items.extend([
        item("copy mode", MenuAction::Act(Action::CopyMode)),
        item(passthrough, MenuAction::TogglePassthrough(pane)),
        item("close pane", MenuAction::Act(Action::ClosePane)),
    ]);
    items
}

fn tab_items() -> Vec<MenuItem> {
    vec![
        item("new tab", MenuAction::Act(Action::NewTab)),
        item("rename tab", MenuAction::Act(Action::RenameTab)),
        item("close tab", MenuAction::Act(Action::CloseTab)),
    ]
}

fn global_items() -> Vec<MenuItem> {
    vec![
        item("new terminal", MenuAction::Act(Action::NewTerminal)),
        item("new tab", MenuAction::Act(Action::NewTab)),
        item("settings", MenuAction::Act(Action::Settings)),
        item("keybinding help", MenuAction::Act(Action::Help)),
        item("reload config", MenuAction::Act(Action::ReloadConfig)),
        item("toggle sidebar", MenuAction::Act(Action::ToggleSidebar)),
        item("detach", MenuAction::Act(Action::Detach)),
    ]
}

fn item(label: &'static str, action: MenuAction) -> MenuItem {
    MenuItem {
        label,
        action,
        enabled: true,
    }
}

fn control_item(state: &crate::app::Pane) -> MenuItem {
    match state.control {
        ControlState::Held => item("release control", MenuAction::Act(Action::ReleaseControl)),
        _ if state.take_back => item("take control", MenuAction::Act(Action::TakeBack)),
        _ => item("take control", MenuAction::Act(Action::TakeControl)),
    }
}

fn blocked_entry<W: WorkspaceView>(ws: &W, pane: PaneId) -> Option<String> {
    ws.attention_entry_ids()
        .into_iter()
        .find(|entry| attention_pane(ws, entry) == Some(pane))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;

    fn labels(state: &ContextMenuState) -> Vec<&'static str> {
        state.items.iter().map(|item| item.label).collect()
    }

    #[test]
    fn menus_list_items_per_target_and_state() {
        let mut ws = Workspace::scripted();
        let alpha = ws
            .open_terminal("term-alpha", "native", "epoch")
            .expect("open term-alpha");
        let beta = ws
            .open_terminal("term-beta", "native", "epoch")
            .expect("open term-beta");
        let mut chrome = Chrome::dark();
        chrome.open_pane(alpha, "alpha");
        chrome.open_pane(beta, "beta");
        chrome.compute_view(&ws, Rect::new(0, 0, 100, 30));
        let focused = chrome.focused_pane().expect("focused pane");
        let other = if focused == alpha { beta } else { alpha };

        // A plain, observed, unnamed pane that is not focused, in an
        // unzoomed tab.
        let menu = build_menu(&ws, &chrome, ContextMenuKind::Pane(other), (10, 5));
        assert_eq!(
            labels(&menu),
            [
                "rename pane",
                "swap with focused pane",
                "split right",
                "split down",
                "zoom",
                "take control",
                "copy mode",
                "send right-clicks to pane",
                "close pane",
            ]
        );
        assert_eq!(menu.items[1].action, MenuAction::SwapWithFocused(other));
        assert_eq!(menu.items[5].action, MenuAction::Act(Action::TakeControl));
        assert_eq!(menu.items[7].action, MenuAction::TogglePassthrough(other));
        assert_eq!(menu.items[8].action, MenuAction::Act(Action::ClosePane));
        assert_eq!(
            (menu.kind, menu.anchor, menu.selected),
            (ContextMenuKind::Pane(other), (10, 5), 0)
        );

        // The focused pane: held, named, blocked, zoomed and passing
        // right-clicks through.
        {
            let pane = ws.pane_mut(focused);
            pane.label = Some("build".to_string());
            pane.control = ControlState::Held;
            pane.right_click_passthrough = true;
        }
        let entry_id = format!("run:{}", ws.pane(focused).terminal_id);
        ws.attention.entries.push(crate::daemon::RosterEntry {
            entry_id: entry_id.clone(),
            ..Default::default()
        });
        chrome.active_tab_mut().expect("active tab").zoomed = true;
        let menu = build_menu(&ws, &chrome, ContextMenuKind::Pane(focused), (10, 5));
        assert_eq!(
            labels(&menu),
            [
                "rename pane",
                "clear pane name",
                "split right",
                "split down",
                "unzoom",
                "release control",
                "respond",
                "copy mode",
                "use gclient menu",
                "close pane",
            ]
        );
        assert_eq!(menu.items[1].action, MenuAction::ClearPaneName(focused));
        assert_eq!(
            menu.items[5].action,
            MenuAction::Act(Action::ReleaseControl)
        );
        assert_eq!(menu.items[6].action, MenuAction::Respond(entry_id));

        let menu = build_menu(&ws, &chrome, ContextMenuKind::Tab(0), (3, 0));
        assert_eq!(labels(&menu), ["new tab", "rename tab", "close tab"]);
        assert_eq!(menu.items[2].action, MenuAction::Act(Action::CloseTab));

        let menu = build_menu(&ws, &chrome, ContextMenuKind::Global, (40, 12));
        assert_eq!(
            labels(&menu),
            [
                "new terminal",
                "new tab",
                "settings",
                "keybinding help",
                "reload config",
                "toggle sidebar",
                "detach",
            ]
        );
        assert_eq!(menu.items[4].action, MenuAction::Act(Action::ReloadConfig));
        assert!(menu.items.iter().all(|item| item.enabled));

        // Rows sit one cell inside the popup at the anchor: `keybinding help`
        // makes it 19 wide, seven items make it 9 tall.
        assert_eq!(
            menu_rect(menu.anchor, &menu.items),
            Rect::new(40, 12, 19, 9)
        );
        assert_eq!(menu.item_rects.len(), 7);
        assert_eq!(menu.item_rects[0], Rect::new(41, 13, 17, 1));
        assert_eq!(menu_hit(&menu, 41, 13), Some(0));
        assert_eq!(menu_hit(&menu, 57, 15), Some(2));
        assert_eq!(menu_hit(&menu, 40, 13), None, "the border is not a row");
        assert_eq!(menu_hit(&menu, 45, 20), None, "below the last row");
        let short = [item("zoom", MenuAction::Act(Action::Zoom))];
        assert_eq!(
            menu_rect((0, 0), &short).width,
            MENU_MIN_WIDTH,
            "short menus keep the floor width"
        );
    }
}
