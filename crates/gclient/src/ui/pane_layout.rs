//! BSP pane layout extracted from herdr `ui/panes.rs`.

pub use gobby_terminal::layout::{NavDirection, PaneId, PaneInfo, SplitBorder};

use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;

pub fn content_inner(area: Rect) -> Rect {
    Rect::new(
        area.x.saturating_add(1),
        area.y.saturating_add(1),
        area.width.saturating_sub(2),
        area.height.saturating_sub(2),
    )
}

pub fn metrics_for(offset: u32, max_offset: u32, viewport_rows: u16) -> ScrollMetrics {
    ScrollMetrics {
        offset_from_bottom: offset as usize,
        max_offset_from_bottom: max_offset as usize,
        viewport_rows: viewport_rows as usize,
    }
}
