use super::*;
use crate::app::{Backend, Pane, PaneId};
use crate::theme::{Theme, ThemeKind};
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

fn paint(backend: &str, frame: FrameData, area: Rect, size: (u16, u16)) -> Terminal<TestBackend> {
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
    let palette = Theme::new(ThemeKind::Dark).palette();
    let mut terminal = Terminal::new(TestBackend::new(size.0, size.1)).expect("test backend");
    terminal
        .draw(|f| render(f, area, &pane, focused, &palette))
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

    let palette = Theme::new(ThemeKind::Dark).palette();
    let mut terminal = Terminal::new(TestBackend::new(20, 4)).expect("test backend");
    terminal
        .draw(|f| {
            render(f, Rect::new(0, 0, 10, 4), &focused, true, &palette);
            render(f, Rect::new(10, 0, 10, 4), &other, false, &palette);
        })
        .expect("draw frame");

    assert_eq!(
        terminal.get_cursor_position().expect("cursor"),
        (2, 1).into(),
        "the unfocused pane drawn after the focused one took the cursor"
    );
}

/// A hosted terminal sends its default colours as wire 0 (`Color::Reset`).
/// Written through, that cleared the chrome's `panel_bg` fill, so the light
/// theme showed the outer terminal's own dark background inside every pane.
#[test]
fn default_cells_take_the_theme_terminal_colours() {
    let named_red = 2;
    let indexed_42 = (1 << 24) | 42;
    let rgb = (2 << 24) | 0x12_34_56;
    let mut source = coordinate_frame(4, 1, None);
    (source.cells[1].fg, source.cells[1].bg) = (named_red, indexed_42);
    (source.cells[2].fg, source.cells[2].bg) = (rgb, rgb);
    // Each channel resolves on its own: a default fg over an explicit bg.
    source.cells[3].bg = named_red;

    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let palette = Theme::new(kind).palette();
        let mut pane = Pane::new_detached(PaneId(1), "term-1", Backend::Native, "epoch");
        pane.latest_frame = Some(source.clone());
        let mut terminal = Terminal::new(TestBackend::new(4, 1)).expect("test backend");
        terminal
            .draw(|f| render(f, Rect::new(0, 0, 4, 1), &pane, true, &palette))
            .expect("draw frame");
        let colours = |x| {
            let cell = &terminal.backend().buffer()[(x, 0)];
            (cell.fg, cell.bg)
        };

        assert_eq!(
            colours(0),
            (palette.text, palette.panel_bg),
            "{kind:?}: default cell"
        );
        assert_eq!(
            colours(1),
            (Color::Red, Color::Indexed(42)),
            "{kind:?}: named, indexed"
        );
        assert_eq!(
            colours(2),
            (Color::Rgb(0x12, 0x34, 0x56), Color::Rgb(0x12, 0x34, 0x56)),
            "{kind:?}: RGB"
        );
        assert_eq!(
            colours(3),
            (palette.text, Color::Red),
            "{kind:?}: default fg only"
        );
    }
}
