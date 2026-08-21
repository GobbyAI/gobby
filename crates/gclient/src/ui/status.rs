//! Status bar from Gobby control and roster state.

use crate::app::Workspace;
use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

pub fn render_status(frame: &mut Frame, area: Rect, ws: &Workspace) {
    let mut parts = vec!["gobby".to_string()];
    if let Some(project) = ws.project_id() {
        parts.push(project.to_string());
    }
    let live = ws.roster_terminal_ids().len();
    parts.push(format!("{live} live"));
    let control = if ws.roster_terminal_ids().is_empty() {
        "observe"
    } else {
        "live"
    };
    parts.push(control.into());
    frame.render_widget(Paragraph::new(parts.join(" ")), area);
}
