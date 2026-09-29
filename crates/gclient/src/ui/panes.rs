// upstream: herdr v0.8.0 src/ui/panes.rs
//! Pane chrome: borders, header titles, edge metadata, focus ring,
//! scrollbars, and the content hook the app shell fills.

use crate::app::{Pane, PaneId};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, WorkspaceView};
use crate::ui::hit::Hit;
use crate::ui::marks::{self, MarkPalette};
use crate::ui::pane_chrome::{
    self, address_rect, exited, pane_corners, title_budget, title_rect, top_reserve, MetadataTone,
    PaneCorners,
};
use crate::ui::pane_layout::{self, PaneInfo, SplitBorder};
use crate::ui::scrollbar::render_pane_scrollbar;
use crate::ui::sidebar_rows::ticker_window;
use crate::ui::text::{display_width, truncate_end};
use gobby_terminal::layout::ScrollMetrics;
use gobby_terminal::selection::Selection;
use ratatui::layout::{Alignment, Direction, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Borders, Paragraph};
use ratatui::Frame;
use std::collections::HashMap;

/// Content painter supplied by the app shell: draws one pane's terminal
/// grid into its inner rect.
pub type PaneContent<'a> = dyn FnMut(&mut Frame, Rect, PaneId) + 'a;

/// Border label for a pane: padded and truncated to the top edge.
pub fn pane_border_title(label: &str, pane_width: u16) -> Option<String> {
    let label = label.trim();
    if label.is_empty() || pane_width <= 4 {
        return None;
    }
    Some(frame_title(&truncate_end(
        label,
        title_budget(pane_width, 0),
    )))
}

fn frame_title(label: &str) -> String {
    format!(" {label} ")
}

/// A live header: the title window at the shared ticker, beside whatever
/// top-edge room the pane's metadata keeps.
fn header_title(
    label: &str,
    info: &PaneInfo,
    corners: &PaneCorners,
    chrome: &Chrome,
    max_travel: usize,
) -> Option<String> {
    let label = label.trim();
    if label.is_empty() || info.rect.width <= 4 {
        return None;
    }
    let budget = title_budget(info.rect.width, top_reserve(info, corners));
    let window = ticker_window(
        label,
        budget,
        chrome.ticker,
        max_travel,
        chrome.prefs.title_scrolling,
    );
    Some(frame_title(&window))
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
    let mut labels: Vec<PaneCorners> = Vec::with_capacity(chrome.view.pane_infos.len());
    let mut frame_states = Vec::with_capacity(chrome.view.pane_infos.len());
    // One ticker period for every scrolling title in the frame (D7).
    let mut max_travel = chrome.view.title_travel;
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
        render_pane_scrollbar(frame, &info, metrics, &chrome.palette, pane.scrolled_at);

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

        max_travel = max_travel.max(pane_chrome::title_travel(ws, chrome, pane, &info));
        labels.push(pane_corners(ws, chrome, pane, info.is_focused));
        frame_states.push(frame_state(ws, pane));
        resolved.push(info);
    }

    if !render_border_lines(
        chrome,
        &resolved,
        &chrome.view.split_borders,
        &frame_states,
        frame,
    ) {
        return;
    }
    let titles: Vec<Option<String>> = resolved
        .iter()
        .zip(&labels)
        .map(|(info, corners)| header_title(&corners.title, info, corners, chrome, max_travel))
        .collect();
    let styles: Vec<Style> = resolved
        .iter()
        .zip(&labels)
        .map(|(info, corners)| corner_style(chrome, info, corners.tone))
        .collect();
    render_pane_border_titles(&resolved, &titles, &styles, frame);
    // The pointer resting on a Read-only or Uncertain title underlines its
    // words, not the padding.
    if matches!(chrome.hover, Some(Hit::ControlIndicator)) {
        for (info, corners) in resolved.iter().zip(&labels) {
            let Some(rect) = title_rect(info, corners).filter(|_| corners.actionable) else {
                continue;
            };
            let words = Rect::new(rect.x + 1, rect.y, rect.width.saturating_sub(2), 1);
            let words = words.intersection(frame.area());
            frame
                .buffer_mut()
                .set_style(words, Style::default().add_modifier(Modifier::UNDERLINED));
        }
    }
    for (info, corners) in resolved.iter().zip(&labels) {
        render_pane_address(frame, chrome, info, corners);
    }
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
    if area.height < 2 || area.width < 8 {
        return;
    }

    let mark = marks::goblin_large();
    let show_mark = area.width >= mark.cols && area.height >= mark.rows + 5;
    let text_y = if show_mark {
        let mark_y = area.y + (area.height - (mark.rows + 5)) / 2;
        let mark_x = area.x + (area.width - mark.cols) / 2;
        marks::render_mark(
            frame,
            (mark_x, mark_y),
            mark,
            &MarkPalette::dimmed(p, chrome.theme.kind),
        );
        mark_y + mark.rows + 1
    } else {
        area.y + area.height.saturating_sub(4) / 2
    };

    let binding_label = |name: &str| {
        chrome
            .keymap
            .binding(name)
            .and_then(|binding| binding.chords.first())
            .map(|chord| {
                chord.label.strip_prefix("prefix+").map_or_else(
                    || chord.label.clone(),
                    |key| format!("{} {key}", chrome.keymap.prefix_label),
                )
            })
            .unwrap_or_else(|| "unset".to_owned())
    };
    let subtext = Style::default().fg(p.subtext0);
    let overlay = Style::default().fg(p.overlay0);
    let row = |lead: &str, action: &str, width: u16| {
        let clipped = truncate_end(&format!("{lead}  {action}"), usize::from(width));
        if let Some(rest) = clipped.strip_prefix(lead) {
            Line::from(vec![
                Span::styled(lead.to_owned(), subtext),
                Span::styled(rest.to_owned(), overlay),
            ])
        } else {
            Line::styled(clipped, subtext)
        }
    };
    let picker_label = binding_label("terminal_picker");
    let sidebar_label = binding_label("toggle_sidebar");
    // The toggle unpins a pinned column, rolls up an open overlay, and
    // otherwise opens the overlay.
    let sidebar_step = if chrome.sidebar.pinned {
        "hide the sidebar"
    } else if chrome.sidebar.overlay {
        "close the sidebar"
    } else {
        "open the sidebar"
    };
    let width = 30usize
        .max(display_width(&picker_label) + 2 + display_width("attach a terminal"))
        .max(display_width(&sidebar_label) + 2 + display_width(sidebar_step))
        .min(usize::from(area.width)) as u16;
    let lines = vec![
        Line::styled(
            truncate_end("No pane open.", usize::from(width)),
            Style::default().fg(p.overlay1).add_modifier(Modifier::BOLD),
        ),
        row(&picker_label, "attach a terminal", width),
        row("File › New terminal", "start one", width),
        row(&sidebar_label, sidebar_step, width),
    ];
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        text_y,
        width,
        area.height.min(4),
    );
    frame.render_widget(Paragraph::new(lines), rect);
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
        // No source and no frame: the pane is detached, and its status says
        // why (a refusal or a deferred attach) so the body is not just blank.
        None => pane.status_message().map(str::to_string),
        Some(_) => None,
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

#[derive(Clone, Copy, PartialEq, Eq)]
enum FrameState {
    Ordinary,
    Attention,
    Exited,
}

fn frame_state<W: WorkspaceView>(ws: &W, pane: &Pane) -> FrameState {
    let agent = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id);
    if exited(pane, agent) {
        FrameState::Exited
    } else if agent.is_some_and(|agent| agent.attention.is_some()) {
        FrameState::Attention
    } else {
        FrameState::Ordinary
    }
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
    if !render_border_lines(chrome, pane_infos, split_borders, &[], frame) {
        return;
    }
    let titles: Vec<Option<String>> = pane_infos
        .iter()
        .zip(titles)
        .map(|(info, title)| {
            title
                .as_deref()
                .and_then(|title| pane_border_title(title, info.rect.width))
        })
        .collect();
    let styles: Vec<Style> = pane_infos
        .iter()
        .map(|info| {
            let tone = if info.is_focused {
                MetadataTone::Focused
            } else {
                MetadataTone::Ordinary
            };
            corner_style(chrome, info, tone)
        })
        .collect();
    render_pane_border_titles(pane_infos, &titles, &styles, frame);
}

/// A corner's text style: its tone's hue, bold on the focused pane.
fn corner_style(chrome: &Chrome, info: &PaneInfo, tone: MetadataTone) -> Style {
    let style = Style::default().fg(tone.color(&chrome.palette));
    if info.is_focused {
        style.add_modifier(Modifier::BOLD)
    } else {
        style
    }
}

/// The line grid of `render_pane_borders`; false when no pane has a border.
fn render_border_lines(
    chrome: &Chrome,
    pane_infos: &[PaneInfo],
    split_borders: &[SplitBorder],
    frame_states: &[FrameState],
    frame: &mut Frame,
) -> bool {
    let pane_gaps = chrome.prefs.pane_gaps;
    if pane_infos.iter().all(|info| info.borders.is_empty()) {
        return false;
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
        let mut state = FrameState::Ordinary;
        for (info, candidate) in pane_infos.iter().zip(frame_states) {
            if !line_touches_pane(x, y, info, pane_gaps) {
                continue;
            }
            if *candidate == FrameState::Attention {
                state = FrameState::Attention;
                break;
            }
            if *candidate == FrameState::Exited {
                state = FrameState::Exited;
            }
        }
        let color = if focused {
            chrome.palette.accent
        } else {
            match state {
                FrameState::Attention => chrome.palette.yellow,
                FrameState::Exited => chrome.palette.red,
                FrameState::Ordinary => chrome.palette.overlay0,
            }
        };
        cell.set_style(Style::default().fg(color));
    }
    for (info, state) in pane_infos.iter().zip(frame_states) {
        let (symbol, color) = match state {
            FrameState::Attention => ("⍾", chrome.palette.yellow),
            FrameState::Exited => ("◌", chrome.palette.red),
            FrameState::Ordinary => continue,
        };
        // A pane with room for its title leads it with this glyph already.
        let titled = info.borders.contains(Borders::TOP) && info.rect.width > 4;
        if !titled && area.contains(info.rect.as_position()) {
            let cell = &mut buf[(info.rect.x, info.rect.y)];
            cell.set_symbol(symbol);
            cell.set_style(Style::default().fg(color));
        }
    }
    true
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
    pane_infos: &[PaneInfo],
    titles: &[Option<String>],
    styles: &[Style],
    frame: &mut Frame,
) {
    let buf = frame.buffer_mut();
    let area = buf.area;
    for ((info, title), style) in pane_infos.iter().zip(titles).zip(styles) {
        if !info.borders.contains(Borders::TOP) || info.rect.width <= 4 {
            continue;
        }
        let Some(title) = title else {
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
        buf.set_stringn(
            start_x,
            y,
            title,
            end_x.saturating_sub(start_x) as usize,
            *style,
        );
    }
}

/// The pane's address in its bottom-right corner, or at the top edge's
/// right end on a pane without its own bottom edge; bold when focused.
fn render_pane_address(frame: &mut Frame, chrome: &Chrome, info: &PaneInfo, corners: &PaneCorners) {
    let Some(rect) = address_rect(info, corners) else {
        return;
    };
    let buf = frame.buffer_mut();
    if !buf.area.contains(rect.as_position()) || rect.right() > buf.area.right() {
        return;
    }
    // The address repeats focus and hold; a seat that needs you says so
    // in its title alone.
    let tone = match corners.tone {
        MetadataTone::Attention => MetadataTone::Ordinary,
        tone => tone,
    };
    let style = corner_style(chrome, info, tone);
    buf.set_string(
        rect.x,
        rect.y,
        format!(" {} ", corners.address_label()),
        style,
    );
    // Only an SRT pane has a mark, and it leads the label; it draws in the
    // destructive token (hue 350, deutan-safe red) so the lock stands out
    // while the address keeps the corner's tone (#23096).
    if let Some(mark) = corners.sandbox_mark {
        buf.set_string(rect.x + 1, rect.y, mark, style.fg(chrome.palette.red));
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
#[path = "panes/tests.rs"]
mod tests;
