//! Context-menu item definitions.

use crate::app::{ControlState, PaneId};
use crate::ui::chrome::attention_pane;
use crate::ui::settings::AgentSort;
use crate::ui::sidebar::agent_blocked;
use crate::ui::{Action, Chrome, WorkspaceView};

use super::{MenuAction, MenuItem};

/// herdr `ContextMenu::items` for a pane, in its order: rename, clear the
/// label it has, swap with the focused pane when it is another pane, the
/// splits, zoom or unzoom by the active tab, the lease item for its control
/// state, respond while an attention entry blocks on it, copy mode, the
/// right-click passthrough flip, close.
pub(super) fn pane_items<W: WorkspaceView>(ws: &W, chrome: &Chrome, pane: PaneId) -> Vec<MenuItem> {
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

pub(super) fn tab_items() -> Vec<MenuItem> {
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
pub(super) fn project_items<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    project_id: &str,
) -> Vec<MenuItem> {
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
pub(super) fn worktree_items(chrome: &Chrome, worktree_id: &str) -> Vec<MenuItem> {
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
pub(super) fn agent_items<W: WorkspaceView>(ws: &W, entry_id: &str) -> Vec<MenuItem> {
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

/// The agents band's `[view]` menu: the scope, then the order, one pair
/// each. The rows below the band show the view they are in, so the menu
/// marks which value is in force rather than naming the next one.
pub(in crate::app::live_loop) fn agents_view_items(chrome: &Chrome) -> Vec<MenuItem> {
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

pub(super) fn global_items() -> Vec<MenuItem> {
    vec![
        item("new terminal", MenuAction::Act(Action::NewTerminal)),
        item("new tab", MenuAction::Act(Action::NewTab)),
        item("new workspace…", MenuAction::Act(Action::NewProject)),
        item("settings", MenuAction::Act(Action::Settings)),
        item("keybinding help", MenuAction::Act(Action::Help)),
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
pub(in crate::app::live_loop) fn passthrough_label(state: &crate::app::Pane) -> &'static str {
    if state.right_click_passthrough {
        "use gclient menu"
    } else {
        "send right-clicks to pane"
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
        ControlState::Held => item("release control", MenuAction::ReleaseControl(pane)),
        _ => item("take control", MenuAction::TakeControl(pane)),
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
