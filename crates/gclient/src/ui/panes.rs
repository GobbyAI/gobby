//! Terminal pane chrome.

use crate::app::{Pane, Workspace};
use crate::ui::pane_layout::content_inner;
use crate::ui::text::truncate_end;
use ratatui::layout::Rect;
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

pub fn render_panes(frame: &mut Frame, area: Rect, ws: &Workspace) {
    let inner = content_inner(area);
    let mut labels = Vec::new();
    for id in ws.roster_terminal_ids() {
        if let Some(pane_id) = ws.pane_for_terminal(&id) {
            labels.push(truncate_end(
                &pane_title(ws.pane(pane_id)),
                inner.width as usize,
            ));
        } else {
            labels.push(truncate_end(&id, inner.width as usize));
        }
    }
    frame.render_widget(
        Paragraph::new(labels.join(" | ")).block(Block::default().borders(Borders::ALL)),
        area,
    );
}

pub fn pane_title(pane: &Pane) -> String {
    let state = if pane.is_held() {
        "held"
    } else if pane.is_lease_lost() {
        "take-back"
    } else {
        "observe"
    };
    format!("{} {state}", pane.terminal_id)
}
