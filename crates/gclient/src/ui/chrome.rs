// upstream: herdr v0.8.0 src/ui.rs
//! UI view-state (herdr `AppState` chrome parts + `compute_view`), owned by
//! the run loop and read by every render module.

use crate::app::project_tabs::{first_slot, ProjectTabs, TabSet};
use crate::app::sidebar_model::{agent_row_state, pane_state, AgentEntry, SidebarModel};
use crate::app::viewer_state::{local_tab_id, ViewerState};
use crate::app::workspace_ops::WorkspaceModel;
use crate::app::{
    short_terminal_id, ClickRun, ContextMenuState, MouseGesture, Pane, PaneId, Workspace,
};
use crate::daemon::{LayoutAxis, LayoutNode};
use crate::theme::{Palette, Theme, ThemeKind};
use crate::ui::chrome_render::ChromeHits;
use crate::ui::dialogs::Dialog;
use crate::ui::hit::{Hit, SidebarSection};
use crate::ui::keybind_help::KeybindHelpState;
use crate::ui::keymap::{Keymap, HERDR_PREFIX};
use crate::ui::navigator::NavigatorState;
use crate::ui::pane_layout;
use crate::ui::settings::{ClientPrefs, SettingsState};
use crate::ui::sidebar::{self, agent_label};
use crate::ui::sidebar_rows;
use crate::ui::status::Toast;
use gobby_terminal::layout::{self, Node, PaneInfo, SplitBorder, TileLayout};
use gobby_terminal::selection::Selection;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use std::collections::{BTreeMap, HashMap};
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
    /// The attached daemon workspace, once its snapshot arrived.
    fn workspace_model(&self) -> Option<&WorkspaceModel>;
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

    fn workspace_model(&self) -> Option<&WorkspaceModel> {
        self.workspace_model()
    }
}

/// herdr `AgentState`, mapped onto Gobby roster rows.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum RowState {
    /// An attention prompt is waiting on this terminal.
    Attention,
    /// The terminal's host is gone; the row can only be destroyed.
    Orphaned,
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
    pub const ALL: [RowState; 6] = [
        RowState::Attention,
        RowState::Orphaned,
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

/// The sidebar's agent for an attention entry, when the roster joined one.
fn agent_entry<'a, W: WorkspaceView>(ws: &'a W, entry_id: &str) -> Option<&'a AgentEntry> {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
}

/// The roster row an attention entry points at.
///
/// The subject is a run id for a spawned agent and a session id for an
/// interactive session — the daemon keys every live entry `session:<uuid>` —
/// while the roster is keyed by terminal. Matching the two by string alone
/// therefore resolves nothing, which is why a blocked session never lit up its
/// row. The terminal that hosts the session is the answer in both cases.
pub fn attention_pane<W: WorkspaceView>(ws: &W, entry_id: &str) -> Option<PaneId> {
    if let Some(agent) = agent_entry(ws, entry_id) {
        if let Some(pane) = ws.pane_for_terminal(&agent.terminal_id) {
            return Some(pane);
        }
    }
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
    if let Some(agent) = agent_entry(ws, entry_id) {
        return agent_label(ws, agent);
    }
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
    /// One of the project dialogs (`ui::dialogs::project`) is open.
    ProjectDialog,
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
    /// Scroll position of each section, by `SidebarSection::index`.
    pub scrolls: [usize; 3],
    /// Selected project-section row, worktree rows included (navigate mode).
    pub selected: usize,
    /// Project ids in the order the user dragged them into; projects the
    /// order does not name follow in model order. `session.json` keeps it.
    pub project_order: Vec<String>,
    /// The one project card whose worktree rows are unfolded; every other
    /// card is folded. Focusing a project expands its card.
    pub expanded_project: Option<String>,
    /// Labels the user gave project cards, by project id; a card without
    /// one shows the daemon's name. `session.json` keeps them.
    pub project_labels: BTreeMap<String, String>,
    /// Machine filter of the sessions section, kept by `session.json`:
    /// `None` lists the rows on the local machine, `Some(ALL_MACHINES)` the
    /// rows on every machine, and `Some(machine_id)` those on that machine;
    /// `all_sessions` bounds the projects the rows come from.
    pub machine_filter: Option<String>,
    /// The projects section lists every project instead of the working
    /// ones (`sidebar_rows::working_projects`). `session.json` keeps it.
    pub all_projects: bool,
    /// The sessions section lists every project's rows, grouped by project,
    /// instead of the focused project's. `session.json` keeps it.
    pub all_sessions: bool,
}

impl Default for SidebarState {
    fn default() -> Self {
        Self {
            collapsed: false,
            hide_when_collapsed: false,
            width: 26,
            min_width: 18,
            max_width: 36,
            scrolls: [0; 3],
            selected: 0,
            project_order: Vec::new(),
            expanded_project: None,
            project_labels: BTreeMap::new(),
            machine_filter: None,
            all_projects: false,
            all_sessions: false,
        }
    }
}

impl SidebarState {
    /// The scroll position of one list section.
    pub fn scroll(&self, section: SidebarSection) -> usize {
        self.scrolls[section.index()]
    }

    pub fn scroll_mut(&mut self, section: SidebarSection) -> &mut usize {
        &mut self.scrolls[section.index()]
    }

    /// Fold `project_id`'s worktree rows when it is the expanded card, else
    /// expand it (folding whichever card was).
    pub fn toggle_group(&mut self, project_id: &str) {
        self.expanded_project = if self.is_expanded(project_id) {
            None
        } else {
            Some(project_id.to_owned())
        };
    }

    /// Whether `project_id`'s worktree rows are listed under its card.
    pub fn is_expanded(&self, project_id: &str) -> bool {
        self.expanded_project.as_deref() == Some(project_id)
    }

    /// herdr `set_manual_sidebar_width`: the pointer column becomes the
    /// sidebar's last column, within the width bounds.
    pub fn set_width_from_column(&mut self, area: Rect, column: u16) {
        let width = column.saturating_sub(area.x).saturating_add(1);
        self.width = width.clamp(self.min_width, self.max_width);
    }
}

/// One tab: a BSP layout whose slots map to workspace panes.
pub struct Tab {
    /// The daemon tab id, or a `local_tab_id` for a tab a scripted path
    /// opened. Focus and zoom live on `Chrome::viewer`, keyed by it.
    pub id: String,
    pub title: String,
    pub layout: TileLayout,
    pub slots: HashMap<layout::PaneId, PaneId>,
    /// The worktree this tab's shell was opened in, when a worktree row
    /// opened it; a second click on that row reveals this tab.
    pub worktree_id: Option<String>,
}

impl Tab {
    pub fn new(title: impl Into<String>, first: PaneId) -> Self {
        let (layout, slot) = TileLayout::new();
        let mut slots = HashMap::new();
        slots.insert(slot, first);
        Self::with_layout(title, layout, slots)
    }

    /// A tab over a rebuilt layout whose slots are already mapped.
    pub fn with_layout(
        title: impl Into<String>,
        layout: TileLayout,
        slots: HashMap<layout::PaneId, PaneId>,
    ) -> Self {
        Self {
            id: local_tab_id(),
            title: title.into(),
            layout,
            slots,
            worktree_id: None,
        }
    }

    /// The first slot in layout order; the one a fresh tab shows.
    pub fn first_slot(&self) -> layout::PaneId {
        first_slot(self.layout.root())
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
    /// The `[working]`/`[all]` control of the projects band.
    pub projects_filter_hit_area: Option<Rect>,
    /// The `[view]` control of the sessions band.
    pub sessions_view_hit_area: Option<Rect>,
    /// Session, agent run and bare terminal rows drawn in the sidebar, by
    /// entry id.
    pub agent_hit_areas: Vec<(String, Rect)>,
    /// Machine rows drawn in the sidebar, by machine id.
    pub machine_hit_areas: Vec<(String, Rect)>,
    /// The `│` column between the sidebar and the content column.
    pub sidebar_divider_x: Option<u16>,
    /// The three sections' rects (band and body), by
    /// `SidebarSection::index`, for the wheel over a bare sidebar cell.
    pub sidebar_section_rects: [Rect; 3],
    pub sidebar_toggle_hit_area: Option<Rect>,
    /// Scrollbar lane beside each section, by `SidebarSection::index`.
    pub sidebar_scrollbar_hit_areas: [Option<Rect>; 3],
    /// Leading control-state span of the status line.
    pub control_indicator_hit_area: Option<Rect>,
    /// Settings popup including its border, while the overlay is drawn.
    pub settings_dialog_area: Option<Rect>,
    /// Settings rows drawn, as indexes into `SettingsRow::ALL`.
    pub settings_row_hit_areas: Vec<(usize, Rect)>,
    /// Buttons of the open dialog, in its button order (confirm close:
    /// `close`, `cancel`); empty while no dialog is drawn.
    pub dialog_button_hit_areas: Vec<Rect>,
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
            dialog_buttons,
        } = hits;
        self.dialog_button_hit_areas = dialog_buttons;
        self.tab_hit_areas = tab_bar.tabs;
        self.tab_scroll_left_hit_area = tab_bar.scroll_left;
        self.tab_scroll_right_hit_area = tab_bar.scroll_right;
        self.new_tab_hit_area = tab_bar.new_tab;
        self.project_hit_areas = sidebar.projects;
        self.worktree_hit_areas = sidebar.worktrees;
        self.group_toggle_hit_areas = sidebar.group_toggles;
        self.projects_new_hit_area = sidebar.projects_new;
        self.projects_menu_hit_area = sidebar.projects_menu;
        self.projects_filter_hit_area = sidebar.projects_filter;
        self.sessions_view_hit_area = sidebar.sessions_view;
        self.agent_hit_areas = sidebar.agents;
        self.machine_hit_areas = sidebar.machines;
        self.sidebar_scrollbar_hit_areas = sidebar.scrollbars;
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
    /// This window's focus, zoom, and active tab over the daemon layout.
    pub viewer: ViewerState,
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
    /// An outer tmux owns the terminal: `keymap` was built for its shifted
    /// prefix and the status line says so. TODO(#21357): retire with tmux.
    pub nested_tmux: bool,
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
    /// Render ticks so far; the sidebar's ticker scrolls the selected
    /// over-long row by it (`sidebar_rows::ticker_window`).
    pub ticker: u64,
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
            viewer: ViewerState::default(),
            tab_scroll: 0,
            tab_scroll_follow_active: true,
            navigator: NavigatorState::default(),
            keybind_help: KeybindHelpState::default(),
            settings: SettingsState::default(),
            dialog: None,
            toast: None,
            status_message: None,
            view: ViewState::default(),
            keymap: Keymap::defaults(HERDR_PREFIX),
            nested_tmux: false,
            selection: None,
            gesture: None,
            hover: None,
            last_click: None,
            last_copy: None,
            link_opener: DEFAULT_LINK_OPENER.to_owned(),
            last_focused: None,
            pending_mouse_capture: None,
            menu: None,
            ticker: 0,
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

    /// The index of this window's active tab in the focused project's set.
    pub fn active_index(&self) -> usize {
        self.viewer
            .active_index(self.project_tabs.key(), &self.tabs().tabs)
    }

    pub fn active_tab(&self) -> Option<&Tab> {
        self.tabs().tabs.get(self.active_index())
    }

    pub fn active_tab_mut(&mut self) -> Option<&mut Tab> {
        let index = self.active_index();
        self.tabs_mut().tabs.get_mut(index)
    }

    /// Show tab `index`, remembering it for this project; out of range is
    /// refused.
    pub fn activate_tab(&mut self, index: usize) -> bool {
        if index >= self.tabs().tabs.len() {
            return false;
        }
        self.set_active_index(index);
        self.tab_scroll_follow_active = true;
        true
    }

    fn set_active_index(&mut self, index: usize) {
        let project = self.project_tabs.key().to_string();
        match self.tabs().tabs.get(index) {
            Some(tab) => {
                let id = tab.id.clone();
                self.viewer.active_tab.insert(project, id);
            }
            None => {
                self.viewer.active_tab.remove(&project);
            }
        }
    }

    /// Show `project_id`'s tab set. The first focus of all adopts the
    /// anonymous set, so its active-tab hint moves to the project with it.
    pub fn focus_project(&mut self, project_id: &str) {
        if self.project_tabs.focused.is_none() {
            if let Some(id) = self.viewer.active_tab.remove(self.project_tabs.key()) {
                self.viewer.active_tab.insert(project_id.to_string(), id);
            }
        }
        self.project_tabs.focus(project_id);
    }

    /// After tabs were removed: keep the active tab when it survived, else
    /// show the tab now at `index`, clamped to the last one.
    pub fn settle_active_index(&mut self, index: usize) {
        let tabs = &self.tabs().tabs;
        let survived = self
            .viewer
            .active_tab
            .get(self.project_tabs.key())
            .is_some_and(|id| tabs.iter().any(|tab| &tab.id == id));
        if !survived {
            self.set_active_index(index.min(tabs.len().saturating_sub(1)));
        }
    }

    /// The slot this window focuses in `tab`.
    pub fn tab_focus(&self, tab: &Tab) -> layout::PaneId {
        self.viewer.focus_of(tab)
    }

    /// The focused slot of the active tab.
    pub fn focus_slot(&self) -> Option<layout::PaneId> {
        self.active_tab().map(|tab| self.viewer.focus_of(tab))
    }

    /// Focus `slot` in the active tab.
    pub fn set_focus_slot(&mut self, slot: layout::PaneId) {
        if let Some(id) = self.active_tab().map(|tab| tab.id.clone()) {
            self.viewer.focus.insert(id, slot);
        }
    }

    /// Whether this window shows the active tab zoomed to its focused slot.
    pub fn is_zoomed(&self) -> bool {
        self.active_tab()
            .is_some_and(|tab| self.viewer.is_zoomed(tab))
    }

    pub fn toggle_zoom(&mut self) {
        let Some(id) = self.active_tab().map(|tab| tab.id.clone()) else {
            return;
        };
        if !self.viewer.zoomed.remove(&id) {
            self.viewer.zoomed.insert(id);
        }
    }

    /// Focused workspace pane in the active tab.
    pub fn focused_pane(&self) -> Option<PaneId> {
        self.active_tab()
            .and_then(|tab| self.viewer.focused_pane(tab))
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
        if self.tabs().tabs.is_empty() {
            let tab = Tab::new(title, pane);
            let slot = tab.first_slot();
            self.push_tab(tab);
            return slot;
        }
        let index = self.active_index();
        let focus = self.tab_focus(&self.tabs().tabs[index]);
        let tab = &mut self.tabs_mut().tabs[index];
        let slot = tab.layout.split_focused(focus, direction);
        tab.slots.insert(slot, pane);
        let id = tab.id.clone();
        self.viewer.focus.insert(id, slot);
        slot
    }

    /// Open `pane` in a fresh tab and make it active.
    pub fn open_tab(&mut self, pane: PaneId, title: &str) {
        self.push_tab(Tab::new(title, pane));
    }

    fn push_tab(&mut self, tab: Tab) {
        let set = self.tabs_mut();
        set.tabs.push(tab);
        let last = set.tabs.len() - 1;
        self.set_active_index(last);
    }

    /// Close the focused slot; drops the tab when it was the last slot.
    pub fn close_focused(&mut self) -> Option<PaneId> {
        let index = self.active_index();
        let focus = self.tab_focus(self.tabs().tabs.get(index)?);
        let tab = &mut self.tabs_mut().tabs[index];
        let pane = tab.slots.remove(&focus);
        match tab.layout.close_focused(focus) {
            Some(next) => {
                let id = tab.id.clone();
                self.viewer.focus.insert(id, next);
            }
            None => {
                let tab = self.tabs_mut().tabs.remove(index);
                self.viewer.focus.remove(&tab.id);
                self.viewer.zoomed.remove(&tab.id);
                self.settle_active_index(index);
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
        if index != self.active_index() {
            self.set_active_index(index);
            self.tab_scroll_follow_active = true;
        }
        let id = self.tabs().tabs[index].id.clone();
        self.viewer.focus.insert(id, slot);
        if previous != Some(pane) {
            self.last_focused = previous;
        }
        true
    }

    /// Rebuild `project_id`'s tab set from the daemon workspace through this
    /// window's viewer state. Returns the terminal ids of slots no roster pane
    /// backs yet; they render empty until the roster delivers them.
    pub fn project_workspace<W: WorkspaceView>(&mut self, ws: &W, project_id: &str) -> Vec<String> {
        let Some(model) = ws.workspace_model() else {
            return Vec::new();
        };
        let mut unresolved = Vec::new();
        let mut tabs = Vec::new();
        for row in model.tabs_for_project(project_id) {
            let mut slots = HashMap::new();
            let mut first_terminal = None;
            let root = project_node(&row.layout, &mut |pane_id: &str| {
                let slot = self.viewer.panes.intern(pane_id);
                if let Some(terminal_id) = model
                    .pane(pane_id)
                    .and_then(|pane| pane.terminal_id.as_deref())
                {
                    first_terminal.get_or_insert_with(|| terminal_id.to_string());
                    match ws.pane_for_terminal(terminal_id) {
                        Some(pane) => {
                            slots.insert(slot, pane);
                        }
                        None => unresolved.push(terminal_id.to_string()),
                    }
                }
                slot
            });
            let layout = TileLayout::from_saved(root);
            let focus = self
                .viewer
                .focus
                .get(&row.id)
                .copied()
                .filter(|slot| layout.pane_ids().contains(slot))
                .or_else(|| {
                    row.focused_pane_id
                        .as_deref()
                        .and_then(|pane_id| self.viewer.panes.slot(pane_id))
                        .filter(|slot| layout.pane_ids().contains(slot))
                })
                .unwrap_or_else(|| first_slot(layout.root()));
            self.viewer.focus.insert(row.id.clone(), focus);
            let title = row.title.clone().unwrap_or_else(|| {
                first_terminal
                    .as_deref()
                    .map_or_else(String::new, |terminal_id| terminal_label(ws, terminal_id))
            });
            let mut tab = Tab::with_layout(title, layout, slots);
            tab.id = row.id.clone();
            tab.worktree_id = row.worktree_id.clone();
            tabs.push(tab);
        }
        let active = self
            .viewer
            .active_tab
            .get(project_id)
            .or(model.workspace.focused_tab_id.as_ref())
            .and_then(|id| tabs.iter().position(|tab| &tab.id == id))
            .unwrap_or(0);
        if let Some(tab) = tabs.get(active) {
            self.viewer
                .active_tab
                .insert(project_id.to_string(), tab.id.clone());
        }
        self.project_tabs
            .sets
            .insert(project_id.to_string(), TabSet { tabs });
        unresolved
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
            Some(tab) => pane_layout::pane_geometry(
                tab,
                self.viewer.focus_of(tab),
                self.viewer.is_zoomed(tab),
                terminal_area,
                &self.prefs,
            ),
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
        let sidebar_section_rects = sidebar::section_rects(ws, self, sidebar_rect);
        self.view = ViewState {
            sidebar_rect,
            tab_bar_rect,
            terminal_area,
            status_rect,
            pane_infos,
            split_borders,
            sidebar_divider_x,
            sidebar_section_rects,
            ..ViewState::default()
        };
    }
}

/// The layout tree of a daemon tab; every leaf is interned through `slot_of`.
fn project_node(node: &LayoutNode, slot_of: &mut impl FnMut(&str) -> layout::PaneId) -> Node {
    match node {
        LayoutNode::Pane { pane_id } => Node::Pane(slot_of(pane_id)),
        LayoutNode::Split {
            axis,
            ratio,
            children,
        } => Node::Split {
            direction: match axis {
                LayoutAxis::Horizontal => Direction::Horizontal,
                LayoutAxis::Vertical => Direction::Vertical,
            },
            ratio: *ratio as f32,
            first: Box::new(project_node(&children[0], slot_of)),
            second: Box::new(project_node(&children[1], slot_of)),
        },
    }
}
