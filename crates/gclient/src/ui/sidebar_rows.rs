// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar row models: roster rows and attention rows, plus the line
//! builders the sidebar and navigator share.
//!
//! Ported from herdr `resolved_token_spans`: a state glyph plus text tokens
//! joined by `" "` after the glyph and `" · "` elsewhere; trailing tokens
//! drop from the right before the title truncates.

use crate::theme::Palette;
use crate::ui::chrome::{
    attention_label, attention_pane, row_state, terminal_label, Chrome, RowState, WorkspaceView,
};
use crate::ui::status::{control_indicator, state_dot, state_label, state_label_color};
use crate::ui::text::{display_width, truncate_end};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RowKind {
    Terminal,
    Attention,
}

#[derive(Debug, Clone)]
pub struct SidebarRow {
    pub id: String,
    pub label: String,
    pub kind: RowKind,
    pub state: RowState,
    /// Backend and control state for terminals; prompt kind for attention.
    pub detail: String,
    pub selected: bool,
    /// The focused pane of the active tab shows this row's terminal (herdr
    /// "active workspace": bold `text` title on `surface_dim`).
    pub active: bool,
}

/// Terminal roster rows in roster order.
pub fn roster_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let focused = focused_terminal(ws, chrome);
    ws.roster_terminal_ids()
        .into_iter()
        .enumerate()
        .map(|(index, id)| SidebarRow {
            label: terminal_label(ws, &id),
            kind: RowKind::Terminal,
            state: row_state(ws, &id),
            detail: terminal_detail(ws, &id, &chrome.palette),
            selected: index == chrome.sidebar.selected,
            active: focused.as_deref() == Some(id.as_str()),
            id,
        })
        .collect()
}

/// Attention rows in arrival order.
pub fn attention_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let focused = chrome.focused_pane();
    ws.attention_entry_ids()
        .into_iter()
        .map(|entry| SidebarRow {
            label: attention_label(ws, &entry),
            kind: RowKind::Attention,
            state: RowState::Attention,
            detail: attention_kind(&entry).to_string(),
            selected: false,
            active: focused.is_some() && attention_pane(ws, &entry) == focused,
            id: entry,
        })
        .collect()
}

/// Terminal shown in the focused pane of the active tab.
fn focused_terminal<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Option<String> {
    chrome
        .focused_pane()
        .map(|id| ws.pane(id).terminal_id.clone())
}

/// `<backend> <control glyph> <control label>` for an attached terminal,
/// `detached` for a roster row without a pane.
pub(crate) fn terminal_detail<W: WorkspaceView>(ws: &W, terminal_id: &str, p: &Palette) -> String {
    match ws.pane_for_terminal(terminal_id).map(|id| ws.pane(id)) {
        Some(pane) => {
            let (glyph, label, _) = control_indicator(pane.control, pane.take_back, p);
            // The address rides with the backend that owns it, which is what
            // keeps two panes sharing a title (`zsh`, `zsh`) tellable apart.
            match pane.address.as_deref() {
                Some(address) => format!("{} {address} {glyph} {label}", pane.backend),
                None => format!("{} {glyph} {label}", pane.backend),
            }
        }
        None => "detached".to_string(),
    }
}

/// Prompt kind of an attention entry keyed `<kind>:<terminal>`.
pub(crate) fn attention_kind(entry_id: &str) -> &str {
    entry_id.split_once(':').map_or("prompt", |(kind, _)| kind)
}

/// One rendered sidebar line for `row` at `width` columns (herdr row
/// composition: state dot, title truncated, trailing state label).
pub fn row_line<'a>(row: &'a SidebarRow, width: u16, chrome: &Chrome) -> Line<'a> {
    let p = &chrome.palette;
    let (glyph, glyph_color) = state_dot(row.state, p);
    let marker_style = if row.selected {
        Style::default().fg(p.accent).bg(p.surface1)
    } else {
        Style::default()
    };
    let title_style = if row.selected || row.active {
        Style::default().fg(p.text).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(p.subtext0)
    };
    let label_style = Style::default()
        .fg(state_label_color(row.state, p))
        .add_modifier(Modifier::DIM);
    // herdr's default agent token is `overlay0` + dim.
    let detail_style = Style::default()
        .fg(if row.selected { p.mauve } else { p.overlay0 })
        .add_modifier(Modifier::DIM);
    let trailing = [
        (state_label(row.state), label_style),
        (row.detail.as_str(), detail_style),
    ];
    let marker = if row.selected { "▸" } else { " " };
    let mut spans = vec![Span::styled(marker, marker_style)];
    spans.extend(fitted_spans(
        (glyph, Style::default().fg(glyph_color)),
        (&row.label, title_style),
        &trailing,
        p,
        usize::from(width).saturating_sub(1),
    ));
    Line::from(spans)
}

/// herdr `resolved_token_spans`, reduced to the glyph + title + trailing
/// shape: `" "` after the glyph, `" · "` between text tokens. Trailing tokens
/// are kept from the left while they fit beside the whole title and dropped
/// from the right otherwise; the title truncates only once it stands alone.
/// A roster row is identified by its terminal title, so the title outranks
/// its state and detail tokens here — the tab-bar rule (truncate the tab's
/// own title so its trailing tokens survive) does not apply to session
/// titles. Empty tokens are elided with their separators.
pub fn fitted_spans(
    glyph: (&str, Style),
    title: (&str, Style),
    trailing: &[(&str, Style)],
    p: &Palette,
    max_width: usize,
) -> Vec<Span<'static>> {
    let separator_style = Style::default().fg(p.overlay0).add_modifier(Modifier::DIM);
    let mut spans = vec![Span::styled(glyph.0.to_string(), glyph.1)];
    let remaining = max_width.saturating_sub(display_width(glyph.0));
    if remaining < 2 || title.0.is_empty() {
        return spans;
    }
    spans.push(Span::styled(" ", separator_style));
    let remaining = remaining - 1;
    let trailing: Vec<&(&str, Style)> = trailing
        .iter()
        .filter(|(text, _)| !text.is_empty())
        .collect();
    let cost = |tokens: &[&(&str, Style)]| -> usize {
        tokens.iter().map(|(text, _)| 3 + display_width(text)).sum()
    };
    let title_width = display_width(title.0);
    let mut keep = trailing.len();
    while keep > 0 && title_width + cost(&trailing[..keep]) > remaining {
        keep -= 1;
    }
    let title_budget = remaining.saturating_sub(cost(&trailing[..keep]));
    spans.push(Span::styled(truncate_end(title.0, title_budget), title.1));
    for (text, style) in &trailing[..keep] {
        spans.push(Span::styled(" · ", separator_style));
        spans.push(Span::styled(text.to_string(), *style));
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
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

    fn line_text(line: &Line<'_>) -> String {
        line.spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect()
    }

    #[test]
    fn roster_rows_follow_roster_order_and_selection() {
        let ws = scripted_workspace();
        let mut chrome = Chrome::dark();
        chrome.sidebar.selected = 1;
        let rows = roster_rows(&ws, &chrome);
        let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        assert_eq!(ids, ws.roster_terminal_ids());
        assert_eq!(rows[0].state, RowState::Attention);
        assert!(!rows[0].selected);
        assert!(rows[1].selected);
        assert!(rows.iter().all(|row| row.kind == RowKind::Terminal));
        assert!(rows[0].detail.contains("native"));
        assert!(rows[0].detail.contains("observe"));
    }

    #[test]
    fn attention_rows_point_at_their_terminal() {
        let ws = scripted_workspace();
        let rows = attention_rows(&ws, &Chrome::dark());
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, "run:term-alpha");
        assert_eq!(rows[0].label, "term-alpha");
        assert_eq!(rows[0].detail, "run");
        assert_eq!(rows[0].kind, RowKind::Attention);
    }

    #[test]
    fn row_line_drops_trailing_tokens_before_truncating_the_title() {
        let chrome = Chrome::dark();
        let row = SidebarRow {
            id: "term-alpha".into(),
            label: "term-alpha".into(),
            kind: RowKind::Terminal,
            state: RowState::Idle,
            detail: "native ○ observe".into(),
            selected: true,
            active: false,
        };
        let wide = line_text(&row_line(&row, 60, &chrome));
        assert_eq!(wide, "▸○ term-alpha · idle · native ○ observe");
        // The detail no longer fits beside the whole title, so it drops and
        // the shorter state label stays.
        let mid = line_text(&row_line(&row, 24, &chrome));
        assert_eq!(mid, "▸○ term-alpha · idle");
        // Narrower still: the title keeps its cells and the tokens go.
        let narrow = line_text(&row_line(&row, 14, &chrome));
        assert_eq!(narrow, "▸○ term-alpha");
        let tiny = line_text(&row_line(&row, 8, &chrome));
        assert_eq!(tiny, "▸○ term…");
    }
}
