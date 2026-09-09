// upstream: herdr v0.8.0 src/ui/keybind_help.rs
//! Keybinding help overlay over the gclient keymap; reserved actions never
//! appear. Rows are `keys  description (name)`, laid out in two columns
//! when the modal is wide enough so the whole table fits a 40-row frame.

use crate::ui::chrome::Chrome;
use crate::ui::keymap::{HelpEntry, Keymap};
use crate::ui::widgets::{
    action_button_width, modal_stack_areas, panel_contrast_fg, render_action_button,
    render_modal_header, render_modal_shell,
};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

/// herdr's modal is 76x22; the flat gclient table needs more room, so the
/// modal grows with the frame up to these caps.
const HELP_MAX_WIDTH: u16 = 120;
const HELP_MAX_HEIGHT: u16 = 40;
/// Two columns once each can hold a keys cell plus a description.
const HELP_COLUMN_MIN_WIDTH: u16 = 50;
const HELP_COLUMN_GAP: u16 = 2;

#[derive(Debug, Clone, Default)]
pub struct KeybindHelpState {
    pub scroll: usize,
    pub query: String,
    pub search_focused: bool,
}

/// Help rows filtered by `query` (case-insensitive substring over name,
/// description, and keys); reserved actions are already excluded.
pub fn filtered_entries(keymap: &Keymap, query: &str) -> Vec<HelpEntry> {
    filter_help_entries(keymap.help_entries(), query)
}

/// herdr `filter_keybind_help_groups` on one flat list: keep the entries whose
/// name, description, or keys contain `query`, case-insensitively.
pub fn filter_help_entries(entries: Vec<HelpEntry>, query: &str) -> Vec<HelpEntry> {
    let query = query.to_lowercase();
    entries
        .into_iter()
        .filter(|entry| {
            query.is_empty()
                || entry.name.to_lowercase().contains(&query)
                || entry.description.to_lowercase().contains(&query)
                || entry.keys.to_lowercase().contains(&query)
        })
        .collect()
}

/// Body rows: the prefix chord first, then every visible binding.
pub fn help_lines(chrome: &Chrome) -> Vec<Line<'static>> {
    let p = &chrome.palette;
    let key_style = Style::default().fg(p.mauve).add_modifier(Modifier::BOLD);
    let label_style = Style::default().fg(p.text);
    let name_style = Style::default().fg(p.overlay1);

    let query = chrome.keybind_help.query.to_lowercase();
    let prefix_label = chrome.keymap.prefix_label.as_str();
    let show_prefix = query.is_empty()
        || "prefix mode".contains(&query)
        || prefix_label.to_lowercase().contains(&query);
    let entries = filtered_entries(&chrome.keymap, &query);
    if entries.is_empty() && !show_prefix {
        return vec![Line::from(Span::styled(
            " no matching keybinds",
            Style::default().fg(p.overlay1),
        ))];
    }

    let key_width = entries
        .iter()
        .map(|entry| entry.keys.chars().count())
        .chain(show_prefix.then(|| prefix_label.chars().count()))
        .max()
        .unwrap_or(8);
    let row = |keys: &str, description: &str, name: &str| {
        Line::from(vec![
            Span::styled(format!(" {keys:<key_width$} "), key_style),
            Span::styled(description.to_string(), label_style),
            Span::styled(format!(" ({name})"), name_style),
        ])
    };

    let mut lines = Vec::with_capacity(entries.len() + 1);
    if show_prefix {
        lines.push(row(prefix_label, "Enter prefix mode", "prefix"));
    }
    for entry in &entries {
        lines.push(row(&entry.keys, entry.description, entry.name));
    }
    lines
}

pub fn render_keybind_help(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    let p = &chrome.palette;
    let popup_w = area.width.saturating_sub(4).min(HELP_MAX_WIDTH);
    let popup_h = area.height.saturating_sub(2).min(HELP_MAX_HEIGHT);
    let Some(inner) = render_modal_shell(frame, area, popup_w, popup_h, p) else {
        return;
    };
    if inner.height < 6 || inner.width < 20 {
        return;
    }

    let stack = modal_stack_areas(inner, 2, 1, 0, 1);
    let [title_row, search_row] =
        Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas::<2>(stack.header);

    render_modal_header(frame, title_row, "keybinds", p);
    let close_label = if chrome.keybind_help.search_focused {
        "back"
    } else {
        "close"
    };
    let button_w = action_button_width(Some("esc"), close_label).min(title_row.width);
    let button = Rect::new(
        title_row.x + title_row.width.saturating_sub(button_w),
        title_row.y,
        button_w,
        1,
    );
    render_action_button(
        frame,
        button,
        Some("esc"),
        close_label,
        Style::default()
            .fg(panel_contrast_fg(p))
            .bg(p.accent)
            .add_modifier(Modifier::BOLD),
    );

    let search_line = if chrome.keybind_help.search_focused {
        Line::from(vec![
            Span::styled(
                " / ",
                Style::default().fg(p.accent).add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                chrome.keybind_help.query.as_str(),
                Style::default().fg(p.text).add_modifier(Modifier::BOLD),
            ),
        ])
    } else {
        Line::from(Span::styled(
            " press / to filter by command or shortcut",
            Style::default().fg(p.overlay0),
        ))
    };
    frame.render_widget(Paragraph::new(search_line), search_row);

    render_body(frame, stack.content, chrome, help_lines(chrome));

    let dim = Style::default().fg(p.overlay0);
    let key = Style::default().fg(p.text);
    let footer = if chrome.keybind_help.search_focused {
        Line::from(vec![
            Span::styled(" filter ", dim),
            Span::styled("type/backspace", key),
            Span::styled(" · ", dim),
            Span::styled("clear ", dim),
            Span::styled("ctrl+u", key),
            Span::styled(" · ", dim),
            Span::styled("scroll ", dim),
            Span::styled("↑↓/pgup/pgdn", key),
            Span::styled(" · ", dim),
            Span::styled("back ", dim),
            Span::styled("esc", key),
        ])
    } else {
        Line::from(vec![
            Span::styled(" search ", dim),
            Span::styled("/", key),
            Span::styled(" · ", dim),
            Span::styled("scroll ", dim),
            Span::styled("j/k/↑↓/pgup/pgdn", key),
            Span::styled(" · ", dim),
            Span::styled("close ", dim),
            Span::styled("esc/enter", key),
        ])
    };
    frame.render_widget(Paragraph::new(footer), stack.footer.unwrap_or_default());
}

/// Lay `lines` out column-major over `body`, scrolled by
/// `chrome.keybind_help.scroll` rows, with a scrollbar when they overflow.
fn render_body(frame: &mut Frame, body: Rect, chrome: &Chrome, lines: Vec<Line<'static>>) {
    if body.width == 0 || body.height == 0 {
        return;
    }
    let p = &chrome.palette;
    let columns = if body.width >= 2 * HELP_COLUMN_MIN_WIDTH + HELP_COLUMN_GAP {
        2u16
    } else {
        1
    };
    let rows = body.height as usize;
    let capacity = rows * columns as usize;
    let max_scroll = lines.len().saturating_sub(capacity);
    let scroll = chrome.keybind_help.scroll.min(max_scroll);
    let track =
        (max_scroll > 0).then(|| Rect::new(body.x + body.width - 1, body.y, 1, body.height));
    let text_width = if track.is_some() {
        body.width - 1
    } else {
        body.width
    };
    let column_width = text_width.saturating_sub(HELP_COLUMN_GAP * (columns - 1)) / columns;

    let end = (scroll + capacity).min(lines.len());
    let visible = &lines[scroll..end];
    for (column, chunk) in visible.chunks(rows).enumerate() {
        let x = body.x + (column_width + HELP_COLUMN_GAP) * column as u16;
        frame.render_widget(
            Paragraph::new(chunk.to_vec()),
            Rect::new(x, body.y, column_width, body.height),
        );
    }

    let Some(track) = track else {
        return;
    };
    let track_rows = track.height as usize;
    let thumb_len = ((track_rows * capacity) / (capacity + max_scroll)).max(1);
    let thumb_top = scroll * track_rows.saturating_sub(thumb_len) / max_scroll;
    let cells: Vec<Line<'static>> = (0..track_rows)
        .map(|row| {
            let color = if row >= thumb_top && row < thumb_top + thumb_len {
                p.overlay1
            } else {
                p.overlay0
            };
            Line::from(Span::styled("▐", Style::default().fg(color)))
        })
        .collect();
    frame.render_widget(Paragraph::new(cells), track);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::keymap::HERDR_PREFIX;

    #[test]
    fn filter_matches_name_description_and_keys_case_insensitively() {
        let keymap = Keymap::defaults(HERDR_PREFIX);
        let by_name = filtered_entries(&keymap, "SPLIT_VERT");
        assert_eq!(by_name.len(), 1);
        assert_eq!(by_name[0].name, "split_vertical");
        let by_keys = filtered_entries(&keymap, "prefix+1..9");
        assert!(by_keys.iter().any(|e| e.name == "switch_tab"));
        let by_description = filtered_entries(&keymap, "side by side");
        assert_eq!(by_description.len(), 1);
        assert!(filtered_entries(&keymap, "custom").is_empty());
        assert!(filtered_entries(&keymap, "no such binding").is_empty());
    }
}
