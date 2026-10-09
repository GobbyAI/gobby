// upstream: herdr v0.8.0 src/ui.rs
//! The sidebar's view state: pinned, overlaid or hidden, its side and
//! width, each section's scroll, the navigate cursor, and the per-window
//! filters.

use crate::ui::hit::SidebarSection;
use crate::ui::settings::SidebarSide;
use ratatui::backend::WindowSize;
use ratatui::layout::Rect;
use std::collections::BTreeMap;
use std::time::Instant;

/// The overlay's width in columns, at most the width it lies over.
pub const OVERLAY_WIDTH: u16 = 34;

/// The widest the pinned sidebar drags, in the pixels the terminal reports
/// (Josh, 2026-10-05: "max 540px width").
const MAX_WIDTH_PIXELS: u32 = 540;

/// The width cap in columns before the terminal reports its pixels, and the
/// floor under the pixel cap: a terminal reporting no pixels keeps it.
const BASE_MAX_WIDTH: u16 = 36;

#[derive(Debug, Clone)]
pub struct SidebarState {
    /// The sidebar takes its column beside the content. Unpinned (the
    /// default) it takes none.
    pub pinned: bool,
    /// Unpinned, the sidebar lies over the content's edge until it rolls
    /// up. Per window, never saved.
    pub overlay: bool,
    /// The edge the sidebar keeps, pinned or as the overlay.
    pub side: SidebarSide,
    pub width: u16,
    pub min_width: u16,
    pub max_width: u16,
    /// Scroll position of each section, by `SidebarSection::index`.
    pub scrolls: [usize; 4],
    /// When each section last scrolled; its thumb stays lit a second after.
    pub scrolled_at: [Option<Instant>; 4],
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
    /// `all_projects` bounds the projects the rows come from.
    pub machine_filter: Option<String>,
    /// List every project instead of only the focused project.
    /// Per window, never saved.
    pub all_projects: bool,
    /// Group agents under project headings when all projects are shown.
    /// Per window, never saved.
    pub all_sessions: bool,
}

impl Default for SidebarState {
    fn default() -> Self {
        Self {
            pinned: false,
            overlay: false,
            side: SidebarSide::Left,
            width: 26,
            min_width: 18,
            max_width: BASE_MAX_WIDTH,
            scrolls: [0; 4],
            scrolled_at: [None; 4],
            selected: 0,
            project_order: Vec::new(),
            expanded_project: None,
            project_labels: BTreeMap::new(),
            machine_filter: Some(crate::ui::sidebar::ALL_MACHINES.to_owned()),
            all_projects: true,
            all_sessions: true,
        }
    }
}

impl SidebarState {
    /// The scroll position of one list section.
    pub fn scroll(&self, section: SidebarSection) -> usize {
        self.scrolls[section.index()]
    }

    /// Move one list section to `offset`; a move that changes it lights that
    /// section's thumb, and one that lands where it was leaves the thumb be.
    pub fn set_scroll(&mut self, section: SidebarSection, offset: usize) {
        let scroll = &mut self.scrolls[section.index()];
        if *scroll != offset {
            *scroll = offset;
            self.scrolled_at[section.index()] = Some(Instant::now());
        }
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
    /// sidebar's edge column, within the width bounds.
    pub fn set_width_from_column(&mut self, area: Rect, column: u16) {
        let width = match self.side {
            SidebarSide::Left => column.saturating_sub(area.x).saturating_add(1),
            SidebarSide::Right => area.right().saturating_sub(column),
        };
        self.width = width.clamp(self.min_width, self.max_width);
    }

    /// Cap the width at the columns `MAX_WIDTH_PIXELS` spans at the
    /// window's cell width, never below `BASE_MAX_WIDTH`.
    pub fn cap_to_window(&mut self, window: WindowSize) {
        let columns = (u32::from(window.columns_rows.width) * MAX_WIDTH_PIXELS)
            .checked_div(u32::from(window.pixels.width))
            .unwrap_or(0);
        self.max_width = u16::try_from(columns)
            .unwrap_or(u16::MAX)
            .max(BASE_MAX_WIDTH);
    }

    /// Split `middle` into the sidebar rect and the content. Pinned, a
    /// `pinned_width` column on `side` beside the content; as the overlay,
    /// `OVERLAY_WIDTH` columns on `side` over content that keeps all of
    /// `middle`; hidden, no columns.
    pub fn layout(&self, middle: Rect, pinned_width: u16) -> (Rect, Rect) {
        let width = if self.pinned {
            pinned_width
        } else if self.overlay {
            OVERLAY_WIDTH.min(middle.width)
        } else {
            0
        };
        let (sidebar_x, content_x) = match self.side {
            SidebarSide::Left => (middle.x, middle.x + width),
            SidebarSide::Right => (middle.right() - width, middle.x),
        };
        let sidebar = Rect::new(sidebar_x, middle.y, width, middle.height);
        if !self.pinned {
            return (sidebar, middle);
        }
        let content = Rect::new(content_x, middle.y, middle.width - width, middle.height);
        (sidebar, content)
    }

    /// The column of `sidebar` that faces the content.
    pub fn edge_x(&self, sidebar: Rect) -> u16 {
        match self.side {
            SidebarSide::Left => sidebar.right().saturating_sub(1),
            SidebarSide::Right => sidebar.x,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_dragged_column_becomes_the_edge_on_either_side() {
        let mut sidebar = SidebarState {
            side: SidebarSide::Right,
            ..SidebarState::default()
        };
        // A right sidebar runs to column 79, so the pointer at 50 widens it
        // to 30; a left one from column 0, so the pointer at 29 does.
        sidebar.set_width_from_column(Rect::new(54, 1, 26, 18), 50);
        assert_eq!(sidebar.width, 30);
        sidebar.side = SidebarSide::Left;
        sidebar.width = 26;
        sidebar.set_width_from_column(Rect::new(0, 1, 26, 18), 29);
        assert_eq!(sidebar.width, 30);
    }

    #[test]
    fn the_width_cap_spans_540_pixels_and_never_drops_below_36() {
        let window = |columns, pixels| WindowSize {
            columns_rows: ratatui::layout::Size::new(columns, 40),
            pixels: ratatui::layout::Size::new(pixels, 800),
        };
        let mut sidebar = SidebarState::default();
        // 8-pixel cells: 540 pixels span 67 whole columns.
        sidebar.cap_to_window(window(200, 1600));
        assert_eq!(sidebar.max_width, 67);
        // 17-pixel cells span 31, under the floor.
        sidebar.cap_to_window(window(100, 1700));
        assert_eq!(sidebar.max_width, 36);
        // A terminal reporting no pixels keeps the floor.
        sidebar.cap_to_window(window(200, 1600));
        sidebar.cap_to_window(window(200, 0));
        assert_eq!(sidebar.max_width, 36);
        // The drag clamps to the new cap.
        sidebar.cap_to_window(window(200, 1600));
        sidebar.set_width_from_column(Rect::new(0, 1, 26, 18), 99);
        assert_eq!(sidebar.width, 67);
    }
}
