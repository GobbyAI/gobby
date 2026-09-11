// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar: the machines, the project cards, the interactive sessions and
//! the agent runs, top to bottom, plus the collapsed rail.
//!
//! herdr geometry kept as is: a `│` separator column on the right, a
//! two-row header on the first section (title, blank) and a three-row
//! header on the others (rule, title, blank), the projects footer row for
//! `new`/`menu`, the `«` toggle on the last row, and the collapsed rail
//! stacking the same four lists around `─` rules.

pub mod agents;
pub mod machines;
pub mod projects;

use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, SidebarState, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::sidebar_rows::{project_rows, row_line, row_second_line, RowKind, SidebarRow};
use crate::ui::status::state_dot;
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;
use std::ops::RangeInclusive;

pub use agents::{
    agent_blocked, agent_label, agent_rows, agent_section, attention_order, next_machine_filter,
    ALL_MACHINES,
};
pub use machines::{local_hostname, machine_rows, MACHINES_HEADER_ROWS};
pub use projects::project_list_metrics;

/// Rows of the machines section while its rule is unset: the header and
/// two rows.
const DEFAULT_MACHINES_ROWS: u16 = 4;
/// Projects share of the rows under the machines section while the
/// projects/sessions rule is unset (herdr's default section split).
const DEFAULT_PROJECTS_SHARE: f32 = 0.5;
/// The least a section keeps while a rule is dragged over it: its header
/// and one row (herdr's three-row clamp).
pub const MIN_SECTION_ROWS: u16 = 3;
/// herdr `AGENT_PANEL_HEADER_ROWS`: the rule, the title row, one blank.
pub const RULED_HEADER_ROWS: u16 = 3;
/// Rows the collapsed rail needs before it draws its rules: one machine
/// row, three rules, one row per list and the toggle row.
const RAIL_MIN_ROWS: u16 = 8;

/// Rects the sidebar renderers drew, for `ViewState`.
#[derive(Debug, Clone, Default)]
pub struct SidebarHits {
    /// Machine rows, by machine id.
    pub machines: Vec<(String, Rect)>,
    /// Project cards, by project id (both lines of the card).
    pub projects: Vec<(String, Rect)>,
    /// Worktree rows, by worktree id.
    pub worktrees: Vec<(String, Rect)>,
    /// The `▸`/`▾` cell of each card that has worktrees, by project id.
    pub group_toggles: Vec<(String, Rect)>,
    pub projects_new: Option<Rect>,
    pub projects_menu: Option<Rect>,
    /// Session and agent rows, by attention entry id.
    pub agents: Vec<(String, Rect)>,
    /// Scrollbar lane beside each section that overflowed, by
    /// `SidebarSection::index`.
    pub scrollbars: [Option<Rect>; 4],
    /// The `grouped`/`priority` sort label of the agents header, when the
    /// mouse is captured.
    pub agent_sort: Option<Rect>,
    /// The `«`/`»` collapse toggle cell.
    pub toggle: Option<Rect>,
}

/// Screen rows of the `─` rules above the projects, sessions and agents
/// sections, each drawn only when its section has room for the rule.
pub fn section_divider_ys(area: Rect, sidebar: &SidebarState) -> [Option<u16>; 3] {
    if sidebar.collapsed {
        return collapsed_sections(area).1;
    }
    let sections = expanded_sections(area, sidebar.section_splits);
    std::array::from_fn(|index| {
        let section = sections[index + 1];
        (section.width > 0 && section.height >= RULED_HEADER_ROWS).then_some(section.y)
    })
}

/// Rows of `section`'s header: the title and a blank on the first section,
/// a rule above them on the others.
pub fn header_rows(section: SidebarSection) -> u16 {
    if section == SidebarSection::Machines {
        MACHINES_HEADER_ROWS
    } else {
        RULED_HEADER_ROWS
    }
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

    let sections = expanded_sections(area, chrome.sidebar.section_splits);
    machines::render_machines(frame, sections[0], ws, chrome, &mut hits);
    projects::render_projects(frame, sections[1], ws, chrome, is_navigating, &mut hits);
    for section in [SidebarSection::Sessions, SidebarSection::Agents] {
        let area = sections[section.index()];
        agents::render_agents(frame, area, section, ws, chrome, &mut hits);
    }
    hits.toggle = render_toggle(frame, expanded_toggle_rect(area), "«", p);
    hits
}

/// The collapsed rail: a dot per machine, then the numbered project cards,
/// sessions and agent runs, each list under a `─` rule.
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
        if cards {
            // The rail numbers the cards; worktree rows stay behind them.
            rows.retain(|row| row.kind == RowKind::Project);
        }
        let numbered = section != SidebarSection::Machines;
        let drawn = render_rail_list(frame, rect, &rows, numbered, cards, is_navigating, p);
        match section {
            SidebarSection::Machines => hits.machines = drawn,
            SidebarSection::Projects => hits.projects = drawn,
            SidebarSection::Sessions | SidebarSection::Agents => hits.agents.extend(drawn),
        }
    }
    let buf = frame.buffer_mut();
    for divider_y in dividers.into_iter().flatten() {
        for x in area.x..area.x + area.width.saturating_sub(1) {
            buf[(x, divider_y)].set_symbol("─");
            buf[(x, divider_y)].set_style(Style::default().fg(p.surface_dim));
        }
    }

    hits.toggle = render_toggle(frame, collapsed_toggle_rect(area), "»", p);
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
/// worktrees, the interactive sessions or the agent runs.
pub fn section_rows<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    section: SidebarSection,
) -> Vec<SidebarRow> {
    match section {
        SidebarSection::Machines => machine_rows(ws, chrome),
        SidebarSection::Projects => project_rows(ws, chrome),
        SidebarSection::Sessions | SidebarSection::Agents => agent_rows(ws, chrome, section),
    }
}

/// Scroll metrics of `section` as the last frame laid it out: its rows
/// against its body height and the section's scroll position.
pub fn section_metrics<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    section: SidebarSection,
) -> ScrollMetrics {
    let area =
        expanded_sections(chrome.view.sidebar_rect, chrome.sidebar.section_splits)[section.index()];
    let heights: Vec<u16> = section_rows(ws, chrome, section)
        .iter()
        .map(SidebarRow::height)
        .collect();
    project_list_metrics(
        &heights,
        section_body_rect(section, area, false).height,
        chrome.sidebar.scroll(section),
    )
}

/// Draw `section`'s header into the top of `area`: the `─` rule on every
/// section but the first, then the bold title. `None` when the area is
/// too short for the header, in which case the section draws nothing.
fn render_header(
    frame: &mut Frame,
    area: Rect,
    section: SidebarSection,
    p: &Palette,
) -> Option<Rect> {
    if area.width == 0 || area.height < header_rows(section) {
        return None;
    }
    let mut y = area.y;
    if section != SidebarSection::Machines {
        frame.render_widget(
            Paragraph::new(Span::styled(
                "─".repeat(usize::from(area.width)),
                Style::default().fg(p.surface_dim),
            )),
            Rect::new(area.x, y, area.width, 1),
        );
        y += 1;
    }
    let title_row = Rect::new(area.x, y, area.width, 1);
    frame.render_widget(
        Paragraph::new(Span::styled(
            section.title(),
            Style::default().fg(p.overlay0).add_modifier(Modifier::BOLD),
        )),
        title_row,
    );
    Some(title_row)
}

/// Draw `rows` as `section`'s list under its header, packed from the
/// section's scroll position and clipped to whole rows, with the scrollbar
/// lane when they overflow. Returns the row rects by id and the lane.
fn render_section_rows(
    frame: &mut Frame,
    area: Rect,
    section: SidebarSection,
    rows: &[SidebarRow],
    chrome: &Chrome,
) -> (Vec<(String, Rect)>, Option<Rect>) {
    let p = &chrome.palette;
    let heights: Vec<u16> = rows.iter().map(SidebarRow::height).collect();
    let viewport = section_body_rect(section, area, false).height;
    let metrics = project_list_metrics(&heights, viewport, chrome.sidebar.scroll(section));
    let has_scrollbar = should_show_scrollbar(metrics);
    let body = section_body_rect(section, area, has_scrollbar);
    let mut drawn = Vec::new();
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
            let row_style = if row.active {
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
            drawn.push((row.id.clone(), Rect::new(body.x, y, body.width, height)));
            y += height;
        }
    }
    let lane = has_scrollbar.then(|| {
        let track = scrollbar_track(area, body);
        render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
        track
    });
    (drawn, lane)
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

/// Where each section starts in a `total_h`-row sidebar, as row offsets
/// with the total last: `bounds[i]..bounds[i + 1]` is section `i`. A
/// dragged rule (`splits[i]`, the first row of section `i + 1`) is kept
/// within `MIN_SECTION_ROWS` of its neighbours; an unset one takes the
/// default share. Under four times the minimum the rows are shared evenly,
/// the remainder to the top.
pub fn section_bounds(total_h: u16, splits: [Option<u16>; 3]) -> [u16; 5] {
    let count = SidebarSection::ALL.len() as u16;
    let mut bounds = [0, 0, 0, 0, total_h];
    if total_h < MIN_SECTION_ROWS * count {
        let (share, extra) = (total_h / count, total_h % count);
        for index in 0..usize::from(count) {
            bounds[index + 1] = bounds[index] + share + u16::from((index as u16) < extra);
        }
        return bounds;
    }
    for divider in 0..3 {
        let default = match divider {
            0 => DEFAULT_MACHINES_ROWS,
            1 => {
                let rest = f32::from(total_h - bounds[1]);
                bounds[1] + (rest * DEFAULT_PROJECTS_SHARE).round() as u16
            }
            _ => bounds[2] + (total_h - bounds[2]) / 2,
        };
        let (low, high) = divider_limits(&bounds, total_h, divider);
        bounds[divider + 1] = splits[divider].unwrap_or(default).clamp(low, high);
    }
    bounds
}

/// The rows rule `divider` may start its section on, given the rules
/// above it: past the section above's minimum and leaving every section
/// below its own.
fn divider_limits(bounds: &[u16; 5], total_h: u16, divider: usize) -> (u16, u16) {
    let below = 3 - divider as u16;
    (
        bounds[divider] + MIN_SECTION_ROWS,
        total_h - MIN_SECTION_ROWS * below,
    )
}

/// Where rule `divider` may be dragged to in a `total_h`-row sidebar, as
/// row offsets; `None` while the sidebar is too short to drag its rules.
pub fn split_range(
    total_h: u16,
    splits: [Option<u16>; 3],
    divider: usize,
) -> Option<RangeInclusive<u16>> {
    if total_h < MIN_SECTION_ROWS * SidebarSection::ALL.len() as u16 {
        return None;
    }
    let (low, high) = divider_limits(&section_bounds(total_h, splits), total_h, divider);
    Some(low..=high)
}

/// The four section rects of the expanded sidebar, top to bottom, without
/// the separator column.
pub fn expanded_sections(area: Rect, splits: [Option<u16>; 3]) -> [Rect; 4] {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    if content.width == 0 || content.height == 0 {
        return [Rect::default(); 4];
    }
    let bounds = section_bounds(content.height, splits);
    std::array::from_fn(|index| {
        Rect::new(
            content.x,
            content.y + bounds[index],
            content.width,
            bounds[index + 1] - bounds[index],
        )
    })
}

/// The rail's four lists and the rules between them: one row of machine
/// dots, then the cards, sessions and agent runs sharing the rest, the
/// remainder to the cards, above the toggle row. Under `RAIL_MIN_ROWS` the
/// cards take every row and no rule is drawn.
pub fn collapsed_sections(area: Rect) -> ([Rect; 4], [Option<u16>; 3]) {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    let mut rects = [Rect::default(); 4];
    let mut dividers = [None; 3];
    if content.width == 0 || content.height == 0 {
        return (rects, dividers);
    }
    if content.height < RAIL_MIN_ROWS {
        rects[SidebarSection::Projects.index()] = content;
        return (rects, dividers);
    }
    // The machine row, three rules and the toggle row come off the top.
    let rest = content.height - 5;
    let (share, extra) = (rest / 3, rest % 3);
    let heights = [
        1,
        share + u16::from(extra > 0),
        share + u16::from(extra > 1),
        share,
    ];
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

/// The rows `section`'s list draws into inside its `area`: under the
/// header and, for the projects, above the footer row.
pub fn section_body_rect(section: SidebarSection, area: Rect, has_scrollbar: bool) -> Rect {
    let header = header_rows(section);
    if area.width == 0 || area.height <= header {
        return Rect::default();
    }
    let body_y = area.y + header;
    let bottom = if section == SidebarSection::Projects {
        area.bottom().saturating_sub(1)
    } else {
        area.bottom()
    };
    Rect::new(
        area.x,
        body_y,
        area.width.saturating_sub(u16::from(has_scrollbar)),
        bottom.saturating_sub(body_y),
    )
}

/// The `«` cell: last row, just left of the separator column.
pub fn expanded_toggle_rect(area: Rect) -> Rect {
    if area.width <= 1 || area.height == 0 {
        return Rect::default();
    }
    Rect::new(
        area.x + area.width.saturating_sub(2),
        area.y + area.height.saturating_sub(1),
        1,
        1,
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
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crate::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use serde_json::json;

    /// Two projects, `alpha` (focused, on `main`, one worktree) and `beta`,
    /// plus one attention entry on `term-alpha`.
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

    fn screen(terminal: &Terminal<TestBackend>) -> String {
        let buffer = terminal.backend().buffer();
        let width = usize::from(buffer.area.width);
        let cells: Vec<String> = buffer
            .content()
            .iter()
            .map(|c| c.symbol().to_string())
            .collect();
        cells
            .chunks(width)
            .map(|row| row.concat())
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn section_bounds_follow_herdr_clamps() {
        // Too short for four headers: even shares, the remainder on top.
        assert_eq!(section_bounds(0, [None; 3]), [0, 0, 0, 0, 0]);
        assert_eq!(section_bounds(5, [None; 3]), [0, 2, 3, 4, 5]);
        assert_eq!(section_bounds(11, [Some(9); 3]), [0, 3, 6, 9, 11]);
        // Defaults: four machine rows, half the rest to the cards, the
        // remainder halved between sessions and agents.
        assert_eq!(section_bounds(40, [None; 3]), [0, 4, 22, 31, 40]);
        assert_eq!(section_bounds(16, [None; 3]), [0, 4, 10, 13, 16]);
        // Exactly four minimums: every section gets its three rows.
        assert_eq!(section_bounds(12, [None; 3]), [0, 3, 6, 9, 12]);
        // A dragged rule holds its row; the ones under it keep their share
        // of what is left and every section its header and one row.
        assert_eq!(
            section_bounds(40, [Some(6), None, None]),
            [0, 6, 23, 31, 40]
        );
        assert_eq!(
            section_bounds(40, [None, Some(30), None]),
            [0, 4, 30, 35, 40]
        );
        assert_eq!(
            section_bounds(40, [None, Some(50), Some(50)]),
            [0, 4, 34, 37, 40]
        );
        assert_eq!(
            section_bounds(40, [Some(0), Some(0), Some(0)]),
            [0, 3, 6, 9, 40]
        );
        assert_eq!(split_range(40, [None; 3], 0), Some(3..=31));
        assert_eq!(split_range(40, [None; 3], 1), Some(7..=34));
        assert_eq!(split_range(40, [None, Some(30), None], 2), Some(33..=37));
        assert_eq!(split_range(11, [None; 3], 0), None);
    }

    #[test]
    fn expanded_sidebar_lists_the_four_sections_with_hits() {
        let ws = scripted_workspace();
        let mut chrome = Chrome::dark();
        chrome.mode = Mode::Navigate;
        chrome.sidebar.selected = 1;
        let area = Rect::new(0, 0, 26, 40);
        let mut terminal = Terminal::new(TestBackend::new(26, 40)).unwrap();
        let mut hits = SidebarHits::default();
        terminal
            .draw(|frame| hits = render_sidebar(frame, area, &ws, &chrome))
            .unwrap();
        let text = screen(&terminal);
        for needle in [
            " machines",
            "· local",
            " projects",
            "● alpha",
            "   main ↑2",
            "feature · #123",
            "○ beta",
            " sessions",
            " agents",
            "term-alpha",
            "blocked",
            " new",
            "menu│",
        ] {
            assert!(text.contains(needle), "missing {needle:?}:\n{text}");
        }
        assert!(!text.contains('!'), "{text}");
        // The machine row carries its blocked agent's state and the local
        // mark; its label is this host's name.
        assert_eq!(hits.machines.len(), 1, "{:?}", hits.machines);
        assert_eq!(hits.machines[0].1, Rect::new(0, 2, 25, 1));
        assert_eq!(
            hits.projects,
            vec![
                ("proj-alpha".to_string(), Rect::new(0, 7, 25, 2)),
                ("proj-beta".to_string(), Rect::new(0, 10, 25, 2)),
            ]
        );
        assert_eq!(
            hits.worktrees,
            vec![("wt-1".to_string(), Rect::new(0, 9, 25, 1))]
        );
        assert_eq!(
            hits.group_toggles,
            vec![("proj-alpha".to_string(), Rect::new(24, 7, 1, 1))]
        );
        assert_eq!(hits.projects_new, Some(Rect::new(0, 21, 4, 1)));
        assert_eq!(hits.projects_menu, Some(Rect::new(21, 21, 4, 1)));
        // The `run:` entry is an agent run: the sessions list stays empty.
        assert_eq!(
            hits.agents,
            vec![("run:term-alpha".to_string(), Rect::new(0, 34, 25, 2))]
        );
        assert_eq!(hits.scrollbars, [None; 4]);
        let lines: Vec<&str> = text.lines().collect();
        assert!(lines[2].starts_with(" ● "), "{:?}", lines[2]);
        for rule in [4, 22, 31] {
            assert!(lines[rule].starts_with("────"), "{:?}", lines[rule]);
        }
        assert!(lines[7].ends_with("▾│"), "{:?}", lines[7]);
        assert!(lines[9].starts_with("▸  └─ ○ feature"), "{:?}", lines[9]);
        assert_eq!(lines[32], " agents           grouped│");
    }

    #[test]
    fn collapsed_rail_shows_indexes_and_dots() {
        let ws = scripted_workspace();
        let chrome = Chrome::dark();
        let area = Rect::new(0, 0, 4, 12);
        let mut terminal = Terminal::new(TestBackend::new(4, 12)).unwrap();
        let mut hits = SidebarHits::default();
        terminal
            .draw(|frame| hits = render_collapsed_sidebar(frame, area, &ws, &chrome))
            .unwrap();
        let text = screen(&terminal);
        let lines: Vec<&str> = text.lines().collect();
        // The machine dot, then the cards, sessions and agent runs under
        // their rules, and the toggle on the last row.
        assert_eq!(lines[0], "  ●│");
        assert_eq!(lines[1], "───│");
        assert_eq!(lines[2], "1 ●│");
        assert_eq!(lines[3], "2 ○│");
        assert_eq!(lines[4], "   │");
        assert_eq!(lines[5], "───│");
        assert_eq!(lines[6], "   │");
        assert_eq!(lines[8], "───│");
        assert_eq!(lines[9], "1 ●│");
        assert_eq!(lines[11], " » │");
        assert_eq!(hits.machines.len(), 1);
        assert_eq!(hits.projects.len(), 2);
        assert!(hits.worktrees.is_empty());
        assert_eq!(hits.agents.len(), 1);
        assert_eq!(hits.agents[0].1, Rect::new(0, 9, 3, 1));
    }

    #[test]
    fn rail_sections_share_the_rows_under_the_machine_dot() {
        let (rects, dividers) = collapsed_sections(Rect::new(0, 0, 4, 12));
        assert_eq!(dividers, [Some(1), Some(5), Some(8)]);
        assert_eq!(
            rects,
            [
                Rect::new(0, 0, 3, 1),
                Rect::new(0, 2, 3, 3),
                Rect::new(0, 6, 3, 2),
                Rect::new(0, 9, 3, 2),
            ]
        );
        // Under eight rows the cards take everything and no rule is drawn.
        let (rects, dividers) = collapsed_sections(Rect::new(0, 0, 4, 7));
        assert_eq!(dividers, [None; 3]);
        assert_eq!(
            rects[SidebarSection::Projects.index()],
            Rect::new(0, 0, 3, 7)
        );
        assert_eq!(rects[SidebarSection::Agents.index()], Rect::default());
    }

    #[test]
    fn list_scroll_clamps_to_the_last_page() {
        let metrics = list_metrics(10, 4, 99);
        assert_eq!(metrics.max_offset_from_bottom, 6);
        assert_eq!(metrics.offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 4);
        assert!(!should_show_scrollbar(list_metrics(3, 4, 0)));
    }
}
