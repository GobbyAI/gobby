// upstream: herdr v0.8.0 src/ui/status.rs
//! Toasts, copy feedback, the config-diagnostic bar, and the state glyphs
//! shared by sidebar, navigator, and pane titles.

use crate::app::ControlState;
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, RowState, WorkspaceView};
use crate::ui::text::display_width_u16;
use crate::ui::widgets::panel_contrast_fg;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::Frame;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToastKind {
    Info,
    Warning,
    Error,
    Success,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Toast {
    pub kind: ToastKind,
    pub title: String,
    pub body: Option<String>,
    /// Roster row the toast points at, if any.
    pub target: Option<String>,
}

/// herdr `state_dot`: glyph plus colour for a roster row state.
/// Attention "●" red, Working "●" yellow, Unseen "●" teal, Idle "○" green,
/// Unknown "·" overlay0.
pub fn state_dot(state: RowState, p: &Palette) -> (&'static str, Color) {
    match state {
        RowState::Attention => ("●", p.red),
        RowState::Working => ("●", p.yellow),
        RowState::Unseen => ("●", p.teal),
        RowState::Idle => ("○", p.green),
        RowState::Unknown => ("·", p.overlay0),
    }
}

/// herdr `state_label`.
pub fn state_label(state: RowState) -> &'static str {
    match state {
        RowState::Attention => "blocked",
        RowState::Working => "working",
        RowState::Unseen => "done",
        RowState::Idle | RowState::Unknown => "idle",
    }
}

/// herdr `state_label_color`.
pub fn state_label_color(state: RowState, p: &Palette) -> Color {
    state_dot(state, p).1
}

/// Control-state indicator for a pane: glyph, label, colour. Gobby-specific;
/// colour is the fourth signal after glyph, label, and title position.
pub fn control_indicator(
    control: ControlState,
    take_back: bool,
    p: &Palette,
) -> (&'static str, &'static str, Color) {
    if take_back {
        return ("▲", "take-back", p.yellow);
    }
    match control {
        ControlState::Observe => ("○", "observe", p.subtext0),
        ControlState::Held => ("●", "held", p.accent),
        ControlState::LeaseLost => ("◌", "lease lost", p.red),
        ControlState::UncertainReadOnly => ("◌", "read-only", p.yellow),
    }
}

/// The colour-free half of `control_indicator`, for titles built without a
/// palette in hand.
pub fn control_glyph_label(control: ControlState, take_back: bool) -> (&'static str, &'static str) {
    if take_back {
        return ("▲", "take-back");
    }
    match control {
        ControlState::Observe => ("○", "observe"),
        ControlState::Held => ("●", "held"),
        ControlState::LeaseLost => ("◌", "lease lost"),
        ControlState::UncertainReadOnly => ("◌", "read-only"),
    }
}

fn toast_dot_color(kind: ToastKind, p: &Palette) -> Color {
    match kind {
        ToastKind::Info => p.blue,
        ToastKind::Warning => p.yellow,
        ToastKind::Error => p.red,
        ToastKind::Success => p.green,
    }
}

/// Status-line name for a non-terminal mode.
fn mode_name(mode: Mode) -> Option<&'static str> {
    Some(match mode {
        Mode::Terminal => return None,
        Mode::Navigate => "navigate",
        Mode::Prefix => "prefix",
        Mode::Copy => "copy",
        Mode::Resize => "resize",
        Mode::ConfirmClose => "confirm close",
        Mode::Rename => "rename",
        Mode::Respond => "respond",
        Mode::Settings => "settings",
        Mode::KeybindHelp => "keys",
        Mode::Navigator => "navigator",
    })
}

/// herdr `toast_notification_rect`, pinned to the bottom-right corner.
pub fn toast_notification_rect(area: Rect, toast: &Toast) -> Option<Rect> {
    if area.width == 0 || area.height == 0 {
        return None;
    }
    let body = toast.body.as_deref().unwrap_or("");
    let content_width = display_width_u16(&toast.title)
        .max(display_width_u16(body))
        .saturating_add(4);
    let width = content_width.saturating_add(2).min(area.width);
    let content_height = if body.is_empty() { 1 } else { 2 };
    let height = (content_height + 2).min(area.height);
    let x = area.x + area.width.saturating_sub(width);
    let y = area.y + area.height.saturating_sub(height);
    Some(Rect::new(x, y, width, height))
}

/// herdr `render_toast_notification`; returns the drawn rect as its hit area.
pub fn render_toast_notification(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Option<Rect> {
    let toast = chrome.toast.as_ref()?;
    let toast_area = toast_notification_rect(area, toast)?;
    let p = &chrome.palette;
    let dot_color = toast_dot_color(toast.kind, p);
    let body = toast.body.as_deref().unwrap_or("");

    frame.render_widget(Clear, toast_area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(p.overlay0))
        .style(Style::default().bg(p.panel_bg));
    let inner = block.inner(toast_area);
    frame.render_widget(block, toast_area);

    if inner.height < 1 {
        return Some(toast_area);
    }

    let [title_row, context_row] =
        Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas(inner);

    let title = Line::from(vec![
        Span::styled("●", Style::default().fg(dot_color)),
        Span::raw(" "),
        Span::styled(
            toast.title.as_str(),
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        ),
    ]);
    let context = Line::from(vec![
        Span::styled("  ", Style::default().fg(p.overlay0)),
        Span::styled(body, Style::default().fg(p.overlay0)),
    ]);

    frame.render_widget(Paragraph::new(title), title_row);
    if !body.is_empty() && inner.height >= 2 {
        frame.render_widget(Paragraph::new(context), context_row);
    }
    Some(toast_area)
}

/// herdr `copy_feedback_rect`, bottom-centre.
fn copy_feedback_rect(area: Rect, message: &str) -> Rect {
    if area.width == 0 || area.height == 0 {
        return Rect::default();
    }

    let content_width = message.len() as u16 + 4;
    let width = content_width.min(area.width);
    let height = 3u16.min(area.height);
    let x = area.x + area.width.saturating_sub(width) / 2;
    let y = area.y + area.height.saturating_sub(height);
    Rect::new(x, y, width, height)
}

/// herdr `render_copy_feedback`.
pub fn render_copy_feedback(frame: &mut Frame, area: Rect, chrome: &Chrome, message: &str) {
    let p = &chrome.palette;
    let feedback_area = copy_feedback_rect(area, message);
    if feedback_area.is_empty() {
        return;
    }

    frame.render_widget(Clear, feedback_area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(p.green))
        .style(Style::default().bg(p.panel_bg));
    let inner = block.inner(feedback_area);
    frame.render_widget(block, feedback_area);

    if inner.height == 0 {
        return;
    }

    let text = Line::from(vec![
        Span::styled("●", Style::default().fg(p.green).bg(p.panel_bg)),
        Span::raw(" "),
        Span::styled(
            message,
            Style::default()
                .fg(p.text)
                .bg(p.panel_bg)
                .add_modifier(Modifier::BOLD),
        ),
    ]);
    frame.render_widget(Paragraph::new(text), inner);
}

/// herdr `render_config_diagnostic`: a one-line warning bar, top right.
pub fn render_diagnostic(frame: &mut Frame, area: Rect, chrome: &Chrome, message: &str) {
    let p = &chrome.palette;
    let style = Style::default()
        .fg(panel_contrast_fg(p))
        .bg(p.yellow)
        .add_modifier(Modifier::BOLD);

    for (row, line) in message
        .lines()
        .filter(|line| !line.trim().is_empty())
        .take(area.height as usize)
        .enumerate()
    {
        let text = format!(" {line} ");
        let width = (text.len() as u16).min(area.width);
        let notif_area = Rect::new(
            area.x + area.width.saturating_sub(width),
            area.y + row as u16,
            width,
            1,
        );

        frame.render_widget(Clear, notif_area);
        frame.render_widget(Paragraph::new(Span::styled(text, style)), notif_area);
    }
}

/// Gobby status line: daemon reachability, focused pane control state, mode.
pub fn render_status_line<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let p = &chrome.palette;
    let base = Style::default().bg(p.surface_dim);
    let mut spans = Vec::new();

    match chrome.focused_pane().map(|id| ws.pane(id)) {
        Some(pane) => {
            let (glyph, label, color) = control_indicator(pane.control, pane.take_back, p);
            spans.push(Span::styled(
                format!(" {glyph} {label}"),
                base.fg(color).add_modifier(Modifier::BOLD),
            ));
            spans.push(Span::styled(
                format!(" │ {}", pane.terminal_id),
                base.fg(p.text),
            ));
        }
        None => spans.push(Span::styled(" no pane", base.fg(p.overlay1))),
    }
    if let Some(name) = mode_name(chrome.mode) {
        spans.push(Span::styled(format!(" │ {name}"), base.fg(p.accent)));
    }
    if !ws.daemon_ready() {
        spans.push(Span::styled(" │ daemon unreachable", base.fg(p.red)));
    }
    if let Some(message) = chrome.status_message.as_deref() {
        spans.push(Span::styled(format!(" │ {message}"), base.fg(p.subtext0)));
    }

    frame.render_widget(Paragraph::new(Line::from(spans)).style(base), area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use serde_json::json;

    fn screen(terminal: &Terminal<TestBackend>) -> String {
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect()
    }

    #[test]
    fn toast_rect_hugs_the_bottom_right_and_grows_with_a_body() {
        let area = Rect::new(0, 0, 80, 24);
        let mut toast = Toast {
            kind: ToastKind::Info,
            title: "term-alpha".to_string(),
            body: None,
            target: None,
        };
        assert_eq!(
            toast_notification_rect(area, &toast),
            Some(Rect::new(64, 21, 16, 3))
        );
        toast.body = Some("waiting on an answer".to_string());
        assert_eq!(
            toast_notification_rect(area, &toast),
            Some(Rect::new(54, 20, 26, 4))
        );
        assert!(toast_notification_rect(Rect::default(), &toast).is_none());
    }

    #[test]
    fn status_line_shows_control_state_terminal_and_mode() {
        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
        }));
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-alpha", "native", "epoch").unwrap();
        let mut chrome = Chrome::dark();
        chrome.open_pane(ws.pane_for_terminal("term-alpha").unwrap(), "alpha");
        chrome.mode = Mode::Navigate;
        chrome.status_message = Some("copied".to_string());

        let mut terminal = Terminal::new(TestBackend::new(80, 1)).unwrap();
        terminal
            .draw(|frame| render_status_line(frame, frame.area(), &ws, &chrome))
            .unwrap();
        let text = screen(&terminal);
        for needle in ["○ observe", "term-alpha", "navigate", "copied"] {
            assert!(text.contains(needle), "status lacks {needle:?}: {text}");
        }
        assert!(!text.contains('!'));
    }

    #[test]
    fn diagnostic_and_copy_feedback_stay_inside_the_area() {
        let chrome = Chrome::dark();
        let mut terminal = Terminal::new(TestBackend::new(40, 6)).unwrap();
        terminal
            .draw(|frame| {
                render_diagnostic(frame, frame.area(), &chrome, "keymap has an unknown action");
                render_copy_feedback(frame, frame.area(), &chrome, "copied 3 lines");
            })
            .unwrap();
        let text = screen(&terminal);
        assert!(text.contains("keymap has an unknown action"));
        assert!(text.contains("copied 3 lines"));
        assert_eq!(
            copy_feedback_rect(Rect::new(0, 0, 40, 6), "copied 3 lines"),
            Rect::new(11, 3, 18, 3)
        );
    }
}
