//! Terminal frame renderer for a workspace pane.

use crate::app::Pane;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};
use ratatui::Frame;

/// Paint the latest semantic frame, clipping native panes and centering tmux panes.
pub fn render(frame: &mut Frame<'_>, area: Rect, pane: &Pane) {
    let Some(grid) = pane.latest_frame() else {
        return;
    };
    if grid.cells.len() != usize::from(grid.width) * usize::from(grid.height) {
        return;
    }

    let width = area.width.min(grid.width);
    let height = area.height.min(grid.height);
    let letterbox = pane.backend == "tmux";
    let dst_x = area.x + letterbox.then_some((area.width - width) / 2).unwrap_or(0);
    let dst_y = area.y + letterbox.then_some((area.height - height) / 2).unwrap_or(0);
    let src_x = letterbox.then_some((grid.width - width) / 2).unwrap_or(0);
    let src_y = letterbox.then_some((grid.height - height) / 2).unwrap_or(0);

    for row in 0..height {
        for col in 0..width {
            let index =
                usize::from(src_y + row) * usize::from(grid.width) + usize::from(src_x + col);
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

    if let Some(cursor) = &grid.cursor {
        if cursor.visible
            && cursor.x >= src_x
            && cursor.x < src_x + width
            && cursor.y >= src_y
            && cursor.y < src_y + height
        {
            frame.set_cursor_position((dst_x + cursor.x - src_x, dst_y + cursor.y - src_y));
        }
    }
}

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
