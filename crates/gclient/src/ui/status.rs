// upstream: herdr v0.8.0 src/ui/status.rs
//! Toasts, copy feedback, and the state glyphs shared by sidebar,
//! navigator, and pane titles.

use std::time::{Duration, Instant};

use crate::app::{ControlState, Pane};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, RowState, WorkspaceView};
use crate::ui::hit::Hit;
use crate::ui::pane_chrome::{metadata_rect, pane_metadata, pane_title};
use crate::ui::text::display_width_u16;
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

/// How long a toast stays up before the render tick drops it.
pub const TOAST_TTL: Duration = Duration::from_secs(6);
/// Most toasts shown at once; the oldest leaves when one more arrives.
pub const TOAST_STACK: usize = 3;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Toast {
    pub kind: ToastKind,
    pub title: String,
    /// Second row: the affected pane by label, or the detail behind the title.
    pub body: Option<String>,
    /// Roster row the toast points at, if any.
    pub target: Option<String>,
}

impl Toast {
    fn new(kind: ToastKind, title: impl Into<String>) -> Self {
        Self {
            kind,
            title: title.into(),
            body: None,
            target: None,
        }
    }

    pub fn info(title: impl Into<String>) -> Self {
        Self::new(ToastKind::Info, title)
    }

    pub fn warning(title: impl Into<String>) -> Self {
        Self::new(ToastKind::Warning, title)
    }

    pub fn error(title: impl Into<String>) -> Self {
        Self::new(ToastKind::Error, title)
    }

    pub fn success(title: impl Into<String>) -> Self {
        Self::new(ToastKind::Success, title)
    }

    pub fn with_body(mut self, body: impl Into<String>) -> Self {
        self.body = Some(body.into());
        self
    }
}

/// A toast on screen and when it went up; `Chrome::expire_toasts` reads the
/// clock against [`TOAST_TTL`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActiveToast {
    pub toast: Toast,
    pub shown_at: Instant,
}

/// Glyph plus colour for a roster row state, one shape per state so the
/// rows read without colour: `▶` working (accent), `⍾` needs you (warning),
/// `◆` unseen output (info), `○` idle, `◌` orphaned (destructive), `·`
/// unknown.
pub fn state_dot(state: RowState, p: &Palette) -> (&'static str, Color) {
    match state {
        RowState::Attention => ("⍾", p.peach),
        RowState::Orphaned => ("◌", p.red),
        // U+2016, one cell in a mono face; the emoji pause would not be.
        RowState::Paused => ("‖", p.yellow),
        RowState::Working => ("▶", p.accent),
        RowState::Unseen => ("◆", p.teal),
        RowState::Idle => ("○", p.overlay0),
        RowState::Unknown => ("·", p.overlay0),
    }
}

/// herdr `state_label`: the word beside a row's glyph where a surface
/// spells the state out.
pub fn state_label(state: RowState) -> &'static str {
    match state {
        RowState::Attention => "needs you",
        RowState::Orphaned => "orphaned",
        RowState::Paused => "paused",
        RowState::Working => "working",
        RowState::Unseen => "unseen",
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

/// Kind cue for a toast: glyph plus label, so Info, Warning, Error, and
/// Success stay apart with colour stripped. Gobby-specific; colour is the
/// fourth signal after glyph, label, and title position.
pub fn toast_cue(kind: ToastKind) -> (&'static str, &'static str) {
    match kind {
        ToastKind::Info => ("◇", "info"),
        ToastKind::Warning => ("△", "warning"),
        ToastKind::Error => ("×", "error"),
        ToastKind::Success => ("✓", "success"),
    }
}

/// Cells the cue occupies ahead of the title: glyph, space, label, two spaces.
pub fn toast_cue_width(kind: ToastKind) -> u16 {
    let (glyph, label) = toast_cue(kind);
    display_width_u16(glyph)
        .saturating_add(display_width_u16(label))
        .saturating_add(3)
}

fn toast_cue_color(kind: ToastKind, p: &Palette) -> Color {
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
        Mode::ContextMenu => "menu",
        Mode::ProjectDialog => "project",
    })
}

/// Size of one toast: the cue, title, body and borders.
fn toast_size(toast: &Toast) -> (u16, u16) {
    let body = toast.body.as_deref().unwrap_or("");
    let content_width = display_width_u16(&toast.title)
        .saturating_add(toast_cue_width(toast.kind))
        .max(display_width_u16(body).saturating_add(2))
        .saturating_add(2);
    let content_height = if body.is_empty() { 1 } else { 2 };
    (content_width.saturating_add(2), content_height + 2)
}

/// herdr `toast_notification_rect`, moved to the top-right corner of `area`
/// (the pane area, D3): the rect of a lone toast.
pub fn toast_notification_rect(area: Rect, toast: &Toast) -> Option<Rect> {
    toast_stack_rects(area, [toast]).pop()
}

/// Rects for `toasts` stacked downward from the top-right corner of `area`,
/// oldest first; toasts the area cannot fit are left out.
pub fn toast_stack_rects<'a>(area: Rect, toasts: impl IntoIterator<Item = &'a Toast>) -> Vec<Rect> {
    let bottom = area.y.saturating_add(area.height);
    let mut rects = Vec::new();
    let mut top = area.y;
    for toast in toasts {
        let (width, height) = toast_size(toast);
        let width = width.min(area.width);
        if width == 0 || height == 0 || top.saturating_add(height) > bottom {
            break;
        }
        let x = area.x + area.width.saturating_sub(width);
        rects.push(Rect::new(x, top, width, height));
        top = top.saturating_add(height);
    }
    rects
}

/// herdr `render_toast_notification` over the stack: every active toast is
/// drawn top-down; returns the union of the drawn rects as the hit area.
pub fn render_toast_notification(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Option<Rect> {
    let toasts = chrome.toasts.iter().map(|active| &active.toast);
    let rects = toast_stack_rects(area, toasts.clone());
    let mut union: Option<Rect> = None;
    for (toast, rect) in toasts.zip(rects) {
        render_one_toast(frame, rect, toast, &chrome.palette);
        union = Some(union.map_or(rect, |acc| acc.union(rect)));
    }
    union
}

fn render_one_toast(frame: &mut Frame, toast_area: Rect, toast: &Toast, p: &Palette) {
    let (glyph, label) = toast_cue(toast.kind);
    let cue_color = toast_cue_color(toast.kind, p);
    let body = toast.body.as_deref().unwrap_or("");

    frame.render_widget(Clear, toast_area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(p.overlay0))
        .style(Style::default().bg(p.panel_bg));
    let inner = block.inner(toast_area);
    frame.render_widget(block, toast_area);

    if inner.height < 1 {
        return;
    }

    let [title_row, context_row] =
        Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas(inner);

    let title = Line::from(vec![
        Span::styled(glyph, Style::default().fg(cue_color)),
        Span::raw(" "),
        Span::styled(label, Style::default().fg(cue_color)),
        Span::raw("  "),
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
}

/// herdr `copy_feedback_rect`, bottom-centre.
pub fn copy_feedback_rect(area: Rect, message: &str) -> Rect {
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

/// Global status line: prefix, mode, and daemon reachability. A pane's
/// title and metadata live on its edges. What the focused pane's edges
/// cannot carry falls here instead: its metadata leads this line when no
/// edge has room for it, and its title ends the line when the pane has no
/// border at all (a lone pane, or borders off), the segment that gives way
/// when width runs out.
///
/// Returns the metadata's cells while they offer take-control (Read-only,
/// Uncertain): a button `pointer::down` dispatches, underlined while the
/// pointer rests on it. Focus alone is a condition, not a button.
pub fn render_status_line<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) -> Option<Rect> {
    if area.width == 0 || area.height == 0 {
        return None;
    }
    let p = &chrome.palette;
    let base = Style::default().bg(p.surface_dim);
    let mut spans = vec![Span::styled(" ", base)];
    let mut indicator = None;
    let overflow = focused_overflow(ws, chrome);
    if let Some((pane, _)) = overflow {
        let meta = pane_metadata(pane, true);
        let mut style = base.fg(meta.tone.color(p)).add_modifier(Modifier::BOLD);
        if meta.actionable {
            let width = display_width_u16(&meta.text).saturating_add(1);
            indicator = Some(Rect::new(area.x, area.y, width.min(area.width), 1));
            if matches!(chrome.hover, Some(Hit::ControlIndicator)) {
                style = style.add_modifier(Modifier::UNDERLINED);
            }
        }
        spans.push(Span::styled(meta.text, style));
        spans.push(Span::styled(" │ ", base.fg(p.subtext0)));
    }
    // The prefix is the way into every chord, quit included, so the status
    // line always names it; under an outer tmux it is the shifted chord.
    spans.push(Span::styled(
        format!("prefix {}", chrome.keymap.prefix_label),
        base.fg(p.subtext0),
    ));
    if let Some(name) = mode_name(chrome.mode) {
        spans.push(Span::styled(format!(" │ {name}"), base.fg(p.accent)));
    }
    // A condition, not an event: it stays until the daemon is back.
    if !ws.daemon_ready() {
        spans.push(Span::styled(" │ Daemon unreachable.", base.fg(p.red)));
    }
    if let Some((pane, true)) = overflow {
        spans.push(Span::styled(
            format!(" │ {}", pane_title(ws, pane)),
            base.fg(p.text),
        ));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)).style(base), area);
    indicator
}

/// The focused pane when its edges cannot place its metadata, paired with
/// whether it has no border for its title either.
fn focused_overflow<'a, W: WorkspaceView>(ws: &'a W, chrome: &Chrome) -> Option<(&'a Pane, bool)> {
    let pane = ws.pane(chrome.focused_pane()?);
    let bordered = chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.is_focused && !info.borders.is_empty());
    match bordered {
        None => Some((pane, true)),
        Some(info) => metadata_rect(info, &pane_metadata(pane, true))
            .is_none()
            .then_some((pane, false)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crate::ui::keymap::{default_prefix, Keymap};
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
    fn toast_rect_hugs_the_top_right_and_grows_with_a_body() {
        let area = Rect::new(0, 0, 80, 24);
        let mut toast = Toast::info("term-alpha");
        // "◇ info  " (8 cells) leads the 10-cell title, plus padding and borders.
        assert_eq!(
            toast_notification_rect(area, &toast),
            Some(Rect::new(58, 0, 22, 3))
        );
        toast = toast.with_body("waiting on an answer");
        assert_eq!(
            toast_notification_rect(area, &toast),
            Some(Rect::new(54, 0, 26, 4))
        );
        assert!(toast_notification_rect(Rect::default(), &toast).is_none());
    }

    #[test]
    fn toast_stack_grows_downward_and_stops_at_the_area_bottom() {
        let area = Rect::new(10, 2, 60, 7);
        let toasts = [
            Toast::info("one"),
            Toast::warning("two").with_body("term-alpha"),
            Toast::error("three"),
        ];
        let rects = toast_stack_rects(area, &toasts);
        assert_eq!(rects.len(), 2, "the third toast does not fit: {rects:?}");
        assert_eq!(rects[0].y, 2);
        assert_eq!(rects[1].y, 5);
        assert!(rects.iter().all(|rect| rect.x + rect.width == 70));
    }

    fn draw_status(ws: &Workspace, chrome: &Chrome) -> (String, Option<Rect>) {
        let mut terminal = Terminal::new(TestBackend::new(80, 1)).unwrap();
        let mut indicator = None;
        terminal
            .draw(|frame| indicator = render_status_line(frame, frame.area(), ws, chrome))
            .unwrap();
        (screen(&terminal), indicator)
    }

    #[test]
    fn status_line_shows_only_global_prefix_mode_and_health() {
        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
        }));
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-alpha", "native", "epoch").unwrap();
        ws.open_terminal("term-beta", "tmux", "epoch").unwrap();
        let mut chrome = Chrome::dark();
        chrome.open_pane(ws.pane_for_terminal("term-alpha").unwrap(), "alpha");
        chrome.open_pane(ws.pane_for_terminal("term-beta").unwrap(), "alpha");
        chrome.compute_view(&ws, Rect::new(0, 0, 120, 20));
        chrome.mode = Mode::Navigate;

        // The focused pane's border carries its title and metadata.
        let (text, indicator) = draw_status(&ws, &chrome);
        assert_eq!(text.trim_end(), " prefix ctrl+b │ navigate");
        assert_eq!(indicator, None);
        for pane_local in ["observe", "tmux", "Focused", "term-beta"] {
            assert!(
                !text.contains(pane_local),
                "status duplicates {pane_local:?}: {text}"
            );
        }

        // Under an outer tmux the shifted prefix is named instead.
        chrome.nested = true;
        chrome.keymap = Keymap::defaults(default_prefix(true));
        let (text, _) = draw_status(&ws, &chrome);
        assert!(
            text.contains("prefix ctrl+]"),
            "status lacks the prefix cue: {text}"
        );
    }

    #[test]
    fn borderless_focused_pane_keeps_its_metadata_and_title_here() {
        for (backend, name) in [("native", "gclient"), ("tmux", "tmux")] {
            let mut ws = Workspace::scripted();
            ws.daemon_mut().set_roster(json!({
                "epoch": "e1",
                "seq": 1,
                "entries": []
            }));
            ws.reconcile_subscribe_first().unwrap();
            ws.open_terminal("term-alpha", backend, "epoch").unwrap();
            let id = ws.pane_for_terminal("term-alpha").unwrap();
            let mut chrome = Chrome::dark();
            chrome.open_pane(id, "alpha");
            // A lone pane draws no border, so nothing else shows these.
            chrome.compute_view(&ws, Rect::new(0, 0, 80, 20));
            assert!(chrome.view.pane_infos[0].borders.is_empty());

            let (text, indicator) = draw_status(&ws, &chrome);
            assert_eq!(
                text.trim_end(),
                format!(" {name} · Focused │ prefix ctrl+b │ term-alpha")
            );
            assert_eq!(indicator, None, "focus is not a button");

            // An exception is the one actionable state: its words are the
            // button, and the pointer resting there underlines them.
            ws.pane_mut(id).control = ControlState::LeaseLost;
            ws.pane_mut(id).take_back = true;
            chrome.hover = Some(Hit::ControlIndicator);
            let mut terminal = Terminal::new(TestBackend::new(80, 1)).unwrap();
            let mut indicator = None;
            terminal
                .draw(|frame| {
                    indicator = render_status_line(frame, frame.area(), &ws, &chrome);
                })
                .unwrap();
            let text = screen(&terminal);
            let label = format!("{name} · Read-only");
            assert!(text.starts_with(&format!(" {label} │ prefix")), "{text}");
            let width = display_width_u16(&label) + 1;
            assert_eq!(indicator, Some(Rect::new(0, 0, width, 1)));
            let buffer = terminal.backend().buffer();
            assert!(buffer[(1, 0)].modifier.contains(Modifier::UNDERLINED));
            assert!(!buffer[(width + 1, 0)]
                .modifier
                .contains(Modifier::UNDERLINED));
        }
    }

    #[test]
    fn metadata_too_wide_for_its_pane_edge_lands_here_without_the_title() {
        let mut ws = Workspace::scripted();
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": []
        }));
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-alpha", "native", "epoch").unwrap();
        ws.open_terminal("term-beta", "native", "epoch").unwrap();
        let mut chrome = Chrome::dark();
        chrome.open_pane(ws.pane_for_terminal("term-alpha").unwrap(), "alpha");
        chrome.open_pane(ws.pane_for_terminal("term-beta").unwrap(), "alpha");
        chrome.compute_view(&ws, Rect::new(0, 0, 60, 20));
        let focused = chrome.focused_pane().unwrap();
        let info = chrome.view.pane_infos.iter().find(|info| info.is_focused);
        let info = info.unwrap();
        // Bordered, but narrower than its padded metadata.
        assert!(!info.borders.is_empty());
        let meta = pane_metadata(ws.pane(focused), true);
        assert_eq!(metadata_rect(info, &meta), None, "{:?}", info.rect);

        // The title keeps the pane's top edge; only the metadata moves.
        let (text, indicator) = draw_status(&ws, &chrome);
        assert_eq!(text.trim_end(), " gclient · Focused │ prefix ctrl+b");
        assert_eq!(indicator, None);

        // An exception with no room on the edge is still a button, here.
        ws.pane_mut(focused).control = ControlState::LeaseLost;
        let (text, indicator) = draw_status(&ws, &chrome);
        assert_eq!(text.trim_end(), " gclient · Read-only │ prefix ctrl+b");
        let width = display_width_u16("gclient · Read-only") + 1;
        assert_eq!(indicator, Some(Rect::new(0, 0, width, 1)));
        assert_eq!(
            crate::ui::pane_chrome::control_indicator_hit_area(&ws, &chrome),
            None,
            "the edge offers no second button"
        );
    }

    #[test]
    fn copy_feedback_stays_inside_the_area() {
        let chrome = Chrome::dark();
        let mut terminal = Terminal::new(TestBackend::new(40, 6)).unwrap();
        terminal
            .draw(|frame| {
                render_copy_feedback(frame, frame.area(), &chrome, "copied 3 lines");
            })
            .unwrap();
        let text = screen(&terminal);
        assert!(text.contains("copied 3 lines"));
        assert_eq!(
            copy_feedback_rect(Rect::new(0, 0, 40, 6), "copied 3 lines"),
            Rect::new(11, 3, 18, 3)
        );
    }
}
