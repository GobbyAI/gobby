//! Gobby terminal token map from `.impeccable.md`.

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
    pub fn rgb(self) -> (u8, u8, u8) {
        oklch_to_srgb(self.lightness, self.chroma, self.hue as f32)
    }

    pub fn color(self) -> Color {
        let (r, g, b) = self.rgb();
        Color::Rgb(r, g, b)
    }
}

#[derive(Debug, Clone)]
pub struct Theme {
    pub kind: ThemeKind,
    pub accent: Token,
    pub info: Token,
    pub warning: Token,
    pub destructive: Token,
    pub success: Token,
}

impl Theme {
    pub fn new(kind: ThemeKind) -> Self {
        match kind {
            ThemeKind::Dark => Self {
                kind,
                accent: Token {
                    name: "accent",
                    hue: 125,
                    lightness: 0.82,
                    chroma: 0.20,
                    icon: "",
                    position_cue: None,
                },
                info: Token {
                    name: "info",
                    hue: 250,
                    lightness: 0.70,
                    chroma: 0.16,
                    icon: "i",
                    position_cue: Some("leading"),
                },
                warning: Token {
                    name: "warning",
                    hue: 75,
                    lightness: 0.78,
                    chroma: 0.16,
                    icon: "w",
                    position_cue: Some("leading"),
                },
                destructive: Token {
                    name: "destructive",
                    hue: 350,
                    lightness: 0.65,
                    chroma: 0.20,
                    icon: "x",
                    position_cue: Some("leading"),
                },
                success: Token {
                    name: "success",
                    hue: 125,
                    lightness: 0.72,
                    chroma: 0.10,
                    icon: "ok",
                    position_cue: Some("trailing"),
                },
            },
            ThemeKind::Light => Self {
                kind,
                accent: Token {
                    name: "accent",
                    hue: 125,
                    lightness: 0.50,
                    chroma: 0.18,
                    icon: "",
                    position_cue: None,
                },
                info: Token {
                    name: "info",
                    hue: 250,
                    lightness: 0.45,
                    chroma: 0.14,
                    icon: "i",
                    position_cue: Some("leading"),
                },
                warning: Token {
                    name: "warning",
                    hue: 75,
                    lightness: 0.52,
                    chroma: 0.14,
                    icon: "w",
                    position_cue: Some("leading"),
                },
                destructive: Token {
                    name: "destructive",
                    hue: 350,
                    lightness: 0.40,
                    chroma: 0.18,
                    icon: "x",
                    position_cue: Some("leading"),
                },
                success: Token {
                    name: "success",
                    hue: 125,
                    lightness: 0.48,
                    chroma: 0.10,
                    icon: "ok",
                    position_cue: Some("trailing"),
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
