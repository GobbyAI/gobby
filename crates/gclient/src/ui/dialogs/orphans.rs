// upstream: none (Gobby-only; shares the shell and destructive token with dialogs/project.rs)
//! The destroy-orphaned-terminals dialog: a checklist of the rows the daemon
//! can only clean up, every candidate pre-checked, with a destructive
//! confirm button in the same token the remove-worktree dialog uses.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::ui::chrome::Chrome;
use crate::ui::widgets::{
    action_button_row_rects, modal_choice_rows, render_action_button, render_modal_description,
    render_modal_header, render_modal_shell, ActionButtonSpec,
};

use super::{primary_button_style, secondary_button_style, OrphanRow};

pub const DESTROY_ORPHANS_TITLE: &str = "destroy orphaned terminals";
const POPUP_WIDTH: u16 = 72;
/// Rows besides the candidates: header, gap, gap, key hints, buttons, and
/// the two border rows.
const BASE_HEIGHT: u16 = 7;

pub fn render_destroy_orphans(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    rows: &[OrphanRow],
    checked: &[bool],
    selected: usize,
) {
    let p = &chrome.palette;
    let list_rows = rows.len().clamp(1, usize::from(u16::MAX)) as u16;
    let Some(inner) = render_modal_shell(frame, area, POPUP_WIDTH, BASE_HEIGHT + list_rows, p)
    else {
        return;
    };
    if inner.height < 5 {
        return;
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
    render_modal_header(frame, areas[0], DESTROY_ORPHANS_TITLE, p);
    if rows.is_empty() {
        render_modal_description(
            frame,
            areas[2],
            "No orphaned terminals.",
            Style::default().fg(p.subtext0),
        );
    }
    for (index, (row, rect)) in rows
        .iter()
        .zip(modal_choice_rows(areas[2], rows.len(), 1))
        .enumerate()
    {
        let is_checked = checked.get(index).copied().unwrap_or(false);
        let is_selected = index == selected;
        let marker = if is_selected { "▸" } else { " " };
        let tick = if is_checked { "[x]" } else { "[ ]" };
        let style = if is_selected {
            Style::default()
                .bg(p.surface0)
                .fg(p.text)
                .add_modifier(Modifier::BOLD)
        } else if is_checked {
            Style::default().fg(p.text)
        } else {
            Style::default().fg(p.subtext0)
        };
        frame.render_widget(
            Paragraph::new(format!(" {marker} {tick} {}", row_summary(row))).style(style),
            rect,
        );
    }
    render_modal_description(
        frame,
        areas[4],
        "space toggle · a all/none",
        Style::default().fg(p.overlay0),
    );
    let count = checked.iter().filter(|flag| **flag).count();
    let destroy = format!("destroy {count}");
    let specs = [
        ActionButtonSpec {
            hint: Some("↵"),
            label: &destroy,
        },
        ActionButtonSpec {
            hint: Some("esc"),
            label: "cancel",
        },
    ];
    let rects = action_button_row_rects(inner, &specs, 2, inner.height.saturating_sub(1));
    if let [destroy_rect, cancel_rect] = rects[..] {
        render_action_button(
            frame,
            destroy_rect,
            Some("↵"),
            &destroy,
            primary_button_style(chrome, p.red),
        );
        render_action_button(
            frame,
            cancel_rect,
            Some("esc"),
            "cancel",
            secondary_button_style(chrome),
        );
    }
}

/// `name · backend · owner-or-"no session" · last seen HH:MM`.
pub fn row_summary(row: &OrphanRow) -> String {
    let owner = row.owner.as_deref().unwrap_or("no session");
    match &row.last_seen {
        Some(seen) => format!(
            "{} · {} · {owner} · last seen {seen}",
            row.name, row.backend
        ),
        None => format!("{} · {} · {owner}", row.name, row.backend),
    }
}
