//! The shipped theme catalog against Josh's board (#23416 "gclient sidebar
//! follow-ups: section header band colour, model colour,
//! provider-model-effort line"): every theme's fills in Dark, Light and
//! System, the floors those fills keep, the menu bar and the unfocused pane,
//! the themes each appearance offers, and Host-matched's hue.

use gobby_client::app::{ContextMenuKind, ContextMenuState};
use gobby_client::theme::{
    contrast_ratio, LightReading, Palette, Theme, ThemeKind, ThemeName, Token, BRAND_HUE,
    GLYPH_CONTRAST, LIGHT_READING, TEXT_CONTRAST, UNFOCUSED_CONTRAST,
};
use gobby_client::ui::menu_bar::{render_menu_bar, MenuBarHits, MenuBarMenu};
use gobby_client::ui::Chrome;
use gobby_terminal::terminal_theme::{DefaultColorKind, RgbColor};
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::Terminal;

/// The board's System host: Catppuccin Mocha's base.
const HOST: (u8, u8, u8) = (0x1e, 0x1e, 0x2e);

#[test]
fn midnight_moss_has_the_approved_label_and_preference_key() {
    let theme = ThemeName::ALL
        .into_iter()
        .find(|theme| theme.label() == "Midnight moss")
        .expect("the approved theme name is offered");
    assert_eq!(serde_json::to_string(&theme).unwrap(), "\"midnight-moss\"");
    assert_eq!(
        serde_json::from_str::<ThemeName>("\"midnight-moss\"").unwrap(),
        theme
    );
    assert!(ThemeName::ALL
        .into_iter()
        .all(|theme| theme.label() != "Your proposal"));
    assert!(serde_json::from_str::<ThemeName>("\"your-proposal\"").is_err());
}

/// A light host for System under a light OS. The board draws no such cell,
/// so this is a fixture: Catppuccin Latte's base.
const LIGHT_HOST: (u8, u8, u8) = (0xef, 0xf1, 0xf5);

/// The board's Dark cells: ground, header fill, selection, unfocused pane
/// and model line.
const DARK: [(ThemeName, [&str; 5]); 9] = [
    (
        ThemeName::Restored,
        ["#0d0e0b", "#232521", "#4c4e4a", "#1c1e1b", "#89c4c6"],
    ),
    (
        ThemeName::Moss,
        ["#041225", "#232521", "#445427", "#102035", "#e1a79d"],
    ),
    (
        ThemeName::MidnightMoss,
        ["#000516", "#1e2b17", "#5a5b58", "#09192d", "#e1a79d"],
    ),
    (
        ThemeName::Staircase,
        ["#0d0400", "#2d2e2b", "#5d5e5b", "#231606", "#89c4c6"],
    ),
    (
        ThemeName::InverseBar,
        ["#1c140c", "#232521", "#d0d2cd", "#292119", "#89c4c6"],
    ),
    (
        ThemeName::GobbyBar,
        ["#000c13", "#232521", "#a7d91d", "#041e26", "#e1a79d"],
    ),
    (
        ThemeName::ContrastChrome,
        ["#2a1c0c", "#a4a5a1", "#dddfda", "#362817", "#89c4c6"],
    ),
    (
        ThemeName::MossChrome,
        ["#011a21", "#293510", "#caddab", "#0c272e", "#e1a79d"],
    ),
    (
        ThemeName::MossBand,
        ["#102034", "#262f15", "#575955", "#1b2b40", "#e1a79d"],
    ),
];

/// The board's System cells over `HOST`: header fill, selection and model
/// line. System has no unfocused fill: focus is the border alone (Josh's
/// pick (c)).
const SYSTEM: [(ThemeName, [&str; 3]); 9] = [
    (ThemeName::Restored, ["#373835", "#5f615d", "#89c4c6"]),
    (ThemeName::Moss, ["#373835", "#57673a", "#e1a79d"]),
    (ThemeName::MidnightMoss, ["#313d18", "#626460", "#e1a79d"]),
    (ThemeName::InverseBar, ["#353633", "#d0d2cd", "#89c4c6"]),
    (ThemeName::GobbyBar, ["#353633", "#a7d91d", "#e1a79d"]),
    (ThemeName::ContrastChrome, ["#a4a5a1", "#dddfda", "#89c4c6"]),
    (ThemeName::MossChrome, ["#2b380f", "#caddab", "#e1a79d"]),
    (ThemeName::Ink, ["#050604", "#474845", "#89c4c6"]),
    (ThemeName::HostMatched, ["#38394a", "#5e5e71", "#89c4c6"]),
];

/// The board's Light cells: ground, unfocused pane and model line, then the
/// header fill and selection under each reading of Josh's rule, literal
/// first (`LightReading`). Contrast chrome has no other-reading cell.
type LightCell = (ThemeName, [&'static str; 3], [Option<[&'static str; 2]>; 2]);
const LIGHT: [LightCell; 9] = [
    (
        ThemeName::Restored,
        ["#f9fbf6", "#e9ebe7", "#04585c"],
        [Some(["#9d9f9b", "#d3d5d1"]), Some(["#e5e7e3", "#b0b2ae"])],
    ),
    (
        ThemeName::Moss,
        ["#fffae8", "#f0ead7", "#7d3d34"],
        [Some(["#9d9f9b", "#c8d8ae"]), Some(["#e5e7e3", "#a1b879"])],
    ),
    (
        ThemeName::MidnightMoss,
        ["#f7ebca", "#e8dbbb", "#7d3d34"],
        [Some(["#979995", "#c6c8c4"]), Some(["#d6d8d4", "#9d9f9b"])],
    ),
    (
        ThemeName::Staircase,
        ["#d9eeff", "#c9dff2", "#04585c"],
        [Some(["#545652", "#c6c8c4"]), Some(["#d0d2cd", "#8b8d89"])],
    ),
    (
        ThemeName::InverseBar,
        ["#e9faff", "#daeaf1", "#04585c"],
        [Some(["#21221f", "#545652"]), Some(["#e5e7e3", "#2d2e2b"])],
    ),
    (
        ThemeName::GobbyBar,
        ["#fffad8", "#f4e8c7", "#7d3d34"],
        [Some(["#979995", "#a7d91d"]), Some(["#e5e7e3", "#3f6300"])],
    ),
    (
        ThemeName::ContrastChrome,
        ["#dff5ff", "#cfe5f8", "#04585c"],
        [Some(["#2d2e2b", "#c9cbc7"]), None],
    ),
    (
        ThemeName::MossChrome,
        ["#f8f1df", "#e9e2d0", "#7d3d34"],
        [Some(["#2d371a", "#c3d1ae"]), Some(["#d7e4c4", "#2d371a"])],
    ),
    (
        ThemeName::MossBand,
        ["#f1f2ee", "#e1e3df", "#7d3d34"],
        [Some(["#929e7f", "#cdceca"]), Some(["#d7e4c4", "#a4a5a1"])],
    ),
];

/// The ground's roles a fill re-inks, with the floor each keeps there:
/// words at AA, state glyphs and bright black at 3:1.
const ROLES: [(&str, f64); 10] = [
    ("text", TEXT_CONTRAST),
    ("subtext0", TEXT_CONTRAST),
    ("model", TEXT_CONTRAST),
    ("identifier", TEXT_CONTRAST),
    ("accent", TEXT_CONTRAST),
    ("yellow", GLYPH_CONTRAST),
    ("green", GLYPH_CONTRAST),
    ("red", GLYPH_CONTRAST),
    ("blue", GLYPH_CONTRAST),
    ("overlay1", GLYPH_CONTRAST),
];

fn role(p: &Palette, name: &str) -> Color {
    match name {
        "text" => p.text,
        "subtext0" => p.subtext0,
        "model" => p.model,
        "identifier" => p.identifier,
        "accent" => p.accent,
        "yellow" => p.yellow,
        "green" => p.green,
        "red" => p.red,
        "blue" => p.blue,
        "overlay1" => p.overlay1,
        other => panic!("no role {other}"),
    }
}

/// One appearance gclient draws in: Dark, Light, or System over a host.
#[derive(Debug, Clone, Copy)]
enum Appearance {
    Dark,
    Light,
    System(ThemeKind, (u8, u8, u8)),
}

const APPEARANCES: [Appearance; 4] = [
    Appearance::Dark,
    Appearance::Light,
    Appearance::System(ThemeKind::Dark, HOST),
    Appearance::System(ThemeKind::Light, LIGHT_HOST),
];

impl Appearance {
    fn kind(self) -> ThemeKind {
        match self {
            Appearance::Dark => ThemeKind::Dark,
            Appearance::Light => ThemeKind::Light,
            Appearance::System(kind, _) => kind,
        }
    }

    fn hosted(self) -> bool {
        matches!(self, Appearance::System(..))
    }

    fn theme(self, name: ThemeName) -> Theme {
        match self {
            Appearance::System(kind, host) => Theme::hosted(name, kind, Some(host)),
            _ => Theme::named(name, self.kind()),
        }
    }

    /// The colour panes sit on: the theme's ground, or the host's own in
    /// System.
    fn ground(self, palette: &Palette) -> (u8, u8, u8) {
        match self {
            Appearance::System(_, host) => host,
            _ => rgb(palette.panel_bg),
        }
    }

    /// Every theme this appearance offers, each drawn in colour and in
    /// monochrome.
    fn drawn(self) -> Vec<(String, Theme, Palette)> {
        ThemeName::ALL
            .into_iter()
            .filter(|name| name.offered(self.kind(), self.hosted()))
            .flat_map(|name| {
                let theme = self.theme(name);
                let colour = Palette::from_theme(&theme);
                let mono = Palette::monochrome(&theme);
                [
                    (format!("{name:?} {self:?}"), theme.clone(), colour),
                    (format!("{name:?} {self:?} monochrome"), theme, mono),
                ]
            })
            .collect()
    }
}

fn rgb(colour: Color) -> (u8, u8, u8) {
    match colour {
        Color::Rgb(r, g, b) => (r, g, b),
        other => panic!("{other:?} is not an RGB colour"),
    }
}

fn hex(colour: Color) -> String {
    let (r, g, b) = rgb(colour);
    format!("#{r:02x}{g:02x}{b:02x}")
}

fn ratio(a: Color, b: Color) -> f64 {
    contrast_ratio(rgb(a), rgb(b))
}

/// Every colour the palette paints with, the fills' inks included.
fn painted(p: &Palette) -> Vec<(String, Color)> {
    let mut colours: Vec<(String, Color)> = [
        ("panel_bg", p.panel_bg),
        ("surface0", p.surface0),
        ("surface1", p.surface1),
        ("surface_dim", p.surface_dim),
        ("overlay0", p.overlay0),
        ("ink", p.ink),
        ("glint", p.glint),
        ("dim", p.dim),
        ("band", p.band),
        ("selection", p.selection),
        ("line", p.line),
        ("wordmark", p.wordmark),
    ]
    .into_iter()
    .chain(p.unfocused.map(|fill| ("unfocused", fill)))
    .map(|(name, colour)| (name.to_string(), colour))
    .collect();
    for (fill, ink) in [("band", &p.band_ink), ("selection", &p.selection_ink)] {
        colours.push((format!("text on {fill}"), ink.text));
        for (name, _) in ROLES {
            colours.push((name.to_string(), role(p, name)));
            colours.push((format!("{name} on {fill}"), ink.ink(role(p, name))));
        }
    }
    colours
}

/// Whether `drawn` sits within one channel step of the board's `hex`. The
/// board finds the unfocused fill stepping in unrounded colour; gclient
/// steps on the colours it draws, so its fill may sit one step further out
/// to hold 1.15:1 as drawn (`unfocused_panes_step_off_the_ground_by_lightness_alone`).
fn near(drawn: Color, hex: &str) -> bool {
    let (r, g, b) = rgb(drawn);
    let board = u32::from_str_radix(&hex[1..], 16).expect("board hex");
    let board = [(board >> 16) as u8, (board >> 8) as u8, board as u8];
    [r, g, b]
        .into_iter()
        .zip(board)
        .all(|(drawn, board)| drawn.abs_diff(board) <= 1)
}

/// Criteria 2-5 and 14: each theme draws the board's ground, header fill,
/// selection, unfocused pane and model line, in every appearance it ships
/// in. The tab row takes the header fill, and the open menu title the
/// selection, so one value drives each.
#[test]
fn every_theme_draws_the_boards_fills_in_each_appearance() {
    let mut failures = Vec::new();
    let mut check = |case: String, drawn: &[Color], board: &[&str], unfocused| {
        let drawn: Vec<String> = drawn.iter().copied().map(hex).collect();
        if drawn != board {
            failures.push(format!("{case}: drawn {drawn:?}, board {board:?}"));
        }
        if let Some((fill, hex)) = unfocused {
            if !near(fill, hex) {
                failures.push(format!("{case}: unfocused {fill:?}, board {hex}"));
            }
        }
    };
    for (name, [ground, band, selection, unfocused, model]) in DARK {
        let theme = Theme::named(name, ThemeKind::Dark);
        let p = theme.palette();
        assert_eq!(theme.name, name);
        let fill = p.unfocused.expect("Dark fills unfocused panes");
        check(
            format!("{name:?} Dark"),
            &[p.panel_bg, p.band, p.selection, p.model],
            &[ground, band, selection, model],
            Some((fill, unfocused)),
        );
    }
    for (name, [band, selection, model]) in SYSTEM {
        let theme = Theme::hosted(name, ThemeKind::Dark, Some(HOST));
        let p = theme.palette();
        assert_eq!(theme.name, name);
        assert_eq!(
            p.unfocused, None,
            "{name:?} System marks focus by the border alone"
        );
        check(
            format!("{name:?} System"),
            &[p.band, p.selection, p.model],
            &[band, selection, model],
            None,
        );
    }
    for (name, [ground, unfocused, model], readings) in LIGHT {
        let theme = Theme::named(name, ThemeKind::Light);
        let p = theme.palette();
        let Some([band, selection]) = readings[LIGHT_READING as usize] else {
            assert_eq!(
                theme.name,
                ThemeName::Restored,
                "{name:?} has no Light cell"
            );
            continue;
        };
        assert_eq!(theme.name, name);
        let fill = p.unfocused.expect("Light fills unfocused panes");
        check(
            format!("{name:?} Light"),
            &[p.panel_bg, p.band, p.selection, p.model],
            &[ground, band, selection, model],
            Some((fill, unfocused)),
        );
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

/// Criteria 2, 4 and 14: in every appearance, colour and monochrome, the
/// header fill stands off the ground, the selection stands off the header
/// fill and the ground, the fills stay distinct, and nothing paints pure
/// black or white.
#[test]
fn every_offered_theme_keeps_the_fill_floors() {
    let mut failures = Vec::new();
    for appearance in APPEARANCES {
        for (case, _, p) in appearance.drawn() {
            let mut expect = |holds: bool, what: String| {
                if !holds {
                    failures.push(format!("{case}: {what}"));
                }
            };
            // The board's floors (selection 1.7:1 off the header fill and
            // 1.4:1 off the ground) bind the cells Josh approved, which are
            // colour over the board's grounds. Monochrome keeps the
            // selection 1.15:1 off the header fill (GobbyBar Light greys to
            // 1.65), and a light host the board never drew leaves the
            // selection-to-ground floor to the host.
            let mono = case.ends_with("monochrome");
            let on_board = !matches!(appearance, Appearance::System(ThemeKind::Light, _));
            let ground = appearance.ground(&p);
            let band = contrast_ratio(rgb(p.band), ground);
            expect(
                band >= UNFOCUSED_CONTRAST,
                format!("band on ground {band:.2}"),
            );
            let selection = ratio(p.selection, p.band);
            let floor = if mono { UNFOCUSED_CONTRAST } else { 1.7 };
            expect(
                selection >= floor,
                format!("selection on band {selection:.2}"),
            );
            let lifted = contrast_ratio(rgb(p.selection), ground);
            expect(
                !on_board || lifted >= 1.4,
                format!("selection on ground {lifted:.2}"),
            );
            // Josh's rule: the selection sits lighter than the header rows;
            // Light's other reading asks only that it sit further from the
            // ground.
            if appearance.kind() == ThemeKind::Light && LIGHT_READING == LightReading::Other {
                expect(lifted > band, "selection no further from the ground".into());
            } else {
                let lightness = |colour| contrast_ratio(rgb(colour), (0, 0, 0));
                expect(
                    lightness(p.selection) > lightness(p.band),
                    "selection not lighter than the header rows".into(),
                );
            }
            // Monochrome drops the hue that tells the tinted fills apart,
            // so there only the lightness floors above hold.
            let mut fills = vec![ground, rgb(p.band), rgb(p.selection)];
            fills.extend(p.unfocused.map(rgb));
            for (i, fill) in fills.iter().enumerate() {
                let repeats = fills[i + 1..].contains(fill);
                expect(!repeats || mono, format!("fills repeat {fills:?}"));
            }
            for (name, colour) in painted(&p) {
                let pure = [(0, 0, 0), (255, 255, 255)].contains(&rgb(colour));
                expect(!pure, format!("{name} is {}", hex(colour)));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

/// Criteria 2, 4 and 5: on the header fill and on the selection, every
/// word the ground draws (headings, model, refs, a selected project's name)
/// reaches AA and every state glyph 3:1, in colour and monochrome.
#[test]
fn fill_inks_reach_their_floors_on_every_fill() {
    let mut failures = Vec::new();
    for appearance in APPEARANCES {
        for (case, _, p) in appearance.drawn() {
            for (fill, ink) in [(p.band, &p.band_ink), (p.selection, &p.selection_ink)] {
                let case = format!("{case} on {}", hex(fill));
                assert_eq!(ink.fill, fill, "{case}");
                let text = ratio(ink.text, fill);
                assert!(text >= TEXT_CONTRAST, "{case}: text {text:.2}");
                for (name, floor) in ROLES {
                    let reached = ratio(ink.ink(role(&p, name)), fill);
                    if reached < floor {
                        failures.push(format!("{case}: {name} {reached:.2} < {floor}"));
                    }
                }
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

fn draw_menu_bar(chrome: &Chrome, width: u16) -> (MenuBarHits, Buffer) {
    let mut terminal = Terminal::new(TestBackend::new(width, 1)).expect("test backend");
    let mut hits = MenuBarHits::default();
    terminal
        .draw(|frame| hits = render_menu_bar(frame, Rect::new(0, 0, width, 1), chrome))
        .expect("draw frame");
    (hits, terminal.backend().buffer().clone())
}

/// Criteria 14 and 15: per theme and appearance, the menu bar sits on the
/// bar fill and the open title on the selection; both titles read at AA
/// and the bar stands at least 1.15:1 off the ground.
#[test]
fn menu_bar_takes_the_bar_fill_and_opens_on_the_selection() {
    const WIDTH: u16 = 80;
    let open = MenuBarMenu::ALL[1];
    for appearance in APPEARANCES {
        for (case, theme, p) in appearance.drawn() {
            let mut chrome = Chrome::new(theme);
            chrome.palette = p;
            chrome.menu = Some(ContextMenuState {
                kind: ContextMenuKind::MenuBar(open),
                anchor: (0, 1),
                items: Vec::new(),
                selected: 0,
                item_rects: Vec::new(),
                parent: None,
            });
            let (hits, buffer) = draw_menu_bar(&chrome, WIDTH);
            let title = hits
                .titles
                .iter()
                .find(|(index, _)| MenuBarMenu::ALL[*index] == open)
                .map(|(_, rect)| *rect)
                .unwrap_or_else(|| panic!("{case}: no {open:?} title"));
            let bar = (p.bar_ink.ink(p.subtext0), p.bar);
            let opened = (p.selection_ink.text, p.selection);
            for x in 0..WIDTH {
                let cell = &buffer[(x, 0)];
                let expected = if (title.left()..title.right()).contains(&x) {
                    opened
                } else {
                    bar
                };
                assert_eq!((cell.fg, cell.bg), expected, "{case}: x={x}");
            }
            for (label, (fg, bg)) in [("bar title", bar), ("open title", opened)] {
                let reached = ratio(fg, bg);
                assert!(reached >= TEXT_CONTRAST, "{case}: {label} {reached:.2}");
            }
            let off = contrast_ratio(rgb(p.bar), appearance.ground(&p));
            assert!(off >= UNFOCUSED_CONTRAST, "{case}: bar on ground {off:.2}");
        }
    }
}

/// #23626 C: the sidebar section headers and the unselected tabs keep the
/// band (`sidebar` and `tabs` tests pin them to it) and the menu bar takes
/// its own fill. In colour the bar stands 1.4:1 off the band, or at least
/// 1.25:1 where a further step would cost its titles AA, and keeps 1.15:1
/// off the ground and the selection. Monochrome keeps the bar apart by
/// lightness alone.
#[test]
fn headers_and_unselected_tabs_keep_the_band_and_the_menu_bar_steps_off_it() {
    let mut failures = Vec::new();
    for appearance in APPEARANCES {
        for (case, _, p) in appearance.drawn() {
            let mut expect = |holds: bool, what: String| {
                if !holds {
                    failures.push(format!("{case}: {what}"));
                }
            };
            expect(p.bar != p.band, format!("bar is the band {}", hex(p.band)));
            if case.ends_with("monochrome") {
                continue;
            }
            let off_band = ratio(p.bar, p.band);
            expect(off_band >= 1.25, format!("bar on band {off_band:.2}"));
            let off_ground = contrast_ratio(rgb(p.bar), appearance.ground(&p));
            expect(
                off_ground >= UNFOCUSED_CONTRAST,
                format!("bar on ground {off_ground:.2}"),
            );
            let off_selection = ratio(p.bar, p.selection);
            expect(
                off_selection >= UNFOCUSED_CONTRAST,
                format!("bar on selection {off_selection:.2}"),
            );
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));

    // The value the design board and fills.py give Restored Dark.
    let restored = Palette::from_theme(&Theme::named(ThemeName::Restored, ThemeKind::Dark));
    assert_eq!(hex(restored.bar), "#3c3e3a");
}

/// Criteria 14 and 15: Dark and Light set an unfocused pane a lightness
/// step off the ground, its hue and chroma kept; text reads at AA on both,
/// no terminal colour at AA on the focused pane drops below it on the
/// unfocused one, and bright black keeps 3:1 on both. System draws no
/// unfocused fill: the border alone marks focus (Josh's pick (c)).
#[test]
fn unfocused_panes_step_off_the_ground_by_lightness_alone() {
    let mut failures = Vec::new();
    for appearance in APPEARANCES {
        for (case, theme, p) in appearance.drawn() {
            if appearance.hosted() {
                assert!(theme.unfocused(Token::color).is_none(), "{case}");
                assert_eq!(p.unfocused, None, "{case}");
                continue;
            }
            let token = theme
                .unfocused(Token::color)
                .expect("Dark and Light fill unfocused panes");
            let ground_token = theme.neutrals.panel_bg;
            assert_eq!(token.hue, ground_token.hue, "{case}: hue moved");
            assert_eq!(token.chroma, ground_token.chroma, "{case}: chroma moved");
            assert_ne!(token.lightness, ground_token.lightness, "{case}");
            let ground = rgb(p.panel_bg);
            let fill = rgb(p.unfocused.expect("Dark and Light fill unfocused panes"));
            let step = contrast_ratio(fill, ground);
            if step < UNFOCUSED_CONTRAST {
                failures.push(format!("{case}: unfocused on ground {step:.3}"));
            }
            for (name, colour) in [("text", p.text), ("subtext0", p.subtext0)] {
                for (pane, bg) in [("focused", ground), ("unfocused", fill)] {
                    let reached = contrast_ratio(rgb(colour), bg);
                    assert!(
                        reached >= TEXT_CONTRAST,
                        "{case}: {name} on {pane} {reached:.2}"
                    );
                }
            }
            // Panes draw the terminal's colours in colour only.
            if case.ends_with("monochrome") {
                continue;
            }
            let terminal = theme.terminal_theme();
            for (slot, colour) in terminal.palette[..16].iter().enumerate() {
                let RgbColor { r, g, b } = colour.unwrap_or_else(|| panic!("{case}: slot {slot}"));
                let focused = contrast_ratio((r, g, b), ground);
                let unfocused = contrast_ratio((r, g, b), fill);
                if focused >= TEXT_CONTRAST && unfocused < TEXT_CONTRAST {
                    failures.push(format!(
                        "{case}: slot {slot} {focused:.2} focused, {unfocused:.2} unfocused"
                    ));
                }
                if slot == 8 && focused.min(unfocused) < GLYPH_CONTRAST {
                    failures.push(format!(
                        "{case}: bright black {focused:.2} focused, {unfocused:.2} unfocused"
                    ));
                }
                // Bright black stays the dim one: under white (slot 7).
                let white = terminal.palette[7].expect("white");
                let white = contrast_ratio((white.r, white.g, white.b), ground);
                if slot == 8 && focused >= white {
                    failures.push(format!(
                        "{case}: bright black {focused:.2}, white {white:.2}"
                    ));
                }
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

/// Criterion 12: each appearance's picker lists only the themes it draws,
/// and a theme it does not offer draws Restored. Ink and Host-matched exist
/// only in System over a dark host; Staircase and Moss band left System as
/// near-duplicates (Josh's pick (d)); a light OS under System draws the
/// Light fills (Josh's pick (e)).
#[test]
fn each_appearance_offers_its_themes() {
    use ThemeName::*;
    let standalone = vec![
        Restored,
        Moss,
        MidnightMoss,
        Staircase,
        InverseBar,
        GobbyBar,
        ContrastChrome,
        MossChrome,
        MossBand,
    ];
    let light: Vec<ThemeName> = standalone
        .iter()
        .copied()
        .filter(|name| LIGHT_READING == LightReading::Literal || *name != ContrastChrome)
        .collect();
    let system = vec![
        Restored,
        Moss,
        MidnightMoss,
        InverseBar,
        GobbyBar,
        ContrastChrome,
        MossChrome,
        Ink,
        HostMatched,
    ];
    let system_light: Vec<ThemeName> = system
        .iter()
        .copied()
        .filter(|name| light.contains(name))
        .collect();
    let offered = |kind, hosted| -> Vec<ThemeName> {
        ThemeName::ALL
            .into_iter()
            .filter(|name| name.offered(kind, hosted))
            .collect()
    };
    assert_eq!(offered(ThemeKind::Dark, false), standalone);
    assert_eq!(offered(ThemeKind::Light, false), light);
    assert_eq!(offered(ThemeKind::Dark, true), system);
    assert_eq!(offered(ThemeKind::Light, true), system_light);

    for appearance in APPEARANCES {
        for name in ThemeName::ALL {
            let drawn = appearance.theme(name);
            let expected = if name.offered(appearance.kind(), appearance.hosted()) {
                name
            } else {
                Restored
            };
            assert_eq!(drawn.name, expected, "{name:?} in {appearance:?}");
            assert_eq!(
                drawn.hosted,
                appearance.hosted(),
                "{name:?} in {appearance:?}"
            );
        }
    }
}

/// Criterion 12 and Josh's pick (f): Host-matched takes the host terminal's
/// hue at chroma min(0.03, the host's), is redrawn when the host reports a
/// new background, and stays neutral until it reports one.
#[test]
fn host_matched_takes_the_host_hue_at_capped_chroma() {
    let mut chrome = Chrome::dark();
    chrome.prefs.theme = "system".to_string();
    chrome.prefs.palette = ThemeName::HostMatched;
    chrome.set_theme(ThemeKind::Dark);
    assert_eq!(chrome.theme.name, ThemeName::HostMatched);
    assert_eq!(chrome.theme.band.chroma, 0.0, "no host reply yet");
    assert_eq!(chrome.theme.band.hue, BRAND_HUE);
    let neutral = hex(chrome.palette.band);

    let report = |chrome: &mut Chrome, kind, (r, g, b): (u8, u8, u8)| {
        chrome.record_host_color(kind, RgbColor { r, g, b });
    };
    // The foreground is not the ground, so it redraws nothing.
    report(
        &mut chrome,
        DefaultColorKind::Foreground,
        (0xcd, 0xd6, 0xf4),
    );
    assert_eq!(hex(chrome.palette.band), neutral);

    // Fills by the board's conversion for Mocha (the board's own host),
    // Solarized dark (chroma past the cap), Nord (under it) and a grey.
    for (host, band, selection, hue, chroma) in [
        (HOST, "#38394a", "#5e5e71", Some(284), 0.03),
        ((0x00, 0x2b, 0x36), "#293e45", "#4e656c", Some(220), 0.03),
        ((0x2e, 0x34, 0x40), "#343b47", "#5a616e", Some(264), 0.0229),
        ((0x28, 0x28, 0x28), "#3a3a3a", "#606060", None, 0.0),
    ] {
        report(&mut chrome, DefaultColorKind::Background, host);
        let theme = &chrome.theme;
        assert_eq!(theme.name, ThemeName::HostMatched);
        assert_eq!(
            [hex(chrome.palette.band), hex(chrome.palette.selection)],
            [band, selection],
            "host {host:?}"
        );
        if let Some(hue) = hue {
            assert_eq!(
                (theme.band.hue, theme.selection.hue),
                (hue, hue),
                "host {host:?}"
            );
        }
        assert!(
            (theme.band.chroma - chroma).abs() < 0.0005,
            "host {host:?}: chroma {}",
            theme.band.chroma
        );
        assert_eq!(theme.band.chroma, theme.selection.chroma, "host {host:?}");
    }

    // Any other theme keeps its own fills over any host.
    chrome.prefs.palette = ThemeName::Restored;
    chrome.set_theme(ThemeKind::Dark);
    report(
        &mut chrome,
        DefaultColorKind::Background,
        (0x00, 0x2b, 0x36),
    );
    assert_eq!(hex(chrome.palette.band), "#373835");
}
