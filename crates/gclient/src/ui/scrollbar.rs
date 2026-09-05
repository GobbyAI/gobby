// upstream: herdr v0.8.0 src/ui/scrollbar.rs
//! Pane scrollbar geometry and rendering: thumb placement, and the click and
//! drag mappings back to a scroll offset.

use crate::theme::Palette;
use gobby_terminal::layout::{PaneInfo, ScrollMetrics};
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::Frame;

pub fn pane_scrollbar_rect(info: &PaneInfo) -> Option<Rect> {
    info.scrollbar_rect
}

pub fn should_show_scrollbar(metrics: ScrollMetrics) -> bool {
    metrics.max_offset_from_bottom > 0
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ScrollbarThumb {
    pub top: u16,
    pub len: u16,
}

pub fn scrollbar_thumb(metrics: ScrollMetrics, track: Rect) -> Option<ScrollbarThumb> {
    if metrics.max_offset_from_bottom == 0 || track.height == 0 {
        return None;
    }

    let track_height = track.height as usize;
    let total_rows = metrics.max_offset_from_bottom + metrics.viewport_rows;
    if total_rows == 0 {
        return None;
    }

    let thumb_len = ((metrics.viewport_rows * track_height) as f32 / total_rows as f32)
        .round()
        .max(1.0)
        .min(track_height as f32) as usize;
    let max_thumb_top = track_height.saturating_sub(thumb_len);
    let scrolled_from_top = metrics
        .max_offset_from_bottom
        .saturating_sub(metrics.offset_from_bottom);
    let thumb_top = if max_thumb_top == 0 || metrics.max_offset_from_bottom == 0 {
        0
    } else {
        ((scrolled_from_top * max_thumb_top) as f32 / metrics.max_offset_from_bottom as f32)
            .round()
            .clamp(0.0, max_thumb_top as f32) as usize
    };

    Some(ScrollbarThumb {
        top: track.y + thumb_top as u16,
        len: thumb_len as u16,
    })
}

pub fn scrollbar_thumb_grab_offset(metrics: ScrollMetrics, track: Rect, row: u16) -> Option<u16> {
    let thumb = scrollbar_thumb(metrics, track)?;
    // Lazy: the subtraction underflows on rows above the thumb.
    (row >= thumb.top && row < thumb.top + thumb.len).then(|| row - thumb.top)
}

fn scrollbar_offset_from_thumb_top(metrics: ScrollMetrics, track: Rect, thumb_top: usize) -> usize {
    if metrics.max_offset_from_bottom == 0 {
        return 0;
    }

    let thumb_len = scrollbar_thumb(metrics, track)
        .map(|thumb| thumb.len as usize)
        .unwrap_or(1);
    let max_thumb_top = track.height as usize - thumb_len.min(track.height as usize);
    if max_thumb_top == 0 {
        return 0;
    }

    let desired_top = thumb_top.min(max_thumb_top);
    let scrolled_from_top = ((desired_top * metrics.max_offset_from_bottom) as f32
        / max_thumb_top as f32)
        .round() as usize;
    metrics
        .max_offset_from_bottom
        .saturating_sub(scrolled_from_top)
}

pub fn scrollbar_offset_from_row(metrics: ScrollMetrics, track: Rect, row: u16) -> usize {
    let thumb = match scrollbar_thumb(metrics, track) {
        Some(thumb) => thumb,
        None => return 0,
    };
    let clamped_row = row.clamp(track.y, track.y + track.height.saturating_sub(1));
    let row_offset = clamped_row.saturating_sub(track.y) as usize;
    let thumb_center = (thumb.len as usize) / 2;
    let desired_top = row_offset.saturating_sub(thumb_center);
    scrollbar_offset_from_thumb_top(metrics, track, desired_top)
}

pub fn scrollbar_offset_from_drag_row(
    metrics: ScrollMetrics,
    track: Rect,
    row: u16,
    grab_row_offset: u16,
) -> usize {
    let clamped_row = row.clamp(track.y, track.y + track.height.saturating_sub(1));
    let row_offset = clamped_row.saturating_sub(track.y) as usize;
    let desired_top = row_offset.saturating_sub(grab_row_offset as usize);
    scrollbar_offset_from_thumb_top(metrics, track, desired_top)
}

pub fn render_scrollbar(
    frame: &mut Frame,
    metrics: ScrollMetrics,
    track: Rect,
    track_color: Color,
    thumb_color: Color,
    thumb_symbol: &str,
) {
    if metrics.max_offset_from_bottom == 0 {
        return;
    }

    let Some(thumb) = scrollbar_thumb(metrics, track) else {
        return;
    };

    let buf = frame.buffer_mut();
    for y in track.y..track.y + track.height {
        let cell = &mut buf[(track.x, y)];
        cell.set_symbol("▕");
        cell.set_style(Style::default().fg(track_color));
    }
    for y in thumb.top..thumb.top + thumb.len {
        let cell = &mut buf[(track.x, y)];
        cell.set_symbol(thumb_symbol);
        cell.set_style(Style::default().fg(thumb_color));
    }
}

/// Scrollbar for one pane in its resolved lane (`info.scrollbar_rect`), with
/// the focused pane drawn in the stronger track and thumb pair.
pub fn render_pane_scrollbar(
    frame: &mut Frame,
    info: &PaneInfo,
    metrics: ScrollMetrics,
    p: &Palette,
) {
    let Some(track) = pane_scrollbar_rect(info) else {
        return;
    };

    let (track_color, thumb_color, thumb_symbol) = if info.is_focused {
        (p.overlay0, p.overlay1, "▐")
    } else {
        (p.surface_dim, p.overlay0, "▕")
    };

    render_scrollbar(
        frame,
        metrics,
        track,
        track_color,
        thumb_color,
        thumb_symbol,
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn metrics(offset: usize, max: usize, rows: usize) -> ScrollMetrics {
        ScrollMetrics {
            offset_from_bottom: offset,
            max_offset_from_bottom: max,
            viewport_rows: rows,
        }
    }

    #[test]
    fn thumb_sits_at_bottom_when_not_scrolled_back() {
        let track = Rect::new(10, 2, 1, 10);
        let thumb = scrollbar_thumb(metrics(0, 90, 10), track).unwrap();
        assert_eq!(thumb.len, 1);
        assert_eq!(thumb.top, track.y + track.height - 1);
        assert!(scrollbar_thumb(metrics(0, 0, 10), track).is_none());
    }

    #[test]
    fn row_click_and_drag_map_back_to_offsets() {
        let track = Rect::new(0, 0, 1, 10);
        let m = metrics(0, 90, 10);
        assert_eq!(scrollbar_offset_from_row(m, track, 0), 90);
        assert_eq!(scrollbar_offset_from_row(m, track, 9), 0);
        assert_eq!(scrollbar_thumb_grab_offset(m, track, 9), Some(0));
        assert_eq!(scrollbar_thumb_grab_offset(m, track, 3), None);
        assert_eq!(scrollbar_offset_from_drag_row(m, track, 0, 0), 90);
    }
}
