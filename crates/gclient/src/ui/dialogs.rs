// upstream: herdr v0.8.0 src/ui/dialogs.rs
//! Modal dialogs: confirm close, rename, and the Gobby attention respond
//! prompt. No plugin or repository-checkout dialogs.

use crate::ui::chrome::Chrome;
use crate::ui::widgets::{
    action_button_row_rects, centered_popup_rect, modal_choice_rows, panel_contrast_fg,
    render_action_button, render_modal_description, render_modal_header, render_modal_shell,
    render_panel_shell, ActionButtonSpec,
};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Clear, Paragraph};
use ratatui::Frame;

pub mod orphans;
pub mod project;

const CONFIRM_CLOSE_POPUP_WIDTH: u16 = 64;
const CONFIRM_CLOSE_POPUP_HEIGHT: u16 = 6;
const RENAME_POPUP_WIDTH: u16 = 56;
const RENAME_POPUP_HEIGHT: u16 = 7;
const RESPOND_POPUP_WIDTH: u16 = 64;
/// Respond rows besides the options: header, gap, prompt (2), gap, input,
/// gap, buttons, plus the two border rows.
const RESPOND_POPUP_BASE_HEIGHT: u16 = 10;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RenameKind {
    Tab,
    Pane,
    Terminal,
    /// A project card's label; the id names the project.
    Project(String),
}

impl RenameKind {
    fn title(&self) -> &'static str {
        match self {
            RenameKind::Tab => "rename tab",
            RenameKind::Pane => "rename pane",
            RenameKind::Terminal => "rename terminal",
            RenameKind::Project(_) => "rename project",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CloseTarget {
    Pane,
    Tab,
    Terminal,
    /// A project without worktree children; the id names it.
    Project(String),
    /// A project and its worktree children; the id names the project.
    WorktreeGroup(String),
}

impl CloseTarget {
    fn noun(&self) -> &'static str {
        match self {
            CloseTarget::Pane => "pane",
            CloseTarget::Tab => "tab",
            CloseTarget::Terminal => "terminal",
            CloseTarget::Project(_) => "project",
            CloseTarget::WorktreeGroup(_) => "worktree group",
        }
    }
}

/// herdr's confirm-close scope line: how much the close destroys.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CloseScope {
    Panes(usize),
    Tabs { tabs: usize, panes: usize },
    Group { workspaces: usize, panes: usize },
}

impl std::fmt::Display for CloseScope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CloseScope::Panes(panes) => f.write_str(&project::plural(*panes, "pane")),
            CloseScope::Tabs { tabs, panes } => write!(
                f,
                "{}, {}",
                project::plural(*tabs, "tab"),
                project::plural(*panes, "pane")
            ),
            CloseScope::Group { workspaces, panes } => write!(
                f,
                "{}, {}",
                project::plural(*workspaces, "workspace"),
                project::plural(*panes, "pane")
            ),
        }
    }
}

/// One row of the open-worktree picker.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorktreeChoice {
    pub worktree_id: String,
    pub branch: String,
    pub path: String,
}

/// One candidate row of the destroy-orphaned-terminals dialog.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrphanRow {
    pub terminal_id: String,
    pub backend: String,
    /// The tmux session name, the row title, or the short terminal id.
    pub name: String,
    /// The Gobby session still owning the row's pane, when one does.
    pub owner: Option<String>,
    /// `HH:MM` of the row's last update, when the daemon sent one.
    pub last_seen: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Dialog {
    ConfirmClose {
        target: CloseTarget,
        title: String,
        scope: CloseScope,
    },
    /// Register a checkout as a project; the daemon's refusal shows inline.
    NewProject {
        path: String,
        cursor: usize,
        error: Option<String>,
    },
    /// Create a client worktree of `project_id` from `base`.
    NewWorktree {
        project_id: String,
        branch: String,
        base: String,
        cursor: usize,
        base_focused: bool,
        error: Option<String>,
    },
    /// Pick one of the project's worktrees no tab shows.
    OpenWorktree {
        project_id: String,
        choices: Vec<WorktreeChoice>,
        selected: usize,
    },
    /// Pick which orphaned terminals to destroy; `checked` parallels `rows`.
    DestroyOrphans {
        rows: Vec<OrphanRow>,
        checked: Vec<bool>,
        selected: usize,
    },
    /// Delete a worktree checkout after its `tabs` and `panes` are closed.
    RemoveWorktree {
        worktree_id: String,
        branch: String,
        path: String,
        tabs: usize,
        panes: usize,
        error: Option<String>,
    },
    Rename {
        kind: RenameKind,
        value: String,
        cursor: usize,
    },
    /// Answer an attention prompt: pick an option or type free text.
    Respond {
        entry_id: String,
        prompt: String,
        options: Vec<String>,
        selected: usize,
        text: String,
    },
}

/// Render `chrome.dialog`, if any, centred over `area`. Returns the button
/// rects the dialog drew, in its button order, so clicks can reach them.
pub fn render_dialog(frame: &mut Frame, area: Rect, chrome: &Chrome) -> Vec<Rect> {
    match &chrome.dialog {
        Some(Dialog::ConfirmClose {
            target,
            title,
            scope,
        }) => {
            return render_confirm_close(frame, area, chrome, target, title, scope);
        }
        Some(Dialog::Rename {
            kind,
            value,
            cursor,
        }) => render_rename(frame, area, chrome, kind, value, *cursor),
        Some(Dialog::NewProject {
            path,
            cursor,
            error,
        }) => {
            project::render_new_project(frame, area, chrome, path, *cursor, error.as_deref());
        }
        Some(Dialog::NewWorktree {
            branch,
            base,
            cursor,
            base_focused,
            error,
            ..
        }) => project::render_new_worktree(
            frame,
            area,
            chrome,
            branch,
            base,
            *cursor,
            *base_focused,
            error.as_deref(),
        ),
        Some(Dialog::OpenWorktree {
            choices, selected, ..
        }) => project::render_open_worktree(frame, area, chrome, choices, *selected),
        Some(Dialog::DestroyOrphans {
            rows,
            checked,
            selected,
        }) => orphans::render_destroy_orphans(frame, area, chrome, rows, checked, *selected),
        Some(Dialog::RemoveWorktree {
            branch,
            path,
            tabs,
            panes,
            error,
            ..
        }) => project::render_remove_worktree(
            frame,
            area,
            chrome,
            branch,
            path,
            *tabs,
            *panes,
            error.as_deref(),
        ),
        Some(Dialog::Respond {
            prompt,
            options,
            selected,
            text,
            ..
        }) => render_respond(frame, area, chrome, prompt, options, *selected, text),
        None => {}
    }
    Vec::new()
}

fn primary_button_style(chrome: &Chrome, bg: ratatui::style::Color) -> Style {
    Style::default()
        .fg(panel_contrast_fg(&chrome.palette))
        .bg(bg)
        .add_modifier(Modifier::BOLD)
}

fn secondary_button_style(chrome: &Chrome) -> Style {
    Style::default()
        .fg(chrome.palette.text)
        .bg(chrome.palette.surface0)
        .add_modifier(Modifier::BOLD)
}

/// Draw the confirm-close popup; returns the `[close, cancel]` button rects
/// it drew, or nothing when the popup does not fit.
pub fn render_confirm_close(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    target: &CloseTarget,
    title: &str,
    scope: &CloseScope,
) -> Vec<Rect> {
    let p = &chrome.palette;
    let Some(popup) =
        centered_popup_rect(area, CONFIRM_CLOSE_POPUP_WIDTH, CONFIRM_CLOSE_POPUP_HEIGHT)
    else {
        return Vec::new();
    };
    let Some(inner) = render_panel_shell(frame, popup, p.red, p.panel_bg) else {
        return Vec::new();
    };
    if inner.height < 3 {
        return Vec::new();
    }

    let warn = Style::default().fg(p.red).add_modifier(Modifier::BOLD);
    let dim = Style::default().fg(p.overlay0);
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas::<4>(inner);

    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" Close {}?", target.noun()),
            warn,
        ))),
        rows[0],
    );
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                format!(" {title}"),
                Style::default().fg(p.text).add_modifier(Modifier::BOLD),
            ),
            Span::styled(format!(" — {scope}"), dim),
        ])),
        rows[1],
    );

    let rects = action_button_row_rects(
        inner,
        &[
            ActionButtonSpec {
                hint: Some("↵"),
                label: "close",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "cancel",
            },
        ],
        2,
        3,
    );
    let [close_rect, cancel_rect] = rects[..] else {
        return Vec::new();
    };
    render_action_button(
        frame,
        close_rect,
        Some("↵"),
        "close",
        primary_button_style(chrome, p.red),
    );
    render_action_button(
        frame,
        cancel_rect,
        Some("esc"),
        "cancel",
        secondary_button_style(chrome),
    );
    vec![close_rect, cancel_rect]
}

pub fn render_rename(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    kind: &RenameKind,
    value: &str,
    cursor: usize,
) {
    let p = &chrome.palette;
    let Some(inner) = render_modal_shell(frame, area, RENAME_POPUP_WIDTH, RENAME_POPUP_HEIGHT, p)
    else {
        return;
    };
    if inner.height < 4 {
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

    render_modal_header(frame, rows[0], kind.title(), p);

    let input_rect = Rect::new(rows[2].x, rows[2].y, rows[2].width, 1);
    frame.render_widget(Clear, input_rect);
    frame.render_widget(
        Paragraph::new(input_line(value, cursor)).style(Style::default().fg(p.text).bg(p.surface0)),
        input_rect,
    );

    let rects = action_button_row_rects(
        inner,
        &[
            ActionButtonSpec {
                hint: Some("↵"),
                label: "save",
            },
            ActionButtonSpec {
                hint: Some("^c"),
                label: "clear",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "cancel",
            },
        ],
        2,
        3,
    );
    if let [save_rect, clear_rect, cancel_rect] = rects[..] {
        render_action_button(
            frame,
            save_rect,
            Some("↵"),
            "save",
            primary_button_style(chrome, p.accent),
        );
        render_action_button(
            frame,
            clear_rect,
            Some("^c"),
            "clear",
            secondary_button_style(chrome),
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

/// ` value` with a block cursor at `cursor` (a char index; past the end
/// appends the block, herdr style).
fn input_line(value: &str, cursor: usize) -> Line<'static> {
    let cursor = cursor.min(value.chars().count());
    let before: String = value.chars().take(cursor).collect();
    let after: String = value.chars().skip(cursor).collect();
    Line::from(vec![
        Span::raw(format!(" {before}")),
        Span::raw("█"),
        Span::raw(after),
    ])
}

pub fn render_respond(
    frame: &mut Frame,
    area: Rect,
    chrome: &Chrome,
    prompt: &str,
    options: &[String],
    selected: usize,
    text: &str,
) {
    let p = &chrome.palette;
    let option_rows = options.len().min(u16::MAX as usize) as u16;
    let Some(inner) = render_modal_shell(
        frame,
        area,
        RESPOND_POPUP_WIDTH,
        RESPOND_POPUP_BASE_HEIGHT + option_rows,
        p,
    ) else {
        return;
    };
    if inner.height < 5 {
        return;
    }

    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(2),
        Constraint::Length(option_rows),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(0),
    ])
    .areas::<7>(inner);

    render_modal_header(frame, rows[0], "respond", p);
    render_modal_description(frame, rows[2], prompt, Style::default().fg(p.text));

    let free_text = selected >= options.len();
    for (idx, (option, rect)) in options
        .iter()
        .zip(modal_choice_rows(rows[3], options.len(), 1))
        .enumerate()
    {
        let is_selected = idx == selected;
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
            Paragraph::new(format!("{marker}{option}")).style(style),
            rect,
        );
    }

    let input_rect = rows[5];
    frame.render_widget(Clear, input_rect);
    let input = if free_text {
        Line::from(vec![
            Span::styled(" > ", Style::default().fg(p.accent)),
            Span::raw(text.to_string()),
            Span::raw("█"),
        ])
    } else {
        Line::from(vec![
            Span::styled(" > ", Style::default().fg(p.overlay1)),
            Span::raw(text.to_string()),
        ])
    };
    frame.render_widget(
        Paragraph::new(input).style(Style::default().fg(p.text).bg(p.surface0)),
        input_rect,
    );

    let rects = action_button_row_rects(
        inner,
        &[
            ActionButtonSpec {
                hint: Some("↵"),
                label: "send",
            },
            ActionButtonSpec {
                hint: Some("esc"),
                label: "cancel",
            },
        ],
        2,
        inner.height.saturating_sub(1),
    );
    if let [send_rect, cancel_rect] = rects[..] {
        render_action_button(
            frame,
            send_rect,
            Some("↵"),
            "send",
            primary_button_style(chrome, p.accent),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn input_line_places_cursor_by_char_index() {
        let line = input_line("héllo", 2);
        let text: String = line.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(text, " hé█llo");
        let line = input_line("ab", 9);
        let text: String = line.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(text, " ab█");
    }
}
