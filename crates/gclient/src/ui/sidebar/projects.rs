// upstream: none (herdr src/client/shell/sidebar.rs workspace cards)
//! The projects section of the expanded sidebar: a band with the
//! `[working]`/`[all]` filter control, one one-line card per listed
//! project with the expanded card's worktree rows under it, and the
//! variable-height scroll metrics the lists need.

use super::{render_band, render_section_rows, BandStyle, SidebarHits};
use crate::ui::chrome::Chrome;
use crate::ui::hit::SidebarSection;
use crate::ui::sidebar_rows::SidebarRow;
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::Frame;

/// The band's filter control while the working projects are listed.
pub const WORKING_LABEL: &str = "[working]";
/// The band's filter control while every project is listed.
pub const ALL_PROJECTS_LABEL: &str = "[all]";

/// What the projects band's filter control reads.
pub fn projects_filter_label(chrome: &Chrome) -> &'static str {
    if chrome.sidebar.all_projects {
        ALL_PROJECTS_LABEL
    } else {
        WORKING_LABEL
    }
}

/// Draw the section into `area` (the content rect, without the separator
/// column) and record its hits.
pub(super) fn render_projects(
    frame: &mut Frame,
    area: Rect,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let section = SidebarSection::Projects;
    let (_, controls) = render_band(
        frame,
        area,
        section.title(),
        &[projects_filter_label(chrome)],
        BandStyle::section(&chrome.palette),
    );
    hits.projects_filter = controls.first().copied();
    render_section_rows(frame, area, section, rows, chrome, hits);
}

/// `list_metrics` for entries of varying `heights`: the largest scroll is
/// the first entry from which the rest fit the viewport, and the viewport
/// holds the entries that fit whole from the scrolled-to one.
pub fn project_list_metrics(heights: &[u16], viewport: u16, requested: usize) -> ScrollMetrics {
    let viewport = usize::from(viewport);
    let total = |from: usize| -> usize { heights[from..].iter().map(|h| usize::from(*h)).sum() };
    let max_scroll = (0..=heights.len())
        .find(|&from| total(from) <= viewport)
        .unwrap_or(heights.len());
    let scroll = requested.min(max_scroll);
    let mut used = 0;
    let mut visible = 0;
    for height in &heights[scroll..] {
        used += usize::from(*height);
        if used > viewport {
            break;
        }
        visible += 1;
    }
    ScrollMetrics {
        offset_from_bottom: max_scroll - scroll,
        max_offset_from_bottom: max_scroll,
        viewport_rows: visible,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::scrollbar::should_show_scrollbar;

    #[test]
    fn project_list_metrics_scroll_by_whole_cards() {
        // Four two-line rows in a five-line viewport: two fit, so the list
        // scrolls two entries at most and shows two whole rows.
        let metrics = project_list_metrics(&[2, 2, 2, 2], 5, 99);
        assert_eq!(metrics.max_offset_from_bottom, 2);
        assert_eq!(metrics.offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 2);
        // A row with two one-line rows under it fits with the next row.
        let metrics = project_list_metrics(&[2, 1, 1, 2], 6, 0);
        assert_eq!(metrics.max_offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 4);
        assert!(!should_show_scrollbar(metrics));
        assert_eq!(project_list_metrics(&[], 4, 3).viewport_rows, 0);
    }
}
