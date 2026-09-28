use gobby_client::theme::{Palette, Theme, ThemeKind};
use gobby_client::ui::marks::{
    goblin_large, goblin_small, parse, render_mark, wordmark, wordmark_shadow, MarkKind,
    MarkPalette,
};
use ratatui::backend::TestBackend;
use ratatui::style::Color;
use ratatui::Terminal;

fn palette(kind: ThemeKind) -> Palette {
    Theme::new(kind).palette()
}

#[test]
fn halfblock_grid_parses_to_its_declared_size() {
    let mark = parse("# halfblock 2x1\n# committed mark comment\n.aid\n").unwrap();
    assert_eq!(mark.kind, MarkKind::Halfblock);
    assert_eq!((mark.cols, mark.rows), (2, 1));

    for (mark, kind, size) in [
        (goblin_large(), MarkKind::Halfblock, (33, 16)),
        (goblin_small(), MarkKind::Halfblock, (29, 14)),
        (wordmark(), MarkKind::Braille, (54, 8)),
        (wordmark_shadow(), MarkKind::Halfblock, (49, 9)),
    ] {
        assert_eq!(mark.kind, kind);
        assert_eq!((mark.cols, mark.rows), size);
    }
}

#[test]
fn malformed_marks_report_the_line_and_reason() {
    for (input, line, reason) in [
        ("# unknown 1x1\na.\n", 1, "header"),
        ("# halfblock 1x1\nz.\n", 2, "role"),
        ("# halfblock 2x1\na.\n", 2, "width"),
        ("# halfblock 1x1\na..\n", 2, "width"),
        ("# braille 1x1\nA\n", 2, "braille"),
        ("# braille 1x1\n⠁ \n", 2, "width"),
        ("# halfblock 1x1\na.", 2, "LF"),
        ("# halfblock 1x2\na.\n", 3, "rows"),
        ("# halfblock 1x1\na.\na.\n", 3, "rows"),
    ] {
        let error = parse(input).unwrap_err().to_string();
        assert!(error.contains(&format!("line {line}")), "{error}");
        assert!(error.contains(reason), "{error}");
    }
}

#[test]
fn halfblock_cells_paint_upper_and_lower_halves_by_the_stated_rule() {
    let mark = parse("# halfblock 4x1\n..a..oai\n").unwrap();
    let colors = palette(ThemeKind::Dark);
    let ink = MarkPalette::normal(&colors);
    let mut terminal = Terminal::new(TestBackend::new(4, 1)).unwrap();
    terminal
        .draw(|frame| {
            for x in 0..4 {
                let cell = frame.buffer_mut().cell_mut((x, 0)).unwrap();
                cell.set_symbol("Q").set_bg(Color::Blue);
            }
            render_mark(frame, (0, 0), &mark, &ink);
        })
        .unwrap();
    let cells = terminal.backend().buffer();
    assert_eq!(cells[(0, 0)].symbol(), "Q");
    assert_eq!(cells[(0, 0)].bg, Color::Blue);
    assert_eq!(cells[(1, 0)].symbol(), "▀");
    assert_eq!(cells[(1, 0)].fg, colors.wordmark);
    assert_eq!(cells[(1, 0)].bg, Color::Blue);
    assert_eq!(cells[(2, 0)].symbol(), "▄");
    assert_eq!(cells[(2, 0)].fg, colors.overlay1);
    assert_eq!(cells[(2, 0)].bg, Color::Blue);
    assert_eq!(cells[(3, 0)].symbol(), "▀");
    assert_eq!(cells[(3, 0)].fg, colors.accent);
    assert_eq!(cells[(3, 0)].bg, colors.ink);

    let edge = parse("# halfblock 2x1\na.io\n").unwrap();
    let mut clipped = Terminal::new(TestBackend::new(2, 1)).unwrap();
    clipped
        .draw(|frame| render_mark(frame, (1, 0), &edge, &ink))
        .unwrap();
    assert_eq!(clipped.backend().buffer()[(1, 0)].symbol(), "▀");
}

#[test]
fn braille_glyphs_paint_in_the_given_role_and_blank_cells_stay_transparent() {
    let mark = parse("# braille 2x1\n⠀⠁\n").unwrap();
    let colors = palette(ThemeKind::Light);
    let normal = MarkPalette::normal(&colors);
    let shadow = MarkPalette::shadow(&colors);
    let mut terminal = Terminal::new(TestBackend::new(2, 2)).unwrap();
    terminal
        .draw(|frame| {
            for y in 0..2 {
                for x in 0..2 {
                    frame
                        .buffer_mut()
                        .cell_mut((x, y))
                        .unwrap()
                        .set_symbol("Q")
                        .set_bg(Color::Blue);
                }
            }
            render_mark(frame, (0, 0), &mark, &normal);
            render_mark(frame, (0, 1), &mark, &shadow);
        })
        .unwrap();
    let cells = terminal.backend().buffer();
    for y in 0..2 {
        assert_eq!(cells[(0, y)].symbol(), "Q");
        assert_eq!(cells[(1, y)].symbol(), "⠁");
        assert_eq!(cells[(1, y)].bg, Color::Blue);
    }
    assert_eq!(cells[(1, 0)].fg, colors.wordmark);
    assert_eq!(cells[(1, 1)].fg, colors.dim);
}

#[test]
fn dimmed_palette_drops_glints_and_uses_theme_fill_and_lines() {
    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let colors = palette(kind);
        let dimmed = MarkPalette::dimmed(&colors, kind);
        let (fill, lines) = match kind {
            ThemeKind::Dark => (colors.overlay0, colors.panel_bg),
            ThemeKind::Light => (colors.surface1, colors.overlay0),
        };
        assert_eq!(dimmed.accent, Some(fill));
        assert_eq!(dimmed.overlay1, Some(lines));
        assert_eq!(dimmed.ink, Some(lines));
        assert_eq!(dimmed.dim, Some(lines));
        assert_eq!(dimmed.glint, None);
    }
}
