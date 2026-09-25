// upstream: none (Gobby-only; shares the modal shell with dialogs/orphans.rs)
//! The alert log: every toast the session raised, newest first, behind the
//! [Menu] button.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::ui::chrome::Chrome;
use crate::ui::status::{toast_cue, Toast};
use crate::ui::text::{display_width_u16, truncate_end};
use crate::ui::widgets::{
    action_button_row_rects, modal_choice_rows, render_action_button, render_modal_description,
    render_modal_header, render_modal_shell, ActionButtonSpec,
};

use super::secondary_button_style;

pub const ALERTS_TITLE: &str = "alerts";
const POPUP_WIDTH: u16 = 72;
/// Header, gap, hint, gap and the button row.
const BASE_HEIGHT: u16 = 7;
const MAX_LIST_ROWS: u16 = 12;

/// Rows of the log the dialog shows at once inside `area`.
pub fn alerts_page_rows(area: Rect) -> usize {
    let room = area.height.saturating_sub(BASE_HEIGHT);
    usize::from(room.clamp(1, MAX_LIST_ROWS))
}

pub fn render_alerts(frame: &mut Frame, area: Rect, chrome: &Chrome, scroll: usize) -> Vec<Rect> {
    let p = &chrome.palette;
    let page = alerts_page_rows(area);
    let list_rows = chrome.alert_log.len().clamp(1, page) as u16;
    let Some(inner) = render_modal_shell(frame, area, POPUP_WIDTH, BASE_HEIGHT + list_rows, p)
    else {
        return Vec::new();
    };
    if inner.height < 5 {
        return Vec::new();
    }
    let areas = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(list_rows),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas::<6>(inner);
    render_modal_header(frame, areas[0], ALERTS_TITLE, p);
    if chrome.alert_log.is_empty() {
        render_modal_description(
            frame,
            areas[2],
            "No alerts yet.",
            Style::default().fg(p.subtext0),
        );
    }
    let shown: Vec<&Toast> = chrome
        .alert_log
        .iter()
        .rev()
        .skip(scroll)
        .take(page)
        .collect();
    for (toast, rect) in shown
        .iter()
        .zip(modal_choice_rows(areas[2], shown.len(), 1))
    {
        frame.render_widget(Paragraph::new(alert_line(toast, rect.width, p)), rect);
    }
    let hidden = chrome.alert_log.len().saturating_sub(scroll + shown.len());
    let hint = if hidden > 0 {
        format!("↑↓ scroll · {hidden} older")
    } else {
        "↑↓ scroll".to_string()
    };
    render_modal_description(frame, areas[4], &hint, Style::default().fg(p.overlay0));
    let specs = [ActionButtonSpec {
        hint: Some("esc"),
        label: "close",
    }];
    let rects = action_button_row_rects(inner, &specs, 2, inner.height.saturating_sub(1));
    if let [close_rect] = rects[..] {
        render_action_button(
            frame,
            close_rect,
            Some("esc"),
            "close",
            secondary_button_style(chrome),
        );
    }
    // The loop hit-tests this as `DialogButton(0)`, which closes the dialog.
    rects
}

/// One log row: the severity cue, the title, then the body after a dot.
fn alert_line(toast: &Toast, width: u16, p: &crate::theme::Palette) -> Line<'static> {
    let (glyph, label) = toast_cue(toast.kind);
    let cue = format!(" {glyph} {label:<7} ");
    let mut text = toast.title.clone();
    if let Some(body) = toast.body.as_deref().filter(|body| !body.is_empty()) {
        text.push_str(" · ");
        text.push_str(body);
    }
    let budget = width.saturating_sub(display_width_u16(&cue));
    let text = truncate_end(&text, usize::from(budget));
    Line::from(vec![
        Span::styled(cue, Style::default().fg(p.subtext0)),
        Span::styled(
            text,
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        ),
    ])
}
