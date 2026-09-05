// upstream: herdr v0.8.0 src/ui/navigator.rs
//! Session navigator: fuzzy filter over roster rows and attention prompts.
//!
//! herdr's popup arithmetic is kept: margins of `max(width / 16, 2)` and
//! `max(height / 10, 1)`, a one-row search line, a rule, the row body, a
//! detail row, and a footer. Rows are flat (no workspace tree), so the tree
//! prefix is the depth-zero `"  "`.

use crate::ui::chrome::{attention_terminal, row_state, Chrome, RowState, WorkspaceView};
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::sidebar_rows::{attention_kind, terminal_detail};
use crate::ui::status::{state_dot, state_label, state_label_color};
use crate::ui::text::{display_width, display_width_u16, middle_elide, truncate_end};
use crate::ui::widgets::{centered_popup_rect, panel_contrast_fg, render_panel_shell};
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Clear, Paragraph};
use ratatui::Frame;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum NavigatorStateFilter {
    #[default]
    All,
    Attention,
    Working,
    Idle,
}

#[derive(Debug, Clone, Default)]
pub struct NavigatorState {
    pub query: String,
    pub selected: usize,
    pub scroll: usize,
    pub search_focused: bool,
    pub filter: NavigatorStateFilter,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NavigatorTarget {
    Terminal(String),
    Attention(String),
}

#[derive(Debug, Clone)]
pub struct NavigatorRow {
    pub target: NavigatorTarget,
    pub title: String,
    pub state: RowState,
    pub detail: String,
}

impl NavigatorRow {
    /// Roster terminal the row switches to.
    fn terminal_id(&self) -> &str {
        match &self.target {
            NavigatorTarget::Terminal(id) => id,
            NavigatorTarget::Attention(entry) => attention_terminal(entry),
        }
    }
}

/// Rows matching the navigator query and filter, in display order.
pub fn navigator_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<NavigatorRow> {
    let p = &chrome.palette;
    let query = chrome.navigator.query.trim().to_lowercase();
    let filter = chrome.navigator.filter;
    let terminals = ws.roster_terminal_ids().into_iter().map(|id| {
        let state = row_state(ws, &id);
        NavigatorRow {
            detail: format!("{} · {}", state_label(state), terminal_detail(ws, &id, p)),
            title: id.clone(),
            target: NavigatorTarget::Terminal(id),
            state,
        }
    });
    let attention = ws
        .attention_entry_ids()
        .into_iter()
        .map(|entry| NavigatorRow {
            title: attention_terminal(&entry).to_string(),
            state: RowState::Attention,
            detail: format!(
                "{} · {}",
                state_label(RowState::Attention),
                attention_kind(&entry)
            ),
            target: NavigatorTarget::Attention(entry),
        });
    terminals
        .chain(attention)
        .filter(|row| matches_filter(row.state, filter))
        .filter(|row| {
            query.is_empty()
                || row.title.to_lowercase().contains(&query)
                || row.detail.to_lowercase().contains(&query)
        })
        .collect()
}

/// `Unseen` (done) and `Unknown` (detached) rows count as idle.
fn matches_filter(state: RowState, filter: NavigatorStateFilter) -> bool {
    match filter {
        NavigatorStateFilter::All => true,
        NavigatorStateFilter::Attention => state == RowState::Attention,
        NavigatorStateFilter::Working => state == RowState::Working,
        NavigatorStateFilter::Idle => {
            matches!(state, RowState::Idle | RowState::Unseen | RowState::Unknown)
        }
    }
}

pub fn render_navigator<W: WorkspaceView>(frame: &mut Frame, area: Rect, ws: &W, chrome: &Chrome) {
    let p = &chrome.palette;
    let margin_x = (area.width / 16).max(2);
    let margin_y = (area.height / 10).max(1);
    let width = area.width.saturating_sub(margin_x.saturating_mul(2)).max(4);
    let height = area
        .height
        .saturating_sub(margin_y.saturating_mul(2))
        .max(4);
    let Some(popup) = centered_popup_rect(area, width, height) else {
        return;
    };
    let Some(inner) = render_panel_shell(frame, popup, p.accent, p.panel_bg) else {
        return;
    };

    let search = Rect::new(inner.x, inner.y, inner.width, inner.height.min(1));
    let body = if inner.height <= 4 {
        Rect::default()
    } else {
        Rect::new(
            inner.x,
            inner.y + 2,
            inner.width,
            inner.height.saturating_sub(4),
        )
    };
    let detail = Rect::new(
        inner.x,
        inner.y + inner.height.saturating_sub(2),
        inner.width,
        inner.height.min(1),
    );
    let footer = Rect::new(
        inner.x,
        inner.y + inner.height.saturating_sub(1),
        inner.width,
        inner.height.min(1),
    );

    let rows = navigator_rows(ws, chrome);
    render_search(frame, search, ws, chrome);
    if body.height > 0 {
        render_separator(
            frame,
            Rect::new(inner.x, search.y + 1, inner.width, 1),
            chrome,
        );
        let current = chrome
            .focused_pane()
            .map(|id| ws.pane(id).terminal_id.clone());
        render_rows(frame, body, &rows, current.as_deref(), chrome);
        render_navigator_scrollbar(frame, body, rows.len(), chrome);
    }
    render_detail(frame, detail, &rows, chrome);
    render_footer(frame, footer, chrome);
}

fn render_search<W: WorkspaceView>(frame: &mut Frame, area: Rect, ws: &W, chrome: &Chrome) {
    let p = &chrome.palette;
    let focus_style = if chrome.navigator.search_focused {
        Style::default().fg(p.accent).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(p.overlay0)
    };
    let count = ws.roster_terminal_ids().len();
    let mut spans = vec![Span::styled(" / ", focus_style)];
    let query = chrome.navigator.query.trim();
    match chrome.navigator.filter {
        NavigatorStateFilter::Attention => push_state_chip(&mut spans, RowState::Attention, chrome),
        NavigatorStateFilter::Working => push_state_chip(&mut spans, RowState::Working, chrome),
        NavigatorStateFilter::Idle => push_state_chip(&mut spans, RowState::Idle, chrome),
        NavigatorStateFilter::All if query.is_empty() => spans.push(Span::styled(
            "search terminals",
            Style::default().fg(p.overlay0),
        )),
        NavigatorStateFilter::All => {
            spans.push(Span::styled(query.to_string(), Style::default().fg(p.text)));
        }
    }
    // herdr pads with `width - 16` (its own placeholder widths); measure the
    // leading spans instead so the count right-aligns inside the popup.
    let suffix = " terminals";
    let leading: usize = spans.iter().map(|span| display_width(&span.content)).sum();
    let width = usize::from(area.width).saturating_sub(leading + display_width(suffix));
    spans.push(Span::styled(
        format!("{count:>width$}{suffix}"),
        Style::default().fg(p.overlay0),
    ));
    frame.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn push_state_chip(spans: &mut Vec<Span<'static>>, state: RowState, chrome: &Chrome) {
    let p = &chrome.palette;
    let (icon, icon_color) = state_dot(state, p);
    spans.push(Span::styled(
        icon,
        Style::default().fg(icon_color).add_modifier(Modifier::BOLD),
    ));
    spans.push(Span::raw(" "));
    spans.push(Span::styled(
        state_label(state),
        Style::default()
            .fg(state_label_color(state, p))
            .add_modifier(Modifier::BOLD),
    ));
}

fn render_separator(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    let line = "─".repeat(usize::from(area.width));
    frame.render_widget(
        Paragraph::new(line).style(Style::default().fg(chrome.palette.surface1)),
        area,
    );
}

fn render_rows(
    frame: &mut Frame,
    body: Rect,
    rows: &[NavigatorRow],
    current: Option<&str>,
    chrome: &Chrome,
) {
    let start = chrome.navigator.scroll.min(rows.len());
    let end = rows
        .len()
        .min(start.saturating_add(usize::from(body.height)));
    for (visible_idx, idx) in (start..end).enumerate() {
        let rect = Rect::new(body.x, body.y + visible_idx as u16, body.width, 1);
        let selected = idx == chrome.navigator.selected;
        let is_current = current == Some(rows[idx].terminal_id());
        render_row(frame, rect, &rows[idx], selected, is_current, chrome);
    }
}

fn render_row(
    frame: &mut Frame,
    rect: Rect,
    row: &NavigatorRow,
    selected: bool,
    is_current: bool,
    chrome: &Chrome,
) {
    let p = &chrome.palette;
    frame.render_widget(Clear, rect);
    let base_style = if selected {
        Style::default().bg(p.accent).fg(panel_contrast_fg(p))
    } else {
        Style::default().bg(p.panel_bg).fg(p.text)
    };
    let dim_style = if selected {
        base_style
    } else {
        Style::default().fg(p.overlay0).bg(p.panel_bg)
    };
    let text_style = if selected {
        base_style.add_modifier(Modifier::BOLD)
    } else if is_current {
        Style::default()
            .fg(p.text)
            .bg(p.panel_bg)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(p.subtext0).bg(p.panel_bg)
    };
    let (status_icon, status_color) = state_dot(row.state, p);
    let status_style = if selected {
        base_style.add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(status_color).bg(p.panel_bg)
    };

    let prefix = "  ";
    let current = if is_current { "◆" } else { " " };
    let gutter = format!(" {current} ");
    let gutter_style = if selected {
        base_style
    } else if is_current {
        Style::default().fg(p.accent).bg(p.panel_bg)
    } else {
        dim_style
    };
    let meta_width = metadata_width(rect.width);
    let left_budget = rect
        .width
        .saturating_sub(meta_width)
        .saturating_sub(display_width_u16(&format!("{gutter}{prefix} ")))
        .saturating_sub(3);
    let title = truncate_end(&row.title, usize::from(left_budget));

    let spans = vec![
        Span::styled(gutter, gutter_style),
        Span::styled(prefix, dim_style),
        Span::styled(" ", base_style),
        Span::styled(status_icon, status_style),
        Span::raw(" "),
        Span::styled(title, text_style),
    ];
    frame.render_widget(Paragraph::new(Line::from(spans)).style(base_style), rect);

    if meta_width > 0 {
        let meta_rect = Rect::new(
            rect.x + rect.width.saturating_sub(meta_width),
            rect.y,
            meta_width,
            1,
        );
        let meta = truncate_end(&row.detail, usize::from(meta_width.saturating_sub(2)));
        let meta_style = if selected {
            base_style
        } else {
            Style::default()
                .fg(state_label_color(row.state, p))
                .bg(p.panel_bg)
        };
        frame.render_widget(
            Paragraph::new(format!(" {meta}")).style(meta_style),
            meta_rect,
        );
    }
}

fn render_navigator_scrollbar(frame: &mut Frame, body: Rect, line_count: usize, chrome: &Chrome) {
    if body.width <= 1 || body.height == 0 {
        return;
    }
    let viewport = usize::from(body.height);
    if line_count <= viewport {
        return;
    }
    let metrics = ScrollMetrics {
        viewport_rows: viewport,
        offset_from_bottom: line_count
            .saturating_sub(viewport)
            .saturating_sub(chrome.navigator.scroll),
        max_offset_from_bottom: line_count.saturating_sub(viewport),
    };
    if !should_show_scrollbar(metrics) {
        return;
    }
    let track = Rect::new(body.x + body.width - 1, body.y, 1, body.height);
    let p = &chrome.palette;
    render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
}

/// herdr `metadata_width`.
fn metadata_width(width: u16) -> u16 {
    if width >= 90 {
        28
    } else if width >= 68 {
        20
    } else if width >= 52 {
        14
    } else {
        0
    }
}

fn render_detail(frame: &mut Frame, area: Rect, rows: &[NavigatorRow], chrome: &Chrome) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    render_separator(frame, area, chrome);
    let Some(row) = rows.get(chrome.navigator.selected) else {
        return;
    };
    let detail = format!("{} · {}", row.title, row.detail);
    let text = middle_elide(&detail, usize::from(area.width.saturating_sub(2)));
    frame.render_widget(
        Paragraph::new(format!(" {text}")).style(Style::default().fg(chrome.palette.overlay0)),
        area,
    );
}

fn render_footer(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    if area.height == 0 {
        return;
    }
    let p = &chrome.palette;
    let key = Style::default().fg(p.accent).add_modifier(Modifier::BOLD);
    let dim = Style::default().fg(p.overlay0);
    let line = if chrome.navigator.search_focused {
        Line::from(vec![
            Span::styled(" enter", key),
            Span::styled(" switch  ", dim),
            Span::styled("↑↓", key),
            Span::styled(" move  ", dim),
            Span::styled("ctrl+u", key),
            Span::styled(" clear  ", dim),
            Span::styled("esc", key),
            Span::styled(" back", dim),
        ])
    } else {
        Line::from(vec![
            Span::styled(" enter", key),
            Span::styled(" switch  ", dim),
            Span::styled("/", key),
            Span::styled(" search  ", dim),
            Span::styled("a/b/w/i", key),
            Span::styled(" states  ", dim),
            Span::styled("j/k/↑↓", key),
            Span::styled(" move  ", dim),
            Span::styled("esc", key),
            Span::styled(" close", dim),
        ])
    };
    frame.render_widget(Paragraph::new(line), area);
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

    fn titles(rows: &[NavigatorRow]) -> Vec<&str> {
        rows.iter().map(|row| row.title.as_str()).collect()
    }

    #[test]
    fn rows_list_terminals_then_attention_entries() {
        let ws = scripted_workspace();
        let rows = navigator_rows(&ws, &Chrome::dark());
        assert_eq!(titles(&rows), ["term-alpha", "term-beta", "term-alpha"]);
        assert_eq!(rows[0].state, RowState::Attention);
        assert_eq!(rows[1].state, RowState::Idle);
        assert_eq!(
            rows[2].target,
            NavigatorTarget::Attention("run:term-alpha".into())
        );
        assert_eq!(rows[2].detail, "blocked · run");
        assert!(rows[1].detail.starts_with("idle · native"));
    }

    #[test]
    fn query_and_state_filter_narrow_the_rows() {
        let ws = scripted_workspace();
        let mut chrome = Chrome::dark();
        chrome.navigator.query = " BETA ".into();
        assert_eq!(titles(&navigator_rows(&ws, &chrome)), ["term-beta"]);

        chrome.navigator.query.clear();
        chrome.navigator.filter = NavigatorStateFilter::Attention;
        let rows = navigator_rows(&ws, &chrome);
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|row| row.state == RowState::Attention));

        chrome.navigator.filter = NavigatorStateFilter::Idle;
        assert_eq!(titles(&navigator_rows(&ws, &chrome)), ["term-beta"]);
    }

    #[test]
    fn popup_renders_rows_with_state_meta() {
        let ws = scripted_workspace();
        let mut chrome = Chrome::dark();
        chrome.navigator.selected = 1;
        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| render_navigator(frame, frame.area(), &ws, &chrome))
            .unwrap();
        let buffer = terminal.backend().buffer();
        let width = usize::from(buffer.area.width);
        let cells: Vec<String> = buffer
            .content()
            .iter()
            .map(|c| c.symbol().to_string())
            .collect();
        let text = cells
            .chunks(width)
            .map(|row| row.concat())
            .collect::<Vec<_>>()
            .join("\n");
        for needle in [
            "search terminals",
            "2 terminals",
            "term-alpha",
            "term-beta",
            "blocked",
            "idle · native",
        ] {
            assert!(text.contains(needle), "missing {needle:?}:\n{text}");
        }
        assert!(!text.contains('!'), "{text}");
        assert_eq!(metadata_width(104), 28);
        assert_eq!(metadata_width(50), 0);
    }
}
