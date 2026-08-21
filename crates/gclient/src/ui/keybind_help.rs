//! Live-filtered keybind help overlay.

use crate::ui::text::truncate_end;
use ratatui::layout::Rect;
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

const BINDS: &[(&str, &str)] = &[
    ("q", "quit"),
    ("/", "filter"),
    ("tab", "next pane"),
    ("shift-tab", "prev pane"),
    ("enter", "take control"),
    ("esc", "release control"),
];

pub fn filtered_binds(query: &str) -> Vec<String> {
    let q = query.to_ascii_lowercase();
    BINDS
        .iter()
        .filter(|(key, action)| key.contains(&q) || action.contains(&q))
        .map(|(key, action)| format!("{key}  {action}"))
        .collect()
}

pub fn render_keybind_help(frame: &mut Frame, area: Rect, query: &str) {
    let body = filtered_binds(query)
        .into_iter()
        .map(|line| truncate_end(&line, area.width.saturating_sub(2) as usize))
        .collect::<Vec<_>>()
        .join("\n");
    frame.render_widget(
        Paragraph::new(body).block(Block::default().borders(Borders::ALL).title("keys")),
        area,
    );
}
