//! Terminal frame renderer for a workspace pane.

use crate::app::Pane;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::Frame;

/// Paint the latest semantic frame from its origin.
///
/// `focused` decides whether this pane may place the terminal cursor.
/// ratatui's `set_cursor_position` is last-write-wins across a frame, so a
/// pane that answers it unconditionally is really bidding on paint order:
/// with a split open the cursor ends up in whichever pane was drawn last,
/// which is not where the user is typing.
///
/// A hosted terminal sends its default fg and bg as wire 0; they are written
/// through as `Color::Reset`, so the pane sits on the same ground as the
/// chrome around it: the theme's under Dark and Light, the hosting
/// terminal's own under System.
pub fn render(frame: &mut Frame<'_>, area: Rect, pane: &Pane, focused: bool) {
    let Some(grid) = pane.latest_frame() else {
        return;
    };
    if grid.cells.len() != usize::from(grid.width) * usize::from(grid.height) {
        return;
    }

    let width = area.width.min(grid.width);
    let height = area.height.min(grid.height);
    let (dst_x, dst_y) = (area.x, area.y);

    for row in 0..height {
        for col in 0..width {
            let index = usize::from(row) * usize::from(grid.width) + usize::from(col);
            let source = &grid.cells[index];
            if source.skip {
                continue;
            }
            if let Some(cell) = frame.buffer_mut().cell_mut((dst_x + col, dst_y + row)) {
                cell.set_symbol(&source.symbol);
                cell.fg = decode_color(source.fg);
                cell.bg = decode_color(source.bg);
                cell.modifier = Modifier::from_bits_truncate(source.modifier & 0x0fff);
            }
        }
    }

    if !focused {
        return;
    }
    if let Some(cursor) = &grid.cursor {
        if cursor.visible && cursor.x < width && cursor.y < height {
            frame.set_cursor_position((dst_x + cursor.x, dst_y + cursor.y));
        }
    }
}

/// Decode a wire colour; the terminal default (wire 0) stays `Color::Reset`.
fn decode_color(value: u32) -> Color {
    match value >> 24 {
        0 => match value & 0xff {
            1 => Color::Black,
            2 => Color::Red,
            3 => Color::Green,
            4 => Color::Yellow,
            5 => Color::Blue,
            6 => Color::Magenta,
            7 => Color::Cyan,
            8 => Color::Gray,
            9 => Color::DarkGray,
            10 => Color::LightRed,
            11 => Color::LightGreen,
            12 => Color::LightYellow,
            13 => Color::LightBlue,
            14 => Color::LightMagenta,
            15 => Color::LightCyan,
            16 => Color::White,
            _ => Color::Reset,
        },
        1 => Color::Indexed((value & 0xff) as u8),
        2 => Color::Rgb(
            ((value >> 16) & 0xff) as u8,
            ((value >> 8) & 0xff) as u8,
            (value & 0xff) as u8,
        ),
        _ => Color::Reset,
    }
}

#[cfg(test)]
#[path = "grid/tests.rs"]
mod tests;
