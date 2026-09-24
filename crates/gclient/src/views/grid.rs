//! Terminal frame renderer for a workspace pane.

use crate::app::{Backend, Pane};
use crate::theme::Palette;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::Frame;

/// Paint the latest semantic frame from its origin, letterboxing a tmux pane
/// that is smaller than the area it was given.
///
/// `focused` decides whether this pane may place the terminal cursor.
/// ratatui's `set_cursor_position` is last-write-wins across a frame, so a
/// pane that answers it unconditionally is really bidding on paint order:
/// with a split open the cursor ends up in whichever pane was drawn last,
/// which is not where the user is typing.
///
/// `palette` supplies the colours a default cell stands for: a hosted
/// terminal sends its default fg and bg as `Color::Reset`, and writing that
/// through would show the outer terminal's colours inside the pane.
pub fn render(frame: &mut Frame<'_>, area: Rect, pane: &Pane, focused: bool, palette: &Palette) {
    let Some(grid) = pane.latest_frame() else {
        return;
    };
    if grid.cells.len() != usize::from(grid.width) * usize::from(grid.height) {
        return;
    }

    let width = area.width.min(grid.width);
    let height = area.height.min(grid.height);
    // A tmux pane carries a geometry of its own, so a frame smaller than the
    // viewport is letterboxed into the middle of it.
    //
    // An oversized frame is the opposite case and is read from its origin. A
    // terminal is anchored there — column 0 begins every line, row 0 is the
    // earliest output — so the far edges are what may be clipped, exactly as
    // the native branch already does. Centering the source instead cropped the
    // prompt and the left of every line away, which rendered a pane wider than
    // its viewport as an empty body.
    let (dst_x, dst_y) = if pane.backend == Backend::Tmux {
        (
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
        )
    } else {
        (area.x, area.y)
    };

    for row in 0..height {
        for col in 0..width {
            let index = usize::from(row) * usize::from(grid.width) + usize::from(col);
            let source = &grid.cells[index];
            if source.skip {
                continue;
            }
            if let Some(cell) = frame.buffer_mut().cell_mut((dst_x + col, dst_y + row)) {
                cell.set_symbol(&source.symbol);
                cell.fg = decode_color(source.fg, palette.text);
                cell.bg = decode_color(source.bg, palette.panel_bg);
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

/// Decode a wire colour; the terminal default (wire 0) becomes `default`.
fn decode_color(value: u32, default: Color) -> Color {
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
            _ => default,
        },
        1 => Color::Indexed((value & 0xff) as u8),
        2 => Color::Rgb(
            ((value >> 16) & 0xff) as u8,
            ((value >> 8) & 0xff) as u8,
            (value & 0xff) as u8,
        ),
        _ => default,
    }
}

#[cfg(test)]
#[path = "grid/tests.rs"]
mod tests;
