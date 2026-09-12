// upstream: herdr v0.8.0 src/ui/panes.rs
//! Pane chrome: borders, titles with control-state indicator, focus ring,
//! scrollbars, and the content hook the app shell fills.

use crate::app::{Pane, PaneId};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, WorkspaceView};
use crate::ui::pane_layout::{self, PaneInfo, SplitBorder};
use crate::ui::scrollbar::render_pane_scrollbar;
use crate::ui::status::control_glyph_label;
use crate::ui::text::truncate_end;
use gobby_terminal::layout::ScrollMetrics;
use gobby_terminal::selection::Selection;
use ratatui::layout::{Alignment, Direction, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;
use std::collections::HashMap;

/// Content painter supplied by the app shell: draws one pane's terminal
/// grid into its inner rect.
pub type PaneContent<'a> = dyn FnMut(&mut Frame, Rect, PaneId) + 'a;

/// Title text for a pane: display name, backend, and the control indicator.
/// The border is the one place the address is left out — it is the first thing
/// a narrow pane truncates away, and the status bar carries it unconditionally.
pub fn pane_title(pane: &Pane) -> String {
    let (glyph, label) = control_glyph_label(pane.control, pane.take_back);
    format!(
        "{} · {} · {glyph} {label}",
        pane.display_name(),
        pane.backend
    )
}

/// Border label for a pane: padded, truncated to the top edge, and marked
/// with "▸" when focused.
pub fn pane_border_title(label: &str, pane_width: u16, focused: bool) -> Option<String> {
    let label = label.trim();
    if label.is_empty() || pane_width <= 4 {
        return None;
    }
    let max_label_width = pane_width.saturating_sub(4) as usize;
    if focused {
        let label = truncate_end(label, max_label_width.saturating_sub(2));
        Some(format!(" ▸ {label} "))
    } else {
        Some(format!(" {} ", truncate_end(label, max_label_width)))
    }
}

/// Render every pane of the active tab from `chrome.view.pane_infos`,
/// then call `content` for each inner rect.
pub fn render_panes<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) {
    let Some(tab) = chrome.active_tab() else {
        return;
    };
    let multi_pane = tab.layout.pane_count() > 1;
    let terminal_active = chrome.mode == Mode::Terminal;

    let mut resolved: Vec<PaneInfo> = Vec::with_capacity(chrome.view.pane_infos.len());
    let mut titles: Vec<Option<String>> = Vec::with_capacity(chrome.view.pane_infos.len());
    for info in &chrome.view.pane_infos {
        let Some(pane_id) = tab.slots.get(&info.id).copied() else {
            continue;
        };
        let pane = ws.pane(pane_id);
        let metrics =
            pane_layout::metrics_for(pane.scroll_offset, pane.max_scroll, info.inner_rect.height);
        let mut info = info.clone();
        info.scrollbar_rect = pane_layout::scrollbar_gutter(
            pane_layout::pane_inner_rect(info.rect, info.borders),
            chrome.prefs.pane_scrollbars,
            metrics,
        );

        content(frame, info.inner_rect, pane_id);
        render_pane_note(frame, info.inner_rect, pane, &chrome.palette);
        if let Some(selection) = chrome
            .selection
            .as_ref()
            .filter(|selection| selection.pane_id == info.id)
        {
            highlight_selection(frame, selection, info.inner_rect, metrics, &chrome.palette);
        }
        render_pane_scrollbar(frame, &info, metrics, &chrome.palette);

        let should_dim = !info.is_focused && multi_pane && !terminal_active;
        if should_dim {
            let inner = info.inner_rect;
            let buf = frame.buffer_mut();
            for y in inner.y..inner.y + inner.height {
                for x in inner.x..inner.x + inner.width {
                    let cell = &mut buf[(x, y)];
                    cell.set_style(cell.style().add_modifier(Modifier::DIM));
                }
            }
        }

        titles.push(Some(pane_title(pane)));
        resolved.push(info);
    }

    render_pane_borders(
        chrome,
        &resolved,
        &chrome.view.split_borders,
        &titles,
        frame,
    );
}

/// One uniform style for every selected cell, so the selection reads the
/// same over whatever the terminal drew there (herdr
/// `automatic_selection_style`). herdr mixes the probed host background in;
/// gclient hosts terminals on its own token map, so the palette alone fixes
/// it: `text` on `surface1` is a contract-checked AA pair.
pub fn selection_style(p: &Palette) -> Style {
    Style::reset().fg(p.text).bg(p.surface1)
}

/// Paint the selection style over the inner cells it covers. The selection
/// stores screen-buffer rows, so `metrics` maps them onto the viewport the
/// same way the scrollbar does.
pub fn highlight_selection(
    frame: &mut Frame,
    selection: &Selection,
    inner: Rect,
    metrics: ScrollMetrics,
    palette: &Palette,
) {
    if !selection.is_visible() {
        return;
    }
    let style = selection_style(palette);
    let buf = frame.buffer_mut();
    for y in inner.y..inner.y + inner.height {
        for x in inner.x..inner.x + inner.width {
            if selection.contains(y - inner.y, x - inner.x, Some(metrics)) {
                buf[(x, y)].set_style(style);
            }
        }
    }
}

/// Placeholder body when no pane is open.
pub fn render_empty(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    let p = &chrome.palette;
    frame.render_widget(
        Block::default().style(Style::default().bg(p.panel_bg)),
        area,
    );
    if area.height < 2 || area.width < 8 {
        return;
    }
    let lines = vec![
        Line::styled(
            "No pane open.",
            Style::default().fg(p.overlay1).add_modifier(Modifier::BOLD),
        ),
        Line::styled(
            "select a terminal in the sidebar to attach",
            Style::default().fg(p.overlay0),
        ),
    ];
    let rect = Rect::new(
        area.x,
        area.y + area.height.saturating_sub(2) / 2,
        area.width,
        2,
    );
    frame.render_widget(Paragraph::new(lines).alignment(Alignment::Center), rect);
}

/// What a pane body says when its grid cannot be painted, and who sizes
/// it when that is not gclient. `grid::render` stays silent on a missing or
/// malformed frame, so this names the reason instead of leaving the body
/// blank; a refused size claim keeps the crop and adds a one-line note.
fn render_pane_note(frame: &mut Frame, area: Rect, pane: &Pane, p: &Palette) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    let muted = Style::default().fg(p.overlay0);
    let body = match pane.latest_frame() {
        Some(grid) if grid.cells.len() != usize::from(grid.width) * usize::from(grid.height) => {
            tracing::debug!(
                pane = pane.id.0,
                width = grid.width,
                height = grid.height,
                cells = grid.cells.len(),
                "frame_size_mismatch"
            );
            Some(format!(
                "frame_size_mismatch {}x{}/{}",
                grid.width,
                grid.height,
                grid.cells.len()
            ))
        }
        None if pane.frame_source().is_some() => Some("waiting for frames".to_string()),
        _ => None,
    };
    if let Some(text) = body {
        let rect = Rect::new(
            area.x,
            area.y + area.height.saturating_sub(1) / 2,
            area.width,
            1,
        );
        frame.render_widget(
            Paragraph::new(Line::styled(text, muted)).alignment(Alignment::Center),
            rect,
        );
    }
    if let Some(viewer) = pane.sized_by.as_deref() {
        let rect = Rect::new(area.x, area.y + area.height - 1, area.width, 1);
        frame.render_widget(
            Paragraph::new(Line::styled(format!("sized by {viewer}"), muted))
                .alignment(Alignment::Center),
            rect,
        );
    }
}

#[derive(Clone, Copy, Default)]
struct LineCell {
    up: bool,
    down: bool,
    left: bool,
    right: bool,
}

/// Draw pane borders as one line grid (junctions composed across panes and
/// split dividers), the focused pane's lines in the accent, then each
/// pane's border title.
pub fn render_pane_borders(
    chrome: &Chrome,
    pane_infos: &[PaneInfo],
    split_borders: &[SplitBorder],
    titles: &[Option<String>],
    frame: &mut Frame,
) {
    let pane_gaps = chrome.prefs.pane_gaps;
    if !chrome.prefs.pane_borders || pane_infos.iter().all(|info| info.borders.is_empty()) {
        return;
    }

    let mut cells = HashMap::<(u16, u16), LineCell>::new();
    for info in pane_infos {
        add_pane_border_cells(&mut cells, info);
    }
    add_split_border_cells(pane_gaps, split_borders, &mut cells);

    let buf = frame.buffer_mut();
    let area = buf.area;
    for ((x, y), line) in cells {
        if x < area.x
            || x >= area.x.saturating_add(area.width)
            || y < area.y
            || y >= area.y.saturating_add(area.height)
        {
            continue;
        }
        let focused = pane_infos
            .iter()
            .any(|info| info.is_focused && line_touches_pane(x, y, info, pane_gaps));
        let symbol = line_cell_symbol(line);
        if symbol.is_empty() {
            continue;
        }
        let cell = &mut buf[(x, y)];
        cell.set_symbol(symbol);
        let color = if focused {
            chrome.palette.accent
        } else {
            chrome.palette.overlay0
        };
        cell.set_style(Style::default().fg(color));
    }

    render_pane_border_titles(&chrome.palette, pane_infos, titles, frame);
}

fn add_split_border_cells(
    pane_gaps: bool,
    split_borders: &[SplitBorder],
    cells: &mut HashMap<(u16, u16), LineCell>,
) {
    if pane_gaps {
        return;
    }

    for split in split_borders {
        match split.direction {
            Direction::Horizontal => {
                let x = split.pos;
                let end = split.area.y.saturating_add(split.area.height);
                for y in split.area.y..=end {
                    if !cells.contains_key(&(x, y)) {
                        continue;
                    }
                    let left = x
                        .checked_sub(1)
                        .and_then(|left_x| cells.get(&(left_x, y)))
                        .is_some_and(|cell| cell.left || cell.right);
                    let right = cells
                        .get(&(x.saturating_add(1), y))
                        .is_some_and(|cell| cell.left || cell.right);
                    let cell = cells.entry((x, y)).or_default();
                    cell.up |= y > split.area.y;
                    cell.down |= y + 1 < end;
                    cell.left |= left;
                    cell.right |= right;
                }
            }
            Direction::Vertical => {
                let y = split.pos;
                let end = split.area.x.saturating_add(split.area.width);
                for x in split.area.x..=end {
                    if !cells.contains_key(&(x, y)) {
                        continue;
                    }
                    let up = y
                        .checked_sub(1)
                        .and_then(|up_y| cells.get(&(x, up_y)))
                        .is_some_and(|cell| cell.up || cell.down);
                    let down = cells
                        .get(&(x, y.saturating_add(1)))
                        .is_some_and(|cell| cell.up || cell.down);
                    let cell = cells.entry((x, y)).or_default();
                    cell.left |= x > split.area.x;
                    cell.right |= x + 1 < end;
                    cell.up |= up;
                    cell.down |= down;
                }
            }
        }
    }
}

fn add_pane_border_cells(cells: &mut HashMap<(u16, u16), LineCell>, info: &PaneInfo) {
    let rect = info.rect;
    if rect.width == 0 || rect.height == 0 {
        return;
    }
    let right = rect.x.saturating_add(rect.width).saturating_sub(1);
    let bottom = rect.y.saturating_add(rect.height).saturating_sub(1);

    if info.borders.contains(Borders::TOP) {
        for x in rect.x..=right {
            let cell = cells.entry((x, rect.y)).or_default();
            cell.left |= x > rect.x;
            cell.right |= x < right;
        }
    }
    if info.borders.contains(Borders::BOTTOM) {
        for x in rect.x..=right {
            let cell = cells.entry((x, bottom)).or_default();
            cell.left |= x > rect.x;
            cell.right |= x < right;
        }
    }
    if info.borders.contains(Borders::LEFT) {
        for y in rect.y..=bottom {
            let cell = cells.entry((rect.x, y)).or_default();
            cell.up |= y > rect.y;
            cell.down |= y < bottom;
        }
    }
    if info.borders.contains(Borders::RIGHT) {
        for y in rect.y..=bottom {
            let cell = cells.entry((right, y)).or_default();
            cell.up |= y > rect.y;
            cell.down |= y < bottom;
        }
    }
}

fn line_touches_pane(x: u16, y: u16, info: &PaneInfo, pane_gaps: bool) -> bool {
    let rect = info.rect;
    if rect.width == 0 || rect.height == 0 {
        return false;
    }
    let right = rect.x.saturating_add(rect.width).saturating_sub(1);
    let bottom = rect.y.saturating_add(rect.height).saturating_sub(1);
    let in_rows = y >= rect.y && y <= bottom;
    let in_cols = x >= rect.x && x <= right;
    let own_border =
        (in_rows && (x == rect.x || x == right)) || (in_cols && (y == rect.y || y == bottom));

    if pane_gaps {
        return own_border;
    }

    let shared_right = rect.x.saturating_add(rect.width);
    let shared_bottom = rect.y.saturating_add(rect.height);
    own_border
        || (in_rows && x == shared_right)
        || (in_cols && y == shared_bottom)
        || (x == shared_right && y == shared_bottom)
}

fn render_pane_border_titles(
    p: &Palette,
    pane_infos: &[PaneInfo],
    titles: &[Option<String>],
    frame: &mut Frame,
) {
    let buf = frame.buffer_mut();
    let area = buf.area;
    for (info, label) in pane_infos.iter().zip(titles) {
        if !info.borders.contains(Borders::TOP) || info.rect.width <= 4 {
            continue;
        }
        let Some(title) = label
            .as_deref()
            .and_then(|label| pane_border_title(label, info.rect.width, info.is_focused))
        else {
            continue;
        };
        let y = info.rect.y;
        if y < area.y || y >= area.y.saturating_add(area.height) {
            continue;
        }
        let start_x = info.rect.x.saturating_add(1);
        let end_x = info
            .rect
            .x
            .saturating_add(info.rect.width)
            .saturating_sub(1)
            .min(area.x.saturating_add(area.width));
        if start_x >= end_x {
            continue;
        }
        let color = if info.is_focused {
            p.accent
        } else {
            p.overlay0
        };
        let mut style = Style::default().fg(color);
        if info.is_focused {
            style = style.add_modifier(Modifier::BOLD);
        }
        buf.set_stringn(
            start_x,
            y,
            title,
            end_x.saturating_sub(start_x) as usize,
            style,
        );
    }
}

fn line_cell_symbol(line: LineCell) -> &'static str {
    match (line.up, line.down, line.left, line.right) {
        (true, true, true, true) => "┼",
        (true, true, true, false) => "┤",
        (true, true, false, true) => "├",
        (true, false, true, true) => "┴",
        (false, true, true, true) => "┬",
        (true, true, false, false) | (true, false, false, false) | (false, true, false, false) => {
            "│"
        }
        (false, false, true, true) | (false, false, true, false) | (false, false, false, true) => {
            "─"
        }
        (false, true, false, true) => "┌",
        (false, true, true, false) => "┐",
        (true, false, false, true) => "└",
        (true, false, true, false) => "┘",
        _ => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crate::ui::text::display_width;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use serde_json::json;

    fn scripted() -> (Workspace, Chrome) {
        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
        }));
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-alpha", "native", "epoch").unwrap();
        ws.open_terminal("term-beta", "native", "epoch").unwrap();
        let mut chrome = Chrome::dark();
        let alpha = ws.pane_for_terminal("term-alpha").unwrap();
        let beta = ws.pane_for_terminal("term-beta").unwrap();
        chrome.open_pane(alpha, "alpha");
        chrome.open_pane(beta, "alpha");
        (ws, chrome)
    }

    fn screen(terminal: &Terminal<TestBackend>) -> String {
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect()
    }

    #[test]
    fn border_title_marks_focus_and_truncates_to_the_top_edge() {
        assert_eq!(pane_border_title("alpha", 12, false).unwrap(), " alpha ");
        assert_eq!(pane_border_title("alpha", 12, true).unwrap(), " ▸ alpha ");
        assert!(pane_border_title("  ", 12, true).is_none());
        assert!(pane_border_title("alpha", 4, false).is_none());
        let long = pane_border_title("a-very-long-terminal-title", 14, true).unwrap();
        assert!(display_width(&long) <= 14, "{long}");
        assert!(long.ends_with("… "));
    }

    #[test]
    fn render_panes_draws_titles_and_focus_marker() {
        let (ws, mut chrome) = scripted();
        let area = Rect::new(0, 0, 120, 40);
        chrome.compute_view(&ws, area);
        assert_eq!(chrome.view.pane_infos.len(), 2);

        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        let mut painted = Vec::new();
        terminal
            .draw(|frame| {
                render_panes(frame, &ws, &chrome, &mut |_, rect, id| {
                    painted.push((id, rect))
                });
            })
            .unwrap();
        let text = screen(&terminal);
        assert_eq!(painted.len(), 2);
        assert!(painted
            .iter()
            .all(|(_, rect)| rect.width > 0 && rect.height > 0));
        for needle in ["term-alpha", "term-beta", "observe", "▸", "┌", "┐"] {
            assert!(text.contains(needle), "frame lacks {needle:?}:\n{text}");
        }
        assert!(!text.contains('!'));
    }

    #[test]
    fn empty_state_names_the_next_step_without_exclamation() {
        let chrome = Chrome::dark();
        let mut terminal = Terminal::new(TestBackend::new(60, 10)).unwrap();
        terminal
            .draw(|frame| render_empty(frame, frame.area(), &chrome))
            .unwrap();
        let text = screen(&terminal);
        assert!(text.contains("No pane open."));
        assert!(!text.contains('!'));
    }
}
