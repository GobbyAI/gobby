// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar: the machines, the project cards and the sessions, each under a
//! one-row band.
//!
//! herdr geometry kept where it still applies: a `│` separator column on
//! the right. The section rules herdr let the user drag are gone: the
//! machines take up to `MACHINES_MAX_ROWS`, the projects what their cards
//! need within the top half, and the sessions everything left
//! (`sidebar_layout`).

pub mod machines;
pub mod projects;
pub mod sessions;

use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::sidebar_rows::{
    project_rows, row_line, row_second_line, row_travel, RowKind, SidebarRow,
};
use crate::ui::text::{display_width, display_width_u16, truncate_end};
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;

pub use machines::{local_hostname, machine_rows};
pub use projects::{project_list_metrics, projects_filter_label};
pub use sessions::{
    agent_blocked, agent_label, attention_order, machine_admits, next_machine_filter, session_rows,
    ALL_MACHINES, TERMINAL_ROW, VIEW_LABEL,
};

/// Rows the machines section lists before it scrolls.
pub const MACHINES_MAX_ROWS: u16 = 4;
/// Every section band is one row.
pub const BAND_ROWS: u16 = 1;
/// The blank row above the projects and sessions bands (D6). It belongs to
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
    /// The `▾`/`▸` cell of each card that has worktrees, by project id.
    pub group_toggles: Vec<(String, Rect)>,
    /// The projects band's `[working]`/`[all]`.
    pub projects_filter: Option<Rect>,
    /// The sessions band's `[view]`.
    pub sessions_view: Option<Rect>,
    /// Session, agent run and bare terminal rows, by entry id (both lines).
    pub agents: Vec<(String, Rect)>,
    /// Scrollbar lane beside each section that overflowed, by
    /// `SidebarSection::index`.
    pub scrollbars: [Option<Rect>; 3],
}

/// Where the sidebar's sections go, without the separator column.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SidebarLayout {
    /// The sections' rects, band, body and blank row, by
    /// `SidebarSection::index`.
    pub sections: [Rect; 3],
}

/// The layout of `area`, from its first row to its last: the machines band
/// with up to `MACHINES_MAX_ROWS` of its `machine_rows`, the projects band
/// with its `project_rows` while the two stay within the top half, and the
/// sessions with everything left, the projects and sessions bands each
/// under a blank row. A section short of its rows scrolls; one with no room
/// at all is empty.
pub fn sidebar_layout(area: Rect, machine_rows: u16, project_rows: u16) -> SidebarLayout {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
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
    let sessions = rows - machines - projects;
    let mut y = content.y;
    for (index, height) in [machines, projects, sessions].into_iter().enumerate() {
        layout.sections[index] = Rect::new(content.x, y, content.width, height);
        y += height;
    }
    layout
}

/// The section rects the sidebar draws into `area` for the workspace as it
/// stands, for `ViewState::sidebar_section_rects`.
pub fn section_rects<W: WorkspaceView>(ws: &W, chrome: &Chrome, area: Rect) -> [Rect; 3] {
    let machines = rows_u16(machine_rows(ws, chrome).len());
    let projects = rows_u16(
        project_rows(ws, chrome)
            .iter()
            .map(|row| usize::from(row.height()))
            .sum(),
    );
    sidebar_layout(area, machines, projects).sections
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
    let p = &chrome.palette;
    let is_navigating = chrome.mode == Mode::Navigate;
    frame.render_widget(
        Block::default().style(Style::default().bg(p.panel_bg)),
        area,
    );
    draw_separator_column(
        frame,
        area,
        if is_navigating {
            p.accent
        } else {
            p.surface_dim
        },
    );

    let machines = machine_rows(ws, chrome);
    let mut projects = project_rows(ws, chrome);
    if !is_navigating {
        for row in &mut projects {
            row.selected = false;
        }
    }
    let sessions = session_rows(ws, chrome);
    let layout = sidebar_layout(
        area,
        rows_u16(machines.len()),
        rows_u16(projects.iter().map(|row| usize::from(row.height())).sum()),
    );
    machines::render_machines(frame, layout.sections[0], &machines, chrome, &mut hits);
    projects::render_projects(frame, layout.sections[1], &projects, chrome, &mut hits);
    sessions::render_sessions(frame, layout.sections[2], &sessions, chrome, &mut hits);
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
        SidebarSection::Sessions => session_rows(ws, chrome),
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

/// Colours of a section band: `surface0`.
#[derive(Debug, Clone, Copy)]
pub(super) struct BandStyle {
    bg: Color,
    title: Style,
    control: Style,
}

impl BandStyle {
    pub(super) fn section(p: &Palette) -> Self {
        Self {
            bg: p.surface0,
            title: Style::default()
                .fg(p.subtext0)
                .bg(p.surface0)
                .add_modifier(Modifier::BOLD),
            control: Style::default().fg(p.overlay0).bg(p.surface0),
        }
    }
}

/// Draw a band into `rect`'s first row: the title at column 1 and the
/// bracketed `controls` right-aligned from the last column but one, one
/// blank apart. Controls that would run into the whole title drop from the
/// right. Returns the title's rect and the drawn controls' rects, in
/// `controls` order.
pub(super) fn render_band(
    frame: &mut Frame,
    rect: Rect,
    title: &str,
    controls: &[&str],
    style: BandStyle,
) -> (Rect, Vec<Rect>) {
    let rect = Rect::new(rect.x, rect.y, rect.width, rect.height.min(BAND_ROWS));
    if rect.width < 3 || rect.height == 0 {
        return (Rect::default(), Vec::new());
    }
    frame.render_widget(Block::default().style(Style::default().bg(style.bg)), rect);
    let width = usize::from(rect.width);
    let title_width = display_width(title);
    let mut kept: Vec<&str> = controls.to_vec();
    let needed = |kept: &[&str]| -> usize {
        kept.iter()
            .map(|control| 1 + display_width(control))
            .sum::<usize>()
            + 1
    };
    while !kept.is_empty() && 1 + title_width + needed(&kept) > width {
        kept.pop();
    }
    let title = truncate_end(title, width - 2);
    let title_rect = Rect::new(rect.x + 1, rect.y, display_width_u16(&title), 1);
    if title_rect.width > 0 {
        frame.render_widget(Paragraph::new(Span::styled(title, style.title)), title_rect);
    }
    let mut x = rect.right() - 1;
    let mut rects = vec![Rect::default(); kept.len()];
    for (index, control) in kept.iter().enumerate().rev() {
        let control_width = display_width_u16(control);
        x = x.saturating_sub(control_width);
        rects[index] = Rect::new(x, rect.y, control_width, 1);
        frame.render_widget(
            Paragraph::new(Span::styled(*control, style.control)),
            rects[index],
        );
        x = x.saturating_sub(1);
    }
    (title_rect, rects)
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
            let row_style = if row.selected {
                Style::default().bg(p.surface1)
            } else if row.active {
                Style::default().bg(p.surface_dim)
            } else {
                Style::default()
            };
            frame.render_widget(
                Paragraph::new(row_line(row, body.width, chrome, max_travel)).style(row_style),
                Rect::new(body.x, y, body.width, 1),
            );
            if height > 1 {
                frame.render_widget(
                    Paragraph::new(row_second_line(row, body.width, chrome)).style(row_style),
                    Rect::new(body.x, y + 1, body.width, 1),
                );
            }
            let rect = Rect::new(body.x, y, body.width, height);
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
                RowKind::Worktree => hits.worktrees.push((row.id.clone(), rect)),
                RowKind::Agent => hits.agents.push((row.id.clone(), rect)),
                RowKind::Machine => hits.machines.push((row.id.clone(), rect)),
                RowKind::Group => {}
            }
            y += height;
        }
    }
    if has_scrollbar {
        let track = scrollbar_track(area, body);
        render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
        hits.scrollbars[section.index()] = Some(track);
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

/// The longest overrun of a Sessions title drawn in the section `area`, the
/// only rows that scroll, for `ViewState::title_travel`.
pub fn sessions_title_travel<W: WorkspaceView>(ws: &W, chrome: &Chrome, area: Rect) -> usize {
    let rows = session_rows(ws, chrome);
    let (_, body) = section_list(area, SidebarSection::Sessions, &rows, chrome);
    list_travel(&rows, body)
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
/// sessions end at the sidebar's last row and keep none.
pub fn section_gap_rows(section: SidebarSection) -> u16 {
    match section {
        SidebarSection::Machines | SidebarSection::Projects => GAP_ROWS,
        SidebarSection::Sessions => 0,
    }
}

fn draw_separator_column(frame: &mut Frame, area: Rect, color: Color) {
    let x = area.x + area.width.saturating_sub(1);
    let buf = frame.buffer_mut();
    for y in area.y..area.y + area.height {
        buf[(x, y)].set_symbol("│");
        buf[(x, y)].set_style(Style::default().fg(color));
    }
}

#[cfg(test)]
#[path = "sidebar/tests.rs"]
mod tests;
