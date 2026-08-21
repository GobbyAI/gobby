//! Tab surface layout.

use ratatui::layout::{Constraint, Layout, Rect};

#[derive(Debug, Clone, Copy)]
pub struct TabSurfaceLayout {
    pub tabs: Rect,
    pub body: Rect,
}

pub fn compute_tab_surface(area: Rect) -> TabSurfaceLayout {
    let chunks = Layout::vertical([Constraint::Length(1), Constraint::Min(1)]).split(area);
    TabSurfaceLayout {
        tabs: chunks[0],
        body: chunks[1],
    }
}

pub fn resize_tab_surface(area: Rect, _delta: i16) -> TabSurfaceLayout {
    compute_tab_surface(area)
}
