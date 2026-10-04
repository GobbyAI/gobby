// upstream: none — native gclient daemon and product information dialogs.

use crate::ui::marks::{self, MarkPalette};
use crate::ui::widgets::{render_modal_header, render_modal_shell};
use crate::ui::Chrome;
use ratatui::layout::Rect;
use ratatui::style::Style;
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
    let (goblin, wordmark) = (marks::goblin_large(), marks::wordmark());
    // A pad, the goblin, a gap, the wordmark and a pad wide; the header row,
    // the goblin's rows and one blank row above the bottom border tall.
    let (cols, rows) = (1 + goblin.cols + 2 + wordmark.cols + 1, 1 + goblin.rows + 1);
    let Some(inner) = render_modal_shell(frame, area, cols + 2, rows + 2, p) else {
        return Vec::new();
    };
    if inner.width < 35 || inner.height < 10 {
        return Vec::new();
    }

    // Short of the full panel, the goblin goes first, then the wordmark.
    let tall = inner.height >= rows;
    let show_goblin = tall && inner.width >= cols;
    let x = if show_goblin {
        inner.x + 1 + goblin.cols + 2
    } else {
        inner.x + 2
    };
    // The wordmark keeps a one-column pad before the right border.
    let show_wordmark = tall && inner.right() > x + wordmark.cols;
    let palette = MarkPalette::normal(p, chrome.prefs.monochrome);
    if show_goblin {
        marks::render_mark(frame, (inner.x + 1, inner.y + 1), goblin, &palette);
    }
    if show_wordmark {
        marks::render_mark(frame, (x, inner.y + 2), wordmark, &palette);
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

    let width = inner.right().saturating_sub(x + 1);
    let text = |frame: &mut Frame, y: u16, line: Line<'static>| {
        if y < inner.bottom() {
            frame.render_widget(Paragraph::new(line), Rect::new(x, y, width, 1));
        }
    };
    // The text starts one blank row under the wordmark, or under the header
    // when the wordmark is dropped.
    let top = if show_wordmark {
        inner.y + 2 + wordmark.rows + 1
    } else {
        inner.y + 2
    };
    text(
        frame,
        top,
        Line::from(Span::styled(
            "Fleet management for AI agents",
            Style::default().fg(p.subtext0),
        )),
    );
    for (offset, label, value) in [
        (2, "gclient", gclient_version),
        (3, "daemon", daemon_version.unwrap_or("—")),
        (4, "url", url),
        (5, "machine", machine),
    ] {
        text(
            frame,
            top + offset,
            Line::from(vec![
                Span::styled(format!("{label:<7} "), Style::default().fg(p.subtext0)),
                Span::styled(value.to_owned(), Style::default().fg(p.text)),
            ]),
        );
    }
    text(
        frame,
        top + 7,
        Line::from(Span::styled("gobby.ai", Style::default().fg(p.overlay0))),
    );
    vec![close]
}
