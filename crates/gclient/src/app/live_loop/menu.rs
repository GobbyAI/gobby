//! Context menu state and item lists.
//!
//! A right-click on a pane, a tab, a sidebar row (project card, worktree
//! row, agent row) or empty chrome opens a menu; the projects footer's
//! `menu` opens the global one. This module is pure: [`build_menu`] reads the
//! workspace and chrome to decide which items apply, [`menu_hit`] maps a
//! screen cell to an item, and the routers in `mouse` and `modal_input` own
//! the open, select, activate and close transitions. The loops dispatch the
//! chosen [`MenuAction`]; the items both loops apply the same way live in
//! [`apply_local_menu_action`].

use ratatui::layout::{Margin, Position, Rect};

use crate::app::{ControlState, PaneId};
use crate::daemon::Daemon;
use crate::ui::chrome::attention_pane;
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
    let control = pane.map_or_else(
        || item("take control", MenuAction::Act(Action::TakeControl)),
        |pane| control_item(ws.pane(pane)),
    );
    // An orphaned row has no host to close; the daemon can only destroy it.
    let close = match orphaned_terminal(ws, entry_id) {
        Some(terminal_id) => item(
            "destroy orphaned terminal",
            MenuAction::DestroyTerminal(terminal_id),
        ),
        None => enabled_if(
            item("close terminal", MenuAction::Act(Action::CloseTerminal)),
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

fn global_items() -> Vec<MenuItem> {
    vec![
        item("new terminal", MenuAction::Act(Action::NewTerminal)),
        item("new tab", MenuAction::Act(Action::NewTab)),
        item("new project", MenuAction::Act(Action::NewProject)),
        item("settings", MenuAction::Act(Action::Settings)),
        item("keybinding help", MenuAction::Act(Action::Help)),
        item("reload config", MenuAction::Act(Action::ReloadConfig)),
        item("toggle sidebar", MenuAction::Act(Action::ToggleSidebar)),
        // Always enabled: the candidates are fetched on activation, and an
        // empty result reports itself in the status line.
        item("destroy orphaned terminals…", MenuAction::DestroyOrphans),
        item("detach", MenuAction::Act(Action::Detach)),
        // The chord is prefix+shift+q; the menu is where a new user finds it.
        item("quit", MenuAction::Act(Action::Quit)),
    ]
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
                "new project",
                "settings",
                "keybinding help",
                "reload config",
                "toggle sidebar",
                "destroy orphaned terminals…",
                "detach",
                "quit",
            ]
        );
        assert_eq!(menu.items[2].action, MenuAction::Act(Action::NewProject));
        assert_eq!(menu.items[5].action, MenuAction::Act(Action::ReloadConfig));
        assert_eq!(menu.items[9].action, MenuAction::Act(Action::Quit));
        assert!(menu.items.iter().all(|item| item.enabled));

        // Rows sit one cell inside the popup at the anchor: `destroy orphaned
        // terminals…` makes it 31 wide, ten items make it 12 tall.
        assert_eq!(
            menu_rect(menu.anchor, &menu.items),
            Rect::new(40, 12, 31, 12)
        );
        assert_eq!(menu.item_rects.len(), 10);
        assert_eq!(menu.item_rects[0], Rect::new(41, 13, 29, 1));
        assert_eq!(menu_hit(&menu, 41, 13), Some(0));
        assert_eq!(menu_hit(&menu, 57, 15), Some(2));
        assert_eq!(menu_hit(&menu, 40, 13), None, "the border is not a row");
        assert_eq!(menu_hit(&menu, 45, 22), Some(9), "the last row");
        assert_eq!(menu_hit(&menu, 45, 23), None, "below the last row");
        let short = [item("zoom", MenuAction::Act(Action::Zoom))];
        assert_eq!(
            menu_rect((0, 0), &short).width,
            MENU_MIN_WIDTH,
            "short menus keep the floor width"
        );
    }

    #[test]
    fn global_menu_offers_destroy_orphaned_terminals() {
        let ws = Workspace::scripted();
        let chrome = Chrome::dark();
        let menu = build_menu(&ws, &chrome, ContextMenuKind::Global, (0, 0));
        let item = menu
            .items
            .iter()
            .find(|item| item.action == MenuAction::DestroyOrphans)
            .expect("destroy orphans item");
        assert_eq!(item.label, "destroy orphaned terminals…");
        assert!(item.enabled, "enabled without knowing the candidates");
    }

    #[test]
    fn row_menu_labels_orphaned_rows() {
        use serde_json::json;

        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_roster(json!({
            "epoch": "attention-1",
            "seq": 1,
            "entries": [
                {
                    "entry_id": "run:term-orphan",
                    "terminal": {
                        "terminal_id": "term-orphan",
                        "backend": "native",
                        "state": "orphaned",
                    },
                },
                {
                    "entry_id": "run:term-live",
                    "terminal": {
                        "terminal_id": "term-live",
                        "backend": "native",
                        "state": "live",
                    },
                },
            ],
        }));
        ws.reconcile_subscribe_first().expect("scripted roster");
        let chrome = Chrome::dark();

        let menu = build_menu(
            &ws,
            &chrome,
            ContextMenuKind::Agent("run:term-orphan".to_string()),
            (3, 20),
        );
        assert_eq!(
            labels(&menu),
            [
                "focus",
                "open in new tab",
                "mark seen",
                "take control",
                "destroy orphaned terminal",
            ]
        );
        let enabled: Vec<bool> = menu.items.iter().map(|item| item.enabled).collect();
        assert_eq!(enabled, [true, true, false, false, true]);
        assert_eq!(
            menu.items[4].action,
            MenuAction::DestroyTerminal("term-orphan".to_string())
        );

        let menu = build_menu(
            &ws,
            &chrome,
            ContextMenuKind::Agent("run:term-live".to_string()),
            (3, 22),
        );
        assert_eq!(labels(&menu)[4], "close terminal");
        assert!(!menu.items[4].enabled, "no pane to close");
    }

    /// 5.3.1: the sidebar rows' menus. A git project with worktree children
    /// lists the worktree items and the fold item by its collapsed state, a
    /// plain project only rename and close; a worktree row's rename and
    /// close act on the tab tagged with it and are disabled until one is; an
    /// agent row lists respond while blocked, enables mark seen only while
    /// the roster carries an attention id, takes the lease item from its
    /// pane's control state, and disables the pane items without a pane.
    #[test]
    fn row_menus_list_items_per_target_and_state() {
        use crate::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
        use serde_json::json;

        let mut ws = Workspace::scripted();
        let blocked = ws
            .open_terminal("term-blocked", "native", "epoch")
            .expect("open term-blocked");
        let held = ws
            .open_terminal("term-held", "native", "epoch")
            .expect("open term-held");
        let project = |id: &str, name: &str| ProjectRow {
            id: id.to_string(),
            name: name.to_string(),
            display_name: name.to_string(),
            checkout: Some(Checkout {
                machine_id: "local".to_string(),
                root_path: format!("/repos/{name}"),
            }),
            ..ProjectRow::default()
        };
        ws.daemon_mut().set_sidebar_rows(SidebarRows {
            projects: vec![project("proj-git", "git"), project("proj-plain", "plain")],
            statuses: [(
                "proj-git".to_string(),
                SourceStatus {
                    current_branch: Some("main".to_string()),
                    ..SourceStatus::default()
                },
            )]
            .into_iter()
            .collect(),
            worktrees: vec![WorktreeRow {
                id: "wt-1".to_string(),
                project_id: "proj-git".to_string(),
                branch_name: "feature".to_string(),
                worktree_path: "/repos/git/.worktrees/feature".to_string(),
                status: "active".to_string(),
                workspace_role: "client".to_string(),
                ..WorktreeRow::default()
            }],
            ..SidebarRows::default()
        });
        ws.daemon_mut().set_roster(json!({
            "epoch": "attention-1",
            "seq": 1,
            "entries": [
                {
                    "entry_id": "run:term-blocked",
                    "terminal": {"terminal_id": "term-blocked", "backend": "native"},
                    "attention": {"attention_id": "att-1", "kind": "actionable"},
                },
                {
                    "entry_id": "run:term-held",
                    "terminal": {"terminal_id": "term-held", "backend": "native"},
                },
                {
                    "entry_id": "run:term-away",
                    "terminal": {"terminal_id": "term-away", "backend": "native"},
                },
            ],
        }));
        ws.select_project("proj-git");
        ws.reconcile_subscribe_first().expect("scripted roster");
        ws.pane_mut(held).control = ControlState::Held;
        let mut chrome = Chrome::dark();
        chrome.open_pane(blocked, "blocked");
        chrome.open_pane(held, "held");
        let enabled = |menu: &ContextMenuState| -> Vec<bool> {
            menu.items.iter().map(|item| item.enabled).collect()
        };
        let git = || ContextMenuKind::Project("proj-git".to_string());

        // The git project with a child, folded then expanded; the plain one.
        let menu = build_menu(&ws, &chrome, git(), (2, 3));
        assert_eq!(
            labels(&menu),
            [
                "rename",
                "close",
                "new worktree",
                "open worktree…",
                "expand"
            ]
        );
        assert_eq!(
            menu.items[0].action,
            MenuAction::RenameProject("proj-git".to_string())
        );
        assert_eq!(
            menu.items[1].action,
            MenuAction::CloseProject("proj-git".to_string())
        );
        assert_eq!(
            menu.items[2].action,
            MenuAction::NewWorktree("proj-git".to_string())
        );
        assert_eq!(
            menu.items[3].action,
            MenuAction::OpenWorktree("proj-git".to_string())
        );
        assert_eq!(
            menu.items[4].action,
            MenuAction::ToggleGroup("proj-git".to_string())
        );
        assert!(menu.items.iter().all(|item| item.enabled));
        assert_eq!((menu.kind, menu.anchor), (git(), (2, 3)));
        chrome.sidebar.toggle_group("proj-git");
        let menu = build_menu(&ws, &chrome, git(), (2, 3));
        assert_eq!(labels(&menu)[4], "collapse");
        let menu = build_menu(
            &ws,
            &chrome,
            ContextMenuKind::Project("proj-plain".to_string()),
            (2, 6),
        );
        assert_eq!(labels(&menu), ["rename", "close"]);
        assert_eq!(
            menu.items[1].action,
            MenuAction::CloseProject("proj-plain".to_string())
        );

        // The worktree row: rename and close wait for a tab tagged with it.
        let worktree = || ContextMenuKind::Worktree("wt-1".to_string());
        let menu = build_menu(&ws, &chrome, worktree(), (4, 4));
        assert_eq!(
            labels(&menu),
            ["rename", "close", "delete worktree checkout…"]
        );
        assert_eq!(enabled(&menu), [false, false, true]);
        assert_eq!(menu.items[0].action, MenuAction::Act(Action::RenameTab));
        assert_eq!(menu.items[1].action, MenuAction::Act(Action::CloseTab));
        assert_eq!(
            menu.items[2].action,
            MenuAction::RemoveWorktree("wt-1".to_string())
        );
        chrome.active_tab_mut().expect("active tab").worktree_id = Some("wt-1".to_string());
        let menu = build_menu(&ws, &chrome, worktree(), (4, 4));
        assert!(menu.items.iter().all(|item| item.enabled));

        // Agent rows: blocked and observed; plain and held; without a pane.
        let agent = |entry: &str| ContextMenuKind::Agent(entry.to_string());
        let menu = build_menu(&ws, &chrome, agent("run:term-blocked"), (3, 20));
        assert_eq!(
            labels(&menu),
            [
                "focus",
                "open in new tab",
                "respond",
                "mark seen",
                "take control",
                "close terminal",
            ]
        );
        assert_eq!(
            menu.items[0].action,
            MenuAction::FocusAgent("run:term-blocked".to_string())
        );
        assert_eq!(
            menu.items[1].action,
            MenuAction::OpenAgentInNewTab("run:term-blocked".to_string())
        );
        assert_eq!(
            menu.items[2].action,
            MenuAction::Respond("run:term-blocked".to_string())
        );
        assert_eq!(
            menu.items[3].action,
            MenuAction::MarkSeen("run:term-blocked".to_string())
        );
        assert_eq!(menu.items[4].action, MenuAction::Act(Action::TakeControl));
        assert_eq!(menu.items[5].action, MenuAction::Act(Action::CloseTerminal));
        assert!(menu.items.iter().all(|item| item.enabled));
        let menu = build_menu(&ws, &chrome, agent("run:term-held"), (3, 22));
        assert_eq!(
            labels(&menu),
            [
                "focus",
                "open in new tab",
                "mark seen",
                "release control",
                "close terminal",
            ]
        );
        assert_eq!(enabled(&menu), [true, true, false, true, true]);
        assert_eq!(
            menu.items[3].action,
            MenuAction::Act(Action::ReleaseControl)
        );
        let menu = build_menu(&ws, &chrome, agent("run:term-away"), (3, 24));
        assert_eq!(
            labels(&menu),
            [
                "focus",
                "open in new tab",
                "mark seen",
                "take control",
                "close terminal",
            ]
        );
        assert_eq!(enabled(&menu), [true, true, false, false, false]);
    }
}
