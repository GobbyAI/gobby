//! Confirm and rename dialogs. No daemon configuration.

use crate::theme::Theme;
use crate::ui::widgets::{centered_popup_rect, render_modal_header, render_panel_shell};
use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

pub fn render_confirm_close(frame: &mut Frame, area: Rect, theme: &Theme, title: &str) {
    let Some(popup) = centered_popup_rect(area, 48, 8) else {
        return;
    };
    let Some(inner) = render_panel_shell(frame, popup, theme) else {
        return;
    };
    render_modal_header(frame, inner, title, theme);
}

pub fn render_rename(frame: &mut Frame, area: Rect, theme: &Theme, value: &str) {
    let Some(popup) = centered_popup_rect(area, 48, 7) else {
        return;
    };
    let Some(inner) = render_panel_shell(frame, popup, theme) else {
        return;
    };
    render_modal_header(frame, inner, "rename", theme);
    frame.render_widget(Paragraph::new(value), inner);
}
