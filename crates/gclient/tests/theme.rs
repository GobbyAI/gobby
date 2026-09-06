//! 3.1.5 / 3.1.9 / 3.1.10: the token map matches `.impeccable.md`, survives
//! monochrome, and keeps AA contrast on every surface in both themes.

use gobby_client::app::ControlState;
use gobby_client::theme::{
    contrast_ratio, relative_luminance, Palette, Theme, ThemeKind, BRAND_HUE, DESTRUCTIVE_HUE,
    INFO_HUE, WARNING_HUE,
};
use gobby_client::ui::chrome::RowState;
use gobby_client::ui::status::{
    control_indicator, render_toast_notification, state_dot, state_label, toast_cue, Toast,
    ToastKind,
};
use gobby_client::ui::Chrome;
use ratatui::backend::TestBackend;
use ratatui::style::Color;
use ratatui::Terminal;
use std::collections::HashSet;

const TOAST_KINDS: [ToastKind; 4] = [
    ToastKind::Info,
    ToastKind::Warning,
    ToastKind::Error,
    ToastKind::Success,
];

/// Renders one toast and returns its title row (colour stripped) plus the
/// foreground of the first cell, where the kind cue sits.
fn toast_title_row(kind: ThemeKind, toast_kind: ToastKind) -> (String, Color) {
    let mut chrome = Chrome::new(Theme::new(kind));
    chrome.toast = Some(Toast {
        kind: toast_kind,
        title: "term-alpha".to_string(),
        body: None,
        target: None,
    });
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    let mut rect = None;
    terminal
        .draw(|frame| rect = render_toast_notification(frame, frame.area(), &chrome))
        .unwrap();
    let rect = rect.expect("toast drawn");
    let buffer = terminal.backend().buffer();
    let y = rect.y + 1;
    let row: String = (rect.x + 1..rect.x + rect.width - 1)
        .map(|x| buffer[(x, y)].symbol())
        .collect();
    (row, buffer[(rect.x + 1, y)].fg)
}

const NEUTRAL_NAMES: &[&str] = &[
    "panel_bg",
    "surface0",
    "surface1",
    "surface_dim",
    "overlay0",
    "overlay1",
    "text",
    "subtext0",
    "mauve",
];

#[test]
fn tokens_match_design_contract_and_survive_monochrome() {
    let dark = Theme::new(ThemeKind::Dark);
    let light = Theme::new(ThemeKind::Light);
    assert!(
        dark.neutrals.panel_bg.relative_luminance() < light.neutrals.panel_bg.relative_luminance()
    );

    for theme in [&dark, &light] {
        let kind = theme.kind;
        assert_eq!(theme.accent.hue, BRAND_HUE);
        assert_eq!(theme.info.hue, INFO_HUE);
        assert_eq!(theme.warning.hue, WARNING_HUE);
        assert_eq!(theme.destructive.hue, DESTRUCTIVE_HUE);
        assert_eq!(theme.success.hue, BRAND_HUE);
        assert!(
            theme.success.lightness != theme.accent.lightness
                && theme.success.chroma < theme.accent.chroma,
            "success is lightness/chroma differentiated brand hue"
        );

        // Every herdr palette name resolves to a contract token.
        let entries = Palette::entries(theme);
        assert_eq!(entries.len(), 16);
        for (name, token) in entries {
            assert!(
                [BRAND_HUE, INFO_HUE, WARNING_HUE, DESTRUCTIVE_HUE].contains(&token.hue),
                "{kind:?} {name} hue {}",
                token.hue
            );
            let rgb = token.rgb();
            assert_ne!(rgb, (0, 0, 0), "{kind:?} {name} is pure black");
            assert_ne!(rgb, (255, 255, 255), "{kind:?} {name} is pure white");
            if NEUTRAL_NAMES.contains(&name) {
                assert_eq!(token.hue, BRAND_HUE, "{kind:?} {name} neutral tint");
                assert!(
                    token.chroma >= 0.005 && token.chroma <= 0.008,
                    "{kind:?} {name} chroma {}",
                    token.chroma
                );
            }
        }
        let palette = theme.palette();
        assert_eq!(palette.red, theme.destructive.color());
        assert_eq!(palette.blue, theme.info.color());
        assert_eq!(palette.yellow, theme.warning.color());
        assert_eq!(palette.green, theme.success.color());
        assert_eq!(palette.accent, theme.accent.color());

        // Neutral ramp is strictly ordered so surfaces stay distinguishable.
        let ramp: Vec<f64> = theme
            .neutrals
            .all()
            .iter()
            .map(|t| t.relative_luminance())
            .collect();
        let ordered = match kind {
            ThemeKind::Dark => ramp.windows(2).all(|w| w[0] < w[1]),
            ThemeKind::Light => ramp.windows(2).all(|w| w[0] > w[1]),
        };
        assert!(ordered, "{kind:?} neutral ramp {ramp:?}");

        // Text and state labels reach AA (4.5:1) on every surface; the
        // focus ring reaches AA non-text (3:1) on every surface.
        let focus = theme.focus_ring();
        assert_eq!(focus.token.hue, BRAND_HUE);
        assert_eq!(focus.token.rgb(), theme.accent.rgb());
        assert!(!focus.position_cue.is_empty() && !focus.marker.is_empty());
        for surface in theme.neutrals.surfaces() {
            let bg = surface.rgb();
            for text in [&theme.neutrals.text, &theme.neutrals.subtext0] {
                let ratio = contrast_ratio(text.rgb(), bg);
                assert!(
                    ratio >= 4.5,
                    "{kind:?} {} on {}: {ratio:.2}",
                    text.name,
                    surface.name
                );
            }
            for state in theme.states() {
                let ratio = contrast_ratio(state.rgb(), bg);
                assert!(
                    ratio >= 4.5,
                    "{kind:?} {} on {}: {ratio:.2}",
                    state.name,
                    surface.name
                );
            }
            let ratio = contrast_ratio(focus.token.rgb(), bg);
            assert!(
                ratio >= 3.0,
                "{kind:?} focus ring on {}: {ratio:.2}",
                surface.name
            );
        }

        // State colours keep distinct grayscale ranks and the ANSI-256
        // lightness order follows them.
        let mut lum: Vec<(&str, f64)> = theme
            .states()
            .iter()
            .map(|t| (t.name, relative_luminance(t.rgb())))
            .collect();
        lum.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        assert!(
            lum.windows(2).all(|w| (w[0].1 - w[1].1).abs() >= 0.02),
            "{kind:?} grayscale ranks too close: {lum:?}"
        );
        let mono = theme.monochrome_ranks();
        assert!(mono.windows(2).all(|w| w[0] != w[1]), "{kind:?} {mono:?}");
        let ansi = theme.ansi256_ranks();
        assert_eq!(ansi, {
            let mut sorted = mono.clone();
            sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
            sorted
        });
        let ansi_names: Vec<&str> = ansi.iter().map(|(n, _)| *n).collect();
        let lum_names: Vec<&str> = lum.iter().map(|(n, _)| *n).collect();
        assert_eq!(
            ansi_names, lum_names,
            "{kind:?} ANSI order differs from luminance order"
        );
        for state in theme.states() {
            assert!(
                !state.icon.is_empty() || state.position_cue.is_some(),
                "{} needs icon or position cue",
                state.name
            );
        }

        // Every rendered indicator carries a glyph plus a label; colour is
        // the fourth signal, so the (glyph, label) pairs must be distinct.
        let mut cues = HashSet::new();
        for state in RowState::ALL {
            let (dot, _) = state_dot(state, &palette);
            let label = state_label(state);
            assert!(!dot.trim().is_empty() && !label.is_empty(), "{state:?}");
            cues.insert((dot, label));
        }
        assert_eq!(
            cues.len(),
            RowState::ALL.len(),
            "row states share a cue: {cues:?}"
        );
        let mut control = HashSet::new();
        for (state, take_back) in [
            (ControlState::Observe, false),
            (ControlState::Held, false),
            (ControlState::LeaseLost, false),
            (ControlState::UncertainReadOnly, false),
            (ControlState::Held, true),
        ] {
            let (glyph, label, _) = control_indicator(state, take_back, &palette);
            assert!(!glyph.trim().is_empty() && !label.is_empty());
            control.insert((glyph, label));
        }
        assert_eq!(control.len(), 5, "control states share a cue: {control:?}");

        // Toasts: every kind leads its title with a fixed cue, so the four
        // title rows stay distinct with colour stripped, and the cue keeps AA
        // contrast on the toast surface.
        let mut toast_cues = HashSet::new();
        for toast_kind in TOAST_KINDS {
            let (row, fg) = toast_title_row(kind, toast_kind);
            let cue = row
                .trim()
                .strip_suffix("term-alpha")
                .unwrap_or_else(|| panic!("{kind:?} {toast_kind:?} title row {row:?}"))
                .trim()
                .to_string();
            let (glyph, label) = toast_cue(toast_kind);
            assert!(
                !glyph.trim().is_empty() && !label.is_empty(),
                "{toast_kind:?}"
            );
            assert_eq!(cue, format!("{glyph} {label}"), "{kind:?} {toast_kind:?}");
            let Color::Rgb(r, g, b) = fg else {
                panic!("{kind:?} {toast_kind:?} cue colour {fg:?}");
            };
            let ratio = contrast_ratio((r, g, b), theme.neutrals.panel_bg.rgb());
            assert!(
                ratio >= 4.5,
                "{kind:?} {toast_kind:?} cue on panel_bg: {ratio:.2}"
            );
            toast_cues.insert(cue);
        }
        assert_eq!(
            toast_cues.len(),
            TOAST_KINDS.len(),
            "{kind:?} toast kinds share a cue: {toast_cues:?}"
        );

        // The map is applied through gobby_terminal's terminal_theme.
        let tt = theme.terminal_theme();
        assert_eq!(tt.foreground, Some(theme.neutrals.text.rgb_color()));
        assert_eq!(tt.background, Some(theme.neutrals.panel_bg.rgb_color()));
        assert_eq!(tt.palette[1], Some(theme.destructive.rgb_color()));
        assert_eq!(tt.palette[2], Some(theme.success.rgb_color()));
        assert_eq!(tt.palette[3], Some(theme.warning.rgb_color()));
        assert_eq!(tt.palette[4], Some(theme.info.rgb_color()));
        assert_eq!(tt.palette[10], Some(theme.accent.rgb_color()));
        assert!(tt.palette[..16].iter().all(Option::is_some));
    }
}

/// `Palette::entries` and `Palette::from_theme` are two hand-maintained lists
/// of the same sixteen role-to-token bindings. `from_theme` is what the render
/// paints with; `entries` is what a test reverse-maps a painted colour back
/// through to name its role (`tests/screens.rs`). Drift between them would
/// rename roles in a screen capture without moving a single pixel, and nothing
/// else pins one list to the other.
#[test]
fn palette_entries_bind_the_same_tokens_the_render_paints_with() {
    for kind in [ThemeKind::Dark, ThemeKind::Light] {
        let theme = Theme::new(kind);
        let palette = theme.palette();
        for (name, token) in Palette::entries(&theme) {
            let painted = match name {
                "accent" => palette.accent,
                "panel_bg" => palette.panel_bg,
                "surface0" => palette.surface0,
                "surface1" => palette.surface1,
                "surface_dim" => palette.surface_dim,
                "overlay0" => palette.overlay0,
                "overlay1" => palette.overlay1,
                "text" => palette.text,
                "subtext0" => palette.subtext0,
                "mauve" => palette.mauve,
                "green" => palette.green,
                "yellow" => palette.yellow,
                "red" => palette.red,
                "blue" => palette.blue,
                "teal" => palette.teal,
                "peach" => palette.peach,
                // A seventeenth role has to be bound here too, or a capture
                // would silently fall back to naming it by raw colour value.
                other => panic!("{kind:?} palette role {other} has no field in this map"),
            };
            assert_eq!(painted, token.color(), "{kind:?} {name}");
        }
    }
}
