// upstream: none — native gclient daemon and product information dialogs.

use crate::ui::marks::{self, MarkPalette};
use crate::ui::widgets::{render_modal_header, render_modal_shell};
use crate::ui::Chrome;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use super::Dialog;

pub fn render_daemon(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Vec<Rect> {
    let Some(Dialog::Daemon {
        url,
        gclient_version,
        daemon_version,
        health,
        last_roster_refresh,
        stages,
    }) = &chrome.dialog
    else {
        return Vec::new();
    };
    let p = &chrome.palette;
    let Some(inner) = render_modal_shell(frame, area, 56, 11, p) else {
        return Vec::new();
    };
    if inner.width < 20 || inner.height < 8 {
        return Vec::new();
    }
    render_modal_header(
        frame,
        Rect::new(inner.x + 1, inner.y, inner.width - 2, 1),
        "Daemon",
        p,
    );
    let close = Rect::new(inner.right().saturating_sub(10), inner.y, 9, 1);
    frame.render_widget(
        Paragraph::new("esc Close").style(Style::default().fg(p.accent)),
        close,
    );
    let roster = last_roster_refresh
        .map(|age| format!("refreshed {} s ago", age.as_secs()))
        .unwrap_or_else(|| "not refreshed".to_owned());
    for (row, label, value) in [
        (1, "url", url.as_str()),
        (2, "gclient", gclient_version.as_str()),
        (3, "daemon", daemon_version.as_deref().unwrap_or("—")),
        (4, "health", health.as_str()),
        (5, "roster", roster.as_str()),
        (6, "startup", stages.as_deref().unwrap_or("—")),
    ] {
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(format!("{label:<9}"), Style::default().fg(p.subtext0)),
                Span::styled(value.to_owned(), Style::default().fg(p.text)),
            ])),
            Rect::new(inner.x + 2, inner.y + row, inner.width - 4, 1),
        );
    }
    vec![close]
}

pub fn render_about(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    url: &str,
    gclient_version: &str,
    daemon_version: Option<&str>,
    machine: &str,
) -> Vec<Rect> {
    let p = &chrome.palette;
    let Some(inner) = render_modal_shell(frame, area, 72, 15, p) else {
        return Vec::new();
    };
    if inner.width < 35 || inner.height < 10 {
        return Vec::new();
    }

    let show_mark = inner.width >= 68 && inner.height >= 13;
    if show_mark {
        marks::render_mark(
            frame,
            (inner.x + 1, inner.y),
            marks::goblin_small(),
            &MarkPalette::normal(p, chrome.prefs.monochrome),
        );
        // The 14-row asset reaches the panel's final row; keep its frame intact.
        for x in inner.x + 1..inner.x + 30 {
            if let Some(cell) = frame.buffer_mut().cell_mut((x, inner.bottom())) {
                cell.set_symbol("─").set_fg(p.accent).set_bg(p.panel_bg);
            }
        }
    }
    render_modal_header(
        frame,
        Rect::new(inner.x + 1, inner.y, inner.width - 2, 1),
        "About gobby",
        p,
    );
    let close = Rect::new(inner.right().saturating_sub(10), inner.y, 9, 1);
    frame.render_widget(
        Paragraph::new("esc Close").style(Style::default().fg(p.accent)),
        close,
    );

    let x = if show_mark { inner.x + 32 } else { inner.x + 2 };
    let width = inner.right().saturating_sub(x + 1);
    let text = |frame: &mut Frame, y: u16, line: Line<'static>| {
        if y < inner.bottom() {
            frame.render_widget(Paragraph::new(line), Rect::new(x, y, width, 1));
        }
    };
    text(
        frame,
        inner.y + 2,
        Line::from(Span::styled(
            "Gobby",
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        )),
    );
    text(
        frame,
        inner.y + 3,
        Line::from(Span::styled(
            "fleet management for AI coding agents",
            Style::default().fg(p.subtext0),
        )),
    );
    for (offset, label, value) in [
        (5, "gclient", gclient_version),
        (6, "daemon", daemon_version.unwrap_or("—")),
        (7, "url", url),
        (8, "machine", machine),
    ] {
        text(
            frame,
            inner.y + offset,
            Line::from(vec![
                Span::styled(format!("{label} "), Style::default().fg(p.subtext0)),
                Span::styled(value.to_owned(), Style::default().fg(p.text)),
            ]),
        );
    }
    text(
        frame,
        inner.y + 10,
        Line::from(Span::styled("gobby.ai", Style::default().fg(p.overlay0))),
    );
    vec![close]
}
