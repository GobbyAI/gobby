// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar: terminal roster on top, attention panel below, collapsed rail.
//!
//! herdr geometry kept as is: a `│` separator column on the right, a
//! two-row roster header with a footer row for the `«` toggle, a three-row
//! attention header (rule + title), and the collapsed rail split in half
//! around a `─` divider.

use crate::theme::Palette;
use crate::ui::chrome::{attention_terminal, Chrome, Mode, WorkspaceView};
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::sidebar_rows::{attention_rows, roster_rows, row_line, SidebarRow};
use crate::ui::status::state_dot;
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;

/// herdr `WORKSPACE_SECTION_HEADER_ROWS`.
const ROSTER_HEADER_ROWS: u16 = 2;
/// herdr `AGENT_PANEL_HEADER_ROWS`.
const ATTENTION_HEADER_ROWS: u16 = 3;
/// Roster share of the sidebar when `SidebarState::section_split` is unset.
const DEFAULT_SECTION_SPLIT: f32 = 0.5;

#[derive(Debug, Clone, Default)]
pub struct SidebarHits {
    pub roster: Vec<(String, Rect)>,
    pub attention: Vec<(String, Rect)>,
}

/// Expanded sidebar; returns the row hit areas.
pub fn render_sidebar<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) -> SidebarHits {
    let mut hits = SidebarHits::default();
    if area.width == 0 || area.height == 0 {
        return hits;
    }
    let p = &chrome.palette;
    let is_navigating = chrome.mode == Mode::Navigate;
    frame.render_widget(
        Block::default().style(Style::default().bg(p.panel_bg)),
        area,
    );
    draw_separator_column(
        frame,
        area,
        if is_navigating {
            p.accent
        } else {
            p.surface_dim
        },
    );

    let (roster_area, attention_area) = expanded_sections(area, chrome.sidebar.section_split);
    hits.roster = render_roster(frame, roster_area, ws, chrome, is_navigating);
    hits.attention = render_attention(frame, attention_area, ws, chrome);
    render_toggle(frame, expanded_toggle_rect(area), "«", p);
    hits
}

/// Collapsed rail (`COLLAPSED_WIDTH` columns): state dots and indexes only.
pub fn render_collapsed_sidebar<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) -> SidebarHits {
    let mut hits = SidebarHits::default();
    if area.width == 0 || area.height == 0 {
        return hits;
    }
    let p = &chrome.palette;
    let is_navigating = chrome.mode == Mode::Navigate;
    frame.render_widget(
        Block::default().style(Style::default().bg(p.panel_bg)),
        area,
    );
    draw_separator_column(
        frame,
        area,
        if is_navigating {
            p.accent
        } else {
            p.surface_dim
        },
    );

    let (roster_area, divider_y, attention_area) = collapsed_sections(area);
    if roster_area == Rect::default() {
        render_toggle(frame, collapsed_toggle_rect(area), "»", p);
        return hits;
    }

    let focused = focused_terminal(ws, chrome);
    for (index, row) in roster_rows(ws, chrome).iter().enumerate() {
        let y = roster_area.y + index as u16;
        if y >= roster_area.y + roster_area.height {
            break;
        }
        let (icon, icon_color) = state_dot(row.state, p);
        let selected = row.selected && is_navigating;
        let active = focused.as_deref() == Some(row.id.as_str());
        let (row_style, num_style) = if selected {
            (
                Style::default().bg(p.surface1),
                Style::default().fg(p.overlay1).bg(p.surface1),
            )
        } else if active {
            (
                Style::default().bg(p.surface_dim),
                Style::default().fg(p.text).bg(p.surface_dim),
            )
        } else {
            (Style::default(), Style::default().fg(p.overlay0))
        };
        let rect = Rect::new(roster_area.x, y, roster_area.width, 1);
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(format!("{}", index + 1), num_style),
                Span::styled(" ", row_style),
                Span::styled(icon, Style::default().fg(icon_color)),
            ]))
            .style(row_style),
            rect,
        );
        hits.roster.push((row.id.clone(), rect));
    }

    if let Some(divider_y) = divider_y {
        let buf = frame.buffer_mut();
        for x in roster_area.x..roster_area.x + roster_area.width {
            buf[(x, divider_y)].set_symbol("─");
            buf[(x, divider_y)].set_style(Style::default().fg(p.surface_dim));
        }
    }

    let content = Rect::new(
        attention_area.x,
        attention_area.y,
        attention_area.width,
        attention_area.height.saturating_sub(1),
    );
    if content != Rect::default() {
        for (index, row) in attention_rows(ws, chrome).iter().enumerate() {
            let y = content.y + index as u16;
            if y >= content.y + content.height {
                break;
            }
            let (icon, icon_color) = state_dot(row.state, p);
            let rect = Rect::new(content.x, y, content.width, 1);
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled(format!("{:<2}", index + 1), Style::default().fg(p.overlay0)),
                    Span::styled(icon, Style::default().fg(icon_color)),
                ])),
                rect,
            );
            hits.attention.push((row.id.clone(), rect));
        }
    }

    render_toggle(frame, collapsed_toggle_rect(area), "»", p);
    hits
}

fn render_roster<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
    is_navigating: bool,
) -> Vec<(String, Rect)> {
    let p = &chrome.palette;
    if area.width == 0 || area.height == 0 {
        return Vec::new();
    }
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            " terminals",
            Style::default().fg(p.overlay0).add_modifier(Modifier::BOLD),
        ))),
        Rect::new(area.x, area.y, area.width, 1),
    );
    let mut rows = roster_rows(ws, chrome);
    if !is_navigating {
        for row in &mut rows {
            row.selected = false;
        }
    }
    let viewport = roster_body_rect(area, false).height;
    let metrics = list_metrics(rows.len(), viewport, chrome.sidebar.scroll);
    let body = roster_body_rect(area, should_show_scrollbar(metrics));
    let hits = render_rows(
        frame,
        body,
        &rows,
        metrics,
        chrome,
        focused_terminal(ws, chrome),
    );
    if should_show_scrollbar(metrics) {
        let track = scrollbar_track(area, body);
        render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
    }
    hits
}

fn render_attention<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) -> Vec<(String, Rect)> {
    let p = &chrome.palette;
    if area.width == 0 || area.height < ATTENTION_HEADER_ROWS {
        return Vec::new();
    }
    frame.render_widget(
        Paragraph::new(Span::styled(
            "─".repeat(usize::from(area.width)),
            Style::default().fg(p.surface_dim),
        )),
        Rect::new(area.x, area.y, area.width, 1),
    );
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            " attention",
            Style::default().fg(p.overlay0).add_modifier(Modifier::BOLD),
        ))),
        Rect::new(area.x, area.y + 1, area.width, 1),
    );
    let rows = attention_rows(ws, chrome);
    let viewport = attention_body_rect(area, false).height;
    let metrics = list_metrics(rows.len(), viewport, chrome.sidebar.attention_scroll);
    let body = attention_body_rect(area, should_show_scrollbar(metrics));
    let hits = render_rows(
        frame,
        body,
        &rows,
        metrics,
        chrome,
        focused_terminal(ws, chrome),
    );
    if should_show_scrollbar(metrics) {
        let track = scrollbar_track(area, body);
        render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
    }
    hits
}

/// One line per visible row; selected rows sit on `surface1`, the focused
/// pane's row on `surface_dim` (herdr `render_workspace_list`).
fn render_rows(
    frame: &mut Frame,
    body: Rect,
    rows: &[SidebarRow],
    metrics: ScrollMetrics,
    chrome: &Chrome,
    focused: Option<String>,
) -> Vec<(String, Rect)> {
    let p = &chrome.palette;
    let mut hits = Vec::new();
    if body.width == 0 || body.height == 0 {
        return hits;
    }
    let scroll = metrics
        .max_offset_from_bottom
        .saturating_sub(metrics.offset_from_bottom);
    for (offset, row) in rows
        .iter()
        .skip(scroll)
        .take(usize::from(body.height))
        .enumerate()
    {
        let rect = Rect::new(body.x, body.y + offset as u16, body.width, 1);
        let active = focused.as_deref() == Some(attention_terminal(&row.id));
        let row_style = if row.selected {
            Style::default().bg(p.surface1)
        } else if active {
            Style::default().bg(p.surface_dim)
        } else {
            Style::default()
        };
        frame.render_widget(
            Paragraph::new(row_line(row, body.width, chrome)).style(row_style),
            rect,
        );
        hits.push((row.id.clone(), rect));
    }
    hits
}

/// Terminal shown in the focused pane of the active tab.
fn focused_terminal<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Option<String> {
    chrome
        .focused_pane()
        .map(|id| ws.pane(id).terminal_id.clone())
}

/// herdr `workspace_list_scroll_metrics` for fixed one-line rows.
fn list_metrics(len: usize, viewport: u16, requested: usize) -> ScrollMetrics {
    let viewport = usize::from(viewport);
    let max_scroll = len.saturating_sub(viewport);
    let scroll = requested.min(max_scroll);
    ScrollMetrics {
        offset_from_bottom: max_scroll.saturating_sub(scroll),
        max_offset_from_bottom: max_scroll,
        viewport_rows: viewport.min(len),
    }
}

fn scrollbar_track(area: Rect, body: Rect) -> Rect {
    Rect::new(
        area.x + area.width.saturating_sub(1),
        body.y,
        1,
        body.height,
    )
}

/// herdr `sidebar_section_heights`; `split` overrides the ratio with an
/// explicit roster row count, clamped the same way.
fn section_heights(total_h: u16, split: Option<u16>) -> (u16, u16) {
    if total_h == 0 {
        return (0, 0);
    }
    if total_h < 6 {
        let roster_h = total_h.div_ceil(2);
        return (roster_h, total_h.saturating_sub(roster_h));
    }
    let roster_h =
        split.unwrap_or_else(|| (f32::from(total_h) * DEFAULT_SECTION_SPLIT).round() as u16);
    let roster_h = roster_h.clamp(3, total_h.saturating_sub(3));
    (roster_h, total_h.saturating_sub(roster_h))
}

/// herdr `expanded_sidebar_sections`: content excludes the separator column.
fn expanded_sections(area: Rect, split: Option<u16>) -> (Rect, Rect) {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    if content.width == 0 || content.height == 0 {
        return (Rect::default(), Rect::default());
    }
    let (roster_h, attention_h) = section_heights(content.height, split);
    (
        Rect::new(content.x, content.y, content.width, roster_h),
        Rect::new(content.x, content.y + roster_h, content.width, attention_h),
    )
}

/// herdr `collapsed_sidebar_sections`: roster, divider row, attention.
fn collapsed_sections(area: Rect) -> (Rect, Option<u16>, Rect) {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    if content.width == 0 || content.height == 0 {
        return (Rect::default(), None, Rect::default());
    }
    if content.height < 7 {
        return (content, None, Rect::default());
    }
    let roster_h = content.height.div_ceil(2);
    let attention_h = content.height.saturating_sub(roster_h + 1);
    if attention_h == 0 {
        return (content, None, Rect::default());
    }
    let divider_y = content.y + roster_h;
    (
        Rect::new(content.x, content.y, content.width, roster_h),
        Some(divider_y),
        Rect::new(content.x, divider_y + 1, content.width, attention_h),
    )
}

/// herdr `workspace_list_body_rect`: below the header, above the footer row.
fn roster_body_rect(area: Rect, has_scrollbar: bool) -> Rect {
    if area.width == 0 || area.height <= ROSTER_HEADER_ROWS {
        return Rect::default();
    }
    let body_y = area.y + ROSTER_HEADER_ROWS;
    let footer_y = area.y + area.height.saturating_sub(1);
    Rect::new(
        area.x,
        body_y,
        area.width.saturating_sub(u16::from(has_scrollbar)),
        footer_y.saturating_sub(body_y),
    )
}

/// herdr `agent_panel_body_rect`.
fn attention_body_rect(area: Rect, has_scrollbar: bool) -> Rect {
    if area.width == 0 || area.height <= ATTENTION_HEADER_ROWS {
        return Rect::default();
    }
    let body_y = area.y + ATTENTION_HEADER_ROWS;
    Rect::new(
        area.x,
        body_y,
        area.width.saturating_sub(u16::from(has_scrollbar)),
        (area.y + area.height).saturating_sub(body_y),
    )
}

/// herdr `expanded_sidebar_toggle_rect`.
fn expanded_toggle_rect(area: Rect) -> Rect {
    if area.width <= 1 || area.height == 0 {
        return Rect::default();
    }
    Rect::new(
        area.x + area.width.saturating_sub(2),
        area.y + area.height.saturating_sub(1),
        1,
        1,
    )
}

/// herdr `collapsed_sidebar_toggle_rect`.
fn collapsed_toggle_rect(area: Rect) -> Rect {
    let content_w = area.width.saturating_sub(1);
    if content_w == 0 || area.height == 0 {
        return Rect::default();
    }
    Rect::new(
        area.x + content_w / 2,
        area.y + area.height.saturating_sub(1),
        1,
        1,
    )
}

fn render_toggle(frame: &mut Frame, rect: Rect, icon: &str, p: &Palette) {
    if rect == Rect::default() {
        return;
    }
    frame.render_widget(
        Paragraph::new(Span::styled(icon, Style::default().fg(p.overlay0))),
        rect,
    );
}

fn draw_separator_column(frame: &mut Frame, area: Rect, color: Color) {
    let sep_x = area.x + area.width.saturating_sub(1);
    let buf = frame.buffer_mut();
    for y in area.y..area.y + area.height {
        buf[(sep_x, y)].set_symbol("│");
        buf[(sep_x, y)].set_style(Style::default().fg(color));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use serde_json::json;

    fn scripted_workspace() -> Workspace {
        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
        }));
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-alpha", "native", "epoch").unwrap();
        ws.open_terminal("term-beta", "native", "epoch").unwrap();
        ws
    }

    fn screen(terminal: &Terminal<TestBackend>) -> String {
        let buffer = terminal.backend().buffer();
        let width = usize::from(buffer.area.width);
        let cells: Vec<String> = buffer
            .content()
            .iter()
            .map(|c| c.symbol().to_string())
            .collect();
        cells
            .chunks(width)
            .map(|row| row.concat())
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn section_heights_follow_herdr_clamps() {
        assert_eq!(section_heights(0, None), (0, 0));
        assert_eq!(section_heights(5, None), (3, 2));
        assert_eq!(section_heights(40, None), (20, 20));
        assert_eq!(section_heights(40, Some(30)), (30, 10));
        assert_eq!(section_heights(40, Some(50)), (37, 3));
        assert_eq!(section_heights(40, Some(0)), (3, 37));
    }

    #[test]
    fn expanded_sidebar_lists_roster_and_attention_with_hits() {
        let ws = scripted_workspace();
        let mut chrome = Chrome::dark();
        chrome.mode = Mode::Navigate;
        chrome.sidebar.selected = 1;
        let area = Rect::new(0, 0, 26, 40);
        let mut terminal = Terminal::new(TestBackend::new(26, 40)).unwrap();
        let mut hits = SidebarHits::default();
        terminal
            .draw(|frame| hits = render_sidebar(frame, area, &ws, &chrome))
            .unwrap();
        let text = screen(&terminal);
        for needle in [
            "terminals",
            "term-alpha",
            "term-beta",
            "attention",
            "blocked",
            "«",
        ] {
            assert!(text.contains(needle), "missing {needle:?}:\n{text}");
        }
        assert!(!text.contains('!'), "{text}");
        assert_eq!(hits.roster.len(), 2);
        assert_eq!(hits.roster[0].0, "term-alpha");
        assert_eq!(hits.roster[0].1, Rect::new(0, 2, 25, 1));
        assert_eq!(
            hits.attention,
            vec![("run:term-alpha".to_string(), Rect::new(0, 23, 25, 1))]
        );
        let selected_line = text.lines().nth(3).unwrap_or_default();
        assert!(
            selected_line.starts_with("▸○ term-beta"),
            "{selected_line:?}"
        );
    }

    #[test]
    fn collapsed_rail_shows_indexes_and_dots() {
        let ws = scripted_workspace();
        let chrome = Chrome::dark();
        let area = Rect::new(0, 0, 4, 12);
        let mut terminal = Terminal::new(TestBackend::new(4, 12)).unwrap();
        let mut hits = SidebarHits::default();
        terminal
            .draw(|frame| hits = render_collapsed_sidebar(frame, area, &ws, &chrome))
            .unwrap();
        let text = screen(&terminal);
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines[0], "1 ●│");
        assert_eq!(lines[1], "2 ○│");
        assert_eq!(lines[6], "───│");
        assert_eq!(lines[7], "1 ●│");
        assert_eq!(lines[11], " » │");
        assert_eq!(hits.roster.len(), 2);
        assert_eq!(hits.attention.len(), 1);
    }

    #[test]
    fn roster_scroll_clamps_to_the_last_page() {
        let metrics = list_metrics(10, 4, 99);
        assert_eq!(metrics.max_offset_from_bottom, 6);
        assert_eq!(metrics.offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 4);
        assert!(!should_show_scrollbar(list_metrics(3, 4, 0)));
    }
}
