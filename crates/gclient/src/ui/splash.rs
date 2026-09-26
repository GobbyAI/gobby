// upstream: none (native Gobby connection splash)
//! Native gclient connection splash.

use ratatui::layout::{Alignment, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;

use crate::app::startup_stages::{StageState, StartupStage};

use super::marks::{self, MarkPalette};
use super::Chrome;

const FULL_WIDTH: u16 = 91;
const WORDMARK_WIDTH: u16 = 54;
const FULL_HEIGHT: u16 = 22;
const WORDMARK_HEIGHT: u16 = 16;
const STAGE_HEIGHT: u16 = 4;

pub fn render_splash(frame: &mut Frame, area: Rect, chrome: &Chrome) {
    if area.is_empty() {
        return;
    }
    let p = &chrome.palette;
    frame.render_widget(Block::default().style(Style::new().bg(p.panel_bg)), area);
    let Some(stages) = chrome.connection.stages.as_ref() else {
        return;
    };

    let show_wordmark = area.width >= WORDMARK_WIDTH && area.height >= WORDMARK_HEIGHT;
    let show_goblin = area.width >= FULL_WIDTH && area.height >= FULL_HEIGHT;
    let group_width = if show_goblin {
        FULL_WIDTH
    } else if show_wordmark {
        WORDMARK_WIDTH
    } else {
        area.width.min(WORDMARK_WIDTH)
    };
    let group_height = if show_goblin {
        FULL_HEIGHT
    } else if show_wordmark {
        WORDMARK_HEIGHT
    } else {
        area.height.min(STAGE_HEIGHT + 2)
    };
    let group_x = area.x + (area.width - group_width) / 2;
    let group_y = area.y + (area.height - group_height) / 2;
    let column_x = group_x + if show_goblin { 37 } else { 0 };
    let column_width = group_width - if show_goblin { 37 } else { 0 };

    if show_goblin {
        marks::render_mark(
            frame,
            (group_x, group_y),
            marks::goblin_large(),
            &MarkPalette::normal(p),
        );
    }
    let stage_y = if show_wordmark {
        marks::render_mark(
            frame,
            (column_x, group_y),
            marks::wordmark(),
            &MarkPalette::normal(p),
        );
        let version = format!(
            "gclient {} · daemon {} · {}",
            env!("CARGO_PKG_VERSION"),
            chrome.connection.daemon_version.as_deref().unwrap_or("—"),
            chrome.connection.machine,
        );
        frame.render_widget(
            Paragraph::new(version).style(Style::new().fg(p.text)),
            Rect::new(column_x, group_y + 8, column_width, 1),
        );
        group_y + 10
    } else {
        group_y
    };

    for (index, stage) in StartupStage::ALL.into_iter().enumerate() {
        let y = stage_y + index as u16;
        if y >= area.bottom() {
            break;
        }
        let (glyph, color, label_color, timing) = match stages.state(stage) {
            StageState::Pending => ("○", p.overlay0, p.overlay0, String::new()),
            StageState::Running { .. } => (
                "◐",
                p.accent,
                p.text,
                format!(
                    "{:.1} s and waiting",
                    stages.elapsed(stage).unwrap_or_default().as_secs_f64()
                ),
            ),
            StageState::Done { took } => (
                "●",
                p.accent,
                p.text,
                format!("{:.1} s", took.as_secs_f64()),
            ),
        };
        let timing_width = timing.chars().count().min(column_width as usize) as u16;
        let label_width = column_width.saturating_sub(timing_width);
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(glyph, Style::new().fg(color)),
                Span::styled(
                    format!(" {}", stage.label()),
                    Style::new().fg(label_color).add_modifier(Modifier::BOLD),
                ),
            ])),
            Rect::new(column_x, y, label_width, 1),
        );
        if timing_width > 0 {
            frame.render_widget(
                Paragraph::new(timing)
                    .style(Style::new().fg(p.overlay1))
                    .alignment(Alignment::Right),
                Rect::new(column_x + label_width, y, timing_width, 1),
            );
        }
    }

    let note_y = if show_goblin {
        group_y + FULL_HEIGHT - 1
    } else if show_wordmark {
        group_y + 15
    } else {
        stage_y + STAGE_HEIGHT + 1
    };
    if note_y < area.bottom() {
        let note = format!(
            "Connecting to {} · menus work now · the status bar names the stage that stalls",
            chrome.connection.url,
        );
        frame.render_widget(
            Paragraph::new(note).style(Style::new().fg(p.overlay0)),
            Rect::new(group_x, note_y, area.right() - group_x, 1),
        );
    }
}
