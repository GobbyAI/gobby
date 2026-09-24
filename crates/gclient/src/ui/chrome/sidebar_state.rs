// upstream: herdr v0.8.0 src/ui.rs
//! The sidebar's view state: pinned or hidden, its width, each section's
//! scroll, the navigate cursor, and the per-window filters.

use crate::ui::hit::SidebarSection;
use ratatui::layout::Rect;
use std::collections::BTreeMap;

#[derive(Debug, Clone)]
pub struct SidebarState {
    /// The sidebar takes its column beside the content. Unpinned (the
    /// default) it takes none.
    pub pinned: bool,
    pub width: u16,
    pub min_width: u16,
    pub max_width: u16,
    /// Scroll position of each section, by `SidebarSection::index`.
    pub scrolls: [usize; 3],
    /// Selected project-section row, worktree rows included (navigate mode).
    pub selected: usize,
    /// Project ids in the order the user dragged them into; projects the
    /// order does not name follow in model order. `prefs.toml` keeps it
    /// (`ClientPrefs::project_order`), mirrored on every drop.
    pub project_order: Vec<String>,
    /// The one project card whose worktree rows are unfolded; every other
    /// card is folded. Focusing a project expands its card.
    pub expanded_project: Option<String>,
    /// Labels the user gave project cards, by project id; a card without
    /// one shows the daemon's name. `prefs.toml` keeps them
    /// (`ClientPrefs::project_labels`), mirrored on every rename.
    pub project_labels: BTreeMap<String, String>,
    /// Machine filter of the sessions section, per window: `None` lists the
    /// rows on the local machine, `Some(ALL_MACHINES)` the rows on every
    /// machine, and `Some(machine_id)` those on that machine;
    /// `all_sessions` bounds the projects the rows come from.
    pub machine_filter: Option<String>,
    /// The projects section lists every project instead of the working
    /// ones (`sidebar_rows::working_projects`). Per window, never saved.
    pub all_projects: bool,
    /// The sessions section lists every project's rows, grouped by project,
    /// instead of the focused project's. Per window, never saved.
    pub all_sessions: bool,
}

impl Default for SidebarState {
    fn default() -> Self {
        Self {
            pinned: false,
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
