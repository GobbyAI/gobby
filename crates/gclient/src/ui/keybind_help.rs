// upstream: herdr v0.8.0 src/ui/keybind_help.rs
//! Keybinding help overlay over the gclient keymap; reserved actions never
//! appear. Rows are `keys  description (name)`, laid out in two columns
//! when the modal is wide enough so the whole table fits a 40-row frame.
//! Help opens on the attention legend card, above the bindings.

use crate::ui::chrome::{Chrome, RowState};
use crate::ui::keymap::{HelpEntry, Keymap};
use crate::ui::status::{state_dot, state_label};
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

fn visible_entries(chrome: &Chrome) -> Vec<HelpEntry> {
    let query = chrome.keybind_help.query.to_lowercase();
    let prefix_label = chrome.keymap.prefix_label.as_str();
    let show_prefix = query.is_empty()
        || "prefix mode".contains(&query)
        || prefix_label.to_lowercase().contains(&query);
    let mut entries = filtered_entries(&chrome.keymap, &query);
    if show_prefix {
        entries.insert(
            0,
            HelpEntry {
                name: "prefix",
                description: "Enter prefix mode",
                keys: prefix_label.to_owned(),
            },
        );
    }
    entries
}

fn key_width(entries: &[HelpEntry]) -> usize {
    entries
        .iter()
        .map(|entry| entry.keys.chars().count())
        .max()
        .unwrap_or(8)
}

fn longest_row_without_name(entries: &[HelpEntry], key_width: usize) -> usize {
    entries
        .iter()
        .map(|entry| key_width + 2 + entry.description.chars().count())
        .max()
        .unwrap_or(0)
}

fn wrap_description(description: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in description.split_whitespace() {
        let word_len = word.chars().count();
        if !current.is_empty() && current.chars().count() + 1 + word_len > width {
            lines.push(std::mem::take(&mut current));
        }
        if word_len > width {
            for character in word.chars() {
                if current.chars().count() == width {
                    lines.push(std::mem::take(&mut current));
                }
                current.push(character);
            }
        } else {
            if !current.is_empty() {
                current.push(' ');
            }
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

fn help_groups(chrome: &Chrome, width: u16) -> Vec<Vec<Line<'static>>> {
    let p = &chrome.palette;
    let key_style = Style::default().fg(p.mauve).add_modifier(Modifier::BOLD);
    let label_style = Style::default().fg(p.text);
    let name_style = Style::default().fg(p.overlay1);
    let entries = visible_entries(chrome);
    if entries.is_empty() {
        return vec![vec![Line::from(Span::styled(
            " no matching keybinds",
            Style::default().fg(p.overlay1),
        ))]];
    }
    let key_width = key_width(&entries);
    let show_names = entries.iter().all(|entry| {
        key_width + 2 + entry.description.chars().count() + entry.name.chars().count() + 3
            <= width as usize
    });
    entries
        .iter()
        .map(|entry| {
            let key_cell = format!(" {:key_width$} ", entry.keys);
            let key_cell_width = key_cell.chars().count();
            let inline = if key_cell_width < width as usize {
                wrap_description(entry.description, width as usize - key_cell_width)
            } else {
                Vec::new()
            };
            let below = wrap_description(entry.description, (width as usize).saturating_sub(1));
            if inline.is_empty() || below.len() + 1 < inline.len() {
                let mut lines = vec![Line::from(Span::styled(key_cell, key_style))];
                lines.extend(below.into_iter().map(|chunk| {
                    Line::from(vec![Span::raw(" "), Span::styled(chunk, label_style)])
                }));
                return lines;
            }
            let indent = " ".repeat(key_cell_width);
            inline
                .into_iter()
                .enumerate()
                .map(|(index, chunk)| {
                    let mut spans = vec![
                        Span::styled(
                            if index == 0 {
                                key_cell.clone()
                            } else {
                                indent.clone()
                            },
                            key_style,
                        ),
                        Span::styled(chunk, label_style),
                    ];
                    if show_names {
                        spans.push(Span::styled(format!(" ({})", entry.name), name_style));
                    }
                    Line::from(spans)
                })
                .collect()
        })
        .collect()
}

/// Body rows: the prefix chord first, then every visible binding.
pub fn help_lines(chrome: &Chrome) -> Vec<Line<'static>> {
    help_rows(chrome, u16::MAX)
}

pub fn help_rows(chrome: &Chrome, width: u16) -> Vec<Line<'static>> {
    help_groups(chrome, width).into_iter().flatten().collect()
}

/// The attention legend Help opens with, in the Attention board's order:
/// each state's glyph and word, what the status counts file it under, and
/// what it means.
const LEGEND: [(RowState, &str, &str); 7] = [
    (
        RowState::Attention,
        "need you",
        "waiting on you: an approval, a question, an error it cannot pass",
    ),
    (RowState::Working, "active", "running a turn"),
    (RowState::Idle, "idle", "at its prompt, nothing pending"),
    (
        RowState::Unseen,
        "idle",
        "finished since you last looked; clears when its pane is focused",
    ),
    (
        RowState::Paused,
        "idle",
        "a run someone paused on purpose; never an agent idle at its prompt",
    ),
    (
        RowState::Orphaned,
        "n gone",
        "process or relay missing; its own count, shown only when not zero",
    ),
    (
        RowState::Unknown,
        "idle",
        "the first seconds after a spawn, before the daemon has a state",
    ),
];
const LEGEND_STATE_WIDTH: usize = 15;
const LEGEND_COUNT_WIDTH: usize = 11;
/// Cells before a legend row's meaning: the glyph cell, the state, the count.
const LEGEND_LEAD: usize = 3 + LEGEND_STATE_WIDTH + LEGEND_COUNT_WIDTH;

/// The legend card `render_body` draws above the bindings while no search
/// narrows them: a heading group, then one group per state, each meaning
/// wrapped like a binding's description.
pub fn legend_groups(chrome: &Chrome, width: u16) -> Vec<Vec<Line<'static>>> {
    if !chrome.keybind_help.query.is_empty() {
        return Vec::new();
    }
    let p = &chrome.palette;
    let dim = Style::default().fg(p.subtext0);
    let width = width as usize;
    let mut groups = vec![vec![
        Line::from(Span::styled(
            " Legend",
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        )),
        Line::from(Span::styled(
            format!(
                "   {:state$}{:count$}means",
                "state",
                "counts as",
                state = LEGEND_STATE_WIDTH,
                count = LEGEND_COUNT_WIDTH
            ),
            Style::default().fg(p.overlay1),
        )),
    ]];
    groups.extend(LEGEND.iter().map(|&(state, counts_as, means)| {
        let (glyph, color) = state_dot(state, p);
        let lead = vec![
            Span::raw(" "),
            Span::styled(format!("{glyph} "), Style::default().fg(color)),
            Span::styled(
                format!("{:width$}", state_label(state), width = LEGEND_STATE_WIDTH),
                Style::default().fg(p.text),
            ),
            Span::styled(
                format!("{counts_as:width$}", width = LEGEND_COUNT_WIDTH),
                dim,
            ),
        ];
        let inline = if LEGEND_LEAD < width {
            wrap_description(means, width - LEGEND_LEAD)
        } else {
            Vec::new()
        };
        let below = wrap_description(means, width.saturating_sub(3));
        if inline.is_empty() || below.len() + 1 < inline.len() {
            let mut lines = vec![Line::from(lead)];
            lines.extend(
                below
                    .into_iter()
                    .map(|chunk| Line::from(vec![Span::raw("   "), Span::styled(chunk, dim)])),
            );
            return lines;
        }
        inline
            .into_iter()
            .enumerate()
            .map(|(index, chunk)| {
                let mut spans = if index == 0 {
                    lead.clone()
                } else {
                    vec![Span::raw(" ".repeat(LEGEND_LEAD))]
                };
                spans.push(Span::styled(chunk, dim));
                Line::from(spans)
            })
            .collect()
    }));
    if let Some(last) = groups.last_mut() {
        last.push(Line::default());
    }
    groups
}

/// Draw the help over `area`. Returns the close button, and the furthest
/// scroll the drawn body shows, which bounds the scroll keys.
pub fn render_keybind_help(frame: &mut Frame, area: Rect, chrome: &Chrome) -> (Vec<Rect>, usize) {
    let p = &chrome.palette;
    let popup_w = area.width.saturating_sub(4).min(HELP_MAX_WIDTH);
    let popup_h = area.height.saturating_sub(2).min(HELP_MAX_HEIGHT);
    let Some(inner) = render_modal_shell(frame, area, popup_w, popup_h, p) else {
        return (Vec::new(), 0);
    };
    if inner.height < 6 || inner.width < 20 {
        return (Vec::new(), 0);
    }

    let stack = modal_stack_areas(inner, 2, 1, 0, 1);
    let [title_row, search_row] =
        Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas::<2>(stack.header);

    render_modal_header(frame, title_row, "Keybinds", p);
    let close_label = if chrome.keybind_help.search_focused {
        "Back"
    } else {
        "Close"
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

    let last_scroll = render_body(frame, stack.content, chrome);

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
    let footer_area = stack.footer.unwrap_or_default();
    let footer = if footer.width() <= footer_area.width as usize {
        footer
    } else if chrome.keybind_help.search_focused && footer_area.width >= 40 {
        Line::from(vec![
            Span::styled(" type filter · ", dim),
            Span::styled("ctrl+u", key),
            Span::styled(" clear · ", dim),
            Span::styled("esc", key),
            Span::styled(" back", dim),
        ])
    } else if chrome.keybind_help.search_focused {
        Line::from(vec![
            Span::styled(" esc back · ", dim),
            Span::styled("ctrl+u", key),
        ])
    } else if footer_area.width >= 40 {
        Line::from(vec![
            Span::styled(" / search · ", dim),
            Span::styled("j/k", key),
            Span::styled(" scroll · ", dim),
            Span::styled("esc", key),
            Span::styled(" close", dim),
        ])
    } else {
        Line::from(vec![
            Span::styled(" / find · ", dim),
            Span::styled("esc close", key),
        ])
    };
    frame.render_widget(Paragraph::new(footer), footer_area);
    (vec![button], last_scroll)
}

/// Lay logical bindings out column-major, wrapping descriptions within each row.
/// Scroll by groups, and return the furthest scroll: the first group of the
/// last full view.
fn render_body(frame: &mut Frame, body: Rect, chrome: &Chrome) -> usize {
    if body.width == 0 || body.height == 0 {
        return 0;
    }
    let p = &chrome.palette;
    let entries = visible_entries(chrome);
    let longest = longest_row_without_name(&entries, key_width(&entries));
    let rows = body.height as usize;
    let layout = |has_track: bool| {
        let text_width = body.width - u16::from(has_track);
        let columns = if 2 * longest + HELP_COLUMN_GAP as usize <= text_width as usize {
            2u16
        } else {
            1
        };
        let column_width = text_width.saturating_sub(HELP_COLUMN_GAP * (columns - 1)) / columns;
        let mut groups = legend_groups(chrome, column_width);
        groups.extend(help_groups(chrome, column_width));
        let total_lines = groups.iter().map(Vec::len).sum::<usize>();
        (columns, column_width, groups, total_lines)
    };
    let initial = layout(false);
    let has_track = initial.3 > rows * initial.0 as usize;
    let (columns, column_width, groups, total_lines) =
        if has_track { layout(true) } else { initial };
    let capacity = rows * columns as usize;
    let max_scroll = if has_track {
        let mut trailing = 0;
        let mut first_visible = groups.len().saturating_sub(1);
        for (index, group) in groups.iter().enumerate().rev() {
            if trailing + group.len() > capacity {
                break;
            }
            trailing += group.len();
            first_visible = index;
        }
        first_visible
    } else {
        0
    };
    let scroll = chrome.keybind_help.scroll.min(max_scroll);
    let track = has_track.then(|| Rect::new(body.x + body.width - 1, body.y, 1, body.height));
    let visible: Vec<_> = groups[scroll..]
        .iter()
        .flat_map(|group| group.iter().cloned())
        .take(capacity)
        .collect();
    for (column, chunk) in visible.chunks(rows).enumerate() {
        let x = body.x + (column_width + HELP_COLUMN_GAP) * column as u16;
        frame.render_widget(
            Paragraph::new(chunk.to_vec()),
            Rect::new(x, body.y, column_width, body.height),
        );
    }

    let Some(track) = track else {
        return max_scroll;
    };
    let track_rows = track.height as usize;
    let thumb_len = ((track_rows * capacity) / total_lines).max(1);
    let thumb_top = scroll * track_rows.saturating_sub(thumb_len) / max_scroll.max(1);
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
    max_scroll
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
