//! Terminal frame renderer for a workspace pane.

use crate::app::{Backend, Pane};
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
pub fn render(frame: &mut Frame<'_>, area: Rect, pane: &Pane, focused: bool) {
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
mod tests {
    use super::*;
    use crate::app::{Backend, Pane, PaneId};
    use gobby_terminal::protocol::{CellData, CursorState, FrameData};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    /// A frame whose every cell names its own coordinate, so a capture says
    /// exactly which region of the source was painted.
    fn coordinate_frame(width: u16, height: u16, cursor: Option<CursorState>) -> FrameData {
        let cells = (0..height)
            .flat_map(|y| {
                (0..width).map(move |x| CellData {
                    symbol: char::from(
                        b'a' + u8::try_from((usize::from(y) * 7 + usize::from(x)) % 26)
                            .expect("index fits"),
                    )
                    .to_string(),
                    fg: 0,
                    bg: 0,
                    modifier: 0,
                    skip: false,
                    hyperlink: None,
                })
            })
            .collect();
        FrameData {
            cells,
            width,
            height,
            cursor,
            hyperlinks: Vec::new(),
            graphics: Vec::new(),
            modes: Default::default(),
        }
    }

    fn paint(
        backend: &str,
        frame: FrameData,
        area: Rect,
        size: (u16, u16),
    ) -> Terminal<TestBackend> {
        paint_pane(backend, frame, area, size, true)
    }

    /// `paint`, with a say in whether the pane is the focused one.
    fn paint_pane(
        backend: &str,
        frame: FrameData,
        area: Rect,
        size: (u16, u16),
        focused: bool,
    ) -> Terminal<TestBackend> {
        let mut pane = Pane::new_detached(PaneId(1), "term-1", Backend::parse(backend), "epoch");
        pane.latest_frame = Some(frame);
        let mut terminal = Terminal::new(TestBackend::new(size.0, size.1)).expect("test backend");
        terminal
            .draw(|f| render(f, area, &pane, focused))
            .expect("draw frame");
        terminal
    }

    fn symbol_at(terminal: &Terminal<TestBackend>, x: u16, y: u16) -> String {
        terminal.backend().buffer()[(x, y)].symbol().to_string()
    }

    /// The defect this file's comment describes: a 200x50 tmux pane rendered
    /// into a smaller viewport showed an empty body because the source was
    /// centred, cutting away the prompt and the left of every line.
    #[test]
    fn an_oversized_tmux_frame_is_painted_from_its_origin() {
        let source = coordinate_frame(200, 50, None);
        let area = Rect::new(0, 0, 94, 38);
        let terminal = paint("tmux", source.clone(), area, (94, 38));

        for (x, y) in [(0, 0), (1, 0), (0, 1), (93, 37)] {
            let index = usize::from(y) * usize::from(source.width) + usize::from(x);
            assert_eq!(
                symbol_at(&terminal, x, y),
                source.cells[index].symbol,
                "viewport ({x}, {y}) must show the frame's own ({x}, {y})"
            );
        }
    }

    /// The letterbox itself is the reason this branch exists, so it has to
    /// survive: a frame smaller than its area still sits in the middle of it.
    #[test]
    fn an_undersized_tmux_frame_is_still_centred() {
        let source = coordinate_frame(10, 4, None);
        let terminal = paint("tmux", source.clone(), Rect::new(0, 0, 20, 10), (20, 10));

        assert_eq!(symbol_at(&terminal, 5, 3), source.cells[0].symbol);
        assert_eq!(symbol_at(&terminal, 4, 3), " ", "left of the letterbox");
        assert_eq!(symbol_at(&terminal, 5, 2), " ", "above the letterbox");
    }

    #[test]
    fn a_native_frame_is_unchanged_by_the_letterbox_branch() {
        let source = coordinate_frame(10, 4, None);
        let terminal = paint("native", source.clone(), Rect::new(0, 0, 20, 10), (20, 10));
        assert_eq!(symbol_at(&terminal, 0, 0), source.cells[0].symbol);

        let wide = coordinate_frame(200, 50, None);
        let clipped = paint("native", wide.clone(), Rect::new(0, 0, 94, 38), (94, 38));
        assert_eq!(symbol_at(&clipped, 0, 0), wide.cells[0].symbol);
    }

    #[test]
    fn the_cursor_follows_the_same_origin_and_leaves_the_viewport_when_clipped() {
        let cursor = |x, y| {
            Some(CursorState {
                x,
                y,
                visible: true,
                shape: Default::default(),
            })
        };
        let mut inside = paint(
            "tmux",
            coordinate_frame(200, 50, cursor(3, 2)),
            Rect::new(0, 0, 94, 38),
            (94, 38),
        );
        assert_eq!(inside.get_cursor_position().expect("cursor"), (3, 2).into());

        // A cursor beyond the clipped region has nowhere to sit; painting it at
        // the edge would claim a position the pane does not have.
        let mut outside = paint(
            "tmux",
            coordinate_frame(200, 50, cursor(120, 2)),
            Rect::new(0, 0, 94, 38),
            (94, 38),
        );
        assert_ne!(
            outside.get_cursor_position().expect("cursor"),
            (120, 2).into()
        );

        let mut letterboxed = paint(
            "tmux",
            coordinate_frame(10, 4, cursor(1, 1)),
            Rect::new(0, 0, 20, 10),
            (20, 10),
        );
        assert_eq!(
            letterboxed.get_cursor_position().expect("cursor"),
            (6, 4).into()
        );
    }

    /// A frame has one cursor and ratatui keeps the last write, so with a
    /// split open paint order decides where it lands unless the unfocused
    /// panes decline it. The focused pane is painted FIRST here for that
    /// reason: painted last it would win by accident, and this test would
    /// pass against a `render` that claims the cursor unconditionally.
    #[test]
    fn only_the_focused_pane_places_the_cursor() {
        let cursor = |x, y| {
            Some(CursorState {
                x,
                y,
                visible: true,
                shape: Default::default(),
            })
        };
        let mut focused = Pane::new_detached(PaneId(1), "term-1", Backend::Native, "epoch");
        focused.latest_frame = Some(coordinate_frame(10, 4, cursor(2, 1)));
        let mut other = Pane::new_detached(PaneId(2), "term-2", Backend::Native, "epoch");
        other.latest_frame = Some(coordinate_frame(10, 4, cursor(3, 3)));

        let mut terminal = Terminal::new(TestBackend::new(20, 4)).expect("test backend");
        terminal
            .draw(|f| {
                render(f, Rect::new(0, 0, 10, 4), &focused, true);
                render(f, Rect::new(10, 0, 10, 4), &other, false);
            })
            .expect("draw frame");

        assert_eq!(
            terminal.get_cursor_position().expect("cursor"),
            (2, 1).into(),
            "the unfocused pane drawn after the focused one took the cursor"
        );
    }
}
