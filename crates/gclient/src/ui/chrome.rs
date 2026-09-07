// upstream: herdr v0.8.0 src/ui.rs
//! UI view-state (herdr `AppState` chrome parts + `compute_view`), owned by
//! the run loop and read by every render module.

use crate::app::{short_terminal_id, ClickRun, MouseGesture, Pane, PaneId, Workspace};
use crate::theme::{Palette, Theme, ThemeKind};
use crate::ui::chrome_render::ChromeHits;
use crate::ui::dialogs::Dialog;
use crate::ui::hit::{Hit, SidebarSection};
use crate::ui::keybind_help::KeybindHelpState;
use crate::ui::keymap::Keymap;
use crate::ui::navigator::NavigatorState;
use crate::ui::pane_layout;
use crate::ui::settings::{ClientPrefs, SettingsState};
use crate::ui::sidebar;
use crate::ui::status::Toast;
use gobby_terminal::layout::{self, PaneInfo, SplitBorder, TileLayout};
use gobby_terminal::selection::Selection;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use std::collections::HashMap;

/// Collapsed sidebar width (herdr `COLLAPSED_WIDTH`).
pub const COLLAPSED_WIDTH: u16 = 4;

/// Command that opens a URL on this platform, the one a ctrl+click uses.
pub const DEFAULT_LINK_OPENER: &str = if cfg!(target_os = "macos") {
    "open"
} else {
    "xdg-open"
};

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

/// Attention entries are keyed `<kind>:<subject>`; this is the subject.
pub fn attention_subject(entry_id: &str) -> &str {
    entry_id.rsplit_once(':').map_or(entry_id, |(_, id)| id)
}

/// The roster row an attention entry points at.
///
/// The subject is a run id for a spawned agent and a session id for an
/// interactive session — the daemon keys every live entry `session:<uuid>` —
/// while the roster is keyed by terminal. Matching the two by string alone
/// therefore resolves nothing, which is why a blocked session never lit up its
/// row. The terminal that hosts the session is the answer in both cases.
pub fn attention_pane<W: WorkspaceView>(ws: &W, entry_id: &str) -> Option<PaneId> {
    let subject = attention_subject(entry_id);
    if let Some(pane) = ws.pane_for_terminal(subject) {
        return Some(pane);
    }
    ws.roster_terminal_ids()
        .iter()
        .filter_map(|id| ws.pane_for_terminal(id))
        .find(|id| ws.pane(*id).session_id.as_deref() == Some(subject))
}

/// What the chrome calls the terminal behind an attention entry: its name,
/// then its address when it has one the name does not already show, so a
/// blocked session reads `15 %15` and never its session uuid.
pub fn attention_label<W: WorkspaceView>(ws: &W, entry_id: &str) -> String {
    let Some(pane) = attention_pane(ws, entry_id) else {
        return short_terminal_id(attention_subject(entry_id)).to_string();
    };
    let pane = ws.pane(pane);
    let name = pane.display_name();
    match pane.address.as_deref().filter(|address| *address != name) {
        Some(address) => format!("{name} {address}"),
        None => name.to_string(),
    }
}

/// The one name every chrome surface shows for a terminal. Roster and attention
/// rows are keyed by terminal id, and an attention row can name a terminal no
/// pane owns yet, which is the case the short id covers.
pub fn terminal_label<W: WorkspaceView>(ws: &W, terminal_id: &str) -> String {
    match ws.pane_for_terminal(terminal_id) {
        Some(id) => ws.pane(id).display_name().to_string(),
        None => short_terminal_id(terminal_id).to_string(),
    }
}

pub fn row_state<W: WorkspaceView>(ws: &W, terminal_id: &str) -> RowState {
    let Some(pane_id) = ws.pane_for_terminal(terminal_id) else {
        return RowState::Unknown;
    };
    if ws
        .attention_entry_ids()
        .iter()
        .any(|entry| attention_pane(ws, entry) == Some(pane_id))
    {
        return RowState::Attention;
    }
    let pane = ws.pane(pane_id);
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
    /// herdr `SidebarCollapsedModeConfig::Hidden`: a collapsed sidebar takes
    /// no columns instead of the rail.
    pub hide_when_collapsed: bool,
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
            hide_when_collapsed: false,
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

impl SidebarState {
    /// The scroll position of one list section.
    pub fn scroll_mut(&mut self, section: SidebarSection) -> &mut usize {
        match section {
            SidebarSection::Roster => &mut self.scroll,
            SidebarSection::Attention => &mut self.attention_scroll,
        }
    }

    /// herdr `set_manual_sidebar_width`: the pointer column becomes the
    /// sidebar's last column, within the width bounds.
    pub fn set_width_from_column(&mut self, area: Rect, column: u16) {
        let width = column.saturating_sub(area.x).saturating_add(1);
        self.width = width.clamp(self.min_width, self.max_width);
    }

    /// herdr `set_sidebar_section_split`: the pointer row becomes the first
    /// attention row, so the roster keeps the rows above it and each section
    /// keeps at least its header. A sidebar under six rows keeps its fixed
    /// halves.
    pub fn set_split_from_row(&mut self, area: Rect, row: u16) {
        if area.height < 6 {
            return;
        }
        let split = row.saturating_sub(area.y).clamp(3, area.height - 3);
        self.section_split = Some(split);
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
    /// The `│` column between the sidebar and the content column.
    pub sidebar_divider_x: Option<u16>,
    /// The `─` row between the roster and attention sections.
    pub sidebar_section_divider_y: Option<u16>,
    pub sidebar_toggle_hit_area: Option<Rect>,
    pub roster_scrollbar_hit_area: Option<Rect>,
    pub attention_scrollbar_hit_area: Option<Rect>,
    /// Leading control-state span of the status line.
    pub control_indicator_hit_area: Option<Rect>,
    /// Settings popup including its border, while the overlay is drawn.
    pub settings_dialog_area: Option<Rect>,
    /// Settings rows drawn, as indexes into `SettingsRow::ALL`.
    pub settings_row_hit_areas: Vec<(usize, Rect)>,
}

impl ViewState {
    /// Record the rects the renderers drew this frame so `hit_test` sees the
    /// frame the user saw (herdr wrote them back from `render`).
    pub fn apply_hits(&mut self, hits: ChromeHits) {
        let ChromeHits {
            tab_bar,
            sidebar,
            control_indicator,
            toast,
            settings,
        } = hits;
        self.tab_hit_areas = tab_bar.tabs;
        self.tab_scroll_left_hit_area = tab_bar.scroll_left;
        self.tab_scroll_right_hit_area = tab_bar.scroll_right;
        self.new_tab_hit_area = tab_bar.new_tab;
        self.roster_hit_areas = sidebar.roster;
        self.attention_hit_areas = sidebar.attention;
        self.roster_scrollbar_hit_area = sidebar.roster_scrollbar;
        self.attention_scrollbar_hit_area = sidebar.attention_scrollbar;
        self.sidebar_toggle_hit_area = sidebar.toggle;
        self.control_indicator_hit_area = control_indicator;
        self.toast_hit_area = toast;
        let (dialog, rows) = settings.map_or((None, Vec::new()), |s| (Some(s.dialog), s.rows));
        self.settings_dialog_area = dialog;
        self.settings_row_hit_areas = rows;
    }
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
    /// The press-and-drag in progress, if any; `route_mouse` owns it.
    pub gesture: Option<MouseGesture>,
    /// What the pointer was over when it last moved with no button down,
    /// for hover styling; `route_mouse` owns it.
    pub hover: Option<Hit>,
    /// Presses on one screen cell in quick succession, for double- and
    /// triple-click; `route_mouse` owns it.
    pub last_click: Option<ClickRun>,
    /// Text of the last mouse copy, for middle-click paste.
    pub last_copy: Option<String>,
    /// Command a ctrl+click hands a link to: `DEFAULT_LINK_OPENER` unless a
    /// test points it elsewhere.
    pub link_opener: String,
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
            gesture: None,
            hover: None,
            last_click: None,
            last_copy: None,
            link_opener: DEFAULT_LINK_OPENER.to_owned(),
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

    /// Show `pane` in the active tab beside the focused slot, or open a first
    /// tab when none exists. Returns the layout slot.
    pub fn open_pane(&mut self, pane: PaneId, title: &str) -> layout::PaneId {
        self.open_split(pane, title, Direction::Horizontal)
    }

    /// Show `pane` in the active tab under the focused slot, or open a first
    /// tab when none exists. Returns the layout slot.
    pub fn open_pane_below(&mut self, pane: PaneId, title: &str) -> layout::PaneId {
        self.open_split(pane, title, Direction::Vertical)
    }

    fn open_split(&mut self, pane: PaneId, title: &str, direction: Direction) -> layout::PaneId {
        if self.tabs.is_empty() {
            let tab = Tab::new(title, pane);
            let slot = tab.layout.focused();
            self.tabs.push(tab);
            self.active_tab = 0;
            return slot;
        }
        let tab = &mut self.tabs[self.active_tab];
        let slot = tab.layout.split_focused(direction);
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

    /// Bring `pane` on screen the way a roster click does: focus it in the
    /// active tab, switch to the tab already showing it, or split it into
    /// the active tab when no tab shows it. A pane is never shown twice.
    pub fn reveal_pane(&mut self, pane: PaneId, title: &str) {
        if self.focus_pane(pane) {
            return;
        }
        if let Some(index) = self
            .tabs
            .iter()
            .position(|tab| tab.slot_for(pane).is_some())
        {
            self.active_tab = index;
            self.tab_scroll_follow_active = true;
            self.focus_pane(pane);
            return;
        }
        self.open_pane(pane, title);
    }

    /// Sidebar width for the current frame (herdr `compute_view` clamp).
    pub fn sidebar_width(&self, area: Rect) -> u16 {
        if self.sidebar.collapsed {
            if self.sidebar.hide_when_collapsed {
                return 0;
            }
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
        let (mut pane_infos, split_borders) = match self.active_tab() {
            Some(tab) => pane_layout::pane_geometry(tab, terminal_area, &self.prefs),
            None => (Vec::new(), Vec::new()),
        };
        // herdr resolved the scrollbar lane in `compute_view`, so hit tests
        // over `view.pane_infos` see the column the renderer draws into. Only
        // panes reachable through the roster are read: `WorkspaceView::pane`
        // panics on an unknown id.
        let attached: Vec<PaneId> = ws
            .roster_terminal_ids()
            .iter()
            .filter_map(|terminal| ws.pane_for_terminal(terminal))
            .collect();
        if let Some(tab) = self.active_tab() {
            for info in &mut pane_infos {
                let Some(pane) = tab
                    .slots
                    .get(&info.id)
                    .filter(|id| attached.contains(id))
                    .map(|id| ws.pane(*id))
                else {
                    continue;
                };
                let metrics = pane_layout::metrics_for(
                    pane.scroll_offset,
                    pane.max_scroll,
                    info.inner_rect.height,
                );
                info.scrollbar_rect = pane_layout::scrollbar_gutter(
                    pane_layout::pane_inner_rect(info.rect, info.borders),
                    self.prefs.pane_scrollbars,
                    metrics,
                );
            }
        }
        let roster_len = ws.roster_terminal_ids().len();
        if roster_len > 0 && self.sidebar.selected >= roster_len {
            self.sidebar.selected = roster_len - 1;
        }
        let sidebar_divider_x =
            (sidebar_rect.width > 0).then(|| sidebar_rect.x + sidebar_rect.width - 1);
        let sidebar_section_divider_y = sidebar::section_divider_y(sidebar_rect, &self.sidebar);
        self.view = ViewState {
            sidebar_rect,
            tab_bar_rect,
            terminal_area,
            status_rect,
            pane_infos,
            split_borders,
            sidebar_divider_x,
            sidebar_section_divider_y,
            ..ViewState::default()
        };
    }
}
