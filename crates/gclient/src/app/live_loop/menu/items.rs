//! Context-menu item definitions.

use crate::app::{ControlState, PaneId};
use crate::theme::ThemeName;
use crate::ui::chrome::attention_pane;
use crate::ui::hit::SidebarSection;
use crate::ui::settings::AgentSort;
use crate::ui::sidebar::{agent_blocked, ALL_MACHINES};
use crate::ui::{Action, Chrome, WorkspaceView};

use super::{ArrangeLayout, ArrangeTarget, MenuAction, MenuItem, Submenu};

/// The Arrange ▸ row, opening the layout choices for `target`; disabled
/// when there is no tab to arrange.
pub(in crate::app::live_loop) fn arrange_row(target: Option<ArrangeTarget>) -> MenuItem {
    let enabled = target.is_some();
    let target = target.unwrap_or(ArrangeTarget {
        tab: String::new(),
        pane: None,
    });
    enabled_if(
        item(
            "Arrange ▸",
            MenuAction::OpenSubmenu(Submenu::Arrange(target)),
        ),
        enabled,
    )
}

fn arrange_items(target: &ArrangeTarget) -> Vec<MenuItem> {
    let arrange = |label, layout| {
        item(
            label,
            MenuAction::Arrange {
                layout,
                target: target.clone(),
            },
        )
    };
    vec![
        arrange("Even horizontal", ArrangeLayout::EvenHorizontal),
        arrange("Even vertical", ArrangeLayout::EvenVertical),
        arrange("Main horizontal", ArrangeLayout::MainHorizontal),
        arrange("Main vertical", ArrangeLayout::MainVertical),
        arrange("Tiled", ArrangeLayout::Tiled),
        item("New grid…", MenuAction::OpenNewGrid),
    ]
}

/// herdr `ContextMenu::items` for a pane, in its order: rename, clear the
/// label it has, swap with the focused pane when it is another pane, the
/// splits, zoom or unzoom by the active tab, the lease item for its control
/// state, respond while an attention entry blocks on it, copy mode, the
/// right-click passthrough flip, close.
pub(super) fn pane_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, pane: PaneId) -> Vec<MenuItem> {
    let state = ws.pane(pane);
    let zoomed = chrome.is_zoomed();
    let mut items = vec![item("Rename pane", MenuAction::Act(Action::RenamePane))];
    if state.label.is_some() {
        items.push(item("Clear pane name", MenuAction::ClearPaneName(pane)));
    }
    if chrome.focused_pane() != Some(pane) {
        items.push(item(
            "Swap with focused pane",
            MenuAction::SwapWithFocused(pane),
        ));
    }
    items.extend([
        item("Split right", MenuAction::Act(Action::SplitVertical)),
        item("Split down", MenuAction::Act(Action::SplitHorizontal)),
        item(
            if zoomed { "Unzoom" } else { "Zoom" },
            MenuAction::Act(Action::Zoom),
        ),
    ]);
    let tab = chrome
        .tabs()
        .tabs
        .iter()
        .find(|tab| tab.slot_for(pane).is_some());
    items.push(arrange_row(tab.map(|tab| ArrangeTarget {
        tab: tab.id.clone(),
        pane: Some(pane),
    })));
    items.push(control_item(state, pane));
    if let Some(entry_id) = blocked_entry(ws, pane) {
        items.push(item("Respond", MenuAction::Respond(entry_id)));
    }
    items.extend([
        item("Copy mode", MenuAction::Act(Action::CopyMode)),
        item(
            passthrough_label(state),
            MenuAction::TogglePassthrough(pane),
        ),
        item("Close pane", MenuAction::Act(Action::ClosePane)),
    ]);
    items
}

pub(super) fn tab_items(chrome: &Chrome, index: usize) -> Vec<MenuItem> {
    let tab = chrome.tabs().tabs.get(index);
    vec![
        item("New tab", MenuAction::Act(Action::NewTab)),
        item("Rename tab", MenuAction::Act(Action::RenameTab)),
        arrange_row(tab.map(|tab| ArrangeTarget {
            tab: tab.id.clone(),
            pane: None,
        })),
        item("Close tab", MenuAction::Act(Action::CloseTab)),
    ]
}

/// herdr `ContextMenu::items` for a workspace card: rename and close for
/// every project, the worktree items when the daemon reports its branch (a
/// git checkout), and the fold item by its collapsed state when it has
/// worktree rows.
pub(super) fn project_items<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    project_id: &str,
) -> Vec<MenuItem> {
    let id = || project_id.to_owned();
    let mut items = vec![
        item("Rename", MenuAction::RenameProject(id())),
        item("Close", MenuAction::CloseProject(id())),
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
        items.push(item("New worktree", MenuAction::NewWorktree(id())));
        items.push(item("Open worktree…", MenuAction::OpenWorktree(id())));
    }
    if !project.worktrees.is_empty() {
        let folded = !chrome.sidebar.is_expanded(project_id);
        items.push(item(
            if folded { "Expand" } else { "Collapse" },
            MenuAction::ToggleGroup(id()),
        ));
    }
    items
}

/// A worktree row: rename and close are the tab items for the tab opened
/// from it (`focus_menu_target` activates that tab first) and wait until
/// one is open; delete asks the daemon to remove the checkout.
pub(super) fn worktree_items(chrome: &Chrome, worktree_id: &str) -> Vec<MenuItem> {
    let shown = chrome
        .project_tabs
        .sets
        .values()
        .flat_map(|set| set.tabs.iter())
        .any(|tab| tab.worktree_id.as_deref() == Some(worktree_id));
    vec![
        enabled_if(item("Rename", MenuAction::Act(Action::RenameTab)), shown),
        enabled_if(item("Close", MenuAction::Act(Action::CloseTab)), shown),
        item(
            "Delete worktree checkout…",
            MenuAction::RemoveWorktree(worktree_id.to_owned()),
        ),
    ]
}

/// An agent row: reveal its pane in place or in a fresh tab, respond while
/// the entry blocks, mark the prompt seen while the roster carries its
/// attention id, then the pane's lease item and close, both idle until the
/// entry's terminal is a pane of this workspace.
pub(super) fn agent_items<W: WorkspaceView>(ws: &W, entry_id: &str) -> Vec<MenuItem> {
    let id = || entry_id.to_owned();
    let pane = attention_pane(ws, entry_id);
    let mut items = vec![
        item("Focus", MenuAction::FocusAgent(id())),
        item("Open in new tab", MenuAction::OpenAgentInNewTab(id())),
    ];
    if agent_blocked(ws, entry_id) {
        items.push(item("Respond", MenuAction::Respond(id())));
    }
    items.push(enabled_if(
        item("Mark seen", MenuAction::MarkSeen(id())),
        attention_id(ws, entry_id).is_some(),
    ));
    // Disabled no-pane rows never dispatch either `Act` placeholder.
    let control = pane.map_or_else(
        || item("Take control", MenuAction::Act(Action::TakeControl)),
        |pane| control_item(ws.pane(pane), pane),
    );
    // An orphaned row has no host to close; the daemon can only destroy it.
    let close = match orphaned_terminal(ws, entry_id) {
        Some(terminal_id) => item(
            "Destroy orphaned terminal",
            MenuAction::DestroyTerminal(terminal_id),
        ),
        None => enabled_if(
            item(
                "Close terminal",
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

/// A submenu's items.
pub(super) fn submenu_items(chrome: &Chrome, submenu: &Submenu) -> Vec<MenuItem> {
    match submenu {
        Submenu::Arrange(target) => arrange_items(target),
        Submenu::Appearance => appearance_items(chrome),
        Submenu::Theme => theme_items(chrome),
        Submenu::Sidebar => sidebar_items(chrome),
        Submenu::Section(SidebarSection::Machines) => {
            let filter = chrome.sidebar.machine_filter.as_deref();
            vec![
                choice(
                    ("✓ This machine", "  This machine"),
                    filter.is_none(),
                    MenuAction::SetMachineScope(false),
                ),
                choice(
                    ("✓ All machines", "  All machines"),
                    filter == Some(ALL_MACHINES),
                    MenuAction::SetMachineScope(true),
                ),
            ]
        }
        Submenu::Section(SidebarSection::Projects) => {
            let all = chrome.sidebar.all_projects;
            let filter = MenuAction::Act(Action::ToggleProjectsFilter);
            vec![
                choice(
                    ("✓ Working projects", "  Working projects"),
                    !all,
                    filter.clone(),
                ),
                choice(("✓ All projects", "  All projects"), all, filter),
            ]
        }
        Submenu::Section(SidebarSection::Agents) => agents_view_items(chrome),
        Submenu::Section(SidebarSection::Terminals) => vec![
            item("New terminal", MenuAction::Act(Action::NewTerminal)),
            item("Destroy orphaned terminals…", MenuAction::DestroyOrphans),
        ],
    }
}

/// View › Sidebar: the column's visibility and pin, then one submenu per
/// section, in the sidebar's order.
fn sidebar_items(chrome: &Chrome) -> Vec<MenuItem> {
    let shown = chrome.sidebar.pinned || chrome.sidebar.overlay;
    let mut items = vec![
        toggle(
            ("✓ Show sidebar", "  Show sidebar"),
            shown,
            MenuAction::Act(Action::ToggleSidebar),
        ),
        toggle(
            ("✓ Pin sidebar", "  Pin sidebar"),
            chrome.sidebar.pinned,
            MenuAction::PinSidebar,
        ),
    ];
    items.extend(SidebarSection::ALL.into_iter().map(|section| {
        let label = match section {
            SidebarSection::Machines => "  Machines ▸",
            SidebarSection::Projects => "  Projects ▸",
            SidebarSection::Agents => "  Agents ▸",
            SidebarSection::Terminals => "  Terminals ▸",
        };
        item(label, MenuAction::OpenSubmenu(Submenu::Section(section)))
    }));
    items
}

/// The agents section's options: the scope, then the order, one pair each.
/// The rows show the view they are in, so the menu marks which value is in
/// force rather than naming the next one.
fn agents_view_items(chrome: &Chrome) -> Vec<MenuItem> {
    let all = chrome.sidebar.all_sessions;
    let priority = chrome.prefs.agent_sort == AgentSort::Priority;
    let scope = MenuAction::Act(Action::ToggleSessionsScope);
    let sort = MenuAction::Act(Action::ToggleAgentSort);
    vec![
        choice(("✓ This project", "  This project"), !all, scope.clone()),
        choice(("✓ All projects", "  All projects"), all, scope),
        choice(("✓ Grouped", "  Grouped"), !priority, sort.clone()),
        choice(("✓ Priority", "  Priority"), priority, sort),
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

/// An on/off row, `(marked, plain)` spellings against one margin. Unlike a
/// `choice` it stays enabled while on: choosing it turns it off.
pub(in crate::app::live_loop) fn toggle(
    labels: (&'static str, &'static str),
    on: bool,
    action: MenuAction,
) -> MenuItem {
    let (marked, plain) = labels;
    item(if on { marked } else { plain }, action)
}

/// Each appearance with its View row and its marked and plain choice.
const APPEARANCES: [(&str, &str, (&str, &str)); 3] = [
    ("dark", "  Appearance: Dark ▸", ("● Dark", "  Dark")),
    ("light", "  Appearance: Light ▸", ("● Light", "  Light")),
    ("system", "  Appearance: System ▸", ("● System", "  System")),
];

/// The appearance in force, read the way `ClientPrefs::theme_kind` reads
/// it: anything but light or system draws dark.
fn appearance_in_force(chrome: &Chrome) -> usize {
    APPEARANCES
        .iter()
        .position(|(value, ..)| value.eq_ignore_ascii_case(&chrome.prefs.theme))
        .unwrap_or(0)
}

/// The View menu's appearance row, naming the appearance in force.
pub(in crate::app::live_loop) fn appearance_row_label(chrome: &Chrome) -> &'static str {
    APPEARANCES[appearance_in_force(chrome)].1
}

/// Dark, Light and System; System follows the terminal's appearance.
pub(super) fn appearance_items(chrome: &Chrome) -> Vec<MenuItem> {
    let chosen = appearance_in_force(chrome);
    APPEARANCES
        .iter()
        .enumerate()
        .map(|(index, (value, _, labels))| {
            choice(*labels, index == chosen, MenuAction::SetAppearance(value))
        })
        .collect()
}

/// Each named theme, in `ThemeName::ALL` order, with its View row and its
/// marked and plain choice.
const THEMES: [(ThemeName, &str, (&str, &str)); ThemeName::ALL.len()] = [
    (
        ThemeName::Restored,
        "  Theme: Restored ▸",
        ("● Restored", "  Restored"),
    ),
    (ThemeName::Moss, "  Theme: Moss ▸", ("● Moss", "  Moss")),
    (
        ThemeName::YourProposal,
        "  Theme: Your proposal ▸",
        ("● Your proposal", "  Your proposal"),
    ),
    (
        ThemeName::Staircase,
        "  Theme: Staircase ▸",
        ("● Staircase", "  Staircase"),
    ),
    (
        ThemeName::InverseBar,
        "  Theme: Inverse bar ▸",
        ("● Inverse bar", "  Inverse bar"),
    ),
    (
        ThemeName::GobbyBar,
        "  Theme: Gobby bar ▸",
        ("● Gobby bar", "  Gobby bar"),
    ),
    (
        ThemeName::ContrastChrome,
        "  Theme: Contrast chrome ▸",
        ("● Contrast chrome", "  Contrast chrome"),
    ),
    (
        ThemeName::MossChrome,
        "  Theme: Moss chrome ▸",
        ("● Moss chrome", "  Moss chrome"),
    ),
    (
        ThemeName::MossBand,
        "  Theme: Moss band ▸",
        ("● Moss band", "  Moss band"),
    ),
    (ThemeName::Ink, "  Theme: Ink ▸", ("● Ink", "  Ink")),
    (
        ThemeName::HostMatched,
        "  Theme: Host-matched ▸",
        ("● Host-matched", "  Host-matched"),
    ),
];

/// The theme drawn, which is Restored where the saved one is not offered.
pub(in crate::app::live_loop) fn theme_row_label(chrome: &Chrome) -> &'static str {
    THEMES
        .iter()
        .find(|(name, ..)| *name == chrome.theme.name)
        .map_or(THEMES[0].1, |(_, row, _)| row)
}

/// Only the themes offered where the chrome draws now.
pub(super) fn theme_items(chrome: &Chrome) -> Vec<MenuItem> {
    let theme = &chrome.theme;
    THEMES
        .iter()
        .filter(|(name, ..)| name.offered(theme.kind, theme.hosted))
        .map(|(name, _, labels)| choice(*labels, *name == theme.name, MenuAction::SetTheme(*name)))
        .collect()
}

pub(super) fn global_items() -> Vec<MenuItem> {
    vec![
        item("New terminal", MenuAction::Act(Action::NewTerminal)),
        item("New tab", MenuAction::Act(Action::NewTab)),
        item("New project…", MenuAction::Act(Action::NewProject)),
        item("Open project…", MenuAction::Act(Action::OpenProject)),
        item("Settings", MenuAction::Act(Action::Settings)),
        item("Keybinding help", MenuAction::Act(Action::Help)),
        item("Reload config", MenuAction::Act(Action::ReloadConfig)),
        item("Toggle sidebar", MenuAction::Act(Action::ToggleSidebar)),
        // Always enabled: the candidates are fetched on activation, and an
        // empty result reports itself in a toast.
        item("Destroy orphaned terminals…", MenuAction::DestroyOrphans),
        item("Detach", MenuAction::Act(Action::Detach)),
        // The chord is prefix+shift+q; the menu is where a new user finds it.
        item("Quit", MenuAction::Act(Action::Quit)),
    ]
}

/// A menu bar title's items: the band menus' items regrouped under the
/// title that names them. The Agent menu's items are section 3.11's.
pub(in crate::app::live_loop) fn passthrough_label(state: &crate::app::Pane) -> &'static str {
    if state.right_click_passthrough {
        "Use gclient menu"
    } else {
        "Send right-clicks to pane"
    }
}

pub(in crate::app::live_loop) fn item(label: &'static str, action: MenuAction) -> MenuItem {
    MenuItem {
        label,
        action,
        enabled: true,
    }
}

pub(in crate::app::live_loop) fn enabled_if(item: MenuItem, enabled: bool) -> MenuItem {
    MenuItem { enabled, ..item }
}

fn control_item(state: &crate::app::Pane, pane: PaneId) -> MenuItem {
    match state.control {
        ControlState::Held => item("Release control", MenuAction::ReleaseControl(pane)),
        _ => item("Take control", MenuAction::TakeControl(pane)),
    }
}

pub(in crate::app::live_loop) fn blocked_entry<W: WorkspaceView>(
    ws: &W,
    pane: PaneId,
) -> Option<String> {
    ws.attention_entry_ids()
        .into_iter()
        .find(|entry| attention_pane(ws, entry) == Some(pane))
}
