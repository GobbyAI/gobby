// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar row models: machine rows, project cards with their worktree
//! rows, and session rows, plus the line builders the sidebar and navigator
//! share.
//!
//! Ported from herdr `resolved_token_spans`: a state glyph plus text tokens
//! joined by `" "` after the glyph and `" · "` elsewhere; trailing tokens
//! drop from the right before the title truncates. Project cards are one
//! line: the name, then the branch and git counts in parentheses, dropped
//! whole before the name truncates.

use crate::app::sidebar_model::ProjectEntry;
use crate::theme::Palette;
use crate::ui::chrome::{terminal_address, Chrome, RowState, WorkspaceView};
use crate::ui::sidebar::machine_admits;
use crate::ui::status::{control_indicator, state_dot, state_label, state_label_color};
use crate::ui::text::{display_width, truncate_end};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthChar;

/// Render ticks per cell the ticker moves (about eight frames a cell).
pub const TICKER_STEP: u64 = 8;
/// Cells' worth of ticks the ticker rests at either end of its run.
pub const TICKER_PAUSE: u64 = 12;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum RowKind {
    /// A project card: one line, the name then the branch.
    #[default]
    Project,
    /// A worktree row indented under its project card.
    Worktree,
    /// A session, agent run or bare terminal row: two lines.
    Agent,
    /// A machine row: one line.
    Machine,
    /// A project sub-heading of the all-projects sessions list: one dim
    /// line, not clickable.
    Group,
}

#[derive(Debug, Clone, Default)]
pub struct SidebarRow {
    pub id: String,
    pub label: String,
    pub kind: RowKind,
    pub state: RowState,
    /// The task ref of a worktree row; a machine row's `local`/`all` mark.
    pub detail: String,
    /// An agent row's second line: provider, model, task ref or tab, and
    /// remote machine, empties already elided.
    pub tokens: Vec<String>,
    /// The project card's branch, `~` without one.
    pub branch: Option<String>,
    pub ahead: u32,
    pub behind: u32,
    /// `Some(expanded)` on a project card that has worktrees, which draws
    /// the `▾`/`▸` toggle at its right edge.
    pub group: Option<bool>,
    /// A row drawn under a parent with a `├─`/`└─` prefix: a worktree
    /// under its card, an agent run under the session that spawned it, a
    /// machine under the hub.
    pub nested: bool,
    /// The last nested row under its parent (`└─` instead of `├─`).
    pub last_child: bool,
    pub selected: bool,
    /// A project card: the focused project. An agent row: the focused pane
    /// of the active tab shows its terminal (herdr "active workspace": bold
    /// `text` title on `surface_dim`).
    pub active: bool,
}

impl SidebarRow {
    /// Screen lines the row takes: an agent row is two, the rest one.
    pub fn height(&self) -> u16 {
        match self.kind {
            RowKind::Agent => 2,
            RowKind::Project | RowKind::Worktree | RowKind::Machine | RowKind::Group => 1,
        }
    }
}

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

/// Project cards in the user's order, the expanded card followed by its
/// worktree rows; `selected` indexes this flat list.
pub fn project_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let focused = ws.focused_project();
    let mut rows = Vec::new();
    for project in listed_projects(ws, chrome) {
        let expanded = chrome.sidebar.is_expanded(&project.project_id);
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
            group: (!project.worktrees.is_empty()).then_some(expanded),
            active: focused == Some(project.project_id.as_str()),
            ..SidebarRow::default()
        });
        if !expanded {
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
                    // A detached worktree has no branch; `~` as on its card.
                    label: worktree
                        .branch
                        .as_deref()
                        .map_or("~", |branch| {
                            branch.strip_prefix("worktree/").unwrap_or(branch)
                        })
                        .to_string(),
                    kind: RowKind::Worktree,
                    state: worktree.state,
                    detail: worktree.task_ref.clone().unwrap_or_default(),
                    nested: true,
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
/// after in model order, under the working filter. Index-based project
/// actions and drag reorders go by this list.
pub fn displayed_project_ids<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<String> {
    listed_projects(ws, chrome)
        .into_iter()
        .map(|project| project.project_id.clone())
        .collect()
}

/// The projects the section lists, in the user's order: every project
/// under the `all` filter; under `working`, the focused one and those with
/// a live entry the machine filter admits, or every project when none
/// qualifies.
pub fn listed_projects<'a, W: WorkspaceView>(ws: &'a W, chrome: &Chrome) -> Vec<&'a ProjectEntry> {
    let model = ws.sidebar();
    let ordered = ordered_projects(&model.projects, &chrome.sidebar.project_order);
    if chrome.sidebar.all_projects {
        return ordered;
    }
    let focused = ws.focused_project();
    let working: Vec<&ProjectEntry> = ordered
        .iter()
        .copied()
        .filter(|project| {
            focused == Some(project.project_id.as_str())
                || model.agents.iter().any(|agent| {
                    agent.project_id == project.project_id
                        && machine_admits(ws, chrome, &agent.machine_id)
                })
        })
        .collect();
    if working.is_empty() {
        ordered
    } else {
        working
    }
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
            let (glyph, label, _) = control_indicator(pane.displayed_control(), pane.take_back, p);
            // The address leads, so it is what the navigator query matches
            // and what keeps two panes sharing a name (`zsh`, `zsh`) apart.
            match terminal_address(ws, terminal_id) {
                Some(address) => format!("{address} {} {glyph} {label}", pane.backend),
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

/// The `├─ `/`└─ ` prefix of a nested row, empty on a top-level one.
fn nest_prefix(row: &SidebarRow) -> &'static str {
    match (row.nested, row.last_child) {
        (false, _) => "",
        (true, false) => "├─ ",
        (true, true) => "└─ ",
    }
}

/// The first rendered line of `row` at `width` columns. A project card is
/// `{marker}{dot} {name} ({branch} ↑a ↓b)` with the group toggle at the
/// right edge; a worktree row is `{marker}  ├─ {dot} {branch} · {task}`
/// with the prefix in `overlay0` so the branch sits under its card's name;
/// a machine row is the same shape without the indent; an agent row is the
/// herdr composition: state dot, the label always bold, `needs you` after
/// a blocked one, with its tokens on `row_second_line`; a group row is the
/// dim project name and a rule. An agent title wider than its budget
/// scrolls on the shared marquee clock, `max_travel` being the longest
/// overrun among the rows drawn with it (`row_travel`).
pub fn row_line<'a>(
    row: &'a SidebarRow,
    width: u16,
    chrome: &Chrome,
    max_travel: usize,
) -> Line<'a> {
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
    let prefix_style = Style::default().fg(p.overlay0);
    let marker = if row.selected { "▸" } else { " " };
    let mut spans = vec![Span::styled(marker, marker_style)];
    let budget = usize::from(width).saturating_sub(1);
    match row.kind {
        RowKind::Project => {
            let toggle = row.group.map(|expanded| if expanded { "▾" } else { "▸" });
            let name_budget = budget.saturating_sub(if toggle.is_some() { 2 } else { 0 });
            spans.extend(card_spans(row, glyph, title_style, p, name_budget));
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
            let prefix = format!("  {}", nest_prefix(row));
            let prefix_width = display_width(&prefix);
            spans.push(Span::styled(prefix, prefix_style));
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[(row.detail.as_str(), detail_style)],
                p,
                budget.saturating_sub(prefix_width),
            ));
        }
        RowKind::Machine => {
            let prefix = nest_prefix(row);
            spans.push(Span::styled(prefix, prefix_style));
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[(row.detail.as_str(), detail_style)],
                p,
                budget.saturating_sub(display_width(prefix)),
            ));
        }
        RowKind::Agent => {
            let prefix = nest_prefix(row);
            spans.push(Span::styled(prefix, prefix_style));
            let budget = budget.saturating_sub(display_width(prefix));
            let label_style = Style::default()
                .fg(state_label_color(row.state, p))
                .add_modifier(Modifier::DIM);
            let mut trailing: Vec<(&str, Style)> = (row.state == RowState::Attention)
                .then(|| (state_label(row.state), label_style))
                .into_iter()
                .collect();
            let label = if chrome.prefs.reduced_motion {
                row.label.clone()
            } else {
                // As on every row, the word drops before the title loses a
                // cell; a title over-long even alone tickers instead.
                if display_width(&row.label) > title_budget(&trailing, budget) {
                    trailing.clear();
                }
                ticker_window(
                    &row.label,
                    title_budget(&trailing, budget),
                    chrome.ticker,
                    max_travel,
                )
            };
            spans.extend(fitted_spans(
                glyph,
                (&label, title_style),
                &trailing,
                p,
                budget,
            ));
        }
        RowKind::Group => {
            let style = Style::default().fg(p.overlay0).add_modifier(Modifier::DIM);
            let name = truncate_end(&row.label, budget.saturating_sub(2));
            let rule = budget.saturating_sub(display_width(&name) + 1);
            spans.push(Span::styled(name, style));
            spans.push(Span::styled(format!(" {}", "─".repeat(rule)), style));
        }
    }
    Line::from(spans)
}

/// A project card's glyph, name and `(branch ↑a ↓b)`: the parenthetical
/// goes whole when it does not fit beside the whole name, and the name
/// truncates only once it stands alone. The branch is `mauve` on the
/// focused project and `overlay0` elsewhere; ` ↑n` is the success role
/// and ` ↓m` the warning role.
fn card_spans(
    row: &SidebarRow,
    glyph: (&str, Style),
    title_style: Style,
    p: &Palette,
    max_width: usize,
) -> Vec<Span<'static>> {
    let paren_style = Style::default().fg(p.overlay0).add_modifier(Modifier::DIM);
    let branch_style = Style::default().fg(if row.active { p.mauve } else { p.overlay0 });
    let mut paren = vec![
        Span::styled(" (", paren_style),
        Span::styled(
            row.branch.clone().unwrap_or_else(|| "~".into()),
            branch_style,
        ),
    ];
    if row.ahead > 0 {
        paren.push(Span::styled(
            format!(" ↑{}", row.ahead),
            Style::default().fg(p.green),
        ));
    }
    if row.behind > 0 {
        paren.push(Span::styled(
            format!(" ↓{}", row.behind),
            Style::default().fg(p.red),
        ));
    }
    paren.push(Span::styled(")", paren_style));
    let paren_width: usize = paren.iter().map(|span| display_width(&span.content)).sum();
    let head = display_width(glyph.0) + 1;
    let mut spans = fitted_spans(glyph, (&row.label, title_style), &[], p, max_width);
    if head + display_width(&row.label) + paren_width <= max_width {
        spans.extend(paren);
    }
    spans
}

/// The cells `fitted_spans` leaves the title beside `trailing` in
/// `max_width`: after the glyph, its blank, and every trailing token.
fn title_budget(trailing: &[(&str, Style)], max_width: usize) -> usize {
    let tokens: usize = trailing
        .iter()
        .filter(|(text, _)| !text.is_empty())
        .map(|(text, _)| 3 + display_width(text))
        .sum();
    max_width.saturating_sub(2 + tokens)
}

/// Cells an agent row's title overruns its marquee budget by at `width`,
/// zero when it fits or the row never scrolls. The longest overrun among
/// the rows drawn together sets their shared period.
pub fn row_travel(row: &SidebarRow, width: u16) -> usize {
    if row.kind != RowKind::Agent {
        return 0;
    }
    let budget = usize::from(width).saturating_sub(1 + display_width(nest_prefix(row)));
    let budget = title_budget(&[], budget);
    if budget < 4 {
        return 0;
    }
    display_width(&row.label).saturating_sub(budget)
}

/// The `budget`-cell window of `text` the marquee shows at `ticker`: the
/// whole text while it fits, else a slice that rests at the start for
/// `TICKER_PAUSE` steps, walks one cell per `TICKER_STEP` ticks to the end
/// and parks there until the period ends, then jumps home. The period is
/// the longest overrun drawn beside it (`max_travel`, at least its own)
/// plus the two pauses, so every scrolling row moves and restarts as one
/// object (D7). Under four cells nothing scrolls and the caller's
/// truncation applies.
pub fn ticker_window(text: &str, budget: usize, ticker: u64, max_travel: usize) -> String {
    let width = display_width(text);
    if width <= budget || budget < 4 {
        return text.to_string();
    }
    let travel = (width - budget) as u64;
    let step = ticker / TICKER_STEP;
    let period = travel.max(max_travel as u64) + 2 * TICKER_PAUSE;
    let offset = (step % period).saturating_sub(TICKER_PAUSE).min(travel);
    let mut skipped = 0;
    let mut taken = 0;
    let mut window = String::new();
    for ch in text.chars() {
        let cell = ch.width().unwrap_or(0);
        if skipped < offset as usize {
            skipped += cell;
            continue;
        }
        if taken + cell > budget {
            break;
        }
        taken += cell;
        window.push(ch);
    }
    window
}

/// An agent row's second line: its tokens under the label, ` · ` apart, in
/// herdr's dim `overlay0` agent style, dropped from the right as the width
/// runs out; a nested row keeps its prefix width. Every other row has one
/// line.
pub fn row_second_line<'a>(row: &'a SidebarRow, width: u16, chrome: &Chrome) -> Line<'a> {
    let p = &chrome.palette;
    if row.kind != RowKind::Agent {
        return Line::default();
    }
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
    // One blank for the marker column and the prefix, then the glyph column
    // blank, so the tokens start under the label (herdr's three-cell indent).
    let indent = 1 + display_width(nest_prefix(row));
    let mut spans = vec![Span::raw(" ".repeat(indent))];
    spans.extend(fitted_spans(
        (" ", token_style),
        (first, token_style),
        &rest,
        p,
        usize::from(width).saturating_sub(indent),
    ));
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
#[path = "sidebar_rows/tests.rs"]
mod tests;
