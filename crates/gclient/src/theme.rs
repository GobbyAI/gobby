//! Gobby terminal token map from `.impeccable.md`, applied through
//! herdr's `terminal_theme` mechanism in `gobby_terminal`.
//!
//! One design-contract palette replaces herdr's named themes: dark is the
//! default, light is an equal peer. Brand accent sits at hue 125; neutrals
//! carry a faint hue-125 tint; state colours are info 250 / warning 75 /
//! destructive 350 / success 125 by lightness. State is never carried by
//! hue alone: every indicator pairs a glyph or position cue with its colour.

use gobby_terminal::terminal_theme::{DefaultColorKind, RgbColor, TerminalTheme};
use ratatui::style::Color;

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

    pub fn all(&self) -> [&Token; 8] {
        [
            &self.panel_bg,
            &self.surface_dim,
            &self.surface0,
            &self.surface1,
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
    pub kind: ThemeKind,
    pub accent: Token,
    pub info: Token,
    pub warning: Token,
    pub destructive: Token,
    pub success: Token,
    pub neutrals: Neutrals,
}

impl Theme {
    pub fn new(kind: ThemeKind) -> Self {
        match kind {
            ThemeKind::Dark => Self {
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
                neutrals: Neutrals {
                    panel_bg: Token::neutral("panel_bg", 0.16),
                    surface_dim: Token::neutral("surface_dim", 0.20),
                    surface0: Token::neutral("surface0", 0.26),
                    surface1: Token::neutral("surface1", 0.32),
                    overlay0: Token::neutral("overlay0", 0.55),
                    overlay1: Token::neutral("overlay1", 0.62),
                    subtext0: Token::neutral("subtext0", 0.76),
                    text: Token::neutral("text", 0.92),
                },
            },
            ThemeKind::Light => Self {
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
                neutrals: Neutrals {
                    panel_bg: Token::neutral("panel_bg", 0.985),
                    surface_dim: Token::neutral("surface_dim", 0.955),
                    surface0: Token::neutral("surface0", 0.925),
                    surface1: Token::neutral("surface1", 0.885),
                    overlay0: Token::neutral("overlay0", 0.62),
                    overlay1: Token::neutral("overlay1", 0.52),
                    subtext0: Token::neutral("subtext0", 0.40),
                    text: Token::neutral("text", 0.20),
                },
            },
        }
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
    pub fn terminal_theme(&self) -> TerminalTheme {
        let n = &self.neutrals;
        let slots: [Token; 16] = [
            n.surface_dim,
            self.destructive,
            self.success,
            self.warning,
            self.info,
            self.destructive,
            self.info,
            n.subtext0,
            n.overlay0,
            self.destructive,
            self.accent,
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
}

impl Palette {
    /// Every herdr palette name paired with the token it resolves to.
    /// `mauve` is a neutral (no purple in the contract); `red` is the
    /// magenta-pink destructive token; `teal` and `blue` are both info.
    pub fn entries(theme: &Theme) -> [(&'static str, Token); 16] {
        let n = &theme.neutrals;
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
        ]
    }

    pub fn from_theme(theme: &Theme) -> Self {
        let n = &theme.neutrals;
        Self {
            accent: theme.accent.color(),
            panel_bg: n.panel_bg.color(),
            surface0: n.surface0.color(),
            surface1: n.surface1.color(),
            surface_dim: n.surface_dim.color(),
            overlay0: n.overlay0.color(),
            overlay1: n.overlay1.color(),
            text: n.text.color(),
            subtext0: n.subtext0.color(),
            mauve: n.subtext0.color(),
            green: theme.success.color(),
            yellow: theme.warning.color(),
            red: theme.destructive.color(),
            blue: theme.info.color(),
            teal: theme.info.color(),
            peach: theme.warning.color(),
        }
    }
}

/// WCAG 2.2 relative luminance of an sRGB triple.
pub fn relative_luminance((r, g, b): (u8, u8, u8)) -> f64 {
    fn linear(channel: u8) -> f64 {
        let c = f64::from(channel) / 255.0;
        if c <= 0.040_45 {
            c / 12.92
        } else {
            ((c + 0.055) / 1.055).powf(2.4)
        }
    }
    0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

/// WCAG 2.2 contrast ratio between two sRGB triples (>= 1.0).
pub fn contrast_ratio(a: (u8, u8, u8), b: (u8, u8, u8)) -> f64 {
    let la = relative_luminance(a);
    let lb = relative_luminance(b);
    let (hi, lo) = if la >= lb { (la, lb) } else { (lb, la) };
    (hi + 0.05) / (lo + 0.05)
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
