//! Navigator overlay listing live terminals.

use crate::app::Workspace;
use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

pub fn render_navigator(frame: &mut Frame, area: Rect, ws: &Workspace) {
    let mut lines = Vec::new();
    if let Some(project) = ws.project_id() {
        lines.push(format!("proj {project}"));
    }
    lines.extend(ws.roster_terminal_ids());
    frame.render_widget(Paragraph::new(lines.join("\n")), area);
}
