// upstream: none (herdr src/client/shell/sidebar.rs workspace cards)
//! The projects section of the expanded sidebar: a three-row header, one
//! two-line card per project with its worktree rows under it, a footer row
//! with the `new` and `menu` controls, and the variable-height scroll
//! metrics the cards need.

use super::{header_rows, render_header, scrollbar_track, section_body_rect, SidebarHits};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::sidebar_rows::{project_rows, row_line, row_second_line, RowKind, SidebarRow};
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::Span;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

const NEW_LABEL: &str = " new";
const MENU_LABEL: &str = "menu";
/// Columns the footer needs before it draws: both labels and a gap.
const FOOTER_MIN_WIDTH: u16 = 10;

/// Draw the section into `area` (the content rect, without the separator
/// column) and record its hits.
pub(super) fn render_projects<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
    is_navigating: bool,
    hits: &mut SidebarHits,
) {
    let p = &chrome.palette;
    if render_header(frame, area, SidebarSection::Projects, p).is_none() {
        return;
    }
    let mut rows = project_rows(ws, chrome);
    if !is_navigating {
        for row in &mut rows {
            row.selected = false;
        }
    }
    let heights: Vec<u16> = rows.iter().map(SidebarRow::height).collect();
    let section = SidebarSection::Projects;
    let viewport = section_body_rect(section, area, false).height;
    let metrics = project_list_metrics(&heights, viewport, chrome.sidebar.scroll(section));
    let body = section_body_rect(section, area, should_show_scrollbar(metrics));
    render_cards(frame, body, &rows, metrics, chrome, hits);
    if should_show_scrollbar(metrics) {
        let track = scrollbar_track(area, body);
        render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
        hits.scrollbars[section.index()] = Some(track);
    }
    if chrome.prefs.mouse_capture {
        render_footer(frame, area, p, hits);
    }
}

/// Cards from the scroll offset down while they fit whole; a selected card
/// sits on `surface1`, the focused project's on `surface_dim`.
fn render_cards(
    frame: &mut Frame,
    body: Rect,
    rows: &[SidebarRow],
    metrics: ScrollMetrics,
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let p = &chrome.palette;
    if body.width == 0 || body.height == 0 {
        return;
    }
    let scroll = metrics
        .max_offset_from_bottom
        .saturating_sub(metrics.offset_from_bottom);
    let mut y = body.y;
    for row in rows.iter().skip(scroll) {
        let height = row.height();
        if y + height > body.bottom() {
            break;
        }
        let rect = Rect::new(body.x, y, body.width, height);
        let row_style = if row.selected {
            Style::default().bg(p.surface1)
        } else if row.active {
            Style::default().bg(p.surface_dim)
        } else {
            Style::default()
        };
        frame.render_widget(
            Paragraph::new(row_line(row, body.width, chrome)).style(row_style),
            Rect::new(body.x, y, body.width, 1),
        );
        if row.kind == RowKind::Project {
            frame.render_widget(
                Paragraph::new(row_second_line(row, body.width, chrome)).style(row_style),
                Rect::new(body.x, y + 1, body.width, 1),
            );
            hits.projects.push((row.id.clone(), rect));
            if row.group.is_some() {
                hits.group_toggles.push((
                    row.id.clone(),
                    Rect::new(body.right().saturating_sub(1), y, 1, 1),
                ));
            }
        } else {
            hits.worktrees.push((row.id.clone(), rect));
        }
        y += height;
    }
}

/// The footer row: ` new` at the left edge, `menu` at the right.
fn render_footer(frame: &mut Frame, area: Rect, p: &Palette, hits: &mut SidebarHits) {
    if area.width < FOOTER_MIN_WIDTH || area.height <= header_rows(SidebarSection::Projects) {
        return;
    }
    let y = area.bottom().saturating_sub(1);
    let style = Style::default().fg(p.overlay0);
    let new_rect = Rect::new(area.x, y, NEW_LABEL.len() as u16, 1);
    frame.render_widget(Paragraph::new(Span::styled(NEW_LABEL, style)), new_rect);
    hits.projects_new = Some(new_rect);
    let menu_x = area.right().saturating_sub(MENU_LABEL.len() as u16);
    let menu_rect = Rect::new(menu_x, y, MENU_LABEL.len() as u16, 1);
    frame.render_widget(Paragraph::new(Span::styled(MENU_LABEL, style)), menu_rect);
    hits.projects_menu = Some(menu_rect);
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

    #[test]
    fn project_list_metrics_scroll_by_whole_cards() {
        // Four two-line cards in a five-line viewport: two fit, so the list
        // scrolls two entries at most and shows two whole cards.
        let metrics = project_list_metrics(&[2, 2, 2, 2], 5, 99);
        assert_eq!(metrics.max_offset_from_bottom, 2);
        assert_eq!(metrics.offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 2);
        // A card with two one-line worktree rows fits with the next card.
        let metrics = project_list_metrics(&[2, 1, 1, 2], 6, 0);
        assert_eq!(metrics.max_offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 4);
        assert!(!should_show_scrollbar(metrics));
        assert_eq!(project_list_metrics(&[], 4, 3).viewport_rows, 0);
    }
}
