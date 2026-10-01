//! Context menu state and item lists.
//!
//! A right-click on a pane, a tab, a sidebar row (project card, worktree
//! row, agent row) or empty chrome opens a menu, the global one for empty
//! chrome; each menu bar title opens its own, and a [`Submenu`] row opens
//! its menu beside it. This module is pure: [`build_menu`] reads the
//! workspace and chrome to decide which items apply, [`menu_hit`] maps a
//! screen cell to an item, and the routers in `mouse` and `modal_input` own
//! the open, select, activate and close transitions. The loops dispatch the
//! chosen [`MenuAction`]; the items both loops apply the same way live in
//! [`apply_local_menu_action`].

use ratatui::layout::{Margin, Position, Rect};

use crate::app::PaneId;
use crate::daemon::Daemon;
use crate::ui::hit::SidebarSection;
use crate::ui::menu_bar::MenuBarMenu;
use crate::ui::sidebar::ALL_MACHINES;
use crate::ui::{Action, Chrome, Mode, WorkspaceView};

use super::super::Workspace;
use super::actions::toggle_sidebar_pin;
use super::modal_input::persist_prefs;

mod items;
pub use items::attention_id;
use items::{
    agent_items, global_items, pane_items, project_items, submenu_items, tab_items, worktree_items,
};
pub(super) use items::{
    arrange_items, blocked_entry, enabled_if, item, passthrough_label, theme_row_label, toggle,
};

/// A menu that opens beside the row of its parent menu that names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Submenu {
    /// View › Theme: the theme choices.
    Theme,
    /// View › Sidebar: showing and pinning the sidebar, and one submenu per
    /// section.
    Sidebar,
    /// View › Sidebar › one section: only that section's options.
    Section(SidebarSection),
}

impl Submenu {
    /// The submenu holding this one's row; `None` for a row of the View
    /// menu itself.
    fn parent(self) -> Option<Self> {
        match self {
            Self::Section(_) => Some(Self::Sidebar),
            Self::Theme | Self::Sidebar => None,
        }
    }
}

/// What the menu was opened on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContextMenuKind {
    Pane(PaneId),
    Tab(usize),
    Project(String),
    Worktree(String),
    Agent(String),
    Global,
    /// A menu bar title.
    MenuBar(MenuBarMenu),
    /// A submenu, beside the row that opened it.
    Submenu(Submenu),
}

/// Layout choices prepared for Window › Arrange.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArrangeLayout {
    EvenHorizontal,
    EvenVertical,
    MainHorizontal,
    MainVertical,
    Tiled,
}

/// What an item does when activated.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MenuAction {
    Arrange(ArrangeLayout),
    OpenNewGrid,
    NewGrid {
        rows: u8,
        cols: u8,
    },
    ShowDaemon,
    ShowAbout,
    /// A keymap action, dispatched exactly as its chord would be once the
    /// menu's pane or tab is the focused one.
    Act(Action),
    /// Close the terminal belonging to this pane, independent of focus.
    CloseTerminal(PaneId),
    /// Take the control lease for this pane, independent of focus.
    TakeControl(PaneId),
    /// Release the control lease for this pane, independent of focus.
    ReleaseControl(PaneId),
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
    /// Pin the sidebar into the layout, or unpin it.
    PinSidebar,
    /// Open this submenu beside the row that names it.
    OpenSubmenu(Submenu),
    /// Save this theme preference (`dark`, `light` or `system`) and draw in it.
    SetTheme(&'static str),
    /// Flip the monochrome preference, save it and redraw in it.
    ToggleMonochrome,
    /// List the agents on every machine (`true`) or on this one.
    SetMachineScope(bool),
    /// Open the alert log.
    ShowAlerts,
    /// Open the destroy-orphaned-terminals dialog.
    DestroyOrphans,
    /// Destroy this orphaned terminal row without the dialog.
    DestroyTerminal(String),
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
    /// The menu this one opened from, drawn behind it and still live, with
    /// the row that opened it selected.
    pub parent: Option<Box<ContextMenuState>>,
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
        ContextMenuKind::Project(project_id) => project_items(ws, chrome, project_id),
        ContextMenuKind::Worktree(worktree_id) => worktree_items(chrome, worktree_id),
        ContextMenuKind::Agent(entry_id) => agent_items(ws, entry_id),
        ContextMenuKind::MenuBar(menu) => super::menu_bar::menu_bar_items(ws, chrome, *menu),
        ContextMenuKind::Submenu(submenu) => submenu_items(chrome, *submenu),
    };
    menu_state(kind, anchor, items)
}

fn menu_state(kind: ContextMenuKind, anchor: (u16, u16), items: Vec<MenuItem>) -> ContextMenuState {
    let item_rects = item_rects(menu_rect(anchor, &items), items.len());
    ContextMenuState {
        kind,
        anchor,
        items,
        selected: 0,
        item_rects,
        parent: None,
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
) -> bool
where
    Workspace<D>: WorkspaceView,
{
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
        MenuAction::ToggleGroup(project_id) => chrome.sidebar.toggle_group(project_id),
        MenuAction::PinSidebar => toggle_sidebar_pin(workspace.gobby_home(), chrome),
        MenuAction::OpenSubmenu(submenu) => {
            chrome.menu = Some(submenu_state(workspace, chrome, *submenu));
            chrome.mode = Mode::ContextMenu;
        }
        MenuAction::SetTheme(theme) => {
            chrome.prefs.theme = (*theme).to_owned();
            let kind = chrome.prefs.theme_kind();
            chrome.set_theme(kind);
            persist_prefs(workspace.gobby_home(), chrome);
        }
        MenuAction::ToggleMonochrome => {
            chrome.prefs.monochrome = !chrome.prefs.monochrome;
            chrome.set_theme(chrome.theme.kind);
            persist_prefs(workspace.gobby_home(), chrome);
        }
        MenuAction::SetMachineScope(all) => {
            chrome.sidebar.machine_filter = all.then(|| ALL_MACHINES.to_owned());
        }
        _ => return false,
    }
    true
}

/// `submenu` open beside the row that names it and level with it, with every
/// menu it cascades from open behind it, each with that row selected. The
/// outermost is the View menu, where it draws under its title.
fn submenu_state<W: WorkspaceView>(ws: &W, chrome: &Chrome, submenu: Submenu) -> ContextMenuState {
    let mut parent = match submenu.parent() {
        Some(outer) => submenu_state(ws, chrome, outer),
        None => {
            let view = MenuBarMenu::View;
            let anchor = chrome
                .view
                .menu_title_hit_areas
                .iter()
                .find(|(index, _)| MenuBarMenu::ALL.get(*index) == Some(&view))
                .map_or((0, 1), |(_, cell)| (cell.x, cell.bottom()));
            build_menu(ws, chrome, ContextMenuKind::MenuBar(view), anchor)
        }
    };
    let row = parent
        .items
        .iter()
        .position(|item| item.action == MenuAction::OpenSubmenu(submenu));
    let anchor = row
        .and_then(|index| parent.item_rects.get(index))
        .map_or(parent.anchor, |row| {
            (row.right().saturating_add(1), row.y.saturating_sub(1))
        });
    parent.selected = row.unwrap_or(parent.selected);
    let mut state = menu_state(
        ContextMenuKind::Submenu(submenu),
        anchor,
        submenu_items(chrome, submenu),
    );
    state.parent = Some(Box::new(parent));
    state
}

/// Exchange `pane`'s slot with the focused slot of the active tab.
fn swap_with_focused(chrome: &mut Chrome, pane: PaneId) {
    let Some(focused) = chrome.focus_slot() else {
        return;
    };
    if let Some(tab) = chrome.active_tab_mut() {
        if let Some(slot) = tab.slot_for(pane).filter(|slot| *slot != focused) {
            tab.layout.swap_panes(focused, slot);
        }
    }
}

#[cfg(test)]
#[path = "menu/tests.rs"]
mod tests;
