// upstream: herdr v0.8.0 src/ui.rs
//! UI view-state (herdr `AppState` chrome parts + `compute_view`), owned by
//! the run loop and read by every render module.

use crate::app::project_tabs::{ProjectTabs, TabSet};
use crate::app::sidebar_model::{agent_row_state, pane_state, SidebarModel};
use crate::app::{
    short_terminal_id, ClickRun, ContextMenuState, MouseGesture, Pane, PaneId, Workspace,
};
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
use crate::ui::sidebar_rows;
use crate::ui::status::Toast;
use gobby_terminal::layout::{self, PaneInfo, SplitBorder, TileLayout};
use gobby_terminal::selection::Selection;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use std::collections::{BTreeSet, HashMap};
use std::path::Path;

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
    /// The project the sidebar's terminal and agent rows belong to.
    fn focused_project(&self) -> Option<&str>;
    fn sidebar(&self) -> &SidebarModel;
    fn roster_terminal_ids(&self) -> Vec<String>;
    fn attention_entry_ids(&self) -> Vec<String>;
    fn pane_for_terminal(&self, terminal_id: &str) -> Option<PaneId>;
    /// Panics on an unknown id, like `Workspace::pane`; ids come from
    /// `pane_for_terminal` or the chrome's own slots.
    fn pane(&self, id: PaneId) -> &Pane;
    fn daemon_ready(&self) -> bool;
    fn gobby_home(&self) -> Option<&Path>;
}

// Inherent methods win over trait methods in method-call syntax, so these
// forward without recursing.
impl WorkspaceView for Workspace {
    fn project_id(&self) -> Option<&str> {
        self.project_id()
    }

    fn focused_project(&self) -> Option<&str> {
        self.project_id()
    }

    fn sidebar(&self) -> &SidebarModel {
        self.sidebar()
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

    fn gobby_home(&self) -> Option<&Path> {
        self.gobby_home()
    }
}

/// herdr `AgentState`, mapped onto Gobby roster rows.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum RowState {
    /// An attention prompt is waiting on this terminal.
    Attention,
    /// Output is arriving on an attached pane.
    Working,
    /// New output landed since the pane was last focused.
    Unseen,
    /// Attached and quiet.
    #[default]
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
    let pane = ws.pane(pane_id);
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == terminal_id)
        .map_or_else(|| pane_state(pane), |agent| agent_row_state(agent, pane))
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
    /// A right-click menu is open; `Chrome::menu` holds it.
    ContextMenu,
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
    /// Rows given to the projects section before the agents panel.
    pub section_split: Option<u16>,
    pub scroll: usize,
    pub agents_scroll: usize,
    /// Selected project-section row, worktree rows included (navigate mode).
    pub selected: usize,
    /// Project ids in the order the user dragged them into; projects the
    /// order does not name follow in model order. `session.json` keeps it.
    pub project_order: Vec<String>,
    /// Projects whose worktree rows are folded under the card.
    pub collapsed_projects: BTreeSet<String>,
    /// Machine whose agents the sidebar lists; none lists every machine.
    pub machine_filter: Option<String>,
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
            agents_scroll: 0,
            selected: 0,
            project_order: Vec::new(),
            collapsed_projects: BTreeSet::new(),
            machine_filter: None,
        }
    }
}

impl SidebarState {
    /// The scroll position of one list section.
    pub fn scroll_mut(&mut self, section: SidebarSection) -> &mut usize {
        match section {
            SidebarSection::Projects => &mut self.scroll,
            SidebarSection::Agents => &mut self.agents_scroll,
        }
    }

    /// Collapse `project_id`'s worktree rows, or expand them again.
    pub fn toggle_group(&mut self, project_id: &str) {
        if !self.collapsed_projects.remove(project_id) {
            self.collapsed_projects.insert(project_id.to_owned());
        }
    }

    /// herdr `set_manual_sidebar_width`: the pointer column becomes the
    /// sidebar's last column, within the width bounds.
    pub fn set_width_from_column(&mut self, area: Rect, column: u16) {
        let width = column.saturating_sub(area.x).saturating_add(1);
        self.width = width.clamp(self.min_width, self.max_width);
    }

    /// herdr `set_sidebar_section_split`: the pointer row becomes the first
    /// agents row, so the projects keep the rows above it and each section
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
    /// The worktree this tab's shell was opened in, when a worktree row
    /// opened it; a second click on that row reveals this tab.
    pub worktree_id: Option<String>,
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
            worktree_id: None,
        }
    }

    /// A tab over a rebuilt layout whose slots are already mapped.
    pub fn with_layout(
        title: impl Into<String>,
        layout: TileLayout,
        slots: HashMap<layout::PaneId, PaneId>,
    ) -> Self {
        Self {
            title: title.into(),
            layout,
            slots,
            zoomed: false,
            worktree_id: None,
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
    /// Project cards drawn in the sidebar, by project id.
    pub project_hit_areas: Vec<(String, Rect)>,
    /// Worktree rows drawn under their cards, by worktree id.
    pub worktree_hit_areas: Vec<(String, Rect)>,
    /// The `▸`/`▾` cell of each card that has worktrees, by project id.
    pub group_toggle_hit_areas: Vec<(String, Rect)>,
    pub projects_new_hit_area: Option<Rect>,
    pub projects_menu_hit_area: Option<Rect>,
    /// Agent rows drawn in the sidebar, by entry id.
    pub agent_hit_areas: Vec<(String, Rect)>,
    pub machine_filter_hit_area: Option<Rect>,
    /// The `│` column between the sidebar and the content column.
    pub sidebar_divider_x: Option<u16>,
    /// The `─` row between the projects and agents sections.
    pub sidebar_section_divider_y: Option<u16>,
    pub sidebar_toggle_hit_area: Option<Rect>,
    pub projects_scrollbar_hit_area: Option<Rect>,
    pub agents_scrollbar_hit_area: Option<Rect>,
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
            // The open menu owns its rows; `Chrome::apply_hits` places them.
            menu_rows: _,
        } = hits;
        self.tab_hit_areas = tab_bar.tabs;
        self.tab_scroll_left_hit_area = tab_bar.scroll_left;
        self.tab_scroll_right_hit_area = tab_bar.scroll_right;
        self.new_tab_hit_area = tab_bar.new_tab;
        self.project_hit_areas = sidebar.projects;
        self.worktree_hit_areas = sidebar.worktrees;
        self.group_toggle_hit_areas = sidebar.group_toggles;
        self.projects_new_hit_area = sidebar.projects_new;
        self.projects_menu_hit_area = sidebar.projects_menu;
        self.agent_hit_areas = sidebar.agents;
        self.machine_filter_hit_area = sidebar.machine_filter;
        self.projects_scrollbar_hit_area = sidebar.projects_scrollbar;
        self.agents_scrollbar_hit_area = sidebar.agents_scrollbar;
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
    /// Every project's tabs; the focused project's set is the tab bar.
    pub project_tabs: ProjectTabs,
    pub tab_scroll: usize,
    pub tab_scroll_follow_active: bool,
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
    /// Pane focus last left, for `LastPane` (herdr `previous_pane_focus`).
    pub last_focused: Option<PaneId>,
    /// Mouse-capture state the settings toggle asked for; the live loop
    /// applies it to the terminal guard after the current event.
    pub pending_mouse_capture: Option<bool>,
    /// The open right-click menu while `mode` is `ContextMenu`.
    pub menu: Option<ContextMenuState>,
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
            project_tabs: ProjectTabs::default(),
            tab_scroll: 0,
            tab_scroll_follow_active: true,
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
            last_focused: None,
            pending_mouse_capture: None,
            menu: None,
        }
    }

    pub fn dark() -> Self {
        Self::new(Theme::new(ThemeKind::Dark))
    }

    pub fn set_theme(&mut self, kind: ThemeKind) {
        self.theme = Theme::new(kind);
        self.palette = self.theme.palette();
    }

    /// Adopt loaded prefs: the theme and the sidebar width take effect at
    /// once; the rest is read from `prefs` wherever it applies.
    pub fn apply_prefs(&mut self, prefs: ClientPrefs) {
        self.set_theme(prefs.theme_kind());
        self.sidebar.width = prefs.sidebar_width;
        self.prefs = prefs;
    }

    /// The focused project's tab bar.
    pub fn tabs(&self) -> &TabSet {
        self.project_tabs.set()
    }

    pub fn tabs_mut(&mut self) -> &mut TabSet {
        self.project_tabs.set_mut()
    }

    pub fn active_tab(&self) -> Option<&Tab> {
        self.tabs().active()
    }

    pub fn active_tab_mut(&mut self) -> Option<&mut Tab> {
        self.tabs_mut().active_mut()
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
        let set = self.tabs_mut();
        if set.tabs.is_empty() {
            let tab = Tab::new(title, pane);
            let slot = tab.layout.focused();
            set.tabs.push(tab);
            set.active_tab = 0;
            return slot;
        }
        let tab = &mut set.tabs[set.active_tab];
        let slot = tab.layout.split_focused(direction);
        tab.slots.insert(slot, pane);
        slot
    }

    /// Open `pane` in a fresh tab and make it active.
    pub fn open_tab(&mut self, pane: PaneId, title: &str) {
        let set = self.tabs_mut();
        set.tabs.push(Tab::new(title, pane));
        set.active_tab = set.tabs.len() - 1;
    }

    /// Close the focused slot; drops the tab when it was the last slot.
    pub fn close_focused(&mut self) -> Option<PaneId> {
        let set = self.tabs_mut();
        let tab = set.tabs.get_mut(set.active_tab)?;
        let slot = tab.layout.focused();
        let pane = tab.slots.remove(&slot);
        if !tab.layout.close_focused() {
            set.tabs.remove(set.active_tab);
            if set.active_tab > 0 && set.active_tab >= set.tabs.len() {
                set.active_tab = set.tabs.len().saturating_sub(1);
            }
        }
        pane
    }

    /// Focus `pane` in whichever tab shows it, switching to that tab, and
    /// remember the pane focus left for `LastPane`. `false` when no tab
    /// shows it.
    pub fn focus_pane(&mut self, pane: PaneId) -> bool {
        let Some((index, slot)) = self
            .tabs()
            .tabs
            .iter()
            .enumerate()
            .find_map(|(index, tab)| tab.slot_for(pane).map(|slot| (index, slot)))
        else {
            return false;
        };
        let previous = self.focused_pane();
        let set = self.tabs_mut();
        if index != set.active_tab {
            set.active_tab = index;
            self.tab_scroll_follow_active = true;
        }
        self.tabs_mut().tabs[index].layout.focus_pane(slot);
        if previous != Some(pane) {
            self.last_focused = previous;
        }
        true
    }

    /// Bring `pane` on screen the way a roster click does: focus it where a
    /// tab shows it, or split it into the active tab. A pane is never shown
    /// twice.
    pub fn reveal_pane(&mut self, pane: PaneId, title: &str) {
        if !self.focus_pane(pane) {
            self.open_pane(pane, title);
        }
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
        !(self.tabs().tabs.len() <= 1 && self.prefs.hide_tab_bar_when_single_tab)
    }

    /// Write the rects the renderers drew back where the hit tests read
    /// them: the chrome map into `view`, the menu rows into the open menu.
    pub fn apply_hits(&mut self, mut hits: ChromeHits) {
        if let (Some(menu), Some(rows)) = (self.menu.as_mut(), hits.menu_rows.take()) {
            menu.item_rects = rows;
        }
        self.view.apply_hits(hits);
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
        let rows = sidebar_rows::project_rows(ws, self).len();
        if rows > 0 && self.sidebar.selected >= rows {
            self.sidebar.selected = rows - 1;
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
