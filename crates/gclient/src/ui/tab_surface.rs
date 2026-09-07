// upstream: herdr v0.8.0 src/ui/tab_surface.rs
//! The surface below the tab bar: zoomed pane or the BSP of the active tab.

use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::panes::{render_empty, render_panes, PaneContent};
use crate::ui::tabs::{render_tab_bar, TabBarHits};
use ratatui::layout::Rect;
use ratatui::Frame;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TabSurfaceLayout {
    pub tabs: Rect,
    pub body: Rect,
}

/// Split `area` into the tab bar row and the body (herdr `compute_tab_surface`).
/// Without a tab bar, `tabs` is the empty rect on the top edge.
pub fn compute_tab_surface(area: Rect, show_tab_bar: bool) -> TabSurfaceLayout {
    if !show_tab_bar || area.height == 0 {
        return TabSurfaceLayout {
            tabs: Rect::new(area.x, area.y, area.width, 0),
            body: area,
        };
    }
    TabSurfaceLayout {
        tabs: Rect::new(area.x, area.y, area.width, 1),
        body: Rect::new(
            area.x,
            area.y.saturating_add(1),
            area.width,
            area.height.saturating_sub(1),
        ),
    }
}

/// Render the tab bar, then the active tab's panes (or the empty state) from
/// the geometry `chrome.compute_view` already resolved into `chrome.view`.
/// Returns the tab bar's hit areas (empty when no bar was drawn).
pub fn render_tab_surface<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) -> TabBarHits {
    let surface = compute_tab_surface(area, chrome.show_tab_bar());
    let hits = if surface.tabs.height > 0 {
        render_tab_bar(frame, surface.tabs, ws, chrome)
    } else {
        TabBarHits::default()
    };
    if chrome.view.pane_infos.is_empty() {
        render_empty(frame, surface.body, chrome);
    } else {
        render_panes(frame, ws, chrome, content);
    }
    hits
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tab_bar_takes_the_top_row_only_when_shown() {
        let area = Rect::new(2, 3, 40, 10);
        let shown = compute_tab_surface(area, true);
        assert_eq!(shown.tabs, Rect::new(2, 3, 40, 1));
        assert_eq!(shown.body, Rect::new(2, 4, 40, 9));

        let hidden = compute_tab_surface(area, false);
        assert_eq!(hidden.tabs.height, 0);
        assert_eq!(hidden.body, area);
    }

    #[test]
    fn zero_height_area_never_yields_a_tab_row() {
        let surface = compute_tab_surface(Rect::new(0, 0, 40, 0), true);
        assert_eq!(surface.tabs.height, 0);
        assert_eq!(surface.body.height, 0);
    }
}
