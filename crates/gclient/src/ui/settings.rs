// upstream: herdr v0.8.0 src/ui/settings.rs
//! Client-local preferences (theme, keymap path, layout knobs) and the
//! settings overlay that edits them. Nothing here reaches the daemon.

use crate::theme::ThemeKind;
use crate::ui::chrome::Chrome;
use crate::ui::widgets::{
    action_button_row_rects, centered_popup_rect, modal_choice_rows, modal_stack_areas,
    panel_contrast_fg, render_action_button, render_panel_shell, ActionButtonSpec,
};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;
use serde::{Deserialize, Serialize};

pub const SETTINGS_POPUP_WIDTH: u16 = 76;
pub const SETTINGS_POPUP_HEIGHT: u16 = 22;
/// Column where a row's current value starts.
const VALUE_COLUMN: usize = 30;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ClientPrefs {
    pub theme: String,
    /// Override file for the keymap; empty means the default path.
    pub keybinds: String,
    pub layout: String,
    pub pane_borders: bool,
    pub pane_scrollbars: bool,
    pub pane_gaps: bool,
    pub confirm_close: bool,
    pub hide_tab_bar_when_single_tab: bool,
    pub sidebar_width: u16,
}

impl Default for ClientPrefs {
    fn default() -> Self {
        Self {
            theme: "dark".to_string(),
            keybinds: String::new(),
            layout: "default".to_string(),
            pane_borders: true,
            pane_scrollbars: true,
            pane_gaps: true,
            confirm_close: true,
            hide_tab_bar_when_single_tab: false,
            sidebar_width: 26,
        }
    }
}

impl ClientPrefs {
    pub fn theme_kind(&self) -> ThemeKind {
        if self.theme.eq_ignore_ascii_case("light") {
            ThemeKind::Light
        } else {
            ThemeKind::Dark
        }
    }
}

/// Rows of the settings overlay, in display order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsRow {
    Theme,
    PaneBorders,
    PaneScrollbars,
    PaneGaps,
    ConfirmClose,
    HideTabBarWhenSingleTab,
    SidebarWidth,
}

impl SettingsRow {
    pub const ALL: [SettingsRow; 7] = [
        SettingsRow::Theme,
        SettingsRow::PaneBorders,
        SettingsRow::PaneScrollbars,
        SettingsRow::PaneGaps,
        SettingsRow::ConfirmClose,
        SettingsRow::HideTabBarWhenSingleTab,
        SettingsRow::SidebarWidth,
    ];
}

#[derive(Debug, Clone, Default)]
pub struct SettingsState {
    pub selected: usize,
    pub scroll: usize,
    pub dirty: bool,
}

fn row_label(row: SettingsRow) -> &'static str {
    match row {
        SettingsRow::Theme => "theme",
        SettingsRow::PaneBorders => "pane borders",
        SettingsRow::PaneScrollbars => "pane scrollbars",
        SettingsRow::PaneGaps => "pane gaps",
        SettingsRow::ConfirmClose => "confirm close",
        SettingsRow::HideTabBarWhenSingleTab => "hide tab bar with one tab",
        SettingsRow::SidebarWidth => "sidebar width",
    }
}

fn on_off(value: bool) -> &'static str {
    if value {
        "on"
    } else {
        "off"
    }
}

fn row_value(row: SettingsRow, prefs: &ClientPrefs) -> String {
    match row {
        SettingsRow::Theme => prefs.theme.clone(),
        SettingsRow::PaneBorders => on_off(prefs.pane_borders).to_string(),
        SettingsRow::PaneScrollbars => on_off(prefs.pane_scrollbars).to_string(),
        SettingsRow::PaneGaps => on_off(prefs.pane_gaps).to_string(),
        SettingsRow::ConfirmClose => on_off(prefs.confirm_close).to_string(),
        SettingsRow::HideTabBarWhenSingleTab => {
            on_off(prefs.hide_tab_bar_when_single_tab).to_string()
        }
        SettingsRow::SidebarWidth => prefs.sidebar_width.to_string(),
    }
}

pub fn render_settings(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    let p = &chrome.palette;
    let Some(popup) = centered_popup_rect(area, SETTINGS_POPUP_WIDTH, SETTINGS_POPUP_HEIGHT) else {
        return;
    };
    let Some(inner) = render_panel_shell(frame, popup, p.accent, p.panel_bg) else {
        return;
    };
    if inner.height < 4 || inner.width < 10 {
        return;
    }

    let stack = modal_stack_areas(inner, 3, 2, 0, 1);
    let header_rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas::<3>(stack.header);

    let mut title = vec![Span::styled(
        " settings",
        Style::default().fg(p.text).add_modifier(Modifier::BOLD),
    )];
    if chrome.settings.dirty {
        title.push(Span::styled("  ● modified", Style::default().fg(p.yellow)));
    }
    frame.render_widget(Paragraph::new(Line::from(title)), header_rows[0]);
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            " client-local preferences; nothing here reaches the daemon",
            Style::default().fg(p.overlay1),
        ))),
        header_rows[1],
    );
    let sep = "─".repeat(inner.width as usize);
    frame.render_widget(
        Paragraph::new(Span::styled(sep, Style::default().fg(p.surface0))),
        header_rows[2],
    );

    let scroll = chrome.settings.scroll.min(SettingsRow::ALL.len());
    let rows = modal_choice_rows(stack.content, SettingsRow::ALL.len() - scroll, 1);
    for (row, rect) in SettingsRow::ALL.iter().skip(scroll).zip(rows) {
        let index = SettingsRow::ALL
            .iter()
            .position(|candidate| candidate == row)
            .unwrap_or(0);
        let is_selected = index == chrome.settings.selected;
        let marker = if is_selected { " ▸ " } else { "   " };
        let style = if is_selected {
            Style::default()
                .bg(p.surface0)
                .fg(p.text)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(p.subtext0)
        };
        let label = format!(
            "{marker}{:<width$}{}",
            row_label(*row),
            row_value(*row, &chrome.prefs),
            width = VALUE_COLUMN
        );
        frame.render_widget(Paragraph::new(label).style(style), rect);
    }

    let Some(footer) = stack.footer else {
        return;
    };
    let [hint_row, _] =
        Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas::<2>(footer);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" ↑↓", Style::default().fg(p.overlay0)),
            Span::styled(" select  ", Style::default().fg(p.overlay1)),
            Span::styled("←→", Style::default().fg(p.overlay0)),
            Span::styled(" change", Style::default().fg(p.overlay1)),
        ])),
        hint_row,
    );
    let rects = action_button_row_rects(
        inner,
        &[
            ActionButtonSpec {
                hint: Some("↵"),
                label: "apply",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "close",
            },
        ],
        2,
        inner.height.saturating_sub(1),
    );
    if let [apply_rect, close_rect] = rects[..] {
        render_action_button(
            frame,
            apply_rect,
            Some("↵"),
            "apply",
            Style::default()
                .fg(panel_contrast_fg(p))
                .bg(p.accent)
                .add_modifier(Modifier::BOLD),
        );
        render_action_button(
            frame,
            close_rect,
            Some("esc"),
            "close",
            Style::default()
                .fg(p.text)
                .bg(p.surface0)
                .add_modifier(Modifier::BOLD),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn row_values_follow_prefs() {
        let mut prefs = ClientPrefs::default();
        assert_eq!(row_value(SettingsRow::Theme, &prefs), "dark");
        assert_eq!(row_value(SettingsRow::PaneGaps, &prefs), "on");
        prefs.pane_gaps = false;
        prefs.sidebar_width = 30;
        assert_eq!(row_value(SettingsRow::PaneGaps, &prefs), "off");
        assert_eq!(row_value(SettingsRow::SidebarWidth, &prefs), "30");
    }
}
