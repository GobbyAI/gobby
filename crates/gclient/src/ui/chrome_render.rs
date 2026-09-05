// upstream: herdr v0.8.0 src/ui.rs
//! Frame composition (herdr `render`): sidebar, tab bar, tab surface or
//! empty state, notifications, then the mode overlay.

use crate::app::PaneId;
use crate::ui::chrome::{Chrome, Mode, WorkspaceView};
use crate::ui::panes::PaneContent;
use crate::ui::{dialogs, keybind_help, navigator, panes, settings, sidebar, status, tab_surface};
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::widgets::Block;
use ratatui::Frame;

/// Compose the whole frame; `content` paints each pane's terminal grid.
pub fn render_workspace_with<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) {
    let area = frame.area();
    frame.render_widget(
        Block::new().style(Style::new().bg(chrome.palette.panel_bg)),
        area,
    );

    render_navigation_chrome(frame, ws, chrome);
    render_content_column(frame, ws, chrome, content);

    let status_rect = chrome.view.status_rect;
    if !status_rect.is_empty() {
        status::render_status_line(frame, status_rect, ws, chrome);
    }

    // Ambient notifications sit above panes, but below interactive overlays.
    render_notifications(frame, chrome);

    let terminal_area = chrome.view.terminal_area;
    let close_area = if terminal_area.is_empty() {
        area
    } else {
        terminal_area
    };
    match chrome.mode {
        Mode::ConfirmClose => render_dialog_overlay(frame, close_area, chrome),
        Mode::Rename | Mode::Respond => render_dialog_overlay(frame, area, chrome),
        Mode::Settings => {
            dim_background(frame, area);
            settings::render_settings(frame, area, chrome);
        }
        Mode::KeybindHelp => {
            dim_background(frame, area);
            keybind_help::render_keybind_help(frame, area, chrome);
        }
        Mode::Navigator => {
            dim_background(frame, area);
            navigator::render_navigator(frame, area, ws, chrome);
        }
        Mode::Terminal | Mode::Navigate | Mode::Prefix | Mode::Copy | Mode::Resize => {}
    }
}

/// Compose the whole frame with empty pane bodies.
pub fn render_workspace<W: WorkspaceView>(frame: &mut Frame, ws: &W, chrome: &Chrome) {
    let mut none = |_: &mut Frame, _: Rect, _: PaneId| {};
    render_workspace_with(frame, ws, chrome, &mut none);
}

/// herdr `render_navigation_chrome`: the sidebar column, collapsed or expanded.
/// Hit areas are returned by the sidebar; the run loop stores them.
fn render_navigation_chrome<W: WorkspaceView>(frame: &mut Frame, ws: &W, chrome: &Chrome) {
    let rect = chrome.view.sidebar_rect;
    if rect.width == 0 {
        return;
    }
    if chrome.sidebar.collapsed {
        sidebar::render_collapsed_sidebar(frame, rect, ws, chrome);
    } else {
        sidebar::render_sidebar(frame, rect, ws, chrome);
    }
}

/// Tab bar row plus terminal area: the active tab's surface, or the empty
/// state when no tab is open. Skipped when the terminal area has no cells.
fn render_content_column<W: WorkspaceView>(
    frame: &mut Frame,
    ws: &W,
    chrome: &Chrome,
    content: &mut PaneContent<'_>,
) {
    let terminal_area = chrome.view.terminal_area;
    if terminal_area.is_empty() {
        return;
    }
    if chrome.tabs.is_empty() {
        panes::render_empty(frame, terminal_area, chrome);
        return;
    }
    let surface = chrome
        .view
        .tab_bar_rect
        .map_or(terminal_area, |tabs| tabs.union(terminal_area));
    tab_surface::render_tab_surface(frame, surface, ws, chrome, content);
}

/// herdr `render_notifications`: diagnostic bar, then the toast, both over
/// the whole frame.
fn render_notifications(frame: &mut Frame, chrome: &Chrome) {
    let area = frame.area();
    if let Some(message) = &chrome.status_message {
        status::render_diagnostic(frame, area, chrome, message);
    }
    if chrome.toast.is_some() {
        status::render_toast_notification(frame, area, chrome);
    }
}

/// Dim `area` and draw the pending dialog over it; nothing when no dialog
/// is set.
fn render_dialog_overlay(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    if chrome.dialog.is_none() {
        return;
    }
    dim_background(frame, area);
    dialogs::render_dialog(frame, area, chrome);
}

/// herdr `dim_background`: DIM modifier over `area`.
pub fn dim_background(frame: &mut Frame, area: Rect) {
    let buf = frame.buffer_mut();
    let area = area.intersection(buf.area);
    for y in area.y..area.y + area.height {
        for x in area.x..area.x + area.width {
            let cell = &mut buf[(x, y)];
            cell.set_style(cell.style().add_modifier(Modifier::DIM));
        }
    }
}

/// herdr `copy_feedback_offset_for_toast`: rows the copy-feedback box lifts
/// by so it clears a toast it would otherwise overlap; `base_offset` when the
/// two rects are apart.
pub fn copy_feedback_offset_for_toast(feedback_rect: Rect, base_offset: u16, toast_rect: Rect) -> u16 {
    if rects_overlap(feedback_rect, toast_rect) {
        base_offset.saturating_add(toast_rect.height)
    } else {
        base_offset
    }
}

/// herdr `rects_overlap`.
pub fn rects_overlap(a: Rect, b: Rect) -> bool {
    a.x < b.x.saturating_add(b.width)
        && b.x < a.x.saturating_add(a.width)
        && a.y < b.y.saturating_add(b.height)
        && b.y < a.y.saturating_add(a.height)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    #[test]
    fn rects_overlap_cases() {
        let a = Rect::new(0, 0, 4, 4);
        assert!(rects_overlap(a, a));
        assert!(rects_overlap(a, Rect::new(2, 2, 4, 4)));
        assert!(rects_overlap(a, Rect::new(3, 3, 1, 1)));
        assert!(
            !rects_overlap(a, Rect::new(4, 0, 2, 2)),
            "edge-adjacent on x"
        );
        assert!(
            !rects_overlap(a, Rect::new(0, 4, 2, 2)),
            "edge-adjacent on y"
        );
        assert!(!rects_overlap(a, Rect::new(10, 10, 1, 1)));
        // herdr semantics: a zero-size rect strictly inside another overlaps
        // it; two zero-size rects never do.
        assert!(rects_overlap(a, Rect::new(1, 1, 0, 0)));
        assert!(!rects_overlap(Rect::new(0, 0, 0, 0), Rect::new(0, 0, 0, 0)));
        assert!(!rects_overlap(a, Rect::new(4, 4, 0, 0)));
    }

    #[test]
    fn dim_background_marks_every_cell_in_area() {
        let mut terminal = Terminal::new(TestBackend::new(6, 4)).unwrap();
        let area = Rect::new(1, 1, 3, 2);
        terminal.draw(|frame| dim_background(frame, area)).unwrap();
        let buffer = terminal.backend().buffer();
        for y in 0..4 {
            for x in 0..6 {
                let dimmed = buffer[(x, y)].modifier.contains(Modifier::DIM);
                let inside = rects_overlap(area, Rect::new(x, y, 1, 1));
                assert_eq!(dimmed, inside, "cell ({x}, {y})");
            }
        }
    }

    #[test]
    fn dim_background_clips_to_the_frame() {
        let mut terminal = Terminal::new(TestBackend::new(4, 3)).unwrap();
        terminal
            .draw(|frame| dim_background(frame, Rect::new(2, 1, 50, 50)))
            .unwrap();
        let buffer = terminal.backend().buffer();
        assert!(buffer[(3, 2)].modifier.contains(Modifier::DIM));
        assert!(!buffer[(0, 0)].modifier.contains(Modifier::DIM));
    }
}
