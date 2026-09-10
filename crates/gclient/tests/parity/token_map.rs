//! 4.1.3: colour normalisation for ported herdr assertions.
//!
//! herdr tests compare cell colours against `Palette::catppuccin()` roles
//! (`palette.red`, `palette.overlay0`, ...). gclient keeps those role names on
//! `theme::Palette` but fills them from the `.impeccable.md` tokens, so a
//! ported assertion keeps its role verbatim and takes the value from
//! [`palette`]. A glyph, layout, truncation, or focus-junction change still
//! fails the ported test; a theme value change moves both the render and the
//! expectation, so it does not.

use gobby_client::theme::{Palette, Theme, ThemeKind};
use gobby_client::ui::chrome::RowState;
use gobby_client::ui::status::{
    render_toast_notification, state_dot, toast_cue_width, Toast, ToastKind,
};
use gobby_client::ui::Chrome;
use ratatui::style::Color;

use super::fixtures::{cell, rect_rows, render};

/// The theme ported tests render with.
pub fn theme() -> Theme {
    Theme::new(ThemeKind::Dark)
}

/// The palette ported colour assertions compare against.
pub fn palette() -> Palette {
    theme().palette()
}

/// The palette role `state_dot` paints each row state with.
fn state_dot_role(state: RowState, p: &Palette) -> Color {
    match state {
        RowState::Attention => p.red,
        RowState::Orphaned => p.peach,
        RowState::Working => p.yellow,
        RowState::Unseen => p.teal,
        RowState::Idle => p.green,
        RowState::Unknown => p.overlay0,
    }
}

/// Renders one info toast under `kind` and returns its title row text plus
/// the cue cell and title cell foregrounds.
fn toast_title_row(kind: ThemeKind) -> (String, Color, Color) {
    let mut chrome = Chrome::new(Theme::new(kind));
    chrome.toast = Some(Toast {
        kind: ToastKind::Info,
        title: "term-alpha".to_string(),
        body: None,
        target: None,
    });
    let mut rect = None;
    let term = render(80, 24, |frame| {
        rect = render_toast_notification(frame, frame.area(), &chrome);
    });
    let rect = rect.expect("toast drawn");
    let inner = ratatui::layout::Rect::new(rect.x + 1, rect.y + 1, rect.width - 2, 1);
    let row = rect_rows(&term, inner).remove(0);
    let cue = cell(&term, inner.x, inner.y).fg;
    // The title starts right after the fixed "<glyph> <label>  " cue.
    let title_x = inner.x + toast_cue_width(ToastKind::Info);
    let title = cell(&term, title_x, inner.y).fg;
    (row, cue, title)
}

/// Theme values are the only allowed divergence between a ported test and
/// its herdr original.
///
/// A glyph or alignment change fails the ported tests; a theme value change
/// does not, because every colour assertion takes its expectation from
/// `palette()`. Proof: the same elements render under both themes with
/// identical text, at least one asserted colour differs between the themes,
/// and each theme's colour equals the matching role on
/// `Theme::new(kind).palette()`.
#[test]
fn theme_values_are_the_only_allowed_divergence() {
    let dark = Theme::new(ThemeKind::Dark).palette();
    let light = Theme::new(ThemeKind::Light).palette();

    for state in RowState::ALL {
        let (dark_glyph, dark_color) = state_dot(state, &dark);
        let (light_glyph, light_color) = state_dot(state, &light);
        assert_eq!(
            dark_glyph, light_glyph,
            "{state:?} glyph is theme-independent"
        );
        assert_eq!(
            dark_color,
            state_dot_role(state, &dark),
            "{state:?} dark role"
        );
        assert_eq!(
            light_color,
            state_dot_role(state, &light),
            "{state:?} light role"
        );
    }

    let (dark_row, dark_cue, dark_title) = toast_title_row(ThemeKind::Dark);
    let (light_row, light_cue, light_title) = toast_title_row(ThemeKind::Light);
    assert_eq!(
        dark_row, light_row,
        "toast title row text is theme-independent"
    );
    assert_eq!(dark_row.trim_end(), "◇ info  term-alpha");
    assert_eq!(dark_cue, dark.blue);
    assert_eq!(light_cue, light.blue);
    assert_eq!(dark_title, dark.text);
    assert_eq!(light_title, light.text);
    assert_ne!(
        dark_title, light_title,
        "themes differ in at least one asserted colour"
    );
}
