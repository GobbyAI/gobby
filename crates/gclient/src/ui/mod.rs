//! Imported herdr UI chrome, rewired to Gobby roster and attention.

pub mod dialogs;
pub mod keybind_help;
pub mod navigator;
pub mod pane_layout;
pub mod panes;
pub mod scrollbar;
pub mod settings;
pub mod sidebar;
pub mod sidebar_rows;
pub mod status;
pub mod tab_surface;
pub mod tabs;
pub mod text;
pub mod widgets;

use crate::app::Workspace;
use crate::theme::Theme;
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::{Constraint, Layout};
use ratatui::Frame;

pub fn render_workspace(frame: &mut Frame, ws: &Workspace, theme: &Theme, query: &str) {
    let area = frame.area();
    let columns = Layout::horizontal([Constraint::Length(24), Constraint::Min(20)]).split(area);
    sidebar::render_sidebar(frame, columns[0], ws);
    let surface = tab_surface::compute_tab_surface(columns[1]);
    let _ = tab_surface::resize_tab_surface(columns[1], 0);
    tabs::render_tab_bar(frame, surface.tabs, ws);
    panes::render_panes(frame, surface.body, ws);
    let metrics = pane_layout::metrics_for(0, 10, surface.body.height);
    scrollbar::render_scrollbar(frame, columns[1], metrics);
    status::render_status(frame, area, ws);
    navigator::render_navigator(frame, area, ws);
    keybind_help::render_keybind_help(frame, area, query);
    dialogs::render_confirm_close(frame, area, theme, "close pane");
    dialogs::render_rename(frame, area, theme, "term");
    let prefs = settings::ClientPrefs::default();
    settings::render_settings(frame, area, theme, &prefs);
    let _ = text::middle_elide("gobby-client workspace", 8);
    let _ = text::display_width_u16("gobby");
    let _ = widgets::split_header_body(area);
    let _ = scrollbar::should_show_scrollbar(ScrollMetrics {
        offset_from_bottom: 0,
        max_offset_from_bottom: 4,
        viewport_rows: 24,
    });
}
