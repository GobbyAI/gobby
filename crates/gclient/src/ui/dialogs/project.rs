// upstream: herdr v0.8.0 src/client/shell/overlay_input.rs
//! The project dialogs: new project (herdr's "new workspace" overlay), new
//! worktree, open worktree, and remove worktree (herdr's
//! `render_worktree_remove_overlay`).

use std::fs;
use std::path::Path;

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{Clear, Paragraph};
use ratatui::Frame;

use crate::ui::chrome::Chrome;
use crate::ui::widgets::{
    action_button_row_rects, centered_popup_rect, modal_choice_rows, render_action_button,
    render_modal_description, render_modal_header, render_modal_shell, render_panel_shell,
    ActionButtonSpec,
};

use super::{input_line, primary_button_style, secondary_button_style, WorktreeChoice};

/// herdr's overlay title for registering a checkout.
pub const NEW_PROJECT_TITLE: &str = "new workspace";
const POPUP_WIDTH: u16 = 64;
const NEW_PROJECT_HEIGHT: u16 = 8;
const NEW_WORKTREE_HEIGHT: u16 = 10;
/// Open-worktree rows besides the choices: header, gap, buttons, borders.
const OPEN_WORKTREE_BASE_HEIGHT: u16 = 6;
const REMOVE_WORKTREE_HEIGHT: u16 = 8;

fn input(frame: &mut Frame, rect: Rect, chrome: &Chrome, value: &str, cursor: Option<usize>) {
    let p = &chrome.palette;
    frame.render_widget(Clear, rect);
    let line = match cursor {
        Some(cursor) => input_line(value, cursor),
        None => Line::from(format!(" {value}")),
    };
    frame.render_widget(
        Paragraph::new(line).style(Style::default().fg(p.text).bg(p.surface0)),
        rect,
    );
}

fn error_line(frame: &mut Frame, rect: Rect, chrome: &Chrome, error: Option<&str>) {
    if let Some(error) = error {
        render_modal_description(frame, rect, error, Style::default().fg(chrome.palette.red));
    }
}

fn buttons(frame: &mut Frame, inner: Rect, chrome: &Chrome, primary: &str, hints: &[&str]) {
    let p = &chrome.palette;
    let mut specs = vec![ActionButtonSpec {
        hint: Some("↵"),
        label: primary,
    }];
    specs.extend(hints.iter().map(|hint| ActionButtonSpec {
        hint: Some(hint),
        label: "complete",
    }));
    specs.push(ActionButtonSpec {
        hint: Some("esc"),
        label: "cancel",
    });
    let rects = action_button_row_rects(inner, &specs, 2, inner.height.saturating_sub(1));
    for (index, (spec, rect)) in specs.iter().zip(rects).enumerate() {
        let style = if index == 0 {
            primary_button_style(chrome, p.accent)
        } else {
            secondary_button_style(chrome)
        };
        render_action_button(frame, rect, spec.hint, spec.label, style);
    }
}

pub fn render_new_project(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    path: &str,
    cursor: usize,
    error: Option<&str>,
) {
    let p = &chrome.palette;
    let Some(inner) = render_modal_shell(frame, area, POPUP_WIDTH, NEW_PROJECT_HEIGHT, p) else {
        return;
    };
    if inner.height < 5 {
        return;
    }
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas::<5>(inner);
    render_modal_header(frame, rows[0], NEW_PROJECT_TITLE, p);
    render_modal_description(
        frame,
        rows[1],
        "checkout path",
        Style::default().fg(p.subtext0),
    );
    input(frame, rows[2], chrome, path, Some(cursor));
    error_line(frame, rows[3], chrome, error);
    buttons(frame, inner, chrome, "open", &["tab"]);
}

#[allow(clippy::too_many_arguments)]
pub fn render_new_worktree(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    branch: &str,
    base: &str,
    cursor: usize,
    base_focused: bool,
    error: Option<&str>,
) {
    let p = &chrome.palette;
    let Some(inner) = render_modal_shell(frame, area, POPUP_WIDTH, NEW_WORKTREE_HEIGHT, p) else {
        return;
    };
    if inner.height < 7 {
        return;
    }
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas::<7>(inner);
    render_modal_header(frame, rows[0], "new worktree", p);
    let label = Style::default().fg(p.subtext0);
    render_modal_description(frame, rows[1], "branch", label);
    input(
        frame,
        rows[2],
        chrome,
        branch,
        (!base_focused).then_some(cursor),
    );
    render_modal_description(frame, rows[3], "base", label);
    input(frame, rows[4], chrome, base, base_focused.then_some(cursor));
    error_line(frame, rows[5], chrome, error);
    buttons(frame, inner, chrome, "create", &[]);
}

pub fn render_open_worktree(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    choices: &[WorktreeChoice],
    selected: usize,
) {
    let p = &chrome.palette;
    let option_rows = choices.len().max(1).min(u16::MAX as usize) as u16;
    let Some(inner) = render_modal_shell(
        frame,
        area,
        POPUP_WIDTH,
        OPEN_WORKTREE_BASE_HEIGHT + option_rows,
        p,
    ) else {
        return;
    };
    if inner.height < 4 {
        return;
    }
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(option_rows),
        Constraint::Min(0),
    ])
    .areas::<4>(inner);
    render_modal_header(frame, rows[0], "open worktree", p);
    if choices.is_empty() {
        render_modal_description(
            frame,
            rows[2],
            "every worktree already has a tab",
            Style::default().fg(p.subtext0),
        );
    }
    for (index, (choice, rect)) in choices
        .iter()
        .zip(modal_choice_rows(rows[2], choices.len(), 1))
        .enumerate()
    {
        let is_selected = index == selected;
        let marker = if is_selected { " ▸ " } else { "   " };
        let style = if is_selected {
            Style::default()
                .bg(p.surface0)
                .fg(p.text)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(p.subtext0)
        };
        frame.render_widget(
            Paragraph::new(format!("{marker}{} — {}", choice.branch, choice.path)).style(style),
            rect,
        );
    }
    buttons(frame, inner, chrome, "open", &[]);
}

#[allow(clippy::too_many_arguments)]
pub fn render_remove_worktree(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    branch: &str,
    path: &str,
    tabs: usize,
    panes: usize,
    error: Option<&str>,
) {
    let p = &chrome.palette;
    let Some(popup) = centered_popup_rect(area, POPUP_WIDTH, REMOVE_WORKTREE_HEIGHT) else {
        return;
    };
    let Some(inner) = render_panel_shell(frame, popup, p.red, p.panel_bg) else {
        return;
    };
    if inner.height < 5 {
        return;
    }
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas::<5>(inner);
    let warn = Style::default().fg(p.red).add_modifier(Modifier::BOLD);
    frame.render_widget(
        Paragraph::new(Line::styled(" delete worktree checkout?", warn)),
        rows[0],
    );
    render_modal_description(
        frame,
        rows[1],
        &format!("{branch} — {path}"),
        Style::default().fg(p.text).add_modifier(Modifier::BOLD),
    );
    render_modal_description(
        frame,
        rows[2],
        &format!(
            "closes {} and {} first",
            plural(tabs, "tab"),
            plural(panes, "pane")
        ),
        Style::default().fg(p.overlay0),
    );
    error_line(frame, rows[3], chrome, error);
    let rects = action_button_row_rects(
        inner,
        &[
            ActionButtonSpec {
                hint: Some("↵"),
                label: "delete",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "cancel",
            },
        ],
        2,
        inner.height.saturating_sub(1),
    );
    if let [delete_rect, cancel_rect] = rects[..] {
        render_action_button(
            frame,
            delete_rect,
            Some("↵"),
            "delete",
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

/// `1 pane` / `2 panes`.
pub fn plural(count: usize, noun: &str) -> String {
    format!("{count} {noun}{}", if count == 1 { "" } else { "s" })
}

/// `~` and `~/...` resolved against `home`; anything else unchanged.
pub fn expand_home(input: &str, home: &Path) -> String {
    if input == "~" {
        return home.to_string_lossy().into_owned();
    }
    match input.strip_prefix("~/") {
        Some(rest) => home.join(rest).to_string_lossy().into_owned(),
        None => input.to_string(),
    }
}

/// `input` completed to the one directory it is a prefix of, with a
/// trailing slash; `None` when no directory or more than one matches. A
/// `~` prefix survives the completion.
pub fn complete_directory(input: &str, home: &Path) -> Option<String> {
    let expanded = expand_home(input, home);
    let (dir, prefix) = match expanded.rsplit_once('/') {
        Some(("", prefix)) => ("/", prefix),
        Some((dir, prefix)) => (dir, prefix),
        None => (".", expanded.as_str()),
    };
    let mut matches: Vec<String> = fs::read_dir(dir)
        .ok()?
        .flatten()
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| {
            name.starts_with(prefix) && (prefix.starts_with('.') || !name.starts_with('.'))
        })
        .collect();
    if matches.len() != 1 {
        return None;
    }
    let name = matches.remove(0);
    let completed = format!("{}/{name}/", dir.trim_end_matches('/'));
    if input.starts_with('~') {
        let home = home.to_string_lossy();
        if let Some(rest) = completed.strip_prefix(home.as_ref()) {
            return Some(format!("~{rest}"));
        }
    }
    Some(completed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expand_home_resolves_tilde_forms_only() {
        let home = Path::new("/home/me");
        assert_eq!(expand_home("~", home), "/home/me");
        assert_eq!(expand_home("~/src/x", home), "/home/me/src/x");
        assert_eq!(expand_home("/abs/~/x", home), "/abs/~/x");
        assert_eq!(expand_home("rel", home), "rel");
    }

    #[test]
    fn complete_directory_needs_one_directory_match_and_keeps_tilde() {
        let root = tempfile::tempdir().expect("root");
        std::fs::create_dir(root.path().join("alpha-one")).expect("alpha-one");
        std::fs::create_dir(root.path().join("beta")).expect("beta");
        std::fs::create_dir(root.path().join(".hidden")).expect(".hidden");
        std::fs::write(root.path().join("alpha.txt"), b"").expect("alpha.txt");
        let root_str = root.path().to_string_lossy().into_owned();
        assert_eq!(
            complete_directory(&format!("{root_str}/al"), root.path()),
            Some(format!("{root_str}/alpha-one/"))
        );
        assert_eq!(
            complete_directory(&format!("{root_str}/alpha-one"), root.path()),
            Some(format!("{root_str}/alpha-one/"))
        );
        assert_eq!(
            complete_directory(&format!("{root_str}/"), root.path()),
            None
        );
        assert_eq!(
            complete_directory(&format!("{root_str}/.h"), root.path()),
            Some(format!("{root_str}/.hidden/"))
        );
        assert_eq!(
            complete_directory(&format!("{root_str}/zz"), root.path()),
            None
        );
        assert_eq!(
            complete_directory("~/be", root.path()),
            Some("~/beta/".to_string())
        );
    }
}
