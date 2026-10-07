// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar: machines, project cards, agents and bare terminals, each under a
//! one-row band.
//!
//! The sidebar sits on the ground with no fill and no separator
//! glyph; herdr's edge column stays reserved as the drag lane of a pinned
//! sidebar. The section rules herdr let the user drag are gone: the
//! machines take up to `MACHINES_MAX_ROWS`, the projects what their cards
//! need within the top half, and agents and terminals share the rest.

pub mod agents;
pub mod machines;
pub mod projects;
pub mod terminals;

use crate::theme::{FillInk, Palette};
use crate::ui::chrome::{Chrome, Mode, RowState, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::scrollbar::{render_scrollbar, scrolled_recently, should_show_scrollbar};
use crate::ui::settings::SidebarSide;
use crate::ui::settings::TitleScrolling;
use crate::ui::sidebar_rows::{
    project_rows, row_line_with_scrolling, row_second_line_with_travel, row_third_line, row_travel,
    worktree_glyph_offset, RowKind, SidebarRow,
};
use crate::ui::text::{display_width_u16, truncate_end};
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;

pub use agents::{
    agent_blocked, agent_label, agent_rows, attention_order, machine_admits, next_machine_filter,
    ALL_MACHINES, TERMINAL_ROW,
};
pub use machines::{local_hostname, machine_rows};
pub use projects::project_list_metrics;
pub use terminals::terminal_rows;

/// Rows the machines section lists before it scrolls.
pub const MACHINES_MAX_ROWS: u16 = 4;
/// Every section band is one row.
pub const BAND_ROWS: u16 = 1;
/// The blank row above the projects and agents bands (D6). It belongs to
/// the rows above it, so the top-half cap on the machines and the cards is
/// unchanged.
pub const GAP_ROWS: u16 = 1;

/// Rects the sidebar renderers drew, for `ViewState`.
#[derive(Debug, Clone, Default)]
pub struct SidebarHits {
    /// Machine rows, by machine id.
    pub machines: Vec<(String, Rect)>,
    /// Project cards, by project id.
    pub projects: Vec<(String, Rect)>,
    /// Worktree rows, by worktree id.
    pub worktrees: Vec<(String, Rect)>,
    /// The state dot of each worktree row with an agent bound, by worktree id.
    pub worktree_glyphs: Vec<(String, Rect)>,
    /// The `▾`/`▸` cell of each card that has worktrees, by project id.
    pub group_toggles: Vec<(String, Rect)>,
    /// Session, agent run and bare terminal rows, by entry id (both lines).
    pub agents: Vec<(String, Rect)>,
    /// Scrollbar lane beside each section that overflowed, by
    /// `SidebarSection::index`.
    pub scrollbars: [Option<Rect>; 4],
}

/// Where the sidebar's sections go, without the separator column.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SidebarLayout {
    /// The sections' rects, band, body and blank row, by
    /// `SidebarSection::index`.
    pub sections: [Rect; 4],
}

/// The layout of `area`, from its first row to its last: the machines band
/// with up to `MACHINES_MAX_ROWS` of its `machine_rows`, the projects band
/// with its `project_rows` while the two stay within the top half, and the
/// agents and terminals with everything left, the projects and agents bands
/// each under a blank row. With no `terminal_rows` the terminals section is
/// gone and the agents take the rest. A section short of its rows scrolls;
/// one with no room at all is empty.
pub fn sidebar_layout(
    area: Rect,
    side: SidebarSide,
    machine_rows: u16,
    project_rows: u16,
    terminal_rows: u16,
) -> SidebarLayout {
    // The edge column faces the content: last on the left, first on the right.
    let body_x = match side {
        SidebarSide::Left => area.x,
        SidebarSide::Right => area.x.saturating_add(1),
    };
    let content = Rect::new(body_x, area.y, area.width.saturating_sub(1), area.height);
    let mut layout = SidebarLayout::default();
    if content.width == 0 || content.height == 0 {
        return layout;
    }
    // The machines and the cards keep their blank rows inside their shares,
    // so the top-half cap holds.
    let rows = content.height;
    let top = rows / 2;
    let machines = (BAND_ROWS + machine_rows.min(MACHINES_MAX_ROWS) + GAP_ROWS).min(top);
    let projects = (BAND_ROWS + GAP_ROWS)
        .saturating_add(project_rows)
        .min(top - machines);
    let remaining = rows - machines - projects;
    // With no bare terminal the Terminals section, heading included, is
    // gone and the agents take the rest.
    let terminals = if remaining < 2 || terminal_rows == 0 {
        0
    } else {
        remaining / 2
    };
    let agents = remaining - terminals;
    let mut y = content.y;
    for (index, height) in [machines, projects, agents, terminals]
        .into_iter()
        .enumerate()
    {
        layout.sections[index] = Rect::new(content.x, y, content.width, height);
        y += height;
    }
    layout
}

/// The section rects the sidebar draws into `area` for the workspace as it
/// stands, for `ViewState::sidebar_section_rects`.
pub fn section_rects<W: WorkspaceView>(ws: &W, chrome: &Chrome, area: Rect) -> [Rect; 4] {
    let machines = rows_u16(machine_rows(ws, chrome).len());
    let projects = rows_u16(
        project_rows(ws, chrome)
            .iter()
            .map(|row| usize::from(row.height()))
            .sum(),
    );
    let terminals = rows_u16(terminal_rows(ws, chrome).len());
    sidebar_layout(area, chrome.sidebar.side, machines, projects, terminals).sections
}

fn rows_u16(rows: usize) -> u16 {
    u16::try_from(rows).unwrap_or(u16::MAX)
}

pub fn render_sidebar<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
) -> SidebarHits {
    let mut hits = SidebarHits::default();
    if area.width == 0 || area.height == 0 {
        return hits;
    }
    let is_navigating = chrome.mode == Mode::Navigate;
    let machines = machine_rows(ws, chrome);
    let mut projects = project_rows(ws, chrome);
    if !is_navigating {
        for row in &mut projects {
            row.selected = false;
        }
    }
    let agents = agent_rows(ws, chrome);
    let terminals = terminal_rows(ws, chrome);
    let layout = sidebar_layout(
        area,
        chrome.sidebar.side,
        rows_u16(machines.len()),
        rows_u16(projects.iter().map(|row| usize::from(row.height())).sum()),
        rows_u16(terminals.len()),
    );
    machines::render_machines(frame, layout.sections[0], &machines, chrome, &mut hits);
    projects::render_projects(frame, layout.sections[1], &projects, chrome, &mut hits);
    agents::render_agents(frame, layout.sections[2], &agents, chrome, &mut hits);
    terminals::render_terminals(frame, layout.sections[3], &terminals, chrome, &mut hits);
    hits
}

/// Scroll metrics for a list of `len` one-row entries in a `viewport`-row
/// body, clamping the requested top row to the last page.
pub fn list_metrics(len: usize, viewport: u16, requested: usize) -> ScrollMetrics {
    let viewport = usize::from(viewport);
    let max_scroll = len.saturating_sub(viewport);
    let scroll = requested.min(max_scroll);
    ScrollMetrics {
        offset_from_bottom: max_scroll.saturating_sub(scroll),
        max_offset_from_bottom: max_scroll,
        viewport_rows: viewport.min(len),
    }
}

/// The rows `section` lists: the machines, the project cards with their
/// worktrees, or the sessions.
pub fn section_rows<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    section: SidebarSection,
) -> Vec<SidebarRow> {
    match section {
        SidebarSection::Machines => machine_rows(ws, chrome),
        SidebarSection::Projects => project_rows(ws, chrome),
        SidebarSection::Agents => agent_rows(ws, chrome),
        SidebarSection::Terminals => terminal_rows(ws, chrome),
    }
}

/// Scroll metrics of `section` as the last frame laid it out: its rows
/// against its body height and the section's scroll position.
pub fn section_metrics<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    section: SidebarSection,
) -> ScrollMetrics {
    let area = chrome.view.sidebar_section_rects[section.index()];
    let heights: Vec<u16> = section_rows(ws, chrome, section)
        .iter()
        .map(SidebarRow::height)
        .collect();
    project_list_metrics(
        &heights,
        section_body_rect(area, section, false).height,
        chrome.sidebar.scroll(section),
    )
}

/// Draw a section heading into `rect`'s first row: a full-width band on the
/// theme's header fill, with the title at column 1 in bold subtext0, or the
/// first ink legible on that fill (#23416).
pub(super) fn render_band(frame: &mut Frame, rect: Rect, title: &str, palette: &Palette) {
    let rect = Rect::new(rect.x, rect.y, rect.width, rect.height.min(BAND_ROWS));
    if rect.width < 3 || rect.height == 0 {
        return;
    }
    frame.render_widget(
        Block::default().style(Style::default().bg(palette.band)),
        rect,
    );
    let title = truncate_end(title, usize::from(rect.width) - 2);
    let title_rect = Rect::new(rect.x + 1, rect.y, display_width_u16(&title), 1);
    let style = Style::default()
        .fg(palette.band_ink.ink(palette.subtext0))
        .bg(palette.band)
        .add_modifier(Modifier::BOLD);
    frame.render_widget(Paragraph::new(Span::styled(title, style)), title_rect);
}

/// Draw `rows` as `section`'s list under its band, packed from the
/// section's scroll position and clipped to whole rows, with the scrollbar
/// lane when they overflow, and record each row's rect by its kind.
pub(super) fn render_section_rows(
    frame: &mut Frame,
    area: Rect,
    section: SidebarSection,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let p = &chrome.palette;
    let (metrics, body) = section_list(area, section, rows, chrome);
    let has_scrollbar = should_show_scrollbar(metrics);
    // One marquee clock for every scrolling title, pane headers included:
    // the longest overrun sets the period (`ViewState::title_travel`).
    let max_travel = chrome.view.title_travel.max(list_travel(rows, body));
    if body.width > 0 && body.height > 0 {
        let scroll = metrics
            .max_offset_from_bottom
            .saturating_sub(metrics.offset_from_bottom);
        let mut y = body.y;
        for row in rows.iter().skip(scroll) {
            let height = row.height();
            if y + height > body.bottom() {
                break;
            }
            // The selected and the active row share the selection fill, the
            // one row fill a theme sets apart from its header rows.
            let on_selection = row.selected || row.active;
            let row_style = if on_selection {
                Style::default().bg(p.selection)
            } else {
                Style::default()
            };
            let title_scrolling = if section == SidebarSection::Terminals {
                TitleScrolling::Off
            } else {
                chrome.prefs.title_scrolling
            };
            frame.render_widget(
                Paragraph::new(row_line_with_scrolling(
                    row,
                    body.width,
                    chrome,
                    max_travel,
                    title_scrolling,
                ))
                .style(row_style),
                Rect::new(body.x, y, body.width, 1),
            );
            if height > 1 {
                frame.render_widget(
                    Paragraph::new(row_second_line_with_travel(
                        row, body.width, chrome, max_travel,
                    ))
                    .style(row_style),
                    Rect::new(body.x, y + 1, body.width, 1),
                );
            }
            if height > 2 {
                frame.render_widget(
                    Paragraph::new(row_third_line(row, body.width, chrome)).style(row_style),
                    Rect::new(body.x, y + 2, body.width, 1),
                );
            }
            let rect = Rect::new(body.x, y, body.width, height);
            if on_selection {
                ink_fill(frame, rect, &p.selection_ink);
            }
            match row.kind {
                RowKind::Project => {
                    hits.projects.push((row.id.clone(), rect));
                    if row.group.is_some() {
                        hits.group_toggles.push((
                            row.id.clone(),
                            Rect::new(body.right().saturating_sub(1), y, 1, 1),
                        ));
                    }
                }
                RowKind::Worktree => {
                    hits.worktrees.push((row.id.clone(), rect));
                    // An unbound worktree draws no dot, so it has no dot hit.
                    let x = body.x.saturating_add(worktree_glyph_offset(row));
                    if row.state != RowState::Unknown && x < body.right() {
                        hits.worktree_glyphs
                            .push((row.id.clone(), Rect::new(x, y, 1, 1)));
                    }
                }
                RowKind::Agent | RowKind::Terminal => hits.agents.push((row.id.clone(), rect)),
                RowKind::Machine => hits.machines.push((row.id.clone(), rect)),
                RowKind::Group => {}
            }
            y += height;
        }
    }
    if has_scrollbar {
        let track = scrollbar_track(area, body);
        // No track: the thumb draws in the dim token at rest, and in
        // overlay0 for a second after its band scrolls or while navigate
        // mode's cursor is in it.
        let lit = scrolled_recently(chrome.sidebar.scrolled_at[section.index()])
            || (chrome.mode == Mode::Navigate && rows.iter().any(|row| row.selected));
        let thumb = if lit { p.overlay0 } else { p.dim };
        render_scrollbar(frame, metrics, track, None, thumb, "▕");
        hits.scrollbars[section.index()] = Some(track);
    }
}

/// Swap each colour drawn on `ink`'s fill inside `rect` for the one legible
/// there (`FillInk::ink`); cells on any other fill keep theirs.
fn ink_fill(frame: &mut Frame, rect: Rect, ink: &FillInk) {
    let buffer = frame.buffer_mut();
    for y in rect.top()..rect.bottom() {
        for x in rect.left()..rect.right() {
            let cell = &mut buffer[(x, y)];
            if cell.bg == ink.fill {
                cell.fg = ink.ink(cell.fg);
            }
        }
    }
}

/// `rows` in `section`'s `area`: their scroll metrics, and the body they
/// draw into, less the scrollbar lane when they overflow.
fn section_list(
    area: Rect,
    section: SidebarSection,
    rows: &[SidebarRow],
    chrome: &Chrome,
) -> (ScrollMetrics, Rect) {
    let heights: Vec<u16> = rows.iter().map(SidebarRow::height).collect();
    let viewport = section_body_rect(area, section, false).height;
    let metrics = project_list_metrics(&heights, viewport, chrome.sidebar.scroll(section));
    let body = section_body_rect(area, section, should_show_scrollbar(metrics));
    (metrics, body)
}

fn list_travel(rows: &[SidebarRow], body: Rect) -> usize {
    rows.iter()
        .map(|row| row_travel(row, body.width))
        .max()
        .unwrap_or(0)
}

/// The longest overrun of a scrolling sidebar title, for
/// `ViewState::title_travel`: an Agents title or task line or a Projects
/// worktree name, each measured in its section of `rects`.
pub fn title_travel<W: WorkspaceView>(ws: &W, chrome: &Chrome, rects: &[Rect; 4]) -> usize {
    [SidebarSection::Projects, SidebarSection::Agents]
        .into_iter()
        .map(|section| {
            let rows = section_rows(ws, chrome, section);
            let (_, body) = section_list(rects[section.index()], section, &rows, chrome);
            list_travel(&rows, body)
        })
        .max()
        .unwrap_or(0)
}

/// The scrollbar lane: the section's last column beside `body`.
fn scrollbar_track(area: Rect, body: Rect) -> Rect {
    Rect::new(
        area.x + area.width.saturating_sub(1),
        body.y,
        1,
        body.height,
    )
}

/// The rows a section's list draws into inside its `area`: under the band
/// and above the section's blank row.
pub fn section_body_rect(area: Rect, section: SidebarSection, has_scrollbar: bool) -> Rect {
    if area.width == 0 || area.height <= BAND_ROWS {
        return Rect::default();
    }
    // A share too small for a row and the blank row keeps the row.
    let rows = area.height - BAND_ROWS;
    let gap = section_gap_rows(section);
    let height = if rows > gap { rows - gap } else { rows };
    Rect::new(
        area.x,
        area.y + BAND_ROWS,
        area.width.saturating_sub(u16::from(has_scrollbar)),
        height,
    )
}

/// The blank row a section keeps under its rows, above the next band. The
/// terminals end at the sidebar's last row and keep none.
pub fn section_gap_rows(section: SidebarSection) -> u16 {
    match section {
        SidebarSection::Machines | SidebarSection::Projects | SidebarSection::Agents => GAP_ROWS,
        SidebarSection::Terminals => 0,
    }
}

#[cfg(test)]
#[path = "sidebar/tests.rs"]
mod tests;
