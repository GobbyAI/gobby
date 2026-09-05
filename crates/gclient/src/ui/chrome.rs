// upstream: herdr v0.8.0 src/ui.rs
//! UI view-state (herdr `AppState` chrome parts + `compute_view`), owned by
//! the run loop and read by every render module.

use crate::app::{Pane, PaneId, Workspace};
use crate::theme::{Palette, Theme, ThemeKind};
use crate::ui::dialogs::Dialog;
use crate::ui::keybind_help::KeybindHelpState;
use crate::ui::keymap::Keymap;
use crate::ui::navigator::NavigatorState;
use crate::ui::pane_layout;
use crate::ui::settings::{ClientPrefs, SettingsState};
use crate::ui::status::Toast;
use gobby_terminal::layout::{self, PaneInfo, SplitBorder, TileLayout};
use gobby_terminal::selection::Selection;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use std::collections::HashMap;

/// Collapsed sidebar width (herdr `COLLAPSED_WIDTH`).
pub const COLLAPSED_WIDTH: u16 = 4;

/// Read-only workspace facts the chrome renders from. Implemented for the
/// scripted workspace here; the live workspace implements it where its pane
/// accessors live.
pub trait WorkspaceView {
    fn project_id(&self) -> Option<&str>;
    fn roster_terminal_ids(&self) -> Vec<String>;
    fn attention_entry_ids(&self) -> Vec<String>;
    fn pane_for_terminal(&self, terminal_id: &str) -> Option<PaneId>;
    /// Panics on an unknown id, like `Workspace::pane`; ids come from
    /// `pane_for_terminal` or the chrome's own slots.
    fn pane(&self, id: PaneId) -> &Pane;
    fn daemon_ready(&self) -> bool;
}

// Inherent methods win over trait methods in method-call syntax, so these
// forward without recursing.
impl WorkspaceView for Workspace {
    fn project_id(&self) -> Option<&str> {
        self.project_id()
    }

    fn roster_terminal_ids(&self) -> Vec<String> {
        self.roster_terminal_ids()
    }

    fn attention_entry_ids(&self) -> Vec<String> {
        self.attention_entry_ids()
    }

    fn pane_for_terminal(&self, terminal_id: &str) -> Option<PaneId> {
        self.pane_for_terminal(terminal_id)
    }

    fn pane(&self, id: PaneId) -> &Pane {
        self.pane(id)
    }

    fn daemon_ready(&self) -> bool {
        // The scripted daemon is always reachable.
        true
    }
}

/// herdr `AgentState`, mapped onto Gobby roster rows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RowState {
    /// An attention prompt is waiting on this terminal.
    Attention,
    /// Output is arriving on an attached pane.
    Working,
    /// New output landed since the pane was last focused.
    Unseen,
    /// Attached and quiet.
    Idle,
    /// No pane is attached to this roster row.
    Unknown,
}

impl RowState {
    pub const ALL: [RowState; 5] = [
        RowState::Attention,
        RowState::Working,
        RowState::Unseen,
        RowState::Idle,
        RowState::Unknown,
    ];
}

/// Attention entries are keyed `<kind>:<terminal>`; the terminal part is the
/// roster row they point at.
pub fn attention_terminal(entry_id: &str) -> &str {
    entry_id.rsplit_once(':').map_or(entry_id, |(_, id)| id)
}

pub fn row_state<W: WorkspaceView>(ws: &W, terminal_id: &str) -> RowState {
    if ws
        .attention_entry_ids()
        .iter()
        .any(|entry| attention_terminal(entry) == terminal_id)
    {
        return RowState::Attention;
    }
    let Some(pane) = ws.pane_for_terminal(terminal_id).map(|id| ws.pane(id)) else {
        return RowState::Unknown;
    };
    if pane.new_output && pane.live {
        RowState::Working
    } else if pane.new_output {
        RowState::Unseen
    } else {
        RowState::Idle
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Terminal,
    Navigate,
    Prefix,
    Copy,
    Resize,
    ConfirmClose,
    Rename,
    Respond,
    Settings,
    KeybindHelp,
    Navigator,
}

#[derive(Debug, Clone)]
pub struct SidebarState {
    pub collapsed: bool,
    pub width: u16,
    pub min_width: u16,
    pub max_width: u16,
    /// Rows given to the roster section before the attention panel.
    pub section_split: Option<u16>,
    pub scroll: usize,
    pub attention_scroll: usize,
    /// Selected roster row (navigate mode).
    pub selected: usize,
}

impl Default for SidebarState {
    fn default() -> Self {
        Self {
            collapsed: false,
            width: 26,
            min_width: 18,
            max_width: 36,
            section_split: None,
            scroll: 0,
            attention_scroll: 0,
            selected: 0,
        }
    }
}

/// One tab: a BSP layout whose slots map to workspace panes.
pub struct Tab {
    pub title: String,
    pub layout: TileLayout,
    pub slots: HashMap<layout::PaneId, PaneId>,
    pub zoomed: bool,
}

impl Tab {
    pub fn new(title: impl Into<String>, first: PaneId) -> Self {
        let (layout, slot) = TileLayout::new();
        let mut slots = HashMap::new();
        slots.insert(slot, first);
        Self {
            title: title.into(),
            layout,
            slots,
            zoomed: false,
        }
    }

    pub fn focused_pane(&self) -> Option<PaneId> {
        self.slots.get(&self.layout.focused()).copied()
    }

    pub fn slot_for(&self, pane: PaneId) -> Option<layout::PaneId> {
        self.slots
            .iter()
            .find(|(_, app)| **app == pane)
            .map(|(slot, _)| *slot)
    }
}

/// Computed frame geometry (herdr `ViewState`).
#[derive(Clone, Default)]
pub struct ViewState {
    pub sidebar_rect: Rect,
    pub tab_bar_rect: Option<Rect>,
    pub tab_hit_areas: Vec<(usize, Rect)>,
    pub tab_scroll_left_hit_area: Option<Rect>,
    pub tab_scroll_right_hit_area: Option<Rect>,
    pub new_tab_hit_area: Option<Rect>,
    pub terminal_area: Rect,
    /// Bottom row of the content column: control state and focused terminal.
    pub status_rect: Rect,
    pub toast_hit_area: Option<Rect>,
    pub pane_infos: Vec<PaneInfo>,
    pub split_borders: Vec<SplitBorder>,
    /// Roster rows drawn in the sidebar, by terminal id.
    pub roster_hit_areas: Vec<(String, Rect)>,
    /// Attention rows drawn in the sidebar, by entry id.
    pub attention_hit_areas: Vec<(String, Rect)>,
}

/// UI view-state the run loop owns and every render module reads.
pub struct Chrome {
    pub theme: Theme,
    pub palette: Palette,
    pub prefs: ClientPrefs,
    pub mode: Mode,
    pub sidebar: SidebarState,
    pub tabs: Vec<Tab>,
    pub active_tab: usize,
    pub tab_scroll: usize,
    pub tab_scroll_follow_active: bool,
    pub hide_tab_bar_when_single_tab: bool,
    pub navigator: NavigatorState,
    pub keybind_help: KeybindHelpState,
    pub settings: SettingsState,
    pub dialog: Option<Dialog>,
    pub toast: Option<Toast>,
    pub status_message: Option<String>,
    pub view: ViewState,
    pub keymap: Keymap,
    /// Mouse selection in progress or retained, keyed by layout slot.
    pub selection: Option<Selection>,
}

impl Chrome {
    pub fn new(theme: Theme) -> Self {
        let palette = theme.palette();
        Self {
            theme,
            palette,
            prefs: ClientPrefs::default(),
            mode: Mode::Terminal,
            sidebar: SidebarState::default(),
            tabs: Vec::new(),
            active_tab: 0,
            tab_scroll: 0,
            tab_scroll_follow_active: true,
            hide_tab_bar_when_single_tab: false,
            navigator: NavigatorState::default(),
            keybind_help: KeybindHelpState::default(),
            settings: SettingsState::default(),
            dialog: None,
            toast: None,
            status_message: None,
            view: ViewState::default(),
            keymap: Keymap::defaults(),
            selection: None,
        }
    }

    pub fn dark() -> Self {
        Self::new(Theme::new(ThemeKind::Dark))
    }

    pub fn set_theme(&mut self, kind: ThemeKind) {
        self.theme = Theme::new(kind);
        self.palette = self.theme.palette();
    }

    pub fn active_tab(&self) -> Option<&Tab> {
        self.tabs.get(self.active_tab)
    }

    pub fn active_tab_mut(&mut self) -> Option<&mut Tab> {
        self.tabs.get_mut(self.active_tab)
    }

    /// Focused workspace pane in the active tab.
    pub fn focused_pane(&self) -> Option<PaneId> {
        self.active_tab().and_then(Tab::focused_pane)
    }

    /// Workspace pane shown in a layout slot of the active tab.
    pub fn pane_for_slot(&self, slot: layout::PaneId) -> Option<PaneId> {
        self.active_tab()
            .and_then(|tab| tab.slots.get(&slot).copied())
    }

    /// Show `pane` in the active tab: split the focused slot side by side,
    /// or open a first tab when none exists. Returns the layout slot.
    pub fn open_pane(&mut self, pane: PaneId, title: &str) -> layout::PaneId {
        if self.tabs.is_empty() {
            let tab = Tab::new(title, pane);
            let slot = tab.layout.focused();
            self.tabs.push(tab);
            self.active_tab = 0;
            return slot;
        }
        let tab = &mut self.tabs[self.active_tab];
        let slot = tab.layout.split_focused(Direction::Horizontal);
        tab.slots.insert(slot, pane);
        slot
    }

    /// Open `pane` in a fresh tab and make it active.
    pub fn open_tab(&mut self, pane: PaneId, title: &str) {
        self.tabs.push(Tab::new(title, pane));
        self.active_tab = self.tabs.len() - 1;
    }

    /// Close the focused slot; drops the tab when it was the last slot.
    pub fn close_focused(&mut self) -> Option<PaneId> {
        let tab = self.tabs.get_mut(self.active_tab)?;
        let slot = tab.layout.focused();
        let pane = tab.slots.remove(&slot);
        if !tab.layout.close_focused() {
            self.tabs.remove(self.active_tab);
            if self.active_tab > 0 && self.active_tab >= self.tabs.len() {
                self.active_tab = self.tabs.len().saturating_sub(1);
            }
        }
        pane
    }

    pub fn focus_pane(&mut self, pane: PaneId) -> bool {
        let Some(tab) = self.tabs.get_mut(self.active_tab) else {
            return false;
        };
        let Some(slot) = tab.slot_for(pane) else {
            return false;
        };
        tab.layout.focus_pane(slot);
        true
    }

    /// Sidebar width for the current frame (herdr `compute_view` clamp).
    pub fn sidebar_width(&self, area: Rect) -> u16 {
        if self.sidebar.collapsed {
            return COLLAPSED_WIDTH.min(area.width);
        }
        let max = self.sidebar.max_width.min(area.width.saturating_sub(1));
        let min = self.sidebar.min_width.min(max);
        self.sidebar.width.clamp(min, max)
    }

    pub fn show_tab_bar(&self) -> bool {
        !(self.tabs.len() <= 1 && self.hide_tab_bar_when_single_tab)
    }

    /// Recompute `view` for `area` (herdr `compute_view`): sidebar column,
    /// tab bar row, terminal area, pane rects, and split borders.
    pub fn compute_view<W: WorkspaceView>(&mut self, ws: &W, area: Rect) {
        let sidebar_w = self.sidebar_width(area);
        let columns =
            Layout::horizontal([Constraint::Length(sidebar_w), Constraint::Min(1)]).split(area);
        let sidebar_rect = columns[0];
        let column =
            Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(columns[1]);
        let content = column[0];
        let status_rect = column[1];
        let (tab_bar_rect, terminal_area) = if self.show_tab_bar() {
            let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(1)]).split(content);
            (Some(rows[0]), rows[1])
        } else {
            (None, content)
        };
        let (pane_infos, split_borders) = match self.active_tab() {
            Some(tab) => pane_layout::pane_geometry(tab, terminal_area, &self.prefs),
            None => (Vec::new(), Vec::new()),
        };
        let roster_len = ws.roster_terminal_ids().len();
        if roster_len > 0 && self.sidebar.selected >= roster_len {
            self.sidebar.selected = roster_len - 1;
        }
        self.view = ViewState {
            sidebar_rect,
            tab_bar_rect,
            terminal_area,
            status_rect,
            pane_infos,
            split_borders,
            ..ViewState::default()
        };
    }
}
