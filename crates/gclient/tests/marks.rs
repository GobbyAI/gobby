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
        (goblin_large(), MarkKind::Halfblock, (41, 18)),
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
    let ink = MarkPalette::normal(&colors, false);
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
    let normal = MarkPalette::normal(&colors, false);
    let shadow = MarkPalette::shadow(&colors, false);
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
fn dimmed_palette_keeps_every_role_distinct_with_the_glint() {
    // Josh's 16:54 empty-tab palette (#23280): every grid role keeps its own
    // token, and the glint stays so the eyes stay white.
    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let colors = palette(kind);
        let dimmed = MarkPalette::dimmed(&colors, kind);
        let (accent, overlay1, ink, glint) = match kind {
            ThemeKind::Dark => (
                colors.overlay0,
                colors.dim,
                colors.panel_bg,
                colors.subtext0,
            ),
            ThemeKind::Light => (
                colors.surface1,
                colors.overlay0,
                colors.subtext0,
                colors.panel_bg,
            ),
        };
        assert_eq!(dimmed.accent, Some(accent), "{kind:?} accent");
        assert_eq!(dimmed.overlay1, Some(overlay1), "{kind:?} overlay1");
        assert_eq!(dimmed.ink, Some(ink), "{kind:?} ink");
        assert_eq!(dimmed.glint, Some(glint), "{kind:?} glint");
    }
}

/// Every palette a goblin surface paints `goblin_large` with: the splash in
/// each theme and in monochrome, and the empty tab's dimmed mark in each.
fn goblin_variants() -> Vec<(String, MarkPalette)> {
    let mut variants = Vec::new();
    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let theme = Theme::new(kind);
        for (shade, colors, monochrome) in [
            ("colour", theme.palette(), false),
            ("mono", Palette::monochrome(&theme), true),
        ] {
            variants.push((
                format!("{kind:?} {shade} normal"),
                MarkPalette::normal(&colors, monochrome),
            ));
            variants.push((
                format!("{kind:?} {shade} dimmed"),
                MarkPalette::dimmed(&colors, kind),
            ));
        }
    }
    variants
}

/// `goblin_large` drawn with `palette` on a blank ground: per cell its
/// symbol, foreground and background.
fn draw_goblin(palette: &MarkPalette) -> Vec<Vec<(String, Color, Color)>> {
    let mark = goblin_large();
    let mut terminal = Terminal::new(TestBackend::new(mark.cols, mark.rows)).unwrap();
    terminal
        .draw(|frame| render_mark(frame, (0, 0), mark, palette))
        .unwrap();
    let buffer = terminal.backend().buffer();
    (0..mark.rows)
        .map(|y| {
            (0..mark.cols)
                .map(|x| {
                    let cell = &buffer[(x, y)];
                    (cell.symbol().to_string(), cell.fg, cell.bg)
                })
                .collect()
        })
        .collect()
}

/// Which halves of each cell carry ink: the shape a variant draws, with
/// its colours dropped.
fn cell_mask(cells: &[Vec<(String, Color, Color)>]) -> Vec<Vec<(bool, bool)>> {
    cells
        .iter()
        .map(|row| {
            row.iter()
                .map(|(symbol, _, bg)| match symbol.as_str() {
                    "▀" => (true, *bg != Color::Reset),
                    "▄" => (false, true),
                    _ => (false, false),
                })
                .collect()
        })
        .collect()
}

#[test]
fn every_goblin_variant_draws_the_same_cells() {
    // Josh via the Assistant, PD ruling 15:44 (#23280 criterion 2): one
    // grid feeds every variant, and only the colours differ.
    let variants = goblin_variants();
    let reference = cell_mask(&draw_goblin(&variants[0].1));
    for (name, palette) in &variants[1..] {
        assert_eq!(
            cell_mask(&draw_goblin(palette)),
            reference,
            "{name} draws different cells from {}",
            variants[0].0
        );
    }
}

#[test]
fn the_goblin_wears_its_node_halo_in_every_variant() {
    // Josh's halo option A (#23280): network nodes and edges ring the head,
    // nodes info blue and edges overlay0, or dim and surface1 in mono.
    let halo_only = |node, edge| MarkPalette {
        accent: None,
        overlay1: None,
        ink: None,
        glint: None,
        dim: None,
        node,
        edge,
        braille: Color::White,
    };
    let painted = |palette: &MarkPalette| {
        cell_mask(&draw_goblin(palette))
            .iter()
            .flatten()
            .filter(|&&(upper, lower)| upper || lower)
            .count()
    };
    assert!(
        painted(&halo_only(Some(Color::White), None)) > 0,
        "no nodes"
    );
    assert!(
        painted(&halo_only(None, Some(Color::White))) > 0,
        "no edges"
    );

    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let theme = Theme::new(kind);
        let colour = theme.palette();
        let normal = MarkPalette::normal(&colour, false);
        assert_eq!(normal.node, Some(colour.blue), "{kind:?} node");
        assert_eq!(normal.edge, Some(colour.overlay0), "{kind:?} edge");
        let grays = Palette::monochrome(&theme);
        let mono = MarkPalette::normal(&grays, true);
        assert_eq!(mono.node, Some(grays.dim), "{kind:?} mono node");
        assert_eq!(mono.edge, Some(grays.surface1), "{kind:?} mono edge");
    }
    for (name, palette) in goblin_variants() {
        assert!(
            palette.node.is_some() && palette.edge.is_some(),
            "{name} drops the halo"
        );
    }
}

#[test]
fn both_goblin_eyes_render_identically_in_every_variant() {
    // The glint marks each eye; a glint-only palette finds them.
    let only_glint = MarkPalette {
        accent: None,
        overlay1: None,
        ink: None,
        glint: Some(Color::White),
        dim: None,
        node: None,
        edge: None,
        braille: Color::White,
    };
    let glints: Vec<(usize, usize)> = draw_goblin(&only_glint)
        .iter()
        .enumerate()
        .flat_map(|(y, row)| {
            row.iter()
                .enumerate()
                .filter(|(_, (symbol, _, _))| symbol.trim() != "")
                .map(move |(x, _)| (x, y))
        })
        .collect();
    assert_eq!(glints.len(), 2, "one glint cell per eye: {glints:?}");
    for (name, palette) in goblin_variants() {
        let cells = draw_goblin(&palette);
        // Each eye's lens: the glint cell, two cells either side, and the
        // rows above and below.
        let lens = |(x, y): (usize, usize)| -> Vec<(String, Color, Color)> {
            (y - 1..=y + 1)
                .flat_map(|row| (x - 2..=x + 2).map(move |col| (row, col)))
                .map(|(row, col)| cells[row][col].clone())
                .collect()
        };
        assert_eq!(lens(glints[0]), lens(glints[1]), "{name} eyes differ");
    }
}
