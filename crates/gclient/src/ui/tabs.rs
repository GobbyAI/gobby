//! Tab bar for BSP workspaces.

use crate::app::Workspace;
use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

pub fn render_tab_bar(frame: &mut Frame, area: Rect, ws: &Workspace) {
    let tabs = ws.roster_terminal_ids().join("  ");
    frame.render_widget(Paragraph::new(tabs), area);
}
