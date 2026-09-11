// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar row models: project cards with their worktree rows, and agent
//! rows, plus the line builders the sidebar and navigator share.
//!
//! Ported from herdr `resolved_token_spans`: a state glyph plus text tokens
//! joined by `" "` after the glyph and `" · "` elsewhere; trailing tokens
//! drop from the right before the title truncates. Project cards follow
//! herdr's workspace cards (`src/client/shell/sidebar.rs`): name on the
//! first line, branch and git counts on the second.

use crate::app::sidebar_model::ProjectEntry;
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::status::{control_indicator, state_dot, state_label, state_label_color};
use crate::ui::text::{display_width, truncate_end};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum RowKind {
    /// A project card: two lines, the name then the branch.
    #[default]
    Project,
    /// A worktree row indented under its project card.
    Worktree,
    /// A session or agent row: two lines.
    Agent,
    /// A machine row: one line.
    Machine,
}

#[derive(Debug, Clone, Default)]
pub struct SidebarRow {
    pub id: String,
    pub label: String,
    pub kind: RowKind,
    pub state: RowState,
    /// The task ref of a worktree row; a machine row's `local`/`all` mark.
    pub detail: String,
    /// An agent row's second line: provider, model, task ref, tab, and
    /// remote machine, empties already elided.
    pub tokens: Vec<String>,
    /// The project card's second line: its checkout branch, `~` without one.
    pub branch: Option<String>,
    pub ahead: u32,
    pub behind: u32,
    /// `Some(collapsed)` on a project card that has worktrees, which draws the
    /// `▸`/`▾` toggle at its right edge.
    pub group: Option<bool>,
    /// The last worktree row under its card (`└─` instead of `├─`).
    pub last_child: bool,
    pub selected: bool,
    /// A project card: the focused project. An agent row: the focused pane
    /// of the active tab shows its terminal (herdr "active workspace": bold
    /// `text` title on `surface_dim`).
    pub active: bool,
}

impl SidebarRow {
    /// Screen lines the row takes: a project card and an agent row are two,
    /// a worktree row one.
    pub fn height(&self) -> u16 {
        match self.kind {
            RowKind::Project | RowKind::Agent => 2,
            RowKind::Worktree | RowKind::Machine => 1,
        }
    }
}

/// Project cards in the user's order, each followed by its worktree rows
/// unless the card is collapsed; `selected` indexes this flat list.
/// The card label of `project_id`: the user's label when one is set, else
/// the daemon's name; `None` for a project the sidebar does not list.
pub fn project_label<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    project_id: &str,
) -> Option<String> {
    let project = ws
        .sidebar()
        .projects
        .iter()
        .find(|project| project.project_id == project_id)?;
    Some(
        chrome
            .sidebar
            .project_labels
            .get(project_id)
            .cloned()
            .unwrap_or_else(|| project.name.clone()),
    )
}

pub fn project_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let focused = ws.focused_project();
    let mut rows = Vec::new();
    for project in ordered_projects(&ws.sidebar().projects, &chrome.sidebar.project_order) {
        let collapsed = chrome
            .sidebar
            .collapsed_projects
            .contains(&project.project_id);
        rows.push(SidebarRow {
            id: project.project_id.clone(),
            label: chrome
                .sidebar
                .project_labels
                .get(&project.project_id)
                .cloned()
                .unwrap_or_else(|| project.name.clone()),
            kind: RowKind::Project,
            state: project.state,
            branch: Some(project.branch.clone().unwrap_or_else(|| "~".to_string())),
            ahead: project.ahead.unwrap_or(0),
            behind: project.behind.unwrap_or(0),
            group: (!project.worktrees.is_empty()).then_some(collapsed),
            active: focused == Some(project.project_id.as_str()),
            ..SidebarRow::default()
        });
        if collapsed {
            continue;
        }
        let count = project.worktrees.len();
        rows.extend(
            project
                .worktrees
                .iter()
                .enumerate()
                .map(|(index, worktree)| SidebarRow {
                    id: worktree.worktree_id.clone(),
                    label: worktree
                        .branch
                        .strip_prefix("worktree/")
                        .unwrap_or(&worktree.branch)
                        .to_string(),
                    kind: RowKind::Worktree,
                    state: worktree.state,
                    detail: worktree.task_ref.clone().unwrap_or_default(),
                    last_child: index + 1 == count,
                    ..SidebarRow::default()
                }),
        );
    }
    for (index, row) in rows.iter_mut().enumerate() {
        row.selected = index == chrome.sidebar.selected;
    }
    rows
}

/// Project ids as the sidebar lists them: the saved order first, the rest
/// after in model order. Index-based project actions and drag reorders go
/// by this list.
pub fn displayed_project_ids<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<String> {
    ordered_projects(&ws.sidebar().projects, &chrome.sidebar.project_order)
        .into_iter()
        .map(|project| project.project_id.clone())
        .collect()
}

/// `projects` with the ones `order` names first, in that order, and the
/// rest after in model order.
fn ordered_projects<'a>(projects: &'a [ProjectEntry], order: &[String]) -> Vec<&'a ProjectEntry> {
    let mut ordered: Vec<&ProjectEntry> = order
        .iter()
        .filter_map(|id| projects.iter().find(|project| project.project_id == *id))
        .collect();
    ordered.extend(
        projects
            .iter()
            .filter(|project| !order.contains(&project.project_id)),
    );
    ordered
}

/// `<backend> <control glyph> <control label>` for an attached terminal,
/// `detached` for a roster row without a pane.
pub(crate) fn terminal_detail<W: WorkspaceView>(ws: &W, terminal_id: &str, p: &Palette) -> String {
    match ws.pane_for_terminal(terminal_id).map(|id| ws.pane(id)) {
        Some(pane) => {
            let (glyph, label, _) = control_indicator(pane.control, pane.take_back, p);
            // The address rides with the backend that owns it, which is what
            // keeps two panes sharing a title (`zsh`, `zsh`) tellable apart.
            match pane.address.as_deref() {
                Some(address) => format!("{} {address} {glyph} {label}", pane.backend),
                None => format!("{} {glyph} {label}", pane.backend),
            }
        }
        None => "detached".to_string(),
    }
}

/// Prompt kind of an attention entry keyed `<kind>:<terminal>`.
pub(crate) fn attention_kind(entry_id: &str) -> &str {
    entry_id.split_once(':').map_or("prompt", |(kind, _)| kind)
}

/// The first rendered line of `row` at `width` columns. A project card is
/// `{marker}{dot} {name}` with the group toggle at the right edge; a
/// worktree row is `{marker}  ├─ {dot} {branch} · {task}` with the prefix in
/// `overlay0` so the branch sits under its card's name; an agent row is the
/// herdr composition: state dot, the label always bold, trailing state
/// label, with its tokens on `row_second_line`.
pub fn row_line<'a>(row: &'a SidebarRow, width: u16, chrome: &Chrome) -> Line<'a> {
    let p = &chrome.palette;
    let (glyph, glyph_color) = state_dot(row.state, p);
    let glyph = (glyph, Style::default().fg(glyph_color));
    let marker_style = if row.selected {
        Style::default().fg(p.accent).bg(p.surface1)
    } else {
        Style::default()
    };
    let title_style = if row.selected || row.active {
        Style::default().fg(p.text).add_modifier(Modifier::BOLD)
    } else if row.kind == RowKind::Agent {
        Style::default().fg(p.subtext0).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(p.subtext0)
    };
    // herdr's default agent token is `overlay0` + dim.
    let detail_style = Style::default()
        .fg(if row.selected { p.mauve } else { p.overlay0 })
        .add_modifier(Modifier::DIM);
    let marker = if row.selected { "▸" } else { " " };
    let mut spans = vec![Span::styled(marker, marker_style)];
    let budget = usize::from(width).saturating_sub(1);
    match row.kind {
        RowKind::Project => {
            let toggle = row.group.map(|collapsed| if collapsed { "▸" } else { "▾" });
            let name_budget = budget.saturating_sub(if toggle.is_some() { 2 } else { 0 });
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[],
                p,
                name_budget,
            ));
            if let Some(toggle) = toggle {
                let used: usize = spans.iter().map(|span| display_width(&span.content)).sum();
                let pad = usize::from(width).saturating_sub(used + 1);
                spans.push(Span::raw(" ".repeat(pad)));
                spans.push(Span::styled(toggle, Style::default().fg(p.accent)));
            }
        }
        RowKind::Worktree => {
            // Two cells after the marker so the branch sits under the card's
            // name (marker, dot, space, name).
            let prefix = if row.last_child {
                "  └─ "
            } else {
                "  ├─ "
            };
            spans.push(Span::styled(prefix, Style::default().fg(p.overlay0)));
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[(row.detail.as_str(), detail_style)],
                p,
                budget.saturating_sub(display_width(prefix)),
            ));
        }
        RowKind::Machine => {
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[(row.detail.as_str(), detail_style)],
                p,
                budget,
            ));
        }
        RowKind::Agent => {
            let label_style = Style::default()
                .fg(state_label_color(row.state, p))
                .add_modifier(Modifier::DIM);
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[(state_label(row.state), label_style)],
                p,
                budget,
            ));
        }
    }
    Line::from(spans)
}

/// A project card's second line: the branch under the name, then ` ↑n` in
/// the success role and ` ↓m` in the warning role when either count is set.
/// The branch is `mauve` on the focused project and `overlay0` elsewhere.
/// An agent row's second line: its tokens under the label, ` · ` apart, in
/// herdr's dim `overlay0` agent style, dropped from the right as the width
/// runs out.
pub fn row_second_line<'a>(row: &'a SidebarRow, width: u16, chrome: &Chrome) -> Line<'a> {
    let p = &chrome.palette;
    if row.kind == RowKind::Agent {
        let Some((first, rest)) = row.tokens.split_first() else {
            return Line::default();
        };
        let token_style = Style::default()
            .fg(if row.selected { p.mauve } else { p.overlay0 })
            .add_modifier(Modifier::DIM);
        let rest: Vec<(&str, Style)> = rest
            .iter()
            .map(|token| (token.as_str(), token_style))
            .collect();
        // One blank for the marker column, then the glyph column blank, so
        // the tokens start under the label (herdr's three-cell indent).
        let mut spans = vec![Span::raw(" ")];
        spans.extend(fitted_spans(
            (" ", token_style),
            (first, token_style),
            &rest,
            p,
            usize::from(width).saturating_sub(1),
        ));
        return Line::from(spans);
    }
    let branch_style = Style::default().fg(if row.active { p.mauve } else { p.overlay0 });
    let mut counts: Vec<Span<'a>> = Vec::new();
    if row.ahead > 0 {
        counts.push(Span::styled(
            format!(" ↑{}", row.ahead),
            Style::default().fg(p.green),
        ));
    }
    if row.behind > 0 {
        counts.push(Span::styled(
            format!(" ↓{}", row.behind),
            Style::default().fg(p.red),
        ));
    }
    let counts_width: usize = counts.iter().map(|span| display_width(&span.content)).sum();
    let budget = usize::from(width).saturating_sub(3 + counts_width);
    let branch = truncate_end(row.branch.as_deref().unwrap_or("~"), budget);
    let mut spans = vec![Span::raw("   "), Span::styled(branch, branch_style)];
    spans.extend(counts);
    Line::from(spans)
}

/// herdr `resolved_token_spans`, reduced to the glyph + title + trailing
/// shape: `" "` after the glyph, `" · "` between text tokens. Trailing tokens
/// are kept from the left while they fit beside the whole title and dropped
/// from the right otherwise; the title truncates only once it stands alone.
/// A roster row is identified by its terminal title, so the title outranks
/// its state and detail tokens here — the tab-bar rule (truncate the tab's
/// own title so its trailing tokens survive) does not apply to session
/// titles. Empty tokens are elided with their separators.
pub fn fitted_spans(
    glyph: (&str, Style),
    title: (&str, Style),
    trailing: &[(&str, Style)],
    p: &Palette,
    max_width: usize,
) -> Vec<Span<'static>> {
    let separator_style = Style::default().fg(p.overlay0).add_modifier(Modifier::DIM);
    let mut spans = vec![Span::styled(glyph.0.to_string(), glyph.1)];
    let remaining = max_width.saturating_sub(display_width(glyph.0));
    if remaining < 2 || title.0.is_empty() {
        return spans;
    }
    spans.push(Span::styled(" ", separator_style));
    let remaining = remaining - 1;
    let trailing: Vec<&(&str, Style)> = trailing
        .iter()
        .filter(|(text, _)| !text.is_empty())
        .collect();
    let cost = |tokens: &[&(&str, Style)]| -> usize {
        tokens.iter().map(|(text, _)| 3 + display_width(text)).sum()
    };
    let title_width = display_width(title.0);
    let mut keep = trailing.len();
    while keep > 0 && title_width + cost(&trailing[..keep]) > remaining {
        keep -= 1;
    }
    let title_budget = remaining.saturating_sub(cost(&trailing[..keep]));
    spans.push(Span::styled(truncate_end(title.0, title_budget), title.1));
    for (text, style) in &trailing[..keep] {
        spans.push(Span::styled(" · ", separator_style));
        spans.push(Span::styled(text.to_string(), *style));
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crate::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
    use crate::ui::hit::SidebarSection;
    use crate::ui::sidebar::agent_rows;
    use serde_json::json;

    fn scripted_workspace() -> Workspace {
        let mut ws = Workspace::scripted();
        let project = |id: &str, name: &str| ProjectRow {
            id: id.to_string(),
            name: name.to_string(),
            display_name: name.to_string(),
            checkout: Some(Checkout {
                machine_id: "local".to_string(),
                root_path: format!("/repos/{name}"),
            }),
            ..ProjectRow::default()
        };
        ws.daemon_mut().set_sidebar_rows(SidebarRows {
            projects: vec![project("proj-alpha", "alpha"), project("proj-beta", "beta")],
            statuses: [(
                "proj-alpha".to_string(),
                SourceStatus {
                    current_branch: Some("main".to_string()),
                    ahead: Some(2),
                    behind: Some(1),
                    ..SourceStatus::default()
                },
            )]
            .into_iter()
            .collect(),
            worktrees: vec![WorktreeRow {
                id: "wt-1".to_string(),
                project_id: "proj-alpha".to_string(),
                task_id: Some("#123".to_string()),
                branch_name: "worktree/feature".to_string(),
                worktree_path: "/repos/alpha/.worktrees/feature".to_string(),
                status: "active".to_string(),
                workspace_role: "task".to_string(),
                ..WorktreeRow::default()
            }],
            ..SidebarRows::default()
        });
        ws.daemon_mut().set_roster(json!({
            "epoch": "e1",
            "seq": 1,
            "entries": [{
                "entry_id": "run:term-alpha",
                "terminal": {"terminal_id": "term-alpha", "backend": "native"},
                "attention": {"attention_id": "att-1", "kind": "actionable", "fingerprint": "fp-1"}
            }]
        }));
        ws.select_project("proj-alpha");
        ws.reconcile_subscribe_first().unwrap();
        ws.open_terminal("term-alpha", "native", "epoch").unwrap();
        ws.open_terminal("term-beta", "native", "epoch").unwrap();
        ws
    }

    fn line_text(line: &Line<'_>) -> String {
        line.spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect()
    }

    #[test]
    fn project_rows_follow_the_saved_order_and_hide_collapsed_worktrees() {
        let ws = scripted_workspace();
        let mut chrome = Chrome::dark();
        chrome.sidebar.selected = 1;
        let rows = project_rows(&ws, &chrome);
        let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        assert_eq!(ids, ["proj-alpha", "wt-1", "proj-beta"]);
        assert_eq!(rows[0].kind, RowKind::Project);
        assert_eq!(rows[0].branch.as_deref(), Some("main"));
        assert_eq!((rows[0].ahead, rows[0].behind), (2, 1));
        assert_eq!(rows[0].group, Some(false));
        assert!(rows[0].active && !rows[0].selected);
        assert_eq!(rows[1].kind, RowKind::Worktree);
        assert_eq!(rows[1].label, "feature");
        assert_eq!(rows[1].detail, "#123");
        assert!(rows[1].last_child && rows[1].selected);
        assert_eq!(rows[2].branch.as_deref(), Some("~"));
        assert_eq!(rows[2].group, None);
        assert!(!rows[2].active);

        chrome.sidebar.project_order = vec!["proj-beta".to_string()];
        chrome
            .sidebar
            .collapsed_projects
            .insert("proj-alpha".to_string());
        let rows = project_rows(&ws, &chrome);
        let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        assert_eq!(ids, ["proj-beta", "proj-alpha"]);
        assert_eq!(rows[1].group, Some(true));
    }

    #[test]
    fn agent_rows_point_at_their_terminal() {
        let ws = scripted_workspace();
        let chrome = Chrome::dark();
        assert!(agent_rows(&ws, &chrome, SidebarSection::Sessions).is_empty());
        let rows = agent_rows(&ws, &chrome, SidebarSection::Agents);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, "run:term-alpha");
        assert_eq!(rows[0].label, "term-alpha");
        assert_eq!(rows[0].kind, RowKind::Agent);
        assert_eq!(rows[0].state, RowState::Attention);
        assert_eq!(rows[0].height(), 2);
        assert!(rows[0].tokens.is_empty(), "{:?}", rows[0].tokens);
    }

    #[test]
    fn project_lines_carry_the_toggle_branch_and_counts() {
        let chrome = Chrome::dark();
        let row = SidebarRow {
            id: "proj-alpha".into(),
            label: "alpha".into(),
            branch: Some("main".into()),
            ahead: 2,
            behind: 1,
            group: Some(true),
            active: true,
            ..SidebarRow::default()
        };
        assert_eq!(line_text(&row_line(&row, 12, &chrome)), " ○ alpha   ▸");
        assert_eq!(
            line_text(&row_second_line(&row, 13, &chrome)),
            "   main ↑2 ↓1"
        );
        let worktree = SidebarRow {
            id: "wt-1".into(),
            label: "feature".into(),
            kind: RowKind::Worktree,
            detail: "#123".into(),
            last_child: true,
            selected: true,
            ..SidebarRow::default()
        };
        assert_eq!(
            line_text(&row_line(&worktree, 30, &chrome)),
            "▸  └─ ○ feature · #123"
        );
    }

    #[test]
    fn row_line_drops_trailing_tokens_before_truncating_the_title() {
        let chrome = Chrome::dark();
        let row = SidebarRow {
            id: "run:term-alpha".into(),
            label: "term-alpha".into(),
            kind: RowKind::Agent,
            state: RowState::Idle,
            tokens: vec!["codex".into(), "gpt-5".into(), "#123".into()],
            selected: true,
            ..SidebarRow::default()
        };
        let wide = line_text(&row_line(&row, 60, &chrome));
        assert_eq!(wide, "▸○ term-alpha · idle");
        // The state label no longer fits beside the whole title, so it drops
        // before the title loses a cell.
        let narrow = line_text(&row_line(&row, 14, &chrome));
        assert_eq!(narrow, "▸○ term-alpha");
        let tiny = line_text(&row_line(&row, 8, &chrome));
        assert_eq!(tiny, "▸○ term…");
        // The tokens sit under the label and drop from the right.
        assert_eq!(
            line_text(&row_second_line(&row, 60, &chrome)),
            "   codex · gpt-5 · #123"
        );
        assert_eq!(
            line_text(&row_second_line(&row, 18, &chrome)),
            "   codex · gpt-5"
        );
        let bare = SidebarRow {
            tokens: Vec::new(),
            ..row
        };
        assert_eq!(line_text(&row_second_line(&bare, 60, &chrome)), "");
    }
}
