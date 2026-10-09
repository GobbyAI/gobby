//! The named themes, picked at runtime under View › Theme and in Settings,
//! as Josh picked them from the theme-mapping board (#23416, 2026-10-05).
//! The appearance (dark, light or system) is a preference of its own; each
//! theme has cells for the appearances it is offered in, and a theme drawn
//! where it is not offered draws Restored instead, its pick still saved.
//!
//! A theme sets two fills and a model colour. The header fill takes the
//! sidebar's section header rows and the tab row, and the menu bar steps
//! off it (`Theme::bar`); the selection fill takes the selected and active
//! sidebar rows, the active tab and the open menu title. Dark and Light also
//! set the theme's own ground; System draws over the host terminal's.

use serde::{Deserialize, Serialize};

use super::{ThemeKind, BRAND_HUE, NEUTRAL_CHROMA};

/// A shipped theme, saved as `[ui] palette` in prefs.toml.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ThemeName {
    /// The pre-regression header band with one selection fill above it.
    /// `classic`, the palette before named themes, loads as Restored.
    #[default]
    #[serde(alias = "classic")]
    Restored,
    Moss,
    MidnightMoss,
    Staircase,
    InverseBar,
    GobbyBar,
    ContrastChrome,
    MossChrome,
    MossBand,
    /// System only: header rows sink below the host's ground.
    Ink,
    /// System only: the fills take the host terminal's own hue.
    HostMatched,
}

/// Which Light column ships. Josh kept the literal reading of his rule, the
/// selection lighter than the header rows (2026-10-05); the other reading,
/// the selection further from the ground, stays in the table so the pick
/// can flip here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LightReading {
    Literal,
    Other,
}

pub const LIGHT_READING: LightReading = LightReading::Literal;

/// A colour by OKLCH lightness, chroma and hue.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Oklch {
    pub lightness: f32,
    pub chroma: f32,
    pub hue: u16,
}

const fn n(lightness: f32) -> Oklch {
    o(lightness, NEUTRAL_CHROMA, BRAND_HUE)
}

const fn o(lightness: f32, chroma: f32, hue: u16) -> Oklch {
    Oklch {
        lightness,
        chroma,
        hue,
    }
}

/// One theme's fills in one appearance.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Fills {
    /// Section header rows and the tab row; the menu bar steps off it.
    pub band: Oklch,
    /// The selected and active sidebar rows, the active tab and the open
    /// menu title.
    pub selection: Oklch,
}

const fn fills(band: Oklch, selection: Oklch) -> Option<Fills> {
    Some(Fills { band, selection })
}

/// The model line's colour family, from the model-line board: slate teal,
/// or clay on the green-tinted themes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModelHue {
    Teal,
    Clay,
}

impl ModelHue {
    pub fn colour(self, kind: ThemeKind) -> Oklch {
        match (self, kind) {
            (ModelHue::Teal, ThemeKind::Dark) => o(0.78, 0.06, 200),
            (ModelHue::Teal, ThemeKind::Light) => o(0.42, 0.07, 200),
            (ModelHue::Clay, ThemeKind::Dark) => o(0.78, 0.07, 30),
            (ModelHue::Clay, ThemeKind::Light) => o(0.44, 0.09, 30),
        }
    }
}

/// The brand chartreuse, Dark's accent, as Gobby bar's selection.
const CHARTREUSE: Oklch = o(0.82, 0.20, 125);

/// One row of the board: a theme's grounds and fills per appearance.
struct Entry {
    model: ModelHue,
    dark_ground: Option<Oklch>,
    light_ground: Option<Oklch>,
    dark: Option<Fills>,
    system: Option<Fills>,
    /// Indexed by `LightReading`: the literal reading, then the other.
    light: [Option<Fills>; 2],
}

impl ThemeName {
    /// Every shipped theme, in the order the pickers list them.
    pub const ALL: [ThemeName; 11] = [
        ThemeName::Restored,
        ThemeName::Moss,
        ThemeName::MidnightMoss,
        ThemeName::Staircase,
        ThemeName::InverseBar,
        ThemeName::GobbyBar,
        ThemeName::ContrastChrome,
        ThemeName::MossChrome,
        ThemeName::MossBand,
        ThemeName::Ink,
        ThemeName::HostMatched,
    ];

    pub fn label(self) -> &'static str {
        match self {
            ThemeName::Restored => "Restored",
            ThemeName::Moss => "Moss",
            ThemeName::MidnightMoss => "Midnight moss",
            ThemeName::Staircase => "Staircase",
            ThemeName::InverseBar => "Inverse bar",
            ThemeName::GobbyBar => "Gobby bar",
            ThemeName::ContrastChrome => "Contrast chrome",
            ThemeName::MossChrome => "Moss chrome",
            ThemeName::MossBand => "Moss band",
            ThemeName::Ink => "Ink",
            ThemeName::HostMatched => "Host-matched",
        }
    }

    /// Whether the pickers list this theme in `kind`, over the host terminal
    /// when `hosted` (System). System lists only the themes with a System
    /// cell, and under a light OS appearance only those that also have
    /// Light fills.
    pub fn offered(self, kind: ThemeKind, hosted: bool) -> bool {
        (!hosted || self.entry().system.is_some()) && self.fills(kind, hosted).is_some()
    }

    /// The fills drawn in `kind`, over the host terminal when `hosted`.
    /// System under a light OS appearance draws the Light column's fills.
    pub fn fills(self, kind: ThemeKind, hosted: bool) -> Option<Fills> {
        let entry = self.entry();
        match kind {
            ThemeKind::Dark if hosted => entry.system,
            ThemeKind::Dark => entry.dark,
            ThemeKind::Light => entry.light[LIGHT_READING as usize],
        }
    }

    /// The theme's own ground in Dark or Light.
    pub fn ground(self, kind: ThemeKind) -> Option<Oklch> {
        match kind {
            ThemeKind::Dark => self.entry().dark_ground,
            ThemeKind::Light => self.entry().light_ground,
        }
    }

    pub fn model(self) -> ModelHue {
        self.entry().model
    }

    fn entry(self) -> Entry {
        use ModelHue::{Clay, Teal};
        match self {
            ThemeName::Restored => Entry {
                model: Teal,
                dark_ground: Some(n(0.16)),
                light_ground: Some(n(0.985)),
                dark: fills(n(0.26), n(0.42)),
                system: fills(n(0.34), n(0.49)),
                light: [fills(n(0.70), n(0.87)), fills(n(0.925), n(0.76))],
            },
            ThemeName::Moss => Entry {
                model: Clay,
                dark_ground: Some(o(0.18, 0.045, 255)),
                light_ground: Some(o(0.985, 0.025, 90)),
                dark: fills(n(0.26), o(0.42, 0.07, 125)),
                system: fills(n(0.34), o(0.49, 0.07, 125)),
                light: [
                    fills(n(0.70), o(0.86, 0.06, 125)),
                    fills(n(0.925), o(0.75, 0.09, 125)),
                ],
            },
            ThemeName::MidnightMoss => Entry {
                model: Clay,
                dark_ground: Some(o(0.12, 0.045, 255)),
                light_ground: Some(o(0.94, 0.045, 90)),
                // The accent at 18% over the ground, solid: #1e2b17.
                dark: fills(o(0.271, 0.04, 135), n(0.47)),
                system: fills(o(0.34, 0.06, 125), n(0.50)),
                light: [fills(n(0.68), n(0.83)), fills(n(0.88), n(0.70))],
            },
            ThemeName::Staircase => Entry {
                model: Teal,
                dark_ground: Some(o(0.12, 0.035, 70)),
                light_ground: Some(o(0.94, 0.035, 245)),
                dark: fills(n(0.30), n(0.48)),
                // Dropped from System: it duplicated Restored there.
                system: None,
                light: [fills(n(0.45), n(0.83)), fills(n(0.86), n(0.64))],
            },
            ThemeName::InverseBar => Entry {
                model: Teal,
                dark_ground: Some(o(0.20, 0.02, 70)),
                light_ground: Some(o(0.975, 0.02, 225)),
                dark: fills(n(0.26), n(0.86)),
                system: fills(n(0.33), n(0.86)),
                light: [fills(n(0.25), n(0.45)), fills(n(0.925), n(0.30))],
            },
            ThemeName::GobbyBar => Entry {
                model: Clay,
                dark_ground: Some(o(0.14, 0.035, 220)),
                light_ground: Some(o(0.985, 0.045, 90)),
                dark: fills(n(0.26), CHARTREUSE),
                system: fills(n(0.33), CHARTREUSE),
                light: [
                    fills(n(0.68), CHARTREUSE),
                    fills(n(0.925), o(0.45, 0.18, 125)),
                ],
            },
            ThemeName::ContrastChrome => Entry {
                model: Teal,
                dark_ground: Some(o(0.24, 0.035, 70)),
                light_ground: Some(o(0.96, 0.035, 245)),
                dark: fills(n(0.72), n(0.90)),
                system: fills(n(0.72), n(0.90)),
                // No other-reading cell: the dark header rows leave no room
                // for a selection darker still.
                light: [fills(n(0.30), n(0.84)), None],
            },
            ThemeName::MossChrome => Entry {
                model: Clay,
                dark_ground: Some(o(0.20, 0.035, 220)),
                light_ground: Some(o(0.96, 0.025, 90)),
                dark: fills(o(0.31, 0.06, 125), o(0.87, 0.07, 125)),
                system: fills(o(0.32, 0.065, 125), o(0.87, 0.07, 125)),
                light: [
                    fills(o(0.32, 0.05, 125), o(0.84, 0.05, 125)),
                    fills(o(0.90, 0.045, 125), o(0.32, 0.05, 125)),
                ],
            },
            ThemeName::MossBand => Entry {
                model: Clay,
                dark_ground: Some(o(0.24, 0.045, 255)),
                light_ground: Some(n(0.96)),
                dark: fills(o(0.29, 0.045, 125), n(0.46)),
                // Dropped from System: it duplicated Midnight moss there.
                system: None,
                light: [
                    fills(o(0.68, 0.045, 125), n(0.85)),
                    fills(o(0.90, 0.045, 125), n(0.72)),
                ],
            },
            ThemeName::Ink => Entry {
                model: Teal,
                dark_ground: None,
                light_ground: None,
                dark: None,
                system: fills(n(0.12), n(0.40)),
                light: [None, None],
            },
            ThemeName::HostMatched => Entry {
                model: Teal,
                dark_ground: None,
                light_ground: None,
                dark: None,
                // Mocha's hue and chroma as drawn on the board; `Theme`
                // swaps in the host terminal's own.
                system: fills(o(0.35, 0.03, 284), o(0.49, 0.03, 284)),
                light: [None, None],
            },
        }
    }
}
