//! Context menu state and item lists.
//!
//! A right-click on a pane, a tab, a sidebar row (project card, worktree
//! row, agent row) or empty chrome opens a menu, the global one for empty
//! chrome; the sessions band's `[view]` and each menu bar title open their
//! own. This
//! module is pure: [`build_menu`] reads the
//! workspace and chrome to decide which items apply, [`menu_hit`] maps a
//! screen cell to an item, and the routers in `mouse` and `modal_input` own
//! the open, select, activate and close transitions. The loops dispatch the
//! chosen [`MenuAction`]; the items both loops apply the same way live in
//! [`apply_local_menu_action`].

use ratatui::layout::{Margin, Position, Rect};

use crate::app::{ControlState, PaneId};
use crate::daemon::Daemon;
use crate::ui::chrome::attention_pane;
use crate::ui::menu_bar::MenuBarMenu;
use crate::ui::settings::AgentSort;
use crate::ui::sidebar::agent_blocked;
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
    /// The sessions band's `[view]` control.
    SessionsView,
    Global,
    /// A menu bar title.
    MenuBar(MenuBarMenu),
}

/// What an item does when activated.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MenuAction {
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
        ContextMenuKind::SessionsView => sessions_view_items(chrome),
        ContextMenuKind::MenuBar(menu) => menu_bar_items(ws, chrome, *menu),
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
        MenuAction::ToggleGroup(project_id) => chrome.sidebar.toggle_group(project_id),
        _ => return false,
    }
    true
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

/// herdr `ContextMenu::items` for a pane, in its order: rename, clear the
/// label it has, swap with the focused pane when it is another pane, the
/// splits, zoom or unzoom by the active tab, the lease item for its control
/// state, respond while an attention entry blocks on it, copy mode, the
/// right-click passthrough flip, close.
fn pane_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, pane: PaneId) -> Vec<MenuItem> {
    let state = ws.pane(pane);
    let zoomed = chrome.is_zoomed();
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
        control_item(state, pane),
    ]);
    if let Some(entry_id) = blocked_entry(ws, pane) {
        items.push(item("respond", MenuAction::Respond(entry_id)));
    }
    items.extend([
        item("copy mode", MenuAction::Act(Action::CopyMode)),
        item(
            passthrough_label(state),
            MenuAction::TogglePassthrough(pane),
        ),
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

/// herdr `ContextMenu::items` for a workspace card: rename and close for
/// every project, the worktree items when the daemon reports its branch (a
/// git checkout), and the fold item by its collapsed state when it has
/// worktree rows.
fn project_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, project_id: &str) -> Vec<MenuItem> {
    let id = || project_id.to_owned();
    let mut items = vec![
        item("rename", MenuAction::RenameProject(id())),
        item("close", MenuAction::CloseProject(id())),
    ];
    let Some(project) = ws
        .sidebar()
        .projects
        .iter()
        .find(|project| project.project_id == project_id)
    else {
        return items;
    };
    if project.branch.is_some() {
        items.push(item("new worktree", MenuAction::NewWorktree(id())));
        items.push(item("open worktree…", MenuAction::OpenWorktree(id())));
    }
    if !project.worktrees.is_empty() {
        let folded = !chrome.sidebar.is_expanded(project_id);
        items.push(item(
            if folded { "expand" } else { "collapse" },
            MenuAction::ToggleGroup(id()),
        ));
    }
    items
}

/// A worktree row: rename and close are the tab items for the tab opened
/// from it (`focus_menu_target` activates that tab first) and wait until
/// one is open; delete asks the daemon to remove the checkout.
fn worktree_items(chrome: &Chrome, worktree_id: &str) -> Vec<MenuItem> {
    let shown = chrome
        .project_tabs
        .sets
        .values()
        .flat_map(|set| set.tabs.iter())
        .any(|tab| tab.worktree_id.as_deref() == Some(worktree_id));
    vec![
        enabled_if(item("rename", MenuAction::Act(Action::RenameTab)), shown),
        enabled_if(item("close", MenuAction::Act(Action::CloseTab)), shown),
        item(
            "delete worktree checkout…",
            MenuAction::RemoveWorktree(worktree_id.to_owned()),
        ),
    ]
}

/// An agent row: reveal its pane in place or in a fresh tab, respond while
/// the entry blocks, mark the prompt seen while the roster carries its
/// attention id, then the pane's lease item and close, both idle until the
/// entry's terminal is a pane of this workspace.
fn agent_items<W: WorkspaceView>(ws: &W, entry_id: &str) -> Vec<MenuItem> {
    let id = || entry_id.to_owned();
    let pane = attention_pane(ws, entry_id);
    let mut items = vec![
        item("focus", MenuAction::FocusAgent(id())),
        item("open in new tab", MenuAction::OpenAgentInNewTab(id())),
    ];
    if agent_blocked(ws, entry_id) {
        items.push(item("respond", MenuAction::Respond(id())));
    }
    items.push(enabled_if(
        item("mark seen", MenuAction::MarkSeen(id())),
        attention_id(ws, entry_id).is_some(),
    ));
    // Disabled no-pane rows never dispatch either `Act` placeholder.
    let control = pane.map_or_else(
        || item("take control", MenuAction::Act(Action::TakeControl)),
        |pane| control_item(ws.pane(pane), pane),
    );
    // An orphaned row has no host to close; the daemon can only destroy it.
    let close = match orphaned_terminal(ws, entry_id) {
        Some(terminal_id) => item(
            "destroy orphaned terminal",
            MenuAction::DestroyTerminal(terminal_id),
        ),
        None => enabled_if(
            item(
                "close terminal",
                pane.map_or(
                    MenuAction::Act(Action::CloseTerminal),
                    MenuAction::CloseTerminal,
                ),
            ),
            pane.is_some(),
        ),
    };
    items.extend([enabled_if(control, pane.is_some()), close]);
    items
}

/// The entry's terminal id when the daemon reports the row `orphaned`.
fn orphaned_terminal<W: WorkspaceView>(ws: &W, entry_id: &str) -> Option<String> {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .filter(|agent| agent.terminal_state.as_deref() == Some("orphaned"))
        .map(|agent| agent.terminal_id.clone())
}

/// The attention id the roster carries for `entry_id`, while it blocks.
pub fn attention_id<W: WorkspaceView>(ws: &W, entry_id: &str) -> Option<String> {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)?
        .attention
        .as_ref()?
        .attention_id
        .clone()
}

/// The sessions band's `[view]` menu: the scope, then the order, one pair
/// each. The rows below the band show the view they are in, so the menu
/// marks which value is in force rather than naming the next one.
fn sessions_view_items(chrome: &Chrome) -> Vec<MenuItem> {
    let all = chrome.sidebar.all_sessions;
    let priority = chrome.prefs.agent_sort == AgentSort::Priority;
    let scope = MenuAction::Act(Action::ToggleSessionsScope);
    let sort = MenuAction::Act(Action::ToggleAgentSort);
    vec![
        choice(("✓ this project", "  this project"), !all, scope.clone()),
        choice(("✓ all projects", "  all projects"), all, scope),
        choice(("✓ grouped", "  grouped"), !priority, sort.clone()),
        choice(("✓ priority", "  priority"), priority, sort),
    ]
}

/// One choice of a pair, `(marked, plain)` spellings against one margin.
/// The value in force is marked and disabled, so choosing what is already
/// chosen closes the menu and changes nothing; the other choice carries the
/// toggle its chord runs.
fn choice(labels: (&'static str, &'static str), active: bool, action: MenuAction) -> MenuItem {
    let (marked, plain) = labels;
    enabled_if(item(if active { marked } else { plain }, action), !active)
}

fn global_items() -> Vec<MenuItem> {
    vec![
        item("new terminal", MenuAction::Act(Action::NewTerminal)),
        item("new tab", MenuAction::Act(Action::NewTab)),
        item("new project", MenuAction::Act(Action::NewProject)),
        item("settings", MenuAction::Act(Action::Settings)),
        item("keybinding help", MenuAction::Act(Action::Help)),
        item("alerts…", MenuAction::ShowAlerts),
        item("reload config", MenuAction::Act(Action::ReloadConfig)),
        item("toggle sidebar", MenuAction::Act(Action::ToggleSidebar)),
        // Always enabled: the candidates are fetched on activation, and an
        // empty result reports itself in a toast.
        item("destroy orphaned terminals…", MenuAction::DestroyOrphans),
        item("detach", MenuAction::Act(Action::Detach)),
        // The chord is prefix+shift+q; the menu is where a new user finds it.
        item("quit", MenuAction::Act(Action::Quit)),
    ]
}

/// A menu bar title's items: the band menus' items regrouped under the
/// title that names them. The Agent menu's items are section 3.11's.
fn menu_bar_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, menu: MenuBarMenu) -> Vec<MenuItem> {
    let act = |label: &'static str, action: Action| item(label, MenuAction::Act(action));
    match menu {
        MenuBarMenu::Gobby => vec![
            item("alerts…", MenuAction::ShowAlerts),
            act("settings", Action::Settings),
        ],
        MenuBarMenu::File => vec![
            act("new project", Action::NewProject),
            act("new tab", Action::NewTab),
            act("new pane", Action::NewTerminal),
        ],
        MenuBarMenu::Edit => edit_items(ws, chrome),
        MenuBarMenu::View => sessions_view_items(chrome),
        MenuBarMenu::Window => vec![
            act("next tab", Action::NextTab),
            act("previous tab", Action::PreviousTab),
            act("next pane", Action::CyclePaneNext),
            act("previous pane", Action::CyclePanePrevious),
        ],
        MenuBarMenu::Agent => Vec::new(),
        MenuBarMenu::Help => vec![act("keys", Action::Help)],
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

fn passthrough_label(state: &crate::app::Pane) -> &'static str {
    if state.right_click_passthrough {
        "use gclient menu"
    } else {
        "send right-clicks to pane"
    }
}

fn item(label: &'static str, action: MenuAction) -> MenuItem {
    MenuItem {
        label,
        action,
        enabled: true,
    }
}

fn enabled_if(item: MenuItem, enabled: bool) -> MenuItem {
    MenuItem { enabled, ..item }
}

fn control_item(state: &crate::app::Pane, pane: PaneId) -> MenuItem {
    match state.control {
        ControlState::Held => item("release control", MenuAction::ReleaseControl(pane)),
        _ => item("take control", MenuAction::TakeControl(pane)),
    }
}

fn blocked_entry<W: WorkspaceView>(ws: &W, pane: PaneId) -> Option<String> {
    ws.attention_entry_ids()
        .into_iter()
        .find(|entry| attention_pane(ws, entry) == Some(pane))
}

#[cfg(test)]
#[path = "menu/tests.rs"]
mod tests;
