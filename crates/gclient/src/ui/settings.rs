//! Client-local preferences: theme, keybinds, layout. Not daemon config.

use crate::theme::{Theme, ThemeKind};
use crate::ui::widgets::{centered_popup_rect, render_modal_header, render_panel_shell};
use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClientPrefs {
    pub theme: String,
    pub keybinds: String,
    pub layout: String,
}

impl Default for ClientPrefs {
    fn default() -> Self {
        Self {
            theme: "dark".into(),
            keybinds: "default".into(),
            layout: "bsp".into(),
        }
    }
}

impl ClientPrefs {
    pub fn theme_kind(&self) -> ThemeKind {
        if self.theme == "light" {
            ThemeKind::Light
        } else {
            ThemeKind::Dark
        }
    }
}

pub fn render_settings(frame: &mut Frame, area: Rect, theme: &Theme, prefs: &ClientPrefs) {
    let Some(popup) = centered_popup_rect(area, 56, 12) else {
        return;
    };
    let Some(inner) = render_panel_shell(frame, popup, theme) else {
        return;
    };
    render_modal_header(frame, inner, "client preferences", theme);
    let body = format!(
        "theme {theme}\nkeybinds {keys}\nlayout {layout}",
        theme = prefs.theme,
        keys = prefs.keybinds,
        layout = prefs.layout
    );
    frame.render_widget(Paragraph::new(body), inner);
}
