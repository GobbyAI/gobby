//! Gobby terminal token map from `.impeccable.md`, applied through
//! herdr's `terminal_theme` mechanism in `gobby_terminal`.
//!
//! One design-contract palette replaces herdr's named themes: dark is the
//! default, light is an equal peer. Brand accent sits at hue 125; neutrals
//! carry a faint hue-125 tint; state colours are info 250 / warning 75 /
//! destructive 350 / success 125 by lightness. State is never carried by
//! hue alone: every indicator pairs a glyph or position cue with its colour.
//! One non-state identifier hue, 315, marks refs and branches as text.

use gobby_terminal::terminal_theme::{DefaultColorKind, RgbColor, TerminalTheme};
use ratatui::style::Color;

mod catalog;

pub use catalog::{Fills, LightReading, ModelHue, Oklch, ThemeName, LIGHT_READING};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThemeKind {
    Dark,
    Light,
}

#[derive(Debug, Clone, Copy)]
pub struct Token {
    pub name: &'static str,
    pub hue: u16,
    pub lightness: f32,
    pub chroma: f32,
    pub icon: &'static str,
    pub position_cue: Option<&'static str>,
}

impl Token {
    const fn state(
        name: &'static str,
        hue: u16,
        lightness: f32,
        chroma: f32,
        icon: &'static str,
        position_cue: Option<&'static str>,
    ) -> Self {
        Self {
            name,
            hue,
            lightness,
            chroma,
            icon,
            position_cue,
        }
    }

    const fn neutral(name: &'static str, lightness: f32) -> Self {
        Self {
            name,
            hue: BRAND_HUE,
            lightness,
            chroma: NEUTRAL_CHROMA,
            icon: "",
            position_cue: None,
        }
    }

    const fn fill(name: &'static str, colour: Oklch) -> Self {
        Self {
            name,
            hue: colour.hue,
            lightness: colour.lightness,
            chroma: colour.chroma,
            icon: "",
            position_cue: None,
        }
    }

    pub fn rgb(self) -> (u8, u8, u8) {
        oklch_to_srgb(self.lightness, self.chroma, self.hue as f32)
    }

    pub fn color(self) -> Color {
        let (r, g, b) = self.rgb();
        Color::Rgb(r, g, b)
    }

    pub fn rgb_color(self) -> RgbColor {
        let (r, g, b) = self.rgb();
        RgbColor { r, g, b }
    }

    pub fn relative_luminance(self) -> f64 {
        relative_luminance(self.rgb())
    }
}

pub const BRAND_HUE: u16 = 125;
pub const INFO_HUE: u16 = 250;
pub const WARNING_HUE: u16 = 75;
pub const DESTRUCTIVE_HUE: u16 = 350;
/// Refs and branches in the sidebar: a text role, never a state, at least
/// 35 degrees from every state hue.
pub const IDENTIFIER_HUE: u16 = 315;
/// Neutrals tint toward the brand hue at chroma 0.005-0.008.
pub const NEUTRAL_CHROMA: f32 = 0.006;

/// Neutral ramp with herdr's surface names. Dark runs low to high
/// lightness from `panel_bg` to `text`; light mirrors it.
#[derive(Debug, Clone, Copy)]
pub struct Neutrals {
    pub panel_bg: Token,
    pub surface_dim: Token,
    pub surface0: Token,
    pub surface1: Token,
    pub dim: Token,
    pub overlay0: Token,
    pub overlay1: Token,
    pub subtext0: Token,
    pub text: Token,
}

impl Neutrals {
    pub fn surfaces(&self) -> [&Token; 4] {
        [
            &self.panel_bg,
            &self.surface_dim,
            &self.surface0,
            &self.surface1,
        ]
    }

    pub fn all(&self) -> [&Token; 9] {
        [
            &self.panel_bg,
            &self.surface_dim,
            &self.surface0,
            &self.surface1,
            &self.dim,
            &self.overlay0,
            &self.overlay1,
            &self.subtext0,
            &self.text,
        ]
    }
}

/// Keyboard focus: brand accent plus a position cue, never a hue shift alone.
#[derive(Debug, Clone, Copy)]
pub struct FocusRing {
    pub token: Token,
    /// Non-colour cue paired with the ring: the focused pane's border is the
    /// only one drawn in the accent and its title carries a leading marker.
    pub position_cue: &'static str,
    pub marker: &'static str,
}

#[derive(Debug, Clone)]
pub struct Theme {
    pub name: ThemeName,
    pub kind: ThemeKind,
    pub accent: Token,
    pub info: Token,
    pub warning: Token,
    pub destructive: Token,
    pub success: Token,
    pub identifier: Token,
    pub neutrals: Neutrals,
    /// Section header rows, the tab row and the menu bar.
    pub band: Token,
    /// The selected and active sidebar rows, the active tab and the open
    /// menu title.
    pub selection: Token,
    /// The model line.
    pub model: Token,
    /// System: drawn over the host terminal's ground, which gclient leaves
    /// in place.
    pub hosted: bool,
}

impl Theme {
    /// The default theme in `kind`.
    pub fn new(kind: ThemeKind) -> Self {
        Self::named(ThemeName::default(), kind)
    }

    /// `name` on its own ground in Dark or Light.
    pub fn named(name: ThemeName, kind: ThemeKind) -> Self {
        Self::drawn(name, kind, None)
    }

    /// `name` in System, over the host terminal, whose background `host`
    /// gives Host-matched its hue.
    pub fn hosted(name: ThemeName, kind: ThemeKind, host: Option<(u8, u8, u8)>) -> Self {
        Self::drawn(name, kind, Some(host))
    }

    fn drawn(name: ThemeName, kind: ThemeKind, host: Option<Option<(u8, u8, u8)>>) -> Self {
        let hosted = host.is_some();
        // A theme not offered here, or a System-only one under a light OS
        // appearance, draws Restored, which every appearance offers.
        let (name, fills) = match name
            .fills(kind, hosted)
            .filter(|_| name.offered(kind, hosted))
        {
            Some(fills) => (name, fills),
            None => (
                ThemeName::Restored,
                ThemeName::Restored
                    .fills(kind, hosted)
                    .expect("Restored has fills in every appearance"),
            ),
        };
        let fills = match name {
            ThemeName::HostMatched => host_matched(fills, host.flatten()),
            _ => fills,
        };
        let mut theme = Self::contract(name, kind, fills);
        match name.ground(kind) {
            Some(ground) if !hosted => theme.neutrals.panel_bg = Token::fill("panel_bg", ground),
            _ => {}
        }
        theme.hosted = hosted;
        theme
    }

    /// The design contract's tokens in `kind`, on Restored's ground, with
    /// `name`'s fills and model colour.
    fn contract(name: ThemeName, kind: ThemeKind, fills: Fills) -> Self {
        let band = Token::fill("band", fills.band);
        let selection = Token::fill("selection", fills.selection);
        let model = Token::fill("model", name.model().colour(kind));
        match kind {
            ThemeKind::Dark => Self {
                name,
                kind,
                accent: Token::state("accent", BRAND_HUE, 0.82, 0.20, "", None),
                info: Token::state("info", INFO_HUE, 0.70, 0.16, "i", Some("leading")),
                warning: Token::state("warning", WARNING_HUE, 0.82, 0.16, "w", Some("leading")),
                destructive: Token::state(
                    "destructive",
                    DESTRUCTIVE_HUE,
                    0.76,
                    0.20,
                    "x",
                    Some("leading"),
                ),
                success: Token::state("success", BRAND_HUE, 0.78, 0.10, "ok", Some("trailing")),
                identifier: Token::state("identifier", IDENTIFIER_HUE, 0.85, 0.10, "", None),
                neutrals: Neutrals {
                    panel_bg: Token::neutral("panel_bg", 0.16),
                    surface_dim: Token::neutral("surface_dim", 0.20),
                    surface0: Token::neutral("surface0", 0.26),
                    surface1: Token::neutral("surface1", 0.32),
                    dim: Token::neutral("dim", 0.36),
                    overlay0: Token::neutral("overlay0", 0.55),
                    overlay1: Token::neutral("overlay1", 0.62),
                    subtext0: Token::neutral("subtext0", 0.76),
                    text: Token::neutral("text", 0.92),
                },
                band,
                selection,
                model,
                hosted: false,
            },
            ThemeKind::Light => Self {
                name,
                kind,
                accent: Token::state("accent", BRAND_HUE, 0.50, 0.18, "", None),
                info: Token::state("info", INFO_HUE, 0.40, 0.14, "i", Some("leading")),
                warning: Token::state("warning", WARNING_HUE, 0.49, 0.14, "w", Some("leading")),
                destructive: Token::state(
                    "destructive",
                    DESTRUCTIVE_HUE,
                    0.35,
                    0.18,
                    "x",
                    Some("leading"),
                ),
                success: Token::state("success", BRAND_HUE, 0.44, 0.10, "ok", Some("trailing")),
                identifier: Token::state("identifier", IDENTIFIER_HUE, 0.50, 0.12, "", None),
                neutrals: Neutrals {
                    panel_bg: Token::neutral("panel_bg", 0.985),
                    surface_dim: Token::neutral("surface_dim", 0.955),
                    surface0: Token::neutral("surface0", 0.925),
                    surface1: Token::neutral("surface1", 0.885),
                    dim: Token::neutral("dim", 0.81),
                    overlay0: Token::neutral("overlay0", 0.62),
                    overlay1: Token::neutral("overlay1", 0.52),
                    subtext0: Token::neutral("subtext0", 0.40),
                    text: Token::neutral("text", 0.20),
                },
                band,
                selection,
                model,
                hosted: false,
            },
        }
    }

    /// The fill unfocused panes take in Dark and Light: the ground at its
    /// own hue and chroma, stepped toward mid-grey (lighter in Dark, darker
    /// in Light) until, as `paint` draws them, it stands
    /// `UNFOCUSED_CONTRAST` off the ground, so the step survives monochrome.
    /// Focus reads by lightness alone and pane text keeps its full colour.
    /// System draws none: focus there is the border and title, as Josh
    /// picked.
    pub fn unfocused(&self, paint: impl Fn(Token) -> Color) -> Option<Token> {
        if self.hosted {
            return None;
        }
        let ground = self.neutrals.panel_bg;
        let step = match self.kind {
            ThemeKind::Dark => 1,
            ThemeKind::Light => -1,
        };
        let mut fill = ground;
        for k in 1..=400 {
            let thousandths = (ground.lightness * 1000.0).round() as i32 + step * k;
            fill = Token {
                name: "unfocused",
                lightness: thousandths as f32 / 1000.0,
                ..ground
            };
            if painted_contrast(paint(fill), paint(ground)) >= UNFOCUSED_CONTRAST {
                break;
            }
        }
        Some(fill)
    }

    pub fn states(&self) -> [&Token; 4] {
        [&self.info, &self.warning, &self.destructive, &self.success]
    }

    pub fn monochrome_ranks(&self) -> Vec<(&'static str, f32)> {
        self.states()
            .into_iter()
            .map(|t| (t.name, t.lightness))
            .collect()
    }

    pub fn ansi256_ranks(&self) -> Vec<(&'static str, f32)> {
        let mut ranks = self.monochrome_ranks();
        ranks.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        ranks
    }

    pub fn focus_ring(&self) -> FocusRing {
        FocusRing {
            token: self.accent,
            position_cue: "focused pane border and title marker",
            marker: "▸",
        }
    }

    pub fn palette(&self) -> Palette {
        Palette::from_theme(self)
    }

    /// Foreground, background, and the 16 ANSI slots, applied through
    /// `gobby_terminal::terminal_theme` so hosted terminals inherit the map.
    /// Bright black (dim text) starts at overlay1 and Light's bright green
    /// is the success green, so every slot keeps its floor on the unfocused
    /// fill.
    pub fn terminal_theme(&self) -> TerminalTheme {
        let n = &self.neutrals;
        let bright_green = match self.kind {
            ThemeKind::Dark => self.accent,
            ThemeKind::Light => self.success,
        };
        // Bright black reads at AA on the unfocused fill as on the ground:
        // overlay1, stepped away from the ground until it does there.
        let bright_black = self.unfocused(Token::color).map_or(n.overlay1, |fill| {
            let step = match self.kind {
                ThemeKind::Dark => 1,
                ThemeKind::Light => -1,
            };
            let from = (n.overlay1.lightness * 1000.0).round() as i32;
            (0..=400)
                .map(|k| Token {
                    lightness: (from + step * k) as f32 / 1000.0,
                    ..n.overlay1
                })
                .find(|token| contrast_ratio(token.rgb(), fill.rgb()) >= TEXT_CONTRAST)
                .unwrap_or(n.overlay1)
        });
        let slots: [Token; 16] = [
            n.surface_dim,
            self.destructive,
            self.success,
            self.warning,
            self.info,
            self.destructive,
            self.info,
            n.subtext0,
            bright_black,
            self.destructive,
            bright_green,
            self.warning,
            self.info,
            self.destructive,
            self.info,
            n.text,
        ];
        let mut theme = TerminalTheme::default()
            .with_color(DefaultColorKind::Foreground, n.text.rgb_color())
            .with_color(DefaultColorKind::Background, n.panel_bg.rgb_color());
        for (index, token) in slots.iter().enumerate() {
            theme = theme.with_palette_color(index as u8, token.rgb_color());
        }
        theme
    }
}

/// herdr's palette names, resolved to design-contract tokens.
#[derive(Debug, Clone, Copy)]
pub struct Palette {
    pub accent: Color,
    pub panel_bg: Color,
    pub surface0: Color,
    pub surface1: Color,
    pub surface_dim: Color,
    pub overlay0: Color,
    pub overlay1: Color,
    pub text: Color,
    pub subtext0: Color,
    pub mauve: Color,
    pub green: Color,
    pub yellow: Color,
    pub red: Color,
    pub blue: Color,
    pub teal: Color,
    pub peach: Color,
    pub ink: Color,
    pub glint: Color,
    pub dim: Color,
    /// Refs and branches: the one non-state hue.
    pub identifier: Color,
    /// Section header rows, the tab row and the menu bar.
    pub band: Color,
    /// The selected and active sidebar rows, the active tab and the open
    /// menu title.
    pub selection: Color,
    /// The model line.
    pub model: Color,
    /// What the ground's ink becomes on `band`.
    pub band_ink: FillInk,
    /// What the ground's ink becomes on `selection`.
    pub selection_ink: FillInk,
    /// Unfocused panes' ground; none in System (`Theme::unfocused`).
    pub unfocused: Option<Color>,
    /// The line under the menu bar and the rule between tabs: a tinted
    /// near-black under the dark theme's ground, in every theme.
    pub line: Color,
    /// The wordmark and the marks' braille: accent in dark, text in light.
    pub wordmark: Color,
}

/// WCAG 2.2 AA for text, and the floor for glyphs and other non-text marks.
pub const TEXT_CONTRAST: f64 = 4.5;
pub const GLYPH_CONTRAST: f64 = 3.0;
/// How far an unfocused pane's ground stands off the focused one.
pub const UNFOCUSED_CONTRAST: f64 = 1.15;

/// The colours one side (dark or light) draws text and glyphs in.
#[derive(Debug, Clone, Copy)]
struct Ink {
    text: Color,
    subtext0: Color,
    model: Color,
    identifier: Color,
    accent: Color,
    warning: Color,
    success: Color,
    destructive: Color,
    info: Color,
    overlay1: Color,
    overlay0: Color,
    dim: Color,
}

impl Ink {
    /// `kind`'s ink, with `name`'s model colour; states and neutrals are
    /// the design contract's in every theme.
    fn of(name: ThemeName, kind: ThemeKind, paint: &impl Fn(Token) -> Color) -> Self {
        let side = Theme::new(kind);
        let n = &side.neutrals;
        Self {
            text: paint(n.text),
            subtext0: paint(n.subtext0),
            model: paint(Token::fill("model", name.model().colour(kind))),
            identifier: paint(side.identifier),
            accent: paint(side.accent),
            warning: paint(side.warning),
            success: paint(side.success),
            destructive: paint(side.destructive),
            info: paint(side.info),
            overlay1: paint(n.overlay1),
            overlay0: paint(n.overlay0),
            dim: paint(n.dim),
        }
    }
}

/// The ink that reads on one fill. Whichever side's text reads better on
/// the fill draws there, so a light bar in a dark theme takes the light
/// theme's ink; a colour under its floor on the fill falls back along the
/// theme-mapping board's chain, words to AA and glyphs to 3:1.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FillInk {
    pub fill: Color,
    /// The default foreground on the fill.
    pub text: Color,
    /// Each ground colour paired with the one that takes its place; words
    /// first, so a colour two roles share takes the stricter floor.
    swaps: [(Color, Color); 12],
}

impl FillInk {
    fn on(fill: Color, own: &Ink, dark: &Ink, light: &Ink) -> Self {
        let ink = if painted_contrast(dark.text, fill) >= painted_contrast(light.text, fill) {
            dark
        } else {
            light
        };
        let pick = |floor: f64, chain: &[Color]| {
            chain
                .iter()
                .copied()
                .find(|&colour| painted_contrast(colour, fill) >= floor)
                .unwrap_or(chain[chain.len() - 1])
        };
        Self {
            fill,
            text: ink.text,
            swaps: [
                (own.text, ink.text),
                (own.subtext0, pick(TEXT_CONTRAST, &[ink.subtext0, ink.text])),
                (
                    own.model,
                    pick(TEXT_CONTRAST, &[ink.model, ink.subtext0, ink.text]),
                ),
                (
                    own.identifier,
                    pick(TEXT_CONTRAST, &[ink.identifier, ink.text]),
                ),
                // Accent marks the cursor and names a selected project.
                (own.accent, pick(TEXT_CONTRAST, &[ink.accent, ink.text])),
                (own.warning, pick(GLYPH_CONTRAST, &[ink.warning, ink.text])),
                (own.success, pick(GLYPH_CONTRAST, &[ink.success, ink.text])),
                (
                    own.destructive,
                    pick(GLYPH_CONTRAST, &[ink.destructive, ink.text]),
                ),
                (own.info, pick(GLYPH_CONTRAST, &[ink.info, ink.text])),
                (
                    own.overlay1,
                    pick(GLYPH_CONTRAST, &[ink.overlay1, ink.subtext0, ink.text]),
                ),
                // Rules and idle glyphs, which need no floor.
                (own.overlay0, ink.overlay0),
                (own.dim, ink.dim),
            ],
        }
    }

    /// The colour a cell drawn in `fg` takes on the fill.
    pub fn ink(&self, fg: Color) -> Color {
        if fg == Color::Reset {
            return self.text;
        }
        self.swaps
            .iter()
            .find(|(from, _)| *from == fg)
            .map_or(fg, |&(_, to)| to)
    }
}

/// Host-matched's fills at the host terminal's hue, at no more chroma than
/// the host's own; neutral until the host answers.
fn host_matched(fills: Fills, host: Option<(u8, u8, u8)>) -> Fills {
    let (chroma, hue) = host.map_or((0.0, BRAND_HUE), |rgb| {
        let (_, chroma, hue) = srgb_to_oklch(rgb);
        (
            (chroma as f32).min(fills.band.chroma),
            hue.round() as u16 % 360,
        )
    });
    let at = |colour: Oklch| Oklch {
        chroma,
        hue,
        ..colour
    };
    Fills {
        band: at(fills.band),
        selection: at(fills.selection),
    }
}

/// The menu bar's underline and tab rule, and the light theme's mark ink.
const LINE: Token = Token::neutral("line", 0.08);

impl Palette {
    /// Every herdr palette name paired with the token it resolves to.
    /// `mauve` is a neutral (the violet is `identifier`); `red` is the
    /// magenta-pink destructive token; `teal` and `blue` are both info.
    pub fn entries(theme: &Theme) -> [(&'static str, Token); 23] {
        let n = &theme.neutrals;
        let (ink, glint) = match theme.kind {
            ThemeKind::Dark => (n.panel_bg, n.text),
            ThemeKind::Light => (LINE, n.panel_bg),
        };
        [
            ("accent", theme.accent),
            ("panel_bg", n.panel_bg),
            ("surface0", n.surface0),
            ("surface1", n.surface1),
            ("surface_dim", n.surface_dim),
            ("overlay0", n.overlay0),
            ("overlay1", n.overlay1),
            ("text", n.text),
            ("subtext0", n.subtext0),
            ("mauve", n.subtext0),
            ("green", theme.success),
            ("yellow", theme.warning),
            ("red", theme.destructive),
            ("blue", theme.info),
            ("teal", theme.info),
            ("peach", theme.warning),
            ("ink", ink),
            ("glint", glint),
            ("dim", n.dim),
            ("identifier", theme.identifier),
            ("band", theme.band),
            ("selection", theme.selection),
            ("model", theme.model),
        ]
    }

    pub fn from_theme(theme: &Theme) -> Self {
        Self::painted(theme, Token::color)
    }

    /// `from_theme` with every token drawn at its own lightness and no
    /// chroma. States already differ by glyph and lightness, so nothing
    /// relies on the hue this drops.
    pub fn monochrome(theme: &Theme) -> Self {
        Self::painted(theme, |token| {
            Token {
                chroma: 0.0,
                ..token
            }
            .color()
        })
    }

    fn painted(theme: &Theme, paint: impl Fn(Token) -> Color) -> Self {
        let n = &theme.neutrals;
        let (ink, glint) = match theme.kind {
            ThemeKind::Dark => (n.panel_bg, n.text),
            ThemeKind::Light => (LINE, n.panel_bg),
        };
        let dark_ink = Ink::of(theme.name, ThemeKind::Dark, &paint);
        let light_ink = Ink::of(theme.name, ThemeKind::Light, &paint);
        let own = match theme.kind {
            ThemeKind::Dark => &dark_ink,
            ThemeKind::Light => &light_ink,
        };
        let band = paint(theme.band);
        let selection = paint(theme.selection);
        let wordmark = match theme.kind {
            ThemeKind::Dark => theme.accent,
            ThemeKind::Light => n.text,
        };
        Self {
            accent: paint(theme.accent),
            panel_bg: paint(n.panel_bg),
            surface0: paint(n.surface0),
            surface1: paint(n.surface1),
            surface_dim: paint(n.surface_dim),
            overlay0: paint(n.overlay0),
            overlay1: paint(n.overlay1),
            text: paint(n.text),
            subtext0: paint(n.subtext0),
            mauve: paint(n.subtext0),
            green: paint(theme.success),
            yellow: paint(theme.warning),
            red: paint(theme.destructive),
            blue: paint(theme.info),
            teal: paint(theme.info),
            peach: paint(theme.warning),
            ink: paint(ink),
            glint: paint(glint),
            dim: paint(n.dim),
            identifier: paint(theme.identifier),
            band,
            selection,
            model: paint(theme.model),
            band_ink: FillInk::on(band, own, &dark_ink, &light_ink),
            selection_ink: FillInk::on(selection, own, &dark_ink, &light_ink),
            unfocused: theme.unfocused(&paint).map(&paint),
            line: paint(LINE),
            wordmark: paint(wordmark),
        }
    }
}

fn linear(channel: u8) -> f64 {
    let c = f64::from(channel) / 255.0;
    if c <= 0.040_45 {
        c / 12.92
    } else {
        ((c + 0.055) / 1.055).powf(2.4)
    }
}

/// WCAG 2.2 relative luminance of an sRGB triple.
pub fn relative_luminance((r, g, b): (u8, u8, u8)) -> f64 {
    0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

/// OKLCH lightness, chroma and hue in degrees of an sRGB triple.
fn srgb_to_oklch((r, g, b): (u8, u8, u8)) -> (f64, f64, f64) {
    let (r, g, b) = (linear(r), linear(g), linear(b));
    let l = (0.412_221_470_8 * r + 0.536_332_536_3 * g + 0.051_445_992_9 * b).cbrt();
    let m = (0.211_903_498_2 * r + 0.680_699_545_1 * g + 0.107_396_956_6 * b).cbrt();
    let s = (0.088_302_461_9 * r + 0.281_718_837_6 * g + 0.629_978_700_5 * b).cbrt();
    let lightness = 0.210_454_255_3 * l + 0.793_617_785_0 * m - 0.004_072_046_8 * s;
    let a = 1.977_998_495_1 * l - 2.428_592_205_0 * m + 0.450_593_709_9 * s;
    let b = 0.025_904_037_1 * l + 0.782_771_766_2 * m - 0.808_675_766_0 * s;
    (
        lightness,
        a.hypot(b),
        b.atan2(a).to_degrees().rem_euclid(360.0),
    )
}

/// WCAG 2.2 contrast ratio between two sRGB triples (>= 1.0).
pub fn contrast_ratio(a: (u8, u8, u8), b: (u8, u8, u8)) -> f64 {
    let la = relative_luminance(a);
    let lb = relative_luminance(b);
    let (hi, lo) = if la >= lb { (la, lb) } else { (lb, la) };
    (hi + 0.05) / (lo + 0.05)
}

/// `contrast_ratio` of two painted colours; 1.0 when either is not RGB.
fn painted_contrast(a: Color, b: Color) -> f64 {
    match (a, b) {
        (Color::Rgb(ar, ag, ab), Color::Rgb(br, bg, bb)) => {
            contrast_ratio((ar, ag, ab), (br, bg, bb))
        }
        _ => 1.0,
    }
}

fn srgb_encode(channel: f64) -> u8 {
    let clipped = channel.clamp(0.0, 1.0);
    let encoded = if clipped <= 0.003_130_8 {
        12.92 * clipped
    } else {
        1.055 * clipped.powf(1.0 / 2.4) - 0.055
    };
    (encoded * 255.0).round().clamp(0.0, 255.0) as u8
}

fn oklch_to_srgb(l: f32, c: f32, h_deg: f32) -> (u8, u8, u8) {
    let l = f64::from(l);
    let c = f64::from(c);
    let h = f64::from(h_deg).to_radians();
    let a = c * h.cos();
    let b = c * h.sin();
    let l_ = l + 0.396_337_777_4 * a + 0.215_803_757_3 * b;
    let m_ = l - 0.105_561_345_8 * a - 0.063_854_172_8 * b;
    let s_ = l - 0.089_484_177_5 * a - 1.291_485_548_0 * b;
    let l3 = l_ * l_ * l_;
    let m3 = m_ * m_ * m_;
    let s3 = s_ * s_ * s_;
    let r = 4.076_741_662_1 * l3 - 3.307_711_591_3 * m3 + 0.230_969_929_2 * s3;
    let g = -1.268_438_004_6 * l3 + 2.609_757_401_1 * m3 - 0.341_319_396_5 * s3;
    let b = -0.004_196_086_3 * l3 - 0.703_418_614_7 * m3 + 1.707_614_701_0 * s3;
    (srgb_encode(r), srgb_encode(g), srgb_encode(b))
}
