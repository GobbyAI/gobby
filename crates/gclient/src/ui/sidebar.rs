//! Agent roster sidebar rewired to Gobby terminals and attention.

use crate::app::Workspace;
use crate::ui::sidebar_rows::roster_rows;
use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

pub fn render_sidebar(frame: &mut Frame, area: Rect, ws: &Workspace) {
    let text = roster_rows(ws)
        .into_iter()
        .map(|row| format!("{} {}", row.kind, row.label))
        .collect::<Vec<_>>()
        .join("\n");
    frame.render_widget(Paragraph::new(text), area);
}
