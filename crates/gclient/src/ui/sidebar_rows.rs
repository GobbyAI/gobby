// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar row models: machine rows, project cards with their worktree
//! rows, agent rows and terminal rows, plus the line builders the sidebar
//! and navigator share.
//!
//! Ported from herdr `resolved_token_spans`: a state glyph plus text tokens
//! joined by `" "` after the glyph and `" · "` elsewhere; trailing tokens
//! drop from the right before the title truncates. Project cards are one
//! line: the name, then the branch and git counts in parentheses, dropped
//! whole before the name truncates.

use crate::app::sidebar_model::ProjectEntry;
use crate::theme::Palette;
use crate::ui::chrome::{terminal_address, Chrome, RowState, WorkspaceView};
use crate::ui::settings::TitleScrolling;
use crate::ui::status::{control_indicator, state_dot};
use crate::ui::text::{display_width, truncate_end};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthChar;

/// Render ticks per cell the ticker moves (about eight frames a cell).
pub const TICKER_STEP: u64 = 8;
/// Cells' worth of ticks the ticker rests at either end of its run.
pub const TICKER_PAUSE: u64 = 12;
/// Narrowest window that scrolls; a narrower title truncates instead.
pub const TICKER_MIN_WINDOW: usize = 4;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum RowKind {
    /// A project card: one line, the name then the branch.
    #[default]
    Project,
    /// A worktree row indented under its project card.
    Worktree,
    /// An agent session or run: definition, task and model on three lines.
    Agent,
    /// A bare terminal: foreground app and address, over its working directory.
    Terminal,
    /// A machine row: one line.
    Machine,
    /// A project sub-heading of the all-projects sessions list: one dim
    /// line, not clickable.
    Group,
}

#[derive(Debug, Clone, Default)]
pub struct SidebarRow {
    pub id: String,
    pub definition: String,
    pub reference: String,
    pub task: Option<(String, String)>,
    pub model_slug: String,
    pub label: String,
    pub kind: RowKind,
    pub state: RowState,
    /// The task ref of a worktree row; a machine row's `local`/`all` mark;
    /// a terminal row's working directory.
    pub detail: String,
    /// A terminal row's address at the right of its first line: the
    /// pane's workspace ref, `tmux %16`, or the backend word alone.
    pub address: String,
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
    /// Screen lines the row takes: agents use three, terminals two, others one.
    pub fn height(&self) -> u16 {
        match self.kind {
            RowKind::Agent => 3,
            RowKind::Terminal => 2,
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
    if let Some(label) = chrome.sidebar.project_labels.get(project_id) {
        return Some(label.clone());
    }
    let project = ws
        .sidebar()
        .projects
        .iter()
        .find(|project| project.project_id == project_id)?;
    Some(project.name.clone())
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
                    // Rollups never yield Unknown, so here it marks a
                    // worktree with no agent bound, drawn without a glyph.
                    state: worktree.state.unwrap_or(RowState::Unknown),
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
/// under the `all` filter; otherwise only the focused project.
pub fn listed_projects<'a, W: WorkspaceView>(ws: &'a W, chrome: &Chrome) -> Vec<&'a ProjectEntry> {
    let model = ws.sidebar();
    let ordered = ordered_projects(&model.projects, &chrome.sidebar.project_order);
    if chrome.sidebar.all_projects {
        return ordered;
    }
    let focused = ws.focused_project();
    ordered
        .into_iter()
        .filter(|project| focused == Some(project.project_id.as_str()))
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

/// Cells a worktree row leaves its name: the width less the marker, the
/// two-cell indent, the nest prefix, the glyph and its spacer. The name's
/// ticker and its travel both measure against this one budget.
fn worktree_name_budget(row: &SidebarRow, width: u16) -> usize {
    usize::from(width).saturating_sub(5 + display_width(nest_prefix(row)))
}

/// A worktree row's lead after the marker: two cells so the branch sits
/// under the card's name (marker, dot, space, name), then the nest prefix.
fn worktree_prefix(row: &SidebarRow) -> String {
    format!("  {}", nest_prefix(row))
}

/// The column of a worktree row's state dot, from the row's left edge: the
/// one cell its click focuses the most urgent bound agent through (#23280).
pub(crate) fn worktree_glyph_offset(row: &SidebarRow) -> u16 {
    u16::try_from(1 + display_width(&worktree_prefix(row))).unwrap_or(u16::MAX)
}

/// The first rendered line of `row` at `width` columns. A project card is
/// `{marker}{dot} {name} ({branch} ↑a ↓b)` with the group toggle at the
/// right edge; a worktree row is `{marker}  ├─ {dot} {branch} · {task}`
/// with the prefix in `overlay0` so the branch sits under its card's name;
/// a machine row is the same shape without the indent; an agent row shows
/// the state glyph, definition and pinned reference; a terminal row shows
/// the state glyph and foreground app, with the pane's address at the
/// right edge while the name leaves it room. A group row is the project
/// name in bold accent and a rule. An agent title too long for its row
/// scrolls after its pinned reference, as its task title does on the second
/// line; a worktree name too long for its row drops its task and scrolls.
pub fn row_line<'a>(
    row: &'a SidebarRow,
    width: u16,
    chrome: &Chrome,
    max_travel: usize,
) -> Line<'a> {
    row_line_with_scrolling(row, width, chrome, max_travel, chrome.prefs.title_scrolling)
}

pub(crate) fn row_line_with_scrolling<'a>(
    row: &'a SidebarRow,
    width: u16,
    chrome: &Chrome,
    max_travel: usize,
    title_scrolling: TitleScrolling,
) -> Line<'a> {
    let p = &chrome.palette;
    let (glyph, glyph_color) = state_dot(row.state, p);
    let glyph = (glyph, Style::default().fg(glyph_color));
    let marker_style = if row.selected {
        Style::default().fg(p.accent).bg(p.selection)
    } else {
        Style::default()
    };
    // Josh's Option B board (#23280): names are text, refs and branches
    // the identifier hue, everything secondary subtext0. overlay0 is left
    // to rules and idle glyphs, which do not need AA.
    let emphasis = |style: Style| {
        if row.selected || row.active {
            style.add_modifier(Modifier::BOLD)
        } else {
            style
        }
    };
    let title_style = emphasis(Style::default().fg(p.text));
    let identifier_style = Style::default().fg(p.identifier);
    let secondary_style = Style::default().fg(p.subtext0);
    // Project names are bold accent; on the selection fill the sidebar
    // re-inks them to the theme's fallback wherever accent falls under AA.
    let project_style = Style::default().fg(p.accent).add_modifier(Modifier::BOLD);
    let prefix_style = Style::default().fg(p.overlay0);
    let marker = if row.selected { "▸" } else { " " };
    let mut spans = vec![Span::styled(marker, marker_style)];
    let budget = usize::from(width).saturating_sub(1);
    match row.kind {
        RowKind::Project => {
            let toggle = row.group.map(|expanded| if expanded { "▾" } else { "▸" });
            let name_budget = budget.saturating_sub(if toggle.is_some() { 2 } else { 0 });
            spans.extend(card_spans(row, glyph, project_style, p, name_budget));
            if let Some(toggle) = toggle {
                let used: usize = spans.iter().map(|span| display_width(&span.content)).sum();
                let pad = usize::from(width).saturating_sub(used + 1);
                spans.push(Span::raw(" ".repeat(pad)));
                spans.push(Span::styled(toggle, Style::default().fg(p.accent)));
            }
        }
        RowKind::Worktree => {
            let prefix = worktree_prefix(row);
            let prefix_width = display_width(&prefix);
            spans.push(Span::styled(prefix, prefix_style));
            // No agent bound, no state: a blank keeps the branch aligned.
            let glyph = if row.state == RowState::Unknown {
                (" ", glyph.1)
            } else {
                glyph
            };
            let name_budget = worktree_name_budget(row, width);
            if name_budget > 0 && display_width(&row.label) > name_budget {
                // A name the row cannot hold drops its task and scrolls
                // on the shared clock, as an agent's task line does.
                spans.push(Span::styled(glyph.0.to_string(), glyph.1));
                spans.push(Span::styled(
                    " ",
                    Style::default().fg(p.overlay0).add_modifier(Modifier::DIM),
                ));
                spans.push(Span::styled(
                    ticker_window(
                        &row.label,
                        name_budget,
                        chrome.ticker,
                        max_travel,
                        title_scrolling,
                    ),
                    emphasis(identifier_style),
                ));
            } else {
                spans.extend(fitted_spans(
                    glyph,
                    (&row.label, emphasis(identifier_style)),
                    &[(row.detail.as_str(), identifier_style)],
                    p,
                    budget.saturating_sub(prefix_width),
                ));
            }
        }
        RowKind::Machine => {
            let prefix = nest_prefix(row);
            spans.push(Span::styled(prefix, prefix_style));
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[(row.detail.as_str(), secondary_style)],
                p,
                budget.saturating_sub(display_width(prefix)),
            ));
        }
        RowKind::Terminal => {
            let prefix = nest_prefix(row);
            spans.push(Span::styled(prefix, prefix_style));
            spans.extend(fitted_spans(
                glyph,
                (&row.label, title_style),
                &[],
                p,
                budget.saturating_sub(display_width(prefix)),
            ));
            // The name outranks the address: it takes the right edge
            // only with a cell to spare after the name.
            let used: usize = spans.iter().map(|span| display_width(&span.content)).sum();
            let address_width = display_width(&row.address);
            if !row.address.is_empty() && used + 1 + address_width <= usize::from(width) {
                let pad = usize::from(width) - used - address_width;
                spans.push(Span::raw(" ".repeat(pad)));
                spans.push(Span::styled(row.address.as_str(), secondary_style));
            }
        }
        RowKind::Agent => {
            let prefix = nest_prefix(row);
            spans.push(Span::styled(prefix, prefix_style));
            let budget = budget.saturating_sub(display_width(prefix));
            spans.push(Span::styled(glyph.0.to_string(), glyph.1));
            if budget > 1 {
                spans.push(Span::raw(" "));
                let budget = budget - 2;
                let definition = (row.definition.as_str(), title_style);
                let pinned = pinned_width(row);
                if pinned < budget {
                    // `{ref}: ` stays put and a title the row cannot hold,
                    // such as a lane seat's manual title, scrolls after it
                    // on the shared clock like the task line.
                    if pinned > 0 {
                        spans.push(Span::styled(row.reference.as_str(), identifier_style));
                        spans.push(Span::styled(": ", secondary_style));
                    }
                    spans.extend(ticker_spans(
                        &[definition],
                        budget - pinned,
                        chrome.ticker,
                        max_travel,
                        title_scrolling,
                    ));
                } else {
                    let segments = [
                        (row.reference.as_str(), identifier_style),
                        (": ", secondary_style),
                        definition,
                    ];
                    let name: String = segments.iter().map(|(text, _)| *text).collect();
                    spans.extend(split_segments(&truncate_end(&name, budget), &segments));
                }
            }
        }
        RowKind::Group => {
            let name = truncate_end(&row.label, budget.saturating_sub(2));
            let rule = budget.saturating_sub(display_width(&name) + 1);
            spans.push(Span::styled(name, project_style));
            spans.push(Span::styled(format!(" {}", "─".repeat(rule)), prefix_style));
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
    let paren_style = Style::default().fg(p.subtext0);
    let branch_style = Style::default().fg(p.identifier);
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
            Style::default().fg(p.subtext0),
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

/// Cells a scrolling title overruns its marquee budget by at `width`: the
/// longer of an agent's title after its pinned `{ref}: ` and its task line,
/// or a worktree name too long for its row. Zero when it fits or the row
/// never scrolls. The longest overrun among the rows drawn together sets
/// their shared period.
pub fn row_travel(row: &SidebarRow, width: u16) -> usize {
    match row.kind {
        RowKind::Agent => {
            let budget = usize::from(width).saturating_sub(3 + display_width(nest_prefix(row)));
            let name = overrun(
                display_width(&row.definition),
                budget.saturating_sub(pinned_width(row)),
            );
            let task = task_line(row).map_or(0, |(lead, title)| {
                overrun(display_width(&lead) + display_width(&title), budget)
            });
            name.max(task)
        }
        RowKind::Worktree => overrun(display_width(&row.label), worktree_name_budget(row, width)),
        _ => 0,
    }
}

/// Cells `text_width` overruns a marquee `window` by; zero when it fits or
/// the window is too narrow to scroll.
fn overrun(text_width: usize, window: usize) -> usize {
    if window < TICKER_MIN_WINDOW {
        return 0;
    }
    text_width.saturating_sub(window)
}

/// Cells an agent's pinned `{ref}: ` takes on line one; zero without a ref.
fn pinned_width(row: &SidebarRow) -> usize {
    if row.reference.is_empty() {
        0
    } else {
        display_width(&row.reference) + 2
    }
}

/// An agent's second line, `Working task 22944` then ` Title`: the number
/// is the task ref after its last `#`. The whole line tickers as one string
/// and each part keeps its own role.
fn task_line(row: &SidebarRow) -> Option<(String, String)> {
    let (reference, title) = row.task.as_ref()?;
    let number = reference.rsplit('#').next().unwrap_or(reference);
    Some((format!("Working task {number}"), format!(" {title}")))
}

/// The `budget`-cell window of `text` the marquee shows at `ticker`: the
/// whole text while it fits, else a slice that rests at the start for
/// `TICKER_PAUSE` steps, walks one cell per `TICKER_STEP` ticks to the end
/// and parks there until the period ends, then jumps home; `Right` runs the
/// same path mirrored, from the end back to the start. The period is the
/// longest overrun drawn beside it (`max_travel`, at least its own) plus
/// the two pauses, so every scrolling title, sidebar row and pane header
/// alike, moves and restarts as one object (D7). A scrolling window is
/// exactly `budget` cells: a wide character cut by either edge leaves its
/// cell blank. `Off`, or a budget under `TICKER_MIN_WINDOW`, truncates.
pub fn ticker_window(
    text: &str,
    budget: usize,
    ticker: u64,
    max_travel: usize,
    direction: TitleScrolling,
) -> String {
    ticker_spans(
        &[(text, Style::default())],
        budget,
        ticker,
        max_travel,
        direction,
    )
    .into_iter()
    .map(|span| span.content)
    .collect()
}

/// `ticker_window` over styled segments joined into one string: every cell
/// the window shows keeps its own segment's style, so a scrolling line
/// keeps its roles.
pub(crate) fn ticker_spans(
    segments: &[(&str, Style)],
    budget: usize,
    ticker: u64,
    max_travel: usize,
    direction: TitleScrolling,
) -> Vec<Span<'static>> {
    let text: String = segments.iter().map(|(segment, _)| *segment).collect();
    let width = display_width(&text);
    if width <= budget || budget < TICKER_MIN_WINDOW || direction == TitleScrolling::Off {
        return split_segments(&truncate_end(&text, budget), segments);
    }
    let travel = (width - budget) as u64;
    let step = ticker / TICKER_STEP;
    // One pass per shared period: rest at the start, walk to the end, rest
    // there, then back to the start while a longer title finishes its pass.
    let period = travel.max(max_travel as u64) + 2 * TICKER_PAUSE;
    let at = step % period;
    let walked = if at < travel + 2 * TICKER_PAUSE {
        at.saturating_sub(TICKER_PAUSE).min(travel)
    } else {
        0
    };
    let offset = match direction {
        TitleScrolling::Off | TitleScrolling::Left => walked,
        TitleScrolling::Right => travel - walked,
    } as usize;
    let mut skipped = 0;
    let mut taken = 0;
    let mut window: Vec<Span<'static>> = Vec::new();
    let cells = segments
        .iter()
        .flat_map(|(segment, style)| segment.chars().map(move |ch| (ch, *style)));
    for (ch, style) in cells {
        let cell = ch.width().unwrap_or(0);
        // A zero-width mark belongs to the character before it, so one
        // after a character left of the window stays out with it.
        if skipped < offset || (cell == 0 && window.is_empty()) {
            skipped += cell;
            continue;
        }
        if window.is_empty() && skipped > offset {
            taken = skipped - offset;
            push_styled(&mut window, &" ".repeat(taken), style);
        }
        if taken + cell > budget {
            break;
        }
        taken += cell;
        push_styled(&mut window, ch.encode_utf8(&mut [0; 4]), style);
    }
    let trailing = window.last().map_or(Style::default(), |span| span.style);
    push_styled(&mut window, &" ".repeat(budget - taken), trailing);
    window
}

/// Append `text` to the last span when it shares `style`, else open a span.
fn push_styled(spans: &mut Vec<Span<'static>>, text: &str, style: Style) {
    if text.is_empty() {
        return;
    }
    match spans.last_mut() {
        Some(last) if last.style == style => last.content.to_mut().push_str(text),
        _ => spans.push(Span::styled(text.to_string(), style)),
    }
}

/// `text`, the joined `segments` cut down from the end (an ellipsis may
/// close it), split back into one span per segment in that segment's style.
/// A cut segment keeps the ellipsis; the segments after it drop out.
fn split_segments(text: &str, segments: &[(&str, Style)]) -> Vec<Span<'static>> {
    let mut rest = text;
    let mut spans = Vec::new();
    for (index, (segment, style)) in segments.iter().enumerate() {
        let take = if index + 1 == segments.len() {
            rest.len()
        } else {
            rest.char_indices()
                .nth(segment.chars().count())
                .map_or(rest.len(), |(at, _)| at)
        };
        let (head, tail) = rest.split_at(take);
        push_styled(&mut spans, head, *style);
        rest = tail;
    }
    spans
}

/// The fixed task reference and scrolling title, or dim empty-task label,
/// under an agent definition. A terminal shows its working directory here.
pub fn row_second_line<'a>(row: &'a SidebarRow, width: u16, chrome: &Chrome) -> Line<'a> {
    row_second_line_with_travel(row, width, chrome, chrome.view.title_travel)
}

pub(crate) fn row_second_line_with_travel<'a>(
    row: &'a SidebarRow,
    width: u16,
    chrome: &Chrome,
    max_travel: usize,
) -> Line<'a> {
    let indent = 3 + display_width(nest_prefix(row));
    let budget = usize::from(width).saturating_sub(indent);
    let mut spans = vec![Span::raw(" ".repeat(indent.min(usize::from(width))))];
    let style = Style::default().fg(chrome.palette.subtext0);
    match row.kind {
        RowKind::Agent => {
            if let Some((lead, title)) = task_line(row) {
                spans.extend(ticker_spans(
                    &[
                        (&lead, Style::default().fg(chrome.palette.text)),
                        (&title, style),
                    ],
                    budget,
                    chrome.ticker,
                    max_travel,
                    chrome.prefs.title_scrolling,
                ));
            } else {
                spans.push(Span::styled(
                    truncate_end("No assigned task", budget),
                    style,
                ));
            }
        }
        RowKind::Terminal => spans.push(Span::styled(truncate_end(&row.detail, budget), style)),
        _ => return Line::default(),
    }
    Line::from(spans)
}

pub fn row_third_line<'a>(row: &'a SidebarRow, width: u16, chrome: &Chrome) -> Line<'a> {
    if row.kind != RowKind::Agent {
        return Line::default();
    }
    let indent = 3 + display_width(nest_prefix(row));
    let budget = usize::from(width).saturating_sub(indent);
    Line::from(vec![
        Span::raw(" ".repeat(indent.min(usize::from(width)))),
        // The theme's model colour, a muted hue no state uses (the
        // model-line board, #23416).
        Span::styled(
            truncate_end(&row.model_slug, budget),
            Style::default().fg(chrome.palette.model),
        ),
    ])
}

/// herdr `resolved_token_spans`, reduced to the glyph + title + trailing
/// shape: `" "` after the glyph, `" · "` between text tokens. Trailing tokens
/// are kept from the left while they fit beside the whole title and dropped
/// from the right otherwise; the title truncates only once it stands alone.
/// Terminal and machine titles outrank their detail tokens here; agent
/// definitions and references use their own layout above. Empty tokens are
/// elided with their separators.
pub fn fitted_spans(
    glyph: (&str, Style),
    title: (&str, Style),
    trailing: &[(&str, Style)],
    p: &Palette,
    max_width: usize,
) -> Vec<Span<'static>> {
    let separator_style = Style::default().fg(p.subtext0);
    let spacer_style = Style::default().fg(p.overlay0).add_modifier(Modifier::DIM);
    let mut spans = vec![Span::styled(glyph.0.to_string(), glyph.1)];
    let remaining = max_width.saturating_sub(display_width(glyph.0));
    if remaining < 2 || title.0.is_empty() {
        return spans;
    }
    spans.push(Span::styled(" ", spacer_style));
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
