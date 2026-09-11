// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar: the menu band, then the machines, the project cards and the
//! sessions, each under a one-row band, and the footer band with the
//! collapse control; plus the collapsed rail.
//!
//! herdr geometry kept where it still applies: a `│` separator column on
//! the right and the rail stacking the lists around `─` rules. The section
//! rules herdr let the user drag are gone: the machines take up to
//! `MACHINES_MAX_ROWS`, the projects what their cards need within the top
//! half, and the sessions everything left (`sidebar_layout`).

pub mod machines;
pub mod projects;
pub mod sessions;

use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::sidebar_rows::{project_rows, row_line, row_second_line, RowKind, SidebarRow};
use crate::ui::status::state_dot;
use crate::ui::text::{display_width, display_width_u16, truncate_end};
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;

pub use machines::{local_hostname, machine_rows};
pub use projects::{project_list_metrics, projects_filter_label};
pub use sessions::{
    agent_blocked, agent_label, attention_order, machine_admits, next_machine_filter, session_rows,
    sessions_scope_label, ALL_MACHINES, TERMINAL_ROW,
};

/// Rows the machines section lists before it scrolls.
pub const MACHINES_MAX_ROWS: u16 = 4;
/// Every band is one row: the menu band, a section's band, the footer band.
pub const BAND_ROWS: u16 = 1;
/// Rows the collapsed rail needs before it draws its rules: one machine
/// row, two rules, one row per list and the toggle row.
const RAIL_MIN_ROWS: u16 = 6;
/// The menu band's controls.
pub const MENU_LABEL: &str = "[Menu]";
pub const NEW_LABEL: &str = "[+]";
/// The footer band's collapse control, and the rail's expand cell.
pub const COLLAPSE_LABEL: &str = "[«]";
const EXPAND_LABEL: &str = "»";

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
    /// The menu band's `[+]` and `[Menu]`.
    pub projects_new: Option<Rect>,
    pub projects_menu: Option<Rect>,
    /// The projects band's `[working]`/`[all]`.
    pub projects_filter: Option<Rect>,
    /// The sessions band's `[project]`/`[all]`.
    pub sessions_scope: Option<Rect>,
    /// The sessions band's `[grouped]`/`[priority]`.
    pub agent_sort: Option<Rect>,
    /// Session, agent run and bare terminal rows, by entry id (both lines).
    pub agents: Vec<(String, Rect)>,
    /// Scrollbar lane beside each section that overflowed, by
    /// `SidebarSection::index`.
    pub scrollbars: [Option<Rect>; 3],
    /// The `[«]` control of the footer band, or the rail's `»` cell.
    pub toggle: Option<Rect>,
}

/// Where the expanded sidebar's parts go, without the separator column.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SidebarLayout {
    pub menu: Rect,
    /// The sections' rects, band and body, by `SidebarSection::index`.
    pub sections: [Rect; 3],
    pub footer: Rect,
}

/// The expanded layout of `area`: the menu band on the first row and the
/// footer band on the last; between them the machines band with up to
/// `MACHINES_MAX_ROWS` of its `machine_rows`, the projects band with its
/// `project_rows` while the two stay within the top half, and the sessions
/// with everything left. A section short of its rows scrolls; one with no
/// room at all is empty.
pub fn sidebar_layout(area: Rect, machine_rows: u16, project_rows: u16) -> SidebarLayout {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    let mut layout = SidebarLayout::default();
    if content.width == 0 || content.height == 0 {
        return layout;
    }
    layout.menu = Rect::new(content.x, content.y, content.width, BAND_ROWS);
    if content.height < 2 * BAND_ROWS {
        return layout;
    }
    layout.footer = Rect::new(
        content.x,
        content.bottom() - BAND_ROWS,
        content.width,
        BAND_ROWS,
    );
    let rows = content.height - 2 * BAND_ROWS;
    let top = rows / 2;
    let machines = (BAND_ROWS + machine_rows.min(MACHINES_MAX_ROWS)).min(top);
    let projects = BAND_ROWS.saturating_add(project_rows).min(top - machines);
    let sessions = rows - machines - projects;
    let mut y = layout.menu.bottom();
    for (index, height) in [machines, projects, sessions].into_iter().enumerate() {
        layout.sections[index] = Rect::new(content.x, y, content.width, height);
        y += height;
    }
    layout
}

/// The section rects the sidebar draws into `area` for the workspace as it
/// stands, for `ViewState::sidebar_section_rects`.
pub fn section_rects<W: WorkspaceView>(ws: &W, chrome: &Chrome, area: Rect) -> [Rect; 3] {
    if chrome.sidebar.collapsed {
        return collapsed_sections(area).0;
    }
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
    let (menu, controls) = render_band(
        frame,
        layout.menu,
        MENU_LABEL,
        &[NEW_LABEL],
        BandStyle::menu(p),
    );
    hits.projects_menu = (menu.width > 0).then_some(menu);
    hits.projects_new = controls.first().copied();
    machines::render_machines(frame, layout.sections[0], &machines, chrome, &mut hits);
    projects::render_projects(frame, layout.sections[1], &projects, chrome, &mut hits);
    sessions::render_sessions(frame, layout.sections[2], &sessions, chrome, &mut hits);
    let (_, controls) = render_band(
        frame,
        layout.footer,
        "",
        &[COLLAPSE_LABEL],
        BandStyle::section(p),
    );
    hits.toggle = controls.first().copied();
    hits
}

/// The collapsed rail: a dot per machine, then the numbered project cards
/// and the numbered session rows, each list under a `─` rule.
pub fn render_collapsed_sidebar<W: WorkspaceView>(
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

    let (sections, dividers) = collapsed_sections(area);
    for section in SidebarSection::ALL {
        let rect = sections[section.index()];
        if rect == Rect::default() {
            continue;
        }
        let mut rows = section_rows(ws, chrome, section);
        let cards = section == SidebarSection::Projects;
        match section {
            // The rail numbers the cards; worktree rows stay behind them.
            SidebarSection::Projects => rows.retain(|row| row.kind == RowKind::Project),
            // The group headings have no row of their own.
            SidebarSection::Sessions => rows.retain(|row| row.kind == RowKind::Agent),
            SidebarSection::Machines => {}
        }
        let numbered = section != SidebarSection::Machines;
        let drawn = render_rail_list(frame, rect, &rows, numbered, cards, is_navigating, p);
        match section {
            SidebarSection::Machines => hits.machines = drawn,
            SidebarSection::Projects => hits.projects = drawn,
            SidebarSection::Sessions => hits.agents = drawn,
        }
    }
    let buf = frame.buffer_mut();
    for divider_y in dividers.into_iter().flatten() {
        for x in area.x..area.x + area.width.saturating_sub(1) {
            buf[(x, divider_y)].set_symbol("─");
            buf[(x, divider_y)].set_style(Style::default().fg(p.surface_dim));
        }
    }

    hits.toggle = render_toggle(frame, collapsed_toggle_rect(area), EXPAND_LABEL, p);
    hits
}

/// One rail row per entry of `rows`, as far as `rect` reaches: the list
/// position when `numbered` (herdr pads single digits and keeps the dot at
/// column 2 for two-digit positions, `10·`, instead of clipping it) and the
/// state dot. `highlight` paints the active card row, and the selected one
/// while `navigating`.
fn render_rail_list(
    frame: &mut Frame,
    rect: Rect,
    rows: &[SidebarRow],
    numbered: bool,
    highlight: bool,
    navigating: bool,
    p: &Palette,
) -> Vec<(String, Rect)> {
    let mut drawn = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        let y = rect.y + index as u16;
        if y >= rect.bottom() {
            break;
        }
        let (icon, icon_color) = state_dot(row.state, p);
        let (row_style, num_style) = if highlight && navigating && row.selected {
            (
                Style::default().bg(p.surface1),
                Style::default().fg(p.overlay1).bg(p.surface1),
            )
        } else if highlight && row.active {
            (
                Style::default().bg(p.surface_dim),
                Style::default().fg(p.text).bg(p.surface_dim),
            )
        } else {
            (Style::default(), Style::default().fg(p.overlay0))
        };
        let number = if numbered {
            format!("{:<2}", index + 1)
        } else {
            "  ".to_string()
        };
        let row_rect = Rect::new(rect.x, y, rect.width, 1);
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(number, num_style),
                Span::styled(icon, Style::default().fg(icon_color)),
            ]))
            .style(row_style),
            row_rect,
        );
        drawn.push((row.id.clone(), row_rect));
    }
    drawn
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
        section_body_rect(area, false).height,
        chrome.sidebar.scroll(section),
    )
}

/// Colours of a band: the menu band is the accent, section and footer
/// bands `surface0`.
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

    fn menu(p: &Palette) -> Self {
        let text = Style::default()
            .fg(p.panel_bg)
            .bg(p.accent)
            .add_modifier(Modifier::BOLD);
        Self {
            bg: p.accent,
            title: text,
            control: text,
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
    let heights: Vec<u16> = rows.iter().map(SidebarRow::height).collect();
    let viewport = section_body_rect(area, false).height;
    let metrics = project_list_metrics(&heights, viewport, chrome.sidebar.scroll(section));
    let has_scrollbar = should_show_scrollbar(metrics);
    let body = section_body_rect(area, has_scrollbar);
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
                Paragraph::new(row_line(row, body.width, chrome)).style(row_style),
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

/// The scrollbar lane: the section's last column beside `body`.
fn scrollbar_track(area: Rect, body: Rect) -> Rect {
    Rect::new(
        area.x + area.width.saturating_sub(1),
        body.y,
        1,
        body.height,
    )
}

/// The rail's three lists and the rules between them: one row of machine
/// dots, then the cards and the sessions sharing the rest, the remainder to
/// the cards, above the toggle row. Under `RAIL_MIN_ROWS` the cards take
/// every row and no rule is drawn.
pub fn collapsed_sections(area: Rect) -> ([Rect; 3], [Option<u16>; 2]) {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    let mut rects = [Rect::default(); 3];
    let mut dividers = [None; 2];
    if content.width == 0 || content.height == 0 {
        return (rects, dividers);
    }
    if content.height < RAIL_MIN_ROWS {
        rects[SidebarSection::Projects.index()] = content;
        return (rects, dividers);
    }
    // The machine row, two rules and the toggle row come off the top.
    let rest = content.height - 4;
    let (share, extra) = (rest / 2, rest % 2);
    let heights = [1, share + extra, share];
    let mut y = content.y;
    for (index, height) in heights.into_iter().enumerate() {
        if index > 0 {
            dividers[index - 1] = Some(y);
            y += 1;
        }
        rects[index] = Rect::new(content.x, y, content.width, height);
        y += height;
    }
    (rects, dividers)
}

/// The rows a section's list draws into inside its `area`: under the band.
pub fn section_body_rect(area: Rect, has_scrollbar: bool) -> Rect {
    if area.width == 0 || area.height <= BAND_ROWS {
        return Rect::default();
    }
    Rect::new(
        area.x,
        area.y + BAND_ROWS,
        area.width.saturating_sub(u16::from(has_scrollbar)),
        area.height - BAND_ROWS,
    )
}

/// The `»` cell: last row, centred in the rail's content columns.
pub fn collapsed_toggle_rect(area: Rect) -> Rect {
    let content_w = area.width.saturating_sub(1);
    if content_w == 0 || area.height == 0 {
        return Rect::default();
    }
    Rect::new(
        area.x + content_w / 2,
        area.y + area.height.saturating_sub(1),
        1,
        1,
    )
}

fn render_toggle(frame: &mut Frame, rect: Rect, icon: &str, p: &Palette) -> Option<Rect> {
    if rect == Rect::default() {
        return None;
    }
    frame.render_widget(
        Paragraph::new(Span::styled(icon, Style::default().fg(p.overlay0))),
        rect,
    );
    Some(rect)
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
