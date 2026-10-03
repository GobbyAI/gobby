// upstream: herdr v0.8.0 src/ui/settings.rs
//! Client-local preferences (theme, keymap path, layout knobs) and the
//! settings overlay that edits them. Nothing here reaches the daemon.

use crate::theme::ThemeKind;
use crate::ui::chrome::Chrome;
use crate::ui::widgets::{
    action_button_row_rects, centered_popup_rect, modal_choice_rows, modal_stack_areas,
    panel_contrast_fg, render_action_button, render_panel_shell, ActionButtonSpec,
};
use crossterm::event::KeyModifiers;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const SETTINGS_POPUP_WIDTH: u16 = 76;
pub const SETTINGS_POPUP_HEIGHT: u16 = 23;
/// Column where a row's current value starts.
const VALUE_COLUMN: usize = 30;

/// Modifier that sends a right-click to the pane's app instead of the pane
/// menu (herdr `right_click_passthrough_modifier`); `None` leaves every
/// right-click to the menu unless the pane's own flag is set.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PassthroughModifier {
    #[default]
    None,
    Shift,
    Alt,
    Ctrl,
}

impl PassthroughModifier {
    /// The modifier a right-click must carry, exactly, to pass through.
    pub fn key_modifiers(self) -> Option<KeyModifiers> {
        match self {
            PassthroughModifier::None => Option::None,
            PassthroughModifier::Shift => Some(KeyModifiers::SHIFT),
            PassthroughModifier::Alt => Some(KeyModifiers::ALT),
            PassthroughModifier::Ctrl => Some(KeyModifiers::CONTROL),
        }
    }

    /// The prefs-file spelling, which the settings row shows.
    pub fn label(self) -> &'static str {
        match self {
            PassthroughModifier::None => "none",
            PassthroughModifier::Shift => "shift",
            PassthroughModifier::Alt => "alt",
            PassthroughModifier::Ctrl => "ctrl",
        }
    }
}

/// Order of the sidebar's agent rows (herdr `agent_sort`): `grouped` keeps
/// the tab order, `priority` puts the most urgent row first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AgentSort {
    #[default]
    Grouped,
    Priority,
}

/// The edge the sidebar keeps, pinned or as an overlay.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SidebarSide {
    #[default]
    Left,
    Right,
}

impl SidebarSide {
    /// The prefs-file spelling, which the settings row shows.
    pub fn label(self) -> &'static str {
        match self {
            Self::Left => "left",
            Self::Right => "right",
        }
    }

    pub fn toggled(self) -> Self {
        match self {
            Self::Left => Self::Right,
            Self::Right => Self::Left,
        }
    }
}

/// How an over-long title moves through its window, on the one ticker the
/// Agents rows and the pane headers share: off (it truncates), or the
/// direction it travels to reveal its tail.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TitleScrolling {
    Off,
    #[default]
    Left,
    Right,
}

impl TitleScrolling {
    const ALL: [TitleScrolling; 3] = [Self::Off, Self::Left, Self::Right];

    /// The prefs-file spelling, which the settings row shows.
    pub fn label(self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Left => "left",
            Self::Right => "right",
        }
    }

    /// The value `delta` steps away, wrapping at either end.
    pub fn stepped(self, delta: isize) -> Self {
        let current = Self::ALL
            .iter()
            .position(|value| *value == self)
            .unwrap_or(0);
        let next = (current as isize + delta).rem_euclid(Self::ALL.len() as isize);
        Self::ALL[next as usize]
    }
}

impl AgentSort {
    /// The prefs-file spelling, which the settings row and the sidebar
    /// header show.
    pub fn label(self) -> &'static str {
        match self {
            AgentSort::Grouped => "grouped",
            AgentSort::Priority => "priority",
        }
    }

    pub fn toggled(self) -> Self {
        match self {
            AgentSort::Grouped => AgentSort::Priority,
            AgentSort::Priority => AgentSort::Grouped,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ClientPrefs {
    pub theme: String,
    /// Draw gclient's chrome in grays (`Palette::monochrome`); pane
    /// contents keep their apps' colours.
    pub monochrome: bool,
    /// Capture mouse events for gclient; `false` leaves the terminal's native
    /// selection and scrolling untouched (`--no-mouse` forces it off).
    pub mouse_capture: bool,
    /// Override file for the keymap; empty means the default path.
    pub keybinds: String,
    pub layout: String,
    pub pane_scrollbars: bool,
    pub pane_gaps: bool,
    pub confirm_close: bool,
    pub hide_tab_bar_when_single_tab: bool,
    pub sidebar_width: u16,
    /// The edge the sidebar keeps.
    pub sidebar_side: SidebarSide,
    /// The sidebar keeps its column beside the panes instead of opening as
    /// an overlay.
    pub sidebar_pinned: bool,
    /// Held alone, this modifier makes a right-click pass through to the
    /// pane's app instead of opening the pane menu.
    pub right_click_passthrough_modifier: PassthroughModifier,
    /// Order of the sidebar's agent rows.
    pub agent_sort: AgentSort,
    /// Project ids in the order the user dragged the cards into.
    pub project_order: Vec<String>,
    /// Labels the user gave project cards, by project id.
    pub project_labels: BTreeMap<String, String>,
    /// Direction used by the shared pane/sidebar title ticker.
    pub title_scrolling: TitleScrolling,
    pub status_left: Vec<String>,
    pub status_right: Vec<String>,
    /// Draw Nerd Font symbols (the pane's sandbox lock); `false` spells them
    /// in plain text for a font without them.
    pub nerd_glyphs: bool,
}

impl Default for ClientPrefs {
    fn default() -> Self {
        Self {
            theme: "dark".to_string(),
            monochrome: false,
            mouse_capture: true,
            keybinds: String::new(),
            layout: "default".to_string(),
            pane_scrollbars: true,
            pane_gaps: true,
            confirm_close: true,
            hide_tab_bar_when_single_tab: false,
            sidebar_width: 26,
            sidebar_side: SidebarSide::Left,
            sidebar_pinned: true,
            right_click_passthrough_modifier: PassthroughModifier::None,
            agent_sort: AgentSort::Grouped,
            project_order: Vec::new(),
            project_labels: BTreeMap::new(),
            title_scrolling: TitleScrolling::Left,
            status_left: vec!["focus".to_string()],
            status_right: Vec::new(),
            nerd_glyphs: true,
        }
    }
}

impl ClientPrefs {
    /// System follows the terminal: its appearance picks the palette and its
    /// own colours stay the ground.
    pub fn follows_system(&self) -> bool {
        self.theme.eq_ignore_ascii_case("system")
    }

    pub fn theme_kind(&self) -> ThemeKind {
        match self.theme.to_ascii_lowercase().as_str() {
            "light" => ThemeKind::Light,
            "system" if matches!(dark_light::detect(), Ok(dark_light::Mode::Light)) => {
                ThemeKind::Light
            }
            _ => ThemeKind::Dark,
        }
    }
}

/// Rows of the settings overlay, in display order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsRow {
    Theme,
    Monochrome,
    MouseCapture,
    PaneScrollbars,
    PaneGaps,
    ConfirmClose,
    HideTabBarWhenSingleTab,
    SidebarWidth,
    SidebarSide,
    SidebarPinned,
    RightClickPassthrough,
    AgentSort,
    TitleScrolling,
}

impl SettingsRow {
    pub const ALL: [SettingsRow; 13] = [
        SettingsRow::Theme,
        SettingsRow::Monochrome,
        SettingsRow::MouseCapture,
        SettingsRow::PaneScrollbars,
        SettingsRow::PaneGaps,
        SettingsRow::ConfirmClose,
        SettingsRow::HideTabBarWhenSingleTab,
        SettingsRow::SidebarWidth,
        SettingsRow::SidebarSide,
        SettingsRow::SidebarPinned,
        SettingsRow::RightClickPassthrough,
        SettingsRow::AgentSort,
        SettingsRow::TitleScrolling,
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
        SettingsRow::Theme => "Theme",
        SettingsRow::Monochrome => "Monochrome",
        SettingsRow::MouseCapture => "Mouse capture",
        SettingsRow::PaneScrollbars => "Pane scrollbars",
        SettingsRow::PaneGaps => "Pane gaps",
        SettingsRow::ConfirmClose => "Confirm close",
        SettingsRow::HideTabBarWhenSingleTab => "Hide tab bar with one tab",
        SettingsRow::SidebarWidth => "Sidebar width",
        SettingsRow::SidebarSide => "Sidebar side",
        SettingsRow::SidebarPinned => "Sidebar pinned",
        SettingsRow::RightClickPassthrough => "Right-click passthrough",
        SettingsRow::AgentSort => "Agent sort",
        SettingsRow::TitleScrolling => "Title scrolling",
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
        SettingsRow::Monochrome => on_off(prefs.monochrome).to_string(),
        SettingsRow::MouseCapture => on_off(prefs.mouse_capture).to_string(),
        SettingsRow::PaneScrollbars => on_off(prefs.pane_scrollbars).to_string(),
        SettingsRow::PaneGaps => on_off(prefs.pane_gaps).to_string(),
        SettingsRow::ConfirmClose => on_off(prefs.confirm_close).to_string(),
        SettingsRow::HideTabBarWhenSingleTab => {
            on_off(prefs.hide_tab_bar_when_single_tab).to_string()
        }
        SettingsRow::SidebarWidth => prefs.sidebar_width.to_string(),
        SettingsRow::SidebarSide => prefs.sidebar_side.label().to_string(),
        SettingsRow::SidebarPinned => on_off(prefs.sidebar_pinned).to_string(),
        SettingsRow::RightClickPassthrough => {
            prefs.right_click_passthrough_modifier.label().to_string()
        }
        SettingsRow::AgentSort => prefs.agent_sort.label().to_string(),
        SettingsRow::TitleScrolling => prefs.title_scrolling.label().to_string(),
    }
}

/// Rects the settings overlay drew: the popup (border included), every
/// row that fit, keyed by its index into `SettingsRow::ALL`, and the footer
/// buttons in drawn order (`done`, `close`), both of which close the popup.
#[derive(Debug, Clone, Default)]
pub struct SettingsHits {
    pub dialog: Rect,
    pub rows: Vec<(usize, Rect)>,
    pub buttons: Vec<Rect>,
}

/// Draw the settings overlay; `None` when `area` cannot fit the popup.
pub fn render_settings(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Option<SettingsHits> {
    let p = &chrome.palette;
    let popup = centered_popup_rect(area, SETTINGS_POPUP_WIDTH, SETTINGS_POPUP_HEIGHT)?;
    let mut hits = SettingsHits {
        dialog: popup,
        rows: Vec::new(),
        buttons: Vec::new(),
    };
    let Some(inner) = render_panel_shell(frame, popup, p.accent, p.panel_bg) else {
        return Some(hits);
    };
    if inner.height < 4 || inner.width < 10 {
        return Some(hits);
    }

    let stack = modal_stack_areas(inner, 3, 2, 0, 1);
    let header_rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas::<3>(stack.header);

    let mut title = vec![Span::styled(
        " Settings",
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
        hits.rows.push((index, rect));
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
        return Some(hits);
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
                label: "Done",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "Close",
            },
        ],
        2,
        inner.height.saturating_sub(1),
    );
    if let [done_rect, close_rect] = rects[..] {
        hits.buttons = vec![done_rect, close_rect];
        render_action_button(
            frame,
            done_rect,
            Some("↵"),
            "Done",
            Style::default()
                .fg(panel_contrast_fg(p))
                .bg(p.accent)
                .add_modifier(Modifier::BOLD),
        );
        render_action_button(
            frame,
            close_rect,
            Some("esc"),
            "Close",
            Style::default()
                .fg(p.text)
                .bg(p.surface0)
                .add_modifier(Modifier::BOLD),
        );
    }
    Some(hits)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn row_values_follow_prefs() {
        let mut prefs = ClientPrefs::default();
        let labels: Vec<&str> = SettingsRow::ALL.into_iter().map(row_label).collect();
        assert_eq!(
            labels,
            [
                "Theme",
                "Monochrome",
                "Mouse capture",
                "Pane scrollbars",
                "Pane gaps",
                "Confirm close",
                "Hide tab bar with one tab",
                "Sidebar width",
                "Sidebar side",
                "Sidebar pinned",
                "Right-click passthrough",
                "Agent sort",
                "Title scrolling",
            ]
        );
        assert_eq!(row_value(SettingsRow::SidebarSide, &prefs), "left");
        assert_eq!(row_value(SettingsRow::SidebarPinned, &prefs), "on");
        prefs.sidebar_side = prefs.sidebar_side.toggled();
        prefs.sidebar_pinned = true;
        assert_eq!(row_value(SettingsRow::SidebarSide, &prefs), "right");
        assert_eq!(row_value(SettingsRow::SidebarPinned, &prefs), "on");
        // Both survive prefs.toml under their own keys.
        let home = tempfile::tempdir().expect("temp gobby home");
        let path = crate::prefs::save_prefs(home.path(), &prefs).expect("save prefs");
        let text = std::fs::read_to_string(path).expect("read prefs");
        assert!(text.contains("sidebar_side = \"right\""), "{text}");
        assert!(text.contains("sidebar_pinned = true"), "{text}");
        let loaded = crate::prefs::load_prefs(home.path()).expect("load prefs");
        assert_eq!(loaded.sidebar_side, SidebarSide::Right);
        assert!(loaded.sidebar_pinned);
        assert_eq!(row_value(SettingsRow::Theme, &prefs), "dark");
        assert_eq!(row_value(SettingsRow::Monochrome, &prefs), "off");
        prefs.monochrome = true;
        assert_eq!(row_value(SettingsRow::Monochrome, &prefs), "on");
        assert_eq!(row_value(SettingsRow::MouseCapture, &prefs), "on");
        assert_eq!(row_value(SettingsRow::PaneGaps, &prefs), "on");
        prefs.pane_gaps = false;
        prefs.sidebar_width = 30;
        prefs.mouse_capture = false;
        assert_eq!(row_value(SettingsRow::PaneGaps, &prefs), "off");
        assert_eq!(row_value(SettingsRow::SidebarWidth, &prefs), "30");
        assert_eq!(row_value(SettingsRow::MouseCapture, &prefs), "off");
        assert_eq!(
            row_value(SettingsRow::RightClickPassthrough, &prefs),
            "none"
        );
        prefs.right_click_passthrough_modifier = PassthroughModifier::Alt;
        assert_eq!(row_value(SettingsRow::RightClickPassthrough, &prefs), "alt");
        assert_eq!(row_value(SettingsRow::AgentSort, &prefs), "grouped");
        prefs.agent_sort = prefs.agent_sort.toggled();
        assert_eq!(row_value(SettingsRow::AgentSort, &prefs), "priority");
        assert_eq!(row_value(SettingsRow::TitleScrolling, &prefs), "left");
        prefs.title_scrolling = TitleScrolling::Right;
        assert_eq!(row_value(SettingsRow::TitleScrolling, &prefs), "right");
        assert_eq!(TitleScrolling::Right.stepped(1), TitleScrolling::Off);
        assert_eq!(TitleScrolling::Off.stepped(-1), TitleScrolling::Right);
        assert_eq!(TitleScrolling::Off.stepped(1), TitleScrolling::Left);
    }
}
